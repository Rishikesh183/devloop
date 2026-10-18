"""Runs test phases against a repo, in Docker or on GitHub Actions.

A phase overlays some files on a fresh checkout of the repo and runs the test
command. DevLoop uses two: "red" (regression test on the original code, should
fail) and "green" (fix + test, should pass). Both backends execute the same
sandbox/phase_runner.py so results are identical.
"""
import base64
import gzip
import io
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("devloop.sandbox_runner")

TEST_RUNNER = os.getenv("TEST_RUNNER", "auto").lower()   # auto | docker | actions | none
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "python:3.11-slim")
TEST_COMMAND = os.getenv("TEST_COMMAND", "pytest")
TIMEOUT_SECONDS = 600
MAX_REPO_BYTES = 50 * 1024 * 1024

ACTIONS_RUNNER_REPO = os.getenv("ACTIONS_RUNNER_REPO", "Rishikesh183/devloop")
ACTIONS_RUNNER_REF = os.getenv("ACTIONS_RUNNER_REF", "main")
ACTIONS_WORKFLOW = os.getenv("ACTIONS_WORKFLOW", "devloop-sandbox.yml")
ACTIONS_TOKEN = os.getenv("ACTIONS_RUNNER_TOKEN") or os.getenv("GITHUB_TOKEN", "")
ACTIONS_POLL_SECONDS = 4
MAX_BUNDLE_CHARS = 60_000   # workflow_dispatch inputs are capped at 65,535 chars total

PHASE_RUNNER = Path(__file__).resolve().parent.parent / "sandbox" / "phase_runner.py"
_PHASE_RE = re.compile(
    r"=====DEVLOOP PHASE (\S+) BEGIN=====\n(.*?)\n?=====DEVLOOP PHASE \1 END exit=(-?\d+) duration=([\d.]+)=====",
    re.DOTALL,
)
_ACTIONS_TS_RE = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?", re.MULTILINE)

StatusCallback = Callable[[str, str | None], None]


def _docker_available() -> bool:
    try:
        import docker
        docker.from_env().ping()
        return True
    except Exception:
        return False


def resolve_backend() -> str:
    if TEST_RUNNER in ("docker", "actions", "none"):
        return TEST_RUNNER
    if _docker_available():
        return "docker"
    if ACTIONS_TOKEN and ACTIONS_RUNNER_REPO:
        return "actions"
    return "none"


def parse_phase_output(text: str) -> dict[str, dict]:
    phases = {}
    for name, output, code, duration in _PHASE_RE.findall(text.replace("\r\n", "\n")):
        code = int(code)
        phases[name] = {
            "passed": code == 0,
            "exit_code": code,
            "output": output.strip(),
            "duration": float(duration),
        }
    return phases


def run_phases(
    repo_name: str,
    ref: str,
    token: str | None,
    phases: list[dict],
    on_status: StatusCallback | None = None,
    backend: str | None = None,
) -> dict:
    """Run phases; returns {"backend", "url", "phases": {name: result}, "error"}."""
    backend = backend or resolve_backend()
    bundle = {"test_command": TEST_COMMAND, "phases": phases}
    logger.info("Running %d test phase(s) on %s via %s", len(phases), repo_name, backend)
    if backend == "docker":
        result = _run_docker(repo_name, ref, token, bundle, on_status)
    elif backend == "actions":
        result = _run_actions(repo_name, ref, bundle, on_status)
    else:
        result = {"phases": {}, "url": None, "error": "No test runner available (set TEST_RUNNER)"}
    result["backend"] = backend
    missing = [p["name"] for p in phases if p["name"] not in result["phases"]]
    if missing and not result.get("error"):
        result["error"] = f"Sandbox produced no result for phase(s): {', '.join(missing)}"
    return result


# ── Docker ────────────────────────────────────────────────────────────────────

