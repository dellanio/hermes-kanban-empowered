"""Hermes Desktop settings form: ``plugin.yaml`` ``config_schema`` and its mapping into ``Settings``.

The Desktop form reads and writes the schema keys as FLAT literals under
``plugins.entries.kanban-empowered.settings`` (see ``hermes_cli/plugins_settings.py``), so the schema
declares flat keys and ``Settings`` maps them onto the nested structure (flat wins over nested).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_ID = "kanban-empowered"

FORM_FLAT_KEYS = {
    "language", "mode", "telegram_owner_id", "default_owner", "reminders", "escalate_after", "digest",
    "blocking_digest_every", "escalate_to", "require_structured_ask", "pin_live_panel", "guard_alert_after",
    "undo_budget_count", "undo_budget_window",
}


def _manifest() -> dict:
    import hermes_yaml as yaml

    return yaml.safe_load((REPO / "plugin.yaml").read_text(encoding="utf-8-sig"))


def _schema() -> dict:
    return _manifest()["config_schema"]


# ---- plugin.yaml ------------------------------------------------------------------------------


def test_schema_declares_exactly_the_form_keys_with_flat_names():
    schema = _schema()
    assert set(schema) == FORM_FLAT_KEYS
    assert not any("." in k for k in schema)  # the Desktop form reads literal flat keys
    for key, spec in schema.items():
        assert spec.get("description"), key
        assert spec.get("type") in {"str", "int", "bool"}, key


def test_schema_mode_is_a_choice_field():
    spec = _schema()["mode"]
    assert spec["type"] == "str" and spec["choices"] == ["enforce", "cooperative-only"]
    assert spec["default"] == "enforce"


def test_schema_defaults_equal_settings_defaults(monkeypatch, tmp_path):
    """Applying every schema default must not change what the plugin does."""
    from ke.settings import Settings

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schema = _schema()
    defaults = {k: v["default"] for k, v in schema.items() if "default" in v}
    assert "telegram_owner_id" not in defaults  # no default: an id is personal
    assert Settings.from_mapping(defaults) == Settings.from_mapping({})


def test_valid_config_has_no_schema_warnings():
    from hermes_cli.plugins_manifest import validate_config_schema

    settings = {
        "language": "pt", "mode": "cooperative-only", "telegram_owner_id": 123456, "default_owner": "ana",
        "reminders": "30m,2h", "escalate_after": "8h", "digest": "daily", "blocking_digest_every": "6h", "escalate_to": "bob",
        "require_structured_ask": True, "pin_live_panel": True, "guard_alert_after": "30m",
        "undo_budget_count": 2, "undo_budget_window": "24h",
    }
    assert set(settings) == FORM_FLAT_KEYS
    assert validate_config_schema(PLUGIN_ID, _schema(), settings) == []
    defaults = {k: v["default"] for k, v in _schema().items() if "default" in v}
    assert validate_config_schema(PLUGIN_ID, _schema(), defaults) == []


def test_manifest_parses_with_no_warnings(caplog):
    from hermes_cli.plugins_manifest import parse_manifest_file

    with caplog.at_level(logging.DEBUG, logger="hermes_cli.plugins"):
        m = parse_manifest_file(REPO / "plugin.yaml", REPO, "user", "")
    assert m is not None
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert not [r for r in caplog.records if "unknown manifest field" in r.getMessage()]
    assert m.manifest_version == 2 and m.version == "0.1.0" and m.license == "MIT"
    assert m.homepage == "https://github.com/dellanio/hermes-kanban-empowered"
    assert m.author == "dellanio" and set(m.config_schema) == FORM_FLAT_KEYS
    assert {"kanban", "telegram"} <= set(m.tags)
    assert m.description.strip() and "\n" not in m.description.strip()


def test_manifest_requires_hermes_parses_and_is_satisfied():
    from hermes_cli.plugin_validate import _requires_hermes_spec_valid
    from hermes_cli.plugins_manifest import version_satisfies

    spec = _manifest()["requires_hermes"]
    assert _requires_hermes_spec_valid(spec) and version_satisfies(spec, "0.22.5")


def test_manifest_label_and_label_per_field_for_the_form():
    assert _manifest()["label"] == "Kanban Empowered"
    for key, spec in _schema().items():
        assert re.match(r"^[A-Z]", spec.get("label", "")), key  # Desktop shows label, else a humanised key


# ---- the real Desktop read/write path ---------------------------------------------------------


def _read_ctx(plugin_id: str):
    """A ``ctx`` whose ``get_config`` is Hermes' own implementation (reads the live config.yaml)."""
    from hermes_cli.plugins import PluginContext
    from hermes_cli.plugins_state import _plugin_relative_segments

    fake = SimpleNamespace(plugin_id=plugin_id, _segments=_plugin_relative_segments)
    return SimpleNamespace(get_config=lambda key, default=None: PluginContext.get_config(fake, key, default))


def test_desktop_save_then_plugin_read_roundtrip(kanban_home):
    """What the Desktop form writes (``save_plugin_settings``) is what ``Settings.from_ctx`` reads."""
    from hermes_cli.plugins_settings import plugin_settings_fields, save_plugin_settings
    from ke.settings import Settings

    fields = {f["key"]: f for f in plugin_settings_fields(PLUGIN_ID, REPO)}
    assert set(fields) == FORM_FLAT_KEYS
    assert fields["mode"]["type"] == "enum" and fields["telegram_owner_id"]["type"] == "number"
    assert fields["reminders"]["value"] == "30m,2h"  # schema default shown before any save

    written = save_plugin_settings(PLUGIN_ID, REPO, {
        "default_owner": "ana", "reminders": "10m,1h", "telegram_owner_id": 424242, "mode": "cooperative-only",
        "undo_budget_count": 4, "undo_budget_window": "12h", "pin_live_panel": False,
        "language": "en", "digest": "hourly", "escalate_to": "bob",
    })
    assert "telegram_owner_id" in written

    refreshed = {f["key"]: f for f in plugin_settings_fields(PLUGIN_ID, REPO)}
    assert refreshed["default_owner"]["value"] == "ana" and refreshed["telegram_owner_id"]["value"] == 424242

    s = Settings.from_ctx(_read_ctx(PLUGIN_ID))
    assert s.default_owner == "ana" and s.reminders_s == (600, 3600) and s.owner_ids == {"ana": 424242}
    assert s.mode == "cooperative-only" and s.infra_undo_count == 4 and s.infra_undo_window_s == 43200
    assert s.pin_live_panel is False and s.language == "en" and s.digest_every_s == 3600 and s.escalate_to == "bob"


def test_catalog_entry_example_parses_with_hermes_catalog_rules(caplog):
    import hermes_yaml as yaml
    from hermes_cli.plugin_catalog import entry_from_mapping

    data = yaml.safe_load((REPO / "docs" / "catalog-entry.example.yaml").read_text(encoding="utf-8"))
    with caplog.at_level(logging.WARNING):
        entry = entry_from_mapping(data, "kanban-empowered")
    assert entry is not None and not caplog.records
    assert entry.image.endswith("/docs/assets/banner.jpg") and entry.tier == "community"
    assert entry.requires_hermes == _manifest()["requires_hermes"] and entry.version == _manifest()["version"]
    assert entry.capabilities.provides_hooks == _manifest()["provides_hooks"]
    assert (REPO / "docs" / "assets" / "banner.jpg").is_file()
