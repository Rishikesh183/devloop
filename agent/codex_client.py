import json
import logging
import os
import posixpath
import re
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from agent.edits import EditError, apply_edits

load_dotenv()

logger = logging.getLogger("devloop.codex_client")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_API_KEY_2 = os.getenv("OPENROUTER_API_KEY_2", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Rate-limit handling: one "batch" = one pass over every model x key.
# When everything in a batch is rate-limited we wait for the earliest reset and retry the batch.
LLM_MAX_BATCHES = int(os.getenv("LLM_MAX_BATCHES", "4"))
LLM_MAX_WAIT_SECONDS = float(os.getenv("LLM_MAX_WAIT_SECONDS", "60"))
LLM_BACKOFF_SECONDS = float(os.getenv("LLM_BACKOFF_SECONDS", "8"))

# Large files: send only a window around the crash line to the fixer
EXCERPT_THRESHOLD_LINES = 250
EXCERPT_RADIUS_LINES = 80

# Step 1: Analyzer — small/fast models, just reads error + trace
# Job: identify root cause, pinpoint exact problem. No code generation.
# Free models come and go on OpenRouter — override with comma-separated env lists
# (check https://openrouter.ai/models?max_price=0) instead of editing code.
def _env_models(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "")
    return [m.strip() for m in raw.split(",") if m.strip()] or default


ANALYZER_MODELS = _env_models("ANALYZER_MODELS", [
    "nvidia/nemotron-3.5-lightning:free",       # fast
    "minimax/minimax-m3.1-flash:free",
    "google/gemma-4-26b-a4b-it:free",
    "poolside/laguna-xs-2.1:free",              # fallback
])

# Step 2: Fixer — code-focused models, receives only the file + analysis
# Job: write the minimal fix as search/replace edits plus a regression test.
FIXER_MODELS = _env_models("FIXER_MODELS", [
    "cohere/north-mini-code:free",
    "poolside/laguna-s-2.1:free",
    "minimax/minimax-m3.1-flash:free",
    "google/gemma-4-31b-it:free",               # fallback
])

ANALYZER_SYSTEM = """You are a senior software engineer analyzing a production error.
Given an error message and stack trace, identify the root cause precisely.

Respond in this exact JSON format with no markdown, no text outside JSON:
{
  "root_cause": "one sentence: what is wrong and why",
  "buggy_line": "the exact line of code that is the source of the bug",
  "fix_strategy": "one sentence: what change will fix it",
  "scope": "minimal — what lines/logic to change"
}"""

_EDIT_RULES = """Edit rules:
- "search" must be copied character-for-character from the file (same indentation) and match exactly one place. Add 1-3 neighbouring lines if needed to make it unique.
- Keep edits minimal. Do not reformat, rename or refactor. Never return the whole file."""

FIXER_SYSTEM = f"""You are an expert software engineer writing a production bug fix with the smallest possible change.
You get a pre-analyzed root cause, the source file (or an excerpt of it), and how the repo's tests are laid out.

Respond in this exact JSON format with no markdown, no text outside JSON:
{{
  "edits": [{{"file": "<source path>", "search": "<exact existing lines>", "replace": "<new lines>"}}],
  "test_path": "<path for ONE new pytest file, next to the existing tests>",
  "test_code": "<complete content of that pytest file>",
  "fix_summary": "<one or two sentences: what you changed and why>"
}}

{_EDIT_RULES}

Test rules:
- Write one focused regression test that FAILS on the current code by reproducing the reported error, and PASSES after your fix.
- Import the code exactly the way the existing tests do. No network, randomness or sleeps."""

REFINE_SYSTEM = f"""You are an expert software engineer. Your previous bug fix did not make the test suite pass.
You get the current source file, the regression test, and the failing test output.

Respond in this exact JSON format with no markdown, no text outside JSON:
{{
  "edits": [{{"file": "<source path or test path>", "search": "<exact existing lines>", "replace": "<new lines>"}}],
  "fix_summary": "<one or two sentences: what you changed this time and why>"
}}

Fix the source code. Only edit the test if the test itself is wrong (bad import, wrong expected value).
{_EDIT_RULES}"""


class LLMError(RuntimeError):
    pass


@dataclass
class TokenUsage:
    prompt: int = 0
    completion: int = 0
    calls: int = 0

    def add(self, usage) -> None:
        self.calls += 1
        if usage is None:
            return
        self.prompt += getattr(usage, "prompt_tokens", 0) or 0
        self.completion += getattr(usage, "completion_tokens", 0) or 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion


@dataclass
class _Lane:
    """One API key (+ provider). `models` set means the lane has its own model list."""
    label: str
    api_key: str
    base_url: str | None
    models: list[str] | None = None

    @property
    def fingerprint(self) -> str:
        return f"{self.base_url}|{self.api_key[-8:]}"

    def client(self) -> OpenAI:
        headers = {"HTTP-Referer": "https://github.com/devloop", "X-Title": "DevLoop"} if self.base_url else None
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=0,      # our batch loop handles retries
            timeout=90,
            default_headers=headers,
        )


