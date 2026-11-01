"""Telegram message format v2 (owner request 31/10/2026): layout, language, config alerts, item-linked replies."""
from __future__ import annotations

import pathlib

import pytest

from conftest import create_task, unbranded, make_coordinator, settings_for_test
from test_inbound import OWNER, BOSS, STRANGER, _comments, _status, reply_event
from test_waits import make_human_block, pt_br  # noqa: F401 (pt_br is a fixture)

H = 3600
D = 86400
ROOT = pathlib.Path(__file__).resolve().parent.parent / "locales"


def _due(task_id, board, age_s, ask, held=(), board_name="", stage="reminder1", reason=""):
    from ke.waits import Due

    return Due(
        board=board, task_id=task_id, version=7, owner="sponsor", stage=stage, age_s=age_s,
        ask=ask, held_behind=list(held), reason=reason, board_name=board_name,
    )


def _ask(need, why, reply, options=(), subject="", next_step=""):
    from ke.asks import Ask

    return Ask(need, why, reply, tuple(options), subject, next_step)


def owner_scenario_pt():
    """The owner's reference message (3 items) as dues."""
    return [
        _due("t_0000000a", "voz-hermes", 14 * D + 3 * H,
             _ask("Conecte a ES3C28P ao USB do notebook.", "O gravador só aparece com a placa no USB.",
                  'responda "conectei"', next_step="backup e gravação."),
             held=["t_00000001", "t_00000002"]),
        _due("t_0000000b", "portal-imobiliario", 11 * D + 5 * H,
             _ask("Teste a conversa pelo celular.", "O pitch e os prints já foram enviados.",
                  "Responda “validado” ou diga o que incomodou"),
             held=["t_00000003"], board_name="Portal Imobiliário"),
        _due("t_0000000c", "matrix", 15 * H + 20 * 60,
             _ask("Escolha como testar a gravação de memórias.",
                  "A execução das 08h falhou com erro HTTP 400 do provedor (t_1a2b3c4d). "
                  "Falta validar uma rodada completa.",
                  "responda com a letra",
                  options=[("A", "Rodar agora"), ("B", "Aguardar a próxima execução"),
                           ("C", "Não recriar o ranking")],
                  subject="Money Hunter"),
             held=["t_00000004", "t_00000005", "t_00000006", "t_00000007", "t_00000008"]),
    ]


def owner_scenario_en():
    return [
        _due("t_0000000a", "voz-hermes", 14 * D + 3 * H,
             _ask("Plug the ES3C28P into the laptop's USB port.", "The recorder only shows up with the board on USB.",
                  'reply "connected"', next_step="backup and recording."),
             held=["t_00000001", "t_00000002"]),
        _due("t_0000000b", "portal-imobiliario", 11 * D + 5 * H,
             _ask("Test the conversation on your phone.", "The pitch and the screenshots were already sent.",
                  "Reply “validated” or tell me what bothered you"),
             held=["t_00000003"], board_name="Portal Imobiliário"),
        _due("t_0000000c", "matrix", 15 * H + 20 * 60,
             _ask("Choose how to test the memory recording.",
                  "The 08:00 run failed with HTTP 400 from the provider (t_1a2b3c4d). One full round is still unchecked.",
                  "reply with the letter",
                  options=[("A", "Run it now"), ("B", "Wait for the next run"), ("C", "Do not rebuild the ranking")],
                  subject="Money Hunter"),
             held=["t_00000004", "t_00000005", "t_00000006", "t_00000007", "t_00000008"]),
    ]


SEP = "-" * 38

EXPECTED_PT = "\n".join([
    "📋 KANBAN · PRECISA DE VOCÊ",
    "3 pendências · 8 cards bloqueados",
    "",
    "① VOZ HERMES",
    "⏳ Parado há 14 dias · bloqueia 2 cards",
    "",
    "👉 Conecte a ES3C28P ao USB do notebook.",
    "Depois, responda: “conectei”.",
    "",
    "Próximo passo: backup e gravação.",
    "",
    SEP,
    "② PORTAL IMOBILIÁRIO",
    "⏳ Parado há 11 dias · bloqueia 1 card",
    "",
    "👉 Teste a conversa pelo celular.",
    "Responda “validado” ou diga o que incomodou.",
    "",
    "O pitch e os prints já foram enviados.",
    "",
    SEP,
    "③ MATRIX · MONEY HUNTER",
    "⏳ Parado há 15 horas · bloqueia 5 cards",
    "",
    "👉 Escolha como testar a gravação de memórias:",
    "",
    "A — Rodar agora",
    "B — Aguardar a próxima execução",
    "C — Não recriar o ranking",
    "",
    "A execução das 08h falhou com erro HTTP 400 do provedor. Falta validar uma rodada completa.",
    "",
    "↩️ Ao responder, indique o item:",
    "“1 — conectei”",
    "“2 — validado”",
    "“3 — A”",
])

