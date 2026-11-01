"""Brand prefix (owner request 01/11/2026): EVERY Telegram message starts with "[Kanban - Empowered]" + newline."""
from __future__ import annotations

import pathlib

from conftest import make_coordinator
from test_msg_format import H, owner_scenario_pt, _ask, _due
from test_waits import pt_br  # noqa: F401 (fixture)

BRAND = "[Kanban - Empowered]"
ROOT = pathlib.Path(__file__).resolve().parent.parent / "locales"


def test_brand_is_one_constant_never_in_the_catalogs():
    from ke import messages

    assert messages.BRAND == BRAND
    assert messages.branded("oi") == BRAND + "\noi"
    assert messages.branded(messages.branded("oi")) == BRAND + "\noi"  # idempotent: never doubled
    assert not any(BRAND in str(v) or "Kanban - Empowered" in str(v) for v in messages.EN.values())
    for f in ROOT.glob("*.yaml"):
        assert "Kanban - Empowered" not in f.read_text(encoding="utf-8"), f.name


def test_waits_chunks_all_start_with_the_brand_and_count_toward_the_limit():
    from ke.messages import _tg_len, render_waits_chunks

    dues = [_due(f"t_{i:08x}", "b", H, _ask("x" * 150 + ". " + "y" * 150 + ".", "w" * 190, "reply done"),
                 board_name="B") for i in range(20)]
    for limit in (1500, 4096):
        chunks = render_waits_chunks("s", dues, limit=limit)
        assert chunks
        for text, _items in chunks:
            assert text.startswith(BRAND + "\n📋 KANBAN · NEEDS YOU\n")
            assert _tg_len(text) <= limit
    assert len(render_waits_chunks("s", dues, limit=1500)) > 1


def test_a_single_oversized_item_keeps_the_brand_and_fits():
    from ke.messages import _tg_len, render_waits_chunks

    due = _due("t_0000000a", "b", H, _ask("x" * 300, "y" * 6000, "reply done"))
    [(text, items)] = render_waits_chunks("s", [due])
    assert text.startswith(BRAND + "\n") and _tg_len(text) <= 4096 and items == [due]


def test_escalation_copy_and_lead_start_with_the_brand():
    from ke.messages import render_waits_chunks

    dues = owner_scenario_pt()[:1]
    [(copy, _)] = render_waits_chunks("s", dues, copy=True)
    assert copy.startswith(BRAND + "\n")
    [(led, _)] = render_waits_chunks("s", dues, lead="LEAD")
    assert led.startswith(BRAND + "\nLEAD\n")


def test_guard_chunks_all_start_with_the_brand_within_the_limit():
    from ke.messages import TELEGRAM_LIMIT, _tg_len, render_guard_chunks

    alerts = [
        {"board": "b", "task_id": f"t_{i:08x}", "reason": "active_pr", "age_s": 2000,
         "title": "x" * 300, "assignee": "dev", "pr_url": "https://github.com/o/r/pull/1" + "0" * 200,
         "pr_at": 1_700_000_000}
        for i in range(40)
    ]
    chunks = render_guard_chunks(alerts)
    assert len(chunks) > 1
    for text, _ in chunks:
        assert text.startswith(BRAND + "\n") and _tg_len(text) <= TELEGRAM_LIMIT
    assert [a for _, items in chunks for a in items] == alerts


def test_panel_starts_with_the_brand_and_fits_when_clipped():
    from ke.messages import TELEGRAM_LIMIT, _tg_len
    from ke.panel import panel_items, render_telegram, shown_waits
    from test_panel import _item, snapshot_fixture

    snap = snapshot_fixture(human_waits=[_item(i, need="x" * 400, age_s=1000 + i) for i in range(1, 60)])
    text = render_telegram(snap)
    assert text.startswith(BRAND + "\n") and _tg_len(text) <= TELEGRAM_LIMIT
    assert len(panel_items(snap)) == len(shown_waits(snap)) > 0
    assert render_telegram(snapshot_fixture()).startswith(BRAND + "\n")


def test_alert_renderers_and_sending_layer_brand_everything(tmp_path, fake_tg):
    """The sending layer is the safety net: even a raw, unbranded text goes out branded, once."""
    from ke import inbound
    from ke.messages import render_answer, render_config_alert, render_infra_alert

    c = make_coordinator(tmp_path, telegram=fake_tg)
    c._tg_send(1, "plain")
    c._tg_send(1, render_config_alert("veto"))
    c._tg_send(1, render_infra_alert({"kind": "systemic", "n": 3, "window_s": 600}))
    c._tg_call("edit", 1, 5, "plain")
    inbound._reply(fake_tg, 1, 9, "plain")
    inbound._reply(fake_tg, 1, 9, render_answer("B", "x", 0))
    texts = [call["text"] for call in fake_tg.calls["send"] + fake_tg.calls["edit"]]
    assert len(texts) == 6
    for text in texts:
        assert text.startswith(BRAND + "\n") and not text.startswith(BRAND + "\n" + BRAND)
    assert fake_tg.calls["send"][0]["text"] == BRAND + "\nplain"
    assert fake_tg.calls["send"][-1]["reply_to"] == 9


def test_sample_pt_reminder_first_line_is_the_brand(pt_br):
    from ke.messages import render_waits_chunks

    [(text, _)] = render_waits_chunks("s", owner_scenario_pt()[:1])
    assert text.splitlines()[0] == BRAND and text.splitlines()[1].startswith("📋")
