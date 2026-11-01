"""The live panel: one snapshot of the fleet, rendered for the pinned Telegram message and the terminal.

``build_snapshot`` reads the boards (through the Hermes adapter) and the plugin state once;
``render_telegram`` / ``render_terminal`` turn the :class:`Snapshot` into text. The coordinator
keeps ONE pinned Telegram message up to date by editing it in place (see
``Coordinator.update_panel``) and ``hermes kanban-empowered status --watch`` redraws the terminal
view every few seconds.

Replies to the panel. The pending items are numbered (①, ②...), and ``"1 — done"`` answered to
the panel resolves item 1 exactly like a reply to a reminder (same authentication, version, TTL
and classification rules, see ``inbound``). The Telegram panel uses the reminders' vocabulary and
shows no card ids; the terminal view keeps them (it is the CLI). Because the message is edited in place its numbering changes over time,
so the panel is recorded as ``sent_messages(kind="panel")`` whose items are the CURRENT numbered
list ``[{board, task_id, version}]``, and the last two lists (plus the last one known to have
been applied) are kept with the time each became current (``kv panel_hist:<chat>:<message>``). A reply is resolved against the list that was
current at the reply's own date, and when the list the user may have been looking at differs
from the current one at the referenced position, the reply is refused ("the panel changed;
reply again") instead of guessing.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import conditions, hermes_api
from .asks import parse_ask
from .messages import (
    TELEGRAM_LIMIT,
    _clip_to,
    _guard_reason_text,
    _prefix,
    _tg_len,
    board_title,
    circled,
    human_age,
    natural_age,
    plural,
    reply_hint,
    short_action,
    t,
)
from .settings import Settings
from .store import Store
from .waits import current_block, display_name, held_behind

MAX_ITEMS = 10  # "Needs you" items shown (more are summarized as "and N more")
NEED_MAX = 120  # characters of the request shown per item
# A reply sent this soon after an edit may have been written looking at the previous list.
SEE_GRACE_S = 30
HISTORY_LEN = 2  # item lists kept per panel message


@dataclass(frozen=True)
class Snapshot:
    ts: float
    running: list = field(default_factory=list)
    held: list = field(default_factory=list)
    human_waits: list = field(default_factory=list)
    resolved_today: int = 0
    last_resolved: Optional[dict] = None
    long_guards: int = 0


# -- building ----------------------------------------------------------------------
def _enrich_waits(store: Store, api: Any, waits: list[dict]) -> list[dict]:
    """Add ``version`` (the live block version), ``need`` and ``held`` (cards behind) to each wait."""
    out: list[dict] = []
    by_board: dict[str, list[dict]] = {}
    for w in waits:
        by_board.setdefault(w["board"], []).append(w)
    for board, group in by_board.items():
        try:
            with api.connect(board) as conn:
                for w in group:
                    out.append(_enrich_one(api, conn, w))
        except Exception:
            done = {w["task_id"] for w in out if w["board"] == board}
            for w in group:
                if w["task_id"] not in done:
                    row = store.latest_wait(board, w["task_id"])
                    out.append({
                        **w, "version": int(row["version"]) if row else 0, "need": "", "held": 0,
                        "hint": t("waits.default_word"), "display": board_title(board),
                    })
    return out


def _enrich_one(api: Any, conn: Any, w: dict) -> dict:
    block = current_block(api.task(conn, w["task_id"]), api.events(conn, w["task_id"]))
    reason = block.reason if block is not None else ""
    ask = parse_ask(reason)
    return {
        **w,
        "version": block.version if block is not None else 0,
        "need": _clip_to(short_action(ask, reason) if (ask is not None or reason.strip()) else "", NEED_MAX),
        "held": len(held_behind(api, conn, w["task_id"])),
        "hint": reply_hint(ask),
        "display": board_title(w["board"], display_name(api, w["board"]), ask.subject if ask is not None else ""),
    }


def build_snapshot(store: Store, api: Any, now: float, settings: Optional[Settings] = None) -> Snapshot:
    """The fleet right now. ``held`` comes only from the tracked respawn guards (``guard_alerts``).

    ``resolved_today`` counts the ``human_done``, ``guard_release`` and ``infra_undo`` actions
    since local midnight; ``long_guards`` the held cards past ``guard_alert_after_s``.
    """
    settings = settings if settings is not None else Settings.from_mapping({})
    fs = conditions.fleet_status(store, now, settings=settings, api=api)
    held = [{**row, "age_s": max(0, int(now - row["first_seen"]))} for row in store.guard_alerts()]
    resolved = [
        a for a in store.actions_since(conditions._local_midnight(now)) if a["kind"] in conditions._RESOLVED_ACTIONS
    ]
    last = None
    if resolved:
        a = max(resolved, key=lambda r: (r["ts"], r["id"]))
        last = {
            "board": a["board"], "task_id": a["task_id"], "kind": a["kind"], "ts": a["ts"],
            "display": board_title(a["board"], display_name(api, a["board"])),
        }
    return Snapshot(
        ts=now,
        running=fs["running"],
        held=held,
        human_waits=_enrich_waits(store, api, fs["human_waits"]),
        resolved_today=len(resolved),
        last_resolved=last,
        long_guards=sum(1 for h in held if h["age_s"] >= settings.guard_alert_after_s),
    )


# -- rendering ---------------------------------------------------------------------
def _ordered(s: Snapshot) -> list[dict]:
    """Waits in display order: oldest first, ties by board and card."""
    return sorted(s.human_waits, key=lambda w: (-int(w.get("age_s", 0)), w["board"], w["task_id"]))


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _head(s: Snapshot) -> list[str]:
    return [
        t("panel.title", time=_clock(s.ts)),
        t("panel.counts", running=len(s.running), held=len(s.held),
          pending=plural("waits.pending", len(s.human_waits))),
    ]


def _needs(shown: list[dict], total: int) -> list[str]:
    """The numbered pending items of the Telegram panel (no ids, the reminders' vocabulary)."""
    if not total:
        return ["", t("panel.nothing")]
    lines: list[str] = []
    for n, w in enumerate(shown, start=1):
        held = int(w.get("held", 0))
        age = natural_age(w.get("age_s", 0))
        title = w.get("display") or board_title(w["board"])
        need = _clip_to(w.get("need") or w.get("title") or "", NEED_MAX)  # one line, no fake numbered lines
        lines += ["", f"{circled(n)} {title}"]
        lines.append(
            t("waits.status.blocks", age=age, cards=plural("waits.cards", held)) if held
            else t("waits.status", age=age)
        )
        if need:
            lines.append(t("waits.action", text=need))
    if total > len(shown):
        lines += ["", t("panel.more", n=total - len(shown))]
    return lines


def _tail(s: Snapshot) -> list[str]:
    lines = ["", t("panel.resolved", n=s.resolved_today)]
    if s.last_resolved:
        lr = s.last_resolved
        lines.append(t("panel.resolved.last", board=lr.get("display") or board_title(lr["board"]), time=_clock(lr["ts"])))
    if s.long_guards:
        lines.append(t("panel.guards", n=s.long_guards))
    return lines


def _compose(s: Snapshot, shown: list[dict]) -> str:
    lines = _head(s) + _needs(shown, len(s.human_waits)) + _tail(s)
    if shown:
        lines += ["", t("waits.footer")]
        lines += [t("waits.footer.example", n=n, word=w.get("hint") or t("waits.default_word"))
                  for n, w in enumerate(shown, start=1)]
    return "\n".join(lines)


def _fit(s: Snapshot) -> tuple[str, list[dict]]:
    ordered = _ordered(s)
    for k in range(min(len(ordered), MAX_ITEMS), -1, -1):
        text = _compose(s, ordered[:k])
        if _tg_len(text) <= TELEGRAM_LIMIT:
            return text, ordered[:k]
    return _prefix(_compose(s, []), TELEGRAM_LIMIT), []  # pathological: nothing fits


def shown_waits(s: Snapshot) -> list[dict]:
    """The "Needs you" items as numbered in the Telegram message (1-based, in this order)."""
    return _fit(s)[1]


def render_telegram(s: Snapshot) -> str:
    """The pinned message: title, counts, numbered "Needs you", resolved today, long guards, reply hint."""
    return _fit(s)[0]


def panel_items(s: Snapshot) -> list[dict]:
    """``[{board, task_id, version}]`` for the numbered items, in display order (what a reply selects)."""
    return [{"board": w["board"], "task_id": w["task_id"], "version": int(w.get("version", 0))} for w in shown_waits(s)]


def render_terminal(s: Snapshot) -> str:
    """The same panel for the terminal: every running and held card is listed, with ids, no reply hint."""
    lines = [
        t("panel.term.title", time=_clock(s.ts)),
        t("panel.term.counts", running=len(s.running), held=len(s.held), waits=len(s.human_waits)),
    ]
    for r in s.running:
        lines.append(
            t("panel.term.running", board=r["board"], id=r["task_id"], title=_clip_to(r.get("title") or "", 60),
              assignee=r.get("assignee") or t("status.row.no_assignee"), age=human_age(r.get("age_s", 0)))
        )
    for h in s.held:
        lines.append(
            t("panel.term.held", board=h["board"], id=h["task_id"], reason=_guard_reason_text(h["reason"]),
              age=human_age(h.get("age_s", 0)))
        )
    ordered = _ordered(s)
    if ordered:
        lines += ["", t("panel.term.needs")]
        for n, w in enumerate(ordered, start=1):
            need = _clip_to(w.get("need") or w.get("title") or w["task_id"], NEED_MAX)
            lines.append(t("panel.term.item", n=n, board=w["board"], id=w["task_id"], need=need,
                           age=human_age(w.get("age_s", 0)), held=w.get("held", 0)))
    else:
        lines += ["", t("panel.nothing")]
    lines += ["", t("panel.resolved", n=s.resolved_today)]
    if s.last_resolved:
        lr = s.last_resolved
        lines.append(t("panel.term.resolved.last", board=lr["board"], id=lr["task_id"], time=_clock(lr["ts"])))
    if s.long_guards:
        lines.append(t("panel.guards", n=s.long_guards))
    if ordered:
        lines += ["", t("panel.term.how")]
    return "\n".join(lines)


# -- items of an edited-in-place message --------------------------------------------
def _hist_key(chat_id: int, message_id: int) -> str:
    return f"panel_hist:{int(chat_id)}:{int(message_id)}"


def load_history(store: Store, chat_id: int, message_id: int) -> dict:
    """``{"entries": [{"ts", "items"}, ...oldest first], "truncated": bool}`` of a panel message."""
    raw = store.kv_get(_hist_key(chat_id, message_id))
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return {"entries": [], "truncated": False}
    return {"entries": data["entries"], "truncated": bool(data.get("truncated"))}


def _trim(entries: list, hist: dict) -> list:
    """Keep the last ``HISTORY_LEN`` lists plus, for every kept uncertain list, the nearest certain list
    before it (the only list known to have been on screen, however many unknown-outcome edits came since).

    ``hist["truncated"]`` is set when something before the first kept list was dropped; a kept list
    whose predecessor in time was dropped is flagged ``gap`` (unknown lists may have been on screen
    just before it became current).
    """
    n = len(entries)
    keep = set(range(max(0, n - HISTORY_LEN), n))
    for i in sorted(keep):
        if entries[i].get("uncertain"):
            certain = max((k for k in range(i) if not entries[k].get("uncertain")), default=None)
            if certain is not None:
                keep.add(certain)
    if len(keep) == n:
        return entries
    out: list = []
    prev = None
    for i in sorted(keep):
        e = dict(entries[i])
        if prev is not None and i != prev + 1:
            e["gap"] = True
        out.append(e)
        prev = i
    if min(keep) > 0:
        hist["truncated"] = True
    return out


def record_items(
    store: Store, chat_id: int, message_id: int, items: list, now: float, certain: bool = True
) -> None:
    """Make ``items`` the current numbered list of the panel message (and its reply-validity clock).

    The list joins the history only when it differs from the current one; ``sent_messages`` gets the
    current list, with ``created_at`` = now (the TTL of a live panel runs from its last update).
    ``certain=False`` records an edit whose outcome is unknown (timeout, 5xx: Telegram may have applied
    it): the list joins the history flagged ``uncertain`` (replies stay checked against the list before
    it) and ``sent_messages`` is left alone; a later certain record of the same list is a new, certain
    entry stamped with that later time (the uncertain attempt is kept as it was).
    """
    with store.lock:
        hist = load_history(store, chat_id, message_id)
        entries = hist["entries"]
        if not entries or entries[-1]["items"] != items:
            entry = {"ts": int(now), "items": items}
            if not certain:
                entry["uncertain"] = True
            entries.append(entry)
            hist["entries"] = entries = _trim(entries, hist)
            store.kv_set(_hist_key(chat_id, message_id), json.dumps(hist))
        elif certain and entries[-1].get("uncertain"):
            # the attempt is confirmed only now: it stays as it was (a reply dated before this moment
            # may have been written against the list before it) and a certain entry starts at this time
            entries.append({"ts": int(now), "items": items})
            hist["entries"] = entries = _trim(entries, hist)
            store.kv_set(_hist_key(chat_id, message_id), json.dumps(hist))
        if certain:
            store.add_sent_message(message_id, chat_id, "panel", items, int(now))


def _pick(items: list, number: Optional[int]) -> Optional[dict]:
    """The item a reply selects in ``items`` (``None``: no item, i.e. "which item?")."""
    if number is not None:
        return items[number - 1] if 1 <= number <= len(items) else None
    return items[0] if len(items) == 1 else None


def _same(a: Optional[dict], b: Optional[dict]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return (a["board"], a["task_id"], int(a["version"])) == (b["board"], b["task_id"], int(b["version"]))


@dataclass(frozen=True)
class ReplyView:
    items: list  # the numbered list the reply refers to
    ambiguous: bool  # the user may have been looking at another list that differs at the referenced position
    scope: list  # every item the reply could mean (all must belong to the sender)


def view_for_reply(
    store: Store, chat_id: int, message_id: int, current: list, reply_ts: float, number: Optional[int]
) -> ReplyView:
    """Resolve a reply to a panel message against the list that was current at ``reply_ts``.

    ``current`` is the list recorded in ``sent_messages`` (used only for a panel without history).
    The reply's date is compared with the times the lists became current, with a grace of
    ``SEE_GRACE_S`` on BOTH sides, because neither clock is exact (the host's against Telegram's, and
    an edit whose answer was lost may have been applied earlier than recorded): the list before the
    chosen one is a candidate when the reply is dated soon after the chosen list became current (or
    the chosen list is of unknown outcome), the list after it when the reply is dated soon before it
    did. The reply is ambiguous when a candidate selects a different item than the chosen list, or when
    the list before the oldest one kept would be a candidate.
    """
    hist = load_history(store, chat_id, message_id)
    entries = hist["entries"]
    if not entries:
        return ReplyView(current, False, current)
    j = max((i for i, e in enumerate(entries) if e["ts"] <= reply_ts), default=None)
    if j is None:  # older than everything we kept
        if hist["truncated"]:
            return ReplyView(entries[0]["items"], True, _scope(entries[0]["items"], [], number))
        j = 0
    chosen = entries[j]
    others: list = []
    unknown = False
    # lists that may have been on screen before the chosen one: walk back while the chosen list is
    # fresh or of unknown outcome, through uncertain lists, up to (and including) a certain one
    k, need = j, reply_ts - chosen["ts"] < SEE_GRACE_S or bool(chosen.get("uncertain"))
    while need:
        if entries[k].get("gap"):
            unknown = True  # lists that were dropped may have been on screen just before it
        if k == 0:
            if hist["truncated"] or entries[0].get("uncertain"):
                unknown = True  # no certain list is known
            break
        others.append(entries[k - 1]["items"])
        need = bool(entries[k - 1].get("uncertain"))
        k -= 1
    # (Edits are at least EVAL_INTERVAL_S = 30 s apart because the coordinator's evaluation is throttled
    # to that, so within one grace window at most one list besides the chosen one is normally in play;
    # the loops below do not rely on it.)
    # lists after it: the next ones (soon after the reply) may already have been on screen
    m = j + 1
    if m < len(entries) and entries[m].get("gap"):
        unknown = True  # dropped lists lie between the chosen one and the next
    while m < len(entries) and entries[m]["ts"] - reply_ts < SEE_GRACE_S:
        others.append(entries[m]["items"])
        m += 1
    pick = _pick(chosen["items"], number)
    ambiguous = unknown or any(not _same(pick, _pick(o, number)) for o in others)
    return ReplyView(chosen["items"], ambiguous, _scope(chosen["items"], others, number) if ambiguous else chosen["items"])


def _scope(chosen: list, others: list, number: Optional[int]) -> list:
    picks = [p for p in (_pick(x, number) for x in [chosen, *others]) if p is not None]
    return picks or chosen
