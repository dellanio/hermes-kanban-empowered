"""Live bug 01/11/2026: a card with no structured ask (reason "initial_status") showed the raw marker as the action."""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from conftest import create_task
from test_msg_format import _ask, _due
from test_msg_format import pt  # noqa: F401 (fixture)
from test_waits import _tracker

TITLE = "OPERADOR — desbloquear CHECK-SBN t_78387100 (vale arq, D-SBN-04)"
CLEAN_TITLE = "OPERADOR — desbloquear CHECK-SBN (vale arq, D-SBN-04)"
BODY = "Validar o relatório do SBN antes do deploy, id t_1a2b3c4d. Depois liberar a fila."


def live_due(body="", board_name="", reason="initial_status", held=(), title=TITLE, age=2 * 3600):
    d = _due("t_85f943b1", "matrix", age, None, held=held, board_name=board_name, reason=reason)
    return replace(d, title=title, body=body)


# -- technical markers -------------------------------------------------------------------
@pytest.mark.parametrize("reason", [
    "initial_status", "dependency", "manual", "gave_up", "unblocked", "parent_not_done", "no_open_parent",
    "needs_input", "ttl_expired_worker_alive", "", "   ", "t_78387100", "1234", "—",
])
def test_technical_or_empty_reasons_are_not_human_text(reason):
    from ke.messages import reason_text

    assert reason_text(reason) == ""


@pytest.mark.parametrize("reason", [
    "Aguardando aprovação do deploy", "please look at the logs", "revisar", "Approve the deploy.",
    "Initial status", "ok", "use_case? no — please decide",
])
def test_human_reasons_are_kept(reason):
    from ke.messages import reason_text

    assert reason_text(reason) == reason


def test_marker_detection_is_conservative():
    from ke.messages import is_technical_reason

    assert is_technical_reason("initial_status") and is_technical_reason("some_new_marker")
    assert not is_technical_reason("Initial status") and not is_technical_reason("aprovar deploy")
    assert not is_technical_reason("dois palavras")


# -- the live scenario ---------------------------------------------------------------------
def test_live_scenario_pt(pt):
    from ke.messages import render_waits

    txt = render_waits("sponsor", [live_due(board_name="MATRIX")])
    assert txt == "\n".join([
        "📋 KANBAN · PRECISA DE VOCÊ",
        "1 pendência",
        "",
        "MATRIX",
        "⏳ Parado há 2 horas",
        "",
        f"👉 {CLEAN_TITLE}",
        "Responda a esta mensagem quando concluir, ou diga o que falta.",
    ])
    assert "initial_status" not in txt and "t_78387100" not in txt and "cards" not in txt


def test_live_scenario_en():
    from ke.messages import render_waits

    txt = render_waits("sponsor", [live_due(board_name="MATRIX")])
    assert txt == "\n".join([
        "📋 KANBAN · NEEDS YOU",
        "1 pending",
        "",
        "MATRIX",
        "⏳ Stalled for 2 hours",
        "",
        f"👉 {CLEAN_TITLE}",
        "Reply to this message when it's done, or say what's missing.",
    ])


def test_title_goes_through_role_replacement():
    from ke import messages
    from ke.messages import render_waits

    messages.set_roles(["operador"])
    try:
        txt = render_waits("s", [live_due()])
    finally:
        messages.set_roles(())
    assert "👉 you — desbloquear CHECK-SBN (vale arq, D-SBN-04)" in txt and "OPERADOR" not in txt


def test_body_first_sentence_is_the_context_line():
    from ke.messages import render_waits

    txt = render_waits("s", [live_due(body=BODY)])
    lines = txt.splitlines()
    assert lines[-1] == "Validar o relatório do SBN antes do deploy, id."
    assert lines[-2] == "" and "t_1a2b3c4d" not in txt and "Depois liberar" not in txt


def test_long_body_context_is_clipped_to_200_and_markdown_noise_dropped():
    from ke.messages import render_waits

    txt = render_waits("s", [live_due(body="palavra " * 80)])
    ctx = txt.splitlines()[-1]
    assert ctx.startswith("palavra") and len(ctx) <= 200


