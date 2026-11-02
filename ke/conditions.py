"""Conditions engine: why a card is (not) moving, and a fleet-wide status.

``explain`` evaluates five conditions for one card, always in this order:
``dependencies``, ``guard``, ``live``, ``fresh``, ``wait``. It only reads
(Hermes DB through ``hermes_api``, plugin state through ``Store``).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import hermes_api
from .board_status import label as status_label
from .messages import human_age, t
from .settings import Settings
from .store import Store
from .waits import WAIT_STATUSES, board_active, held_behind, human_block, ladder_anchor, wait_candidates

_CLOSED = frozenset({"done", "archived"})
# Events that carry no news about the card itself.
_NOISE_EVENTS = frozenset({"heartbeat"})
_RESOLVED_ACTIONS = frozenset({"human_done", "guard_release", "infra_undo"})
_KIND_SHOWN_IN = frozenset({"blocked", "todo", "triage"})


@dataclass(frozen=True)
class Condition:
    name: str
    ok: bool
    text: str
    # ``ok=False`` conditions that are a heads-up rather than a hard stop render as ⚠, not ✗.
    warn: bool = False


@dataclass(frozen=True)
class CardConditions:
    board: str
    task_id: str
    status: str
    age_s: int
    conditions: list[Condition] = field(default_factory=list)
    title: str = ""


# -- helpers --------------------------------------------------------------------
def _age(now: float, ts: Optional[int]) -> int:
    return max(0, int(now - ts)) if ts else 0


def _events(conn: Any, tid: str, api: Any = None) -> list:
    return sorted((api if api is not None else hermes_api).events(conn, tid), key=lambda e: e.id)


def _last_event_ts(events: list, kinds: Optional[frozenset] = None) -> Optional[int]:
    for ev in reversed(events):
        if kinds is None:
            if ev.kind not in _NOISE_EVENTS and ev.kind != "commented":
                return ev.created_at
        elif ev.kind in kinds:
            return ev.created_at
    return None




def _count(n: int, key: str) -> str:
    return t(f"{key}.{'one' if n == 1 else 'many'}", n=n)


# -- conditions -----------------------------------------------------------------
def _open_roots(conn: Any, tid: str) -> list:
    """Blocking roots of ``tid``: open ancestors with no open parent of their own.

    Memoized per node, so a diamond (X <- P1, P2 <- R) reports R once and never an
    intermediate node; nodes still being resolved (a dependency cycle) contribute nothing.
    If a cycle leaves no root at all, the open direct parents are reported instead.
    """
    memo: dict[str, list] = {}
    in_progress: set[str] = set()

    def open_parents(node: str) -> list:
        found = []
        for pid in hermes_api.parents(conn, node):
            parent = hermes_api.task(conn, pid)
            if parent is not None and parent.status not in _CLOSED:
                found.append(parent)
        return found

    def roots_of(parent: Any) -> list:
        if parent.id in memo:
            return memo[parent.id]
        if parent.id in in_progress:
            return []
        in_progress.add(parent.id)
        above = open_parents(parent.id)
        roots = [parent] if not above else [r for p in above for r in roots_of(p)]
        in_progress.discard(parent.id)
        memo[parent.id] = roots
        return roots

    in_progress.add(tid)
    direct = open_parents(tid)
    unique: dict[str, Any] = {}
    for parent in direct:
        for root in roots_of(parent):
            unique.setdefault(root.id, root)
    if not unique:
        unique = {p.id: p for p in direct}
    return list(unique.values())


def _dependencies(conn, board, task, store, now, settings) -> Condition:
    roots = _open_roots(conn, task.id)
    if not roots:
        return Condition("dependencies", True, t("explain.dependencies.none"))
    parts = []
    for root in roots:
        label = root.status
        if root.block_kind and root.status in _KIND_SHOWN_IN:
            label += f" {root.block_kind}"
        root_events = _events(conn, root.id)
        since = _last_event_ts(root_events) or root.created_at
        wait = store.latest_wait(board, root.id)
        if wait and wait.get("owner"):
            owner = wait["owner"]
        elif human_block(root, root_events, settings) is not None:
            owner = settings.owner_for(board)
        else:
            owner = root.assignee or t("explain.dependencies.no_owner")
        parts.append(
            t("explain.dependencies.waiting", id=root.id, label=label, age=human_age(_age(now, since)), owner=owner)
        )
    return Condition("dependencies", False, "; ".join(parts))


def _guard_hits(events: list) -> tuple[list, str]:
    """``respawn_guarded`` events since the last ``claimed`` (heartbeats never count) and the latest reason."""
    last_claim = max((i for i, e in enumerate(events) if e.kind == "claimed"), default=-1)
    hits = [e for e in events[last_claim + 1:] if e.kind == "respawn_guarded"]
    payload = hits[-1].payload if hits and isinstance(hits[-1].payload, dict) else {}
    return hits, payload.get("reason") or t("explain.guard.unknown_reason")


def current_guard(conn: Any, tid: str) -> Optional[dict]:
    """The card's active respawn guard: ``{"reason", "count", "since"}`` or ``None``.

    Built from the ``respawn_guarded`` events since the last ``claimed``; ``reason`` is the latest
    one and ``since`` the time of the first. The one implementation behind ``explain`` and ``release``.
    """
    hits, reason = _guard_hits(_events(conn, tid))
    if not hits:
        return None
    return {"reason": reason, "count": len(hits), "since": hits[0].created_at}


def _guard(conn, board, task, store, now, settings) -> Condition:
    guard = current_guard(conn, task.id)
    if guard is None:
        return Condition("guard", True, t("explain.guard.none"))
    since = human_age(_age(now, guard["since"]))
    return Condition("guard", False, t("explain.guard.active", reason=guard["reason"], count=guard["count"], age=since))


def _live(conn, board, task, store, now, settings) -> Condition:
    if task.status != "running" or task.current_run_id is None:
        return Condition("live", True, t("explain.live.none"))
    hb = (
        t("explain.live.heartbeat", age=human_age(_age(now, task.last_heartbeat_at)))
        if task.last_heartbeat_at
        else t("explain.live.no_heartbeat")
    )
    if task.worker_pid is None:
        return Condition("live", False, t("explain.live.no_pid", run=task.current_run_id, heartbeat=hb), warn=True)
    return Condition(
        "live", True, t("explain.live.ok", run=task.current_run_id, pid=task.worker_pid, heartbeat=hb)
    )


def _fresh(conn, board, task, store, now, settings) -> Condition:
    run = hermes_api.latest_summary_run(conn, task.id)
    if run is None:
        return Condition("fresh", True, t("explain.fresh.none"))
    ts = run.ended_at if run.ended_at is not None else run.started_at
    events = _events(conn, task.id)
    boundary = max(
        (e.id for e in events if e.run_id == run.id and e.kind not in _NOISE_EVENTS), default=None
    )
    if boundary is not None:
        after = [e for e in events if e.id > boundary]
    else:
        after = [e for e in events if e.created_at > ts]
    news = [e for e in after if e.kind not in _NOISE_EVENTS and e.kind != "commented"]
    # Comments by timestamp, or by the "commented" events past the run's last event
    # (exact within the same second); the larger of the two wins.
    by_time = sum(1 for c in hermes_api.comments(conn, task.id) if c.created_at > ts)
    n_comments = max(by_time, sum(1 for e in after if e.kind == "commented"))
    age = human_age(_age(now, ts))
    if not n_comments and not news:
        return Condition("fresh", True, t("explain.fresh.clean", age=age))
    bits = []
    if n_comments:
        bits.append(_count(n_comments, "explain.comment"))
    if news:
        bits.append(_count(len(news), "explain.event"))
    joined = bits[0] if len(bits) == 1 else t("explain.fresh.and", first=bits[0], second=bits[1])
    return Condition("fresh", False, t("explain.fresh.stale", age=age, news=joined), warn=True)


def _next_reminder_in(
    age: int, settings: Settings, row: Optional[dict] = None, now: float = 0.0, digest: Optional[int] = None
) -> int:
    """Seconds to the wait's next message. ``digest`` is its effective period (the blocking one when it
    holds other cards); a digest already sent is repeated one period after it was sent."""
    for threshold in sorted((*settings.reminders_s, settings.escalate_after_s)):
        if threshold > age:
            return threshold - age
    digest = max(1, settings.digest_every_s if digest is None else digest)
    last = None if row is None else row.get("last_sent_at")
    if row is not None and row.get("stage") == "digest" and last is not None:
        return max(0, int(last + digest - now))
    return max(0, settings.escalate_after_s + digest - age)  # the first digest; 0 when it is overdue


def _digest_period(conn, task, settings: Settings) -> int:
    """The wait's digest period: ``blocking_digest_s`` when it holds other cards back, else the plain one."""
    plain, blocking = max(1, settings.digest_every_s), max(1, settings.blocking_digest_s)
    if plain == blocking:
        return plain
    try:
        return blocking if held_behind(hermes_api, conn, task.id) else plain
    except Exception:
        return plain