# (lane fingerprint, model) -> monotonic time it may be used again.
# Shared across runs so parallel pipelines don't hammer a model that just rate-limited.
_cooldowns: dict[tuple[str, str], float] = {}
_cooldown_lock = threading.Lock()


def _cooldown_remaining(key: tuple[str, str]) -> float:
    with _cooldown_lock:
        return max(0.0, _cooldowns.get(key, 0.0) - time.monotonic())


def _set_cooldown(key: tuple[str, str], seconds: float) -> None:
    with _cooldown_lock:
        _cooldowns[key] = max(_cooldowns.get(key, 0.0), time.monotonic() + seconds)


def _lanes(user_key: str | None) -> list[_Lane]:
    if user_key:
        return [_Lane("your key", user_key, OPENROUTER_BASE_URL)]
    keys = [k for k in [OPENROUTER_API_KEY, OPENROUTER_API_KEY_2] if k]
    lanes = [_Lane(f"key{i + 1}", k, OPENROUTER_BASE_URL) for i, k in enumerate(keys)]
    if not lanes and OPENAI_API_KEY:
        lanes.append(_Lane("openai", OPENAI_API_KEY, None, models=[OPENAI_MODEL]))
    if not lanes:
        raise LLMError("No LLM credentials. Set OPENROUTER_API_KEY or OPENAI_API_KEY in .env")
    return lanes


def _candidates(models: list[str], lanes: list[_Lane]) -> list[tuple[_Lane, str]]:
    """Model-major order: try a model on every key before moving to the next model."""
    seen: set = set()
    models = [m for m in models if m and m not in seen and not seen.add(m)]
    out = [(lane, m) for m in models for lane in lanes if lane.models is None]
    out += [(lane, m) for lane in lanes if lane.models for m in lane.models]
    return out


