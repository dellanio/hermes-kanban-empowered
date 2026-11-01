"""Plugin settings.

Config lives under ``plugins.entries.kanban-empowered.settings`` and is read
through ``ctx.get_config("<dotted.key>", default)``. ``Settings.from_mapping``
accepts the same structure as a nested dict (``{"waits": {"reminders": [...]}}``),
so tests and ``from_ctx`` share one code path.

The Hermes Desktop settings form (``plugin.yaml`` ``config_schema``) reads and writes its keys as FLAT
literals (``settings.default_owner``, never ``settings.waits.default_owner``), so the form uses the
flat names in ``_FORM_ALIASES`` plus ``telegram_owner_id``. They are folded onto the nested structure
before parsing: a non-blank flat value WINS over the nested key it aliases (it is the one a person just
edited in the UI); a blank flat string ("" - the form's way of saying unset) is ignored. The nested
keys keep working unchanged for people who edit ``config.yaml`` by hand.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

_log = logging.getLogger(__name__)

DEFAULT_INFRA_PATTERNS: tuple[str, ...] = (
    "Kanban execution unknown",
    "retained by owner",
    "Gateway connection failed",
    "WorkerPersistenceError",
)

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])?$")


def parse_duration(text: Any) -> int:
    """``"30m"`` -> 1800, ``"2h"`` -> 7200, ``"7d"`` -> 604800. Bare ints are seconds."""
    if isinstance(text, bool):
        raise ValueError(f"invalid duration: {text!r}")
    if isinstance(text, (int, float)):
        if text < 0:
            raise ValueError(f"invalid duration: {text!r}")
        return int(text)
    m = _DURATION_RE.match(str(text).strip().lower())
    if not m:
        raise ValueError(f"invalid duration: {text!r}")
    return int(m.group(1)) * _UNITS[m.group(2) or "s"]


def _hermes_root() -> Path:
    """The Hermes root (fallback when the Hermes adapter cannot be imported): ``HERMES_HOME`` minus a
    ``profiles/<name>`` tail, or ``~/.hermes``."""
    env = os.environ.get("HERMES_HOME")
    if not env:
        return Path.home() / ".hermes"
    home = Path(env)
    return home.parent.parent if home.parent.name == "profiles" else home


def _profile_home(profile: str) -> Path:
    try:
        from . import hermes_api

        return hermes_api.profile_home(profile)
    except Exception:
        root = _hermes_root()
        return root if profile == "default" else root / "profiles" / profile


def default_shared_state_dir(coordinator_profile: str = "default") -> Path:
    """``plugin-data/kanban-empowered`` under the COORDINATOR profile's home, whatever profile runs.

    Every profile (coordinator, workers, the CLI) must agree on one state directory, so the default is
    derived from ``coordinator_profile`` and never from the active profile: ``<hermes root>`` for
    ``default``, ``<hermes root>/profiles/<name>`` otherwise.
    """
    return _profile_home(coordinator_profile or "default") / "plugin-data" / "kanban-empowered"


def _get(m: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = m
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return default
        cur = cur[part]
    return default if cur is None else cur


_DIGEST_ALIASES = {"hourly": 3600, "daily": 86400, "weekly": 604800}
_VALID_MODES = ("enforce", "cooperative-only")
_TRUE = {"true", "1", "yes", "y", "on"}
_FALSE = {"false", "0", "no", "n", "off"}


def _parse_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"invalid boolean for {key}: {value!r}")


def _parse_digest(value: Any) -> int:
    if isinstance(value, str) and value.strip().lower() in _DIGEST_ALIASES:
        return _DIGEST_ALIASES[value.strip().lower()]
    return parse_duration(value)


def _board_owners(m: Mapping[str, Any]) -> dict[str, str]:
    """Per-board overrides: ``boards.<board>.waits.default_owner``."""
    boards = _get(m, "boards") or {}
    out: dict[str, str] = {}
    if isinstance(boards, Mapping):
        for name, cfg in boards.items():
            owner = _get(cfg, "waits.default_owner") if isinstance(cfg, Mapping) else None
            if owner:
                out[str(name)] = str(owner)
    return out


# Flat settings-form key -> the nested key it overrides. ``telegram_owner_id`` is handled apart (it feeds
# ``owner_ids``); ``mode`` and ``language`` are already flat.
_FORM_ALIASES: dict[str, str] = {
    "default_owner": "waits.default_owner",
    "reminders": "waits.reminders",
    "escalate_after": "waits.escalate_after",
    "digest": "waits.digest",
    "escalate_to": "waits.escalate_to",
    "require_structured_ask": "waits.require_structured_ask",
    "pin_live_panel": "telegram.pin_live_panel",
    "guard_alert_after": "guards.alert_after",
    "undo_budget_count": "infra_failures.undo_budget.count",
    "undo_budget_window": "infra_failures.undo_budget.window",
}
# Every flat key the form can write (read by ``from_ctx`` next to the nested keys).
FORM_FLAT_KEYS: tuple[str, ...] = (*_FORM_ALIASES, "telegram_owner_id")


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _set_path(root: dict[str, Any], dotted: str, value: Any) -> None:
    """Set ``dotted`` in ``root``, copying each mapping on the way so the caller's dict is untouched."""
    cur = root
    *parents, leaf = dotted.split(".")
    for part in parents:
        child = cur.get(part)
        if isinstance(child, Mapping):
            child = dict(child)
        elif part == "undo_budget" and child is not None:
            child = {"count": child}  # a bare int is the budget's count
        else:
            child = {}
        cur[part] = child
        cur = child
    cur[leaf] = value


