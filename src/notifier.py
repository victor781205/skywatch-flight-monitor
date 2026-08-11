"""通知模組：Discord / Telegram。"""
from __future__ import annotations

import hashlib
from typing import List, Optional, Tuple

import requests

from .config import (
    DISCORD_WEBHOOK_URL,
    MESSAGE_DEDUPE_HOURS,
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
