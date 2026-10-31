import pathlib
import re

from conftest import create_task


def test_constants():
    from ke import hermes_api as h

    assert h.HERMES_COMPAT == "bf0203a794"


def test_boards_default_and_extra(kanban_home):
    from hermes_cli import kanban_db_boards as kbb
    from ke import hermes_api as h

    assert h.boards() == ["default"]
    kbb.create_board("side")
    assert h.boards() == ["default", "side"]


def test_comment_prefix_and_author(kanban_home):
    from ke import hermes_api as h

    with h.connect("default") as conn:
        tid = create_task(conn, title="t")
        cid = h.comment(conn, tid, "segurado")
        assert isinstance(cid, int) and cid > 0
        c = h.comments(conn, tid)[-1]
        assert c.author == "kanban-empowered" and c.body.startswith("kanban-empowered: ")
        assert c.body == "kanban-empowered: segurado"


def test_comment_does_not_double_prefix(kanban_home):
    from ke import hermes_api as h

    with h.connect("default") as conn:
        tid = create_task(conn, title="t")
        h.comment(conn, tid, "kanban-empowered: already")
        assert h.comments(conn, tid)[-1].body == "kanban-empowered: already"


def test_task_and_events(kanban_home):
    from ke import hermes_api as h

    with h.connect("default") as conn:
        tid = create_task(conn, title="hello")
        assert h.task(conn, tid).title == "hello"
        assert h.task(conn, "nope") is None
        assert any(e.kind == "created" for e in h.events(conn, tid))


def test_parents_children(kanban_home):
    from ke import hermes_api as h

    with h.connect("default") as conn:
        a = create_task(conn, title="a")
        b = create_task(conn, title="b", parents=[a])
        assert h.children(conn, a) == [b]
        assert h.parents(conn, b) == [a]
        assert h.parents(conn, a) == [] and h.children(conn, b) == []


def test_unblock(kanban_home):
    from ke import hermes_api as h

    with h.connect("default") as conn:
        tid = create_task(conn, title="t", assignee="default")
        assert h.unblock(conn, tid) is False  # not blocked
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        conn.commit()
        assert h.unblock(conn, tid) is True
        assert h.task(conn, tid).status == "ready"


def test_assign(kanban_home):
    from ke import hermes_api as h

    with h.connect("default") as conn:
        tid = create_task(conn, title="t")
        assert h.assign(conn, tid, "alice") is True
        assert h.task(conn, tid).assignee == "alice"
        assert h.assign(conn, "missing", "alice") is False


def test_profiles_lists_dirs_plus_default(kanban_home):
    from ke import hermes_api as h

    assert h.profiles() == ["default"]
    (kanban_home / "profiles" / "zed").mkdir(parents=True)
    (kanban_home / "profiles" / "alice").mkdir()
    (kanban_home / "profiles" / "afile.txt").write_text("x")
    (kanban_home / "profiles" / ".deleted").mkdir()
    assert h.profiles() == ["default", "alice", "zed"]


def test_profiles_no_duplicate_default(kanban_home):
    from ke import hermes_api as h

    (kanban_home / "profiles" / "default").mkdir(parents=True)
    assert h.profiles() == ["default"]


def test_veto_hooks_detection_reflects_valid_hooks(monkeypatch):
    from ke import hermes_api as h
    import hermes_cli.plugins as p

    monkeypatch.setattr(p, "VALID_HOOKS", set(p.VALID_HOOKS) - {"kanban_pre_claim"})
    assert h.veto_hooks_available() is False


def test_veto_hooks_available_when_both_present(monkeypatch):
    from ke import hermes_api as h
    import hermes_cli.plugins as p

    monkeypatch.setattr(
        p, "VALID_HOOKS", set(p.VALID_HOOKS) | {"kanban_pre_claim", "kanban_pre_complete"}
    )
    assert h.veto_hooks_available() is True


def test_only_adapter_imports_hermes_cli():
    ke_dir = pathlib.Path(__file__).resolve().parent.parent / "ke"
    pat = re.compile(r"^\s*(from|import)\s+hermes_cli", re.M)
    offenders = [
        p.name
        for p in ke_dir.glob("*.py")
        if p.name != "hermes_api.py" and pat.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
