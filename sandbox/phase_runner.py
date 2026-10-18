"""Runs DevLoop test phases inside a sandbox (Docker container or GitHub Actions job).

usage: python phase_runner.py BUNDLE_JSON WORKDIR

BUNDLE_JSON: {"test_command": "pytest",
              "phases": [{"name": "red", "files": {"path": "content"}, "target": "tests/test_x.py"}, ...]}

Phases run in order on the same checkout; each phase's files are written on top
of the previous ones. Results are printed between markers that the DevLoop
server parses out of the container / job logs. Always exits 0 — the per-phase
exit codes in the markers are the result.
"""
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

OUTPUT_LIMIT = 6000
PHASE_TIMEOUT_SECONDS = 300
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "site-packages", "__pycache__"}


def safe_path(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    if root.resolve() not in path.parents:
        raise ValueError(f"path escapes workdir: {rel}")
    return path


def find_requirements(root: Path, max_depth: int = 3) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and depth < max_depth]
        if "requirements.txt" in filenames:
            found.append(Path(dirpath) / "requirements.txt")
    return sorted(found)


def pip_install(args: list[str], root: Path) -> None:
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *args]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"pip install {' '.join(args)} failed:\n{(proc.stdout + proc.stderr)[-2000:]}", flush=True)


def build_command(base: str, target: str | None) -> str:
    cmd = base
    if "pytest" in base:
        cmd += " -q --tb=short -p no:cacheprovider"
    if target:
        cmd += " " + shlex.quote(target)
    return cmd


def main() -> None:
    bundle = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    root = Path(sys.argv[2]).resolve()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    pip_install(["pytest"], root)
    for req in find_requirements(root):
        pip_install(["-r", str(req)], root)

    for phase in bundle.get("phases", []):
        name = phase["name"]
        for rel, content in phase.get("files", {}).items():
            dest = safe_path(root, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        cmd = build_command(bundle.get("test_command") or "pytest", phase.get("target"))
        print(f"=====DEVLOOP PHASE {name} BEGIN=====", flush=True)
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=root, env=env,
                capture_output=True, text=True, timeout=PHASE_TIMEOUT_SECONDS,
            )
            output, code = proc.stdout + proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            output, code = f"Timed out after {PHASE_TIMEOUT_SECONDS}s", 124
        print(output[-OUTPUT_LIMIT:].rstrip(), flush=True)
        print(f"=====DEVLOOP PHASE {name} END exit={code} duration={time.monotonic() - start:.2f}=====", flush=True)


if __name__ == "__main__":
    main()
