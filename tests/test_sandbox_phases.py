"""Runs sandbox/phase_runner.py for real on a tiny project and parses its markers."""
import json
import subprocess
import sys
from pathlib import Path

from agent.sandbox_runner import _ACTIONS_TS_RE, parse_phase_output

RUNNER = Path(__file__).resolve().parent.parent / "sandbox" / "phase_runner.py"

BUGGY = "def total(items):\n    return sum(i['price'] for i in items) * None\n"
FIXED = "def total(items):\n    return sum(i['price'] for i in items)\n"
TEST = "from shop import total\n\ndef test_total():\n    assert total([{'price': 2}]) == 2\n"


def test_red_then_green(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    (work / "shop.py").write_text(BUGGY)
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({
        "test_command": f'"{sys.executable}" -m pytest',
        "phases": [
            {"name": "red", "files": {"test_shop.py": TEST}, "target": "test_shop.py"},
            {"name": "green1", "files": {"shop.py": FIXED, "test_shop.py": TEST}},
        ],
    }))
    proc = subprocess.run([sys.executable, str(RUNNER), str(bundle), str(work)],
                          capture_output=True, text=True, timeout=300)
    phases = parse_phase_output(proc.stdout)
    assert phases["red"]["exit_code"] == 1, proc.stdout
    assert phases["green1"]["passed"], proc.stdout


def test_path_escape_rejected(tmp_path):
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"phases": [{"name": "x", "files": {"../evil.py": "x"}}]}))
    proc = subprocess.run([sys.executable, str(RUNNER), str(bundle), str(tmp_path / "w")],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode != 0
    assert not (tmp_path / "evil.py").exists()


def test_parse_actions_log():
    log = (
        "2026-10-18T10:00:00.1234567Z =====DEVLOOP PHASE red BEGIN=====\n"
        "2026-10-18T10:00:01.1234567Z F  [100%]\n"
        "2026-10-18T10:00:01.2234567Z =====DEVLOOP PHASE red END exit=1 duration=0.52=====\n"
    )
    phases = parse_phase_output(_ACTIONS_TS_RE.sub("", log))
    assert phases == {"red": {"passed": False, "exit_code": 1, "output": "F  [100%]", "duration": 0.52}}
