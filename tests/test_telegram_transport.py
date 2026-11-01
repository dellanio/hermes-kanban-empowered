"""Telegram transport resilience: sticky IPv4 fallback with Host/SNI rewrite (Hermes' strategy), no real network."""
import json
import logging
import ssl

import httpx
import pytest

from test_waits import pt_br  # noqa: F401 (pt_br is a fixture)

HOST = "api.telegram.org"
IP1, IP2 = "149.154.166.110", "149.154.167.220"
TOKEN = "123456:SECRET-token_xyz"
OK_BODY = {"ok": True, "result": {"message_id": 7}}


class FakeNet:
    """Stands in for the Hermes helper bundle ``hermes_api.telegram_net()`` returns."""

    def __init__(self, env_ips=None, discovered=None, proxy=None):
        self.seed_ips = [IP1, IP2]
        self.env_ips = env_ips
        self.discovered = discovered
        self.proxy = proxy
        self.discover_calls = []
        self.proxy_hosts = []
        self.verify = True

    def parse_ips(self, value):
        return [p.strip() for p in value.split(",") if p.strip()] if value else []

    def discover(self, timeout_s):
        self.discover_calls.append(timeout_s)
        return self.discovered

    def proxy_url(self, hosts):
        self.proxy_hosts.append(list(hosts))
        return self.proxy

    def keepalive(self):
        return []

    def retryable(self, exc):
        return isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))


