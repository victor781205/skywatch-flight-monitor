"""安全、可測試的 Telegram 指令路由器。

這個模組只處理指令解析、聊天室授權與 ``/check`` 的二階段確認。
Telegram polling/webhook、訊息傳送與實際查價由應用層注入，避免和 Streamlit
或特定 Telegram SDK 耦合。
"""

from __future__ import annotations

import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

ChatId = str | int


@dataclass(frozen=True)
class TelegramCommandContext:
    """傳給應用層 callback 的最小 Telegram 請求資訊。"""

    chat_id: str
    user_id: Optional[str]
    username: Optional[str]
    raw_text: str
    command: str
    chat_type: str = "private"


@dataclass(frozen=True)
class TelegramCommandResult:
    """路由結果；應用層只需在 ``handled`` 時傳送 ``text``。"""

    handled: bool
    text: str = ""
    command: Optional[str] = None
    ok: bool = True
    executed: bool = False


@dataclass(frozen=True)
class CheckEstimate:
    """一次即時查價預估會使用的 API 次數與選填說明。"""

    api_calls: int
    detail: str = ""


@dataclass(frozen=True)
class ParsedTelegramCommand:
    name: str
    bot_suffix: Optional[str]
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class _PendingCheck:
    chat_id: str
    user_id: Optional[str]
    expires_at: float


@dataclass(frozen=True)
class _ConsumedToken:
    reason: str
    purge_at: float


_COMMAND_RE = re.compile(
    r"^/(?P<name>[A-Za-z][A-Za-z0-9_]*)"
    r"(?:@(?P<bot>[A-Za-z0-9_]+))?"
    r"(?:\s+(?P<arguments>.*?))?\s*$",
    re.DOTALL,
)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")


def parse_telegram_command(text: str) -> Optional[ParsedTelegramCommand]:
    """解析 Telegram slash command，包含 ``@BotName`` suffix。

    一般聊天文字會回傳 ``None``；以斜線開頭但格式錯誤的文字，也交由路由器
    回覆安全的格式錯誤訊息。
    """

    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    match = _COMMAND_RE.fullmatch(stripped)
    if not match:
        return None
    raw_arguments = match.group("arguments") or ""
    arguments = tuple(raw_arguments.split())
    return ParsedTelegramCommand(
        name=match.group("name").lower(),
        bot_suffix=match.group("bot"),
        arguments=arguments,
    )


