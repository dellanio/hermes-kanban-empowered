import pytest

from conftest import block_task, create_task


def _chain(conn):
    """root -> a -> (b, c) -> d, plus an open parent of d outside the chain."""
    from hermes_cli import kanban_db as kb

    root = create_task(conn, title="HR-11 — run it", assignee="dev")
    assert kb.claim_task(conn, root) is not None
    assert block_task(conn, root, reason="Preciso: ligar\nPor quê: b\nComo responder: c", kind="needs_input")
    a = create_task(conn, title="WP-003 — tests", parents=[root])
    b = create_task(conn, title="GATE FINAL", parents=[a])
    c = create_task(conn, title="GATE VISUAL", parents=[a])
    other = create_task(conn, title="OTHER — elsewhere")
    d = create_task(conn, title="ENTREGA", parents=[b, c, other])
    return root, a, b, c, d


def test_levels_summary_and_outside_parents(kanban_home):
    from ke import hermes_api
    from ke.chain import explain_block

    with hermes_api.connect("default") as conn:
        root, a, b, c, d = _chain(conn)
    out = explain_block("default", root)
    assert [[n["task_id"] for n in row] for row in out["levels"]][:2] == [[root], [a]]
    assert {n["task_id"] for n in out["levels"][2]} == {b, c} and out["levels"][3][0]["task_id"] == d
    assert out["levels"][3][0]["also_waits_on"] == ["OTHER"]
    assert out["held_count"] == 4 and out["need"] == "ligar"
    assert out["summary"].startswith("HR-11 → WP-003 → GATE")


def test_card_holding_nothing_and_unknown_card(kanban_home):
    from ke import hermes_api
    from ke.chain import explain_block

    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title="alone")
    out = explain_block("default", tid)
    assert out["held_count"] == 0 and len(out["levels"]) == 1
    with pytest.raises(LookupError):
        explain_block("default", "t_nope")