EXPECTED_EN = "\n".join([
    "📋 KANBAN · NEEDS YOU",
    "3 pending · 8 blocked cards",
    "",
    "① VOZ HERMES",
    "⏳ Stalled for 14 days · blocks 2 cards",
    "",
    "👉 Plug the ES3C28P into the laptop's USB port.",
    "Then reply: “connected”.",
    "",
    "Next step: backup and recording.",
    "",
    SEP,
    "② PORTAL IMOBILIÁRIO",
    "⏳ Stalled for 11 days · blocks 1 card",
    "",
    "👉 Test the conversation on your phone.",
    "Reply “validated” or tell me what bothered you.",
    "",
    "The pitch and the screenshots were already sent.",
    "",
    SEP,
    "③ MATRIX · MONEY HUNTER",
    "⏳ Stalled for 15 hours · blocks 5 cards",
    "",
    "👉 Choose how to test the memory recording:",
    "",
    "A — Run it now",
    "B — Wait for the next run",
    "C — Do not rebuild the ranking",
    "",
    "The 08:00 run failed with HTTP 400 from the provider. One full round is still unchecked.",
    "",
    "↩️ When replying, name the item:",
    "“1 — connected”",
    "“2 — validated”",
    "“3 — A”",
])


@pytest.fixture
def pt(kanban_home, monkeypatch):
    """The Hermes language id "pt" (what Hermes itself supports) with the shipped pt pack."""
    from agent import i18n, i18n_layers

    entries = [
        i18n_layers.register_pack("en", "core", i18n_layers.parse_locale_file(ROOT / "en.yaml"), source="test"),
        i18n_layers.register_pack("pt", "core", i18n_layers.parse_locale_file(ROOT / "pt.yaml"), source="test"),
    ]
    monkeypatch.setenv("HERMES_LANGUAGE", "pt")
    i18n.reset_language_cache()
    yield
    for e in entries:
        i18n_layers.unregister_pack(e)
    monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
    i18n.reset_language_cache()


# -- §2 reminder layout --------------------------------------------------------------
def test_owner_scenario_renders_the_reference_layout_in_pt(pt_br):  # noqa: F811
    from ke.messages import render_waits

    assert render_waits("sponsor", owner_scenario_pt()) == EXPECTED_PT


def test_owner_scenario_renders_the_reference_layout_in_pt_language_id(pt):
    from ke.messages import render_waits

    assert render_waits("sponsor", owner_scenario_pt()) == EXPECTED_PT


def test_owner_scenario_renders_in_en():
    from ke.messages import render_waits

    assert render_waits("sponsor", owner_scenario_en()) == EXPECTED_EN


def test_single_item_has_no_number_no_separator_no_footer(pt_br):  # noqa: F811
    from ke.messages import render_waits

    txt = render_waits("sponsor", owner_scenario_pt()[:1])
    assert txt == "\n".join([
        "📋 KANBAN · PRECISA DE VOCÊ",
        "1 pendência · 2 cards bloqueados",
        "",
        "VOZ HERMES",
        "⏳ Parado há 14 dias · bloqueia 2 cards",
        "",
        "👉 Conecte a ES3C28P ao USB do notebook.",
        "Depois, responda: “conectei”.",
        "",
        "Próximo passo: backup e gravação.",
    ])


def test_single_item_en():
    from ke.messages import render_waits

    txt = render_waits("sponsor", owner_scenario_en()[1:2])
    assert txt.splitlines()[:5] == [
        "📋 KANBAN · NEEDS YOU", "1 pending · 1 blocked card", "", "PORTAL IMOBILIÁRIO",
        "⏳ Stalled for 11 days · blocks 1 card",
    ]
    assert "↩️" not in txt and SEP not in txt


def test_status_line_omits_blocks_when_nothing_is_held_and_singular_forms():
    from ke.messages import render_waits

    due = _due("t_0000000a", "voz", 40 * 60, _ask("Do it.", "w", "reply done"))
    lines = render_waits("s", [due]).splitlines()
    assert lines[1] == "1 pending"  # no "· 0 blocked cards" when nothing is held
    assert lines[4] == "⏳ Stalled for 40 min"
    one_hour = render_waits("s", [_due("t_0000000a", "voz", H + 59, _ask("Do it.", "w", "reply done"))])
    assert "⏳ Stalled for 1 hour" in one_hour
    one_day = render_waits("s", [_due("t_0000000a", "voz", D + 5, _ask("Do it.", "w", "reply done"))])
    assert "⏳ Stalled for 1 day" in one_day


