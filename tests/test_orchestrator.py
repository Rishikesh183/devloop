"""Pipeline flow with GitHub, LLM and sandbox faked: red/green, self-heal loop, draft PRs."""
import pytest

from agent import codex_client, github_client, orchestrator, sandbox_runner, slack_notify

SOURCE = "def f(x):\n    return x * None\n"
PARSED = {
    "error_message": "TypeError: boom", "stack_trace": "...", "filename": "app/f.py",
    "lineno": 2, "environment": "production", "_repo_override": "me/app",
}


@pytest.fixture
def fake(monkeypatch):
    calls = {"commits": [], "prs": [], "runs": [], "refines": 0}
    monkeypatch.setattr(orchestrator, "_get_user_config", lambda *a, **k: {
        "github_token": "t", "github_repo": "me/app", "github_base_branch": "main",
        "slack_webhook_url": "", "sentry_secret": None, "user_openrouter_key": None, "user_model": None,
    })
    monkeypatch.setattr(orchestrator, "_legacy_log", lambda r: None)
    monkeypatch.setattr(github_client, "fetch_file", lambda *a, **k: SOURCE if a[1] == "app/f.py" else "import app\n")
    monkeypatch.setattr(github_client, "get_file_tree", lambda *a, **k: ["app/f.py", "tests/test_old.py"])
    monkeypatch.setattr(github_client, "create_branch", lambda *a, **k: a[1])
    monkeypatch.setattr(github_client, "commit_files", lambda repo, br, files, msg, token=None: calls["commits"].append(files) or "sha")
    monkeypatch.setattr(github_client, "open_pr", lambda *a, **k: calls["prs"].append(k) or "https://github.com/me/app/pull/1")
    monkeypatch.setattr(slack_notify, "send_notification", lambda **k: None)
    monkeypatch.setattr(codex_client, "analyze", lambda *a, **k: {
        "root_cause": "multiplies by None", "buggy_line": "return x * None", "fix_strategy": "drop None", "scope": "1 line"})
    monkeypatch.setattr(codex_client, "propose_fix", lambda *a, **k: {
        "files": {"app/f.py": "def f(x):\n    return x * 0\n", "tests/test_devloop_f.py": "def test_f(): ..."},
        "test_path": "tests/test_devloop_f.py", "fix_summary": "first try"})

    def refine(analysis, files, filename, test_path, failure, usage, **k):
        calls["refines"] += 1
        assert "assert 0 == 2" in failure          # test output is fed back
        return {"files": {**files, filename: "def f(x):\n    return x\n"}, "fix_summary": "second try"}

    monkeypatch.setattr(codex_client, "refine_fix", refine)
    monkeypatch.setattr(sandbox_runner, "resolve_backend", lambda: "actions")
    return calls


def _phase(code, out=""):
    return {"passed": code == 0, "exit_code": code, "output": out, "duration": 1.0}


def test_self_heals_on_second_attempt(fake, monkeypatch):
    def run_phases(repo, ref, token, phases, on_status=None, backend=None):
        fake["runs"].append([p["name"] for p in phases])
        if phases[0]["name"] == "red":
            return {"backend": backend, "url": "u", "error": None,
                    "phases": {"red": _phase(1), "green1": _phase(1, "assert 0 == 2")}}
        return {"backend": backend, "url": "u", "error": None, "phases": {"green2": _phase(0)}}

    monkeypatch.setattr(sandbox_runner, "run_phases", run_phases)
    events = []
    monkeypatch.setattr("agent.events._sink", lambda uid, ev: events.append(ev))

    orchestrator.run_orchestrator(dict(PARSED), "anonymous")

    assert fake["runs"] == [["red", "green1"], ["green2"]]   # red+green share one sandbox run
    assert fake["refines"] == 1
    assert fake["commits"][0]["app/f.py"] == "def f(x):\n    return x\n"
    assert "tests/test_devloop_f.py" in fake["commits"][0]  # regression test ships in the PR
    assert fake["prs"][0]["draft"] is False
    done = [e for e in events if e["type"] == "done"][0]
    assert done["status"] == "success" and done["attempts"] == 2 and done["reproduced"] is True
    assert [e["attempt"] for e in events if e["type"] == "patch"] == [1, 2]


def test_gives_up_after_max_attempts_and_opens_draft(fake, monkeypatch):
    monkeypatch.setattr(orchestrator, "MAX_FIX_ATTEMPTS", 2)
    monkeypatch.setattr(sandbox_runner, "run_phases", lambda repo, ref, token, phases, **k: {
        "backend": "actions", "url": None, "error": None,
        "phases": {p["name"]: _phase(1, "assert 0 == 2") for p in phases}})

    orchestrator.run_orchestrator(dict(PARSED), "anonymous")

    assert fake["refines"] == 1
    assert fake["prs"][0]["draft"] is True


def test_no_runner_still_opens_pr(fake, monkeypatch):
    monkeypatch.setattr(sandbox_runner, "resolve_backend", lambda: "none")
    monkeypatch.setattr(sandbox_runner, "run_phases", lambda *a, **k: pytest.fail("should not run"))
    orchestrator.run_orchestrator(dict(PARSED), "anonymous")
    assert fake["prs"][0]["draft"] is False
