"""Data of the Hermes Desktop "Empowered" dashboard (served by ``dashboard/plugin_api.py``).

Everything here is built from the same pieces as Telegram, so a card reads the same on both surfaces:
``conditions.fleet_status`` (running cards, human waits), ``waits.human_block`` (THE definition of a
human wait), the reminder renderer (``messages.render_wait_item``, ``short_action``, ``context_line``,
``reply_instruction``, ``strip_ids`` / ``agent_text`` and the role replacement), ``InfraRecovery``
(cards whose infrastructure-undo budget is used up) and, for answers, ``inbound.apply_answer``: the
classification and resolution a Telegram reply goes through (same live version check, same lock).

Reads only, except :func:`answer`, which writes through the Hermes domain functions wrapped in
``hermes_api`` (unblock, comment) and the plugin Store (audit action). No Hermes module is imported
here; ``hermes_api`` is the adapter.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from . import conditions, hermes_api, messages, telegram
from .asks import parse_ask
from .inbound import apply_answer
from .infra import InfraRecovery
from .messages import (
    _clip_to,
    agent_text,
    board_title,
    natural_age,
    plural,
    replace_roles,
    reply_hint,
    short_action,
    strip_ids,
    t,
)
from .settings import Settings
from .store import Store
from .waits import Due, display_name, held_behind, human_block

log = logging.getLogger(__name__)

PLUGIN_ID = "kanban-empowered"
TIMELINE_MAX = 20
COMMENTS_MAX = 5
TITLE_MAX = 120
WHY_MAX = 200
COMMENT_BODY_MAX = 600
ACTIVITY_MAX = 120
INFRA_WINDOW_S = 3600
_ACTION_KINDS = ("human_done", "guard_release", "infra_undo")
_SENT_KINDS = ("waits", "panel")


# -- settings ---------------------------------------------------------------------------------
_cache: dict[str, Any] = {}


def load_settings(api: Any = hermes_api) -> Settings:
    """The plugin settings of the active profile (cached until the config or the Hermes home changes).

    An invalid config falls back to the defaults (the dashboard stays usable; ``doctor`` names the
    error). The language override and the role names are applied to the message layer, exactly as
    the agent half's ``register`` does.
    """
    raw = api.plugin_settings(PLUGIN_ID)
    try:
        home = str(api.hermes_home())
    except Exception:
        home = os.environ.get("HERMES_HOME", "")
    key = json.dumps([home, raw], sort_keys=True, default=str)
    cached = _cache.get("settings")
    if cached is not None and cached[0] == key:
        if messages._LANG != cached[2]:
            messages._LANG = cached[2]  # someone else (a test, a reload) moved it: put ours back
        return cached[1]
    try:
        settings = Settings.from_mapping(raw)
    except Exception as exc:
        log.warning("kanban-empowered: invalid plugin config (%s); the dashboard uses the defaults", exc)
        settings = Settings.from_mapping({})
    messages.set_language(settings.language)
    roles = set(settings.owner_ids)
    if settings.escalate_to:
        roles.add(settings.escalate_to)
    messages.set_roles(roles)
    _cache["settings"] = (key, settings, messages._LANG)
    return settings


def settings_view(settings: Settings) -> dict:
    """``GET /settings``: the Desktop switches and the language the plugin speaks."""
    return {
        "show_sidebar_menu": bool(settings.show_sidebar_menu),
        "show_kanban_button": bool(settings.show_kanban_button),
        "language": messages.active_language(),
    }


def ui_strings() -> dict:
    """``GET /i18n``: every ``ui.*`` catalog key in the plugin language (English for a missing translation).

    Placeholders (``{age}``, ``{n}``...) are left for the Desktop half to fill.
    """
    return {
        "language": messages.active_language(),
        "strings": {k: t(k) for k in messages.EN if k.startswith("ui.")},
    }


# -- helpers ----------------------------------------------------------------------------------
def _age(now: float, ts: Any) -> int:
    try:
        return max(0, int(now - float(ts))) if ts else 0
    except (TypeError, ValueError):
        return 0


def _clean(text: Any, limit: int) -> str:
    """Agent- or user-written text as the dashboard may show it: one line, role names replaced, no ids."""
    return _clip_to(strip_ids(replace_roles(text or "")), limit)


def _stage_text(stage: Optional[str]) -> str:
    if not stage:
        return t("ui.stage.none")
    if stage.startswith("reminder") and stage[len("reminder"):].isdigit():
        return t("ui.stage.reminder", n=int(stage[len("reminder"):]))
    if stage in ("escalation", "digest"):
        return t(f"ui.stage.{stage}")
    return stage


def _boards(api: Any) -> list[str]:
    return list(api.boards())


def _check_board(api: Any, board: Optional[str]) -> tuple[list[str], Optional[str]]:
    """``(every board, the filter or None for all)``; LookupError for an unknown board."""
    boards = _boards(api)
    if board in (None, "", "all"):
        return boards, None
    if board not in boards:
        raise LookupError(board)
    return boards, board


# -- items ------------------------------------------------------------------------------------
def _wait_entry(api: Any, conn: Any, store: Store, settings: Settings, now: float, board: str, task: Any,
                block: Any) -> tuple[dict, Due]:
    """The "needs you" entry of a human wait, and the reminder ``Due`` it renders as on Telegram."""
    ask = parse_ask(block.reason)
    row = store.latest_wait(board, task.id)
    if row is not None and int(row["version"]) != block.version:
        row = None  # a row of an older block (the tracker has not synced yet)
    age = _age(now, (row or {}).get("started_at") or block.started_at)
    held = held_behind(api, conn, task.id)
    title = str(getattr(task, "title", "") or "")
    body = str(getattr(task, "body", "") or "")
    name = display_name(api, board)
    digest = conditions._digest_period(conn, task, settings)
    next_in = conditions._next_reminder_in(age, settings, row, now, digest)
    stage = (row or {}).get("stage")
    action = short_action(ask, block.reason, title)
    entry = {
        "kind": "human_wait",
        "board": board,
        "board_name": name or board,
        "board_title": board_title(board, name, ask.subject if ask is not None else ""),
        "task_id": task.id,
        "version": int(block.version),
        "title": _clean(title, TITLE_MAX),
        "age_s": age,
        "age_text": natural_age(age),
        "held_behind_count": len(held),
        "need": action,
        "why": _clip_to(agent_text(ask.why), WHY_MAX) if ask is not None else "",
        "next_step": _clip_to(agent_text(ask.next_step), WHY_MAX) if ask is not None and ask.next_step else "",
        "context": messages.context_line(ask, body, action),
        "reply_line": messages.reply_instruction(ask),
        "options": [{"letter": k, "label": _clean(v, 120)} for k, v in (ask.options if ask is not None else ())],
        "reply_hint": reply_hint(ask),
        "stage": stage,
        "stage_text": _stage_text(stage),
        "reminder_sent": bool(stage),
        "next_reminder_in_s": int(next_in),
        "next_reminder_text": natural_age(next_in) if next_in > 0 else "",
        "_held": list(held),  # internal: the impacted-cards metric (dropped before the JSON goes out)
    }
    due = Due(
        board=board, task_id=task.id, version=int(block.version),
        owner=(row or {}).get("owner") or settings.owner_for(board), stage=stage or "reminder1", age_s=age,
        ask=ask, held_behind=held, reason=block.reason, board_name=name, title=title, body=body,
    )
    return entry, due


def _infra_entry(api: Any, conn: Any, now: float, row: dict) -> dict:
    board = row["board"]
    name = display_name(api, board)
    age = _age(now, row.get("since"))
    try:
        held = len(held_behind(api, conn, row["task_id"]))
    except Exception:
        held = 0
    return {
        "kind": "infra_exhausted",
        "board": board,
        "board_name": name or board,
        "board_title": board_title(board, name),
        "task_id": row["task_id"],
        "title": _clean(row.get("title"), TITLE_MAX),
        "age_s": age,
        "age_text": natural_age(age),
        "held_behind_count": held,
        "attempts": int(row.get("attempts", 0)),
        "attempts_text": plural("ui.infra.attempts", int(row.get("attempts", 0))),
        "failures": int(row.get("failures", 0)),
        "patterns": list(row.get("patterns") or []),
        "last_error_class": row.get("last_error_class") or "infra",
        "need": t("ui.needs.exhausted_text"),
    }


def _sort_key(entry: dict) -> tuple:
    """Biggest impact first (cards held behind), then the oldest."""
    return (-int(entry.get("held_behind_count", 0)), -int(entry.get("age_s", 0)), entry["board"], entry["task_id"])


def _needs_you(api: Any, store: Store, settings: Settings, now: float, waits: list, exhausted: list) -> list:
    by_board: dict[str, list] = {}
    for w in waits:
        by_board.setdefault(w["board"], []).append(("wait", w))
    for row in exhausted:
        by_board.setdefault(row["board"], []).append(("infra", row))
    out: list[dict] = []
    for board, group in by_board.items():
        try:
            with api.connect(board) as conn:
                for kind, data in group:
                    try:
                        if kind == "infra":
                            out.append(_infra_entry(api, conn, now, data))
                            continue
                        task = api.task(conn, data["task_id"])
                        block = human_block(task, api.events(conn, data["task_id"]), settings)
                        if block is not None:
                            out.append(_wait_entry(api, conn, store, settings, now, board, task, block)[0])
                    except Exception:
                        log.exception("kanban-empowered: dashboard could not read %s/%s", board, data["task_id"])
        except Exception:
            log.exception("kanban-empowered: dashboard could not read board %s", board)
    return sorted(out, key=_sort_key)


def _last_activity(api: Any, conn: Any, task: Any, now: float) -> dict:
    """The newest comment on the card (else "started"), with the heartbeat age apart."""
    text, ts = t("ui.activity.started"), getattr(task, "started_at", None) or getattr(task, "created_at", None)
    try:
        comments = list(api.comments(conn, task.id))
    except Exception:
        comments = []
    if comments:
        newest = max(comments, key=lambda c: (c.created_at or 0, c.id))
        said = _clip_to(agent_text(newest.body), ACTIVITY_MAX)
        if said:
            text, ts = said, newest.created_at
    hb = getattr(task, "last_heartbeat_at", None)
    return {"text": text, "age_s": _age(now, ts), "heartbeat_age_s": _age(now, hb) if hb else None}


def _running(api: Any, now: float, rows: list) -> list:
    by_board: dict[str, list] = {}
    for r in rows:
        by_board.setdefault(r["board"], []).append(r)
    out: list[dict] = []
    for board, group in by_board.items():
        name = display_name(api, board)
        try:
            with api.connect(board) as conn:
                for r in group:
                    task = api.task(conn, r["task_id"])
                    if task is None:
                        continue
                    out.append({
                        "board": board,
                        "board_name": name or board,
                        "task_id": r["task_id"],
                        "title": _clean(r.get("title"), TITLE_MAX),
                        "profile": r.get("assignee") or "",
                        "since_s": int(r.get("age_s", 0)),
                        "last_activity": _last_activity(api, conn, task, now),
                    })
        except Exception:
            log.exception("kanban-empowered: dashboard could not read running cards of %s", board)
    return sorted(out, key=lambda r: -r["since_s"])


# -- timeline ---------------------------------------------------------------------------------
def _source(detail: Any) -> str:
    head = str(detail or "").split(":", 1)[0].strip().lower()
    return head if head in ("telegram", "desktop", "cli") else "other"


def _titles(api: Any, wanted: set) -> dict:
    """``{(board, task_id): cleaned title}`` for the cards a timeline names (unreadable ones are left out)."""
    by_board: dict[str, set] = {}
    for board, tid in wanted:
        by_board.setdefault(board, set()).add(tid)
    out: dict = {}
    for board, ids in by_board.items():
        try:
            with api.connect(board) as conn:
                for tid in ids:
                    task = api.task(conn, tid)
                    if task is not None:
                        out[(board, tid)] = _clean(task.title, TITLE_MAX)
        except Exception:
            continue
    return out


def _timeline(api: Any, store: Store, boards: list, only: Optional[str]) -> list:
    entries: list[dict] = []
    for a in store.recent_actions(_ACTION_KINDS, limit=TIMELINE_MAX * 10):
        if only is not None and a["board"] != only:
            continue
        kind = a["kind"]
        if kind == "human_done":
            source = _source(a["detail"])
            detail, text = source, t("ui.timeline.human_done", source=t(f"ui.timeline.source.{source}"))
        elif kind == "guard_release":
            detail = str(a["detail"] or "").rsplit("->", 1)[-1].strip() if "->" in str(a["detail"] or "") else ""
            text = t("ui.timeline.guard_release")
        else:
            detail = str(a["detail"] or "").split(":", 1)[-1].strip()
            text = t("ui.timeline.infra_undo")
        entries.append({"ts": int(a["ts"]), "kind": kind, "board": a["board"], "task_id": a["task_id"],
                        "title": "", "detail": detail, "text": text})
    for m in store.recent_sent_messages(_SENT_KINDS, limit=TIMELINE_MAX * 10):
        items = [i for i in m["items"] if isinstance(i, dict) and "board" in i and "task_id" in i]
        if m["kind"] == "panel":
            if only is not None:
                continue  # the panel spans every board
            entries.append({"ts": int(m["created_at"]), "kind": "panel_updated", "board": "", "task_id": "",
                            "title": "", "detail": str(len(items)), "text": t("ui.timeline.panel_updated")})
            continue
        if only is not None:
            items = [i for i in items if i["board"] == only]
            if not items:
                continue
        first = items[0] if items else {"board": "", "task_id": ""}
        entries.append({"ts": int(m["created_at"]), "kind": "reminder_sent", "board": str(first["board"]),
                        "task_id": str(first["task_id"]), "title": "", "detail": str(len(items)),
                        "text": plural("ui.timeline.reminder_sent", len(items))})
    entries.sort(key=lambda e: -e["ts"])
    entries = entries[:TIMELINE_MAX]
    titles = _titles(api, {(e["board"], e["task_id"]) for e in entries if e["board"] and e["task_id"]})
    for e in entries:
        e["title"] = titles.get((e["board"], e["task_id"]), "")
        e.pop("task_id")
    return entries


# -- infrastructure -----------------------------------------------------------------------------
def _infra(store: Store, settings: Settings, now: float, exhausted: list) -> dict:
    recent = store.count_actions_since("infra_exit", int(now - INFRA_WINDOW_S), "infra:")
    in_window = store.count_actions_since("infra_exit", int(now - settings.infra_alert_window_s), "infra:")
    systemic = settings.infra_alert_count > 0 and in_window >= settings.infra_alert_count
    path = telegram.read_path_file(settings.shared_state_dir / "telegram_path")
    kind = telegram.classify_path(path) if path else None
    token = bool((os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip())  # this process only; never returned
    return {
        "recent_infra_exits_1h": int(recent),
        "systemic_alert_active": bool(systemic),
        "systemic_window_exits": int(in_window),
        "alert_count": int(settings.infra_alert_count),
        "budget_exhausted": [
            {"board": r["board"], "task_id": r["task_id"], "title": _clean(r.get("title"), TITLE_MAX),
             "attempts": int(r.get("attempts", 0)), "last_error_class": r.get("last_error_class") or "infra",
             "patterns": list(r.get("patterns") or [])}
            for r in exhausted
        ],
        "telegram_path": kind,
        "telegram_token": token,
        "telegram_ok": bool(token and kind in ("host", "ip")),
    }


# -- public API -------------------------------------------------------------------------------
def overview(settings: Settings, store: Store, now: float, board: Optional[str] = None, api: Any = hermes_api) -> dict:
    """``GET /overview``: metrics, needs-you items, running cards, the coordinator timeline, infrastructure.

    ``board`` filters every list (``None`` / ``"all"``: every board); LookupError for an unknown board.
    """
    boards, only = _check_board(api, board)
    scope = [only] if only is not None else boards
    fs = conditions.fleet_status(store, now, settings=settings, api=api)
    waits = [w for w in fs["human_waits"] if w["board"] in scope]
    running_rows = [r for r in fs["running"] if r["board"] in scope]
    exhausted = InfraRecovery(settings, store, api).exhausted(now, scope)
    needs = _needs_you(api, store, settings, now, waits, exhausted)
    infra = _infra(store, settings, now, exhausted)
    impacted = {(e["board"], tid) for e in needs for tid in e.pop("_held", ())}
    running = _running(api, now, running_rows)
    return {
        "generated_at": int(now),
        "board": only or "all",
        "metrics": {
            "needs_you": sum(1 for e in needs if e["kind"] == "human_wait"),
            "impacted_cards": len(impacted),
            "workers_running": len(running),
            "incidents": len(exhausted) + (1 if infra["systemic_alert_active"] else 0),
        },
        "needs_you": needs,
        "running": running,
        "timeline": _timeline(api, store, boards, only),
        "infra": infra,
        "boards": boards,
        "board_names": {b: display_name(api, b) or b for b in boards},
    }


def _comments(api: Any, conn: Any, task_id: str, now: float) -> list:
    rows = sorted(api.comments(conn, task_id), key=lambda c: (c.created_at or 0, c.id))[-COMMENTS_MAX:]
    return [
        {"author": _clean(c.author, 60), "body": _clean(c.body, COMMENT_BODY_MAX), "ts": int(c.created_at or 0),
         "age_s": _age(now, c.created_at)}
        for c in rows
    ]


def item(settings: Settings, store: Store, now: float, board: str, task_id: str, api: Any = hermes_api) -> dict:
    """``GET /item``: the needs-you entry plus the reminder text as Telegram renders it, the ``explain``
    conditions and the last comments. LookupError when the card is not waiting on anybody (any more)."""
    _check_board(api, board)
    if board in (None, "", "all"):
        raise LookupError(board)
    with api.connect(board) as conn:
        task = api.task(conn, task_id)
        if task is None:
            raise LookupError(task_id)
        block = human_block(task, api.events(conn, task_id), settings)
        if block is not None:
            entry, due = _wait_entry(api, conn, store, settings, now, board, task, block)
            entry.pop("_held", None)
            entry["reminder_text"] = messages.render_wait_item(due)
        else:
            rows = [r for r in InfraRecovery(settings, store, api).exhausted(now, [board]) if r["task_id"] == task_id]
            if not rows:
                raise LookupError(task_id)
            entry = _infra_entry(api, conn, now, rows[0])
            alert = {"kind": "budget", "board": board, "board_name": display_name(api, board), "task_id": task_id,
                     "title": task.title, "count": rows[0]["attempts"], "window_s": settings.infra_undo_window_s}
            entry["reminder_text"] = messages.render_infra_alert(alert).split("\n", 1)[-1]
        cc = conditions.explain(conn, board, task_id, store, now, settings)
        entry["conditions"] = [{"name": c.name, "ok": c.ok, "warn": c.warn, "text": c.text} for c in cc.conditions]
        entry["comments"] = _comments(api, conn, task_id, now)
    return entry


def answer(
    settings: Settings,
    store: Store,
    now: float,
    board: str,
    task_id: str,
    version: int,
    text: Optional[str] = None,
    option: Optional[str] = None,
    api: Any = hermes_api,
) -> dict:
    """``POST /answer``: the owner's answer typed in the Desktop drawer, classified and applied exactly like
    a Telegram reply (option letter / confirmation / negation = note only / any other text = the answer).

    ``option`` wins over ``text``. The comment says "via Kanban-Empowered (desktop)" and the audit action
    is ``human_done`` (or ``human_note``) with detail ``desktop``: Desktop sends no identity, and no
    body field is trusted as one.
    """
    def not_found() -> dict:
        return {"result": "not_found", "message": t("ui.answer.not_found"), "overview_item_removed": False,
                "held_behind_count": 0}

    if board not in _boards(api):
        return not_found()
    with api.connect(board) as conn:
        if api.task(conn, task_id) is None:
            return not_found()
    body = (option if option else text or "").strip()
    outcome = apply_answer(
        api, store, board, task_id, int(version), body, now,
        detail="desktop",
        resolved_comment=lambda said: t("inbound.comment.desktop", text=said),
        note_comment=lambda said: t("inbound.comment.note_desktop", text=said),
    )
    return {
        "result": outcome.result,
        "message": outcome.message,
        "overview_item_removed": outcome.result == "resolved",
        "held_behind_count": len(outcome.held),
    }
