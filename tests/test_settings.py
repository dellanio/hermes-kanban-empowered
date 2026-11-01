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
        "mode": "cooperative-only",
        "shared_state_dir": "/tmp/x",
        "telegram": {"owner_ids": {"sponsor": "123"}, "pin_live_panel": False, "reply_ttl": "1d"},
        "boards": {"b1": {"waits": {"default_owner": "bob"}}},
        "waits": {"default_owner": "ana", "reminders": ["10m", 3600], "escalate_after": "4h", "digest": "12h",
                  "escalate_to": "bob", "human_kinds": ["needs_input", "approval"],
                  "require_structured_ask": False},
        "guards": {"alert_after": "15m"},
        "infra_failures": {"undo_budget": {"count": 5, "window": "2d"}, "alert_count": 4,
                           "alert_window": "30m", "patterns": ["boom"]},
    })
    assert s.mode == "cooperative-only" and str(s.shared_state_dir).replace("\\", "/") == "/tmp/x"
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
              "telegram.owner_ids": {"sponsor": 7}, "mode": "cooperative-only"}
    seen = []

    class Ctx:
        def get_config(self, key, default=None):
            seen.append(key)
            return values.get(key, default)

    s = Settings.from_ctx(Ctx())
    assert s.reminders_s == (300,) and s.escalate_after_s == 3600
    assert s.owner_ids == {"sponsor": 7} and s.mode == "cooperative-only"
    assert s.digest_every_s == 86400 and s.guard_alert_after_s == 1800
    assert "guards.alert_after" in seen and "infra_failures.undo_budget" in seen


# Every value below differs from the Settings default, so an unwired or
# misnamed key makes the assertions fail.
SECTION_8 = {
    "mode": "cooperative-only",
    "language": "PT",
    "show_sidebar_menu": False,
    "show_kanban_button": False,
    "coordinator_profile": "builder",
    "shared_state_dir": "/srv/ke-state",
    "telegram": {"owner_ids": {"ana": 111, "bob": 222}, "pin_live_panel": False,
                 "quiet_hours": None, "reply_ttl": "2d"},
    "waits": {"default_owner": "ana", "reminders": ["10m", "1h"], "escalate_after": "4h",
              "escalate_to": "bob", "digest": "weekly", "blocking_digest_every": "6h",
              "require_structured_ask": False,
              "human_kinds": ["needs_input", "approval"]},
    "guards": {"alert_after": "15m"},
    "infra_failures": {"undo_budget": {"count": 5, "window": "12h"},
                       "alert_count": 7, "alert_window": "2h", "patterns": ["boom", "kaput"]},
    "quota": {"groups": {"anthropic-principal": ["builder"]}, "backoff": ["5m"], "max_wait": "30m"},
    "boards": {"projeto-x": {"waits": {"default_owner": "bob"}},
               "outro": {"waits": {"reminders": ["5m"]}}},
}


def _assert_section8(s):
    assert s.mode == "cooperative-only" and s.coordinator_profile == "builder"
    assert str(s.shared_state_dir).replace("\\", "/") == "/srv/ke-state"
    assert s.owner_ids == {"ana": 111, "bob": 222} and s.pin_live_panel is False
    assert s.reply_ttl_s == 172800
    assert s.default_owner == "ana"
    assert s.owner_for("projeto-x") == "bob"
    assert s.owner_for("outro") == "ana" and s.owner_for("desconhecido") == "ana"
    assert s.reminders_s == (600, 3600) and s.escalate_after_s == 14400
    assert s.escalate_to == "bob" and s.digest_every_s == 604800 and s.blocking_digest_s == 21600
    assert s.require_structured_ask is False and s.human_kinds == ("needs_input", "approval")
    assert s.guard_alert_after_s == 900
    assert s.infra_undo_count == 5 and s.infra_undo_window_s == 43200
    assert s.infra_alert_count == 7 and s.infra_alert_window_s == 7200
    assert s.infra_patterns == ("boom", "kaput")
    assert s.language == "pt"
    assert s.show_sidebar_menu is False and s.show_kanban_button is False


