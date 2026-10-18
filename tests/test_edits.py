import pytest

from agent.edits import EditError, apply_edits, changed_line_count, unified_diff

SRC = '''def get_discount():
    active = []
    if active:
        return active[0]
    return None
'''


def test_exact_edit():
    out = apply_edits({"a.py": SRC}, [{"file": "a.py", "search": "    return None", "replace": "    return 0"}])
    assert out["a.py"].endswith("    return 0\n")
    assert changed_line_count(SRC, out["a.py"]) == 2


def test_trailing_whitespace_tolerated():
    edit = {"file": "a.py", "search": "    if active:   \n        return active[0]", "replace": "    if active:\n        return active[0] or 0"}
    out = apply_edits({"a.py": SRC}, [edit])
    assert "active[0] or 0\n" in out["a.py"]


def test_crlf_file_kept_crlf():
    src = SRC.replace("\n", "\r\n")
    out = apply_edits({"a.py": src}, [{"file": "a.py", "search": "return None", "replace": "return 0"}])
    assert "return 0\r\n" in out["a.py"]


def test_ambiguous_search_rejected():
    with pytest.raises(EditError, match="matches 2 places"):
        apply_edits({"a.py": "x = 1\nx = 1\n"}, [{"file": "a.py", "search": "x = 1", "replace": "x = 2"}])


def test_missing_search_rejected():
    with pytest.raises(EditError, match="not found"):
        apply_edits({"a.py": SRC}, [{"file": "a.py", "search": "nope()", "replace": ""}])


def test_unknown_file_rejected():
    with pytest.raises(EditError, match="unknown file"):
        apply_edits({"a.py": SRC}, [{"file": "b.py", "search": "x", "replace": "y"}])


def test_edits_are_sequential():
    edits = [
        {"file": "a.py", "search": "return None", "replace": "return 0"},
        {"file": "a.py", "search": "return 0", "replace": "return 1"},
    ]
    assert "return 1" in apply_edits({"a.py": SRC}, edits)["a.py"]


def test_unified_diff_new_file():
    diff = unified_diff("tests/test_x.py", "", "def test_x():\n    assert True\n")
    assert diff.startswith("--- /dev/null\n+++ b/tests/test_x.py")