def _retry_after_seconds(e: APIStatusError) -> float | None:
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    value = headers.get("retry-after")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    reset = headers.get("x-ratelimit-reset")
    if reset:
        try:
            ts = float(reset)
            ts = ts / 1000 if ts > 1e11 else ts   # OpenRouter sends epoch milliseconds
            return max(0.0, ts - time.time())
        except ValueError:
            pass
    return None


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end]).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _call_json(
    candidates: list[tuple[_Lane, str]],
    system: str,
    user: str,
    required_keys: set,
    label: str,
    usage: TokenUsage,
    max_tokens: int,
    validate=None,
) -> dict:
    """Try candidates batch by batch until one returns valid JSON.

    - 429 / transient errors: that model+key cools down, try the next one.
    - Bad output: skip the model for this batch (other keys would give the same answer).
    - Whole batch cooling down: wait for the earliest reset, then run the batch again.
    """
    bad_output: dict[str, int] = {}
    dead: set[tuple[str, str]] = set()
    last_error: Exception | None = None

    for batch in range(1, LLM_MAX_BATCHES + 1):
        waits: list[float] = []
        skip_models: set[str] = set()

        for lane, model in candidates:
            if model in skip_models or bad_output.get(model, 0) >= 2 or (lane.label, model) in dead:
                continue
            cool_key = (lane.fingerprint, model)
            remaining = _cooldown_remaining(cool_key)
            if remaining > 0:
                waits.append(remaining)
                continue

            logger.info("[%s] trying %s (%s)", label, model, lane.label)
            try:
                response = lane.client().chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    temperature=0.1,
                    max_tokens=max_tokens,
                )
                usage.add(getattr(response, "usage", None))
            except RateLimitError as e:
                wait = _retry_after_seconds(e) or LLM_BACKOFF_SECONDS * batch
                _set_cooldown(cool_key, wait)
                waits.append(wait)
                last_error = e
                logger.warning("[%s] %s (%s) rate-limited, cooling %.0fs", label, model, lane.label, wait)
                continue
            except (APIConnectionError, APITimeoutError) as e:
                _set_cooldown(cool_key, 5)
                waits.append(5)
                last_error = e
                logger.warning("[%s] %s (%s) connection problem: %s", label, model, lane.label, e)
                continue
            except APIStatusError as e:
                last_error = e
                if e.status_code >= 500:
                    _set_cooldown(cool_key, 10)
                    waits.append(10)
                    logger.warning("[%s] %s (%s) server error %s", label, model, lane.label, e.status_code)
                else:
                    dead.add((lane.label, model))
                    logger.warning("[%s] %s (%s) error %s, skipping: %s", label, model, lane.label, e.status_code, e)
                continue
            except OpenAIError as e:
                dead.add((lane.label, model))
                last_error = e
                logger.warning("[%s] %s (%s) API error: %s", label, model, lane.label, e)
                continue

            try:
                if not response.choices or not response.choices[0].message.content:
                    raise ValueError("empty response")
                result = _extract_json(response.choices[0].message.content)
                if not isinstance(result, dict):
                    raise ValueError("response is not a JSON object")
                missing = required_keys - result.keys()
                if missing:
                    raise ValueError(f"missing keys: {sorted(missing)}")
                if validate:
                    result = validate(result)
            except (ValueError, json.JSONDecodeError) as e:   # EditError is a ValueError
                bad_output[model] = bad_output.get(model, 0) + 1
                skip_models.add(model)
                last_error = e
                logger.warning("[%s] %s gave unusable output: %s", label, model, e)
                continue

            logger.info("[%s] success with %s (%s)", label, model, lane.label)
            return result

        if not waits:
            break   # nothing is rate-limited; every model failed for good, waiting won't help
        if batch == LLM_MAX_BATCHES:
            break
        wait = max(1.0, min(waits))
        if wait > LLM_MAX_WAIT_SECONDS:
            raise LLMError(
                f"All {label} models are rate-limited; earliest reset in {wait:.0f}s "
                f"(free-tier quota likely used up — add OPENROUTER_API_KEY_2 or your own key)"
            )
        logger.warning(
            "⏳ [%s] every model is rate-limited — waiting %.0fs, then retrying batch %d/%d",
            label, wait, batch + 1, LLM_MAX_BATCHES,
        )
        time.sleep(wait)

    raise LLMError(f"All {label} models failed. Last: {last_error}")


def _models(defaults: list[str], user_key: str | None, user_model: str | None) -> list[str]:
    # A custom model is only honoured with the user's own key, so it can't bill ours
    return [user_model] if (user_key and user_model) else defaults


def _numbered_excerpt(source: str, lineno: int | None) -> tuple[str, str]:
    """Return (header, text) — the whole file, or a window around the crash line for big files."""
    lines = source.splitlines()
    if len(lines) <= EXCERPT_THRESHOLD_LINES or not isinstance(lineno, int):
        return "full file", source
    start = max(1, lineno - EXCERPT_RADIUS_LINES)
    end = min(len(lines), lineno + EXCERPT_RADIUS_LINES)
    return f"excerpt, lines {start}-{end} of {len(lines)}", "\n".join(lines[start - 1:end])