def test_section8_fixture_is_all_non_default():
    """Guard the guard: every Settings field must differ from its default here."""
    import dataclasses
    from ke.settings import Settings
    s, d = Settings.from_mapping(SECTION_8), Settings.from_mapping({})
    same = [f.name for f in dataclasses.fields(Settings)
            if f.name != "conflicts"  # derived diagnostics, not a setting
            and getattr(s, f.name) == getattr(d, f.name)]
    assert same == []


def test_section8_yaml_structure_via_from_mapping():
    from ke.settings import Settings
    _assert_section8(Settings.from_mapping(SECTION_8))


def test_section8_yaml_structure_via_from_ctx():
    from ke.settings import Settings

    class Ctx:
        def get_config(self, key, default=None):
            cur = SECTION_8
            for part in key.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return default
                cur = cur[part]
            return cur

    _assert_section8(Settings.from_ctx(Ctx()))


def test_digest_aliases_and_durations():
    from ke.settings import Settings
    for raw, expected in (("daily", 86400), ("hourly", 3600), ("weekly", 604800), ("6h", 21600), (7200, 7200)):
        assert Settings.from_mapping({"waits": {"digest": raw}}).digest_every_s == expected
    with pytest.raises(ValueError):
        Settings.from_mapping({"waits": {"digest": "sometimes"}})


def test_undo_budget_bare_int_and_partial_mapping():
    from ke.settings import Settings
    s = Settings.from_mapping({"infra_failures": {"undo_budget": 4}})
    assert s.infra_undo_count == 4 and s.infra_undo_window_s == 86400
    s = Settings.from_mapping({"infra_failures": {"undo_budget": {"window": "1h"}}})
    assert s.infra_undo_count == 2 and s.infra_undo_window_s == 3600


def test_bool_strings_parsed_explicitly():
    from ke.settings import Settings
    for raw in ("false", "0", "no", "False", False, 0):
        s = Settings.from_mapping({"telegram": {"pin_live_panel": raw}, "waits": {"require_structured_ask": raw}})
        assert s.pin_live_panel is False and s.require_structured_ask is False
    for raw in ("true", "1", "yes", True):
        assert Settings.from_mapping({"telegram": {"pin_live_panel": raw}}).pin_live_panel is True
    with pytest.raises(ValueError, match="pin_live_panel"):
        Settings.from_mapping({"telegram": {"pin_live_panel": "maybe"}})


def test_mode_validated():
    from ke.settings import Settings
    assert Settings.from_mapping({"mode": "cooperative-only"}).mode == "cooperative-only"
    with pytest.raises(ValueError, match="mode"):
        Settings.from_mapping({"mode": "observe"})


def test_empty_string_boolean_rejected():
    from ke.settings import Settings
    with pytest.raises(ValueError, match="pin_live_panel"):
        Settings.from_mapping({"telegram": {"pin_live_panel": ""}})
    with pytest.raises(ValueError, match="require_structured_ask"):
        Settings.from_mapping({"waits": {"require_structured_ask": "  "}})


# -- shared_state_dir defaults to the COORDINATOR profile's plugin-data (final review I-3) ------
def test_default_shared_state_dir_is_the_coordinator_profiles_even_from_a_worker_profile(kanban_home, monkeypatch):
    from ke.settings import Settings

    worker = kanban_home / "profiles" / "worker"
    worker.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(worker))  # this process runs as the "worker" profile
    s = Settings.from_mapping({})
    assert s.shared_state_dir.resolve() == (kanban_home / "plugin-data" / "kanban-empowered").resolve()
    named = Settings.from_mapping({"coordinator_profile": "boss"})
    expected = kanban_home / "profiles" / "boss" / "plugin-data" / "kanban-empowered"
    assert named.shared_state_dir.resolve() == expected.resolve()
    explicit = Settings.from_mapping({"coordinator_profile": "boss", "shared_state_dir": str(kanban_home / "x")})
    assert explicit.shared_state_dir == kanban_home / "x"