def _wait(conn, board, task, store, now, settings) -> Condition:
    row = store.latest_wait(board, task.id) if task.status in WAIT_STATUSES else None
    if row is not None:
        block = human_block(task, _events(conn, task.id), settings)
        if block is None or block.version != int(row["version"]):
            row = None  # the card's block changed since the tracker last synced: not that wait any more
    if row is None:
        return Condition("wait", True, t("explain.wait.none"))
    age = _age(now, row["started_at"])
    owner = row.get("owner") or settings.owner_for(board)
    statuses = store.board_statuses()
    if not board_active(board, statuses):
        status = status_label(statuses[board]["status"]).lower()
        return Condition("wait", False, t("explain.wait.paused", owner=owner, age=human_age(age), status=status))
    ladder = _age(now, ladder_anchor(row, statuses))  # the ladder restarts at a reactivation
    return Condition(
        "wait",
        False,
        t(
            "explain.wait.active", owner=owner, age=human_age(age),
            next=human_age(_next_reminder_in(ladder, settings, row, now, _digest_period(conn, task, settings))),
        ),
    )


_CONDITIONS = (_dependencies, _guard, _live, _fresh, _wait)


def _project_hold(conn, board, task, store, now, settings) -> Optional[Condition]:
    """A ``ready`` card of a suspended/done board: its start is held by the ``kanban_pre_claim`` veto (the
    card would otherwise look stuck). ``None`` (no line) for any other card or an active board."""
    if getattr(task, "status", None) != "ready":
        return None
    row = store.board_statuses().get(board)
    if row is None or row["status"] == "active":
        return None
    return Condition("project", False, t("explain.project.held", status=status_label(row["status"]).lower()), warn=True)


