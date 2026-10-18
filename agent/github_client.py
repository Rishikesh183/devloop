import base64
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from github import Github, GithubException, InputGitTreeElement

load_dotenv()

logger = logging.getLogger("devloop.github_client")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "main")


def _get_client(token: str = None) -> Github:
    t = token or GITHUB_TOKEN
    if not t:
        raise ValueError("GITHUB_TOKEN not set in environment")
    return Github(t)


def fetch_file(repo_name: str, filepath: str, branch: str = None, token: str = None) -> str:
    branch = branch or GITHUB_BASE_BRANCH
    logger.info("Fetching %s from %s@%s", filepath, repo_name, branch)
    try:
        g = _get_client(token)
        repo = g.get_repo(repo_name)
        content = repo.get_contents(filepath, ref=branch)
        decoded = base64.b64decode(content.content).decode("utf-8")
        logger.info("Fetched %s (%d bytes)", filepath, len(decoded))
        return decoded
    except GithubException as e:
        logger.error("GitHub error fetching %s: %s", filepath, e)
        raise
    except Exception as e:
        logger.error("Unexpected error fetching %s: %s", filepath, e)
        raise


def get_file_tree(repo_name: str, ref: str, token: str = None, limit: int = 3000) -> list[str]:
    """All file paths in the repo at `ref` (used to show the LLM where tests live)."""
    g = _get_client(token)
    repo = g.get_repo(repo_name)
    tree = repo.get_git_tree(ref, recursive=True).tree
    return [e.path for e in tree if e.type == "blob"][:limit]


def create_branch(repo_name: str, branch_name: str, base_branch: str = None, token: str = None) -> str:
    base_branch = base_branch or GITHUB_BASE_BRANCH
    logger.info("Creating branch %s in %s", branch_name, repo_name)
    try:
        g = _get_client(token)
        repo = g.get_repo(repo_name)
        base_ref = repo.get_branch(base_branch)
        repo.create_git_ref(f"refs/heads/{branch_name}", base_ref.commit.sha)
        logger.info("Branch %s created from %s", branch_name, base_branch)
        return branch_name
    except GithubException as e:
        logger.error("GitHub error creating branch %s: %s", branch_name, e)
        raise
    except Exception as e:
        logger.error("Unexpected error creating branch %s: %s", branch_name, e)
        raise


def commit_patch(
    repo_name: str,
    branch: str,
    filepath: str,
    new_content: str,
    message: str,
    token: str = None,
) -> str:
    logger.info("Committing patch to %s on branch %s", filepath, branch)
    try:
        g = _get_client(token)
        repo = g.get_repo(repo_name)
        current = repo.get_contents(filepath, ref=branch)
        result = repo.update_file(
            path=filepath,
            message=message,
            content=new_content,
            sha=current.sha,
            branch=branch,
        )
        commit_sha = result["commit"].sha
        logger.info("Committed patch: %s", commit_sha)
        return commit_sha
    except GithubException as e:
        logger.error("GitHub error committing patch to %s: %s", filepath, e)
        raise
    except Exception as e:
        logger.error("Unexpected error committing patch to %s: %s", filepath, e)
        raise


def commit_files(repo_name: str, branch: str, files: dict[str, str], message: str, token: str = None) -> str:
    """Create or update several files in one commit on `branch`. Returns the commit sha."""
    logger.info("Committing %d file(s) to %s on branch %s", len(files), repo_name, branch)
    try:
        g = _get_client(token)
        repo = g.get_repo(repo_name)
        ref = repo.get_git_ref(f"heads/{branch}")
        parent = repo.get_git_commit(ref.object.sha)
        elements = [InputGitTreeElement(path, "100644", "blob", content=content) for path, content in files.items()]
        tree = repo.create_git_tree(elements, parent.tree)
        commit = repo.create_git_commit(message, tree, [parent])
        ref.edit(commit.sha)
        logger.info("Committed %s", commit.sha)
        return commit.sha
    except GithubException as e:
        logger.error("GitHub error committing to %s: %s", branch, e)
        raise


def open_pr(
    repo_name: str,
    branch: str,
    title: str,
    body: str,
    base_branch: str = None,
    token: str = None,
    draft: bool = False,
) -> str:
    base_branch = base_branch or GITHUB_BASE_BRANCH
    logger.info("Opening %sPR from %s in %s", "draft " if draft else "", branch, repo_name)
    try:
        g = _get_client(token)
        repo = g.get_repo(repo_name)
        pr = repo.create_pull(
            title=title,
            body=body,
            head=branch,
            base=base_branch,
            draft=draft,
        )
        logger.info("PR opened: %s", pr.html_url)
        return pr.html_url
    except GithubException as e:
        logger.error("GitHub error opening PR from %s: %s", branch, e)
        raise
    except Exception as e:
        logger.error("Unexpected error opening PR from %s: %s", branch, e)
        raise


def make_branch_name() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"fix/devloop-{ts}"


def _test_line(result: dict | None, expect_fail: bool = False) -> str:
    if not result:
        return "not run"
    if expect_fail:
        if result.get("exit_code") == 1:
            return "❌ failed on the original code (bug reproduced)"
        return f"⚠️ did not reproduce the bug (exit {result.get('exit_code')})"
    return "✅ passing" if result.get("passed") else f"❌ failing (exit {result.get('exit_code')})"


def build_pr_body(
    error_message: str,
    filename: str,
    analysis: dict,
    fix_summary: str,
    test_path: str,
    red: dict | None,
    attempts: list[dict],
    backend: str,
    tokens_used: int,
    duration: float,
) -> str:
    last = attempts[-1] if attempts else {}
    green = last.get("result")
    history = "\n".join(
        f"| {a['attempt']} | {_test_line(a.get('result'))} | {a.get('summary', '').replace('|', '/')[:160]} |"
        for a in attempts
    ) or "| – | not run | |"
    output = (green or {}).get("output") or (red or {}).get("output") or "No output captured"

    return f"""## Error Summary
**File:** `{filename}`
**Error:** {error_message}

## Root Cause
{analysis.get("root_cause", "")}

**Buggy line:** `{analysis.get("buggy_line", "").strip()}`

## Fix Applied
{fix_summary}

## Proof
| Check | Result |
|---|---|
| Regression test `{test_path}` on original code | {_test_line(red, expect_fail=True)} |
| Full test suite with fix | {_test_line(green)} |

Tests ran on **{backend}**.

### Attempts
| # | Tests | Change |
|---|---|---|
{history}

<details><summary>Last test output</summary>

```
{output[-3000:]}
```
</details>

---
*Automatically generated by [DevLoop](https://github.com/devloop) in {duration:.0f}s using {tokens_used:,} LLM tokens.*
"""