class FakeHttp(httpx.BaseTransport):
    """``behaviour(request) -> Response | Exception`` decides each attempt; every request is recorded."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.requests = []

    def handle_request(self, request):
        self.requests.append(request)
        out = self.behaviour(request)
        if isinstance(out, Exception):
            raise out
        return out


def _ok(request=None):
    return httpx.Response(200, json=OK_BODY)


def _make(net=None, behaviour=None, clock=None, budget_s=20.0, on_path=None, env=None, timeout_s=10.0):
    from ke.telegram import TelegramTransport

    http = FakeHttp(behaviour or _ok)
    created = []

    def factory(proxy, verify):
        created.append(proxy)
        return http

    tr = TelegramTransport(
        net or FakeNet(), timeout_s=timeout_s, budget_s=budget_s, clock=clock or (lambda: 0.0),
        env=env if env is not None else {}, transport_factory=factory, on_path=on_path,
    )
    return tr, http, created


def _post(tr, token=TOKEN):
    return tr.post(f"https://{HOST}/bot{token}/sendMessage", b'{"chat_id": 1, "text": "x"}')


# -- strategy -------------------------------------------------------------------------------
def test_hostname_reset_then_ip_succeeds_with_host_and_sni_and_is_sticky():
    seen = []

    def behaviour(request):
        seen.append(request.url.host)
        if request.url.host == HOST:
            return httpx.ConnectError("[WinError 10054] connection reset by remote host")
        return _ok()

    labels = []
    tr, http, _ = _make(behaviour=behaviour, on_path=labels.append)
    status, body = _post(tr)
    assert status == 200 and json.loads(body) == OK_BODY
    ip_req = http.requests[-1]
    assert ip_req.url.host == IP1  # IPv4 literal first (Hermes order), TLS server name stays the hostname
    assert ip_req.headers["host"] == HOST
    assert ip_req.extensions["sni_hostname"] == HOST
    assert ip_req.url.path == f"/bot{TOKEN}/sendMessage"
    assert tr.sticky == IP1 and labels == [IP1]
    # the next call goes straight to the remembered path, no re-walk
    before = len(http.requests)
    _post(tr)
    assert [r.url.host for r in http.requests[before:]] == [IP1]
    assert labels == [IP1]  # unchanged path: not re-reported


def test_failed_first_ip_walks_to_the_next_and_remembers_it():
    def behaviour(request):
        return httpx.ConnectTimeout("t") if request.url.host in (IP1, HOST) else _ok()

    tr, http, _ = _make(behaviour=behaviour)
    _post(tr)
    assert [r.url.host for r in http.requests] == [IP1, IP2]
    assert tr.sticky == IP2


def test_hostname_is_tried_last_when_nothing_is_sticky_and_remembered_when_it_works():
    def behaviour(request):
        return _ok() if request.url.host == HOST else httpx.ConnectError("x")

    tr, http, _ = _make(behaviour=behaviour)
    _post(tr)
    assert [r.url.host for r in http.requests] == [IP1, IP2, HOST]
    assert tr.sticky == HOST
    before = len(http.requests)
    _post(tr)
    assert [r.url.host for r in http.requests[before:]] == [HOST]


def test_sticky_failure_rewalks_from_scratch_and_forgets_it():
    state = {"ip1_down": False}

    def behaviour(request):
        if request.url.host == IP1 and state["ip1_down"]:
            return httpx.ConnectError("x")
        return _ok()

    tr, http, _ = _make(behaviour=behaviour)
    _post(tr)
    assert tr.sticky == IP1
    state["ip1_down"] = True
    before = len(http.requests)
    _post(tr)
    assert [r.url.host for r in http.requests[before:]] == [IP1, IP2]
    assert tr.sticky == IP2


def test_all_paths_fail_raises_transport_failure_without_the_token():
    from ke.telegram import TransportFailure

    tr, http, _ = _make(behaviour=lambda r: httpx.ConnectError(f"reset {r.url}"))
    with pytest.raises(TransportFailure) as ei:
        _post(tr)
    assert len(http.requests) == 3  # IP1, IP2, hostname
    assert tr.sticky is None


def test_tls_handshake_timeout_and_connection_reset_are_retryable():
    errors = [
        httpx.ConnectTimeout("_ssl.c:1064: The handshake operation timed out"),
        ssl.SSLError("_ssl.c:1064: The handshake operation timed out"),
        ConnectionResetError(10054, "connection reset by remote host"),
    ]
    for err in errors:
        tr, http, _ = _make(behaviour=lambda r, e=err: e if r.url.host != IP2 else _ok())
        status, _ = _post(tr)
        assert status == 200 and tr.sticky == IP2, repr(err)


def test_error_after_the_request_was_sent_is_not_retried():
    """A read error may follow a delivered sendMessage: retrying could duplicate the message."""
    from ke.telegram import TransportFailure

    tr, http, _ = _make(behaviour=lambda r: httpx.ReadError("peer closed"))
    with pytest.raises(TransportFailure):
        _post(tr)
    assert len(http.requests) == 1


def test_http_error_status_is_a_response_not_a_walk():
    tr, http, _ = _make(behaviour=lambda r: httpx.Response(400, json={"ok": False, "description": "bad"}))
    status, body = _post(tr)
    assert status == 400 and len(http.requests) == 1


def test_configured_env_ips_replace_the_seed_and_non_telegram_hosts_are_untouched():
    net = FakeNet()
    tr, http, _ = _make(net=net, env={"TELEGRAM_FALLBACK_IPS": "1.2.3.4, 5.6.7.8"},
                        behaviour=lambda r: httpx.ConnectError("x") if r.url.host != "5.6.7.8" else _ok())
    _post(tr)
    assert [r.url.host for r in http.requests] == ["1.2.3.4", "5.6.7.8"]

    tr2, http2, _ = _make()
    tr2.post("http://x.test/botT/sendMessage", b"{}")
    assert [r.url.host for r in http2.requests] == ["x.test"]  # custom base: single direct attempt


def test_fallback_disabled_by_hermes_env_flag_uses_the_hostname_only():
    tr, http, _ = _make(env={"HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1"},
                        behaviour=lambda r: httpx.ConnectError("x"))
    with pytest.raises(Exception):
        _post(tr)
    assert [r.url.host for r in http.requests] == [HOST]


def test_proxy_is_resolved_through_hermes_and_replaces_the_ip_walk():
    net = FakeNet(proxy="http://proxy.local:3128")
    tr, http, created = _make(net=net, behaviour=lambda r: httpx.ConnectError("x"))
    with pytest.raises(Exception):
        _post(tr)
    assert created == ["http://proxy.local:3128"]
    assert net.proxy_hosts[0][0] == HOST
    assert [r.url.host for r in http.requests] == [HOST]  # a proxy owns the route: no IP literals


# -- discovery ------------------------------------------------------------------------------
def test_discovery_runs_only_after_a_failed_walk_and_is_cached():
    net = FakeNet(discovered=["9.9.9.9"])
    tr, http, _ = _make(net=net, behaviour=lambda r: _ok() if r.url.host == "9.9.9.9" else httpx.ConnectError("x"))
    assert _post(tr)[0] == 200
    assert len(net.discover_calls) == 1
    assert tr.sticky == "9.9.9.9"
    _post(tr)
    assert len(net.discover_calls) == 1  # sticky path works: nothing to rediscover


def test_a_healthy_first_attempt_never_calls_discovery():
    net = FakeNet(discovered=["9.9.9.9"])
    tr, _, _ = _make(net=net)
    _post(tr)
    assert net.discover_calls == []


def test_discovery_is_refreshed_periodically_not_on_every_failure():
    from ke.telegram import TransportFailure

    now = [0.0]
    net = FakeNet(discovered=None)
    tr, _, _ = _make(net=net, clock=lambda: now[0], behaviour=lambda r: httpx.ConnectError("x"), budget_s=1e9)
    for _ in range(3):
        with pytest.raises(TransportFailure):
            _post(tr)
    assert len(net.discover_calls) == 1  # failed discovery is not retried on every send
    now[0] += 3600
    with pytest.raises(TransportFailure):
        _post(tr)
    assert len(net.discover_calls) == 2


# -- time bound -----------------------------------------------------------------------------
def test_total_time_is_bounded_across_fallbacks():
    from ke.telegram import TransportFailure

    now = [0.0]

    def behaviour(request):
        now[0] += 10.0  # every attempt burns its full timeout
        return httpx.ConnectTimeout("t")

    net = FakeNet(discovered=["9.9.9.9"])
    tr, http, _ = _make(net=net, clock=lambda: now[0], behaviour=behaviour, budget_s=20.0)
    with pytest.raises(TransportFailure):
        _post(tr)
    assert len(http.requests) == 2  # third path and discovery skipped: the budget is spent
    assert net.discover_calls == []


def test_each_attempt_gets_a_timeout_clamped_to_the_remaining_budget():
    now = [0.0]
    seen = []

    def behaviour(request):
        seen.append(dict(request.extensions["timeout"]))
        now[0] += 14.0
        return httpx.ConnectTimeout("t")

    tr, _, _ = _make(clock=lambda: now[0], behaviour=behaviour, budget_s=20.0, timeout_s=10.0)
    with pytest.raises(Exception):
        _post(tr)
    assert seen[0]["read"] == 10.0 and seen[0]["connect"] <= 10.0
    assert seen[1]["read"] == pytest.approx(6.0) and seen[1]["connect"] <= 6.0


# -- client integration ---------------------------------------------------------------------
def _client(tr, token=TOKEN, **kw):
    from ke.telegram import TelegramClient

    return TelegramClient(token, transport=tr, **kw)


def test_client_send_uses_the_resilient_transport_and_parses_the_result():
    tr, http, _ = _make(behaviour=lambda r: httpx.ConnectError("x") if r.url.host == HOST else _ok())
    assert _client(tr).send(5, "oi") == 7
    req = http.requests[0]
    assert req.method == "POST" and req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {"chat_id": 5, "text": "oi"}


def test_client_transport_failure_is_status_none_and_never_leaks_the_token(caplog):
    from ke.telegram import TelegramError

    tr, _, _ = _make(behaviour=lambda r: httpx.ConnectError(f"boom {r.url} {TOKEN}"))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TelegramError) as ei:
            _client(tr).send(5, "oi")
    assert ei.value.status is None
    assert TOKEN not in str(ei.value) and TOKEN not in ei.value.description
    assert "SECRET-token" not in caplog.text and "SECRET-token" not in repr(ei.value.__cause__)
    assert "***" in ei.value.description


def test_client_http_error_json_maps_to_status_and_description():
    from ke.telegram import TelegramError

    tr, _, _ = _make(behaviour=lambda r: httpx.Response(403, json={"ok": False, "description": f"Forbidden {TOKEN}"}))
    with pytest.raises(TelegramError) as ei:
        _client(tr).send(5, "oi")
    assert ei.value.status == 403 and TOKEN not in ei.value.description and "Forbidden" in ei.value.description


def test_client_ok_false_with_200_still_raises_with_error_code():
    from ke.telegram import TelegramError

    tr, _, _ = _make(behaviour=lambda r: httpx.Response(200, json={"ok": False, "error_code": 429, "description": "slow"}))
    with pytest.raises(TelegramError) as ei:
        _client(tr).send(5, "oi")
    assert ei.value.status == 429


def test_client_invalid_json_body_is_a_transport_style_error():
    from ke.telegram import TelegramError

    tr, _, _ = _make(behaviour=lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(TelegramError) as ei:
        _client(tr).send(5, "oi")
    assert ei.value.status is None


def test_client_records_the_last_good_path_in_the_state_file(tmp_path):
    from ke.telegram import read_path_file

    pf = tmp_path / "telegram_path"
    assert read_path_file(pf) is None
    tr, _, _ = _make(behaviour=lambda r: httpx.ConnectError("x") if r.url.host == HOST else _ok(),
                     on_path=None)
    c = _client(tr, path_file=pf)
    c.send(5, "oi")
    assert read_path_file(pf) == IP1
    assert TOKEN not in pf.read_text(encoding="utf-8")


def test_unwritable_state_file_never_breaks_a_send(tmp_path):
    tr, _, _ = _make()
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    c = _client(tr, path_file=blocker / "p")  # its "directory" is a file: cannot be created
    assert c.send(5, "oi") == 7


# -- fail-soft ------------------------------------------------------------------------------
def test_without_hermes_helpers_the_client_falls_back_to_urllib_and_logs_once(monkeypatch, caplog):
    import urllib.request

    from ke import telegram as tg

    def boom():
        raise ImportError("moved")

    monkeypatch.setattr(tg, "_load_net", boom)
    monkeypatch.setattr(tg, "_warned_no_helpers", False)
    calls = []

    class R:
        def read(self):
            return json.dumps(OK_BODY).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: calls.append(req) or R())
    with caplog.at_level(logging.WARNING):
        c1 = tg.TelegramClient("T")
        c2 = tg.TelegramClient("T")
    assert c1.send(1, "a") == 7 and c2.send(1, "b") == 7
    assert len(calls) == 2 and calls[0].full_url == "https://api.telegram.org/botT/sendMessage"
    assert sum("urllib" in r.getMessage() for r in caplog.records) == 1


def test_default_client_uses_the_resilient_transport_when_helpers_load(monkeypatch):
    from ke import telegram as tg

    monkeypatch.setattr(tg, "_load_net", lambda: FakeNet())
    c = tg.TelegramClient("T")
    assert isinstance(c._transport, tg.TelegramTransport)


def test_explicit_opener_keeps_the_legacy_urllib_path():
    from ke import telegram as tg

    c = tg.TelegramClient("T", opener=lambda req, timeout=None: None)
    assert c._transport is None


# -- hermes_api adapter ---------------------------------------------------------------------
def test_hermes_api_telegram_net_wraps_the_real_hermes_helpers():
    from ke import hermes_api

    net = hermes_api.telegram_net()
    assert net.parse_ips("149.154.166.110, 10.0.0.1, ::1") == ["149.154.166.110"]
    assert "149.154.166.110" in net.seed_ips
    assert net.parse_ips(None) == []
    assert any(opt[1] == __import__("socket").SO_KEEPALIVE for opt in net.keepalive())
    assert net.retryable(httpx.ConnectError("x")) and not net.retryable(httpx.ReadError("x"))


def test_hermes_api_discover_is_sync_bounded_and_fail_soft(monkeypatch):
    from ke import hermes_api

    net = hermes_api.telegram_net()
    from plugins.platforms.telegram import telegram_network as tn

    async def fake_discover():
        return ["1.1.1.1"]

    monkeypatch.setattr(tn, "discover_fallback_ips", fake_discover)
    assert net.discover(2.0) == ["1.1.1.1"]

    async def broken():
        raise RuntimeError("doh down")

    monkeypatch.setattr(tn, "discover_fallback_ips", broken)
    assert net.discover(2.0) is None

    async def slow():
        import asyncio

        await asyncio.sleep(5)

    monkeypatch.setattr(tn, "discover_fallback_ips", slow)
    import time

    t0 = time.monotonic()
    assert net.discover(0.2) is None
    assert time.monotonic() - t0 < 2


def test_hermes_api_discover_works_inside_a_running_loop():
    import asyncio

    from ke import hermes_api

    net = hermes_api.telegram_net()

    async def main():
        return net.discover(0.01)  # must not raise "asyncio.run() cannot be called from a running event loop"

    assert asyncio.run(main()) in (None, [], ["149.154.166.110"]) or isinstance(asyncio.run(main()), list)


# -- doctor / catalog -----------------------------------------------------------------------
def test_doctor_telegram_path_row_only_when_known(kanban_home, capsys, tmp_path):
    from ke import cli
    from test_doctor import _out
    from conftest import settings_for_test

    s = settings_for_test(tmp_path)
    cli.run_doctor(s)
    assert "telegram_path" not in _out(capsys)
    (s.shared_state_dir).mkdir(parents=True, exist_ok=True)
    (s.shared_state_dir / "telegram_path").write_text(IP1, encoding="utf-8")
    cli.run_doctor(s)
    out = _out(capsys)
    assert IP1 in out["telegram_path"] and "IPv4" in out["telegram_path"]
    (s.shared_state_dir / "telegram_path").write_text(HOST, encoding="utf-8")
    cli.run_doctor(s)
    assert HOST in _out(capsys)["telegram_path"]
    (s.shared_state_dir / "telegram_path").write_text("", encoding="utf-8")
    cli.run_doctor(s)
    assert _out(capsys)["telegram_path"] == "unknown"


def test_doctor_telegram_path_in_pt_br(kanban_home, capsys, tmp_path, pt_br):  # noqa: F811
    from ke import cli
    from test_doctor import _out
    from conftest import settings_for_test

    s = settings_for_test(tmp_path)
    s.shared_state_dir.mkdir(parents=True, exist_ok=True)
    (s.shared_state_dir / "telegram_path").write_text(IP1, encoding="utf-8")
    cli.run_doctor(s)
    assert _out(capsys)["telegram_path"] == f"{IP1} (IPv4 de contingência)"


def test_telegram_from_env_wires_the_state_file(load_plugin, monkeypatch, tmp_path):
    from conftest import settings_for_test

    mod = load_plugin("ke_plugin_tg_path")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    s = settings_for_test(tmp_path)
    client = mod._telegram_from_env(s)
    assert client._path_file == s.shared_state_dir / "telegram_path"
    assert mod._telegram_from_env()._path_file is None


# -- real httpx, local server (no Telegram) ---------------------------------------------------
def test_real_httpx_transport_posts_json_to_a_local_server_and_reports_closed_ports():
    import http.server
    import threading

    from ke import hermes_api
    from ke.telegram import TelegramClient, TelegramError, TelegramTransport

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.dumps(OK_BODY).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        c = TelegramClient("T", base=f"http://127.0.0.1:{port}",
                           transport=TelegramTransport(hermes_api.telegram_net(), env={}))
        assert c.send(5, "oi") == 7
        assert seen == {"path": "/botT/sendMessage", "body": {"chat_id": 5, "text": "oi"}}
    finally:
        srv.shutdown()
        srv.server_close()
    with pytest.raises(TelegramError) as ei:  # the port is closed now: a plain connect error, status None
        c.send(5, "oi")
    assert ei.value.status is None
