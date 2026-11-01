import pytest
from conftest import settings_for_test
from test_waits import pt_br  # noqa: F401 (pt_br is a fixture)

from ke.worker_guard import pre_tool_call

GOOD = "Preciso: a\nPor quê: b\nComo responder: c"


@pytest.fixture
def S(tmp_path):
    return lambda **kw: settings_for_test(tmp_path, **kw)


def test_guard_blocks_unstructured_needs_input(S):
    r = pre_tool_call("kanban_block", {"reason": "aguardando o sponsor", "kind": "needs_input"}, settings=S())
    assert r["action"] == "block"
    # agent-facing text goes through messages.t(): English by default
    assert r["message"].startswith("kanban-empowered: a block that waits on a person needs a structured ask")
    for label in ("Need:", "Why:", "How to reply:"):
        assert label in r["message"]


def test_guard_message_follows_the_language(S, pt_br):  # noqa: F811
    r = pre_tool_call("kanban_block", {"reason": "aguardando", "kind": "needs_input"}, settings=S())
    assert r["message"].startswith("kanban-empowered: bloqueio esperando pessoa")
    for label in ("Preciso:", "Por quê:", "Como responder:"):
        assert label in r["message"]


def test_guard_blocks_when_kind_omitted(S):
    r = pre_tool_call("kanban_block", {"reason": "aguardando"}, settings=S())
    assert r["action"] == "block"


def test_guard_allows_structured(S):
    assert pre_tool_call("kanban_block", {"reason": GOOD, "kind": "needs_input"}, settings=S()) is None
    assert pre_tool_call("kanban_block", {"reason": GOOD}, settings=S()) is None


def test_guard_ignores_dependency_kind_and_other_tools(S):
    assert pre_tool_call("kanban_block", {"reason": "x", "kind": "dependency"}, settings=S()) is None
    assert pre_tool_call("terminal", {"command": "ls"}, settings=S()) is None


def test_guard_off_when_not_required(S):
    s = S(require_structured_ask=False)
    assert pre_tool_call("kanban_block", {"reason": "x", "kind": "needs_input"}, settings=s) is None


def test_guard_honors_custom_human_kinds(S):
    s = S(human_kinds=("needs_input", "capability"))
    assert pre_tool_call("kanban_block", {"reason": "x", "kind": "capability"}, settings=s)["action"] == "block"
    assert pre_tool_call("kanban_block", {"reason": "x", "kind": "transient"}, settings=s) is None


@pytest.mark.parametrize("args", [None, "str", 5, [], {}, {"reason": None}, {"reason": 7, "kind": "needs_input"},
                                  {"reason": ["Preciso: a"], "kind": ["x"]}])
def test_guard_never_raises_on_odd_input(S, args):
    r = pre_tool_call("kanban_block", args, settings=S())
    assert r is None or r["action"] == "block"


def test_guard_odd_args_for_human_kind_block(S):
    assert pre_tool_call("kanban_block", {"kind": "needs_input"}, settings=S())["action"] == "block"
    assert pre_tool_call("kanban_block", {"reason": 7, "kind": "needs_input"}, settings=S())["action"] == "block"
    assert pre_tool_call("kanban_block", None, settings=S())["action"] == "block"  # no kind == human wait


def test_guard_ignores_unknown_extra_kwargs(S):
    assert pre_tool_call("terminal", {}, settings=S(), task_id="t1", session_id="s") is None


def test_guard_tool_name_non_str(S):
    assert pre_tool_call(None, {}, settings=S()) is None


def test_guard_without_settings_uses_defaults():
    r = pre_tool_call("kanban_block", {"reason": "x"})
    assert r["action"] == "block"