def test_held_count_is_distinct_across_items():
    from ke.messages import render_waits

    a = _due("t_0000000a", "b", H, _ask("x.", "y", "reply done"), held=["t_00000001", "t_00000002"])
    b = _due("t_0000000b", "b", H, _ask("x.", "y", "reply done"), held=["t_00000002"])
    assert render_waits("s", [a, b]).splitlines()[1] == "2 pending · 2 blocked cards"


def test_circled_numbers_fall_back_after_twenty():
    from ke.messages import circled

    assert circled(1) == "①" and circled(20) == "⑳" and circled(21) == "(21)"


def test_ids_hashes_and_cli_never_reach_telegram():
    from ke.messages import render_waits

    ask = _ask(
        "Approve the deploy t_deadbeef 9f8e7d6c5b now. Then check it. And a third sentence.",
        "Deploy history: abc1234ef was reverted; run hermes kanban-empowered explain t_deadbeef for more.",
        "reply done",
    )
    txt = render_waits("s", [_due("t_cafebabe", "b", H, ask, held=["t_12345678"])])
    for bad in ("t_deadbeef", "t_cafebabe", "t_12345678", "9f8e7d6c5b", "abc1234ef", "hermes kanban", "third sentence"):
        assert bad not in txt, bad
    assert "👉 Approve the deploy now. Then check it." in txt


def test_no_urgent_marker_anywhere(pt_br):  # noqa: F811
    from ke.messages import render_waits, render_waits_chunks

    esc = [_due("t_0000000a", "b", 9 * H, _ask("x.", "y", "responda feito"), stage="escalation")]
    dig = [_due("t_0000000a", "b", 40 * H, _ask("x.", "y", "responda feito"), stage="digest")]
    for dues in (esc, dig):
        for text, _ in render_waits_chunks("sponsor", dues):
            assert "urgent" not in text.lower() and "🚨" not in text and "🔴" not in text
    assert render_waits("s", esc).splitlines()[0] == "📋 KANBAN · PRECISA DE VOCÊ"


def test_role_names_are_never_rendered(pt_br):  # noqa: F811
    from ke.messages import render_waits, render_waits_chunks

    dues = owner_scenario_pt()
    assert "sponsor" not in render_waits("sponsor", dues)
    ((copy, _),) = render_waits_chunks("sponsor", dues, lead="", copy=True)
    assert "sponsor" not in copy


def test_escalation_copy_has_its_lead_and_no_reply_footer(pt_br):  # noqa: F811
    from ke.messages import render_waits_chunks

    ((copy, _),) = render_waits_chunks("sponsor", owner_scenario_pt(), copy=True)
    lines = copy.splitlines()
    assert lines[0] == "[Kanban - Empowered]"
    assert lines[1] == "Cópia para acompanhamento"
    assert lines[2] == "📋 KANBAN · PRECISA DE VOCÊ"
    assert "↩️" not in copy and "responda" not in copy.lower()


def test_old_format_ask_and_raw_reason_still_render():
    from ke.asks import Ask
    from ke.messages import render_waits

    old = _due("t_0000000a", "b", H, Ask("ligue a placa", "o teste roda com ela", "responda feito"))
    txt = render_waits("s", [old])
    assert "👉 ligue a placa" in txt and "Then reply: “feito”." in txt
    raw = _due("t_0000000a", "b", H, None, reason="aguardando a placa t_0000beef")
    txt = render_waits("s", [raw])
    assert "👉 aguardando a placa" in txt and "t_0000beef" not in txt and "Reply to this message when it's done, or say what's missing." in txt


def test_chunks_repeat_the_header_and_number_from_one():
    from ke.messages import _tg_len, render_waits_chunks

    dues = [_due(f"t_{i:08x}", "b", H, _ask("x" * 150 + ". " + "y" * 150 + ".", "w" * 190, "reply done"),
                 board_name="B") for i in range(20)]
    chunks = render_waits_chunks("s", dues, limit=1500)
    assert len(chunks) > 1
    for text, items in chunks:
        assert _tg_len(text) <= 1500
        assert text.startswith("[Kanban - Empowered]\n📋 KANBAN · NEEDS YOU\n")
        if len(items) > 1:
            assert "① B" in text and "↩️ When replying, name the item:" in text