def test_no_context_when_body_is_empty_or_only_ids():
    from ke.messages import render_waits

    for body in ("", "t_1a2b3c4d", "   "):
        txt = render_waits("s", [live_due(body=body)])
        assert txt.splitlines()[-1] == "Reply to this message when it's done, or say what's missing."


def test_title_with_only_an_id_falls_back_to_no_reason():
    from ke.messages import render_waits

    txt = render_waits("s", [live_due(title="t_78387100")])
    assert "👉 (no reason given)" in txt


def test_human_reason_without_ask_still_wins_over_title():
    from ke.messages import render_waits

    txt = render_waits("s", [live_due(reason="Aprove o deploy", body=BODY)])
    assert "👉 Aprove o deploy" in txt and "desbloquear" not in txt


# -- header ---------------------------------------------------------------------------------
def test_header_omits_blocked_part_when_zero_and_keeps_it_otherwise():
    from ke.messages import render_waits

    assert render_waits("s", [live_due()]).splitlines()[1] == "1 pending"
    assert render_waits("s", [live_due(), live_due()]).splitlines()[1] == "2 pending"
    held = render_waits("s", [live_due(held=["t_00000001"])])
    assert held.splitlines()[1] == "1 pending · 1 blocked card"


def test_header_pt_without_blocked(pt):
    from ke.messages import render_waits

    assert render_waits("s", [live_due(), live_due()]).splitlines()[1] == "2 pendências"
    assert render_waits("s", [live_due(held=["t_00000001", "t_00000002"])]).splitlines()[1] == \
        "1 pendência · 2 cards bloqueados"


# -- reply hint and footer -------------------------------------------------------------------
def test_footer_example_for_items_without_reply_words(pt):
    from ke.messages import render_waits

    d1 = _due("t_0000000a", "voz", 3600, _ask("Ligue a placa.", "porque sim", 'responda "ligada"'))
    txt = render_waits("s", [d1, live_due()])
    assert txt.splitlines()[-2:] == ["“1 — ligada”", "“2 — <sua resposta>”"]
    assert "feito" not in txt


def test_footer_example_en():
    from ke.messages import render_waits

    txt = render_waits("s", [live_due(), live_due()])
    assert txt.splitlines()[-2:] == ["“1 — <your answer>”", "“2 — <your answer>”"]


def test_reply_hint_without_ask_is_neutral_and_with_ask_unchanged():
    from ke.messages import reply_hint

    assert reply_hint(None) == "<your answer>"
    assert reply_hint(_ask("x.", "y", "responda feito")) == "feito"
    assert reply_hint(_ask("x.", "y", "r", options=[("B", "z")])) == "B"


def test_structured_ask_cards_are_unchanged_even_with_a_title():
    from ke.messages import render_waits

    d = replace(_due("t_0000000a", "voz", 3600, _ask("Conecte a placa.", "porque", 'responda "conectei"')),
                title="Some title", body="Some body sentence.")
    txt = render_waits("s", [d])
    assert "Some title" not in txt and "Some body" not in txt
    assert "Then reply: “conectei”." in txt


# -- tracker populates title/body; initial_status human card ----------------------------------
def test_tracker_builds_due_with_title_and_body_for_initial_status_card(kanban_home, tmp_path):
    from ke import hermes_api
    from ke.messages import render_waits

    wt, store, _ = _tracker(tmp_path)
    with hermes_api.connect("default") as conn:
        tid = create_task(conn, title=TITLE, body=BODY, assignee="default", initial_status="blocked",
                          created_by="diretor")
    wt.sync(time.time())
    (row,) = store.all_waits()
    dues = wt.collect_due(row["started_at"] + 1800)
    assert len(dues) == 1
    d = dues[0]
    assert d.ask is None and d.reason == "initial_status" and d.title == TITLE and d.body == BODY
    txt = render_waits("s", dues)
    assert "initial_status" not in txt and "t_78387100" not in txt and tid not in txt
    assert f"👉 {CLEAN_TITLE}" in txt
    assert txt.splitlines()[1] == "1 pending"


