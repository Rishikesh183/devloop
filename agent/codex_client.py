import json
import logging
import os
import time

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

load_dotenv()

logger = logging.getLogger("devloop.codex_client")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_API_KEY_2 = os.getenv("OPENROUTER_API_KEY_2", "")

# Step 1: Analyzer — small/fast models, just reads error + trace
# Job: identify root cause, pinpoint exact problem. No code generation.
ANALYZER_MODELS = [
    "meta-llama/llama-3.2-3b-instruct:free",   # tiny, fast, different bucket
    "liquid/lfm-2.5-1.2b-instruct:free",        # smallest available
    "nvidia/nemotron-nano-9b-v2:free",
    "meta-llama/llama-3.3-70b-instruct:free",   # fallback
]

# Step 2: Fixer — code-focused models, receives only the file + analysis
# Job: write the minimal fix. Smaller context since analysis is pre-digested.
FIXER_MODELS = [
    "qwen/qwen3-coder:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "deepseek/deepseek-v4-flash:free",
    "meta-llama/llama-3.3-70b-instruct:free",   # fallback
]

ANALYZER_SYSTEM = """You are a senior software engineer analyzing a production error.
Given an error message and stack trace, identify the root cause precisely.

Respond in this exact JSON format with no markdown, no text outside JSON:
{
  "root_cause": "one sentence: what is wrong and why",
  "buggy_line": "the exact line of code that is the source of the bug",
  "fix_strategy": "one sentence: what change will fix it",
  "scope": "minimal — what lines/logic to change"
}"""

FIXER_SYSTEM = """You are an expert software engineer writing a production bug fix.
You will receive a file and a pre-analyzed root cause. Your job:
1. Apply the minimal fix described in the analysis
2. Return the complete corrected file content
3. Do NOT refactor, rename, or change anything beyond the bug fix

Respond in this exact JSON format with no markdown, no text outside JSON:
{
  "fixed_code": "...complete corrected file content...",
  "fix_summary": "...exactly what lines you changed and why..."
}"""


def _openrouter_client(api_key: str) -> OpenAI:
    return OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        max_retries=0,
        default_headers={"HTTP-Referer": "https://github.com/devloop", "X-Title": "DevLoop"},
    )


def _get_openrouter_keys() -> list[str]:
    return [k for k in [OPENROUTER_API_KEY, OPENROUTER_API_KEY_2] if k]


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])
    return text.strip()


def _call_model(client: OpenAI, model: str, system: str, user: str) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
    )
    if not response.choices:
        raise ValueError(f"Model {model} returned no choices")
    content = response.choices[0].message.content
    if not content:
        raise ValueError(f"Model {model} returned empty content (choices[0].message.content is None)")
    return content.strip()


def _try_models(keys: list[str], models: list[str], system: str, user: str, required_keys: set, label: str) -> dict:
    """Try each model × each key. Return parsed dict on first success, raise if all fail."""
    seen: set = set()
    model_list = [m for m in models if m and m not in seen and not seen.add(m)]

    last_error = None
    for model in model_list:
        for i, key in enumerate(keys):
            key_label = f"key{i + 1}"
            logger.info("[%s] trying %s (%s)", label, model, key_label)
            try:
                client = _openrouter_client(key)
                raw = _call_model(client, model, system, user)
                raw = _strip_fences(raw)
                result = json.loads(raw)
                missing = required_keys - result.keys()
                if missing:
                    raise ValueError(f"Missing keys: {missing}")
                logger.info("[%s] success with %s (%s)", label, model, key_label)
                return result
            except OpenAIError as e:
                status = getattr(e, "status_code", None)
                if status == 429:
                    logger.warning("[%s] %s (%s) rate-limited, trying next", label, model, key_label)
                else:
                    logger.warning("[%s] %s (%s) API error: %s", label, model, key_label, e)
                last_error = e
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("[%s] %s parse error: %s", label, model, e)
                last_error = e
                break  # parse error is model output problem, not key problem — skip to next model

    raise RuntimeError(f"All {label} models failed. Last: {last_error}")


def _analyze(keys: list[str], error_message: str, stack_trace: str, filename: str) -> dict:
    """Step 1: Small LLM identifies root cause. No file content — keeps context tiny."""
    user = f"""Error: {error_message}

Stack trace:
{stack_trace}

Affected file: {filename}"""

    return _try_models(
        keys, ANALYZER_MODELS, ANALYZER_SYSTEM, user,
        required_keys={"root_cause", "buggy_line", "fix_strategy", "scope"},
        label="analyzer",
    )


def _fix(keys: list[str], analysis: dict, file_content: str, filename: str) -> dict:
    """Step 2: Code LLM applies the fix. Gets file + pre-digested analysis only."""
    user = f"""Root cause: {analysis['root_cause']}
Buggy line: {analysis['buggy_line']}
Fix strategy: {analysis['fix_strategy']}
Scope: {analysis['scope']}

File ({filename}):
{file_content}"""

    return _try_models(
        keys, FIXER_MODELS, FIXER_SYSTEM, user,
        required_keys={"fixed_code", "fix_summary"},
        label="fixer",
    )


def generate_fix(
    error_message: str,
    stack_trace: str,
    file_content: str,
    filename: str,
) -> dict:
    keys = _get_openrouter_keys()
    if not keys and not OPENAI_API_KEY:
        raise ValueError("No LLM credentials. Set OPENROUTER_API_KEY or OPENAI_API_KEY in .env")

    if keys:
        logger.info("Starting two-LLM pipeline for: %s (%d OpenRouter key(s))", filename, len(keys))
    else:
        logger.info("Starting two-LLM pipeline for: %s (OpenAI fallback)", filename)
        keys = [OPENAI_API_KEY]  # reuse same flow; OpenAI client built below is unused

    MAX_RETRIES = 3
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("Pipeline attempt %d/%d", attempt, MAX_RETRIES)

            # Step 1: Analyze — fast small model, no file content
            logger.info("   Step 2a — Analyzer LLM pinpointing root cause...")
            analysis = _analyze(keys, error_message, stack_trace, filename)
            logger.info("   ✓ Root cause: %s", analysis["root_cause"])
            logger.info("   ✓ Buggy line: %s", analysis.get("buggy_line", "?"))
            logger.info("   ✓ Fix strategy: %s", analysis["fix_strategy"])

            # Step 2: Fix — code model gets file + analysis summary only
            logger.info("   Step 2b — Fixer LLM writing patch...")
            fix = _fix(keys, analysis, file_content, filename)
            logger.info("   ✓ Patch generated (%d chars)", len(fix.get("fixed_code", "")))

            return {
                "fixed_code": fix["fixed_code"],
                "root_cause": analysis["root_cause"],
                "fix_summary": fix["fix_summary"],
                "analysis": analysis,
            }

        except RuntimeError as e:
            logger.warning("Attempt %d failed: %s", attempt, e)
            last_error = e

        if attempt < MAX_RETRIES:
            wait = 5.0 * attempt
            logger.info("Waiting %.0fs before retry...", wait)
            time.sleep(wait)

    logger.error("Pipeline failed after %d attempts: %s", MAX_RETRIES, last_error)
    raise RuntimeError(f"Fix generation failed after {MAX_RETRIES} attempts: {last_error}")