# -- §4 structured ask ------------------------------------------------------------------
@pytest.mark.parametrize("sep", ["—", "–", "-", ")", ":"])
def test_parse_ask_options_subject_next_step(sep):
    from ke.asks import parse_ask

    a = parse_ask(
        "Preciso: escolha como testar\nPor quê: falhou\nComo responder: a letra\nAssunto: Money Hunter\n"
        f"Próximo passo: rodar de novo\nOpções:\nA {sep} Rodar agora\nb {sep} Aguardar\n\nC{sep} Não recriar"
    )
    assert a.options == (("A", "Rodar agora"), ("B", "Aguardar"), ("C", "Não recriar"))
    assert a.subject == "Money Hunter" and a.next_step == "rodar de novo"
    assert a.option("b") == "Aguardar" and a.option("D") is None


def test_parse_ask_english_optional_labels_and_old_asks_unchanged():
    from ke.asks import Ask, parse_ask

    a = parse_ask("Need: pick\nWhy: w\nReply: the letter\nSubject: S\nNext step: N\nOptions: A — one\nB) two")
    assert (a.subject, a.next_step, a.options) == ("S", "N", (("A", "one"), ("B", "two")))
    assert parse_ask("Need: a\nWhy: b\nHow to reply: c") == Ask("a", "b", "c")


@pytest.mark.parametrize(
    "reply,words",
    [
        ('responda "conectei"', ["conectei"]),
        ("responda “conectei”", ["conectei"]),
        ("responda feito", ["feito"]),
        ("reply done / approved", ["done", "approved"]),
        ("validado, ou diga o que incomodou", ["validado"]),
        ("Responda “validado” ou diga o que incomodou", ["validado"]),
        ("liguei ou feito", ["liguei", "feito"]),
        ("say done or ok", ["done", "ok"]),
    ],
)
def test_reply_words(reply, words):
    from ke.asks import reply_words

    assert reply_words(_ask("n", "w", reply)) == words


def test_guard_template_shows_the_optional_fields(pt_br):  # noqa: F811
    from ke import messages
    from ke.asks import ask_template

    for label in ("Subject:", "Next step:", "Options:"):
        assert label in messages.EN["guard.template"]
    pt_template = ask_template()
    for label in ("Assunto:", "Próximo passo:", "Opções:"):
        assert label in pt_template


# -- §1 language -----------------------------------------------------------------------
def test_catalog_parity_across_all_shipped_catalogs():
    import string

    from agent.i18n_layers import parse_locale_file
    from ke import messages

    en_code = {messages.PREFIX + k: v for k, v in messages.EN.items()}
    cats = {name: parse_locale_file(ROOT / f"{name}.yaml") for name in ("en", "pt", "pt-br")}
    assert set(en_code) == set(cats["en"]) == set(cats["pt"]) == set(cats["pt-br"])
    assert en_code == cats["en"]
    assert cats["pt"] == cats["pt-br"]

    def fields(text):
        return {f for _, f, _, _ in string.Formatter().parse(text) if f}

    for key, text in en_code.items():
        assert fields(cats["pt"][key]) == fields(text), key


def test_plugin_yaml_declares_pt():
    import yaml

    data = yaml.safe_load((ROOT.parent / "plugin.yaml").read_text(encoding="utf-8"))
    ids = [(e["id"] if isinstance(e, dict) else e) for e in data["provides_locales"]]
    assert ids == ["en", "pt", "pt-br"]


def test_language_setting_overrides_the_hermes_language(kanban_home, monkeypatch):
    from agent import i18n, i18n_layers
    from ke import messages
    from ke.settings import Settings

    assert Settings.from_mapping({"language": "pt"}).language == "pt"
    assert Settings.from_mapping({}).language is None
    entries = [
        i18n_layers.register_pack("en", "core", i18n_layers.parse_locale_file(ROOT / "en.yaml"), source="test"),
        i18n_layers.register_pack("pt", "core", i18n_layers.parse_locale_file(ROOT / "pt.yaml"), source="test"),
    ]
    monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
    i18n.reset_language_cache()
    try:
        assert messages.t("waits.header") == "📋 KANBAN · NEEDS YOU"  # Hermes language: English
        messages.set_language("pt")
        assert messages.t("waits.header") == "📋 KANBAN · PRECISA DE VOCÊ"
        messages.set_language(None)
        assert messages.t("waits.header") == "📋 KANBAN · NEEDS YOU"
    finally:
        messages.set_language(None)
        for e in entries:
            i18n_layers.unregister_pack(e)
        i18n.reset_language_cache()