# -- panel and inbound confirmation ------------------------------------------------------------
def test_panel_uses_the_title_for_a_technical_reason(kanban_home, tmp_path):
    from ke import hermes_api as h
    from ke.panel import build_snapshot, render_telegram
    from ke.store import Store

    store = Store(tmp_path / "state" / "state.db")
    with h.connect("default") as conn:
        create_task(conn, title=TITLE, body=BODY, assignee="default", initial_status="blocked", created_by="diretor")
    snap = build_snapshot(store, h, time.time() + 7200)
    (w,) = snap.human_waits
    assert w["need"] == CLEAN_TITLE
    assert w["hint"] == "<your answer>"
    text = render_telegram(snap)
    assert "initial_status" not in text and "t_78387100" not in text
    assert f"👉 {CLEAN_TITLE}" in text
    assert "“1 — <your answer>”" in text


def test_short_action_prefers_human_reason_then_title():
    from ke.messages import short_action

    assert short_action(None, "initial_status", TITLE) == CLEAN_TITLE
    assert short_action(None, "Aprove o deploy", TITLE) == "Aprove o deploy"
    assert short_action(None, "", "") == "(no reason given)"
    assert short_action(None, "initial_status") == "(no reason given)"


# -- fix round 1: the footer placeholder / neutral instruction typed back must not resolve -----------
from test_msg_format import rig3, rig1  # noqa: E402,F401 (fixtures)
from test_inbound import _comments, _status  # noqa: E402
from test_waits import pt_br  # noqa: E402,F401 (fixture)

NEUTRAL_PT = "Responda a esta mensagem quando concluir, ou diga o que falta."
NEUTRAL_EN = "Reply to this message when it's done, or say what's missing."


@pytest.mark.parametrize("text", [
    "<sua resposta>", "<your answer>", "<qualquer coisa>", "<>", " <x> ", "<your answer>.", "<resposta>!",
    NEUTRAL_PT, NEUTRAL_EN, NEUTRAL_EN.lower(), NEUTRAL_PT.rstrip("."), NEUTRAL_EN + " ✅",
])
def test_classify_marks_placeholders_and_the_neutral_instruction_as_placeholder(text):
    from ke.inbound import classify

    assert classify(text, None)[0] == "placeholder"
    assert classify(text, _ask("x.", "y", "responda feito"))[0] == "placeholder"


@pytest.mark.parametrize("text", [
    "a < b e c > d", "<b>negrito</b>", "use <b> e <i>", "ok <sua resposta>", "feito", "responda quando concluir",
])
def test_classify_leaves_real_text_alone(text):
    from ke.inbound import classify

    assert classify(text, None)[0] != "placeholder"


def test_typed_placeholder_does_not_resolve_pt(rig3, pt_br):  # noqa: F811
    for n, text in enumerate(["1 — <sua resposta>", "2 — <your answer>", f"3 — {NEUTRAL_PT}"], start=1):
        assert rig3.reply(text, message_id=str(80 + n))["action"] == "skip"
        assert unbranded_text(rig3).startswith("Escreva a resposta depois do número do item")
    assert [_status(t) for t in rig3.tids] == ["blocked"] * 3
    assert all("sua resposta" not in c and "your answer" not in c for t in rig3.tids for c in [_comments(t)[-1] if _comments(t) else ""])


def test_typed_placeholder_does_not_resolve_en(rig3):
    for n, text in enumerate(["1 — <your answer>", "2 — <sua resposta>", f"3 — {NEUTRAL_EN}"], start=1):
        assert rig3.reply(text, message_id=str(90 + n))["action"] == "skip"
        assert unbranded_text(rig3).startswith("Write your answer after the item number")
    assert [_status(t) for t in rig3.tids] == ["blocked"] * 3


def test_single_item_bare_placeholder_does_not_resolve(rig1):
    assert rig1.reply("<your answer>")["action"] == "skip"
    assert _status(rig1.tids[0]) == "blocked"
    assert unbranded_text(rig1).startswith("Write your answer")


def unbranded_text(rig):
    from conftest import unbranded

    return unbranded(rig.tg.last_text)


# -- fix round 2 ------------------------------------------------------------------------------
@pytest.fixture
def rig_ask_less(kanban_home, tmp_path, fake_tg, clock):
    from test_msg_format import Rig

    return Rig(tmp_path, fake_tg, clock, ["initial_status"])


@pytest.fixture
def rig_ask_less2(kanban_home, tmp_path, fake_tg, clock):
    from test_msg_format import Rig

    return Rig(tmp_path, fake_tg, clock, ["initial_status", "dependency"])


