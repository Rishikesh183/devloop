"""Search/replace edits returned by the fixer LLM.

The fixer returns small {file, search, replace} blocks instead of the whole
file, which keeps output tokens proportional to the fix, not the file size.
"""
import difflib


class EditError(ValueError):
    """An edit could not be applied (search text missing or ambiguous)."""


def apply_edits(files: dict[str, str], edits: list) -> dict[str, str]:
    """Apply edits to a copy of `files` (path -> content). Only listed paths may be edited."""
    if not isinstance(edits, list) or not edits:
        raise EditError("no edits returned")
    out = dict(files)
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise EditError(f"edit {i} is not an object")
        path = edit.get("file")
        search = edit.get("search")
        replace = edit.get("replace", "")
        if path not in out:
            raise EditError(f"edit {i}: unknown file {path!r}, allowed: {sorted(out)}")
        if not isinstance(search, str) or not search.strip():
            raise EditError(f"edit {i}: empty search text")
        if not isinstance(replace, str):
            raise EditError(f"edit {i}: replace must be a string")
        out[path] = _replace_once(out[path], search, replace, i)
    return out


def _replace_once(text: str, search: str, replace: str, i: int) -> str:
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    search = search.replace("\r\n", "\n")
    replace = replace.replace("\r\n", "\n")

    count = text.count(search)
    if count == 1:
        result = text.replace(search, replace, 1)
    elif count > 1:
        raise EditError(f"edit {i}: search text matches {count} places, add surrounding lines")
    else:
        result = _replace_lines_loose(text, search, replace, i)

    return result.replace("\n", "\r\n") if crlf else result


def _replace_lines_loose(text: str, search: str, replace: str, i: int) -> str:
    """Fallback: match whole lines ignoring trailing whitespace (models often drop it)."""
    lines = text.splitlines(keepends=True)
    want = [ln.rstrip() for ln in search.strip("\n").split("\n")]
    have = [ln.rstrip() for ln in lines]
    n = len(want)
    hits = [j for j in range(len(lines) - n + 1) if have[j:j + n] == want]
    if not hits:
        raise EditError(f"edit {i}: search text not found in file")
    if len(hits) > 1:
        raise EditError(f"edit {i}: search text matches {len(hits)} places, add surrounding lines")
    j = hits[0]
    ends_with_newline = lines[j + n - 1].endswith("\n")
    new_block = replace.strip("\n")
    if new_block:
        new_block += "\n" if ends_with_newline else ""
    return "".join(lines[:j]) + new_block + "".join(lines[j + n:])


def unified_diff(path: str, old: str, new: str) -> str:
    def norm(s: str) -> list[str]:
        return [ln if ln.endswith("\n") else ln + "\n" for ln in s.splitlines(keepends=True)]

    return "".join(difflib.unified_diff(
        norm(old), norm(new),
        fromfile=f"a/{path}" if old else "/dev/null",
        tofile=f"b/{path}",
        n=3,
    ))


def changed_line_count(old: str, new: str) -> int:
    return sum(
        1 for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    )
