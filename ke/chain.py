"""The block chain: which open cards a blocked card holds back, level by level (``GET /chain``).

Level 0 is the card the block starts at. A held card sits one level below its deepest parent in the
chain, so parallel cards share a level and a card waiting on two others comes after both. Only reads.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Optional

from . import hermes_api
from .asks import parse_ask
from .messages import t
from .waits import current_block, display_name

_CLOSED = frozenset({"done", "archived"})
MAX_CARDS = 200
SHORT_MAX = 28


def short_title(title: Optional[str], fallback: str) -> str:
    """The card's short name: what comes before `` — `` in its title, clipped."""
    head = (title or "").split(" — ", 1)[0].strip() or fallback
    return head if len(head) <= SHORT_MAX else head[: SHORT_MAX - 1].rstrip() + "…"


def _walk(api: Any, conn: Any, root: str) -> dict[str, Any]:
    """The open cards below ``root``, breadth first and bounded, each once."""
    found: dict[str, Any] = {}
    seen, queue = {root}, deque([root])
    while queue and len(found) < MAX_CARDS:
        for child in api.children(conn, queue.popleft()):
            if child in seen:
                continue
            seen.add(child)
            task = api.task(conn, child)
            if task is not None and task.status not in _CLOSED:
                found[child] = task
                queue.append(child)
    return found


def _levels(root: str, parents: dict[str, list[str]]) -> dict[str, int]:
    """Longest distance from ``root``; bounded passes, so a cycle cannot hang it."""
    level = {root: 0, **{tid: 1 for tid in parents}}
    for _ in range(len(parents)):
        changed = False
        for tid, ps in parents.items():
            deepest = max((level[p] + 1 for p in ps), default=1)
            if level[tid] < deepest <= len(parents):
                level[tid], changed = deepest, True
        if not changed:
            break
    return level


def _summary(levels: list[list[dict]]) -> str:
    joiner = f" {t('ui.chain.and')} "
    return " → ".join(joiner.join(n["short"] for n in row) for row in levels)


def explain_block(board: str, task_id: str, api: Any = hermes_api) -> dict:
    """The chain below ``task_id``. LookupError when the board or the card does not exist."""
    if board not in api.boards():
        raise LookupError(board)
    with api.connect(board) as conn:
        root = api.task(conn, task_id)
        if root is None:
            raise LookupError(task_id)
        held = _walk(api, conn, task_id)
        inside = {task_id, *held}
        parents: dict[str, list[str]] = {}
        outside: dict[str, list[str]] = {}
        for tid in held:
            ps = api.parents(conn, tid)
            parents[tid] = [p for p in ps if p in inside]
            others = (api.task(conn, p) for p in ps if p not in inside)
            outside[tid] = [short_title(o.title, o.id) for o in others if o is not None and o.status not in _CLOSED]
        block = current_block(root, api.events(conn, task_id))
    reason = block.reason if block is not None else ""
    ask = parse_ask(reason)
    level = _levels(task_id, parents)

    def node(tid: str, task: Any) -> dict:
        return {
            "task_id": tid,
            "title": task.title,
            "short": short_title(task.title, tid),
            "status": task.status,
            "assignee": task.assignee,
            "also_waits_on": outside.get(tid, []),
        }

    rows: list[list[dict]] = [[node(task_id, root)]]
    for tid, task in sorted(held.items(), key=lambda kv: level[kv[0]]):
        while len(rows) <= level[tid]:
            rows.append([])
        rows[level[tid]].append(node(tid, task))
    rows = [row for row in rows if row]
    return {
        "board": board,
        "board_title": display_name(api, board) or board,
        "need": ask.need if ask is not None else reason.strip(),
        "levels": rows,
        "held_count": len(held),
        "summary": _summary(rows),
    }
