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
RUNS_LOG_PATH = Path("devloop_runs.json")


def _append_run_log(run_record: dict) -> None:
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
        logger.warning("Could not write run log: %s", e)


async def run_orchestrator(parsed_error: dict) -> None:
    error_message = parsed_error["error_message"]
    stack_trace = parsed_error["stack_trace"]
    filename = parsed_error["filename"]
    environment = parsed_error.get("environment", "production")

    started_at = datetime.now(timezone.utc).isoformat()
    run_record = {
        "started_at": started_at,
        "error_message": error_message,
        "filename": filename,
        "environment": environment,
        "status": "failed",
        "pr_url": None,
        "test_passed": None,
    }

    logger.info("DevLoop triggered for error: %s", error_message)
    logger.info("Affected file: %s | Environment: %s", filename, environment)

    if not GITHUB_REPO:
        logger.error("GITHUB_REPO not set — aborting")
        _append_run_log(run_record)
        return

    # Step 1: Fetch affected file from GitHub
    try:
        file_content = github_client.fetch_file(GITHUB_REPO, filename, GITHUB_BASE_BRANCH)
    except Exception as e:
        logger.error("Failed to fetch file %s from GitHub: %s", filename, e)
        _append_run_log(run_record)
        return

    # Step 2: Generate fix with Codex
    try:
        fix_result = codex_client.generate_fix(
            error_message=error_message,
            stack_trace=stack_trace,
            file_content=file_content,
            filename=filename,
        )
    except Exception as e:
        logger.error("Codex fix generation failed: %s — aborting, no PR will be opened", e)
        _append_run_log(run_record)
        return

    fixed_code = fix_result["fixed_code"]
    root_cause = fix_result["root_cause"]
    fix_summary = fix_result["fix_summary"]
    logger.info("Codex root cause: %s", root_cause)

    # Step 3: Create new branch
    branch_name = github_client.make_branch_name()
    try:
        github_client.create_branch(GITHUB_REPO, branch_name)
    except Exception as e:
        logger.error("Failed to create branch %s: %s", branch_name, e)
        _append_run_log(run_record)
        return

    # Step 4: Run sandbox tests against the patch
    logger.info("Running sandbox tests...")
    with tempfile.TemporaryDirectory() as tmpdir:
        test_results = sandbox_runner.run_tests(filename, fixed_code)

    logger.info(
        "Sandbox tests %s in %.2fs",
        "PASSED" if test_results["passed"] else "FAILED",
        test_results["duration"],
    )

    # Step 5: Commit patch to branch
    commit_message = f"fix: auto-patch by DevLoop\n\n{fix_summary}"
    try:
        github_client.commit_patch(
            repo_name=GITHUB_REPO,
            branch=branch_name,
            filepath=filename,
            new_content=fixed_code,
            message=commit_message,
        )
    except Exception as e:
        logger.error("Failed to commit patch: %s", e)
        _append_run_log(run_record)
        return

    # Step 6: Open PR
    pr_title = f"fix: DevLoop auto-patch — {error_message[:80]}"
    pr_body = github_client.build_pr_body(
        error_message=error_message,
        root_cause=root_cause,
        fix_summary=fix_summary,
        test_results=test_results,
        filename=filename,
    )

    try:
        pr_url = github_client.open_pr(
            repo_name=GITHUB_REPO,
            branch=branch_name,
            title=pr_title,
            body=pr_body,
        )
    except Exception as e:
        logger.error("Failed to open PR: %s", e)
        _append_run_log(run_record)
        return

    logger.info("PR opened: %s", pr_url)

    # Step 7: Slack notification
    try:
        slack_notify.send_notification(
            error_message=error_message,
            filename=filename,
            test_results=test_results,
            pr_url=pr_url,
        )
    except Exception as e:
        logger.warning("Slack notification failed (non-fatal): %s", e)

    # Step 8: Log run
    run_record["status"] = "success"
    run_record["pr_url"] = pr_url
    run_record["test_passed"] = test_results["passed"]
    run_record["branch"] = branch_name
    run_record["completed_at"] = datetime.now(timezone.utc).isoformat()
    _append_run_log(run_record)

    logger.info("DevLoop run complete. PR: %s | Tests: %s", pr_url, "PASS" if test_results["passed"] else "FAIL")
