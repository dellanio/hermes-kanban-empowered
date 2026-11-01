
from conftest import settings_for_test
from test_waits import pt_br  # noqa: F401 (pt_br is a fixture)


def _out(capsys) -> dict:
    lines = capsys.readouterr().out.strip().splitlines()
    return dict(line.split(": ", 1) for line in lines)


def test_doctor_reports_missing_hooks(monkeypatch, capsys, tmp_path):
    from ke import cli, hermes_api as h

    monkeypatch.setattr(h, "veto_hooks_available", lambda: False)
    cli.run_doctor(settings_for_test(tmp_path))
    assert "veto_hooks: missing" in capsys.readouterr().out


def test_doctor_reports_present_hooks(monkeypatch, capsys, tmp_path):
    from ke import cli, hermes_api as h

    monkeypatch.setattr(h, "veto_hooks_available", lambda: True)
    cli.run_doctor(settings_for_test(tmp_path))
    assert "veto_hooks: present" in capsys.readouterr().out


def test_doctor_all_keys(kanban_home, capsys, tmp_path):
    from ke import cli

    s = settings_for_test(tmp_path)
    cli.run_doctor(s)
    out = _out(capsys)
    assert list(out) == [
        "hermes_compat",
        "hermes_head",
        "veto_hooks",
        "coordinator",
        "active_profile",
        "coordinator_profile",
        "shared_state_dir",
        "profiles_with_plugin",
        "telegram_token",
        "owner_chats",
    ]
    assert out["hermes_compat"] == "bf0203a794"
    assert out["coordinator"] == "none"
    assert out["shared_state_dir"] == str(s.shared_state_dir)
    assert out["profiles_with_plugin"] == "none"


def test_doctor_reports_coordinator_pid(kanban_home, capsys, tmp_path):
    import os
    import time

    from ke import cli
    from ke.leader import LeaderLock

    s = settings_for_test(tmp_path)
    assert LeaderLock(s.shared_state_dir / "leader.lock").acquire(time.time())
    cli.run_doctor(s)
    assert _out(capsys)["coordinator"] == str(os.getpid())


def _profile(root, name, config, plugin_dir=True):
    """A fake profile home under ``root`` (``root`` itself for ``default``)."""
    home = root if name == "default" else root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / "config.yaml").write_text(config, encoding="utf-8")
    if plugin_dir:
        (home / "plugins" / "kanban-empowered").mkdir(parents=True)
    return home


def test_doctor_profiles_with_plugin(kanban_home, capsys, tmp_path):
    from ke import cli

    _profile(kanban_home, "default", "plugins:\n  entries:\n    kanban-empowered:\n      enabled: true\n")
    _profile(kanban_home, "alice", "plugins:\n  entries:\n    kanban-empowered: {}\n")
    _profile(kanban_home, "bob", "plugins:\n  entries:\n    other: {}\n")
    _profile(kanban_home, "carol", "")
    (kanban_home / "profiles" / "dave").mkdir()  # no config.yaml
    cli.run_doctor(settings_for_test(tmp_path))
    assert _out(capsys)["profiles_with_plugin"] == "default, alice"


def test_profiles_with_plugin_reads_the_enabled_list(kanban_home):
    from ke import hermes_api as h

    enabled = "plugins:\n  enabled:\n    - orca-status\n    - kanban-empowered\n  disabled: []\n  entries: {}\n"
    _profile(kanban_home, "default", enabled)
    _profile(kanban_home, "builder", enabled)
    _profile(kanban_home, "other", "plugins:\n  enabled:\n    - orca-status\n")
    assert h.profiles_with_plugin() == ["default", "builder"]


def test_profiles_with_plugin_skips_disabled_and_missing_dir(kanban_home):
    from ke import hermes_api as h

    both = "plugins:\n  enabled: [kanban-empowered]\n  disabled: [kanban-empowered]\n"
    _profile(kanban_home, "off", both)
    _profile(kanban_home, "nodir", "plugins:\n  enabled: [kanban-empowered]\n", plugin_dir=False)
    _profile(kanban_home, "entries_nodir", "plugins:\n  entries:\n    kanban-empowered: {}\n", plugin_dir=False)
    _profile(kanban_home, "ok", "plugins:\n  enabled: [kanban-empowered]\n")
    assert h.profiles_with_plugin() == ["ok"]


