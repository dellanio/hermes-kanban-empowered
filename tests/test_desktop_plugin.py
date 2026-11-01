"""The Desktop half (``desktop/plugin.js``): Hermes' own admission lint plus the loader's import allowlist.

There is no Node toolchain in the test environment, so these are static checks: the file must pass
``hermes_cli.plugin_validate_desktop`` (the catalog's "desktop surface" check), import only the three
specifiers the runtime loader resolves, export the plugin id, avoid JSX syntax and hardcoded colours,
and the whole package must pass ``validate_plugin_dir`` (``hermes plugins validate``).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_JS = REPO / "desktop" / "plugin.js"
ALLOWED = {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}

_STRING = re.compile(r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`", re.DOTALL)
_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\w])//[^\n]*", re.DOTALL)


def _source() -> str:
    return PLUGIN_JS.read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """The source with comments removed and string literals emptied."""
    from hermes_cli.plugin_validate_desktop import _strip_comments

    return _STRING.sub('""', _strip_comments(src))


def test_desktop_surface_lint_is_clean():
    from hermes_cli.plugin_validate_desktop import desktop_surface_findings, desktop_surface_hits

    assert desktop_surface_findings(_source()) == []
    assert desktop_surface_hits(REPO) == []


def test_imports_only_the_specifiers_the_loader_resolves():
    src = _source()
    specs = re.findall(r"^\s*import\s+(?:[^;'\"]*?\bfrom\s*)?['\"]([^'\"]+)['\"]", src, re.MULTILINE)
    assert specs and set(specs) <= ALLOWED, specs
    assert "import(" not in _code_only(src)  # no dynamic import at all
    assert "require(" not in _code_only(src)


def test_exports_the_plugin_id_and_feature_detects_new_sdk_exports():
    src = _source()
    assert re.search(r"\bid:\s*'kanban-empowered'", src)
    assert "export default" in src
    # WORKSPACE_PAGE_HEADER_AREA is "new in this release": never a named import (a missing named
    # export stops the module from loading on an older Desktop)
    named = re.search(r"import\s*\{([^}]*)\}\s*from\s*'@hermes/plugin-sdk'", src).group(1)
    assert "WORKSPACE_PAGE_HEADER_AREA" not in named
    assert "import * as sdk from '@hermes/plugin-sdk'" in src


def test_no_jsx_syntax_and_no_hardcoded_colours():
    code = _code_only(_source())
    # a JSX tag is "<div" / "</div" with no space after "<" (comparisons are written "a < b")
    assert not re.search(r"</?[A-Za-z][\w.]*[\s>/]", code), "JSX syntax does not parse uncompiled"
    src = _source()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", src)
    assert not re.search(r"\brgba?\(|\bhsla?\(", src)


def test_uses_only_tracked_timers_and_listeners():
    code = _code_only(_source())
    assert "window.setInterval" not in code and "window.setTimeout" not in code
    assert not re.search(r"(?<![\w.])set(Interval|Timeout)\(", code)  # only ctx.setInterval / ctx.setTimeout
    assert "window.addEventListener" not in code
    assert "localStorage" not in code and "document." not in code


def test_identifiers_used_in_jsx_calls_are_imported():
    """A component used in jsx()/jsxs() but never imported is a ReferenceError at render (SDK doc pitfall)."""
    src = _source()
    used = set(re.findall(r"\bjsxs?\(\s*([A-Z][A-Za-z0-9]*)\b", src))
    declared = set(re.findall(r"\bfunction\s+([A-Z][A-Za-z0-9]*)\s*\(", src))
    declared |= set(re.findall(r"\bconst\s+([A-Z][A-Za-z0-9]*)\s*=", src))
    named = re.search(r"import\s*\{([^}]*)\}\s*from\s*'@hermes/plugin-sdk'", src).group(1)
    imported = {n.strip().split(" as ")[-1] for n in named.split(",") if n.strip()}
    react_named = re.search(r"import\s*\{([^}]*)\}\s*from\s*'react'", src)
    if react_named:
        imported |= {n.strip() for n in react_named.group(1).split(",") if n.strip()}
    assert used <= declared | imported | {"Fragment"}, used - declared - imported


def test_validate_plugin_dir_passes():
    from hermes_cli.plugin_validate import validate_plugin_dir

    report = validate_plugin_dir(REPO)
    failed = [c for c in report.checks if not c[1]]
    assert not failed, failed
    assert any(c[0] == "desktop surface" for c in report.checks)


def test_kanban_button_follows_router_navigation_not_only_hashchange():
    """Review finding 2: Desktop's HashRouter navigates with pushState (no hashchange, no popstate).

    The header button is registered once with ``when: () => <route is /kanban>`` (re-evaluated whenever the
    header area is rebuilt) and a route watcher (hashchange, popstate and a 500 ms ctx.setInterval) forces a
    rebuild when the route flips, so it never stays on another page and appears after a sidebar navigation.
    """
    src = _source()
    block = src[src.index("id: 'kanban-button'"):][:400]
    assert re.search(r"when:\s*onKanban", block), block
    assert "'popstate'" in src and "'hashchange'" in src
    assert re.search(r"ctx\.setInterval\(\s*checkRoute\s*,\s*ROUTE_POLL_MS\s*\)", src)
    assert re.search(r"const ROUTE_POLL_MS = 500\b", src)