def test_language_setting_is_applied_by_register(kanban_home, tmp_path, load_plugin):
    import sys

    from test_doctor import _Ctx

    mod = load_plugin()
    mod.register(_Ctx(tmp_path / "state", config={"language": "pt"}))
    messages = sys.modules[mod.__name__ + ".ke.messages"]
    try:
        assert messages._LANG == "pt"
    finally:
        messages.set_language(None)


# -- §3 config alerts --------------------------------------------------------------------
def test_veto_alert_is_a_separate_setup_message_sent_once(tmp_path, fake_tg, clock):
    c = make_coordinator(tmp_path, clock=clock, veto_hooks=False, telegram=fake_tg, owner_ids={"sponsor": 111})
    c.evaluate(1.0)
    c.evaluate(100.0)
    texts = [m["text"] for m in fake_tg.calls["send"]]
    assert len(texts) == 1 and texts[0].startswith("[Kanban - Empowered]\n⚙️ KANBAN · SETUP\n")


def test_escalate_to_without_chat_id_is_a_setup_alert_sent_once(tmp_path, fake_tg, clock):
    c = make_coordinator(tmp_path, clock=clock, telegram=fake_tg, owner_ids={"sponsor": 111}, escalate_to="boss")
    c.evaluate(1.0)
    c.evaluate(100.0)
    texts = [m["text"] for m in fake_tg.calls["send"]]
    assert len(texts) == 1 and texts[0].startswith("[Kanban - Empowered]\n⚙️ KANBAN · SETUP\n")
    assert "boss" not in texts[0]
    assert fake_tg.calls["send"][0]["chat_id"] == 111


def test_setup_alert_never_opens_a_reminder(tmp_path, fake_tg, clock):
    c = make_coordinator(tmp_path, clock=clock, veto_hooks=False, telegram=fake_tg,
                         owner_ids={"sponsor": 111}, escalate_to="boss")
    c.pending_dues = [_due("t_0000000a", "b", 9 * H, _ask("x.", "y", "reply done"), stage="escalation")]
    c._deliver_waits(1.0)
    c.evaluate(2.0)
    reminders = [m["text"] for m in fake_tg.calls["send"] if "NEEDS YOU" in m["text"]]
    assert reminders and all(r.startswith("[Kanban - Empowered]\n📋 KANBAN · NEEDS YOU") for r in reminders)
    assert all("⚙️" not in r for r in reminders)


def test_owner_without_chat_id_is_only_logged(tmp_path, fake_tg, clock, caplog):
    c = make_coordinator(tmp_path, clock=clock, veto_hooks=False, telegram=fake_tg, owner_ids={})
    c.evaluate(1.0)
    assert fake_tg.count("send") == 0


def test_setup_alert_is_pt_when_configured(tmp_path, fake_tg, clock, pt_br):  # noqa: F811
    c = make_coordinator(tmp_path, clock=clock, veto_hooks=False, telegram=fake_tg, owner_ids={"sponsor": 111})
    c.evaluate(1.0)
    assert fake_tg.last_text.startswith("[Kanban - Empowered]\n⚙️ KANBAN · CONFIGURAÇÃO\n")


# -- §5 item-linked replies ---------------------------------------------------------------
PT_ASKS = [
    'Preciso: Conecte a ES3C28P ao USB do notebook.\nPor quê: o gravador só aparece assim.\n'
    'Como responder: responda "conectei"',
    "Preciso: Teste a conversa pelo celular.\nPor quê: o pitch já foi enviado.\n"
    "Como responder: Responda “validado” ou diga o que incomodou",
    "Preciso: Escolha como testar a gravação.\nPor quê: a execução falhou.\nComo responder: a letra\n"
    "Opções:\nA — Rodar agora\nB — Aguardar a próxima execução\nC — Não recriar o ranking",
]


class Rig:
    def __init__(self, tmp_path, fake_tg, clock, reasons):
        from ke import hermes_api
        from ke.store import Store
        from ke.waits import WaitTracker

        self.settings = settings_for_test(tmp_path, owner_ids={"sponsor": int(OWNER), "boss": int(BOSS)},
                                          escalate_to="boss")
        self.store = Store(self.settings.shared_state_dir / "state.db")
        self.tg, self.clock = fake_tg, clock
        self.tids, items = [], []
        with hermes_api.connect("default") as conn:
            for i, reason in enumerate(reasons):
                tid, ev = make_human_block(conn, reason=reason, title=f"card {i}")
                self.tids.append(tid)
                items.append({"board": "default", "task_id": tid, "version": int(ev.id)})
            self.held = create_task(conn, title="held", parents=[self.tids[0]])
        clock.now = float(ev.created_at) + 1800
        WaitTracker(self.settings, self.store, hermes_api).sync(clock.now)
        self.sent_id = 4343
        self.store.add_sent_message(self.sent_id, int(OWNER), "waits", items, int(clock.now))

    def reply(self, text, user=OWNER, message_id=None):
        from ke import hermes_api
        from ke.inbound import handle_inbound

        kw = {"message_id": message_id} if message_id is not None else {}
        ev = reply_event(user_id=user, reply_to=self.sent_id, text=text, **kw)
        return handle_inbound(ev, self.settings, self.store, hermes_api, self.tg, self.clock.now)


