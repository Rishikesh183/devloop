import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from agent import codex_client, github_client, sandbox_runner, slack_notify

load_dotenv()

logger = logging.getLogger("devloop.orchestrator")

GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "main")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
RUNS_LOG_PATH = Path("devloop_runs.json")


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


def _get_user_config(user_id: str, repo_override: str = None, demo: bool = False) -> dict:
    """Fetch per-user GitHub token + Slack webhook. Repo from override, demo, or first user_repo."""
    github_token = os.getenv("GITHUB_TOKEN", "")
    slack_webhook_url = SLACK_WEBHOOK_URL
    repo = repo_override or (GITHUB_REPO if not demo else "rishikesh183/devloop-demo-app")
    base_branch = GITHUB_BASE_BRANCH
    sentry_secret = None

    try:
        from db.users import get_user
        from db.repos import list_repos, get_repo
        user = get_user(user_id)
        if user:
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

    return {
        "github_token": github_token,
        "github_repo": repo,
        "github_base_branch": base_branch,
        "slack_webhook_url": slack_webhook_url,
        "sentry_secret": sentry_secret,
    }


async def run_orchestrator(parsed_error: dict, user_id: str = "anonymous", demo: bool = False) -> None:
    error_message = parsed_error["error_message"]
    stack_trace = parsed_error["stack_trace"]
    filename = parsed_error["filename"]
    environment = parsed_error.get("environment", "production")

    # ── Supabase run record ──────────────────────────────────────────────────
    run_id = None
    try:
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
        db_log = None

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

    # ── Get user-specific config ─────────────────────────────────────────────
    repo_override = parsed_error.get("_repo_override") or parsed_error.get("_demo_repo")
    config = _get_user_config(user_id, repo_override=repo_override, demo=demo)
    repo = config["github_repo"]
    base_branch = config["github_base_branch"]
    github_token = config["github_token"]
    slack_webhook = config["slack_webhook_url"]

    info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    info("🔧 DevLoop pipeline started")
    info("   Error : %s", error_message)
    info("   File  : %s", filename)
    info("   Env   : %s  |  Repo: %s", environment, repo or "(not set)")
    info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if not repo:
        error("❌ No GitHub repo configured for user %s — aborting", user_id)
        _legacy_log(run_record)
        return

    # Step 1: Fetch file
    info("📥 [1/6] Fetching source file from GitHub...")
    info("   %s @ %s", filename, base_branch)
    try:
        file_content = github_client.fetch_file(repo, filename, base_branch, token=github_token)
        info("   ✓ Fetched %d bytes", len(file_content))
    except Exception as e:
        error("❌ Failed to fetch %s from GitHub: %s", filename, e)
        _legacy_log(run_record)
        if run_id:
            try:
                update_run(run_id, status="failed")
            except Exception:
                pass
        return

    # Step 2: LLM fix
    info("🧠 [2/6] Running two-LLM diagnosis + fix pipeline...")
    info("   Step 2a — Analyzer LLM: reading error + stack trace...")
    try:
        fix_result = codex_client.generate_fix(
            error_message=error_message,
            stack_trace=stack_trace,
            file_content=file_content,
            filename=filename,
        )
    except Exception as e:
        error("❌ Fix generation failed: %s — no PR will be opened", e)
        _legacy_log(run_record)
        if run_id:
            try:
                update_run(run_id, status="failed")
            except Exception:
                pass
        return

    fixed_code = fix_result["fixed_code"]
    root_cause = fix_result["root_cause"]
    fix_summary = fix_result["fix_summary"]
    info("   ✓ Root cause : %s", root_cause)
    info("   ✓ Fix summary: %s", fix_summary)

    # Step 3: Create branch
    branch_name = github_client.make_branch_name()
    info("🌿 [3/6] Creating fix branch: %s", branch_name)
    try:
        github_client.create_branch(repo, branch_name, token=github_token)
        info("   ✓ Branch created on %s", repo)
    except Exception as e:
        error("❌ Failed to create branch %s: %s", branch_name, e)
        _legacy_log(run_record)
        if run_id:
            try:
                update_run(run_id, status="failed")
            except Exception:
                pass
        return

    # Step 4: Sandbox tests
    info("🧪 [4/6] Running sandbox tests on the patch...")
    test_results = sandbox_runner.run_tests(filename, fixed_code)
    if test_results["passed"]:
        info("   ✅ Tests PASSED in %.2fs", test_results["duration"])
    else:
        info("   ⚠️  Tests FAILED in %.2fs — committing anyway, PR will note this", test_results["duration"])

    # Step 5: Commit patch
    info("💾 [5/6] Committing patch to branch...")
    commit_message = f"fix: auto-patch by DevLoop\n\n{fix_summary}"
    try:
        github_client.commit_patch(
            repo_name=repo,
            branch=branch_name,
            filepath=filename,
            new_content=fixed_code,
            message=commit_message,
            token=github_token,
        )
        info("   ✓ Committed: %s", filename)
    except Exception as e:
        error("❌ Failed to commit patch: %s", e)
        _legacy_log(run_record)
        if run_id:
            try:
                update_run(run_id, status="failed")
            except Exception:
                pass
        return

    # Step 6: Open PR
    info("🔀 [6/6] Opening pull request → %s", base_branch)
    pr_title = f"fix: DevLoop auto-patch — {error_message[:80]}"
    pr_body = github_client.build_pr_body(
        error_message=error_message,
        root_cause=root_cause,
        fix_summary=fix_summary,
        test_results=test_results,
        filename=filename,
    )
    try:
        pr_url = github_client.open_pr(repo, branch_name, pr_title, pr_body, token=github_token)
        info("   ✓ PR opened: %s", pr_url)
    except Exception as e:
        error("❌ Failed to open PR: %s", e)
        _legacy_log(run_record)
        if run_id:
            try:
                update_run(run_id, status="failed")
            except Exception:
                pass
        return

    # Slack notification
    info("📣 Sending Slack notification...")
    try:
        slack_notify.send_notification(
            error_message=error_message,
            filename=filename,
            test_results=test_results,
            pr_url=pr_url,
            webhook_url=slack_webhook,
        )
        info("   ✓ Slack notified")
    except Exception as e:
        warning("   ⚠️  Slack notification failed (non-fatal): %s", e)

    # Done
    run_record.update({"status": "success", "pr_url": pr_url,
                        "test_passed": test_results["passed"], "branch": branch_name,
                        "completed_at": datetime.now(timezone.utc).isoformat()})
    _legacy_log(run_record)

    if run_id:
        try:
            update_run(run_id, status="success", pr_url=pr_url,
                       test_passed=test_results["passed"], branch=branch_name)
        except Exception as e:
            warning("Could not update Supabase run record: %s", e)

    info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    info("✅ DevLoop complete!")
    info("   PR    : %s", pr_url)
    info("   Tests : %s", "PASS ✅" if test_results["passed"] else "FAIL ⚠️")
    info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
