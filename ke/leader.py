"""Single-leader election between Hermes processes (gateway, dispatcher, CLI).

The lock is one small JSON file ``{"pid", "token", "ts"}``. A process owns it
while it keeps refreshing ``ts`` (heartbeat). Anyone else may take it over once
the heartbeat is older than ``ttl_s`` or the recorded pid no longer exists.
Ownership is tracked by a per-instance ``token`` (not by pid), so two lock
objects in one process still exclude each other.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

try:  # psutil ships in the Hermes venv; stay stdlib-only if it does not
    import psutil as _psutil
except Exception:  # pragma: no cover - depends on the environment
    _psutil = None


def _pid_alive_fallback(pid: int) -> bool:
    """Liveness without psutil.

    NOTE: on Windows ``os.kill(pid, 0)`` is NOT a probe: any signal other than
    CTRL_C/CTRL_BREAK ends up in ``TerminateProcess``, so it would kill the
    process we only wanted to look at. Windows therefore goes through
    ``OpenProcess`` + ``GetExitCodeProcess``.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists, not ours
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def pid_alive(pid: int) -> bool:
    """True if a process with this pid currently exists."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if _psutil is not None:
        try:
            if not _psutil.pid_exists(pid):
                return False
            return _psutil.Process(pid).status() != _psutil.STATUS_ZOMBIE
        except _psutil.NoSuchProcess:
            return False
        except Exception:
            return _pid_alive_fallback(pid)
    return _pid_alive_fallback(pid)


class LeaderLock:
    def __init__(self, path: Path, ttl_s: int = 120) -> None:
        self.path = Path(path)
        self.ttl_s = ttl_s
        self.token = uuid.uuid4().hex

    # -- file helpers ------------------------------------------------------
    def _read(self) -> Optional[dict]:
        """Lock contents; ``None`` when absent or unreadable garbage.

        Raises OSError when the file exists but cannot be read right now
        (e.g. another process is replacing it on Windows).
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _write(self, now: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{self.token}.tmp")
        try:
            tmp.write_text(self._payload(now), encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass  # already moved into place (or never created)

    def _payload(self, now: float) -> str:
        return json.dumps({"pid": os.getpid(), "token": self.token, "ts": now})

    def _create_exclusive(self, now: float) -> bool:
        """Publish the lock only if no file exists yet, with its FULL payload.

        The payload goes to a private tmp file first and is then hard-linked to
        the lock path: ``os.link`` fails with FileExistsError if somebody got
        there first, and a reader can never observe an empty/partial lock file
        (which a plain ``O_CREAT|O_EXCL`` + write would expose, letting a
        concurrent acquirer treat it as corrupt and replace it).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{self.token}.new")
        try:
            tmp.write_text(self._payload(now), encoding="utf-8")
            try:
                os.link(tmp, self.path)
            except FileExistsError:
                return False
            return True
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    def _live_holder(self, data: Optional[dict], now: float) -> bool:
        """Is ``data`` a lock somebody else still validly holds?"""
        if not data:
            return False
        try:
            fresh = now - float(data["ts"]) <= self.ttl_s
            pid = int(data["pid"])
        except (KeyError, TypeError, ValueError):
            return False
        return fresh and pid_alive(pid)

    # -- API ---------------------------------------------------------------
    def acquire(self, now: float) -> bool:
        """Take the lock if free/stale/orphaned, or refresh it if already ours."""
        try:
            if not self.path.exists() and self._create_exclusive(now):
                return True
            # Lock exists (possibly created just now by a rival): judge it by its content.
            data = self._read()
            if data and data.get("token") != self.token and self._live_holder(data, now):
                return False
            self._write(now)  # ours, stale, orphaned or corrupt: replace
            # Residual (accepted): two acquirers that both judge the SAME lock stale can
            # both replace it; the read-back below makes the earlier writer back off
            # unless the later replace lands after its read. The heartbeat TTL bounds it.
            check = self._read()  # lost a takeover race if someone wrote after us
            return bool(check and check.get("token") == self.token)
        except OSError:
            return False

    def renew(self, now: float) -> None:
        """Refresh the heartbeat; a no-op unless this instance holds the lock."""
        try:
            data = self._read()
            if data and data.get("token") == self.token:
                self._write(now)
        except OSError:
            pass

    def release(self) -> None:
        """Drop the lock; a no-op unless this instance holds it."""
        try:
            data = self._read()
            if data and data.get("token") == self.token:
                self.path.unlink()
        except OSError:
            pass

    def holder_pid(self, now: float) -> Optional[int]:
        """Pid of the live leader (fresh heartbeat, running process), else ``None``."""
        try:
            data = self._read()
        except OSError:
            return None
        if not self._live_holder(data, now):
            return None
        return int(data["pid"])  # type: ignore[index]
