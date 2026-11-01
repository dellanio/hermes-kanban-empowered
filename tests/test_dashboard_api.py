"""The "Empowered" dashboard backend (``dashboard/plugin_api.py``) through FastAPI's TestClient.

The router is loaded the way Hermes' dashboard loads it (``spec_from_file_location`` under
``hermes_dashboard_plugin_<name>``), mounted at ``/api/plugins/kanban-empowered`` and driven against a
temporary ``HERMES_HOME`` (``kanban_home``) with real boards, cards and plugin state.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from conftest import create_task
from test_infra import gave_up_with_runs
from test_waits import make_human_block, pt_br  # noqa: F401 (pt_br is a fixture)

REPO = Path(__file__).resolve().parent.parent
API_FILE = REPO / "dashboard" / "plugin_api.py"
PREFIX = "/api/plugins/kanban-empowered"
MODULE = "hermes_dashboard_plugin_kanban-empowered"

ASK_OPTIONS = (
    "Preciso: Escolha como testar a gravação de memórias (t_1a2b3c4d).\n"
    "Por quê: A execução das 08h falhou com erro HTTP 400 do provedor.\n"
    "Como responder: responda com a letra\n"
    "Assunto: Money Hunter\n"
    "Opções:\n"
    "A — Rodar agora\n"
    "B — Aguardar a próxima execução\n"
)
ASK_WORD = (
    "Need: Plug the ES3C28P into the laptop's USB port.\n"
    "Why: The recorder only shows up with the board on USB.\n"
    'How to reply: reply "connected"\n'
    "Next step: backup and recording."
)


def _load_router_module():
    """Import plugin_api.py exactly like hermes_cli/web_server_dashboard.py does."""
    spec = importlib.util.spec_from_file_location(MODULE, API_FILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(MODULE, None)
        raise
    return mod


@pytest.fixture
def api_mod(kanban_home):
    mod = _load_router_module()
    yield mod
    mod.close_stores()
    sys.modules.pop(MODULE, None)


@pytest.fixture
def client(api_mod):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api_mod.router, prefix=PREFIX)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def store(kanban_home):
    """The plugin Store the backend reads (default shared_state_dir under the temp HERMES_HOME)."""
    from ke.settings import Settings
    from ke.store import Store

    s = Store(Settings.from_mapping({}).shared_state_dir / "state.db")
    yield s
    s.close()


def _conn(board="default"):
    from ke import hermes_api

    return hermes_api.connect(board)


def _status(tid, board="default"):
    from ke import hermes_api

    with _conn(board) as conn:
        return hermes_api.task(conn, tid).status


def _comments(tid, board="default"):
    from ke import hermes_api

    with _conn(board) as conn:
        return [c.body for c in hermes_api.comments(conn, tid)]


def _waiting(reason=ASK_WORD, board="default", held=0, **kw):
    """A card blocked on a person (+ ``held`` todo children behind it); returns (task id, block version)."""
    with _conn(board) as conn:
        tid, ev = make_human_block(conn, reason=reason, **kw)
        for i in range(held):
            create_task(conn, title=f"child {i}", parents=[tid])
    return tid, int(ev.id)


def _item(overview, tid):
    return next(i for i in overview["needs_you"] if i["task_id"] == tid)


# -- module shape -----------------------------------------------------------------------------
def test_dashboard_manifest_points_hermes_at_the_router():
    from hermes_cli.web_server_dashboard import _safe_plugin_api_relpath

    data = json.loads((REPO / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert data["name"] == "kanban-empowered" and data["api"] == "plugin_api.py"
    assert _safe_plugin_api_relpath(data["api"], dashboard_dir=REPO / "dashboard") == "plugin_api.py"


def test_plugin_api_imports_no_hermes_module_directly():
    """Only ke/hermes_api.py imports Hermes; the router may import FastAPI/pydantic and the stdlib."""
    import ast

    tree = ast.parse(API_FILE.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    assert names <= {"__future__", "fastapi", "pydantic", "importlib", "logging", "sys", "threading", "time",
                     "typing", "pathlib"}, names


def test_router_is_an_apirouter(api_mod):
    from fastapi import APIRouter

    assert isinstance(api_mod.router, APIRouter)
    paths = {r.path for r in api_mod.router.routes}
    assert {"/settings", "/i18n", "/overview", "/item", "/answer"} <= paths


# -- settings / i18n --------------------------------------------------------------------------
def test_settings_defaults(client):
    r = client.get(PREFIX + "/settings")
    assert r.status_code == 200
    assert r.json() == {"show_sidebar_menu": True, "show_kanban_button": True, "language": "en"}


def test_settings_follow_the_desktop_form(client, kanban_home):
    from hermes_cli.plugins_settings import save_plugin_settings

    save_plugin_settings("kanban-empowered", REPO, {"show_sidebar_menu": False, "show_kanban_button": False})
    body = client.get(PREFIX + "/settings").json()
    assert body["show_sidebar_menu"] is False and body["show_kanban_button"] is False


def test_i18n_returns_every_ui_key(client):
    from ke import messages

    body = client.get(PREFIX + "/i18n").json()
    ui = {k: v for k, v in messages.EN.items() if k.startswith("ui.")}
    assert len(ui) >= 40
    assert body["language"] == "en"
    assert body["strings"] == ui


def test_i18n_follows_the_hermes_language(client, pt_br):  # noqa: F811
    body = client.get(PREFIX + "/i18n").json()
    assert body["strings"]["ui.section.needs_you"] == "Precisa de você"
    assert body["strings"]["ui.metric.workers"] == "Workers ativos"


# -- overview ---------------------------------------------------------------------------------
def test_overview_empty(client):
    r = client.get(PREFIX + "/overview")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"generated_at", "metrics", "needs_you", "running", "timeline", "infra", "boards"}
    assert body["metrics"] == {"needs_you": 0, "impacted_cards": 0, "workers_running": 0, "incidents": 0}
    assert body["needs_you"] == [] and body["running"] == [] and body["timeline"] == []
    assert body["boards"] == ["default"]
    assert set(body["infra"]) >= {
        "recent_infra_exits_1h", "systemic_alert_active", "budget_exhausted", "telegram_path", "telegram_ok",
    }


def test_overview_item_carries_the_telegram_texts(client):
    from ke import messages
    from ke.asks import parse_ask

    tid, version = _waiting(ASK_OPTIONS, held=2, title="Gravação de memórias")
    item = _item(client.get(PREFIX + "/overview").json(), tid)
    ask = parse_ask(ASK_OPTIONS)
    assert item["kind"] == "human_wait" and item["board"] == "default" and item["version"] == version
    assert item["title"] == "Gravação de memórias"
    assert item["need"] == messages.short_action(ask, ASK_OPTIONS, "Gravação de memórias")
    assert "t_1a2b3c4d" not in item["need"]
    assert item["board_title"] == "DEFAULT · MONEY HUNTER"
    assert item["options"] == [{"letter": "A", "label": "Rodar agora"},
                               {"letter": "B", "label": "Aguardar a próxima execução"}]
    assert item["reply_hint"] == "A" and item["held_behind_count"] == 2
    assert item["why"] == "A execução das 08h falhou com erro HTTP 400 do provedor."
    assert item["age_s"] >= 0 and item["next_reminder_in_s"] > 0 and item["stage"] is None
    body = client.get(PREFIX + "/overview").json()
    assert body["metrics"]["needs_you"] == 1 and body["metrics"]["impacted_cards"] == 2


def test_overview_word_item_has_reply_line_and_next_step(client):
    tid, _ = _waiting(ASK_WORD)
    item = _item(client.get(PREFIX + "/overview").json(), tid)
    assert item["options"] == [] and item["reply_hint"] == "connected"
    assert item["next_step"] == "backup and recording."
    assert item["reply_line"] == "Then reply: “connected”."
    assert item["context"] == "Next step: backup and recording."


def test_overview_sorts_by_impact_then_age(client):
    a, _ = _waiting(ASK_WORD, title="old, blocks nothing")
    b, _ = _waiting(ASK_WORD, held=1, title="blocks one")
    c, _ = _waiting(ASK_WORD, held=3, title="blocks three")
    order = [i["task_id"] for i in client.get(PREFIX + "/overview").json()["needs_you"]]
    assert order == [c, b, a]


def test_overview_board_filter(client):
    from hermes_cli import kanban_db_boards as kbb

    kbb.create_board("voz-hermes", name="Voz Hermes")
    a, _ = _waiting(ASK_WORD)
    b, _ = _waiting(ASK_WORD, board="voz-hermes")
    every = client.get(PREFIX + "/overview", params={"board": "all"}).json()
    assert {i["task_id"] for i in every["needs_you"]} == {a, b}
    assert every["boards"] == ["default", "voz-hermes"]
    only = client.get(PREFIX + "/overview", params={"board": "voz-hermes"}).json()
    assert [i["task_id"] for i in only["needs_you"]] == [b]
    assert only["needs_you"][0]["board_name"] == "Voz Hermes"
    assert only["metrics"]["needs_you"] == 1
    assert client.get(PREFIX + "/overview", params={"board": "nope"}).status_code == 404


def test_overview_running_section(client):
    from hermes_cli import kanban_db as kb
    from ke import hermes_api

    with _conn() as conn:
        tid = create_task(conn, title="Escrever o relatório t_deadbeef", assignee="writer")
        assert kb.claim_task(conn, tid) is not None
        hermes_api.comment(conn, tid, "Rascunho pronto, revisando a seção 2.")
    body = client.get(PREFIX + "/overview").json()
    (row,) = body["running"]
    assert row["task_id"] == tid and row["profile"] == "writer" and row["board"] == "default"
    assert "t_deadbeef" not in row["title"]
    assert "Rascunho pronto" in row["last_activity"]["text"]
    assert row["since_s"] >= 0 and row["last_activity"]["age_s"] >= 0
    assert body["metrics"]["workers_running"] == 1


def test_overview_timeline_from_actions_and_sent_messages(client, store):
    tid, _ = _waiting(ASK_WORD, title="Conectar a placa")
    now = int(time.time())
    store.log_action("default", tid, "human_done", "telegram:6802889854", now - 50)
    store.log_action("default", tid, "guard_release", "cli:ana: active_pr -> reviewer", now - 40)
    store.log_action("default", tid, "infra_undo", "2 infrastructure failure(s): Gateway connection failed", now - 30)
    store.log_action("default", tid, "human_note", "telegram:6802889854", now - 25)  # not a coordinator action
    store.add_sent_message(11, 99, "waits", [{"board": "default", "task_id": tid, "version": 1}], now - 20)
    store.add_sent_message(12, 99, "panel", [], now - 10)
    timeline = client.get(PREFIX + "/overview").json()["timeline"]
    assert [e["kind"] for e in timeline] == [
        "panel_updated", "reminder_sent", "infra_undo", "guard_release", "human_done",
    ]
    done = timeline[-1]
    assert done["detail"] == "telegram" and done["title"] == "Conectar a placa" and done["board"] == "default"
    assert "6802889854" not in json.dumps(timeline)  # no Telegram ids reach the UI
    assert all(e["text"] for e in timeline)
    for i in range(30):
        store.log_action("default", tid, "human_done", "desktop", now - 100 - i)
    assert len(client.get(PREFIX + "/overview").json()["timeline"]) == 20


def test_overview_infra_section(client, store):
    from ke.settings import Settings

    now = int(time.time())
    for run in range(3):
        store.log_action("default", f"t_0000000{run}", "infra_exit", f"infra:{run}", now - 60)
    store.log_action("default", "t_00000009", "infra_exit", "content:9", now - 60)
    (Settings.from_mapping({}).shared_state_dir / "telegram_path").write_text("149.154.167.220", encoding="utf-8")
    body = client.get(PREFIX + "/overview").json()
    infra = body["infra"]
    assert infra["recent_infra_exits_1h"] == 3 and infra["systemic_alert_active"] is True
    assert infra["telegram_path"] == "ip"
    assert body["metrics"]["incidents"] == 1


def test_overview_lists_budget_exhausted_cards(client, store):
    tid = gave_up_with_runs(["infra", "infra"])
    now = int(time.time())
    store.add_infra_undo("default", tid, now - 100)
    store.add_infra_undo("default", tid, now - 50)
    body = client.get(PREFIX + "/overview").json()
    (row,) = body["infra"]["budget_exhausted"]
    assert row["task_id"] == tid and row["attempts"] == 2 and row["last_error_class"] == "infra"
    item = _item(body, tid)
    assert item["kind"] == "infra_exhausted" and item["board"] == "default"
    assert body["metrics"]["incidents"] == 1
    assert _status(tid) == "blocked"  # read-only: no undo happened


# -- item detail ------------------------------------------------------------------------------
def test_item_reminder_text_equals_the_telegram_renderer(client, store, kanban_home):
    from ke import messages
    from ke.settings import Settings
    from ke.waits import WaitTracker

    tid, version = _waiting(ASK_OPTIONS, held=1, title="Gravação")
    settings = Settings.from_mapping({})
    tracker = WaitTracker(settings, store)
    tracker.sync(time.time())
    due = tracker.due_for(store.latest_wait("default", tid), time.time())
    telegram = messages.render_waits(due.owner, [due])
    body = client.get(PREFIX + "/item", params={"board": "default", "task_id": tid}).json()
    assert body["version"] == version and body["kind"] == "human_wait"
    # the single-item reminder minus its 3-line header ("📋 KANBAN · NEEDS YOU", counts, blank)
    assert body["reminder_text"] == telegram.split("\n", 3)[3]
    assert messages.BRAND not in body["reminder_text"]


def test_item_detail_has_conditions_and_recent_comments(client):
    from ke import hermes_api

    tid, _ = _waiting(ASK_WORD)
    with _conn() as conn:
        for i in range(7):
            hermes_api.comment(conn, tid, f"nota {i} sobre t_cafebabe")
    body = client.get(PREFIX + "/item", params={"board": "default", "task_id": tid}).json()
    assert [c["name"] for c in body["conditions"]] == ["dependencies", "guard", "live", "fresh", "wait"]
    assert len(body["comments"]) == 5
    assert body["comments"][-1]["body"].endswith("nota 6 sobre")
    assert all("t_cafebabe" not in c["body"] for c in body["comments"])


def test_item_unknown_card_is_404(client):
    assert client.get(PREFIX + "/item", params={"board": "default", "task_id": "t_00000000"}).status_code == 404
    assert client.get(PREFIX + "/item", params={"board": "nope", "task_id": "t_00000000"}).status_code == 404


# -- answer -----------------------------------------------------------------------------------
def _answer(client, tid, version, **kw):
    return client.post(PREFIX + "/answer", json={"board": "default", "task_id": tid, "version": version, **kw})


def test_answer_option_resolves(client, store):
    tid, version = _waiting(ASK_OPTIONS, held=1)
    r = _answer(client, tid, version, option="b")
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "resolved" and body["overview_item_removed"] is True
    assert body["message"].startswith("✅") and "B — Aguardar a próxima execução" in body["message"]
    assert _status(tid) != "blocked"
    (comment,) = [c for c in _comments(tid) if "desktop" in c]
    assert "via Kanban-Empowered (desktop)" in comment and "choice: B — Aguardar a próxima execução" in comment
    (done,) = [a for a in store.actions_since(0) if a["kind"] == "human_done"]
    assert done["detail"] == "desktop" and done["task_id"] == tid
    after = client.get(PREFIX + "/overview").json()
    assert tid not in {i["task_id"] for i in after["needs_you"]}
    assert after["timeline"][0]["kind"] == "human_done" and after["timeline"][0]["detail"] == "desktop"


def test_answer_option_letter_typed_as_text_is_the_same(client):
    tid, version = _waiting(ASK_OPTIONS)
    assert _answer(client, tid, version, text="A").json()["result"] == "resolved"


def test_answer_confirmation_word_resolves(client):
    tid, version = _waiting(ASK_WORD)
    body = _answer(client, tid, version, text="connected!").json()
    assert body["result"] == "resolved" and _status(tid) != "blocked"


def test_answer_negation_only_comments(client, store):
    tid, version = _waiting(ASK_WORD)
    body = _answer(client, tid, version, text="not yet, the cable is missing").json()
    assert body["result"] == "noted" and body["overview_item_removed"] is False
    assert body["message"] == "Noted; the item is still waiting."
    assert _status(tid) == "blocked"
    assert any("the cable is missing" in c and "desktop" in c for c in _comments(tid))
    assert [a["detail"] for a in store.actions_since(0) if a["kind"] == "human_note"] == ["desktop"]


def test_answer_free_text_resolves_and_posts_the_text(client):
    tid, version = _waiting(ASK_WORD)
    body = _answer(client, tid, version, text="Use a porta USB-C da esquerda, a outra está ruim.").json()
    assert body["result"] == "resolved" and _status(tid) != "blocked"
    assert any("Use a porta USB-C da esquerda" in c for c in _comments(tid))


def test_answer_undeclared_option_is_ambiguous(client):
    tid, version = _waiting(ASK_OPTIONS)
    body = _answer(client, tid, version, option="D").json()
    assert body["result"] == "ambiguous" and "D" in body["message"] and _status(tid) == "blocked"


def test_answer_bare_confirmation_on_an_option_item_is_ambiguous(client):
    tid, version = _waiting(ASK_OPTIONS)
    assert _answer(client, tid, version, text="ok").json()["result"] == "ambiguous"
    assert _status(tid) == "blocked"


def test_answer_version_mismatch_is_changed(client):
    tid, version = _waiting(ASK_WORD)
    body = _answer(client, tid, version + 1, text="connected").json()
    assert body["result"] == "changed" and body["overview_item_removed"] is False
    assert _status(tid) == "blocked"


def _block_event_id(tid, board="default"):
    from ke import hermes_api
    from ke.waits import block_version

    with _conn(board) as conn:
        return block_version(hermes_api.events(conn, tid))


def test_answer_never_resolves_a_breaker_park(client, store):
    """Telegram only lists human waits; /answer enforces the same rule (review finding 1)."""
    tid = gave_up_with_runs(["infra", "infra"])
    before = _comments(tid)
    body = _answer(client, tid, _block_event_id(tid), text="feito").json()
    assert body["result"] == "not_found" and body["overview_item_removed"] is False
    assert _status(tid) == "blocked" and _comments(tid) == before
    assert not [a for a in store.actions_since(0) if a["kind"] in ("human_done", "human_note")]


def test_answer_never_resolves_a_non_human_block_kind(client):
    tid, version = _waiting(ASK_WORD, kind="capability")
    assert _answer(client, tid, version, text="connected").json()["result"] == "not_found"
    assert _status(tid) == "blocked"


def test_answer_on_a_card_already_resolved_elsewhere_is_not_found(client):
    from ke import hermes_api

    tid, version = _waiting(ASK_WORD)
    with _conn() as conn:
        assert hermes_api.unblock(conn, tid)
    assert _answer(client, tid, version, text="connected").json()["result"] == "not_found"


def test_answer_unknown_card_is_not_found(client):
    body = _answer(client, "t_00000000", 1, text="done").json()
    assert body["result"] == "not_found"
    body = client.post(PREFIX + "/answer", json={"board": "nope", "task_id": "t_00000000", "version": 1,
                                                 "text": "done"}).json()
    assert body["result"] == "not_found"


def test_answer_body_author_is_untrusted_and_ignored(client):
    tid, version = _waiting(ASK_WORD)
    _answer(client, tid, version, text="connected", author="Mallory (6802889854)")
    assert not any("Mallory" in c for c in _comments(tid))


@pytest.mark.parametrize("payload", [
    {"board": "default", "task_id": "t_00000001", "version": 1},  # neither text nor option
    {"board": "default", "task_id": "t_00000001", "version": 1, "option": "AB"},
    {"board": "default", "task_id": "t_00000001", "version": -1, "text": "x"},
    {"board": "default", "task_id": "x" * 200, "version": 1, "text": "x"},
    {"board": "default", "task_id": "t_00000001", "version": 1, "text": "x" * 5000},
])
def test_answer_is_validated(client, payload):
    assert client.post(PREFIX + "/answer", json=payload).status_code == 422


def test_answer_classification_matches_telegram_exactly(client):
    """Same classifier as a Telegram reply: every class maps to the same outcome."""
    from ke.asks import parse_ask
    from ke.inbound import classify

    cases = [("A", "resolved"), ("D", "ambiguous"), ("ok", "ambiguous"), ("não", "noted"),
             ("<sua resposta>", "ambiguous"), ("pode rodar amanhã cedo", "resolved")]
    expected = {"option": "resolved", "unknown_option": "ambiguous", "need_option": "ambiguous",
                "negation": "noted", "placeholder": "ambiguous", "answer": "resolved"}
    for text, outcome in cases:
        assert expected[classify(text, parse_ask(ASK_OPTIONS))[0]] == outcome
        tid, version = _waiting(ASK_OPTIONS)
        assert _answer(client, tid, version, text=text).json()["result"] == outcome, text


def test_private_package_path_without_the_agent_half(client, api_mod, monkeypatch):
    """No ``ke`` loaded in the process (the dashboard on its own): the router imports a private copy
    and keeps answering (regression: the Store cache lock was re-taken while importing)."""
    import threading

    for name in [n for n in sys.modules if n == "ke" or n.startswith("ke.")]:
        monkeypatch.delitem(sys.modules, name)
    results = []
    worker = threading.Thread(target=lambda: results.extend(client.get(PREFIX + "/settings").status_code
                                                            for _ in range(2)), daemon=True)
    worker.start()
    worker.join(60)
    assert results == [200, 200], "the private-package path deadlocked or failed"
    assert api_mod._ke_package() == api_mod._PRIVATE_PKG + ".ke"
    for name in [n for n in sys.modules if n == api_mod._PRIVATE_PKG or n.startswith(api_mod._PRIVATE_PKG + ".")]:
        sys.modules.pop(name, None)


def test_web_dashboard_bundle_is_a_slot_only_placeholder():
    """The manifest makes `hermes dashboard` load dashboard/dist/index.js (no api-only flag exists): ship the
    documented slot-only placeholder so the web dashboard neither 404s nor reports NO_REGISTER."""
    data = json.loads((REPO / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    bundle = REPO / "dashboard" / data.get("entry", "dist/index.js")
    src = bundle.read_text(encoding="utf-8")
    assert 'register("kanban-empowered"' in src and "__HERMES_PLUGINS__" in src
    assert "import" not in src and "fetch" not in src  # nothing but the placeholder
    assert data["tab"]["hidden"] is True


def test_settings_never_rewrite_the_agent_halfs_message_language(kanban_home, monkeypatch):
    """Review minor 5: with the agent half in the process the dashboard reads the language it set; only the
    dashboard's private package copy applies the config's language and role names itself."""
    from ke import dashboard, hermes_api, messages

    monkeypatch.setattr(hermes_api, "plugin_settings", lambda plugin="kanban-empowered": {"language": "pt",
                                                                                          "escalate_to": "boss"})
    dashboard._cache.clear()
    monkeypatch.setattr(messages, "_LANG", None)
    monkeypatch.setattr(messages, "_ROLES", frozenset({"sponsor"}))
    s = dashboard.load_settings(apply_messages=False)
    assert s.language == "pt"
    assert messages._LANG is None and messages._ROLES == frozenset({"sponsor"})
    dashboard._cache.clear()
    dashboard.load_settings(apply_messages=True)
    assert messages._LANG == "pt" and "boss" in messages._ROLES
    dashboard._cache.clear()


def test_count_is_the_light_needs_you_metric(client):
    """Review minor 6: the Kanban header button polls GET /count, not the whole overview."""
    assert client.get(PREFIX + "/count").json()["needs_you"] == 0
    _waiting(ASK_WORD)
    _waiting(ASK_OPTIONS, held=1)
    gave_up_with_runs(["infra"])  # a breaker park is not a human wait
    body = client.get(PREFIX + "/count").json()
    assert body["needs_you"] == 2 == client.get(PREFIX + "/overview").json()["metrics"]["needs_you"]
    assert set(body) == {"needs_you", "generated_at"}
