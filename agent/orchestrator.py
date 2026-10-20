import json
import logging
import os
import posixpath
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from agent import codex_client, github_client, sandbox_runner, slack_notify
from agent.edits import changed_line_count, unified_diff
from agent.events import RunEvents

load_dotenv()

logger = logging.getLogger("devloop.orchestrator")

GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "main")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
DEMO_REPO = os.getenv("DEMO_REPO") or "manjunath4518/devloop-demo-app"
RUNS_LOG_PATH = Path("devloop_runs.json")
MAX_FIX_ATTEMPTS = max(1, int(os.getenv("MAX_FIX_ATTEMPTS", "3")))


def _legacy_log(run_record: dict) -> None:
    runs = []
    if RUNS_LOG_PATH.exists():
        try:
            runs = json.loads(RUNS_LOG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            runs = []
    runs.append(run_record)
    try:
        RUNS_LOG_PATH.write_text(json.dumps(runs, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write legacy run log: %s", e)


def _is_db_user(user_id: str) -> bool:
    """Only real Supabase users have UUID ids; 'anonymous', 'anon_*' and 'gh_*' do not."""
    try:
        uuid.UUID(user_id)
        return True
    except (ValueError, TypeError):
        return False


def _get_user_config(user_id: str, repo_override: str = None, demo: bool = False) -> dict:
    """Fetch per-user GitHub token + Slack webhook. Repo from override, demo, or first user_repo."""
    # Triggering the demo repo from the repo list is still a demo run (server token)
    if repo_override and repo_override.lower() == DEMO_REPO.lower():
        demo = True
    github_token = os.getenv("GITHUB_TOKEN", "")
    slack_webhook_url = SLACK_WEBHOOK_URL
    repo = repo_override or (GITHUB_REPO if not demo else DEMO_REPO)
    base_branch = GITHUB_BASE_BRANCH
    sentry_secret = None
    user = None

    if not _is_db_user(user_id):
        return {
            "github_token": github_token,
            "github_repo": repo,
            "github_base_branch": base_branch,
            "slack_webhook_url": slack_webhook_url,
            "sentry_secret": None,
            "user_openrouter_key": None,
            "user_model": None,
        }

    try:
        from db.users import get_user
        from db.repos import list_repos, get_repo
        user = get_user(user_id)
        if user:
            # Demo repo is owned by us, so a visitor's own token usually can't push to it
            if not demo:
                github_token = user.get("github_token") or github_token
            slack_webhook_url = user.get("slack_webhook_url") or slack_webhook_url

        if not repo_override and not demo:
            repos = list_repos(user_id)
            if repos:
                first = repos[0]
                repo = first["repo"]
                base_branch = first.get("base_branch", "main")
                sentry_secret = first.get("sentry_secret")
        elif repo_override:
            repo_record = get_repo(user_id, repo_override)
            if repo_record:
                base_branch = repo_record.get("base_branch", "main")
                sentry_secret = repo_record.get("sentry_secret")

    except Exception as e:
        logger.warning("Could not fetch user config from Supabase: %s", e)

    user_openrouter_key = None
    user_model = None
    if user:
        user_openrouter_key = user.get("user_openrouter_key") or None
        user_model = user.get("preferred_model") or None

    return {
        "github_token": github_token,
        "github_repo": repo,
        "github_base_branch": base_branch,
        "slack_webhook_url": slack_webhook_url,
        "sentry_secret": sentry_secret,
        "user_openrouter_key": user_openrouter_key,
        "user_model": user_model,
    }


def _test_context(tree: list[str], source_path: str, repo: str, ref: str, token: str) -> str:
    """Short description of where tests live + how they import, so the LLM writes a runnable test."""
    tests = [p for p in tree if p.endswith(".py") and posixpath.basename(p).startswith("test")]
    if not tests:
        return ("No existing tests. Put the test in a tests/ folder next to the source package "
                "and import the module by its package path.")
    src_parts = posixpath.dirname(source_path).split("/")

    def closeness(path: str) -> int:
        parts = posixpath.dirname(path).split("/")
        n = 0
        for a, b in zip(parts, src_parts):
            if a != b:
                break
            n += 1
        return -n

    tests.sort(key=lambda p: (closeness(p), p))
    lines = ["Existing test files:"] + [f"- {p}" for p in tests[:15]]
    try:
        sample = github_client.fetch_file(repo, tests[0], ref, token=token)
        head = "\n".join(sample.splitlines()[:20])
        lines += ["", f"First lines of {tests[0]}:", head]
    except Exception as e:
        logger.warning("Could not fetch sample test %s: %s", tests[0], e)
    return "\n".join(lines)


def _test_event(ev: RunEvents, phase: str, attempt: int, result: dict | None, run: dict) -> None:
    ev.emit(
        "test", phase=phase, attempt=attempt, backend=run.get("backend"), url=run.get("url"),
        passed=bool(result and result["passed"]),
        exit_code=result["exit_code"] if result else None,
        output=(result["output"][-2000:] if result else (run.get("error") or "")[-2000:]),
        duration=result["duration"] if result else None,
    )


def run_orchestrator(parsed_error: dict, user_id: str = "anonymous", demo: bool = False) -> None:
    """Blocking pipeline; main.py runs it in a worker thread."""
    started = time.monotonic()
    error_message = parsed_error["error_message"]
    stack_trace = parsed_error["stack_trace"]
    filename = parsed_error["filename"]
    lineno = parsed_error.get("lineno") if isinstance(parsed_error.get("lineno"), int) else None
    environment = parsed_error.get("environment", "production")
    run_key = parsed_error.get("_run_key") or uuid.uuid4().hex
    ev = RunEvents(user_id, run_key, prior=parsed_error.get("_prior_events"))
    usage = codex_client.TokenUsage()

    # ── Supabase run record ──────────────────────────────────────────────────
    run_id = None
    try:
        if not _is_db_user(user_id):
            raise ValueError(f"no DB user for {user_id}")
        from db.runs import create_run, update_run, append_log as db_log

        def log_to_db(level: str, msg: str):
            if run_id:
                try:
                    db_log(run_id, user_id, level, msg)
                except Exception:
                    pass

        run_id = create_run(user_id, error_message, filename, environment)
    except Exception as e:
        logger.warning("Supabase unavailable, running without DB: %s", e)

        def log_to_db(level: str, msg: str):
            pass

    # Legacy fallback record
    run_record = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "error_message": error_message,
        "filename": filename,
        "environment": environment,
        "status": "failed",
        "pr_url": None,
        "test_passed": None,
        "user_id": user_id,
    }

    def info(msg, *args):
        full = msg % args if args else msg
        logger.info(full)
        log_to_db("INFO", full)

    def error(msg, *args):
        full = msg % args if args else msg
        logger.error(full)
        log_to_db("ERROR", full)

    def warning(msg, *args):
        full = msg % args if args else msg
        logger.warning(full)
        log_to_db("WARNING", full)

    summary: dict = {}

    def finish(status: str, **fields) -> None:
        duration = time.monotonic() - started
        summary.update(fields)
        ev.emit("done", status=status, duration=round(duration, 1),
                tokens=usage.total, llm_calls=usage.calls, **summary)
        run_record.update({
            "status": status, "pr_url": summary.get("pr_url"),
            "test_passed": summary.get("test_passed"), "branch": summary.get("branch"),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        _legacy_log(run_record)
        if run_id:
            try:
                update_run(
                    run_id, status=status,
                    pr_url=summary.get("pr_url"), test_passed=summary.get("test_passed"),
                    branch=summary.get("branch"),
                    root_cause=summary.get("root_cause"), fix_summary=summary.get("fix_summary"),
                    diff=summary.get("diff"), attempts=summary.get("attempts"),
                    reproduced=summary.get("reproduced"), duration_seconds=round(duration, 1),
                    tokens_used=usage.total, events=ev.events,
                )
            except Exception as e:
                warning("Could not update Supabase run record: %s", e)

    def fail(step: str, msg: str) -> None:
        ev.step(step, "failed", detail=msg)
        error("❌ %s", msg)
        finish("failed", failed_step=step, reason=msg)

    # ── Get user-specific config ─────────────────────────────────────────────
    repo_override = parsed_error.get("_repo_override") or parsed_error.get("_demo_repo")
    config = _get_user_config(user_id, repo_override=repo_override, demo=demo)
    repo = config["github_repo"]
    base_branch = parsed_error.get("_base_branch") or config["github_base_branch"]
    github_token = config["github_token"]
    slack_webhook = config["slack_webhook_url"]
    user_openrouter_key = config.get("user_openrouter_key")
    user_model = config.get("user_model")

    ev.emit("run", error=error_message, filename=filename, repo=repo, base=base_branch,
            environment=environment, demo=demo,
            started_at=datetime.now(timezone.utc).isoformat())

    info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    info("🔧 DevLoop pipeline started")
    info("   Error : %s", error_message)
    info("   File  : %s", filename)
    info("   Env   : %s  |  Repo: %s@%s", environment, repo or "(not set)", base_branch)
    info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if not repo:
        return fail("fetch", f"No GitHub repo configured for user {user_id}")

    # Step 1: Fetch file + repo layout
    info("📥 [1/6] Fetching %s @ %s...", filename, base_branch)
    ev.step("fetch", "running")
    try:
        source = github_client.fetch_file(repo, filename, base_branch, token=github_token)
    except Exception as e:
        return fail("fetch", f"Failed to fetch {filename} from GitHub: {e}")
    try:
        tree = github_client.get_file_tree(repo, base_branch, token=github_token)
    except Exception as e:
        warning("   Could not list repo files: %s", e)
        tree = []
    test_context = _test_context(tree, filename, repo, base_branch, github_token)
    info("   ✓ Fetched %d bytes (%d lines)", len(source), len(source.splitlines()))
    ev.step("fetch", "done", detail=f"{len(source.splitlines())} lines")

    # Step 2: Analyze
    info("🧠 [2/6] Analyzer LLM: reading error + stack trace...")
    ev.step("analyze", "running")
    try:
        analysis = codex_client.analyze(error_message, stack_trace, filename, usage,
                                        user_key=user_openrouter_key, user_model=user_model)
    except Exception as e:
        return fail("analyze", f"Analysis failed: {e}")
    info("   ✓ Root cause: %s", analysis["root_cause"])
    info("   ✓ Buggy line: %s", analysis.get("buggy_line", "?"))
    ev.emit("analysis", root_cause=analysis["root_cause"], buggy_line=analysis.get("buggy_line", ""),
            fix_strategy=analysis.get("fix_strategy", ""))
    ev.step("analyze", "done")
    summary["root_cause"] = analysis["root_cause"]

    # Step 3: Fix + regression test (search/replace edits, not a whole file)
    info("🛠  [3/6] Fixer LLM: writing patch + regression test...")
    ev.step("fix", "running", attempt=1)
    try:
        proposal = codex_client.propose_fix(
            analysis, filename, source, lineno, test_context, set(tree), usage,
            user_key=user_openrouter_key, user_model=user_model,
        )
    except Exception as e:
        return fail("fix", f"Fix generation failed: {e}")
    files = proposal["files"]
    test_path = proposal["test_path"]
    fix_summary = proposal["fix_summary"]

    def patch_diff() -> str:
        return unified_diff(filename, source, files[filename]) + unified_diff(test_path, "", files[test_path])

    changed = changed_line_count(source, files[filename])
    info("   ✓ %d line(s) changed in %s, regression test %s", changed, filename, test_path)
    info("   ✓ Fix summary: %s", fix_summary)
    ev.emit("patch", attempt=1, diff=patch_diff(), summary=fix_summary, lines_changed=changed)
    ev.step("fix", "done", attempt=1)

    # Step 4: Reproduce (red) + verify (green), self-healing on failure
    backend = sandbox_runner.resolve_backend()
    red = None
    reproduced = None
    attempts: list[dict] = []
    green_passed = None
    sandbox_url = None

    def on_status(status: str, url: str | None) -> None:
        nonlocal sandbox_url
        sandbox_url = url or sandbox_url
        ev.step("verify", "running", attempt=len(attempts) + 1, detail=f"{backend}: {status}", url=sandbox_url)

    if backend == "none":
        warning("⚠️  [4/6] No test runner available — skipping tests (set TEST_RUNNER=actions or docker)")
        ev.step("reproduce", "skipped", detail="no test runner")
        ev.step("verify", "skipped", detail="no test runner")
        attempts.append({"attempt": 1, "summary": fix_summary, "result": None})
    else:
        info("🧪 [4/6] Running tests via %s: reproduce bug, then verify fix...", backend)
        ev.step("reproduce", "running", detail=backend)
        ev.step("verify", "running", attempt=1, detail=backend)
        run = sandbox_runner.run_phases(
            repo, base_branch, github_token,
            [
                {"name": "red", "files": {test_path: files[test_path]}, "target": test_path},
                {"name": "green1", "files": dict(files)},
            ],
            on_status=on_status, backend=backend,
        )
        if run.get("error"):
            warning("   Sandbox: %s", run["error"][:500])
        red = run["phases"].get("red")
        reproduced = bool(red and red["exit_code"] == 1)
        _test_event(ev, "red", 1, red, run)
        if reproduced:
            info("   🔴 Regression test fails on the original code — bug reproduced")
            ev.step("reproduce", "done", detail="test fails on original code")
        else:
            warning("   ⚠️  Regression test did not fail on the original code (exit %s)",
                    red["exit_code"] if red else "n/a")
            ev.step("reproduce", "warning", detail="test did not reproduce the bug")

        green = run["phases"].get("green1")
        attempts.append({"attempt": 1, "summary": fix_summary, "result": green})
        _test_event(ev, "green", 1, green, run)

        while not (green and green["passed"]) and len(attempts) < MAX_FIX_ATTEMPTS:
            n = len(attempts) + 1
            failure = (green["output"] if green else run.get("error")) or "No test output"
            info("   ❌ Attempt %d failed — feeding test output back to the fixer (attempt %d/%d)",
                 n - 1, n, MAX_FIX_ATTEMPTS)
            ev.step("verify", "retrying", attempt=n - 1)
            ev.step("fix", "running", attempt=n)
            try:
                refined = codex_client.refine_fix(
                    analysis, files, filename, test_path, failure, usage,
                    user_key=user_openrouter_key, user_model=user_model,
                )
            except Exception as e:
                warning("   Refinement failed, keeping attempt %d: %s", n - 1, e)
                ev.step("fix", "failed", attempt=n, detail=str(e)[:200])
                break
            files = refined["files"]
            fix_summary = refined["fix_summary"] or fix_summary
            changed = changed_line_count(source, files[filename])
            info("   ✓ Attempt %d patch: %s", n, fix_summary)
            ev.emit("patch", attempt=n, diff=patch_diff(), summary=fix_summary, lines_changed=changed)
            ev.step("fix", "done", attempt=n)
            ev.step("verify", "running", attempt=n, detail=backend)
            run = sandbox_runner.run_phases(
                repo, base_branch, github_token,
                [{"name": f"green{n}", "files": dict(files)}],
                on_status=on_status, backend=backend,
            )
            green = run["phases"].get(f"green{n}")
            attempts.append({"attempt": n, "summary": fix_summary, "result": green})
            _test_event(ev, "green", n, green, run)

        green_passed = bool(green and green["passed"])
        if green_passed:
            info("   ✅ Tests PASS on attempt %d", len(attempts))
            ev.step("verify", "done", attempt=len(attempts), url=sandbox_url)
        else:
            warning("   ⚠️  Tests still failing after %d attempt(s) — PR will be a draft", len(attempts))
            ev.step("verify", "failed", attempt=len(attempts), url=sandbox_url)

    summary.update(fix_summary=fix_summary, diff=patch_diff(), attempts=len(attempts),
                   reproduced=reproduced, test_passed=green_passed, test_path=test_path,
                   lines_changed=changed, file_lines=len(source.splitlines()))

    # Step 5: Branch + single commit (fix + regression test) + PR
    branch_name = github_client.make_branch_name()
    info("🌿 [5/6] Committing fix + test to %s and opening PR → %s", branch_name, base_branch)
    ev.step("pr", "running")
    try:
        github_client.create_branch(repo, branch_name, base_branch=base_branch, token=github_token)
        github_client.commit_files(
            repo, branch_name,
            {filename: files[filename], test_path: files[test_path]},
            f"fix: auto-patch by DevLoop\n\n{fix_summary}\n\nRegression test: {test_path}",
            token=github_token,
        )
        title_prefix = "fix" if green_passed is not False else "[draft] fix"
        pr_body = github_client.build_pr_body(
            error_message=error_message, filename=filename, analysis=analysis,
            fix_summary=fix_summary, test_path=test_path, red=red, attempts=attempts,
            backend=backend, tokens_used=usage.total, duration=time.monotonic() - started,
        )
        pr_url = github_client.open_pr(
            repo, branch_name, f"{title_prefix}: DevLoop auto-patch — {error_message[:80]}", pr_body,
            base_branch=base_branch, token=github_token, draft=green_passed is False,
        )
    except Exception as e:
        return fail("pr", f"Failed to open PR: {e}")
    info("   ✓ PR opened: %s", pr_url)
    ev.step("pr", "done", url=pr_url, detail="draft" if green_passed is False else None)
    summary.update(pr_url=pr_url, branch=branch_name, draft=green_passed is False)

    # Step 6: Slack
    info("📣 [6/6] Sending Slack notification...")
    ev.step("notify", "running")
    try:
        slack_notify.send_notification(
            error_message=error_message,
            filename=filename,
            test_results={"passed": bool(green_passed)},
            pr_url=pr_url,
            webhook_url=slack_webhook,
        )
        ev.step("notify", "done" if (slack_webhook or slack_notify.SLACK_WEBHOOK_URL) else "skipped")
    except Exception as e:
        warning("   ⚠️  Slack notification failed (non-fatal): %s", e)
        ev.step("notify", "failed", detail=str(e)[:200])

    finish("success")
    info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    info("✅ DevLoop complete in %.0fs — %s", time.monotonic() - started, pr_url)
    info("   Tests : %s  |  Attempts: %d  |  LLM tokens: %d",
         "PASS ✅" if green_passed else ("not run" if green_passed is None else "FAIL ⚠️"),
         len(attempts), usage.total)
    info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