# -- public API -----------------------------------------------------------------
def explain(
    conn: Any,
    board: str,
    task_id: str,
    store: Store,
    now: float,
    settings: Optional[Settings] = None,
) -> CardConditions:
    """Evaluate every condition for ``task_id``. Raises ``LookupError`` for an unknown card."""
    task = hermes_api.task(conn, task_id)
    if task is None:
        raise LookupError(t("explain.unknown_card", card=task_id, board=board))
    settings = settings if settings is not None else Settings.from_mapping({})
    since = _last_event_ts(_events(conn, task_id)) or task.created_at
    return CardConditions(
        board=board,
        task_id=task.id,
        status=task.status,
        age_s=_age(now, since),
        conditions=[fn(conn, board, task, store, now, settings) for fn in _CONDITIONS]
        + [c for c in (_project_hold(conn, board, task, store, now, settings),) if c is not None],
        title=task.title,
    )


def format_explain(cc: CardConditions) -> str:
    """Header line (id, title, status, age) plus one ✓/✗/⚠ line per condition."""
    lines = [t("explain.header", id=cc.task_id, title=cc.title, status=cc.status, age=human_age(cc.age_s))]
    width = max((len(c.name) for c in cc.conditions), default=0)
    for c in cc.conditions:
        mark = "✓" if c.ok else ("⚠" if c.warn else "✗")
        lines.append(f"  {mark} {c.name.ljust(width)}  {c.text}")
    return "\n".join(lines)


