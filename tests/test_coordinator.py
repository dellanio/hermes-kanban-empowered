import logging

import pytest

from conftest import make_coordinator


def test_coordinator_ignores_non_coordinator_profile(tmp_path, monkeypatch):
    c = make_coordinator(tmp_path, coordinator_profile="default", active_profile="builder")
    c.on_tick("matrix", result=None)
    assert c.store.kv_get("last_eval") is None


def test_evaluates_and_records_last_eval(tmp_path, clock):
    c = make_coordinator(tmp_path, clock=clock)
    c.on_tick("matrix", result=None)
    assert float(c.store.kv_get("last_eval")) == clock.now


def test_throttled_to_once_per_30s(tmp_path, clock):
    c = make_coordinator(tmp_path, clock=clock)
    calls = []
    c.register_step("waits", lambda now: calls.append(now))
    c.on_tick("a", result=None)
    clock.advance(29)
    c.on_tick("b", result=None)
    assert len(calls) == 1
    clock.advance(1)
    c.on_tick("b", result=None)
    assert len(calls) == 2


def test_dry_run_is_ignored(tmp_path, clock):
    c = make_coordinator(tmp_path, clock=clock)
    c.on_tick("a", result=None, dry_run=True)
    assert c.store.kv_get("last_eval") is None


def test_unknown_hook_kwargs_are_accepted(tmp_path, clock):
    c = make_coordinator(tmp_path, clock=clock)
    c.on_tick("a", result=None, profile_name="default", outcome="ok", extra=1)
    assert c.store.kv_get("last_eval") is not None


def test_steps_run_in_order_and_receive_now(tmp_path, clock):
    c = make_coordinator(tmp_path, clock=clock)
    order = []
    for name in ("panel", "infra", "guards", "waits"):  # registration order must not matter
        c.register_step(name, lambda now, n=name: order.append((n, now)))
    c.evaluate(123.0)
    assert order == [("waits", 123.0), ("guards", 123.0), ("infra", 123.0), ("panel", 123.0)]


def test_failing_step_does_not_stop_the_others(tmp_path, clock, caplog):
    c = make_coordinator(tmp_path, clock=clock)
    ran = []

    def boom(now):
        raise RuntimeError("x")

    c.register_step("waits", boom)
    c.register_step("guards", lambda now: ran.append("guards"))
    with caplog.at_level(logging.ERROR):
        c.evaluate(1.0)
    assert ran == ["guards"]


def test_on_tick_never_raises_into_the_dispatcher(tmp_path, clock):
    c = make_coordinator(tmp_path, clock=clock)

    def boom(now):
        raise RuntimeError("x")

    c.register_step("waits", boom)
    c.on_tick("a", result=None)  # must not raise


def test_unknown_step_name_rejected(tmp_path):
    c = make_coordinator(tmp_path)
    with pytest.raises(ValueError):
        c.register_step("nope", lambda now: None)


def test_missing_veto_hooks_warns_once(tmp_path, clock, caplog):
    c = make_coordinator(tmp_path, clock=clock, veto_hooks=False)
    with caplog.at_level(logging.WARNING):
        c.evaluate(1.0)
        c.evaluate(2.0)
    assert c.store.kv_get("veto_hooks_warned") == "1"
    warnings = [r for r in caplog.records if "veto" in r.getMessage().lower()]
    assert len(warnings) == 1


def test_present_veto_hooks_no_warning(tmp_path, clock, caplog):
    c = make_coordinator(tmp_path, clock=clock, veto_hooks=True)
    with caplog.at_level(logging.WARNING):
        c.evaluate(1.0)
    assert c.store.kv_get("veto_hooks_warned") is None
    assert not [r for r in caplog.records if "veto" in r.getMessage().lower()]
