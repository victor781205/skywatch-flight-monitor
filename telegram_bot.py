"""Telegram command service for SkyWatch.

This process is intentionally separate from Streamlit and the scheduler.  It
uses Telegram Bot API long polling, persists the next update offset before
executing a command, and therefore gives costly commands at-most-once handling
across process restarts.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

from src import database as db
from src.config import (
    DATA_DIR,
    SCHEDULER_HEARTBEAT_STALE_MINUTES,
    SERPAPI_MONTHLY_LIMIT,
    TELEGRAM_ALLOWED_CHAT_IDS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_COMMANDS_ENABLED,
)
from src.monitor import check_all_monitors
from src.runtime_lock import (
    ProcessLock,
    lock_owner_alive,
    maintenance_active,
    wait_until_maintenance_complete,
)
from src.telegram_commands import CheckEstimate, TelegramCommandRouter

ROOT = Path(__file__).resolve().parent
STATUS_FILE = ROOT / "data" / "scheduler_status.json"
OFFSET_FILE = ROOT / "data" / "telegram_offset.json"
LOCK_FILE = ROOT / "data" / "telegram_bot.lock"
WATCHDOG_STATE_FILE = ROOT / "data" / "telegram_watchdog.json"
POLL_TIMEOUT_SECONDS = 25
MAX_BACKOFF_SECONDS = 30

MODE_CALLS = {"lean": 1, "standard": 2, "full": 5}
MODE_LABELS = {"lean": "精簡", "standard": "標準", "full": "完整"}


def _read_scheduler_status(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        return dict(document) if isinstance(document, Mapping) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def scheduler_heartbeat_health(
    status: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    stale_after_minutes: int = SCHEDULER_HEARTBEAT_STALE_MINUTES,
) -> tuple[str, Optional[timedelta]]:
    """Classify the independent scheduler heartbeat without trusting state alone."""
    raw = status.get("updated_at")
    if not raw:
        return "missing", None
    try:
        updated = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return "missing", None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=ZoneInfo("Asia/Taipei"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = max(timedelta(0), current.astimezone(timezone.utc) - updated.astimezone(timezone.utc))
    explicit_error = (
        str(status.get("state") or "").lower() == "error"
        or str(status.get("supervisor_state") or "").lower() == "error"
    )
    if explicit_error:
        return "error", age
    if age > timedelta(minutes=max(3, int(stale_after_minutes))):
        return "stale", age
    return "fresh", age


class TelegramAPIError(RuntimeError):
    """A deliberately sanitised Telegram/network failure."""

    def __init__(
        self,
        *,
        retry_after: Optional[float] = None,
        delivery_unknown: bool = False,
    ) -> None:
        super().__init__("Telegram API request failed")
        self.retry_after = retry_after
        self.delivery_unknown = bool(delivery_unknown)


class TelegramAPI:
    """Small synchronous Bot API client with an injectable requests session."""

    def __init__(
        self,
        token: str,
        *,
        session: Optional[requests.Session] = None,
        connect_timeout: float = 5.0,
    ) -> None:
        cleaned = str(token or "").strip()
        if not cleaned:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self._base_url = f"https://api.telegram.org/bot{cleaned}"
        self._session = session or requests.Session()
        self._connect_timeout = max(1.0, float(connect_timeout))

    def _post(self, method: str, payload: Mapping[str, Any], read_timeout: float) -> Any:
        try:
            response = self._session.post(
                f"{self._base_url}/{method}",
                json=dict(payload),
                timeout=(self._connect_timeout, max(1.0, float(read_timeout))),
            )
        except requests.Timeout:
            raise TelegramAPIError(delivery_unknown=method == "sendMessage") from None
        except requests.RequestException:
            # Never propagate a requests exception: its URL contains the token.
            raise TelegramAPIError() from None

        try:
            document = response.json()
        except (ValueError, TypeError):
            document = {}

        status_value = getattr(response, "status_code", 200)
        status = status_value if isinstance(status_value, int) else 200
        if status >= 400 or not isinstance(document, Mapping) or document.get("ok") is not True:
            retry_after = None
            parameters = document.get("parameters") if isinstance(document, Mapping) else None
            if isinstance(parameters, Mapping):
                try:
                    retry_after = max(0.0, float(parameters.get("retry_after")))
                except (TypeError, ValueError):
                    retry_after = None
            if retry_after is None:
                try:
                    retry_after = max(0.0, float(response.headers.get("Retry-After")))
                except (AttributeError, TypeError, ValueError):
                    retry_after = None
            raise TelegramAPIError(retry_after=retry_after)
        return document.get("result")

    def get_updates(self, offset: int, *, timeout: int = POLL_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
        result = self._post(
            "getUpdates",
            {
                "offset": max(0, int(offset)),
                "timeout": max(1, int(timeout)),
                "allowed_updates": ["message"],
            },
            read_timeout=max(5, int(timeout) + 10),
        )
        if not isinstance(result, list):
            raise TelegramAPIError()
        return [dict(item) for item in result if isinstance(item, Mapping)]

    def send_message(self, chat_id: str | int, text: str) -> Any:
        # sendMessage is deliberately attempted once.  Retrying after an
        # ambiguous network timeout can create a duplicate Telegram message.
        return self._post(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": str(text),
                "disable_web_page_preview": True,
            },
            read_timeout=15,
        )


class OffsetStore:
    """Atomic persistence for the next Telegram update id."""

    def __init__(self, path: Path = OFFSET_FILE) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._next_offset = self._read()

    def _read(self) -> int:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            value = document.get("next_offset") if isinstance(document, Mapping) else document
            return max(0, int(value))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return 0

    @property
    def next_offset(self) -> int:
        with self._lock:
            return self._next_offset

    def advance_to(self, next_offset: int) -> int:
        with self._lock:
            candidate = max(self._next_offset, max(0, int(next_offset)))
            if candidate == self._next_offset:
                return self._next_offset
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temp_path.write_text(
                json.dumps({"next_offset": candidate}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
            self._next_offset = candidate
            return candidate


@dataclass(frozen=True)
class WatchdogEvent:
    phase: str
    health: str
    message: str
    recipients: tuple[str, ...]


class SchedulerHeartbeatWatchdog:
    """Persist scheduler incidents and per-chat delivery acknowledgements."""

    def __init__(
        self,
        *,
        status_path: Path = STATUS_FILE,
        state_path: Path = WATCHDOG_STATE_FILE,
        stale_after_minutes: int = SCHEDULER_HEARTBEAT_STALE_MINUTES,
        startup_grace_seconds: float = 60.0,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self.status_path = Path(status_path)
        self.state_path = Path(state_path)
        self.stale_after_minutes = max(3, int(stale_after_minutes))
        self.startup_grace_seconds = max(0.0, float(startup_grace_seconds))
        self.now_func = now_func
        self.monotonic_func = monotonic_func
        self._started = self.monotonic_func()
        self._state = self._read_state()

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "phase": "none",
            "health": "unknown",
            "recipients": [],
            "delivered": [],
            "delivery_unknown": [],
        }

    def _read_state(self) -> dict[str, Any]:
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(document, Mapping):
                return self._empty_state()
            phase = str(document.get("phase") or "none")
            if phase not in {"none", "incident", "recovery"}:
                phase = "incident" if document.get("alerted") else "none"
            return {
                "phase": phase,
                "health": str(document.get("health") or "unknown"),
                "recipients": sorted(
                    {str(item) for item in document.get("recipients") or [] if str(item)}
                ),
                "delivered": sorted(
                    {str(item) for item in document.get("delivered") or [] if str(item)}
                ),
                "delivery_unknown": sorted(
                    {
                        str(item)
                        for item in document.get("delivery_unknown") or []
                        if str(item)
                    }
                ),
            }
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return self._empty_state()

    def _persist(self, state: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        document = {
            "phase": str(state.get("phase") or "none"),
            "health": str(state.get("health") or "unknown"),
            "recipients": sorted({str(item) for item in state.get("recipients") or []}),
            "delivered": sorted({str(item) for item in state.get("delivered") or []}),
            "delivery_unknown": sorted(
                {str(item) for item in state.get("delivery_unknown") or []}
            ),
            "updated_at": self.now_func().isoformat(timespec="seconds"),
        }
        temporary.write_text(
            json.dumps(document, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)
        self._state = document

    def poll(
        self,
        alert_chat_ids: set[str] | list[str] | tuple[str, ...],
    ) -> Optional[WatchdogEvent]:
        if self.monotonic_func() - self._started < self.startup_grace_seconds:
            return None
        recipients = tuple(
            sorted({str(item).strip() for item in alert_chat_ids if str(item).strip()})
        )
        if not recipients:
            return None
        status = _read_scheduler_status(self.status_path)
        health, age = scheduler_heartbeat_health(
            status,
            now=self.now_func(),
            stale_after_minutes=self.stale_after_minutes,
        )
        problem = health in {"missing", "stale", "error"}
        phase = str(self._state.get("phase") or "none")
        if problem:
            if phase != "incident":
                self._persist(
                    {
                        "phase": "incident",
                        "health": health,
                        "recipients": recipients,
                        "delivered": [],
                        "delivery_unknown": [],
                    }
                )
            else:
                merged = sorted(set(self._state.get("recipients") or []) | set(recipients))
                if health != self._state.get("health") or merged != self._state.get("recipients"):
                    updated = dict(self._state)
                    updated.update({"health": health, "recipients": merged})
                    self._persist(updated)
            if health == "error":
                detail = "排程器回報執行錯誤。"
            elif age is None:
                detail = "找不到有效的排程器 heartbeat。"
            else:
                detail = f"已超過 {int(age.total_seconds() // 60)} 分鐘沒有更新。"
            pending = tuple(
                item
                for item in self._state["recipients"]
                if item not in set(self._state.get("delivered") or [])
                and item not in set(self._state.get("delivery_unknown") or [])
            )
            return WatchdogEvent(
                phase="incident",
                health=health,
                message=(
                    "⚠️ SkyWatch 排程器異常\n"
                    f"{detail}\n"
                    "票價排程可能已停止，請檢查 scheduler 服務與執行紀錄。"
                ),
                recipients=pending,
            ) if pending else None
        if phase == "incident":
            self._persist(
                {
                    "phase": "recovery",
                    "health": health,
                    "recipients": recipients,
                    "delivered": [],
                    "delivery_unknown": [],
                }
            )
            phase = "recovery"
        if phase != "recovery":
            return None
        merged = sorted(set(self._state.get("recipients") or []) | set(recipients))
        if merged != self._state.get("recipients"):
            updated = dict(self._state)
            updated["recipients"] = merged
            self._persist(updated)
        pending = tuple(
            item
            for item in self._state["recipients"]
            if item not in set(self._state.get("delivered") or [])
            and item not in set(self._state.get("delivery_unknown") or [])
        )
        return WatchdogEvent(
            phase="recovery",
            health=health,
            message="✅ SkyWatch 排程器 heartbeat 已恢復，漏跑監督與補跑服務重新上線。",
            recipients=pending,
        ) if pending else None

    def mark_delivered(self, event: WatchdogEvent, chat_id: str | int) -> None:
        """Acknowledge one recipient only after Telegram confirms success."""
        if str(self._state.get("phase")) != event.phase:
            return
        recipient = str(chat_id).strip()
        if recipient not in set(self._state.get("recipients") or []):
            return
        updated = dict(self._state)
        updated["delivered"] = sorted(
            set(self._state.get("delivered") or []) | {recipient}
        )
        if (
            event.phase == "recovery"
            and (
                set(updated["delivered"])
                | set(updated.get("delivery_unknown") or [])
            ) >= set(updated.get("recipients") or [])
        ):
            updated = self._empty_state()
        self._persist(updated)

    def mark_delivery_unknown(self, event: WatchdogEvent, chat_id: str | int) -> None:
        """Fence an ambiguous send so the watchdog never auto-retries it."""
        if str(self._state.get("phase")) != event.phase:
            return
        recipient = str(chat_id).strip()
        if recipient not in set(self._state.get("recipients") or []):
            return
        updated = dict(self._state)
        updated["delivery_unknown"] = sorted(
            set(self._state.get("delivery_unknown") or []) | {recipient}
        )
        self._persist(updated)


class SingleInstanceLock(ProcessLock):
    """Compatibility name for the shared PID/start-fingerprint process lock."""

    def __init__(self, path: Path = LOCK_FILE) -> None:
        super().__init__(Path(path), "telegram_commands")


class CommandCallbacks:
    """Production callbacks used by :class:`TelegramCommandRouter`."""

    def __init__(
        self,
        *,
        database: Any = db,
        status_path: Path = STATUS_FILE,
        monthly_limit: int = SERPAPI_MONTHLY_LIMIT,
        check_func: Callable[..., list[dict[str, Any]]] = check_all_monitors,
        stale_after_minutes: int = SCHEDULER_HEARTBEAT_STALE_MINUTES,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.database = database
        self.status_path = Path(status_path)
        self.monthly_limit = max(0, int(monthly_limit))
        self.check_func = check_func
        self.stale_after_minutes = max(3, int(stale_after_minutes))
        self.now_func = now_func

    def _active_monitors(self) -> list[dict[str, Any]]:
        return list(self.database.list_monitors(active_only=True))

    @staticmethod
    def _mode(monitor: Mapping[str, Any]) -> str:
        value = str(monitor.get("quota_mode") or "full").lower()
        return value if value in MODE_CALLS else "full"

    @classmethod
    def _estimated_calls(cls, monitors: list[dict[str, Any]]) -> int:
        return sum(MODE_CALLS[cls._mode(monitor)] for monitor in monitors)

    def status(self, _context: object) -> str:
        monitors = self._active_monitors()
        status = _read_scheduler_status(self.status_path)
        heartbeat, heartbeat_age = scheduler_heartbeat_health(
            status,
            now=self.now_func(),
            stale_after_minutes=self.stale_after_minutes,
        )

        state = str(status.get("state") or "unknown").lower()
        state_label = {
            "waiting": "🟢 正常待命",
            "healthy": "🟢 最近執行正常",
            "running": "🔵 正在查價",
            "skipped_locked": "🟡 已略過重複工作",
            "error": "🔴 最近執行失敗",
            "unknown": "⚪ 尚無排程心跳",
        }.get(state, "⚪ 狀態未知")
        if heartbeat == "missing":
            state_label = "🔴 找不到排程心跳"
        elif heartbeat == "stale":
            minutes = int((heartbeat_age or timedelta()).total_seconds() // 60)
            state_label = f"🔴 心跳已中斷 {minutes} 分鐘"
        elif heartbeat == "error":
            state_label = "🔴 排程器回報異常"
        lines = [f"排程器：{state_label}", f"啟用監控：{len(monitors)} 筆"]

        if monitors:
            routes = []
            for monitor in monitors[:5]:
                route = f"{monitor.get('origin', '?')}→{monitor.get('destination', '?')}"
                routes.append(f"{route}（{MODE_LABELS[self._mode(monitor)]}）")
            suffix = f"，另 {len(monitors) - 5} 筆" if len(monitors) > 5 else ""
            lines.append(f"監控路線：{'、'.join(routes)}{suffix}")

        if status.get("last_completed_at"):
            lines.append(f"上次完成：{status['last_completed_at']}")
        if status.get("next_run_at"):
            lines.append(f"下次執行：{status['next_run_at']}")
        if str(status.get("supervisor_state") or "").lower() == "error":
            lines.append("監督器：🔴 異常，請查看本機執行紀錄")
        return "\n".join(lines)

    def quota(self, _context: object) -> str:
        summary = self.database.get_api_usage_summary("serpapi", self.monthly_limit)
        monitors = self._active_monitors()
        limit = int(summary.get("limit") or 0)
        used = int(summary.get("used") or 0)
        attempted = int(summary.get("attempted") or 0)
        failed = int(summary.get("failed") or 0)
        remaining = summary.get("remaining")
        lines = [
            f"SerpAPI 本月：{used} / {limit} 次" if limit else f"SerpAPI 本月成功：{used} 次（未設定上限）",
            f"請求紀錄：{attempted} 次（失敗 {failed} 次）",
            f"目前 {len(monitors)} 筆監控，手動完整檢查上限 {self._estimated_calls(monitors)} 次",
        ]
        if remaining is not None:
            lines.insert(1, f"剩餘：{int(remaining)} 次")
        return "\n".join(lines)

    def estimate_check(self, _context: object) -> CheckEstimate:
        monitors = self._active_monitors()
        if not monitors:
            raise RuntimeError("no active monitors")
        counts = Counter(self._mode(monitor) for monitor in monitors)
        calls = self._estimated_calls(monitors)
        breakdown = "、".join(
            f"{MODE_LABELS[mode]} {counts[mode]} 筆"
            for mode in ("lean", "standard", "full")
            if counts[mode]
        )
        summary = self.database.get_api_usage_summary("serpapi", self.monthly_limit)
        remaining = summary.get("remaining")
        detail = f"啟用監控 {len(monitors)} 筆（{breakdown}），這是最保守的單次請求上限。"
        if remaining is not None:
            detail += f" 本月剩餘 {int(remaining)} 次。"
            if calls > int(remaining):
                detail += " ⚠️ 目前剩餘額度不足，實際執行會由額度保護機制阻擋。"
        return CheckEstimate(api_calls=calls, detail=detail)

    def check(self, _context: object) -> str:
        monitor_map = {
            int(item["id"]): item for item in self._active_monitors() if item.get("id") is not None
        }
        results = list(self.check_func(scheduled_report=False))
        succeeded = sum(1 for result in results if result.get("ok"))
        failed = len(results) - succeeded
        hits = sum(1 for result in results if result.get("hit"))
        notified = sum(1 for result in results if result.get("notified"))
        calls = sum(max(0, int(result.get("api_calls") or 0)) for result in results)
        lines = [
            f"完成 {len(results)} 筆：成功 {succeeded}、失敗 {failed}",
            f"命中目標 {hits} 筆、通知 {notified} 筆、實際 API {calls} 次",
        ]
        for result in results[:5]:
            monitor = monitor_map.get(int(result.get("monitor_id") or 0), {})
            route = f"{monitor.get('origin', '?')}→{monitor.get('destination', '?')}"
            if result.get("ok") and result.get("best_price") is not None:
                price = f"NT${float(result['best_price']):,.0f}"
                outcome = "命中" if result.get("hit") else "未達目標"
                lines.append(f"• {route}：{price}（{outcome}）")
            else:
                lines.append(f"• {route}：查價失敗")
        if len(results) > 5:
            lines.append(f"另有 {len(results) - 5} 筆，請至網頁查看完整紀錄。")
        return "\n".join(lines)


def build_router(
    *,
    allowed_chat_ids: set[str] | list[str] | tuple[str, ...] = tuple(TELEGRAM_ALLOWED_CHAT_IDS),
    allowed_user_ids: Optional[set[str] | list[str] | tuple[str, ...]] = None,
    bot_username: Optional[str] = None,
    callbacks: Optional[CommandCallbacks] = None,
) -> TelegramCommandRouter:
    handlers = callbacks or CommandCallbacks()
    if allowed_user_ids is None:
        allowed_user_ids = tuple(
            value.strip()
            for value in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
            if value.strip()
        )
    return TelegramCommandRouter(
        allowed_chat_ids=allowed_chat_ids,
        allowed_user_ids=allowed_user_ids,
        status_callback=handlers.status,
        quota_callback=handlers.quota,
        check_estimate_callback=handlers.estimate_check,
        check_callback=handlers.check,
        bot_username=bot_username or os.getenv("TELEGRAM_BOT_USERNAME", ""),
        confirmation_ttl_seconds=120,
    )


def telegram_readiness(
    *,
    lock_path: Path = LOCK_FILE,
    status_path: Path = STATUS_FILE,
    now: Optional[datetime] = None,
) -> dict[str, dict[str, Any]]:
    """Expose independent report, command, and watchdog readiness for the UI."""
    report_configured = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    commands_configured = bool(
        TELEGRAM_COMMANDS_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS
    )
    commands_running = commands_configured and lock_owner_alive(Path(lock_path))
    scheduler_health, age = scheduler_heartbeat_health(
        _read_scheduler_status(Path(status_path)), now=now
    )
    return {
        "report": {
            "ready": report_configured,
            "configured": report_configured,
            "reason": "ready" if report_configured else "missing_bot_token_or_report_chat",
        },
        "commands": {
            "ready": commands_running,
            "configured": commands_configured,
            "running": commands_running,
            "reason": (
                "ready"
                if commands_running
                else "service_not_running"
                if commands_configured
                else "commands_not_configured"
            ),
        },
        "watchdog": {
            "ready": commands_running,
            "configured": commands_configured,
            "scheduler_health": scheduler_health,
            "heartbeat_age_seconds": (
                max(0, int(age.total_seconds())) if age is not None else None
            ),
            "reason": "ready" if commands_running else "command_service_not_running",
        },
    }


class TelegramBotRunner:
    """Long-poll updates and hand each unique command to the router once."""

    def __init__(
        self,
        api: TelegramAPI,
        router: TelegramCommandRouter,
        *,
        offset_store: Optional[OffsetStore] = None,
        poll_timeout: int = POLL_TIMEOUT_SECONDS,
        max_backoff: float = MAX_BACKOFF_SECONDS,
        sleep_func: Callable[[float], None] = time.sleep,
        logger: Callable[[str], None] = print,
        heartbeat_watchdog: Optional[SchedulerHeartbeatWatchdog] = None,
        alert_chat_ids: set[str] | list[str] | tuple[str, ...] = (),
    ) -> None:
        self.api = api
        self.router = router
        self.offset_store = offset_store or OffsetStore()
        self.poll_timeout = max(1, int(poll_timeout))
        self.max_backoff = max(1.0, float(max_backoff))
        self.sleep_func = sleep_func
        self.logger = logger
        self.heartbeat_watchdog = heartbeat_watchdog
        self.alert_chat_ids = tuple(
            sorted({str(chat_id).strip() for chat_id in alert_chat_ids if str(chat_id).strip()})
        )
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _update_id(update: Mapping[str, Any]) -> Optional[int]:
        try:
            value = int(update.get("update_id"))
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    @staticmethod
    def _sender_is_bot(update: Mapping[str, Any]) -> bool:
        message = update.get("message")
        sender = message.get("from") if isinstance(message, Mapping) else None
        return bool(sender.get("is_bot")) if isinstance(sender, Mapping) else False

    @staticmethod
    def _chat_id(update: Mapping[str, Any]) -> Optional[str | int]:
        message = update.get("message")
        chat = message.get("chat") if isinstance(message, Mapping) else None
        return chat.get("id") if isinstance(chat, Mapping) else None

    def poll_once(self) -> int:
        if maintenance_active(DATA_DIR):
            # Leave updates unacknowledged until restore/maintenance completes;
            # no command callback or watchdog write may race a database swap.
            return 0
        updates = self.api.get_updates(
            self.offset_store.next_offset,
            timeout=self.poll_timeout,
        )
        if self.heartbeat_watchdog is not None:
            event = self.heartbeat_watchdog.poll(self.alert_chat_ids)
            if event:
                first_error: Optional[TelegramAPIError] = None
                for chat_id in event.recipients:
                    try:
                        self.api.send_message(chat_id, event.message)
                    except TelegramAPIError as exc:
                        # One broken or rate-limited chat must not starve the
                        # remaining recipients.  Successful recipients are
                        # acknowledged now; failed ones remain pending and the
                        # runner applies its normal bounded retry backoff.
                        if exc.delivery_unknown:
                            self.heartbeat_watchdog.mark_delivery_unknown(event, chat_id)
                        elif first_error is None:
                            first_error = exc
                    else:
                        self.heartbeat_watchdog.mark_delivered(event, chat_id)
                if first_error is not None:
                    raise first_error
        sortable = [
            (update_id, update)
            for update in updates
            if (update_id := self._update_id(update)) is not None
        ]
        processed = 0
        for update_id, update in sorted(sortable, key=lambda item: item[0]):
            if update_id < self.offset_store.next_offset:
                continue

            # Persist before routing so a restart cannot repeat /check or its
            # reply.  This intentionally favours no duplicate spending over
            # retrying an ambiguously interrupted command.
            self.offset_store.advance_to(update_id + 1)
            processed += 1
            if self._sender_is_bot(update):
                continue

            result = self.router.handle_update(update)
            chat_id = self._chat_id(update)
            if result.handled and result.text and chat_id is not None:
                self.api.send_message(chat_id, result.text)
        return processed

    def run_forever(self, *, max_cycles: Optional[int] = None) -> None:
        failures = 0
        cycles = 0
        while not self._stop.is_set() and (max_cycles is None or cycles < max_cycles):
            cycles += 1
            try:
                self.poll_once()
                failures = 0
            except TelegramAPIError as exc:
                failures += 1
                exponential = min(self.max_backoff, 2 ** min(failures - 1, 8))
                delay = min(
                    self.max_backoff,
                    max(exponential, float(exc.retry_after or 0)),
                )
                self.logger(f"Telegram 連線暫時中斷，{delay:g} 秒後重試。")
                if not self._stop.is_set():
                    self.sleep_func(delay)
            except Exception:
                # Callback and malformed-update bugs must not expose secrets or
                # terminate the resident service.
                failures += 1
                delay = min(self.max_backoff, 2 ** min(failures - 1, 8))
                self.logger(f"Telegram 指令處理暫時失敗，{delay:g} 秒後重試。")
                if not self._stop.is_set():
                    self.sleep_func(delay)


def main() -> int:
    if not wait_until_maintenance_complete(DATA_DIR, timeout_seconds=30):
        print("Telegram 指令服務未啟動：系統仍在維護或復原。")
        return 3
    try:
        db.init_db()
    except Exception:
        print("Telegram 指令服務未啟動：資料庫初始化失敗。")
        return 3
    if not TELEGRAM_COMMANDS_ENABLED:
        print("Telegram 指令服務已由 TELEGRAM_COMMANDS_ENABLED 關閉。")
        return 0
    if not TELEGRAM_BOT_TOKEN:
        print("Telegram 指令服務未啟動：尚未設定 TELEGRAM_BOT_TOKEN。")
        return 2
    if not TELEGRAM_ALLOWED_CHAT_IDS:
        print("Telegram 指令服務未啟動：請設定 TELEGRAM_ALLOWED_CHAT_IDS 或 TELEGRAM_CHAT_ID。")
        return 2

    singleton = SingleInstanceLock()
    if not singleton.acquire():
        print("Telegram 指令服務已在執行中，本次不重複啟動。")
        return 0

    try:
        api = TelegramAPI(TELEGRAM_BOT_TOKEN)
        runner = TelegramBotRunner(
            api,
            build_router(),
            heartbeat_watchdog=SchedulerHeartbeatWatchdog(),
            alert_chat_ids=TELEGRAM_ALLOWED_CHAT_IDS,
        )
        print("Telegram 指令服務已啟動：/status、/quota、/check。")
        runner.run_forever()
    except KeyboardInterrupt:
        print("Telegram 指令服務已停止。")
    finally:
        singleton.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
