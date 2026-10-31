import pytest


def test_parse_duration():
    from ke.settings import parse_duration
    assert parse_duration("30m") == 1800 and parse_duration("2h") == 7200 and parse_duration("7d") == 604800


def test_parse_duration_variants():
    from ke.settings import parse_duration
    assert parse_duration("45s") == 45 and parse_duration(" 8h ") == 28800
    assert parse_duration(90) == 90 and parse_duration("90") == 90
    for bad in ("", "abc", "5x", "-3m"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_settings_defaults_match_spec():
    from ke.settings import Settings
    s = Settings.from_mapping({})
    assert s.reminders_s == (1800, 7200) and s.escalate_after_s == 28800 and s.digest_every_s == 86400
    assert s.guard_alert_after_s == 1800 and s.infra_undo_count == 2 and s.infra_undo_window_s == 86400
    assert s.owner_for("qualquer") == "sponsor"


def test_settings_other_defaults(monkeypatch, tmp_path):
    from ke.settings import Settings
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    s = Settings.from_mapping({})
    assert s.mode == "enforce" and s.coordinator_profile == "default"
    assert s.pin_live_panel is True and s.default_owner == "sponsor"
    assert s.escalate_to is None and s.human_kinds == ("needs_input",)
    assert s.require_structured_ask is True and s.reply_ttl_s == 604800
    assert s.infra_alert_count == 3 and s.infra_alert_window_s == 3600
    assert s.infra_patterns == (
        "Kanban execution unknown", "retained by owner",
        "Gateway connection failed", "WorkerPersistenceError",
    )
    assert s.owner_ids == {} and s.board_owner == {}
    assert s.shared_state_dir.parts[-2:] == ("plugin-data", "kanban-empowered")
    assert s.shared_state_dir.parent.parent.resolve() == tmp_path.resolve()


def test_settings_from_mapping_overrides():
    from ke.settings import Settings
    s = Settings.from_mapping({
        "mode": "observe",
        "shared_state_dir": "/tmp/x",
        "telegram": {"owner_ids": {"sponsor": "123"}, "pin_live_panel": False, "reply_ttl": "1d"},
        "owners": {"default": "ana", "boards": {"b1": "bob"}},
        "waits": {"reminders": ["10m", 3600], "escalate_after": "4h", "digest_every": "12h",
                  "escalate_to": "bob", "human_kinds": ["needs_input", "approval"],
                  "require_structured_ask": False},
        "guards": {"alert_after": "15m"},
        "infra_failures": {"undo_budget": 5, "undo_window": "2d", "alert_count": 4,
                           "alert_window": "30m", "patterns": ["boom"]},
    })
    assert s.mode == "observe" and str(s.shared_state_dir).replace("\\", "/") == "/tmp/x"
    assert s.owner_ids == {"sponsor": 123} and s.pin_live_panel is False and s.reply_ttl_s == 86400
    assert s.default_owner == "ana" and s.owner_for("b1") == "bob" and s.owner_for("b2") == "ana"
    assert s.reminders_s == (600, 3600) and s.escalate_after_s == 14400 and s.digest_every_s == 43200
    assert s.escalate_to == "bob" and s.human_kinds == ("needs_input", "approval")
    assert s.require_structured_ask is False and s.guard_alert_after_s == 900
    assert s.infra_undo_count == 5 and s.infra_undo_window_s == 172800
    assert s.infra_alert_count == 4 and s.infra_alert_window_s == 1800 and s.infra_patterns == ("boom",)


def test_settings_is_frozen():
    from dataclasses import FrozenInstanceError
    from ke.settings import Settings
    with pytest.raises(FrozenInstanceError):
        Settings.from_mapping({}).mode = "x"


def test_settings_from_ctx_reads_dotted_keys():
    from ke.settings import Settings
    values = {"waits.reminders": ["5m"], "waits.escalate_after": "1h",
              "telegram.owner_ids": {"sponsor": 7}, "mode": "observe"}
    seen = []

    class Ctx:
        def get_config(self, key, default=None):
            seen.append(key)
            return values.get(key, default)

    s = Settings.from_ctx(Ctx())
    assert s.reminders_s == (300,) and s.escalate_after_s == 3600
    assert s.owner_ids == {"sponsor": 7} and s.mode == "observe"
    assert s.digest_every_s == 86400 and s.guard_alert_after_s == 1800
    assert "guards.alert_after" in seen and "infra_failures.undo_budget" in seen
