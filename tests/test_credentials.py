"""Catalog rule 11: the Telegram token (and every other secret) is read only through Hermes' secret scope."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def api():
    from ke import hermes_api

    hermes_api._warned_unscoped.clear()
    yield hermes_api
    hermes_api._warned_unscoped.clear()


@pytest.fixture
def multiplexed():
    """This task runs under multiplex semantics (``get_secret`` fails closed with no scope bound)."""
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context

    token = set_multiplex_context(True)
    yield
    reset_multiplex_context(token)


@pytest.fixture
def scope():
    """Bind a secret scope for the duration of a block: ``with scope({...}): ...``."""
    import contextlib

    from agent.secret_scope import reset_secret_scope, set_secret_scope

    @contextlib.contextmanager
    def bind(mapping, profile_home=None):
        token = set_secret_scope(mapping, profile_home=profile_home)
        try:
            yield
        finally:
            reset_secret_scope(token)

    return bind


@pytest.fixture
def foreign_home(tmp_path):
    """A HERMES_HOME override naming another profile (a routed body that bound no scope)."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    other = tmp_path / "other-profile"
    other.mkdir()
    token = set_hermes_home_override(str(other))
    yield other
    reset_hermes_home_override(token)


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "TELEGRAM_BOT_TOKEN" in r.getMessage()]


def test_token_is_the_scoped_value_not_the_process_env(api, scope, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:ambient")
    with scope({"TELEGRAM_BOT_TOKEN": " 111:scoped "}):
        assert api.telegram_token() == "111:scoped"


def test_token_is_the_scoped_value_under_multiplexing(api, scope, multiplexed, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:ambient")
    with scope({"TELEGRAM_BOT_TOKEN": "111:scoped"}):
        assert api.telegram_token() == "111:scoped"
    with scope({"OTHER": "x"}):  # a bound scope without the key: never another profile's env value
        assert api.telegram_token() is None


def test_single_profile_process_reads_through_get_secret(api, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    assert api.telegram_token() == "123:abc"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    assert api.telegram_token() is None
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert api.telegram_token() is None


def test_unscoped_read_returns_none_with_one_warning(api, multiplexed, foreign_home, monkeypatch, caplog):
    """UnscopedSecretError while serving another profile: None, one warning, never os.environ."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:ambient")
    caplog.set_level(logging.WARNING)
    assert api.telegram_token() is None
    assert api.telegram_token() is None
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "999:ambient" not in caplog.text


def test_unscoped_read_without_a_launch_scope_returns_none(api, multiplexed, monkeypatch, caplog):
    """A Hermes build without the launch-profile body scope: None and one warning."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:ambient")
    monkeypatch.setattr(api, "_launch_profile_secret", lambda name: api._NO_LAUNCH_SCOPE)
    caplog.set_level(logging.WARNING)
    assert api.telegram_token() is None
    assert api.secret("TELEGRAM_BOT_TOKEN") is None
    assert len(_warnings(caplog)) == 1


def test_unscoped_launch_profile_body_uses_hermes_launch_scope(api, multiplexed, tmp_path, monkeypatch, caplog):
    """The gateway's kanban dispatch tick: multiplexed, nothing bound, the launch profile's own context.
    The token comes from Hermes' launch-profile body scope (frozen launch env + launch home .env)."""
    import tui_gateway.launch_profile_policy as policy

    home = tmp_path / "launch-home"
    home.mkdir()
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=222:launch\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:ambient")
    monkeypatch.setattr(policy, "_snapshot", {})  # the launch env frozen at activation (empty here)
    caplog.set_level(logging.WARNING)
    assert api.telegram_token() == "222:launch"
    assert _warnings(caplog) == []


def test_scoped_env_reads_through_the_secret_scope(api, scope, monkeypatch):
    from ke import telegram

    monkeypatch.setenv("TELEGRAM_FALLBACK_IPS", "9.9.9.9")
    env = telegram._scoped_env()
    assert isinstance(env, api.ScopedEnv)
    with scope({"TELEGRAM_FALLBACK_IPS": "1.2.3.4"}):
        assert env.get("TELEGRAM_FALLBACK_IPS") == "1.2.3.4"
        assert env.get("UNSET_NAME", "d") == "d"


def _package_sources():
    for path in sorted(_REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel.startswith(("tests/", ".superpowers/", "docs/")) or "/__pycache__/" in rel:
            continue
        yield rel, path.read_text(encoding="utf-8")


def test_no_direct_token_env_reads_in_the_package():
    """Every TELEGRAM_BOT_TOKEN read goes through hermes_api.telegram_token (no os.environ / getenv)."""
    sources = dict(_package_sources())
    assert "ke/hermes_api.py" in sources and "__init__.py" in sources
    env_read = re.compile(r"(environ|getenv)\b.*TELEGRAM_BOT_TOKEN|TELEGRAM_BOT_TOKEN.*\b(environ|getenv)\b")
    literal = re.compile(r"""["']TELEGRAM_BOT_TOKEN["']""")
    for rel, text in sources.items():
        for no, line in enumerate(text.splitlines(), 1):
            assert not env_read.search(line), f"{rel}:{no}: {line.strip()}"
            if rel != "ke/hermes_api.py":
                assert not literal.search(line), f"{rel}:{no}: token name outside hermes_api: {line.strip()}"
    for rel in ("__init__.py", "ke/cli.py", "ke/dashboard.py"):
        assert "telegram_token()" in sources[rel], rel


def test_only_hermes_api_imports_hermes_modules():
    pattern = re.compile(
        r"^\s*(from|import)\s+(hermes_cli|agent|plugins|tui_gateway|gateway|hermes_constants|hermes_yaml)\b",
        re.M,
    )
    offenders = [rel for rel, text in _package_sources() if rel != "ke/hermes_api.py" and pattern.search(text)]
    assert offenders == []
