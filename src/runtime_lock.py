"""Cross-process ownership and maintenance locks used by SkyWatch services.

The lock payload includes both a PID and a process-start fingerprint.  A PID by
itself is not sufficient because operating systems may reuse it after a crash.
Lock files are removed only when their exact payload is still unchanged.
"""

from __future__ import annotations

import ctypes
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _windows_process_fingerprint(pid: int) -> Optional[str]:
    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(process_query_limited_information, 0, pid)
    if not handle:
        return None
    try:
        creation = ctypes.c_ulonglong()
        exit_time = ctypes.c_ulonglong()
        kernel_time = ctypes.c_ulonglong()
        user_time = ctypes.c_ulonglong()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        return f"win:{creation.value}"
    finally:
        kernel32.CloseHandle(handle)


def process_fingerprint(pid: int) -> Optional[str]:
    """Return an immutable process-start fingerprint when the OS exposes one."""

    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_fingerprint(pid)
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        # Field 22 is the process start time.  The command field may contain
        # spaces and parentheses, so split only after the final ``)``.
        raw = stat_path.read_text(encoding="utf-8")
        tail = raw.rsplit(")", 1)[1].strip().split()
        return f"proc:{tail[19]}"
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(process_query_limited_information, 0, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class LockOwner:
    pid: int
    token: str
    fingerprint: Optional[str]
    service: str
    created_at: str

    @classmethod
    def current(cls, service: str) -> "LockOwner":
        pid = os.getpid()
        return cls(
            pid=pid,
            token=uuid.uuid4().hex,
            fingerprint=process_fingerprint(pid),
            service=service,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def to_bytes(self) -> bytes:
        # Do not append a newline.  Windows opens low-level ``os.open`` file
        # descriptors in text mode unless O_BINARY is requested, which would
        # otherwise translate LF to CRLF and break exact-owner release checks.
        return json.dumps(
            {
                "version": 1,
                "pid": self.pid,
                "token": self.token,
                "fingerprint": self.fingerprint,
                "service": self.service,
                "created_at": self.created_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def parse_lock_owner(raw: bytes | str) -> Optional[LockOwner]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    if not text:
        return None
    try:
        payload: Any = json.loads(text)
        if isinstance(payload, dict):
            return LockOwner(
                pid=int(payload["pid"]),
                token=str(payload.get("token") or "legacy"),
                fingerprint=(str(payload["fingerprint"]) if payload.get("fingerprint") else None),
                service=str(payload.get("service") or "unknown"),
                created_at=str(payload.get("created_at") or ""),
            )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass

    # Backward compatibility with ``123:token`` and ``pid=123 started=...``.
    try:
        pid_text = text.split(":", 1)[0]
        if pid_text.startswith("pid="):
            pid_text = pid_text.split("=", 1)[1].split(None, 1)[0]
        return LockOwner(int(pid_text), "legacy", None, "legacy", "")
    except (TypeError, ValueError):
        return None


def lock_owner_alive(path: Path) -> bool:
    try:
        owner = parse_lock_owner(path.read_bytes())
    except (FileNotFoundError, OSError):
        return False
    if owner is None or not pid_alive(owner.pid):
        return False
    if owner.fingerprint:
        current = process_fingerprint(owner.pid)
        return current is not None and current == owner.fingerprint
    return True


class ProcessLock:
    """Atomic PID/start-fingerprint lock with safe stale-owner recovery."""

    def __init__(self, path: Path, service: str) -> None:
        self.path = Path(path)
        self.owner = LockOwner.current(service)
        self._payload = self.owner.to_bytes()
        self._held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                descriptor = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(descriptor, self._payload)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._held = True
                return True
            except FileExistsError:
                if attempt or lock_owner_alive(self.path):
                    return False
                try:
                    observed = self.path.read_bytes()
                    if lock_owner_alive(self.path) or self.path.read_bytes() != observed:
                        return False
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    return False
        return False

    def release(self) -> None:
        if not self._held:
            return
        try:
            if self.path.read_bytes() == self._payload:
                self.path.unlink()
        except (FileNotFoundError, OSError):
            pass
        finally:
            self._held = False

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            raise RuntimeError(f"{self.owner.service} is already running")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def maintenance_active(data_dir: Path) -> bool:
    return lock_owner_alive(Path(data_dir) / "maintenance.lock")


def wait_until_maintenance_complete(data_dir: Path, timeout_seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while maintenance_active(data_dir):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    return True


__all__ = [
    "LockOwner",
    "ProcessLock",
    "lock_owner_alive",
    "maintenance_active",
    "parse_lock_owner",
    "pid_alive",
    "process_fingerprint",
    "wait_until_maintenance_complete",
]
