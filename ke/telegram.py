"""Minimal Telegram Bot API client: ``sendMessage``, ``editMessageText`` and ``pinChatMessage``.

Outbound only. The plugin never polls (no ``getUpdates``): replies reach it through the
gateway's ``pre_gateway_dispatch`` hook. Stdlib ``urllib`` only, JSON POSTs, no retries
(the coordinator simply tries again on a later evaluation). Every failure surfaces as
:class:`TelegramError`; the bot token never appears in its message.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional


class TelegramError(Exception):
    """A Bot API failure: ``status`` is the HTTP/API error code, ``None`` for transport errors."""

    def __init__(self, status: Optional[int], description: str = "") -> None:
        super().__init__(f"telegram error {status}: {description}" if status else f"telegram error: {description}")
        self.status = status
        self.description = description


class TelegramClient:
    def __init__(
        self,
        token: str,
        base: str = "https://api.telegram.org",
        timeout_s: float = 10,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self._token = token
        self.base = base.rstrip("/")
        self.timeout_s = timeout_s
        self._opener = opener

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

    def _call(self, method: str, payload: dict) -> Any:
        req = urllib.request.Request(
            f"{self.base}/bot{self._token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            resp = self._opener(req, timeout=self.timeout_s)
            try:
                body = resp.read()
            finally:
                close = getattr(resp, "close", None)
                if close:
                    close()
        except urllib.error.HTTPError as exc:  # the Bot API answers errors with a JSON body too
            status, description = exc.code, str(exc.reason or exc)
            try:
                data = json.loads(exc.read().decode("utf-8"))
                if isinstance(data, dict):
                    description = str(data.get("description") or description)
            except Exception:
                pass
            raise TelegramError(status, self._scrub(description)) from None
        except TelegramError:
            raise
        except Exception as exc:  # timeouts, DNS, connection resets
            raise TelegramError(None, self._scrub(f"{type(exc).__name__}: {exc}")) from None
        try:
            data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except ValueError:
            raise TelegramError(None, "invalid JSON from the Bot API") from None
        if not isinstance(data, dict) or not data.get("ok"):
            data = data if isinstance(data, dict) else {}
            code = data.get("error_code")
            raise TelegramError(int(code) if code else None, self._scrub(str(data.get("description", ""))))
        return data.get("result")
