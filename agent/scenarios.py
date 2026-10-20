"""'Break prod' demo scenarios for the demo repo.

Each scenario is a complete version of store/pricing.py with one realistic bug.
DevLoop pushes it to a fresh incident/* branch (a "bad deploy"), raises a
Sentry-shaped alert for the crash it causes, then runs the normal pipeline
against that branch. Writing the whole file keeps scenarios independent of
whatever state the demo repo's main branch is in.
"""
import os
import posixpath
import uuid
from dataclasses import dataclass

# Folder inside DEMO_REPO that contains store/ and tests/ ("" if they're at the root)
DEMO_APP_ROOT = os.getenv("DEMO_APP_ROOT", "")
SOURCE_REL = "store/pricing.py"


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    commit_message: str
    source: str
    exc_type: str
    exc_value: str
    frames: tuple[tuple[str, str], ...]   # (function, text on the line) — oldest call first


_HEAD = '''def apply_discount(price, discount_percent):
    return price - (price * discount_percent / 100)

'''

SCENARIOS = [
    Scenario(
        id="none-discount",
        title="Seasonal discount returns None",
        commit_message="feat: drive discounts from active seasons",
        source=_HEAD + '''def calculate_order_total(items):
    subtotal = 0
    for item in items:
        subtotal += item["price"] * item["quantity"]

    discount = get_seasonal_discount()
    total = apply_discount(subtotal, discount)
    return round(total, 2)

def get_seasonal_discount():
    active_seasons = []
    if active_seasons:
        return active_seasons[0]["discount"]
    return None
''',
        exc_type="TypeError",
        exc_value="unsupported operand type(s) for *: 'int' and 'NoneType'",
        frames=(
            ("calculate_order_total", "total = apply_discount(subtotal, discount)"),
            ("apply_discount", "return price - (price * discount_percent / 100)"),
        ),
    ),
    Scenario(
        id="renamed-field",
        title="Cart field renamed to qty",
        commit_message="refactor: shorter cart field names",
        source=_HEAD + '''def calculate_order_total(items):
    subtotal = 0
    for item in items:
        subtotal += item["price"] * item["qty"]

    discount = get_seasonal_discount()
    total = apply_discount(subtotal, discount)
    return round(total, 2)

def get_seasonal_discount():
    return 0
''',
        exc_type="KeyError",
        exc_value="'qty'",
        frames=(("calculate_order_total", 'subtotal += item["price"] * item["qty"]'),),
    ),
    Scenario(
        id="off-by-one",
        title="Off-by-one in order loop",
        commit_message="perf: index-based loop over cart items",
        source=_HEAD + '''def calculate_order_total(items):
    subtotal = 0
    for i in range(len(items) + 1):
        item = items[i]
        subtotal += item["price"] * item["quantity"]

    discount = get_seasonal_discount()
    total = apply_discount(subtotal, discount)
    return round(total, 2)

def get_seasonal_discount():
    return 0
''',
        exc_type="IndexError",
        exc_value="list index out of range",
        frames=(("calculate_order_total", "item = items[i]"),),
    ),
    Scenario(
        id="string-config",
        title="Discount read from config as a string",
        commit_message="feat: discount percentage from env config",
        source=_HEAD + '''DISCOUNT_CONFIG = {"seasonal_percent": "0"}

def calculate_order_total(items):
    subtotal = 0
    for item in items:
        subtotal += item["price"] * item["quantity"]

    discount = get_seasonal_discount()
    total = apply_discount(subtotal, discount)
    return round(total, 2)

def get_seasonal_discount():
    return DISCOUNT_CONFIG["seasonal_percent"]
''',
        exc_type="TypeError",
        exc_value="unsupported operand type(s) for /: 'str' and 'int'",
        frames=(
            ("calculate_order_total", "total = apply_discount(subtotal, discount)"),
            ("apply_discount", "return price - (price * discount_percent / 100)"),
        ),
    ),
]

SCENARIOS_BY_ID = {s.id: s for s in SCENARIOS}


def source_path() -> str:
    return posixpath.join(DEMO_APP_ROOT, SOURCE_REL) if DEMO_APP_ROOT else SOURCE_REL


def _lineno(source: str, needle: str) -> int:
    for i, line in enumerate(source.splitlines(), start=1):
        if needle in line:
            return i
    raise ValueError(f"scenario line not found: {needle!r}")


def build_sentry_payload(scenario: Scenario, environment: str = "production") -> dict:
    """A Sentry webhook payload (same shape parse_sentry_payload expects) for this crash."""
    path = source_path()
    lines = scenario.source.splitlines()
    frames = []
    for function, needle in scenario.frames:
        lineno = _lineno(scenario.source, needle)
        frames.append({
            "filename": path,
            "lineno": lineno,
            "function": function,
            "in_app": True,
            "context_line": lines[lineno - 1],
        })
    return {
        "project": "devloop-demo-app",
        "environment": environment,
        "event": {
            "event_id": uuid.uuid4().hex,
            "title": f"{scenario.exc_type}: {scenario.exc_value}",
            "environment": environment,
            "exception": {"values": [{
                "type": scenario.exc_type,
                "value": scenario.exc_value,
                "stacktrace": {"frames": frames},
            }]},
        },
    }