def _local_midnight(now: float) -> int:
    lt = time.localtime(now)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def fleet_status(store: Store, now: float, settings: Optional[Settings] = None, api: Any = None) -> dict:
    """Snapshot across all boards: running cards, human waits, guarded cards, resolved today.

    A board that cannot be read is skipped rather than failing the whole status. ``api`` is the
    Hermes adapter to read the boards through (default: ``hermes_api``).
    """
    settings = settings if settings is not None else Settings.from_mapping({})
    api = api if api is not None else hermes_api
    boards = api.boards()
    running: list[dict] = []
    human_waits: list[dict] = []
    guarded: list[dict] = []
    for board in boards:
        try:
            with api.connect(board) as conn:
                for task in api.list_tasks(conn, status="running"):
                    running.append(
                        {
                            "board": board,
                            "task_id": task.id,
                            "title": task.title,
                            "assignee": task.assignee,
                            "age_s": _age(now, task.started_at or task.created_at),
                            "heartbeat_age_s": _age(now, task.last_heartbeat_at) if task.last_heartbeat_at else None,
                        }
                    )
                for task in wait_candidates(api, conn):
                    block = human_block(task, _events(conn, task.id, api), settings)
                    if block is None:  # the one definition of a human wait (shared with WaitTracker)
                        continue
                    wait = store.latest_wait(board, task.id)
                    if wait is not None and int(wait["version"]) != block.version:
                        wait = None  # a row of an older block (the tracker has not synced yet)
                    since = (wait or {}).get("started_at") or block.started_at
                    human_waits.append(
                        {
                            "board": board,
                            "task_id": task.id,
                            "title": task.title,
                            "owner": (wait or {}).get("owner") or settings.owner_for(board),
                            "age_s": _age(now, since),
                        }
                    )
                for task in api.list_tasks(conn, status="ready"):
                    hits, reason = _guard_hits(_events(conn, task.id, api))
                    if not hits:
                        continue
                    guarded.append(
                        {
                            "board": board,
                            "task_id": task.id,
                            "title": task.title,
                            "reason": reason,
                            "count": len(hits),
                            "age_s": _age(now, hits[0].created_at),
                        }
                    )
        except Exception:
            continue
    resolved = sum(1 for a in store.actions_since(_local_midnight(now)) if a["kind"] in _RESOLVED_ACTIONS)
    return {
        "running": running,
        "human_waits": human_waits,
        "guarded": guarded,
        "resolved_today": resolved,
        "boards": boards,
    }


def format_status(fs: dict) -> str:
    """Plain-text rendering of ``fleet_status`` for the terminal."""
    lines = [
        t("status.running", n=len(fs["running"])),
        t("status.human_waits", n=len(fs["human_waits"])),
        t("status.guarded", n=len(fs["guarded"])),
        t("status.resolved_today", n=fs["resolved_today"]),
        t("status.boards", boards=", ".join(fs["boards"]) or t("status.no_boards")),
    ]
    for r in fs["running"]:
        hb = (
            t("status.row.heartbeat", age=human_age(r["heartbeat_age_s"]))
            if r["heartbeat_age_s"] is not None
            else ""
        )
        lines.append(
            t(
                "status.row.running",
                board=r["board"],
                id=r["task_id"],
                title=r["title"],
                assignee=r["assignee"] or t("status.row.no_assignee"),
                age=human_age(r["age_s"]),
                heartbeat=hb,
            )
        )
    for w in fs["human_waits"]:
        lines.append(
            t(
                "status.row.wait",
                board=w["board"],
                id=w["task_id"],
                title=w["title"],
                owner=w["owner"],
                age=human_age(w["age_s"]),
            )
        )
    for g in fs["guarded"]:
        lines.append(
            t(
                "status.row.guarded",
                board=g["board"],
                id=g["task_id"],
                title=g["title"],
                reason=g["reason"],
                count=g["count"],
                age=human_age(g["age_s"]),
            )
        )
    return "\n".join(lines)