def _nested_value(m: Mapping[str, Any], nested: str) -> Any:
    """The config.yaml value a flat form key stands for (a bare-int ``undo_budget`` is its count)."""
    if nested.startswith("infra_failures.undo_budget."):
        budget = _get(m, "infra_failures.undo_budget")
        if not isinstance(budget, Mapping):
            return budget if nested.endswith(".count") else None
        return budget.get(nested.rsplit(".", 1)[1])
    return _get(m, nested)


# How to compare a flat form value with its nested twin so "30m,2h" and [30m, 2h] are not a conflict.
_NORMALIZERS: dict[str, Any] = {
    "reminders": lambda v: _durations(v), "digest": lambda v: _parse_digest(v),
    "escalate_after": parse_duration, "guard_alert_after": parse_duration,
    "undo_budget_window": parse_duration, "undo_budget_count": int,
    "pin_live_panel": lambda v: _parse_bool(v, "pin_live_panel"),
    "require_structured_ask": lambda v: _parse_bool(v, "require_structured_ask"),
}


def _same(flat: str, a: Any, b: Any) -> bool:
    norm = _NORMALIZERS.get(flat, lambda v: str(v).strip())
    try:
        return norm(a) == norm(b)
    except (ValueError, TypeError):
        return False


Conflict = tuple[str, str, str, str, str]  # (kind, form key, config.yaml key, form value, config value)


def _fold_form_keys(m: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[Conflict]]:
    """Overlay the settings form's flat keys onto a copy of the nested mapping (flat wins; blank ignored).

    Also returns the shadowing it did: every non-blank flat value that replaces a DIFFERENT, set nested value.
    """
    out: Optional[dict[str, Any]] = None
    conflicts: list[Conflict] = []
    for flat, nested in _FORM_ALIASES.items():
        value = m.get(flat)
        if _blank(value):
            continue
        shadowed = _nested_value(m, nested)
        if not _blank(shadowed) and not _same(flat, value, shadowed):
            conflicts.append(("override", flat, nested, str(value), str(shadowed)))
        if out is None:
            out = dict(m)
        _set_path(out, nested, value)
    return (m if out is None else out), conflicts


