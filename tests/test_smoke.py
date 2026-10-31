import pytest


def test_hermes_kanban_importable(kanban_home):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x")
        assert kb.get_task(conn, tid).status in ("ready", "todo")


def test_create_task_helper(kanban_home):
    from conftest import create_task
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect() as conn:
        tid = create_task(conn, title="via helper")
        assert kb.get_task(conn, tid).title == "via helper"


def test_plugin_register_is_callable():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "ke_plugin_root", Path(__file__).resolve().parent.parent / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.register(object()) is None


def test_fake_clock_and_fake_tg(clock, fake_tg):
    t0 = clock.now
    clock.advance(60)
    assert clock.now == t0 + 60
    a = fake_tg.send(1, "oi")
    b = fake_tg.send(1, "dois")
    assert b > a and fake_tg.count("send") == 2 and fake_tg.last_text == "dois"
    fake_tg.edit(1, a, "editado")
    assert fake_tg.last_text == "editado" and fake_tg.count("edit") == 1
    fake_tg.fail_next_edit = RuntimeError("message to edit not found")
    with pytest.raises(RuntimeError):
        fake_tg.edit(1, a, "x")
    fake_tg.edit(1, a, "ok")
    assert fake_tg.count("edit") == 2
    fake_tg.fail_next = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        fake_tg.send(1, "x")
    assert fake_tg.count("send") == 2
    fake_tg.pin(1, a)
    assert fake_tg.count("pin") == 1
