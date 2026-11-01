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


class TelegramTransport:
    """Synchronous POST with Hermes' fallback strategy: sticky path, IPv4 literals, hostname last.

    Order per call: the last path that worked first, then the fallback IPv4 literals (``TELEGRAM_FALLBACK_IPS``
    or Hermes' seed list), the dual-stack hostname last (a blackholed IPv6 route never errors, it only hangs).
    A literal is reached with ``Host`` and the TLS server name still ``api.telegram.org``. Only connect-phase
    failures (connect error/timeout, TLS handshake timeout, connection reset) move on to the next path: an
    error after the request went out is NOT retried, it could duplicate a message. Fallback IPs are
    rediscovered (Hermes' DNS-over-HTTPS) only after a whole walk failed, at most hourly (every 5 min while
    discovery itself keeps failing). A proxy (``TELEGRAM_PROXY``, via Hermes) owns the route: single path.
    One call never takes longer than ``budget_s`` across all paths; each attempt gets ``timeout_s`` clamped to
    what is left of it.
    """

    DISCOVERY_REFRESH_S = 3600.0
    DISCOVERY_RETRY_S = 300.0
    DISCOVERY_TIMEOUT_S = 4.0
    CONNECT_CAP_S = 6.0
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
        configured = net.parse_ips(self._env.get(_FALLBACK_IPS_ENV))
        self._discovery_allowed = not configured
        self._ips: list[str] = list(dict.fromkeys(configured or net.seed_ips))
        self._disabled = str(self._env.get(_DISABLE_FALLBACK_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}
        self._next_discovery = 0.0
        self._proxy: Any = _UNSET
        self._transports: dict[Optional[str], Any] = {}
        self._lock = threading.Lock()
        self._sticky: Any = _UNSET  # _UNSET = none yet, None = the hostname, str = an IPv4 literal

    # -- state ---------------------------------------------------------------
    @property
    def sticky(self) -> Optional[str]:
        """The last path that worked: an IPv4 literal or the hostname; ``None`` before the first success."""
        sticky = self._sticky
        if sticky is _UNSET:
            return None
        return API_HOST if sticky is None else str(sticky)

    def _default_factory(self, proxy: Optional[str], verify: Any):
        kwargs: dict[str, Any] = {
            "verify": verify,
            "socket_options": self._net.keepalive(),
            "limits": self._httpx.Limits(max_connections=4, max_keepalive_connections=2),
        }
        if proxy:
            kwargs["proxy"] = proxy
        return self._httpx.HTTPTransport(**kwargs)

    def _transport(self, path: Optional[str], proxy: Optional[str]):
        with self._lock:
            transport = self._transports.get(path)
            if transport is None:
                transport = self._factory(proxy, self._net.verify)
                self._transports[path] = transport
            return transport

    def _discard(self, path: Optional[str]) -> None:
        """Drop the pool of a failed path: a peer-closed connect leaves half-closed sockets in it."""
        with self._lock:
            transport = self._transports.pop(path, None)
        close = getattr(transport, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass

    # -- paths ---------------------------------------------------------------
    def _route(self, host: str) -> tuple[Optional[str], bool]:
        """``(proxy, walkable)``: the proxy to use and whether this call may walk IPv4 fallbacks."""
        if host != API_HOST:
            return None, False
        if self._proxy is _UNSET:
            try:
                self._proxy = self._net.proxy_url([API_HOST, *self._ips]) or None
            except Exception:
                self._proxy = None
        return self._proxy, not (self._proxy or self._disabled)

    def _order(self, walkable: bool) -> list[Optional[str]]:
        if not walkable:
            return [None]
        order: list[Optional[str]] = []
        if self._sticky is not _UNSET:
            order.append(self._sticky)
        order.extend(ip for ip in self._ips if ip not in order)
        if None not in order:
            order.append(None)
        return order

    def _retryable(self, exc: Exception) -> bool:
        try:
            if self._net.retryable(exc):
                return True
        except Exception:
            pass
        # A TLS handshake timeout or reset that escaped httpx's own exception mapping.
        return isinstance(exc, (ssl.SSLError, ConnectionError)) and not isinstance(exc, self._httpx.HTTPError)

    def _request(self, path: Optional[str], url: str, body: bytes, timeout_s: float):
        httpx = self._httpx
        parsed = httpx.URL(url)
        headers = {"Content-Type": "application/json"}
        extensions: dict[str, Any] = {
            "timeout": {"connect": min(timeout_s, self.CONNECT_CAP_S), "read": timeout_s, "write": timeout_s, "pool": timeout_s}
        }
        if path is not None:  # IPv4 literal: Host and SNI keep the hostname (like ``curl --resolve``)
            headers["Host"] = parsed.host
            extensions["sni_hostname"] = parsed.host
            parsed = parsed.copy_with(host=path)
        return httpx.Request("POST", parsed, headers=headers, content=body, extensions=extensions)

    def _try(self, path: Optional[str], url: str, body: bytes, proxy: Optional[str], deadline: float):
        """One attempt: ``(status, body)``; raises the underlying exception."""
        remaining = deadline - self._clock()
        request = self._request(path, url, body, min(self.timeout_s, remaining))
        response = self._transport(path, proxy).handle_request(request)
        try:
            data = response.read()
        finally:
            try:
                response.close()
            except Exception:
                pass
        return int(response.status_code), data

    def _succeeded(self, path: Optional[str], failed: bool) -> None:
        with self._lock:
            changed = self._sticky is _UNSET or self._sticky != path
            self._sticky = path
        if not changed:
            return
        label = API_HOST if path is None else path
        (log.warning if failed else log.info)("kanban-empowered: Telegram path is now %s", label)
        if self.on_path:
            try:
                self.on_path(label)
            except Exception:
                pass

    def _discover(self, tried: set, remaining: float) -> list:
        """New fallback IPs from Hermes' discovery after a failed walk (refresh-limited), minus ``tried``."""
        if not self._discovery_allowed or self._clock() < self._next_discovery or remaining < 2 * self.MIN_ATTEMPT_S:
            return []
        try:
            found = self._net.discover(min(self.DISCOVERY_TIMEOUT_S, remaining - self.MIN_ATTEMPT_S))
        except Exception:
            found = None
        self._next_discovery = self._clock() + (self.DISCOVERY_REFRESH_S if found else self.DISCOVERY_RETRY_S)
        if not found:
            return []
        self._ips = list(dict.fromkeys([*found, *self._ips]))
        return [ip for ip in found if ip not in tried]

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
                    status, data = self._try(path, url, body, proxy, deadline)
                except Exception as exc:
                    last = exc
                    if not self._retryable(exc):
                        raise TransportFailure(f"{type(exc).__name__}: {exc}") from None
                    failed_paths += 1
                    log.warning("kanban-empowered: Telegram path %s failed (%s)", path or API_HOST, type(exc).__name__)
                    self._discard(path)
                    with self._lock:
                        if self._sticky is not _UNSET and self._sticky == path:
                            self._sticky = _UNSET  # re-walk the literals before trusting it again
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
    """The last-good path a client recorded (``None`` when there is no file, ``""`` when it is empty)."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
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
        try:
            self._path_file.parent.mkdir(parents=True, exist_ok=True)
            self._path_file.write_text(label, encoding="utf-8")
        except Exception:
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