def _owner_ids(m: Mapping[str, Any], default_owner: str) -> tuple[dict[str, int], list[Conflict]]:
    """``telegram.owner_ids`` when set; otherwise the form's single ``telegram_owner_id`` as the default
    owner's id. Also reports a ``telegram_owner_id`` that an explicit ``owner_ids`` leaves without effect."""
    explicit = _get(m, "telegram.owner_ids") or {}
    single = m.get("telegram_owner_id")
    if explicit:
        ids = {str(k): int(v) for k, v in dict(explicit).items()}
        lost = not _blank(single) and default_owner not in ids
        return ids, ([("owner_id_ignored", "telegram_owner_id", "telegram.owner_ids", str(single), default_owner)]
                     if lost else [])
    if _blank(single):
        return {}, []
    return {default_owner: int(single)}, []


def _durations(value: Any) -> tuple[int, ...]:
    if isinstance(value, str):
        value = [part for part in value.split(",") if part.strip()]  # "30m,2h" from the settings form
    elif isinstance(value, (int, float)):
        value = [value]
    return tuple(parse_duration(v) for v in value)


def _strs(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    return tuple(str(v) for v in value)


@dataclass(frozen=True)
class Settings:
    mode: str = "enforce"
    coordinator_profile: str = "default"
    # None resolves to default_shared_state_dir(coordinator_profile) (see __post_init__)
    shared_state_dir: Optional[Path] = None
    owner_ids: dict[str, int] = field(default_factory=dict)
    pin_live_panel: bool = True
    default_owner: str = "sponsor"
    board_owner: dict[str, str] = field(default_factory=dict)
    reminders_s: tuple[int, ...] = (1800, 7200)
    escalate_after_s: int = 28800
    digest_every_s: int = 86400
    escalate_to: str | None = None
    human_kinds: tuple[str, ...] = ("needs_input",)
    require_structured_ask: bool = True
    guard_alert_after_s: int = 1800
    infra_undo_count: int = 2
    infra_undo_window_s: int = 86400
    infra_alert_count: int = 3
    infra_alert_window_s: int = 3600
    infra_patterns: tuple[str, ...] = DEFAULT_INFRA_PATTERNS
    reply_ttl_s: int = 604800
    language: str | None = None  # plugin messages' language ("pt", "en"...); None follows Hermes
    # Settings-form values that shadow or lose against config.yaml (see Conflict); shown by doctor.
    conflicts: tuple[Conflict, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        if self.shared_state_dir is None:
            object.__setattr__(self, "shared_state_dir", default_shared_state_dir(self.coordinator_profile))

    def owner_for(self, board: str) -> str:
        return self.board_owner.get(board) or self.default_owner

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any]) -> "Settings":
        """Build from a nested mapping shaped like the plugin's ``settings`` subtree."""
        defaults = cls()  # for non-trivial defaults
        m, conflicts = _fold_form_keys(m)

        def dur(key: str, field_name: str) -> int:
            v = _get(m, key)
            return parse_duration(v) if v is not None else getattr(defaults, field_name)

        def num(key: str, field_name: str) -> int:
            v = _get(m, key)
            return int(v) if v is not None else getattr(defaults, field_name)

        def flag(key: str, field_name: str) -> bool:
            v = _get(m, key)
            return _parse_bool(v, key) if v is not None else getattr(defaults, field_name)

        mode = str(_get(m, "mode", defaults.mode)).strip() or defaults.mode
        if mode not in _VALID_MODES:
            raise ValueError(f"invalid value for mode: {mode!r} (expected one of {_VALID_MODES})")

        # infra_failures.undo_budget: {count, window}; a bare int is the count.
        budget = _get(m, "infra_failures.undo_budget")
        undo_count = defaults.infra_undo_count
        undo_window = defaults.infra_undo_window_s
        if isinstance(budget, Mapping):
            if budget.get("count") is not None:
                undo_count = int(budget["count"])
            if budget.get("window") is not None:
                undo_window = parse_duration(budget["window"])
        elif budget is not None:
            undo_count = int(budget)

        digest = _get(m, "waits.digest")
        reminders = _get(m, "waits.reminders")
        human_kinds = _get(m, "waits.human_kinds")
        patterns = _get(m, "infra_failures.patterns")
        state_dir = _get(m, "shared_state_dir")
        escalate_to = _get(m, "waits.escalate_to")
        language = _get(m, "language")

        coordinator_profile = str(_get(m, "coordinator_profile", defaults.coordinator_profile))
        default_owner = str(_get(m, "waits.default_owner", defaults.default_owner))
        owner_ids, owner_conflicts = _owner_ids(m, default_owner)
        conflicts += owner_conflicts
        for kind, flat, nested, form_value, config_value in conflicts:
            if kind == "override":
                _log.warning("kanban-empowered: form value %s overrides config.yaml %s %s (settings form key %s)",
                             form_value, nested, config_value, flat)
            else:
                _log.warning("kanban-empowered: telegram_owner_id %s is ignored: telegram.owner_ids is set and "
                             "has no id for %s", form_value, config_value)
        return cls(
            mode=mode,
            coordinator_profile=coordinator_profile,
            shared_state_dir=Path(state_dir) if state_dir else default_shared_state_dir(coordinator_profile),
            owner_ids=owner_ids,
            pin_live_panel=flag("telegram.pin_live_panel", "pin_live_panel"),
            default_owner=default_owner,
            board_owner=_board_owners(m),
            reminders_s=_durations(reminders) if reminders is not None else defaults.reminders_s,
            escalate_after_s=dur("waits.escalate_after", "escalate_after_s"),
            digest_every_s=_parse_digest(digest) if digest is not None else defaults.digest_every_s,
            escalate_to=str(escalate_to) if escalate_to else None,
            human_kinds=_strs(human_kinds) if human_kinds is not None else defaults.human_kinds,
            require_structured_ask=flag("waits.require_structured_ask", "require_structured_ask"),
            guard_alert_after_s=dur("guards.alert_after", "guard_alert_after_s"),
            infra_undo_count=undo_count,
            infra_undo_window_s=undo_window,
            infra_alert_count=num("infra_failures.alert_count", "infra_alert_count"),
            infra_alert_window_s=dur("infra_failures.alert_window", "infra_alert_window_s"),
            infra_patterns=_strs(patterns) if patterns is not None else defaults.infra_patterns,
            reply_ttl_s=dur("telegram.reply_ttl", "reply_ttl_s"),
            language=str(language).strip().lower() or None if language else None,
            conflicts=tuple(conflicts),
        )

    @classmethod
    def from_ctx(cls, ctx: Any) -> "Settings":
        """Build from ``ctx.get_config(key, default)`` using dotted keys."""
        keys = (
            "mode",
            "language",
            "coordinator_profile",
            "shared_state_dir",
            "telegram.owner_ids",
            "telegram.pin_live_panel",
            "telegram.reply_ttl",
            "boards",
            "waits.default_owner",
            "waits.reminders",
            "waits.escalate_after",
            "waits.digest",
            "waits.escalate_to",
            "waits.human_kinds",
            "waits.require_structured_ask",
            "guards.alert_after",
            "infra_failures.undo_budget",
            "infra_failures.alert_count",
            "infra_failures.alert_window",
            "infra_failures.patterns",
            *FORM_FLAT_KEYS,
        )
        nested: dict[str, Any] = {}
        for key in keys:
            try:
                value = ctx.get_config(key, None)
            except Exception:
                value = None
            if value is None:
                continue
            cur = nested
            *parents, leaf = key.split(".")
            for p in parents:
                cur = cur.setdefault(p, {})
            cur[leaf] = value
        return cls.from_mapping(nested)
