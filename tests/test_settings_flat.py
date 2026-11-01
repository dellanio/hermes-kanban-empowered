"""Settings mapping for the Hermes Desktop settings form (flat keys, flat wins over nested)."""
from __future__ import annotations

import pytest


class FlatCtx:
    """``ctx.get_config`` over a settings dict, walking dotted keys like Hermes does."""

    def __init__(self, settings: dict) -> None:
        self.settings = settings

    def get_config(self, key, default=None):
        cur = self.settings
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


# ---- Settings mapping -------------------------------------------------------------------------


def test_flat_form_keys_map_to_settings():
    from ke.settings import Settings

    s = Settings.from_mapping({
        "language": "pt", "mode": "cooperative-only", "default_owner": "ana", "reminders": "10m, 1h",
        "escalate_after": "4h", "digest": "weekly", "escalate_to": "bob", "require_structured_ask": False,
        "pin_live_panel": False, "guard_alert_after": "15m", "undo_budget_count": 5, "undo_budget_window": "2d",
    })
    assert s.language == "pt" and s.mode == "cooperative-only" and s.default_owner == "ana"
    assert s.reminders_s == (600, 3600) and s.escalate_after_s == 14400 and s.digest_every_s == 604800
    assert s.escalate_to == "bob" and s.require_structured_ask is False and s.pin_live_panel is False
    assert s.guard_alert_after_s == 900 and s.infra_undo_count == 5 and s.infra_undo_window_s == 172800


def test_flat_wins_over_nested():
    from ke.settings import Settings

    s = Settings.from_mapping({
        "default_owner": "flat", "waits": {"default_owner": "nested", "reminders": ["5m"]},
        "reminders": "1h", "pin_live_panel": False, "telegram": {"pin_live_panel": True},
        "infra_failures": {"undo_budget": {"count": 9, "window": "3d"}}, "undo_budget_count": 1,
    })
    assert s.default_owner == "flat" and s.reminders_s == (3600,) and s.pin_live_panel is False
    assert s.infra_undo_count == 1 and s.infra_undo_window_s == 259200  # window not overridden -> nested


def test_nested_still_works_without_flat():
    from ke.settings import Settings

    s = Settings.from_mapping({"waits": {"default_owner": "nested", "reminders": ["5m"]}})
    assert s.default_owner == "nested" and s.reminders_s == (300,)


def test_blank_form_strings_fall_back_to_nested_or_default():
    from ke.settings import Settings

    s = Settings.from_mapping({
        "language": "", "default_owner": "  ", "reminders": "", "escalate_after": "", "digest": "",
        "escalate_to": "", "guard_alert_after": "", "undo_budget_window": "",
        "waits": {"default_owner": "nested", "escalate_to": "bob"},
    })
    assert s.language is None and s.default_owner == "nested" and s.escalate_to == "bob"
    assert s.reminders_s == (1800, 7200) and s.escalate_after_s == 28800 and s.digest_every_s == 86400
    assert s.guard_alert_after_s == 1800 and s.infra_undo_window_s == 86400


def test_reminders_comma_separated_string_and_list_forms():
    from ke.settings import Settings

    assert Settings.from_mapping({"reminders": "30m,2h"}).reminders_s == (1800, 7200)
    assert Settings.from_mapping({"reminders": " 10m , 1h , 1d "}).reminders_s == (600, 3600, 86400)
    assert Settings.from_mapping({"reminders": "45m"}).reminders_s == (2700,)
    assert Settings.from_mapping({"waits": {"reminders": "30m,2h"}}).reminders_s == (1800, 7200)
    assert Settings.from_mapping({"waits": {"reminders": ["30m", "2h"]}}).reminders_s == (1800, 7200)
    with pytest.raises(ValueError):
        Settings.from_mapping({"reminders": "30m,abc"})


def test_telegram_owner_id_maps_to_default_owner():
    from ke.settings import Settings

    assert Settings.from_mapping({"telegram_owner_id": 12345}).owner_ids == {"sponsor": 12345}
    s = Settings.from_mapping({"telegram_owner_id": 12345, "default_owner": "ana"})
    assert s.owner_ids == {"ana": 12345}
    s = Settings.from_mapping({"telegram_owner_id": "-100777", "waits": {"default_owner": "ana"}})
    assert s.owner_ids == {"ana": -100777}


def test_explicit_owner_ids_win_over_telegram_owner_id():
    from ke.settings import Settings

    s = Settings.from_mapping({"telegram_owner_id": 1, "telegram": {"owner_ids": {"sponsor": 2, "bob": 3}}})
    assert s.owner_ids == {"sponsor": 2, "bob": 3}


def test_telegram_owner_id_blank_or_absent_means_no_owner():
    from ke.settings import Settings

    assert Settings.from_mapping({"telegram_owner_id": ""}).owner_ids == {}
    assert Settings.from_mapping({}).owner_ids == {}
    with pytest.raises(ValueError):
        Settings.from_mapping({"telegram_owner_id": "abc"})


def test_from_ctx_reads_flat_form_keys():
    from ke.settings import Settings

    ctx = FlatCtx({
        "default_owner": "ana", "reminders": "5m,1h", "telegram_owner_id": 42, "undo_budget_count": 4,
        "undo_budget_window": "12h", "guard_alert_after": "20m", "digest": "hourly", "escalate_after": "6h",
        "escalate_to": "bob", "pin_live_panel": False, "require_structured_ask": False,
    })
    s = Settings.from_ctx(ctx)
    assert s.default_owner == "ana" and s.reminders_s == (300, 3600) and s.owner_ids == {"ana": 42}
    assert s.infra_undo_count == 4 and s.infra_undo_window_s == 43200 and s.guard_alert_after_s == 1200
    assert s.digest_every_s == 3600 and s.escalate_after_s == 21600 and s.escalate_to == "bob"
    assert s.pin_live_panel is False and s.require_structured_ask is False


def test_from_ctx_flat_wins_over_nested():
    from ke.settings import Settings

    ctx = FlatCtx({"default_owner": "flat", "waits": {"default_owner": "nested"}})
    assert Settings.from_ctx(ctx).default_owner == "flat"