def test_profiles_with_plugin_entries_only_counts(kanban_home):
    from ke import hermes_api as h

    _profile(kanban_home, "e", "plugins:\n  entries:\n    kanban-empowered:\n      settings: {}\n")
    assert h.profiles_with_plugin() == ["e"]


def test_profiles_with_plugin_tolerates_malformed_plugins_section(kanban_home):
    from ke import hermes_api as h

    _profile(kanban_home, "a", "plugins: nope\n")
    _profile(kanban_home, "b", "plugins:\n  enabled: kanban-empowered\n  disabled: null\n")
    _profile(kanban_home, "c", "plugins: [x\n")
    assert h.profiles_with_plugin() == []


def test_hermes_head_is_short_sha_or_none():
    from ke import hermes_api as h

    head = h.hermes_head()
    assert head is None or (4 <= len(head) <= 12 and all(c in "0123456789abcdef" for c in head))


def _fake_checkout(monkeypatch, tmp_path):
    import hermes_cli

    root = tmp_path / "hermes"
    (root / "hermes_cli").mkdir(parents=True)
    monkeypatch.setattr(hermes_cli, "__file__", str(root / "hermes_cli" / "__init__.py"))
    return root


def test_hermes_head_reads_loose_ref(monkeypatch, tmp_path):
    from ke import hermes_api as h

    root = _fake_checkout(monkeypatch, tmp_path)
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".git" / "refs" / "heads" / "main").write_text("0123456789abcdef0123456789abcdef01234567\n")
    assert h.hermes_head() == "0123456789"


def test_hermes_head_falls_back_to_packed_refs(monkeypatch, tmp_path):
    from ke import hermes_api as h

    root = _fake_checkout(monkeypatch, tmp_path)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".git" / "packed-refs").write_text(
        "# pack-refs\nfedcba9876543210fedcba9876543210fedcba98 refs/heads/main\n^1111111111111111111111111111111111111111\n"
    )
    assert h.hermes_head() == "fedcba9876"


def test_hermes_head_detached_and_gitdir_file(monkeypatch, tmp_path):
    from ke import hermes_api as h

    root = _fake_checkout(monkeypatch, tmp_path)
    real = tmp_path / "real-git"
    real.mkdir()
    (real / "HEAD").write_text("aaaaaaaaaabbbbbbbbbbccccccccccdddddddddd\n")
    (root / ".git").write_text(f"gitdir: {real}\n")
    assert h.hermes_head() == "aaaaaaaaaa"


def test_hermes_head_unknown_cases_return_none(monkeypatch, tmp_path):
    from ke import hermes_api as h

    root = _fake_checkout(monkeypatch, tmp_path)
    assert h.hermes_head() is None  # no .git at all
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/gone\n")
    assert h.hermes_head() is None  # ref neither loose nor packed


def test_active_profile_follows_hermes_home(kanban_home, monkeypatch):
    from ke import hermes_api as h

    assert isinstance(h.active_profile(), str) and h.active_profile()
    prof = kanban_home / "profiles" / "builder"
    prof.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(prof))
    assert h.active_profile() == "builder"


def test_cli_setup_and_run(kanban_home, capsys, tmp_path, monkeypatch):
    import argparse

    from ke import cli

    monkeypatch.setattr(cli, "_settings", None)  # restored on teardown
    monkeypatch.setattr(cli, "_config_error", None)
    cli.bind(settings_for_test(tmp_path))
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    args = parser.parse_args(["doctor"])
    cli.run(args)
    assert "hermes_compat: bf0203a794" in capsys.readouterr().out


class _Ctx:
    def __init__(self, state_dir, config=None):
        self.state_dir = state_dir
        self.config = config or {}
        self.hooks, self.cmds = {}, {}

    def get_config(self, key, default=None):
        if key in self.config:
            return self.config[key]
        return str(self.state_dir) if key == "shared_state_dir" else default

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cmds[name] = (setup_fn, handler_fn)


def test_register_wires_hook_and_cli(kanban_home, tmp_path, load_plugin):
    mod = load_plugin()
    ctx = _Ctx(tmp_path / "state")
    mod.register(ctx)
    assert "on_kanban_dispatch_tick" in ctx.hooks
    assert "kanban-empowered" in ctx.cmds
    setup_fn, handler_fn = ctx.cmds["kanban-empowered"]
    assert callable(setup_fn) and callable(handler_fn)
    # Hermes passes keyword args; the hook must never raise into the dispatcher.
    for dry in (True, False):
        ctx.hooks["on_kanban_dispatch_tick"](
            board="default", profile_name="default", dry_run=dry, outcome=None, result=None
        )