class TelegramCommandRouter:
    """同步 Telegram 指令路由器。

    Callback 介面均為 ``callback(context)``。``status_callback``、
    ``quota_callback`` 與 ``check_callback`` 建議回傳可直接顯示的繁體中文文字；
    ``check_estimate_callback`` 可回傳 :class:`CheckEstimate`、正整數、
    ``(api_calls, detail)``，或含 ``api_calls`` 的 mapping。

    現有專案的 notifier 為同步 I/O，因此這裡刻意使用同步 callback。若 Telegram
    接收端是 async framework，可在線程池呼叫 ``handle``，網路傳送仍留在框架層。
    """

    def __init__(
        self,
        *,
        allowed_chat_ids: Iterable[ChatId] | ChatId,
        allowed_user_ids: Iterable[ChatId] | ChatId = (),
        status_callback: Callable[[TelegramCommandContext], Any],
        quota_callback: Callable[[TelegramCommandContext], Any],
        check_estimate_callback: Callable[[TelegramCommandContext], Any],
        check_callback: Callable[[TelegramCommandContext], Any],
        bot_username: Optional[str] = None,
        confirmation_ttl_seconds: float = 120,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Optional[Callable[[], str]] = None,
        rate_limit_commands: int = 12,
        rate_limit_window_seconds: float = 60.0,
    ) -> None:
        if confirmation_ttl_seconds <= 0:
            raise ValueError("confirmation_ttl_seconds 必須大於 0")
        for callback in (
            status_callback,
            quota_callback,
            check_estimate_callback,
            check_callback,
        ):
            if not callable(callback):
                raise TypeError("所有 callback 都必須可呼叫")

        if isinstance(allowed_chat_ids, (str, int)):
            allowed_values: Iterable[ChatId] = (allowed_chat_ids,)
        else:
            allowed_values = allowed_chat_ids

        self._allowed_chat_ids = {
            self._normalise_id(chat_id) for chat_id in allowed_values
        }
        if isinstance(allowed_user_ids, (str, int)):
            allowed_users: Iterable[ChatId] = (allowed_user_ids,)
        else:
            allowed_users = allowed_user_ids
        self._allowed_user_ids = {
            self._normalise_id(user_id) for user_id in allowed_users
            if self._normalise_id(user_id)
        }
        self._status_callback = status_callback
        self._quota_callback = quota_callback
        self._check_estimate_callback = check_estimate_callback
        self._check_callback = check_callback
        self._bot_username = (bot_username or "").lstrip("@").lower() or None
        self._ttl = float(confirmation_ttl_seconds)
        self._clock = clock
        self._token_factory = token_factory or (
            lambda: secrets.token_urlsafe(6).upper()
        )
        self._pending: dict[str, _PendingCheck] = {}
        self._consumed: dict[str, _ConsumedToken] = {}
        self._rate_limit_commands = max(1, int(rate_limit_commands))
        self._rate_limit_window = max(1.0, float(rate_limit_window_seconds))
        self._rate_windows: dict[tuple[str, str], tuple[float, int]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _normalise_id(value: ChatId) -> str:
        return str(value).strip()

    def handle(
        self,
        text: str,
        chat_id: ChatId,
        *,
        user_id: Optional[ChatId] = None,
        username: Optional[str] = None,
        chat_type: str = "private",
    ) -> TelegramCommandResult:
        """處理一則 Telegram 文字訊息，不執行任何網路傳送。"""

        parsed = parse_telegram_command(text)
        if parsed is None:
            if isinstance(text, str) and text.strip().startswith("/"):
                normalised_chat_id = self._normalise_id(chat_id)
                normalised_user_id = None if user_id is None else self._normalise_id(user_id)
                if not self._is_authorised(
                    normalised_chat_id, normalised_user_id, chat_type
                ):
                    return self._unauthorised_result()
                if not self._within_rate_limit(normalised_chat_id, normalised_user_id):
                    return TelegramCommandResult(handled=False)
                return TelegramCommandResult(
                    handled=True,
                    text="⚠️ 指令格式不正確。可用指令：/status、/quota、/check。",
                    ok=False,
                )
            return TelegramCommandResult(handled=False)

        if (
            parsed.bot_suffix
            and self._bot_username
            and parsed.bot_suffix.lower() != self._bot_username
        ):
            # 群組中給其他 bot 的指令必須保持安靜。
            return TelegramCommandResult(handled=False)

        normalised_chat_id = self._normalise_id(chat_id)
        normalised_user_id = None if user_id is None else self._normalise_id(user_id)
        if not self._is_authorised(normalised_chat_id, normalised_user_id, chat_type):
            return self._unauthorised_result(parsed.name)
        if not self._within_rate_limit(normalised_chat_id, normalised_user_id):
            return TelegramCommandResult(handled=False)

        context = TelegramCommandContext(
            chat_id=normalised_chat_id,
            user_id=normalised_user_id,
            username=username,
            raw_text=text.strip(),
            command=parsed.name,
            chat_type=str(chat_type or "private").lower(),
        )

        if parsed.name == "status":
            if parsed.arguments:
                return self._usage_error("status", "/status")
            return self._run_information_callback(
                "status",
                "📡 系統狀態",
                "⚠️ 無法取得系統狀態，請稍後再試。",
                self._status_callback,
                context,
            )

        if parsed.name == "quota":
            if parsed.arguments:
                return self._usage_error("quota", "/quota")
            return self._run_information_callback(
                "quota",
                "📊 API 額度",
                "⚠️ 無法取得 API 額度，請稍後再試。",
                self._quota_callback,
                context,
            )

        if parsed.name == "check":
            if not parsed.arguments:
                return self._prepare_check(context)
            if len(parsed.arguments) == 2 and parsed.arguments[0].lower() == "confirm":
                return self._confirm_check(parsed.arguments[1], context)
            return self._usage_error(
                "check", "/check 或 /check confirm <確認碼>"
            )

        return TelegramCommandResult(
            handled=True,
            text="❓ 不支援此指令。可用指令：/status、/quota、/check。",
            command=parsed.name,
            ok=False,
        )

    def handle_update(self, update: Mapping[str, Any]) -> TelegramCommandResult:
        """從 Telegram Bot API update mapping 擷取必要欄位後交給 ``handle``。"""

        if not isinstance(update, Mapping):
            return TelegramCommandResult(handled=False)
        message = update.get("message")
        if not isinstance(message, Mapping):
            return TelegramCommandResult(handled=False)
        chat = message.get("chat")
        sender = message.get("from")
        text = message.get("text")
        if not isinstance(chat, Mapping) or "id" not in chat or not isinstance(text, str):
            return TelegramCommandResult(handled=False)
        if not isinstance(sender, Mapping):
            sender = {}
        return self.handle(
            text,
            chat["id"],
            user_id=sender.get("id"),
            username=sender.get("username"),
            chat_type=str(chat.get("type") or "private"),
        )

    def _is_authorised(
        self,
        chat_id: str,
        user_id: Optional[str],
        chat_type: str,
    ) -> bool:
        if chat_id not in self._allowed_chat_ids:
            return False
        if str(chat_type or "private").lower() in {"group", "supergroup"}:
            return bool(user_id and user_id in self._allowed_user_ids)
        return True

    def _within_rate_limit(self, chat_id: str, user_id: Optional[str]) -> bool:
        now = self._clock()
        key = (chat_id, user_id or "-")
        with self._lock:
            started, count = self._rate_windows.get(key, (now, 0))
            if now - started >= self._rate_limit_window:
                started, count = now, 0
            if count >= self._rate_limit_commands:
                self._rate_windows[key] = (started, count)
                return False
            self._rate_windows[key] = (started, count + 1)
            stale_before = now - self._rate_limit_window * 2
            for identity, (window_started, _) in list(self._rate_windows.items()):
                if window_started < stale_before:
                    del self._rate_windows[identity]
            return True

    @staticmethod
    def _unauthorised_result(command: Optional[str] = None) -> TelegramCommandResult:
        # Stay silent to avoid confirming bot existence or access policy to an
        # untrusted sender.
        return TelegramCommandResult(handled=False, command=command, ok=False)

    @staticmethod
    def _usage_error(command: str, usage: str) -> TelegramCommandResult:
        return TelegramCommandResult(
            handled=True,
            text=f"⚠️ 指令格式不正確。用法：{usage}",
            command=command,
            ok=False,
        )

    @staticmethod
    def _display_text(value: Any, default: str) -> str:
        if isinstance(value, str):
            return value.strip() or default
        if isinstance(value, Mapping):
            for key in ("text", "message", "detail"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        if (
            isinstance(value, tuple)
            and len(value) >= 2
            and isinstance(value[0], bool)
        ):
            candidate = value[1]
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        if value is None:
            return default
        rendered = str(value).strip()
        return rendered or default

    def _run_information_callback(
        self,
        command: str,
        heading: str,
        failure_message: str,
        callback: Callable[[TelegramCommandContext], Any],
        context: TelegramCommandContext,
    ) -> TelegramCommandResult:
        try:
            detail = self._display_text(callback(context), "目前沒有可顯示的資料。")
        except Exception:
            return TelegramCommandResult(
                handled=True,
                text=failure_message,
                command=command,
                ok=False,
            )
        return TelegramCommandResult(
            handled=True,
            text=f"{heading}\n{detail}",
            command=command,
        )

    @staticmethod
    def _coerce_estimate(value: Any) -> CheckEstimate:
        if isinstance(value, CheckEstimate):
            estimate = value
        elif isinstance(value, int) and not isinstance(value, bool):
            estimate = CheckEstimate(value)
        elif isinstance(value, tuple) and value:
            estimate = CheckEstimate(int(value[0]), str(value[1]) if len(value) > 1 else "")
        elif isinstance(value, Mapping):
            raw_calls = value.get("api_calls", value.get("estimated_api_calls"))
            estimate = CheckEstimate(
                int(raw_calls),
                str(value.get("detail") or value.get("message") or ""),
            )
        else:
            raise TypeError("無法辨識查價預估")
        if isinstance(estimate.api_calls, bool) or estimate.api_calls < 1:
            raise ValueError("付費查價的 API 次數預估必須至少為 1")
        return estimate

    def _prepare_check(
        self, context: TelegramCommandContext
    ) -> TelegramCommandResult:
        try:
            estimate = self._coerce_estimate(self._check_estimate_callback(context))
        except Exception:
            return TelegramCommandResult(
                handled=True,
                text="⚠️ 無法估算查價額度，尚未執行查價。請稍後再試。",
                command="check",
                ok=False,
            )

        try:
            token = self._issue_token(context)
        except RuntimeError:
            return TelegramCommandResult(
                handled=True,
                text="⚠️ 無法建立安全確認碼，尚未執行查價。請稍後再試。",
                command="check",
                ok=False,
            )

        seconds = max(1, math.ceil(self._ttl))
        detail = f"\n{estimate.detail.strip()}" if estimate.detail.strip() else ""
        return TelegramCommandResult(
            handled=True,
            text=(
                "🔎 即時查價確認\n"
                f"預估會使用 {estimate.api_calls} 次查價 API。{detail}\n"
                "這會消耗免費額度，尚未執行查價。\n"
                f"請在 {seconds} 秒內輸入：/check confirm {token}\n"
                "確認碼僅限本聊天室與目前使用者使用一次。"
            ),
            command="check",
        )

    def _issue_token(self, context: TelegramCommandContext) -> str:
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            # 同一使用者只保留最新確認碼，避免連續點擊造成多次付費查價。
            for token, pending in list(self._pending.items()):
                if pending.chat_id == context.chat_id and pending.user_id == context.user_id:
                    del self._pending[token]
                    self._consumed[token] = _ConsumedToken(
                        reason="replaced", purge_at=pending.expires_at + self._ttl
                    )

            for _ in range(20):
                raw_token = str(self._token_factory()).strip().upper()
                token = re.sub(r"[^A-Z0-9_-]", "", raw_token)[:32]
                if not _TOKEN_RE.fullmatch(token):
                    continue
                if token in self._pending or token in self._consumed:
                    continue
                self._pending[token] = _PendingCheck(
                    chat_id=context.chat_id,
                    user_id=context.user_id,
                    expires_at=now + self._ttl,
                )
                return token
        raise RuntimeError("無法建立不重複的確認碼")

    def _cleanup_locked(self, now: float) -> None:
        for token, pending in list(self._pending.items()):
            if now >= pending.expires_at:
                del self._pending[token]
                self._consumed[token] = _ConsumedToken(
                    reason="expired", purge_at=now + self._ttl
                )
        for token, consumed in list(self._consumed.items()):
            if now >= consumed.purge_at:
                del self._consumed[token]

    def _consume_token(
        self, token: str, context: TelegramCommandContext
    ) -> tuple[bool, str]:
        normalised_token = token.strip().upper()
        if not _TOKEN_RE.fullmatch(normalised_token):
            return False, "invalid"
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            consumed = self._consumed.get(normalised_token)
            if consumed:
                return False, consumed.reason
            pending = self._pending.get(normalised_token)
            if pending is None:
                return False, "invalid"
            if pending.chat_id != context.chat_id or pending.user_id != context.user_id:
                return False, "invalid"
            # 在執行 callback 前原子消耗；即使 callback 失敗也不可重放。
            del self._pending[normalised_token]
            self._consumed[normalised_token] = _ConsumedToken(
                reason="used", purge_at=now + self._ttl
            )
            return True, "used"

    def _confirm_check(
        self, token: str, context: TelegramCommandContext
    ) -> TelegramCommandResult:
        accepted, reason = self._consume_token(token, context)
        if not accepted:
            messages = {
                "used": "⛔ 此確認碼已使用，未再次執行查價。",
                "expired": "⌛ 確認碼已逾時，請重新輸入 /check 取得新確認碼。",
                "replaced": "⛔ 此確認碼已失效，請使用最新的確認碼。",
                "invalid": "⛔ 確認碼無效或不屬於此聊天室，未執行查價。",
            }
            return TelegramCommandResult(
                handled=True,
                text=messages.get(reason, messages["invalid"]),
                command="check",
                ok=False,
            )

        try:
            detail = self._display_text(
                self._check_callback(context), "查價已完成。"
            )
        except Exception:
            return TelegramCommandResult(
                handled=True,
                text="❌ 即時查價失敗；為避免重複扣除額度，此確認碼已失效。請重新輸入 /check。",
                command="check",
                ok=False,
                executed=True,
            )
        return TelegramCommandResult(
            handled=True,
            text=f"✅ 即時查價完成\n{detail}",
            command="check",
            executed=True,
        )


__all__ = [
    "CheckEstimate",
    "ParsedTelegramCommand",
    "TelegramCommandContext",
    "TelegramCommandResult",
    "TelegramCommandRouter",
    "parse_telegram_command",
]
