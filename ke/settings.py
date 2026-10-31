"""Plugin settings.

Config lives under ``plugins.entries.kanban-empowered.settings`` and is read
through ``ctx.get_config("<dotted.key>", default)``. ``Settings.from_mapping``
accepts the same structure as a nested dict (``{"waits": {"reminders": [...]}}``),
so tests and ``from_ctx`` share one code path.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

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


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME")
        return Path(env) if env else Path.home() / ".hermes"


def default_shared_state_dir() -> Path:
    return _hermes_home() / "plugin-data" / "kanban-empowered"


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
_FALSE = {"false", "0", "no", "n", "off", ""}


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


def _durations(value: Any) -> tuple[int, ...]:
    if isinstance(value, (str, int, float)):
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
    shared_state_dir: Path = field(default_factory=default_shared_state_dir)
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

    def owner_for(self, board: str) -> str:
        return self.board_owner.get(board) or self.default_owner

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any]) -> "Settings":
        """Build from a nested mapping shaped like the plugin's ``settings`` subtree."""
        defaults = cls()  # for non-trivial defaults

        def dur(key: str, field_name: str) -> int:
            v = _get(m, key)
            return parse_duration(v) if v is not None else getattr(defaults, field_name)

        def num(key: str, field_name: str) -> int:
            v = _get(m, key)
            return int(v) if v is not None else getattr(defaults, field_name)

        def flag(key: str, field_name: str) -> bool:
            v = _get(m, key)
            return _parse_bool(v, key) if v is not None else getattr(defaults, field_name)

        mode = str(_get(m, "mode", defaults.mode))
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
        owner_ids = _get(m, "telegram.owner_ids") or {}
        escalate_to = _get(m, "waits.escalate_to")

        return cls(
            mode=mode,
            coordinator_profile=str(_get(m, "coordinator_profile", defaults.coordinator_profile)),
            shared_state_dir=Path(state_dir) if state_dir else default_shared_state_dir(),
            owner_ids={str(k): int(v) for k, v in dict(owner_ids).items()},
            pin_live_panel=flag("telegram.pin_live_panel", "pin_live_panel"),
            default_owner=str(_get(m, "waits.default_owner", defaults.default_owner)),
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
        )

    @classmethod
    def from_ctx(cls, ctx: Any) -> "Settings":
        """Build from ``ctx.get_config(key, default)`` using dotted keys."""
        keys = (
            "mode",
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