def test_register_survives_broken_setup(kanban_home, tmp_path, load_plugin):
    mod = load_plugin()
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    ctx = _Ctx(blocker / "state")  # cannot mkdir under a regular file
    mod.register(ctx)  # must not raise; hermes keeps loading other plugins
    assert "kanban-empowered" in ctx.cmds  # doctor stays available


def test_register_with_invalid_config_keeps_doctor(kanban_home, tmp_path, load_plugin, capsys):
    mod = load_plugin()
    ctx = _Ctx(tmp_path / "state", config={"mode": "bogus"})
    mod.register(ctx)
    assert "kanban-empowered" in ctx.cmds
    assert "on_kanban_dispatch_tick" not in ctx.hooks  # no coordinator on guessed settings
    setup_fn, handler_fn = ctx.cmds["kanban-empowered"]
    import argparse

    parser = argparse.ArgumentParser()
    setup_fn(parser)
    handler_fn(parser.parse_args(["doctor"]))
    out = capsys.readouterr().out
    assert "config: invalid (" in out and "mode" in out
    assert "hermes_compat: bf0203a794" in out


def test_doctor_no_config_line_when_valid(kanban_home, capsys, tmp_path):
    from ke import cli

    cli.run_doctor(settings_for_test(tmp_path))
    assert "config:" not in capsys.readouterr().out


def test_register_wires_pre_tool_call_guard(kanban_home, tmp_path, load_plugin):
    mod = load_plugin()
    ctx = _Ctx(tmp_path / "state")
    mod.register(ctx)
    guard = ctx.hooks["pre_tool_call"]
    r = guard(tool_name="kanban_block", args={"reason": "x", "kind": "needs_input"}, task_id="t")
    assert r["action"] == "block"
    assert guard(tool_name="terminal", args={"command": "ls"}, session_id="s") is None


def test_register_guard_honors_config_and_survives_invalid_config(kanban_home, tmp_path, load_plugin):
    mod = load_plugin()
    ctx = _Ctx(tmp_path / "state", config={"waits.require_structured_ask": False})
    mod.register(ctx)
    assert ctx.hooks["pre_tool_call"](tool_name="kanban_block", args={"reason": "x"}) is None

    mod2 = load_plugin("ke_plugin_pkg2")
    bad = _Ctx(tmp_path / "state2", config={"mode": "bogus"})
    mod2.register(bad)
    assert "on_kanban_dispatch_tick" not in bad.hooks
    r = bad.hooks["pre_tool_call"](tool_name="kanban_block", args={"reason": "x"})
    assert r["action"] == "block"


def test_doctor_never_says_leader(kanban_home, capsys, tmp_path):
    from ke import cli

    cli.run_doctor(settings_for_test(tmp_path), config_error="x")
    rows = _out(capsys)
    rows.pop("shared_state_dir")  # a path; may contain anything
    assert "leader" not in " ".join([*rows, *rows.values()]).lower()


# -- final review I-3 / M-7: profile and Telegram rows -----------------------------------------
def test_doctor_reports_profiles_token_and_owner_chats(kanban_home, capsys, tmp_path, monkeypatch):
    from ke import cli

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cli.run_doctor(settings_for_test(tmp_path, coordinator_profile="boss"))
    out = _out(capsys)
    assert out["active_profile"] == "default" and out["coordinator_profile"] == "boss"
    assert out["telegram_token"] == "missing" and out["owner_chats"] == "0 configured"

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    cli.run_doctor(settings_for_test(tmp_path, owner_ids={"sponsor": 1, "boss": 2}))
    out = _out(capsys)
    assert out["telegram_token"] == "present" and out["owner_chats"] == "2 configured"
    assert "123:abc" not in " ".join(out.values())  # never print the token itself


def test_doctor_rows_in_pt_br(kanban_home, capsys, tmp_path, monkeypatch, pt_br):  # noqa: F811
    from ke import cli

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cli.run_doctor(settings_for_test(tmp_path, owner_ids={"sponsor": 1}))
    out = _out(capsys)
    assert out["telegram_token"] == "ausente" and out["owner_chats"] == "1 configurado(s)"


