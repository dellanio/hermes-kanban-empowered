"""Shared fixtures for the kanban-empowered test suite."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HERMES_SRC = os.environ.get(
    "HERMES_AGENT_SRC", "C:/Users/della/AppData/Local/hermes/hermes-agent"
)
for _p in (str(_REPO_ROOT), _HERMES_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def create_task(conn, **kw) -> str:
    """Thin wrapper over ``hermes_cli.kanban_db.create_task``."""
    from hermes_cli import kanban_db as kb

    kw.setdefault("title", "task")
    return kb.create_task(conn, **kw)


def block_task(conn, task_id: str, **kw) -> bool:
    """Thin wrapper over ``hermes_cli.kanban_db.block_task``."""
    from hermes_cli import kanban_db as kb

    return kb.block_task(conn, task_id, **kw)


class FakeClock:
    """Controllable wall clock: ``clock.now`` is a float, ``advance(s)`` moves it."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = float(now)

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class FakeTelegram:
    """Records Bot API calls by method name (send/edit/pin) instead of hitting the network.

    ``fail_next`` / ``fail_next_edit`` hold an exception raised once by the next
    send / edit call respectively (``fail_next_pin`` likewise for pin).
    """

    METHODS = ("send", "edit", "pin")

    def __init__(self) -> None:
        self.calls: dict[str, list[dict]] = {m: [] for m in self.METHODS}
        self.fail_next: BaseException | None = None
        self.fail_next_edit: BaseException | None = None
        self.fail_next_pin: BaseException | None = None
        self._log: list[tuple[str, dict]] = []
        self._next_id = 1000

    def _pop_failure(self, attr: str) -> None:
        exc = getattr(self, attr)
        if exc is not None:
            setattr(self, attr, None)
            raise exc

    def _record(self, method: str, call: dict) -> None:
        self.calls[method].append(call)
        self._log.append((method, call))

    def send(self, chat_id, text, **kw) -> int:
        self._pop_failure("fail_next")
        self._next_id += 1
        self._record(
            "send",
            {"chat_id": chat_id, "text": text, "message_id": self._next_id, **kw},
        )
        return self._next_id

    def edit(self, chat_id, message_id, text, **kw):
        self._pop_failure("fail_next_edit")
        self._record(
            "edit", {"chat_id": chat_id, "message_id": message_id, "text": text, **kw}
        )
        return True

    def pin(self, chat_id, message_id, **kw):
        self._pop_failure("fail_next_pin")
        self._record("pin", {"chat_id": chat_id, "message_id": message_id, **kw})
        return True

    def count(self, method: str) -> int:
        return len(self.calls[method])

    @property
    def last_text(self) -> str | None:
        """Text of the most recent send or edit, whichever happened last."""
        for method, call in reversed(self._log):
            if method in ("send", "edit"):
                return call["text"]
        return None


@pytest.fixture
def fake_tg() -> FakeTelegram:
    return FakeTelegram()


# -- Coordinator helpers -------------------------------------------------------
def settings_for_test(tmp_path: Path, **overrides):
    """Settings whose shared state lives under ``tmp_path``; ``overrides`` set fields directly."""
    import dataclasses

    from ke.settings import Settings

    base = Settings.from_mapping({"shared_state_dir": str(tmp_path / "state")})
    return dataclasses.replace(base, **overrides) if overrides else base


class FakeApi:
    """Stand-in for ``ke.hermes_api`` as seen by the Coordinator."""

    def __init__(self, active_profile: str = "default", veto_hooks: bool = True) -> None:
        self._active = active_profile
        self._veto = veto_hooks

    def active_profile(self) -> str:
        return self._active

    def veto_hooks_available(self) -> bool:
        return self._veto

    def boards(self) -> list[str]:
        """No boards: the coordinator's waits step then never touches a real kanban DB."""
        return []


def make_coordinator(
    tmp_path: Path,
    coordinator_profile: str = "default",
    active_profile: str = "default",
    veto_hooks: bool = True,
    clock=None,
    telegram=None,
    api=None,
    **settings_overrides,
):
    """A Coordinator wired to a temp Store/LeaderLock and a FakeApi (or ``api``, e.g. ``hermes_api``)."""
    from ke.coordinator import Coordinator
    from ke.store import Store

    settings = settings_for_test(
        tmp_path, coordinator_profile=coordinator_profile, **settings_overrides
    )
    store = Store(settings.shared_state_dir / "state.db")
    kw = {"clock": clock} if clock is not None else {}
    return Coordinator(
        settings,
        store,
        api=api if api is not None else FakeApi(active_profile, veto_hooks),
        telegram=telegram,
        **kw,
    )


@pytest.fixture
def load_plugin():
    """Load the repo root as a package like Hermes does; sweep its modules afterwards.

    ``register()`` binds ``<pkg>.ke.cli`` (a different module object from the
    top-level ``ke.cli``), so isolation comes from dropping every ``<pkg>`` entry.
    """
    import importlib.util

    created: list[str] = []
    before = set(sys.modules)

    def load(name: str = "ke_plugin_pkg"):
        spec = importlib.util.spec_from_file_location(
            name, _REPO_ROOT / "__init__.py", submodule_search_locations=[str(_REPO_ROOT)]
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        created.append(name)
        spec.loader.exec_module(mod)
        return mod

    yield load
    for key in set(sys.modules) - before:
        if any(key == n or key.startswith(n + ".") for n in created):
            del sys.modules[key]