@pytest.fixture
def rig3(kanban_home, tmp_path, fake_tg, clock):
    return Rig(tmp_path, fake_tg, clock, PT_ASKS)


@pytest.fixture
def rig1(kanban_home, tmp_path, fake_tg, clock):
    return Rig(tmp_path, fake_tg, clock, PT_ASKS[:1])


@pytest.mark.parametrize("text", ["1 — conectei", "1 – conectei", "1 - conectei", "1: conectei", "1) conectei",
                                  "1. conectei", "1 conectei", "1 — Conectei!", "1 — feito ✅", "1 — ok"])
def test_a_confirmation_linked_to_an_item_resolves_it(rig3, text):
    assert rig3.reply(text)["action"] == "skip"
    assert _status(rig3.tids[0]) != "blocked"
    assert _status(rig3.tids[1]) == "blocked" and _status(rig3.tids[2]) == "blocked"
    reply = rig3.tg.last_text
    assert "t_" not in reply and "DEFAULT" in reply


def test_free_text_linked_to_an_item_is_the_answer(rig3):
    assert rig3.reply("2 — o campo ficou pequeno", message_id="77")["action"] == "skip"
    assert _status(rig3.tids[1]) != "blocked"
    assert _status(rig3.tids[0]) == "blocked" and _status(rig3.tids[2]) == "blocked"
    assert "o campo ficou pequeno" in _comments(rig3.tids[1])[-1]
    assert "t_" not in rig3.tg.last_text


def test_free_text_comment_is_clipped_to_1000(rig3):
    rig3.reply("2 — " + "z" * 3000)
    body = _comments(rig3.tids[1])[-1]
    assert "z" * 990 in body and "z" * 1001 not in body


def test_a_declared_option_letter_resolves_with_the_choice(rig3):
    assert rig3.reply("3 — A")["action"] == "skip"
    assert _status(rig3.tids[2]) != "blocked"
    assert "choice: A — Rodar agora" in _comments(rig3.tids[2])[-1]
    assert "Rodar agora" in rig3.tg.last_text


@pytest.mark.parametrize("text", ["3 — b.", "3 b", "3: C!"])
def test_option_letter_forms(rig3, text):
    rig3.reply(text)
    assert _status(rig3.tids[2]) != "blocked"


def test_an_undeclared_letter_is_ambiguous_and_changes_nothing(rig3):
    assert rig3.reply("3 — D")["action"] == "skip"
    assert [_status(t) for t in rig3.tids] == ["blocked"] * 3
    assert "D" in rig3.tg.last_text and "A, B, C" in rig3.tg.last_text
    assert not any("D" == c.split(":")[-1].strip() for c in _comments(rig3.tids[2]))


def test_a_negation_only_comments_and_keeps_waiting(rig3):
    assert rig3.reply("1 — ainda não")["action"] == "skip"
    assert _status(rig3.tids[0]) == "blocked"
    assert "ainda não" in _comments(rig3.tids[0])[-1]
    assert unbranded(rig3.tg.last_text) == "Noted; the item is still waiting."
    assert not any(a["kind"] == "human_done" for a in rig3.store.actions_since(0))


@pytest.mark.parametrize("text", ["1 — não", "1 nao, falta o cabo", "1 — negativo", "1 — not yet", "1 no", "1 — nope"])
def test_negation_forms(rig3, text):
    rig3.reply(text)
    assert _status(rig3.tids[0]) == "blocked"


def test_noted_reply_in_pt(rig3, pt_br):  # noqa: F811
    rig3.reply("1 — ainda não")
    assert unbranded(rig3.tg.last_text) == "Anotado; o item continua esperando."


def test_multi_item_message_without_number_asks_which(rig3):
    assert rig3.reply("o campo ficou pequeno")["action"] == "skip"
    assert [_status(t) for t in rig3.tids] == ["blocked"] * 3
    assert "which item" in rig3.tg.last_text.lower()


def test_single_item_message_accepts_bare_text(rig1):
    assert rig1.reply("conectei")["action"] == "skip"
    assert _status(rig1.tids[0]) != "blocked"


