"""Structured pipeline events for the dashboard.

main.py installs a sink that forwards events to the user's SSE stream
(as `event: devloop`). Each run also keeps its own list so the full
timeline can be stored on the run record and replayed later.
"""
import logging
import time
from typing import Callable

logger = logging.getLogger("devloop.events")

_sink: Callable[[str, dict], None] | None = None


def set_sink(fn: Callable[[str, dict], None] | None) -> None:
    global _sink
    _sink = fn


def publish(user_id: str, event: dict) -> None:
    if _sink is None:
        return
    try:
        _sink(user_id, event)
    except Exception as e:
        logger.debug("Event sink failed: %s", e)


class RunEvents:
    """Collects one run's events and publishes each one live."""

    def __init__(self, user_id: str, run_key: str, prior: list[dict] | None = None):
        self.user_id = user_id
        self.run_key = run_key
        self.events: list[dict] = list(prior or [])

    def emit(self, type_: str, **fields) -> dict:
        event = {"type": type_, "run": self.run_key, "ts": time.time(), **fields}
        self.events.append(event)
        publish(self.user_id, event)
        return event

    def step(self, step: str, status: str, **fields) -> dict:
        return self.emit("step", step=step, status=status, **fields)
