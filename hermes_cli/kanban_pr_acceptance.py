"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.
"""
from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=30, check=True)
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


def _pr_required_checks(pr_id: str, sha: str, branch: str, state: str) -> set[tuple[str, int | None]]:
    """Read GitHub's PR-specific requiredness signal with complete pagination."""
    required: set[tuple[str, int | None]] = set()
    seen_nodes = set()
    seen_cursors = set()
    expected_total = None
    after = None
    while True:
        after_clause = "" if after is None else ",after:" + json.dumps(after)
        query = '''{node(id:%s){...on PullRequest{
            id headRefOid baseRefName state
            commits(last:1){nodes{commit{
                oid
                statusCheckRollup{contexts(first:100%s){
                    totalCount
                    pageInfo{hasNextPage endCursor}
                    nodes{
                        __typename
                        ...on StatusContext{
                            id context isRequired(pullRequestId:%s)
                        }
                        ...on CheckRun{
                            databaseId name isRequired(pullRequestId:%s)
                            checkSuite{app{databaseId}}
                        }
                    }
                }}
            }}}
        }}}''' % (json.dumps(pr_id), after_clause, json.dumps(pr_id), json.dumps(pr_id))
        current = _api("graphql", query=query)["data"]["node"]
        if (current["id"], current["headRefOid"], current["baseRefName"], current["state"]) != (
                pr_id, sha, branch, state):
            raise ValueError("PR changed while collecting required checks")
        commits = current["commits"]["nodes"]
        if len(commits) != 1 or commits[0]["commit"]["oid"] != sha:
            raise ValueError("PR status rollup is not bound to the captured head")
        rollup = commits[0]["commit"].get("statusCheckRollup")
        if rollup is None:
            if after is not None:
                raise ValueError("Incomplete status-check pagination")
            return set()
        contexts = rollup["contexts"]
        total = contexts["totalCount"]
        if type(total) is not int or total < 0 or (expected_total is not None and total != expected_total):
            raise ValueError("Invalid status-check total")
        expected_total = total
        for node in contexts["nodes"]:
            kind = node["__typename"]
            is_required = node["isRequired"]
            if not isinstance(is_required, bool):
                raise TypeError("Invalid requiredness signal")
            if kind == "CheckRun":
                check_id = node["databaseId"]
                if type(check_id) is not int:
                    raise TypeError("Invalid check-run identity")
                identity = (kind, check_id)
                context = node["name"]
                app = ((node.get("checkSuite") or {}).get("app") or {})
                app_id = app.get("databaseId")
                if app_id is not None and type(app_id) is not int:
                    raise TypeError("Invalid check-run application identity")
            elif kind == "StatusContext":
                status_id = node["id"]
                if not isinstance(status_id, str) or not status_id:
                    raise TypeError("Invalid status-context identity")
                identity = (kind, status_id)
                context, app_id = node["context"], None
            else:
                raise ValueError("Unknown status-check kind")
            if identity in seen_nodes or not isinstance(context, str) or not context:
                raise ValueError("Invalid or duplicate status-check identity")
            seen_nodes.add(identity)
            if is_required:
                required.add((context, app_id))
        page_info = contexts["pageInfo"]
        has_next = page_info["hasNextPage"]
        if not isinstance(has_next, bool):
            raise TypeError("Invalid status-check pagination")
        if not has_next:
            break
        cursor = page_info["endCursor"]
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ValueError("Invalid status-check cursor")
        seen_cursors.add(cursor)
        after = cursor
    if len(seen_nodes) != expected_total:
        raise ValueError("Incomplete status-check pagination")
    return required


def collect_acceptance(contract: str, published_pr: str | None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    try:
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        query = '''{repository(owner:%s,name:%s){pullRequest(number:%d){id headRefOid baseRefName state
            baseRef{branchProtectionRule{requiredStatusChecks{context app{databaseId}}}}}}}''' % (
                json.dumps(owner), json.dumps(name), number)
        pr = _api("graphql", query=query)["data"]["repository"]["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
            raise ValueError("PR is closed or current head is unavailable")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
        required.update(_pr_required_checks(pr["id"], sha, branch, pr["state"]))
        rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100", paginate=True)
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required:
            receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
            return receipt
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest", paginate=True)
        runs = [run for page in pages for run in page["check_runs"]]
        if len({r["id"] for r in runs}) != pages[0]["total_count"]:
            raise ValueError("Incomplete check-run pagination")
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100", paginate=True) for s in page]
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
            legacy = [s for s in statuses if s["context"] == context] if app_id in (None, -1) else []
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"],
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}")
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra", detail="GitHub acceptance evidence unavailable or incomplete; check gh authentication/API access and retry.")
        return receipt


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