def test_confirmation_for_an_ask_less_card_names_the_card_title_and_the_board_once(rig_ask_less):
    rig_ask_less.reply("feito")
    assert _status(rig_ask_less.tids[0]) != "blocked"
    text = unbranded_text(rig_ask_less)
    assert text.splitlines()[0] == "✅ DEFAULT: card 0"
    assert text.count("DEFAULT") == 1 and "t_" not in text


def test_empty_reply_guidance_for_an_ask_less_item_is_neutral(rig_ask_less):
    rig_ask_less.reply("<your answer>")
    text = unbranded_text(rig_ask_less)
    assert "<your answer>" in text and "done" not in text and "feito" not in text
    assert _status(rig_ask_less.tids[0]) == "blocked"


def test_empty_reply_guidance_for_a_structured_item_keeps_its_word(rig1):
    rig1.reply("<your answer>")
    assert "conectei" in unbranded_text(rig1)


def test_which_item_guidance_for_ask_less_items_is_neutral(rig_ask_less2):
    rig_ask_less2.reply("qualquer coisa")
    text = unbranded_text(rig_ask_less2)
    assert "<your answer>" in text and "done" not in text and "feito" not in text


def test_which_item_guidance_for_ask_less_items_in_pt(rig_ask_less2, pt_br):  # noqa: F811
    rig_ask_less2.reply("qualquer coisa")
    text = unbranded_text(rig_ask_less2)
    assert "<sua resposta>" in text and "feito" not in text


def test_panel_fallback_hint_is_the_neutral_example(kanban_home, tmp_path):
    from ke.panel import _enrich_waits
    from ke.store import Store

    class Broken:
        def connect(self, board):
            raise RuntimeError("locked")

    store = Store(tmp_path / "state" / "state.db")
    (w,) = _enrich_waits(store, Broken(), [{"board": "b", "task_id": "t_00000001"}])
    assert w["hint"] == "<your answer>"


@pytest.mark.parametrize("body, expected", [
    ("# Contexto\n\nValidar o relatório do SBN antes do deploy.", "Validar o relatório do SBN antes do deploy."),
    ("## A\n### B\nTexto útil do corpo. Mais.", "Texto útil do corpo."),
    ("**Importante:** validar o relatório antes do deploy.", "Importante: validar o relatório antes do deploy."),
    ("__Atenção__ confira o `relatório` final.", "Atenção confira o relatório final."),
    ("1. Validar o relatório do SBN\n2. Liberar a fila", "Validar o relatório do SBN"),
    ("- Validar o relatório do SBN\n- Liberar a fila", "Validar o relatório do SBN"),
    ("| col | col |\n|---|---|\n| a | b |\n\nValidar o relatório do SBN.", "Validar o relatório do SBN."),
    ("```\ncodigo qualquer aqui dentro\n```\nValidar o relatório do SBN.", "Validar o relatório do SBN."),
    ("Veja [o relatório](https://x.example/r?id=1) e valide.", "Veja o relatório e valide."),
    ("---\n\nValidar o relatório do SBN.", "Validar o relatório do SBN."),
    ("> Validar o relatório do SBN.", "Validar o relatório do SBN."),
    ("snake_case_name stays", "snake_case_name stays"),
])
def test_body_context_picks_the_first_useful_line(body, expected):
    from ke.messages import body_context

    assert body_context(body) == expected


@pytest.mark.parametrize("body", [
    "# So um titulo", "ok", "t_1a2b3c4d", "| a | b |\n|---|---|", "```\nfoo bar baz qux quux\n```",
    "1234567890", "**ok**", "",
])
def test_body_context_none_when_nothing_useful(body):
    from ke.messages import body_context

    assert body_context(body) == ""


def test_huge_body_is_truncated_before_cleaning_so_it_is_fast():
    import time as _t

    from ke.messages import body_context

    t0 = _t.time()
    out = body_context("deadbeef" * 200000 + " fim")
    assert _t.time() - t0 < 2.0 and len(out) <= 200
    t0 = _t.time()
    assert body_context("a" * 5000 + ". " + "b" * 50) != "b" * 50
    assert _t.time() - t0 < 2.0
