"""通知模組：Discord / Telegram。"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import List, Optional, Tuple

import requests

from .config import (
    DISCORD_WEBHOOK_URL,
    MESSAGE_DEDUPE_HOURS,
    OUTBOX_MAX_ATTEMPTS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from . import database as db


DUPLICATE_SUPPRESSED = "duplicate_suppressed"


def _claim_delivery(channel: str, monitor_id: Optional[int], message: str) -> Optional[str]:
    """Claim an exact per-channel payload before making a network request."""
    if monitor_id is None:
        return None
    digest = hashlib.sha256(
        f"delivery:{channel.lower()}:{monitor_id}:{message.strip()}".encode("utf-8")
    ).hexdigest()
    if db.claim_notification(monitor_id, digest, ttl_hours=MESSAGE_DEDUPE_HOURS):
        return digest
    return ""


def _deliver_once(
    channel: str,
    monitor_id: Optional[int],
    message: str,
    sender,
) -> Tuple[str, bool, str]:
    claim_key = _claim_delivery(channel, monitor_id, message)
    if claim_key == "":
        return channel, False, DUPLICATE_SUPPRESSED

    ok, detail = sender(message)
    if ok:
        db.log_notification(monitor_id, message, channel, True)
    else:
        db.log_notification(monitor_id, message, channel, False)
        if claim_key:
            db.release_notification_claim(claim_key)
    return channel, ok, detail


def send_all(message: str, monitor_id: Optional[int] = None) -> List[Tuple[str, bool, str]]:
    """
    嘗試所有已設定的通知管道。
    回傳 [(channel, success, detail), ...]
    """
    results: List[Tuple[str, bool, str]] = []

    if DISCORD_WEBHOOK_URL:
        results.append(_deliver_once("discord", monitor_id, message, send_discord))

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        results.append(_deliver_once("telegram", monitor_id, message, send_telegram))

    if not results:
        # 沒有設定任何通知，仍寫入 log 方便儀表板查看
        db.log_notification(monitor_id, f"[未設定通知管道] {message}", "local", True)
        results.append(("local", True, "已寫入本機通知紀錄（未設定外部管道）"))

    return results


def send_discord(message: str) -> Tuple[bool, str]:
    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message[:1900]},
            timeout=20,
        )
        if resp.status_code in (200, 204):
            return True, "OK"
        return False, f"HTTP {resp.status_code}"
    except requests.Timeout:
        return False, "連線逾時"
    except requests.RequestException:
        return False, "Discord 連線失敗"


def send_telegram(message: str) -> Tuple[bool, str]:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message[:4000]},
            timeout=20,
        )
        data = resp.json()
        if data.get("ok"):
            return True, "OK"
        description = str(data.get("description") or "Telegram 拒絕請求")
        return False, description[:160]
    except requests.Timeout:
        return False, "連線逾時"
    except (requests.RequestException, ValueError):
        return False, "Telegram 連線失敗"


def configured_channels() -> List[str]:
    channels = []
    if DISCORD_WEBHOOK_URL:
        channels.append("Discord")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        channels.append("Telegram")
    if not channels:
        channels.append("本機紀錄 only")
    return channels


def enqueue_all(
    message: str,
    *,
    monitor_id: Optional[int] = None,
    run_id: Optional[str] = None,
    purpose: str = "fare_report",
    dedupe_scope: Optional[str] = None,
    next_attempt_at: Optional[datetime] = None,
    replace_pending: bool = False,
) -> List[dict]:
    """Persist deliveries before sending so a crash cannot silently lose them."""
    channels: List[str] = []
    if DISCORD_WEBHOOK_URL:
        channels.append("discord")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        channels.append("telegram")
    if not channels:
        db.log_notification(monitor_id, f"[未設定通知管道] {message}", "local", True)
        return [
            {
                "id": None,
                "channel": "local",
                "status": "sent",
                "created": True,
                "detail": "已寫入本機通知紀錄（未設定外部管道）",
            }
        ]

    queued: List[dict] = []
    for channel in channels:
        digest = hashlib.sha256(
            (
                f"outbox:{purpose}:{channel}:{monitor_id or '-'}:"
                f"{dedupe_scope if dedupe_scope is not None else run_id or '-'}:"
                f"{'' if dedupe_scope is not None else message.strip()}"
            ).encode("utf-8")
        ).hexdigest()
        queued.append(
            db.enqueue_notification(
                channel=channel,
                message=message,
                dedupe_key=digest,
                monitor_id=monitor_id,
                run_id=run_id,
                max_attempts=OUTBOX_MAX_ATTEMPTS,
                next_attempt_at=next_attempt_at,
                replace_pending=replace_pending,
            )
        )
    return queued


def process_notification_outbox(limit: int = 20) -> List[dict]:
    """Send due items once; failures are retained with exponential backoff."""
    results: List[dict] = []
    senders = {"telegram": send_telegram, "discord": send_discord}
    for item in db.claim_due_notifications(limit=limit):
        channel = str(item["channel"]).lower()
        sender = senders.get(channel)
        if sender is None:
            ok, detail = False, f"不支援的通知管道：{channel}"
        else:
            try:
                ok, detail = sender(str(item["message"]))
            except Exception as exc:
                # Always return the lease to a terminal/retryable state even
                # when an unexpected custom sender exception escapes.
                ok, detail = False, f"sender_error:{type(exc).__name__}"
        updated = db.complete_outbox_delivery(
            int(item["id"]),
            success=ok,
            error="" if ok else detail,
        )
        db.log_notification(item.get("monitor_id"), str(item["message"]), channel, ok)
        results.append(
            {
                "id": int(item["id"]),
                "channel": channel,
                "success": ok,
                "detail": detail,
                "status": updated["status"],
                "attempt_count": int(updated["attempt_count"]),
                "next_attempt_at": updated["next_attempt_at"],
            }
        )
    return results


def send_all_reliable(
    message: str,
    *,
    monitor_id: Optional[int] = None,
    run_id: Optional[str] = None,
    purpose: str = "fare_report",
    dedupe_scope: Optional[str] = None,
    next_attempt_at: Optional[datetime] = None,
    replace_pending: bool = False,
) -> List[Tuple[str, bool, str]]:
    """Queue then immediately try delivery; queued retries remain durable."""
    queued = enqueue_all(
        message,
        monitor_id=monitor_id,
        run_id=run_id,
        purpose=purpose,
        dedupe_scope=dedupe_scope,
        next_attempt_at=next_attempt_at,
        replace_pending=replace_pending,
    )
    if queued and queued[0].get("channel") == "local":
        return [("local", True, str(queued[0]["detail"]))]
    queued_ids = {int(item["id"]) for item in queued if item.get("id") is not None}
    delivered = process_notification_outbox(limit=max(20, len(queued_ids)))
    by_id = {int(item["id"]): item for item in delivered}
    results: List[Tuple[str, bool, str]] = []
    for item in queued:
        delivery = by_id.get(int(item["id"])) if item.get("id") is not None else None
        if delivery:
            results.append((delivery["channel"], bool(delivery["success"]), str(delivery["detail"])))
        elif item.get("status") == "sent":
            results.append((str(item["channel"]), True, "already_sent"))
        else:
            results.append((str(item["channel"]), False, "queued_for_retry"))
    return results
