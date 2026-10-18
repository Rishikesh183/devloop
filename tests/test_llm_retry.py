"""Batch-wise rate-limit handling in codex_client._call_json, with a fake OpenAI client."""
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import RateLimitError

from agent import codex_client
from agent.codex_client import LLMError, TokenUsage, _Lane


def _rate_limit(reset_in: float | None = None) -> RateLimitError:
    headers = {"retry-after": str(reset_in)} if reset_in is not None else {}
    resp = httpx.Response(429, headers=headers, request=httpx.Request("POST", "https://x"))
    return RateLimitError("rate limited", response=resp, body=None)


def _ok(payload: dict):
    msg = SimpleNamespace(content=json.dumps(payload))
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


class FakeLane(_Lane):
    def __init__(self, label, script):
        super().__init__(label, f"key-{label}-xxxxxxxx", "https://fake")
        self.script = script   # model -> list of results/exceptions, consumed in order

    def client(self):
        lane = self

        def create(model, **_):
            item = lane.script[model].pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    codex_client._cooldowns.clear()
    sleeps = []
    monkeypatch.setattr(codex_client.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(codex_client, "LLM_MAX_WAIT_SECONDS", 60)
    return sleeps


def test_rate_limited_batch_waits_then_retries(fast, monkeypatch):
    # Both keys limited on the only model in batch 1; batch 2 succeeds.
    # Fake the clock so cooldowns expire after the (mocked) sleep.
    clock = {"t": 1000.0}
    monkeypatch.setattr(codex_client.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(codex_client.time, "sleep", lambda s: (fast.append(s), clock.__setitem__("t", clock["t"] + s)))
    a = FakeLane("k1", {"m": [_rate_limit(3), _ok({"x": 1})]})
    b = FakeLane("k2", {"m": [_rate_limit(7)]})
    usage = TokenUsage()
    out = codex_client._call_json([(a, "m"), (b, "m")], "sys", "user", {"x"}, "t", usage, 100)
    assert out == {"x": 1}
    assert fast == [3.0]          # waited for the earliest reset only
    assert usage.total == 15


def test_falls_through_to_next_model_without_waiting(fast):
    a = FakeLane("k1", {"m1": [_rate_limit(30)], "m2": [_ok({"x": 2})]})
    out = codex_client._call_json([(a, "m1"), (a, "m2")], "s", "u", {"x"}, "t", TokenUsage(), 100)
    assert out == {"x": 2}
    assert fast == []


def test_bad_output_skips_other_keys_of_same_model(fast):
    bad = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))], usage=None)
    a = FakeLane("k1", {"m1": [bad], "m2": [_ok({"x": 3})]})
    b = FakeLane("k2", {"m1": []})   # would IndexError if called
    out = codex_client._call_json([(a, "m1"), (b, "m1"), (a, "m2")], "s", "u", {"x"}, "t", TokenUsage(), 100)
    assert out == {"x": 3}


def test_long_reset_fails_fast(fast):
    a = FakeLane("k1", {"m": [_rate_limit(3600)]})
    with pytest.raises(LLMError, match="quota"):
        codex_client._call_json([(a, "m")], "s", "u", {"x"}, "t", TokenUsage(), 100)
    assert fast == []


def test_validate_failure_moves_to_next_model(fast):
    a = FakeLane("k1", {"m1": [_ok({"x": 0})], "m2": [_ok({"x": 5})]})

    def validate(r):
        if r["x"] == 0:
            raise ValueError("bad edit")
        return r

    out = codex_client._call_json([(a, "m1"), (a, "m2")], "s", "u", {"x"}, "t", TokenUsage(), 100, validate=validate)
    assert out == {"x": 5}


def test_think_tags_and_prose_stripped():
    assert codex_client._extract_json('<think>hmm</think>Sure! {"a": 1} hope it helps') == {"a": 1}


def test_clean_test_path():
    existing = {"tests/test_pricing.py"}
    assert codex_client._clean_test_path("tests/test_pricing.py", "store/p.py", existing) == "tests/test_pricing_devloop1.py"
    assert codex_client._clean_test_path("tests/regression.py", "store/p.py", existing) == "tests/test_regression.py"
    with pytest.raises(ValueError):
        codex_client._clean_test_path("../evil.py", "store/p.py", existing)
