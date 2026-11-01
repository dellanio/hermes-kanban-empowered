"""kanban-empowered dashboard backend: routes mounted by Hermes at ``/api/plugins/kanban-empowered/``.

The Desktop half (``desktop/plugin.js``) reaches these through ``ctx.rest``. Hermes imports this file on
its own (``spec_from_file_location``), not as part of the plugin package, so the plugin's ``ke``
package is located first: the copy the agent half already loaded in this process (the same module
objects, hence the same resolve lock and message language), else a private import of the plugin root.
Every handler is a thin wrapper over ``ke.dashboard``; no logic lives here and no Hermes module is
imported here (``ke/hermes_api.py`` is the only adapter).

Routes:
    GET  /settings                     -> {show_sidebar_menu, show_kanban_button, language}
    GET  /i18n                         -> {language, strings: {ui.*: text}}
    GET  /overview?board=<slug|all>    -> metrics, needs_you, running, timeline, infra, boards
    GET  /count                        -> {needs_you, generated_at} (the Kanban header button's badge)
    GET  /item?board=&task_id=         -> one needs-you entry + reminder text, conditions, comments
    POST /answer                       -> {result, message, overview_item_removed, held_behind_count};
                                          result: resolved | noted | ambiguous | changed | not_found | error
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

router = APIRouter()

_ROOT = Path(__file__).resolve().parent.parent  # the plugin package (plugin.yaml, ke/, locales/)
_PRIVATE_PKG = "kanban_empowered_dashboard_pkg"
_lock = threading.Lock()  # the Store cache
_load_lock = threading.Lock()  # the private package import (never taken while _lock is held by this module)
_stores: dict[str, Any] = {}
_locale_owners: list = []  # the hermes_api module(s) that registered the plugin catalogs for this router


# -- the plugin's ke package ----------------------------------------------------------------------
def _ke_package() -> str:
    """Dotted name of a loaded ``ke`` package that lives in this plugin's folder, loading one if needed."""
    target = (_ROOT / "ke").resolve()
    for name, mod in list(sys.modules.items()):
        if name == _PRIVATE_PKG + ".ke" or (name != "ke" and not name.endswith(".ke")):
            continue  # the agent half's copy first; the private one is the fallback below
        file = getattr(mod, "__file__", None)
        try:
            if file and Path(file).resolve().parent == target:
                return name
        except OSError:
            continue
    with _load_lock:
        if _PRIVATE_PKG not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                _PRIVATE_PKG, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)]
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load the kanban-empowered package from {_ROOT}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[_PRIVATE_PKG] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(_PRIVATE_PKG, None)
                raise
    return _PRIVATE_PKG + ".ke"


def _ke(sub: str):
    return importlib.import_module(f"{_ke_package()}.{sub}")


def _context():
    """``(dashboard module, settings, store)`` for one request."""
    package = _ke_package()
    dashboard = _ke("dashboard")
    adapter = _ke("hermes_api")
    if adapter.ensure_locales(_ROOT / "locales"):
        with _lock:
            _locale_owners.append(adapter)
    # the agent half's message layer (language, role names) is left as the agent configured it
    settings = dashboard.load_settings(apply_messages=package.startswith(_PRIVATE_PKG + "."))
    path = str(Path(settings.shared_state_dir) / "state.db")
    store_cls = _ke("store").Store  # resolved before taking the cache lock
    with _lock:
        store = _stores.get(path)
        if store is None:
            store = store_cls(Path(path))
            _stores[path] = store
    return dashboard, settings, store


def close_stores() -> None:
    """Close the cached plugin Stores and drop locale packs this module registered (tests, reloads)."""
    with _lock:
        for store in _stores.values():
            try:
                store.close()
            except Exception:
                pass
        _stores.clear()
        owners = list(_locale_owners)
        _locale_owners.clear()
    for adapter in owners:  # the copy that registered them, even if another copy is loaded now
        try:
            adapter.release_locales()
        except Exception:
            pass


# -- models -----------------------------------------------------------------------------------
_SLUG = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$"
_TASK = r"^[A-Za-z0-9_\-]{1,64}$"


class AnswerBody(BaseModel):
    """The drawer's answer. Desktop sends no identity, and any extra field (an "author") is dropped."""

    model_config = ConfigDict(extra="ignore")

    board: str = Field(..., pattern=_SLUG)
    task_id: str = Field(..., pattern=_TASK)
    version: int = Field(..., ge=0)
    text: Optional[str] = Field(None, max_length=4000)
    option: Optional[str] = Field(None, pattern=r"^[A-Za-z]$")

    @model_validator(mode="after")
    def _something_to_say(self):
        if not self.option and not (self.text or "").strip():
            raise ValueError("text or option is required")
        return self


# -- routes -----------------------------------------------------------------------------------
@router.get("/settings")
def get_settings() -> dict:
    dashboard, settings, _store = _context()
    return dashboard.settings_view(settings)


@router.get("/i18n")
def get_i18n() -> dict:
    dashboard, _settings, _store = _context()
    return dashboard.ui_strings()


@router.get("/overview")
def get_overview(board: Optional[str] = Query(None, max_length=64)) -> dict:
    dashboard, settings, store = _context()
    try:
        return dashboard.overview(settings, store, time.time(), board=board)
    except LookupError:
        raise HTTPException(status_code=404, detail="board not found")


@router.get("/count")
def get_count() -> dict:
    dashboard, settings, _store = _context()
    return dashboard.count(settings, time.time())


@router.get("/item")
def get_item(board: str = Query(..., max_length=64), task_id: str = Query(..., max_length=64)) -> dict:
    dashboard, settings, store = _context()
    try:
        return dashboard.item(settings, store, time.time(), board, task_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="not_found")


@router.post("/answer")
def post_answer(body: AnswerBody) -> dict:
    dashboard, settings, store = _context()
    return dashboard.answer(
        settings, store, time.time(), body.board, body.task_id, body.version, text=body.text, option=body.option
    )