def test_single_item_message_bare_free_text_is_the_answer(rig1):
    rig1.reply("a placa não liga, comprei outra")
    assert _status(rig1.tids[0]) != "blocked"
    assert "a placa não liga, comprei outra" in _comments(rig1.tids[0])[-1]


def test_non_owner_free_text_still_passes_through_untouched(rig3):
    assert rig3.reply("2 — o campo ficou pequeno", user=STRANGER, message_id="88") is None
    assert rig3.reply("3 — A", user=BOSS) is None
    assert [_status(t) for t in rig3.tids] == ["blocked"] * 3
    assert rig3.tg.count("send") == 0
    assert rig3.store.mark_update(f"tg:{OWNER}:88", int(rig3.clock.now)) is True  # not consumed


def test_resolved_confirmation_names_board_and_action_without_ids(rig3, pt_br):  # noqa: F811
    rig3.reply("1 — conectei")
    text = rig3.tg.last_text
    assert "DEFAULT" in text and "Conecte a ES3C28P ao USB do notebook" in text
    assert "t_" not in text and "1 card" in text


def test_free_text_reply_to_an_unknown_message_passes_through(rig3):
    from ke import hermes_api
    from ke.inbound import handle_inbound

    ev = reply_event(user_id=OWNER, reply_to=1, text="1 — conectei")
    assert handle_inbound(ev, rig3.settings, rig3.store, hermes_api, rig3.tg, rig3.clock.now) is None


def test_a_reply_without_text_passes_through(rig1):
    assert rig1.reply("   ") is None
    assert _status(rig1.tids[0]) == "blocked"


# -- fix round 1 ----------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["1 — No it still fails", "1 no I couldn't", "1 — not done yet", "1 — never"])
def test_english_negations_keep_the_card_waiting(rig3, text):
    assert rig3.reply(text)["action"] == "skip"
    assert _status(rig3.tids[0]) == "blocked"
    assert unbranded(rig3.tg.last_text) == "Noted; the item is still waiting."


def test_portuguese_no_at_the_start_is_not_a_negation(rig3, pt_br):  # noqa: F811
    rig3.reply("1 — no celular funcionou")
    assert _status(rig3.tids[0]) != "blocked"  # "no celular" = "on the phone": an answer
    assert "no celular funcionou" in _comments(rig3.tids[0])[-1]


def test_the_comment_has_no_item_number(rig3):
    rig3.reply("2 — o campo ficou pequeno")
    body = _comments(rig3.tids[1])[-1]
    assert body.endswith(": o campo ficou pequeno") and "2 —" not in body


def test_a_bare_confirmation_on_an_option_item_asks_for_a_letter(rig3):
    assert rig3.reply("3 — ok")["action"] == "skip"
    assert _status(rig3.tids[2]) == "blocked"
    assert "A, B, C" in rig3.tg.last_text


def test_a_decimal_is_not_an_item_number(rig3):
    assert rig3.reply("1.5 GB faltam")["action"] == "skip"
    assert [_status(t) for t in rig3.tids] == ["blocked"] * 3
    assert "which item" in rig3.tg.last_text.lower()


def test_a_single_item_reminder_takes_other_leading_numbers_as_text(rig1):
    rig1.reply("2 cabos faltando")
    assert _status(rig1.tids[0]) != "blocked"
    assert "2 cabos faltando" in _comments(rig1.tids[0])[-1]


def test_cli_stripping_leaves_prose_alone():
    from ke.messages import strip_ids

    assert strip_ids("Abra o Hermes Kanban e aprove o card.") == "Abra o Hermes Kanban e aprove o card."
    assert strip_ids("Aprove; rode hermes kanban-empowered done t_deadbeef. Depois avise.") == "Aprove. Depois avise."
    assert strip_ids("veja hermes kanban show t_deadbeef") == "veja"


def test_a_need_that_was_only_a_command_falls_back_to_no_reason():
    from ke.messages import render_waits

    due = _due("t_0000000a", "b", H, None, reason="hermes kanban-empowered explain t_deadbeef")
    assert "👉 (no reason given)" in render_waits("s", [due])


def test_uppercase_hashes_and_uuids_are_stripped_plain_words_kept():
    from ke.messages import strip_ids

    assert strip_ids("build 9F8E7D6C5B ok") == "build ok"
    assert strip_ids("run 123e4567-e89b-12d3-a456-426614174000 failed") == "run failed"
    assert strip_ids("defaced effaced DEADBEEF 1234567") == "defaced effaced DEADBEEF 1234567"