def analyze(
    error_message: str,
    stack_trace: str,
    filename: str,
    usage: TokenUsage,
    user_key: str | None = None,
    user_model: str | None = None,
) -> dict:
    """Step 1: Small LLM identifies root cause. No file content — keeps context tiny."""
    user = f"""Error: {error_message}

Stack trace:
{stack_trace}

Affected file: {filename}"""
    return _call_json(
        _candidates(_models(ANALYZER_MODELS, user_key, user_model), _lanes(user_key)),
        ANALYZER_SYSTEM, user,
        required_keys={"root_cause", "buggy_line", "fix_strategy", "scope"},
        label="analyzer", usage=usage, max_tokens=1024,
    )


def _clean_test_path(raw, source_path: str, existing: set[str]) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("test_path missing")
    path = posixpath.normpath(raw.strip().replace("\\", "/").lstrip("/"))
    if path.startswith("..") or not path.endswith(".py") or path == source_path:
        raise ValueError(f"bad test_path {raw!r}")
    base = posixpath.basename(path)
    if not base.startswith("test_"):
        path = posixpath.join(posixpath.dirname(path), f"test_{base}")
    root, ext = posixpath.splitext(path)
    n = 1
    while path in existing:   # never overwrite an existing test file
        path = f"{root}_devloop{n}{ext}"
        n += 1
    return path


def propose_fix(
    analysis: dict,
    filename: str,
    source: str,
    lineno: int | None,
    test_context: str,
    existing_paths: set[str],
    usage: TokenUsage,
    user_key: str | None = None,
    user_model: str | None = None,
) -> dict:
    """Step 2: Code LLM returns search/replace edits + a regression test.

    Returns {"files": {path: new content}, "test_path", "fix_summary"}.
    """
    header, text = _numbered_excerpt(source, lineno)
    user = f"""Root cause: {analysis['root_cause']}
Buggy line: {analysis['buggy_line']}
Fix strategy: {analysis['fix_strategy']}
Scope: {analysis['scope']}

Source path: {filename}
Source ({header}):
{text}

Tests in this repo:
{test_context}"""

    def validate(result: dict) -> dict:
        fixed = apply_edits({filename: source}, result["edits"])[filename]
        if fixed == source:
            raise EditError("edits did not change the file")
        test_path = _clean_test_path(result["test_path"], filename, existing_paths)
        test_code = result["test_code"]
        if not isinstance(test_code, str) or "def test" not in test_code:
            raise ValueError("test_code has no test function")
        return {
            "files": {filename: fixed, test_path: test_code},
            "test_path": test_path,
            "fix_summary": str(result.get("fix_summary", "")).strip(),
        }

    return _call_json(
        _candidates(_models(FIXER_MODELS, user_key, user_model), _lanes(user_key)),
        FIXER_SYSTEM, user,
        required_keys={"edits", "test_path", "test_code", "fix_summary"},
        label="fixer", usage=usage, max_tokens=4096, validate=validate,
    )


def refine_fix(
    analysis: dict,
    files: dict[str, str],
    filename: str,
    test_path: str,
    failure_output: str,
    usage: TokenUsage,
    user_key: str | None = None,
    user_model: str | None = None,
) -> dict:
    """Self-heal step: feed the failing test output back and get follow-up edits.

    Returns {"files": {path: new content}, "fix_summary"}.
    """
    user = f"""Root cause: {analysis['root_cause']}
Fix strategy: {analysis['fix_strategy']}

Source path: {filename}
Current source:
{files[filename]}

Regression test path: {test_path}
Current test:
{files[test_path]}

Failing test output (tail):
{failure_output[-3000:]}"""

    def validate(result: dict) -> dict:
        new_files = apply_edits(files, result["edits"])
        if new_files == files:
            raise EditError("edits did not change anything")
        return {"files": new_files, "fix_summary": str(result.get("fix_summary", "")).strip()}

    return _call_json(
        _candidates(_models(FIXER_MODELS, user_key, user_model), _lanes(user_key)),
        REFINE_SYSTEM, user,
        required_keys={"edits", "fix_summary"},
        label="refiner", usage=usage, max_tokens=4096, validate=validate,
    )
