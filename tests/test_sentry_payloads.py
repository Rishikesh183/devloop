"""Sentry webhook shapes: legacy plugin, Internal Integration alert/error/issue."""
import hashlib
import hmac
import json
import os

os.environ.setdefault("JWT_SECRET", "test-secret")

import main  # noqa: E402

EVENT = {
    "event_id": "e1",
    "environment": "production",
    "exception": {"values": [{
        "type": "TypeError",
        "value": "bad operand",
        "stacktrace": {"frames": [
            {"filename": "lib/x.py", "lineno": 1, "function": "a", "in_app": False},
            {"filename": "store/pricing.py", "lineno": 2, "function": "apply_discount", "in_app": True},
        ]},
    }]},
}


def test_legacy_plugin_shape():
    p = main.parse_sentry_payload({"project": "p", "event": EVENT})
    assert (p["filename"], p["lineno"], p["error_message"]) == ("store/pricing.py", 2, "TypeError: bad operand")


def test_internal_integration_alert_shape():
    p = main.parse_sentry_payload({"action": "triggered", "data": {"event": EVENT, "triggered_rule": "r"}})
    assert p["filename"] == "store/pricing.py" and p["lineno"] == 2


def test_internal_integration_error_shape():
    p = main.parse_sentry_payload({"action": "created", "data": {"error": EVENT}})
    assert p["filename"] == "store/pricing.py"


def test_issue_shape_falls_back_to_metadata():
    issue = {"title": "TypeError: bad operand", "metadata": {
        "type": "TypeError", "value": "bad operand", "filename": "store/pricing.py", "function": "apply_discount"}}
    p = main.parse_sentry_payload({"action": "created", "data": {"issue": issue}})
    assert (p["filename"], p["error_message"]) == ("store/pricing.py", "TypeError: bad operand")


def test_ignores_non_creation_events():
    assert main.sentry_should_ignore({"action": "resolved"}, "issue")
    assert main.sentry_should_ignore({"action": "created"}, "installation")
    assert main.sentry_should_ignore({"action": "created"}, "issue") is None
    assert main.sentry_should_ignore({"action": "triggered"}, "event_alert") is None


def test_webhook_endpoint_ignores_resolved(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "SENTRY_WEBHOOK_SECRET", "s")
    monkeypatch.setattr("db.repos.list_repos_with_sentry_secret", lambda: [])
    body = json.dumps({"action": "resolved", "data": {"issue": {}}}).encode()
    sig = hmac.new(b"s", body, hashlib.sha256).hexdigest()
    r = TestClient(main.app).post("/webhook/sentry", content=body, headers={
        "Sentry-Hook-Signature": sig, "Sentry-Hook-Resource": "issue"})
    assert r.json()["status"] == "ignored"