def test_role_names_in_agent_text_become_you(pt_br):  # noqa: F811
    from ke import messages

    messages.set_roles(["sponsor", "ops"])
    try:
        ask = _ask("Aguardando o sponsor aprovar.", "OPS pediu isso.", "responda feito", next_step="avisar o Sponsor")
        txt = messages.render_waits("sponsor", [_due("t_0000000a", "b", H, ask)])
        assert "sponsor" not in txt.lower() and "ops" not in txt.lower()
        assert "👉 Aguardando você aprovar." in txt and "Próximo passo: avisar você" in txt
    finally:
        messages.set_roles(())


def test_register_hands_the_role_names_over(kanban_home, tmp_path, load_plugin):
    import sys

    from test_doctor import _Ctx

    mod = load_plugin()
    mod.register(_Ctx(tmp_path / "state", config={"telegram.owner_ids": {"ana": 1}, "waits.escalate_to": "bob"}))
    messages = sys.modules[mod.__name__ + ".ke.messages"]
    assert messages._ROLES == {"ana", "bob"}


@pytest.mark.parametrize("reply,words", [("responda com a letra", []), ("diga o que achou", []),
                                         ("liguei e testei", ["liguei"])])
def test_reply_words_fallback_needs_a_plausible_word(reply, words):
    from ke.asks import reply_words

    assert reply_words(_ask("n", "w", reply)) == words


def test_rich_reply_without_a_word_is_rendered_as_written():
    from ke.messages import render_waits

    txt = render_waits("s", [_due("t_0000000a", "b", H, _ask("Olhe o layout.", "w", "diga o que achou"))])
    assert "Diga o que achou." in txt and "“1 —" not in txt


def test_unsupported_language_setting_follows_hermes(kanban_home, caplog):
    from ke import messages

    with caplog.at_level("WARNING"):
        messages.set_language("klingon")
    try:
        assert messages._LANG is None
        assert any("klingon" in r.getMessage() for r in caplog.records)
        messages.set_language("pt-BR")
        assert messages._LANG in ("pt", "pt-br")
    finally:
        messages.set_language(None)


# -- fix round 2 ----------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["3 — A/B", "3 — A ou B", "3 — a, b", "3 — A or C", "3 — B e C"])
def test_several_letters_are_ambiguous_and_change_nothing(rig3, text):
    assert rig3.reply(text)["action"] == "skip"
    assert _status(rig3.tids[2]) == "blocked"
    assert "A, B, C" in rig3.tg.last_text


def test_letters_on_an_item_without_options_are_an_answer(rig3):
    rig3.reply("2 — A ou B")
    assert _status(rig3.tids[1]) != "blocked"


@pytest.mark.parametrize("raw,expected", [
    ("Aguardando resposta do sponsor.", "Aguardando resposta de você."),
    ("Depende da sponsor.", "Depende de você."),
    ("Está no sponsor agora.", "Está em você agora."),
    ("Validado pelo sponsor.", "Validado por você."),
    ("Mande ao sponsor.", "Mande a você."),
    ("Mostre às sponsor.", "Mostre a você."),
    ("O sponsor precisa ligar.", "você precisa ligar."),
    ("Responda a sponsor.", "Responda a você."),
    ("Do sponsor depende tudo.", "De você depende tudo."),
])
def test_pt_contractions_before_a_role_name(pt_br, raw, expected):  # noqa: F811
    from ke import messages

    messages.set_roles(["sponsor"])
    try:
        assert messages.replace_roles(raw) == expected
    finally:
        messages.set_roles(())


def test_en_role_names_drop_the_but_not_pt_contractions(kanban_home):
    from ke import messages

    messages.set_roles(["sponsor"])
    try:
        assert messages.replace_roles("Waiting for the sponsor.") == "Waiting for you."
        assert messages.replace_roles("ask do sponsor") == "ask do you"  # pt contractions only in pt
    finally:
        messages.set_roles(())


def test_role_names_never_leak_through_the_choice_or_infra_titles(kanban_home):
    from ke import messages

    messages.set_roles(["sponsor"])
    try:
        answer = messages.render_answer("B", "x", 0, choice=("A", "Esperar o sponsor"))
        assert "sponsor" not in answer.lower()
        alert = messages.render_infra_alert(
            {"kind": "budget", "board": "b", "task_id": "t_1", "title": "Deploy for sponsor", "count": 2,
             "window_s": 3600})
        assert "sponsor" not in alert.lower()
        guards = messages.render_guards([{"board": "b", "task_id": "t_1", "reason": "active_pr", "age_s": 60,
                                          "title": "Ask the sponsor", "assignee": "dev"}])
        assert "sponsor" not in guards.lower()
    finally:
        messages.set_roles(())
