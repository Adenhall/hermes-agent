"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": [],
             "run_name": "required", "app_id": 1}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                        {"context": "required", "app": {"databaseId": 1}}]}}}}}}
            elif "/rules/branches/" in self.path:
                if state.get("rules_forbidden"):
                    self.send_error(403)
                    return
                value = [[]]
            elif "/check-runs" in self.path:
                if state.get("checks_forbidden"):
                    self.send_error(403)
                    return
                run = {"id": 42, "name": state["run_name"], "head_sha": sha,
                       "app": {"id": state["app_id"]}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.linux_only
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.linux_only
def test_explicit_named_policy_is_persisted_and_fail_closed(github):
    with connect() as conn:
        with pytest.raises(ValueError, match="non-empty"):
            kb.create_task(conn, title="empty", completion_contract="acme/repo",
                           completion_checks=[])

        # Explicit mode is deliberately owner-specified: unreadable repository
        # policy is not consulted and an unrelated green check is never inferred.
        github.update(rules_forbidden=True, conclusion="success", run_name="optional",
                      app_id=1, head="a" * 40)
        named = kb.create_task(conn, title="named", completion_contract="acme/repo",
            completion_checks=[{"context": "required", "app_id": 1}])
        assert kb.get_task(conn, named).completion_checks == [
            {"context": "required", "app_id": 1}]
        assert not kb.complete_task(conn, named,
            metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'",
            (named,)).fetchone()[0])
        assert receipt["policy_source"] == "explicit"
        assert receipt["required"] == [{"context": "required", "app_id": 1}]
        assert receipt["classification"] == "missing"
        assert not any("/rules/branches/" in request for request in github["requests"])

        # Exact app identity and every non-success conclusion remain closed.
        for conclusion in ("failure", "pending", "cancelled", "timed_out",
                           "action_required", "neutral", "skipped", None):
            github.update(conclusion=conclusion, run_name="required", app_id=1,
                          head="a" * 40)
            tid = kb.create_task(conn, title=str(conclusion), completion_contract="acme/repo",
                completion_checks=[{"context": "required", "app_id": 1}])
            assert not kb.complete_task(conn, tid,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        github.update(conclusion="success", app_id=2)
        wrong_app = kb.create_task(conn, title="wrong app", completion_contract="acme/repo",
            completion_checks=[{"context": "required", "app_id": 1}])
        assert not kb.complete_task(conn, wrong_app,
            metadata={"published_pr": "https://github.com/acme/repo/pull/7"})

        # Check API errors are not reclassified as success, while a complete
        # exact-head result for the declared name/app can complete.
        github.update(app_id=1, checks_forbidden=True)
        unavailable = kb.create_task(conn, title="unavailable", completion_contract="acme/repo",
            completion_checks=[{"context": "required", "app_id": 1}])
        assert not kb.complete_task(conn, unavailable,
            metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        unavailable_receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'",
            (unavailable,)).fetchone()[0])
        assert unavailable_receipt["classification"] == "infra"
        assert unavailable_receipt["phase"] == "check_runs"
        github.pop("checks_forbidden")
        github["head_change"] = True
        changed = kb.create_task(conn, title="changed", completion_contract="acme/repo",
            completion_checks=[{"context": "required", "app_id": 1}])
        assert not kb.complete_task(conn, changed,
            metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        changed_receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'",
            (changed,)).fetchone()[0])
        assert changed_receipt["classification"] == "stale"
        github.pop("head_change")
        github["head"] = "a" * 40
        accepted = kb.create_task(conn, title="accepted", completion_contract="acme/repo",
            completion_checks=[{"context": "required", "app_id": 1}])
        assert kb.complete_task(conn, accepted,
            metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        accepted_receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'",
            (accepted,)).fetchone()[0])
        assert accepted_receipt["ok"] is True
        assert accepted_receipt["policy_source"] == "explicit"
        assert accepted_receipt["checks"][0]["id"] == 42

        # Existing repository-policy mode remains the default and still fails
        # closed when required-check discovery is unreadable.
        repository = kb.create_task(conn, title="repository", completion_contract="acme/repo")
        assert not kb.complete_task(conn, repository,
            metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        repository_receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'",
            (repository,)).fetchone()[0])
        assert repository_receipt["policy_source"] == "repository"
        assert repository_receipt["phase"] == "repository_policy"
        assert repository_receipt["classification"] == "infra"


@pytest.mark.linux_only
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")
