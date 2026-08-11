"""Quota summaries, schedule projections, and one-time monthly warnings."""
from __future__ import annotations

import calendar
import os
from datetime import datetime
from typing import Any, Callable, Dict, List

from . import database as db
from .config import (
    API_USAGE_WARNING_PERCENT,
    SERPAPI_MONTHLY_LIMIT,
)


def scheduled_runs_per_day() -> float:
    interval_raw = os.getenv("CHECK_INTERVAL_MINUTES", "").strip()
    if interval_raw:
        try:
            minutes = max(5, int(interval_raw))
            return 1440.0 / minutes
        except ValueError:
            return 2.0
    hours_raw = os.getenv("SCHEDULE_HOURS", "0,12").strip() or "0,12"
    try:
        hours = {int(value.strip()) for value in hours_raw.split(",") if value.strip()}
        valid = {hour for hour in hours if 0 <= hour <= 23}
        return float(len(valid) or 2)
    except ValueError:
        return 2.0


def estimate_monthly_primary_searches(active_monitors: int, now: datetime | None = None) -> int:
    current = now or datetime.now()
    days = calendar.monthrange(current.year, current.month)[1]
    return int(round(max(0, active_monitors) * scheduled_runs_per_day() * days))


def quota_summaries(now: datetime | None = None) -> List[Dict[str, Any]]:
    return [
        db.get_api_usage_summary("serpapi", SERPAPI_MONTHLY_LIMIT, now=now),
    ]


def maybe_send_quota_warnings(
    send_func: Callable[..., List],
    now: datetime | None = None,
) -> List[Dict[str, Any]]:
    sent: List[Dict[str, Any]] = []
    threshold = int(round(API_USAGE_WARNING_PERCENT))
    for summary in quota_summaries(now=now):
        percent = summary.get("percent")
        if not summary["limit"] or percent is None or percent < API_USAGE_WARNING_PERCENT:
            continue
        if not db.claim_api_quota_warning(summary["provider"], summary["period"], threshold):
            continue
        provider_name = "SerpAPI"
        message = (
            f"⚠️ SkyWatch API 免費額度提醒\n"
            f"{provider_name} 本月已使用 {summary['used']} / {summary['limit']} 次 "
            f"({percent:.0f}%)，剩餘 {summary['remaining']} 次。\n"
            "系統仍會維持額度節省模式；請視需要停用低優先監控或調低排程頻率。"
        )
        results = send_func(message, monitor_id=None)
        if not any(bool(result[1]) for result in results):
            db.release_api_quota_warning(summary["provider"], summary["period"], threshold)
        sent.append({"summary": summary, "results": results})
    return sent
