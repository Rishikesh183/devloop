# DevLoop Agent Instructions

This file tells Codex how to work in this repository.

## Language & Framework
- Python 3.11+
- FastAPI for the webhook server
- Standard library + packages from requirements.txt

## Test Command
```
pytest
```
Run from the repo root. Tests are in `tests/` if present, otherwise pytest auto-discovers.

## Code Style Rules
- Keep fixes **minimal** — change only what is broken, nothing else
- Do NOT refactor, rename, or restructure surrounding code
- Do NOT change function signatures unless the signature itself is the bug
- Preserve existing code style (formatting, naming, comment style)
- Do NOT add logging or print statements
- Do NOT add new imports unless strictly required by the fix

## Branch Naming
- Format: `fix/devloop-{YYYYMMDDHHMMSS}`
- Example: `fix/devloop-20240315143022`

## What NOT to Change
- `requirements.txt` — do not add or remove packages
- `.env` / any config files — never touch environment or secrets
- `alembic/` or any migration files — migrations are off-limits
- `AGENTS.md` — this file
- `main.py` webhook receiver — only touch if the bug is explicitly there
- Any file not mentioned in the stack trace

## Fix Philosophy
- Identify the single root cause from the stack trace
- Write the smallest possible change that eliminates the error
- Prefer defensive fixes (None checks, type guards) over structural changes
- If the fix requires more than ~10 lines changed, something is wrong — reconsider
