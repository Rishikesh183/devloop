"""Every 'break prod' scenario must crash the way its Sentry payload claims."""
import pytest

from agent.scenarios import SCENARIOS, build_sentry_payload, source_path

CART = [{"price": 50, "quantity": 2}]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_scenario_crashes_as_advertised(scenario):
    module: dict = {}
    exec(compile(scenario.source, "pricing.py", "exec"), module)
    with pytest.raises(Exception) as info:
        module["calculate_order_total"](CART)
    assert type(info.value).__name__ == scenario.exc_type
    assert scenario.exc_value in str(info.value) or repr(str(info.value)) == scenario.exc_value
    # The demo app's existing test of apply_discount keeps passing — only the bug is new
    assert module["apply_discount"](100, 10) == 90.0


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_payload_points_at_crash_line(scenario):
    frames = build_sentry_payload(scenario)["event"]["exception"]["values"][0]["stacktrace"]["frames"]
    lines = scenario.source.splitlines()
    for frame, (_, needle) in zip(frames, scenario.frames):
        assert frame["filename"] == source_path()
        assert needle in lines[frame["lineno"] - 1]
