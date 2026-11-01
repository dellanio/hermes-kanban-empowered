"""Minimal Telegram Bot API client: ``sendMessage``, ``editMessageText`` and ``pinChatMessage``.

Outbound only. The plugin never polls (no ``getUpdates``): replies reach it through the
gateway's ``pre_gateway_dispatch`` hook. JSON POSTs, no retries of a failed *call* (the coordinator
simply tries again on a later evaluation). Every failure surfaces as :class:`TelegramError`; the bot
token never appears in its message.

Transport: the path from some hosts to ``api.telegram.org`` is intermittently broken (connection
resets, TLS handshake timeouts, a blackholed IPv6 route). Hermes' own Telegram adapter survives that
with ``telegram_network`` (IPv4-literal fallbacks that keep Host and SNI as ``api.telegram.org``, a
sticky last-good path); :class:`TelegramTransport` applies the same strategy synchronously on top of
``httpx`` and Hermes' helpers (reached only through ``hermes_api.telegram_net``). If those helpers
cannot be loaded the client falls back to plain stdlib ``urllib`` (logged once).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

API_HOST = "api.telegram.org"
_DISABLE_FALLBACK_ENV = "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS"
_FALLBACK_IPS_ENV = "TELEGRAM_FALLBACK_IPS"
_UNSET = object()
_warned_no_helpers = False


class TelegramError(Exception):
    """A Bot API failure: ``status`` is the HTTP/API error code, ``None`` for transport errors."""

    def __init__(self, status: Optional[int], description: str = "") -> None:
        super().__init__(f"telegram error {status}: {description}" if status else f"telegram error: {description}")
        self.status = status
        self.description = description


class TransportFailure(Exception):
    """No HTTP answer from any path (connect failure on every path, or an error after the request was sent)."""


def _load_net():
    """Hermes' Telegram network helpers (``hermes_api.telegram_net``); raises when Hermes has none."""
    from . import hermes_api

    return hermes_api.telegram_net()


_SEED_IPS = ("149.154.166.110", "149.154.167.220")  # last resort when Hermes' helper bundle has no seed list
_LOCAL_KEEPALIVE = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]


def _resolve_bounded(host: str, timeout_s: float) -> bool:
    """Whether ``host`` resolves within ``timeout_s`` (``getaddrinfo`` has no timeout of its own).

    The lookup runs in a daemon thread that is abandoned on timeout; an IP literal needs no lookup. The OS
    resolver caches the answer, so the connect that follows does not wait on DNS again.
    """
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    box: list = []

    def run() -> None:
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            box.append(True)
        except OSError:
            box.append(False)

    worker = threading.Thread(target=run, name="ke-telegram-dns", daemon=True)
    worker.start()
    worker.join(max(0.0, timeout_s))
    return bool(box and box[0])


class _Pool:
    """One path's httpx transport plus a user count, so a failed pool is closed only when idle."""

    __slots__ = ("transport", "users", "retired")

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.users = 0
        self.retired = False

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass


class TelegramTransport:
    """Synchronous POST with Hermes' fallback strategy: sticky path, IPv4 literals, hostname last.

    Order per call: the last path that worked first, then the fallback IPv4 literals (``TELEGRAM_FALLBACK_IPS``
    or Hermes' seed list), the dual-stack hostname last (a blackholed IPv6 route never errors, it only hangs).
    A literal is reached with ``Host`` and the TLS server name still ``api.telegram.org``. Only connect-phase
    failures (connect error/timeout, TLS handshake timeout, connection reset, DNS that does not answer in time)
    move on to the next path: an error after the request went out is NOT retried, it could duplicate a message.
    Fallback IPs are rediscovered (Hermes' DNS-over-HTTPS) only after a whole walk failed, at most hourly
    (every 5 min while discovery itself keeps failing).

    Proxy: when Hermes resolves a proxy (``TELEGRAM_PROXY`` / system proxy) the plugin uses ONE proxied path to
    the hostname and never walks IPv4 literals (Hermes' own transport does walk them through the proxy; the
    single path is the deliberately simpler, safer behaviour here).

    Time: one call never takes longer than ``budget_s`` (20 s) across all paths and discovery. Each attempt gets
    ``timeout_s`` clamped to what is left. The hostname is resolved first with a bounded lookup (``DNS_CAP_S``);
    httpx's connect timeout is applied to TCP and to TLS separately, so it is set to half of the attempt's connect
    allowance (``CONNECT_CAP_S``) to keep TCP + TLS inside it; and a watchdog bounds the whole attempt (a slow
    write/read cannot stack on top). Past the watchdog the outcome of the request is unknown, so that is a
    :class:`TransportFailure` without a retry on another path.

    Every Hermes helper is called defensively: a helper that raises (renamed or moved in a Hermes upgrade)
    degrades only that feature, never the send itself.
    """

    DISCOVERY_REFRESH_S = 3600.0
    DISCOVERY_RETRY_S = 300.0
    DISCOVERY_TIMEOUT_S = 4.0
    CONNECT_CAP_S = 6.0
    DNS_CAP_S = 4.0
    MIN_ATTEMPT_S = 1.0

    def __init__(
        self,
        net: Any,
        *,
        timeout_s: float = 10.0,
        budget_s: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
        env: Any = None,
        transport_factory: Optional[Callable[[Optional[str], Any], Any]] = None,
        on_path: Optional[Callable[[str], None]] = None,
        resolver: Callable[[str, float], bool] = _resolve_bounded,
    ) -> None:
        import httpx  # a Hermes dependency; an ImportError here selects the urllib fallback

        self._httpx = httpx
        self._net = net
        self.timeout_s = float(timeout_s)
        self.budget_s = float(budget_s)
        self._clock = clock
        self._env = os.environ if env is None else env
        self._factory = transport_factory or self._default_factory
        self.on_path = on_path
        self._resolver = resolver
        try:
            configured = list(net.parse_ips(self._env.get(_FALLBACK_IPS_ENV)))
        except Exception:
            configured = []
        try:
            seed = list(net.seed_ips)
        except Exception:
            seed = list(_SEED_IPS)
        self._discovery_allowed = not configured
        self._ips: list[str] = list(dict.fromkeys(configured or seed))
        self._disabled = str(self._env.get(_DISABLE_FALLBACK_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}
        self._next_discovery = 0.0
        self._proxy: Any = _UNSET
        self._pools: dict[Optional[str], _Pool] = {}
        self._lock = threading.Lock()
        self._sticky: Any = _UNSET  # _UNSET = none yet, None = the hostname, str = an IPv4 literal

    # -- state ---------------------------------------------------------------
    @property
    def sticky(self) -> Optional[str]:
        """The last path that worked: an IPv4 literal or the hostname; ``None`` before the first success."""
        with self._lock:
            sticky = self._sticky
        if sticky is _UNSET:
            return None
        return API_HOST if sticky is None else str(sticky)

    def _keepalive(self) -> list:
        try:
            return list(self._net.keepalive())
        except Exception:
            return list(_LOCAL_KEEPALIVE)

    def _verify(self) -> Any:
        try:
            return self._net.verify
        except Exception:
            return True

    def _default_factory(self, proxy: Optional[str], verify: Any):
        kwargs: dict[str, Any] = {
            "verify": verify,
            "socket_options": self._keepalive(),
            "limits": self._httpx.Limits(max_connections=4, max_keepalive_connections=2),
        }
        if proxy:
            kwargs["proxy"] = proxy
        return self._httpx.HTTPTransport(**kwargs)

    def _acquire(self, path: Optional[str], proxy: Optional[str]) -> _Pool:
        with self._lock:
            pool = self._pools.get(path)
            if pool is None:
                pool = self._pools[path] = _Pool(self._factory(proxy, self._verify()))
            pool.users += 1
            return pool

    def _release(self, pool: _Pool) -> None:
        with self._lock:
            pool.users -= 1
            idle_retired = pool.retired and pool.users == 0
        if idle_retired:
            pool.close()

    def _retire(self, path: Optional[str], pool: _Pool) -> None:
        """Drop a failed path's pool (a peer-closed connect leaves half-closed sockets in it); it is closed
        now when idle, otherwise by its last user, never under another thread's request."""
        with self._lock:
            if self._pools.get(path) is pool:
                del self._pools[path]
            pool.retired = True
            idle = pool.users == 0
        if idle:
            pool.close()

    # -- paths ---------------------------------------------------------------
    def _route(self, host: str) -> tuple[Optional[str], bool]:
        """``(proxy, walkable)``: the proxy to use and whether this call may walk IPv4 fallbacks."""
        if host != API_HOST:
            return None, False
        with self._lock:
            proxy, ips = self._proxy, list(self._ips)
        if proxy is _UNSET:
            try:
                proxy = self._net.proxy_url([API_HOST, *ips]) or None
            except Exception:
                proxy = None
            with self._lock:
                self._proxy = proxy
        return proxy, not (proxy or self._disabled)

    def _order(self, walkable: bool) -> list:
        if not walkable:
            return [None]
        with self._lock:
            sticky, ips = self._sticky, list(self._ips)
        order: list = []
        if sticky is not _UNSET:
            order.append(sticky)
        order.extend(ip for ip in ips if ip not in order)
        if None not in order:
            order.append(None)
        return order

    def _retryable(self, exc: Exception) -> bool:
        httpx = self._httpx
        if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
            return True  # always: a broken Hermes helper must not switch the fallback off
        try:
            if self._net.retryable(exc):
                return True
        except Exception:
            pass
        # A TLS handshake timeout or reset that escaped httpx's own exception mapping.
        return isinstance(exc, (ssl.SSLError, ConnectionError)) and not isinstance(exc, httpx.HTTPError)

    def _request(self, path: Optional[str], url: str, body: bytes, attempt_s: float):
        httpx = self._httpx
        parsed = httpx.URL(url)
        # httpx applies the connect timeout to the TCP connect and to the TLS handshake separately.
        connect = min(attempt_s, self.CONNECT_CAP_S) / 2
        headers = {"Content-Type": "application/json"}
        extensions: dict[str, Any] = {
            "timeout": {"connect": connect, "read": attempt_s, "write": attempt_s, "pool": attempt_s}
        }
        if path is not None:  # IPv4 literal: Host and SNI keep the hostname (like ``curl --resolve``)
            headers["Host"] = parsed.host
            extensions["sni_hostname"] = parsed.host
            parsed = parsed.copy_with(host=path)
        return httpx.Request("POST", parsed, headers=headers, content=body, extensions=extensions)

    def _send(self, pool: _Pool, request: Any) -> tuple[int, bytes]:
        response = pool.transport.handle_request(request)
        try:
            data = response.read()
        finally:
            try:
                response.close()
            except Exception:
                pass
        return int(response.status_code), data

    def _try(self, path: Optional[str], url: str, body: bytes, proxy: Optional[str], attempt_s: float):
        """One attempt, hard-bounded to ``attempt_s``: ``(status, body)``; raises the underlying exception.

        Runs in a helper thread joined with the allowance; an overrun abandons the thread (its own httpx
        timeouts end it; its pool is released and closed then) and raises :class:`TransportFailure`.
        """
        pool = self._acquire(path, proxy)
        box: list = []

        def run() -> None:
            try:
                box.append((True, self._send(pool, self._request(path, url, body, attempt_s))))
            except BaseException as exc:  # handed to the caller's thread below
                box.append((False, exc))
            finally:
                self._release(pool)

        worker = threading.Thread(target=run, name="ke-telegram-send", daemon=True)
        worker.start()
        worker.join(attempt_s)
        if not box:
            raise TransportFailure(f"no answer within {attempt_s:.0f}s on {path or API_HOST}") from None
        ok, value = box[0]
        if ok:
            return value
        raise value

    def _succeeded(self, path: Optional[str], failed: bool) -> None:
        with self._lock:
            if self._sticky is not _UNSET and self._sticky == path:
                return
            self._sticky = path
            label = API_HOST if path is None else path
            (log.warning if failed else log.info)("kanban-empowered: Telegram path is now %s", label)
            if self.on_path:  # under the lock: two threads cannot report their paths out of order
                try:
                    self.on_path(label)
                except Exception:
                    pass

    def _discover(self, tried: set, remaining: float) -> list:
        """New fallback IPs from Hermes' discovery after a failed walk (refresh-limited), minus ``tried``."""
        with self._lock:
            due = self._discovery_allowed and self._clock() >= self._next_discovery
        if not due or remaining < 2 * self.MIN_ATTEMPT_S:
            return []
        try:
            found = self._net.discover(min(self.DISCOVERY_TIMEOUT_S, remaining - self.MIN_ATTEMPT_S))
        except Exception:
            found = None
        with self._lock:
            self._next_discovery = self._clock() + (self.DISCOVERY_REFRESH_S if found else self.DISCOVERY_RETRY_S)
            if found:
                self._ips = list(dict.fromkeys([*found, *self._ips]))
        return [ip for ip in (found or []) if ip not in tried]

    # -- the call ------------------------------------------------------------
    def post(self, url: str, body: bytes) -> tuple[int, bytes]:
        """POST ``body`` to ``url``; ``(status, body)`` for any HTTP answer, else :class:`TransportFailure`."""
        deadline = self._clock() + self.budget_s
        host = urllib.parse.urlsplit(url).hostname or ""
        proxy, walkable = self._route(host)
        walkable = walkable and url.lower().startswith("https://")
        queue: list = self._order(walkable)
        tried: set = set()
        last: Optional[Exception] = None
        failed_paths = 0
        discovered = False
        while True:
            while queue:
                path = queue.pop(0)
                if self._clock() > deadline - self.MIN_ATTEMPT_S:
                    queue.clear()
                    break
                tried.add(path)
                try:
                    if path is None and not proxy:  # the hostname path: DNS first, bounded
                        dns_s = min(self.DNS_CAP_S, deadline - self._clock() - self.MIN_ATTEMPT_S)
                        if dns_s <= 0 or not self._resolver(host, dns_s):
                            raise self._httpx.ConnectTimeout(f"{host} did not resolve within {max(dns_s, 0):.0f}s")
                    attempt_s = min(self.timeout_s, deadline - self._clock())
                    status, data = self._try(path, url, body, proxy, attempt_s)
                except Exception as exc:
                    last = exc
                    if not self._retryable(exc):
                        raise TransportFailure(f"{type(exc).__name__}: {exc}") from None
                    failed_paths += 1
                    log.warning("kanban-empowered: Telegram path %s failed (%s)", path or API_HOST, type(exc).__name__)
                    with self._lock:
                        pool = self._pools.get(path)
                        if self._sticky is not _UNSET and self._sticky == path:
                            self._sticky = _UNSET  # re-walk the literals before trusting it again
                    if pool is not None:
                        self._retire(path, pool)
                    continue
                self._succeeded(path, failed=failed_paths > 0)
                return status, data
            if not walkable or discovered or last is None:
                break
            discovered = True
            queue = self._discover(tried, deadline - self._clock())
            if not queue:
                break
        if last is None:
            raise TransportFailure("no time left to try the Telegram API")
        raise TransportFailure(f"{type(last).__name__}: {last} (after {failed_paths} path(s))") from None


def read_path_file(path: Path) -> Optional[str]:
    """The last-good path a client recorded: ``None`` when there is no (readable) file, ``""`` when empty.

    Reads at most 256 bytes and never raises on a corrupt file (invalid UTF-8 decodes with replacement
    characters and classifies as unknown)."""
    try:
        with open(path, "rb") as fh:
            return fh.read(256).decode("utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return None


def classify_path(value: str) -> str:
    """``"ip"``, ``"host"`` or ``"unknown"`` for a recorded path value."""
    try:
        ipaddress.ip_address(value)
        return "ip"
    except ValueError:
        return "host" if value == API_HOST else "unknown"


def _default_transport(timeout_s: float) -> Optional[TelegramTransport]:
    """The resilient transport, or ``None`` (plain urllib, warned once) when Hermes' helpers or httpx are missing."""
    global _warned_no_helpers
    try:
        return TelegramTransport(_load_net(), timeout_s=timeout_s)
    except Exception as exc:
        if not _warned_no_helpers:
            _warned_no_helpers = True
            log.warning(
                "kanban-empowered: Hermes' Telegram network helpers are unavailable (%s: %s); "
                "using plain urllib without IPv4 fallback",
                type(exc).__name__, exc,
            )
        return None


class TelegramClient:
    def __init__(
        self,
        token: str,
        base: str = "https://api.telegram.org",
        timeout_s: float = 10,
        opener: Optional[Callable[..., Any]] = None,
        transport: Optional[TelegramTransport] = None,
        path_file: Optional[Path] = None,
    ) -> None:
        self._token = token
        self.base = base.rstrip("/")
        self.timeout_s = timeout_s
        self._path_file = Path(path_file) if path_file else None
        # An explicit ``opener`` selects the plain urllib path (tests, embedding); otherwise the resilient
        # transport, with urllib as the fail-soft fallback when Hermes' helpers cannot be loaded.
        self._transport: Optional[TelegramTransport] = None
        if opener is None:
            self._transport = transport or _default_transport(timeout_s)
        elif transport is not None:
            self._transport = transport
        self._opener = opener or urllib.request.urlopen
        if self._transport is not None and self._path_file is not None:
            previous = self._transport.on_path

            def remember(label: str) -> None:
                if previous:
                    previous(label)
                self._remember_path(label)

            self._transport.on_path = remember

    def _remember_path(self, label: str) -> None:
        """Record the last-good path where ``doctor`` (another process) can read it. Never raises."""
        tmp = self._path_file.with_name(f"{self._path_file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            self._path_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(label, encoding="utf-8")
            os.replace(tmp, self._path_file)  # atomic: doctor never reads a half-written value
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass

    @property
    def bot_id(self) -> Optional[int]:
        """The bot's own user id (the numeric prefix of the token ``<id>:<secret>``), or ``None``."""
        head = (self._token or "").split(":", 1)[0].strip()
        return int(head) if head.isdigit() else None

    # -- Bot API methods ---------------------------------------------------
    def send(self, chat_id: int, text: str, reply_to: Optional[int] = None) -> int:
        """Send ``text`` (plain, no parse mode) and return the new ``message_id``."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        result = self._call("sendMessage", payload)
        try:
            return int(result["message_id"])
        except (TypeError, KeyError, ValueError):
            raise TelegramError(None, "sendMessage response has no message_id") from None

    def edit(self, chat_id: int, message_id: int, text: str) -> None:
        self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})

    def pin(self, chat_id: int, message_id: int) -> None:
        """Pin silently (``disable_notification``)."""
        self._call(
            "pinChatMessage",
            {"chat_id": chat_id, "message_id": message_id, "disable_notification": True},
        )

    # -- transport ---------------------------------------------------------
    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def _post_urllib(self, url: str, body: bytes) -> tuple[int, bytes, str]:
        """Plain urllib POST: ``(status, body, reason)``; HTTP errors are answers too (the Bot API sends JSON)."""
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            resp = self._opener(req, timeout=self.timeout_s)
            try:
                return 200, resp.read(), ""
            finally:
                close = getattr(resp, "close", None)
                if close:
                    close()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(), str(exc.reason or exc)
            except Exception:
                return exc.code, b"", str(exc.reason or exc)
            finally:
                try:
                    exc.close()
                except Exception:
                    pass

    def _call(self, method: str, payload: dict) -> Any:
        url = f"{self.base}/bot{self._token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        reason = ""
        try:
            if self._transport is not None:
                status, raw = self._transport.post(url, body)
            else:
                status, raw, reason = self._post_urllib(url, body)
        except TelegramError:
            raise
        except Exception as exc:  # timeouts, DNS, connection resets, every path failed
            detail = str(exc) if isinstance(exc, TransportFailure) else f"{type(exc).__name__}: {exc}"
            raise TelegramError(None, self._scrub(detail)) from None
        if status >= 400:  # the Bot API answers errors with a JSON body too
            description = reason or _reason(status)
            try:
                data = json.loads(raw.decode("utf-8"))
                if isinstance(data, dict):
                    description = str(data.get("description") or description)
            except Exception:
                pass
            raise TelegramError(status, self._scrub(description))
        try:
            data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except ValueError:
            raise TelegramError(None, "invalid JSON from the Bot API") from None
        if not isinstance(data, dict) or not data.get("ok"):
            data = data if isinstance(data, dict) else {}
            try:
                code = int(data.get("error_code"))
            except (TypeError, ValueError):
                code = None
            raise TelegramError(code or None, self._scrub(str(data.get("description", ""))))
        return data.get("result")


def _reason(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return f"HTTP {status}"