def test_doctor_survives_an_unreadable_active_profile(kanban_home, capsys, tmp_path, monkeypatch):
    from ke import cli, hermes_api as h

    def boom():
        raise RuntimeError("moved")

    monkeypatch.setattr(h, "active_profile", boom)
    cli.run_doctor(settings_for_test(tmp_path))
    assert _out(capsys)["active_profile"] == "unknown"


# -- final review I-3: no Store outside the coordinator profile --------------------------------
def test_worker_profile_register_creates_no_state_db(kanban_home, tmp_path, load_plugin):
    mod = load_plugin()
    state = tmp_path / "state"
    ctx = _Ctx(state, config={"coordinator_profile": "other"})  # this process is not the coordinator
    mod.register(ctx)
    assert "pre_tool_call" in ctx.hooks and "kanban-empowered" in ctx.cmds
    assert "on_kanban_dispatch_tick" not in ctx.hooks
    assert not (state / "state.db").exists()
    setup_fn, handler_fn = ctx.cmds["kanban-empowered"]
    import argparse

    parser = argparse.ArgumentParser()
    setup_fn(parser)
    handler_fn(parser.parse_args(["doctor"]))  # doctor needs no Store either
    assert not (state / "state.db").exists()


def test_coordinator_profile_register_opens_the_store(kanban_home, tmp_path, load_plugin):
    mod = load_plugin()
    state = tmp_path / "state"
    ctx = _Ctx(state)
    mod.register(ctx)
    assert "on_kanban_dispatch_tick" in ctx.hooks and (state / "state.db").exists()


# -- final review M-1: the worker guard survives a broken CLI import ---------------------------
def test_worker_guard_survives_a_hermes_module_move(kanban_home, tmp_path, load_plugin, monkeypatch):
    import sys

    import hermes_cli

    monkeypatch.delattr(hermes_cli, "kanban_db_boards", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db_boards", None)  # "moved": import fails
    mod = load_plugin()
    ctx = _Ctx(tmp_path / "state")
    mod.register(ctx)  # must not raise
    guard = ctx.hooks["pre_tool_call"]
    r = guard(tool_name="kanban_block", args={"reason": "x", "kind": "needs_input"})
    assert r["action"] == "block" and "How to reply:" in r["message"]


def test_doctor_lists_config_conflicts(kanban_home, capsys, tmp_path):
    from ke import cli
    from ke.settings import Settings

    s = Settings.from_mapping({
        "shared_state_dir": str(tmp_path / "state"), "default_owner": "flat",
        "telegram_owner_id": 5, "telegram": {"owner_ids": {"bob": 3}}, "waits": {"default_owner": "nested"},
    })
    cli.run_doctor(s)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("config_conflict: ")]
    assert lines == [
        "config_conflict: form value flat overrides config.yaml waits.default_owner nested",
        "config_conflict: telegram_owner_id 5 is ignored: telegram.owner_ids is set and has no id for flat",
    ]


def test_doctor_config_conflicts_in_pt_br(kanban_home, capsys, tmp_path, pt_br):  # noqa: F811
    from ke import cli
    from ke.settings import Settings

    s = Settings.from_mapping({
        "shared_state_dir": str(tmp_path / "state"), "default_owner": "flat", "waits": {"default_owner": "nested"},
    })
    cli.run_doctor(s)
    out = capsys.readouterr().out
    assert "config_conflict: " in out and "waits.default_owner" in out and "overrides" not in out


def test_doctor_no_conflict_rows_without_conflicts(kanban_home, capsys, tmp_path):
    from ke import cli

    cli.run_doctor(settings_for_test(tmp_path))
    assert "config_conflict" not in capsys.readouterr().out


def test_doctor_lists_owner_id_difference_conflict(kanban_home, capsys, tmp_path):
    from ke import cli
    from ke.settings import Settings

    s = Settings.from_mapping({
        "shared_state_dir": str(tmp_path / "state"), "telegram_owner_id": 5,
        "telegram": {"owner_ids": {"sponsor": 3}},
    })
    cli.run_doctor(s)
    assert ("config_conflict: telegram_owner_id 5 is ignored: config.yaml telegram.owner_ids.sponsor is 3"
            in capsys.readouterr().out.splitlines())