def _download_repo(repo_name: str, ref: str, token: str | None, dest: str) -> None:
    """Extract the repo at `ref` into dest, so tests see the whole project, not just one file."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(
        f"https://api.github.com/repos/{repo_name}/tarball/{ref}",
        headers=headers, timeout=60,
    )
    resp.raise_for_status()
    if len(resp.content) > MAX_REPO_BYTES:
        raise ValueError(f"Repo tarball too large ({len(resp.content)} bytes)")

    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        members = []
        for m in tar.getmembers():
            # GitHub wraps everything in "<owner>-<repo>-<sha>/" — strip it
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            m.name = parts[1]
            members.append(m)
        tar.extractall(dest, members=members, filter="data")


def _run_docker(repo_name: str, ref: str, token: str | None, bundle: dict, on_status: StatusCallback | None) -> dict:
    import docker
    from docker.errors import DockerException, ImageNotFound

    container = None
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        tools = Path(tmpdir) / "devloop"
        workspace.mkdir()
        tools.mkdir()
        try:
            _download_repo(repo_name, ref, token, str(workspace))
        except Exception as e:
            logger.warning("Could not download repo, testing overlay files alone: %s", e)
        (tools / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        shutil.copy(PHASE_RUNNER, tools / "phase_runner.py")

        try:
            client = docker.from_env()
            try:
                client.images.get(DOCKER_IMAGE)
            except ImageNotFound:
                logger.info("Pulling image %s...", DOCKER_IMAGE)
                client.images.pull(DOCKER_IMAGE)
            if on_status:
                on_status("in_progress", None)
            container = client.containers.run(
                image=DOCKER_IMAGE,
                command=["python", "/devloop/phase_runner.py", "/devloop/bundle.json", "/workspace"],
                volumes={
                    str(workspace): {"bind": "/workspace", "mode": "rw"},
                    str(tools): {"bind": "/devloop", "mode": "ro"},
                },
                working_dir="/workspace",
                detach=True,
                mem_limit="512m",
                cpu_period=100000,
                cpu_quota=50000,
            )
            try:
                container.wait(timeout=TIMEOUT_SECONDS)
            except Exception as e:
                logger.warning("Container timed out or wait failed: %s", e)
                try:
                    container.kill()
                except Exception:
                    pass
                return {"phases": {}, "url": None, "error": f"Test run timed out after {TIMEOUT_SECONDS}s"}
            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
            phases = parse_phase_output(logs)
            return {"phases": phases, "url": None, "error": None if phases else logs[-2000:]}
        except DockerException as e:
            logger.error("Docker error during test run: %s", e)
            return {"phases": {}, "url": None, "error": f"Docker error: {e}"}
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception as cleanup_err:
                    logger.warning("Failed to clean up container: %s", cleanup_err)


# ── GitHub Actions ────────────────────────────────────────────────────────────

def _gh(method: str, path: str, **kwargs) -> requests.Response:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {ACTIONS_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.request(method, f"https://api.github.com{path}", headers=headers, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp


def _find_run(run_title: str, since: str) -> dict | None:
    resp = _gh(
        "GET",
        f"/repos/{ACTIONS_RUNNER_REPO}/actions/workflows/{ACTIONS_WORKFLOW}/runs",
        params={"event": "workflow_dispatch", "per_page": 30, "created": f">={since}"},
    )
    for run in resp.json().get("workflow_runs", []):
        if run.get("display_title") == run_title:
            return run
    return None


def _run_actions(repo_name: str, ref: str, bundle: dict, on_status: StatusCallback | None) -> dict:
    encoded = base64.b64encode(gzip.compress(json.dumps(bundle).encode())).decode()
    if len(encoded) > MAX_BUNDLE_CHARS:
        return {"phases": {}, "url": None, "error": f"Patch too large for Actions dispatch ({len(encoded)} chars)"}

    run_token = uuid.uuid4().hex[:16]
    run_title = f"devloop-{run_token}"
    since = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _gh(
            "POST",
            f"/repos/{ACTIONS_RUNNER_REPO}/actions/workflows/{ACTIONS_WORKFLOW}/dispatches",
            json={"ref": ACTIONS_RUNNER_REF, "inputs": {
                "repo": repo_name, "ref": ref, "bundle": encoded, "run_token": run_token,
            }},
        )
    except requests.RequestException as e:
        resp = getattr(e, "response", None)
        body = resp.text[:300] if resp is not None else ""
        return {"phases": {}, "url": None,
                "error": f"Could not dispatch {ACTIONS_WORKFLOW} on {ACTIONS_RUNNER_REPO}: {e} {body}"}

    deadline = time.monotonic() + TIMEOUT_SECONDS
    run = None
    last_status = None
    while time.monotonic() < deadline:
        time.sleep(ACTIONS_POLL_SECONDS)
        try:
            if run is None:
                run = _find_run(run_title, since)
            else:
                run = _gh("GET", f"/repos/{ACTIONS_RUNNER_REPO}/actions/runs/{run['id']}").json()
        except requests.RequestException as e:
            logger.warning("Polling Actions failed (will retry): %s", e)
            continue
        if run is None:
            continue
        if run["status"] != last_status:
            last_status = run["status"]
            logger.info("Actions run %s: %s", run["id"], last_status)
            if on_status:
                on_status(last_status, run.get("html_url"))
        if run["status"] == "completed":
            break
    else:
        return {"phases": {}, "url": run.get("html_url") if run else None,
                "error": f"Actions run did not finish within {TIMEOUT_SECONDS}s"}

    url = run.get("html_url")
    try:
        jobs = _gh("GET", f"/repos/{ACTIONS_RUNNER_REPO}/actions/runs/{run['id']}/jobs").json().get("jobs", [])
        if not jobs:
            return {"phases": {}, "url": url, "error": f"Actions run {run.get('conclusion')} with no jobs"}
        logs = _gh("GET", f"/repos/{ACTIONS_RUNNER_REPO}/actions/jobs/{jobs[0]['id']}/logs").text
    except requests.RequestException as e:
        return {"phases": {}, "url": url, "error": f"Could not read Actions logs: {e}"}

    logs = _ACTIONS_TS_RE.sub("", logs)
    phases = parse_phase_output(logs)
    error = None if phases else f"Actions run {run.get('conclusion')}: {logs[-1500:]}"
    return {"phases": phases, "url": url, "error": error}
