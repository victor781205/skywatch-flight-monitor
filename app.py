"""SkyWatch 航班價格監控的 Streamlit 操作介面。

執行：streamlit run app.py
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import streamlit as st

from src import database as db
from src import ui
from src.airports import airport_data_status, options_for_select
from src.api_usage import quota_summaries, scheduled_runs_per_day
from src.backup import (
    BackupError,
    BackupValidationError,
    create_full_backup,
    export_monitors,
    import_monitors,
    inspect_backup,
    preview_monitor_import,
    restore_full_backup,
)
from src.config import (
    API_USAGE_WARNING_PERCENT,
    APP_VERSION,
    DB_PATH,
    MISSED_RUN_LOOKBACK_HOURS,
    SCHEDULER_TICK_MINUTES,
    SERPAPI_API_KEY,
    SERPAPI_MONTHLY_LIMIT,
    TELEGRAM_ALLOWED_CHAT_IDS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_COMMANDS_ENABLED,
)
from src.flight_api import FlightAPIError, format_flight_summary
from src.monitor import check_all_monitors, check_one_monitor, search_and_save
from src.notifier import configured_channels, process_notification_outbox
from src.policies import (
    AirlineCode,
    AirlineRuleSet,
    NotificationPolicy,
    QuotaPolicy,
    SchedulePolicy,
    normalize_airline,
)
from src.runtime_lock import lock_owner_alive, parse_lock_owner
from src.travelpayouts import TravelpayoutsError, get_low_fare_calendar
from src.travelpayouts import configured as travelpayouts_ready

ROOT = Path(__file__).resolve().parent
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
CLASS_NAMES = {1: "經濟艙", 2: "豪華經濟艙", 3: "商務艙", 4: "頭等艙"}
CLASS_CODES = {label: code for code, label in CLASS_NAMES.items()}
QUOTA_LABELS = {"lean": "精簡", "standard": "標準", "full": "完整"}
SCHEDULE_LABELS = {
    "global": "沿用系統排程",
    "fixed_times": "每日多個固定時間",
    "daily": "每日固定時間",
    "interval": "固定間隔",
}
NOTIFICATION_LABELS = {
    "always": "每次查價皆回報",
    "target_only": "僅達目標價",
    "price_drop": "僅價格下降",
    "digest": "彙整通知",
}
AIRLINES = (("EVA", "長榮航空"), ("CI", "中華航空"), ("JX", "星宇航空"))
RUN_STATUS_LABELS = {
    "running": "執行中",
    "succeeded": "成功",
    "partial": "部分完成",
    "failed": "失敗",
    "skipped": "已略過",
    "missed": "偵測到漏跑",
    "backfilled": "已補跑",
    "recovered": "已復原",
}
OUTBOX_STATUS_LABELS = {
    "pending": "等待外送",
    "retry": "等待重試",
    "sending": "外送中",
    "sent": "已送達",
    "failed": "外送失敗",
}
RUN_TRIGGER_LABELS = {
    "manual": "手動檢查",
    "scheduled": "排程執行",
    "schedule": "排程執行",
    "backfill": "自動補跑",
    "telegram": "Telegram 指令",
    "global": "系統排程",
}


st.set_page_config(
    page_title="SkyWatch 航班價格監控",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded",
)

db.init_db()
try:
    st.html(f"<style>\n{(ROOT / 'style.css').read_text(encoding='utf-8')}\n</style>")
except (FileNotFoundError, OSError, UnicodeError):
    pass


def _now() -> datetime:
    return datetime.now(TAIPEI_TZ)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=TAIPEI_TZ) if parsed.tzinfo is None else parsed.astimezone(TAIPEI_TZ)
    except (TypeError, ValueError):
        return None


def _format_datetime(value: Any, fallback: str = "尚無紀錄") -> str:
    parsed = value if isinstance(value, datetime) else _parse_datetime(value)
    return parsed.strftime("%Y/%m/%d %H:%M") if parsed else fallback


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _json_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    try:
        parsed = json.loads(str(value or "[]"))
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _arrow_safe_frame(data: Any) -> pd.DataFrame:
    """Return a display-only frame whose object columns are Arrow-safe strings.

    SQLite rows can legitimately mix integers, ``None``, nested JSON values,
    and text in the same presentation column.  Pandas accepts that mixture,
    while Streamlit's Arrow conversion does not.  Numeric and datetime columns
    stay typed so number formatting and sorting continue to work.
    """
    frame = data.copy() if isinstance(data, pd.DataFrame) else pd.DataFrame(data)

    def display_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list, tuple, set)):
            return json.dumps(value, ensure_ascii=False, default=str)
        try:
            if bool(pd.isna(value)):
                return ""
        except (TypeError, ValueError):
            pass
        return str(value)

    for column in frame.columns:
        if pd.api.types.is_object_dtype(frame[column].dtype):
            frame[column] = frame[column].map(display_text).astype("string")
    return frame


def _telegram_command_runtime() -> dict[str, Any]:
    """Separate report configuration from command/watchdog runtime health."""
    report_ready = "Telegram" in configured_channels()
    command_configured = bool(
        TELEGRAM_COMMANDS_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS
    )
    lock_path = ROOT / "data" / "telegram_bot.lock"
    owner = None
    try:
        owner = parse_lock_owner(lock_path.read_bytes())
    except (FileNotFoundError, OSError):
        owner = None
    command_running = bool(command_configured and lock_owner_alive(lock_path))
    scheduler_status = get_scheduler_health()
    watchdog_ready = bool(command_running and scheduler_status.get("updated_at"))
    return {
        "report_ready": report_ready,
        "command_configured": command_configured,
        "command_running": command_running,
        "watchdog_ready": watchdog_ready,
        "pid": owner.pid if command_running and owner is not None else None,
    }


def _currency(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"NT$ {float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _price_delta_label(value: Any, percent: Any) -> str:
    if pd.isna(value) or float(value) == 0:
        return "持平"
    amount = float(value)
    percentage = float(percent)
    if amount < 0:
        return f"-NT$ {abs(amount):,.0f} ({percentage:.1f}%)"
    return f"+NT$ {amount:,.0f} (+{percentage:.1f}%)"


def _localized_status(value: Any, labels: dict[str, str]) -> str:
    raw = str(value or "").strip().lower()
    return labels.get(raw, raw or "未知")


def _clock_value(value: Any, fallback: time) -> time:
    try:
        return time.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback


def flight_api_ready() -> bool:
    return bool(SERPAPI_API_KEY.strip())


def get_scheduler_health() -> dict[str, Any]:
    """從排程器 heartbeat 取得動態健康狀態，不在 UI 內另開排程器。"""
    status_path = ROOT / "data" / "scheduler_status.json"
    result: dict[str, Any] = {
        "healthy": False,
        "state": "unknown",
        "supervisor_state": "unknown",
        "label": "尚未收到排程器狀態",
        "last_run": None,
        "next_run": None,
        "updated_at": None,
        "last_error": "",
        "recovered_count": 0,
        "retry_count": 0,
        "schedule": "依每筆監控策略",
        "last_hit_count": 0,
        "last_run_duration": 0.0,
        "last_success_rate": 0.0,
        "last_checked_count": 0,
    }
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return result
    except (FileNotFoundError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return result

    updated = _parse_datetime(payload.get("updated_at"))
    last_run = _parse_datetime(payload.get("last_completed_at"))
    next_run = _parse_datetime(payload.get("next_run_at"))
    fresh_for = max(15 * 60, int(SCHEDULER_TICK_MINUTES) * 180)
    fresh = bool(updated and (_now() - updated).total_seconds() <= fresh_for)
    state = str(payload.get("state") or "waiting").lower()
    supervisor = str(payload.get("supervisor_state") or "unknown").lower()
    healthy = fresh and state != "error" and supervisor != "error"
    label = "運作正常" if healthy else "狀態過期或異常"
    result.update(
        healthy=healthy,
        state=state,
        supervisor_state=supervisor,
        label=label,
        last_run=last_run,
        next_run=next_run,
        updated_at=updated,
        last_error=str(payload.get("last_error") or ""),
        recovered_count=int(payload.get("recovered_count") or 0),
        retry_count=int(payload.get("retry_count") or 0),
        schedule=str(payload.get("schedule") or result["schedule"]),
        last_hit_count=int(payload.get("last_hit_count") or 0),
        last_run_duration=float(payload.get("last_run_duration") or 0.0),
        last_success_rate=float(payload.get("last_success_rate") or 0.0),
        last_checked_count=int(payload.get("last_checked_count") or 0),
    )
    return result


def _global_schedule_policy() -> SchedulePolicy:
    raw_interval = os.getenv("CHECK_INTERVAL_MINUTES", "").strip()
    if raw_interval:
        try:
            return SchedulePolicy.from_dict({"mode": "interval", "interval_minutes": max(5, int(raw_interval))})
        except ValueError:
            pass
    raw_hours = os.getenv("SCHEDULE_HOURS", "0,12")
    clocks: list[str] = []
    for item in raw_hours.split(","):
        try:
            hour = int(item.strip())
            if 0 <= hour <= 23:
                clocks.append(f"{hour:02d}:00")
        except ValueError:
            continue
    return SchedulePolicy.from_dict({"mode": "fixed_times", "fixed_times": clocks or ["00:00", "12:00"]})


def _schedule_policy(schedule_type: str, schedule_value: str) -> SchedulePolicy:
    if schedule_type == "global":
        return _global_schedule_policy()
    if schedule_type == "fixed_times":
        return SchedulePolicy.from_dict({"mode": schedule_type, "fixed_times": schedule_value})
    if schedule_type == "daily":
        return SchedulePolicy.from_dict({"mode": schedule_type, "daily_time": schedule_value})
    return SchedulePolicy.from_dict({"mode": "interval", "interval_minutes": int(schedule_value)})


def _monitor_runs_per_day(monitor: dict[str, Any]) -> float:
    try:
        return _schedule_policy(
            str(monitor.get("schedule_type") or "global"),
            str(monitor.get("schedule_value") or ""),
        ).runs_per_day
    except (TypeError, ValueError):
        return scheduled_runs_per_day()


def _schedule_update_fields(
    monitor: dict[str, Any],
    schedule_type: str,
    schedule_value: str,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Only reschedule a monitor when its schedule itself changed."""
    validated = _schedule_policy(schedule_type, schedule_value)
    current_type = str(monitor.get("schedule_type") or "global")
    current_value = str(monitor.get("schedule_value") or "").strip()
    next_value = str(schedule_value or "").strip()
    if schedule_type == current_type and next_value == current_value:
        return {}, str(monitor.get("next_run_at") or "") or None
    next_run = validated.next_run(now or _now()).isoformat(timespec="seconds")
    return {
        "schedule_type": schedule_type,
        "schedule_value": next_value,
        "next_run_at": next_run,
    }, next_run


def _schedule_label(schedule_type: Any, schedule_value: Any) -> str:
    kind = str(schedule_type or "global")
    value = str(schedule_value or "")
    if kind == "global":
        return SCHEDULE_LABELS[kind]
    if kind == "interval":
        return f"每 {value or '?'} 分鐘"
    return f"{SCHEDULE_LABELS.get(kind, kind)} · {value}"


def _page_header(title: str, subtitle: str, *, eyebrow: str, badges: list[Any] | None = None) -> None:
    st.html(ui.page_header(title, subtitle, eyebrow=eyebrow, badges=badges or [], meta=f"v{APP_VERSION}"))


def _metric_row(cards: list[dict[str, Any]]) -> None:
    columns = st.columns(len(cards))
    for column, card in zip(columns, cards, strict=True):
        with column:
            st.html(ui.metric_card(**card))


def _airport_input(label: str, key: str, default: str) -> str:
    query = st.text_input(f"搜尋{label}", key=f"{key}_query", placeholder="城市、機場或 IATA")
    options = options_for_select(query, limit=80)
    if not options:
        st.warning("找不到符合的機場")
        return default
    if airport_data_status() == "fallback" and not st.session_state.get("airport_fallback_notice"):
        st.info("目前離線使用內建熱門機場清單；網路恢復後重啟即可載入全球機場資料。")
        st.session_state["airport_fallback_notice"] = True
    labels = list(options)
    default_index = next((i for i, item in enumerate(labels) if options[item] == default), 0)
    selected = st.selectbox(label, labels, index=default_index, key=f"{key}_select")
    return options[selected]


def _schedule_fields(prefix: str, existing_type: str = "global", existing_value: str = "") -> tuple[str, str, SchedulePolicy]:
    types = list(SCHEDULE_LABELS)
    selected = st.selectbox(
        "執行排程",
        types,
        index=types.index(existing_type) if existing_type in types else 0,
        format_func=lambda item: SCHEDULE_LABELS[item],
        key=f"{prefix}_schedule_type",
    )
    value = ""
    if selected == "fixed_times":
        value = st.text_input(
            "每日執行時間",
            value=existing_value if existing_type == selected and existing_value else "08:00,20:00",
            help="以逗號分隔 24 小時制時間，例如 08:00,20:00。",
            key=f"{prefix}_fixed_times",
        ).strip()
    elif selected == "daily":
        raw = existing_value if existing_type == selected and existing_value else "08:00"
        try:
            initial = time.fromisoformat(raw)
        except ValueError:
            initial = time(8, 0)
        value = st.time_input("每日執行時間", value=initial, key=f"{prefix}_daily_time").strftime("%H:%M")
    elif selected == "interval":
        try:
            initial_minutes = max(5, int(existing_value)) if existing_type == selected else 720
        except (TypeError, ValueError):
            initial_minutes = 720
        value = str(
            st.number_input(
                "執行間隔（分鐘）",
                min_value=5,
                max_value=10080,
                value=initial_minutes,
                step=5,
                key=f"{prefix}_interval",
            )
        )
    try:
        policy = _schedule_policy(selected, value)
        st.caption(f"預估每日 {policy.runs_per_day:.1f} 次；下次執行 {_format_datetime(policy.next_run(_now()))}")
    except (TypeError, ValueError) as exc:
        st.error(f"排程格式尚未完成：{exc}")
        policy = _global_schedule_policy()
    return selected, value, policy


def _notification_fields(
    prefix: str,
    existing_strategy: str = "always",
    existing_quiet: dict[str, Any] | None = None,
) -> tuple[str, dict[str, str]]:
    strategies = list(NOTIFICATION_LABELS)
    strategy = st.selectbox(
        "通知策略",
        strategies,
        index=strategies.index(existing_strategy) if existing_strategy in strategies else 0,
        format_func=lambda item: NOTIFICATION_LABELS[item],
        key=f"{prefix}_notification",
    )
    quiet = existing_quiet or {}
    enabled = st.checkbox("啟用安靜時段", value=bool(quiet), key=f"{prefix}_quiet_enabled")
    quiet_hours: dict[str, str] = {}
    if enabled:
        left, right = st.columns(2)
        try:
            start_value = time.fromisoformat(str(quiet.get("start") or "23:00"))
            end_value = time.fromisoformat(str(quiet.get("end") or "07:00"))
        except ValueError:
            start_value, end_value = time(23, 0), time(7, 0)
        start = left.time_input("安靜開始", value=start_value, key=f"{prefix}_quiet_start")
        end = right.time_input("安靜結束", value=end_value, key=f"{prefix}_quiet_end")
        quiet_hours = {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
    NotificationPolicy.from_dict({"strategy": strategy, "quiet_hours": quiet_hours or None})
    return strategy, quiet_hours


def _airline_rule_fields(
    prefix: str,
    existing_rules: list[dict[str, Any]] | None = None,
    *,
    round_trip: bool = True,
) -> list[dict[str, Any]]:
    existing = {str(item.get("airline")): item for item in (existing_rules or [])}
    rules: list[dict[str, Any]] = []
    st.caption("可為三家國籍航空設定不同目標價、轉機上限與出發時段；未啟用者仍可出現在一般最低價結果。")
    for code, name in AIRLINES:
        saved = existing.get(code, {})
        with st.expander(f"{name} · {code}", expanded=bool(saved)):
            enabled = st.checkbox("啟用專屬條件", value=bool(saved), key=f"{prefix}_{code}_enabled")
            if not enabled:
                continue
            target = st.number_input(
                "航空公司目標價",
                min_value=100.0,
                max_value=1_000_000.0,
                value=float(saved.get("target_price") or 10000),
                step=100.0,
                key=f"{prefix}_{code}_target",
            )
            c1, c2, c3 = st.columns(3)
            direct = c1.checkbox(
                "僅限直飛",
                value=bool(saved.get("direct_only", False)),
                key=f"{prefix}_{code}_direct",
            )
            max_stops = int(
                c2.number_input(
                    "最多轉機",
                    min_value=0,
                    max_value=3,
                    value=int(saved.get("max_stops") or 0),
                    disabled=direct,
                    key=f"{prefix}_{code}_stops",
                )
            )
            basis = c3.selectbox(
                "價格判定",
                ["total", "per_person"],
                index=1 if saved.get("price_basis") == "per_person" else 0,
                format_func=lambda item: "每人價格" if item == "per_person" else "行程總價",
                key=f"{prefix}_{code}_basis",
            )
            window = saved.get("outbound_window") or {}
            has_outbound_window = st.checkbox(
                "限制去程出發時段",
                value=bool(window),
                key=f"{prefix}_{code}_outbound_window_enabled",
            )
            outbound_window = None
            if has_outbound_window:
                wc1, wc2 = st.columns(2)
                out_start = wc1.time_input(
                    "去程最早",
                    value=_clock_value(window.get("start"), time(6, 0)),
                    key=f"{prefix}_{code}_out_start",
                )
                out_end = wc2.time_input(
                    "去程最晚",
                    value=_clock_value(window.get("end"), time(22, 0)),
                    key=f"{prefix}_{code}_out_end",
                )
                outbound_window = {"start": out_start.strftime("%H:%M"), "end": out_end.strftime("%H:%M")}
            return_window = None
            if round_trip:
                saved_return = saved.get("return_window") or {}
                has_return_window = st.checkbox(
                    "限制回程出發時段",
                    value=bool(saved_return),
                    key=f"{prefix}_{code}_return_window_enabled",
                )
                if has_return_window:
                    rc1, rc2 = st.columns(2)
                    ret_start = rc1.time_input(
                        "回程最早",
                        value=_clock_value(saved_return.get("start"), time(6, 0)),
                        key=f"{prefix}_{code}_ret_start",
                    )
                    ret_end = rc2.time_input(
                        "回程最晚",
                        value=_clock_value(saved_return.get("end"), time(22, 0)),
                        key=f"{prefix}_{code}_ret_end",
                    )
                    return_window = {"start": ret_start.strftime("%H:%M"), "end": ret_end.strftime("%H:%M")}
            rules.append(
                {
                    "airline": code,
                    "target_price": float(target),
                    "direct_only": direct,
                    "max_stops": 0 if direct else max_stops,
                    "outbound_window": outbound_window,
                    "return_window": return_window,
                    "price_basis": basis,
                    "require_same_airline_roundtrip": round_trip,
                }
            )
    return AirlineRuleSet.from_dict(rules).to_dict()["rules"]


def _quota_forecast(mode: str, runs_per_day: float, monitor_count: int = 1) -> dict[str, Any]:
    usage = db.get_api_usage_summary("serpapi", SERPAPI_MONTHLY_LIMIT)
    return QuotaPolicy(mode=mode, monthly_limit=SERPAPI_MONTHLY_LIMIT).forecast(
        runs_per_day=runs_per_day,
        monitor_count=monitor_count,
        used_requests=int(usage["attempted"]),
    ).to_dict()


def _remaining_month_projection(
    monitors: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Count future slots through month-end instead of comparing full months."""
    current = (now or _now()).astimezone(TAIPEI_TZ)
    month_end = (current.replace(day=28) + timedelta(days=4)).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    run_count = 0
    request_count = 0
    for monitor in monitors:
        if not bool(monitor.get("active", True)):
            continue
        policy = _schedule_policy(
            str(monitor.get("schedule_type") or "global"),
            str(monitor.get("schedule_value") or ""),
        )
        candidate = _parse_datetime(monitor.get("next_run_at"))
        if candidate is None or candidate <= current:
            candidate = policy.next_run(
                current,
                last_run=_parse_datetime(monitor.get("last_checked")),
            )
        monitor_runs = 0
        # A five-minute interval is the supported maximum frequency.  The
        # bound protects the UI from malformed future policy implementations.
        while candidate < month_end and monitor_runs < 10_000:
            monitor_runs += 1
            candidate = policy.next_run(candidate, last_run=candidate)
        run_count += monitor_runs
        request_count += monitor_runs * QuotaPolicy(
            mode=str(monitor.get("quota_mode") or "full")
        ).max_requests_per_run
    return {"runs": run_count, "requests": request_count}


def _flight_leg_payload(
    flight: dict[str, Any],
    *,
    flight_date: Any,
    origin: str,
    destination: str,
) -> dict[str, Any]:
    return {
        "date": flight_date or "",
        "airline": flight.get("airline") or "航空公司待確認",
        "flight_number": flight.get("flight_number") or "",
        "departure_time": flight.get("departure_time") or "--:--",
        "arrival_time": flight.get("arrival_time") or "--:--",
        "origin": flight.get("departure") or origin,
        "destination": flight.get("arrival") or destination,
        "duration": flight.get("duration") or "",
        "stops": flight.get("stops"),
    }


def _render_flight_pair(result: dict[str, Any], flight: dict[str, Any], *, best: bool = False) -> bool:
    returning = flight.get("return_flight") or flight.get("returning")
    if not isinstance(returning, dict):
        return False
    outbound = _flight_leg_payload(
        flight,
        flight_date=result.get("outbound_date"),
        origin=str(result.get("origin") or "---"),
        destination=str(result.get("destination") or "---"),
    )
    inbound = _flight_leg_payload(
        returning,
        flight_date=result.get("return_date"),
        origin=str(result.get("destination") or "---"),
        destination=str(result.get("origin") or "---"),
    )
    st.html(
        ui.flight_pair_card(
            outbound,
            inbound,
            total_price=flight.get("price"),
            currency=result.get("currency") or "TWD",
            best=best,
            price_note=f"{int(result.get('adults') or 1)} 位旅客行程總價",
        )
    )
    return True


def _upload_to_temp(upload: Any, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile(prefix="skywatch-upload-", suffix=suffix, delete=False) as handle:
        handle.write(upload.getvalue())
        return Path(handle.name)


def page_dashboard() -> None:
    health = get_scheduler_health()
    stats = db.get_dashboard_stats()
    usage = db.get_api_usage_summary("serpapi", SERPAPI_MONTHLY_LIMIT)
    runs = db.list_check_runs(limit=6)
    outbox = db.list_notification_outbox(limit=200)
    failed_outbox = sum(item.get("status") == "failed" for item in outbox)

    _page_header(
        "營運總覽",
        "一眼確認排程、額度、票價監控與通知是否正常。",
        eyebrow="OPERATIONS",
        badges=[
            {"text": "系統正常" if health["healthy"] else "需要檢查", "tone": "success" if health["healthy"] else "warning"},
            {"text": f"{len(configured_channels())} 個通知管道", "tone": "info"},
        ],
    )

    if health["healthy"]:
        st.html(
            ui.status_banner(
                "排程器運作正常",
                f"最近心跳 {_format_datetime(health['updated_at'])}，下一次預計 {_format_datetime(health['next_run'])}。",
                status="success",
                label="即時",
                details=[f"已自動補跑 {health['recovered_count']} 次", f"通知重試 {health['retry_count']} 次"],
            )
        )
    else:
        details = [f"最後心跳：{_format_datetime(health['updated_at'])}"]
        if health.get("last_error"):
            details.append(f"最近錯誤：{health['last_error']}")
        st.html(
            ui.status_banner(
                "排程器未回報正常心跳",
                "介面本身可以使用，但自動查價可能沒有持續執行。請到「系統設定」查看啟動方式。",
                status="danger",
                label="需處理",
                details=details,
            )
        )

    _metric_row(
        [
            {"label": "啟用中監控", "value": stats["active_monitors"], "caption": f"共 {stats['total_monitors']} 筆", "tone": "primary", "icon": "◎"},
            {"label": "達到目標價", "value": stats["hit_count"], "caption": "目前啟用項目", "tone": "success", "icon": "↓"},
            {"label": "24 小時成功通知", "value": stats["notifications_24h"], "caption": f"共嘗試 {stats['notification_attempts_24h']} 次", "tone": "info", "icon": "↗"},
            {"label": "待處理通知異常", "value": failed_outbox, "caption": "可在執行與通知重試", "tone": "danger" if failed_outbox else "neutral", "icon": "!"},
        ]
    )

    # 新增 heartbeat 觀測統計 (item 11)
    if health.get("last_checked_count", 0) or health.get("last_hit_count", 0):
        _metric_row(
            [
                {"label": "最近檢查數", "value": health.get("last_checked_count", 0), "caption": "本次執行", "tone": "primary", "icon": "≡"},
                {"label": "命中目標", "value": health.get("last_hit_count", 0), "caption": "達標次數", "tone": "success", "icon": "↓"},
                {"label": "成功率", "value": f"{health.get('last_success_rate', 0.0) * 100:.1f}%", "caption": "OK / 總數", "tone": "info", "icon": "%"},
                {"label": "執行耗時", "value": f"{health.get('last_run_duration', 0.0):.2f}s", "caption": "最近一次", "tone": "neutral", "icon": "⏱"},
            ]
        )

    left, right = st.columns([1.25, 1])
    with left:
        st.html(ui.section_heading("API 額度", "每次網路查價都會計入嘗試次數。", eyebrow="QUOTA"))
        if usage["limit"]:
            st.html(
                ui.quota_meter(
                    usage["attempted"],
                    usage["limit"],
                    label="SerpAPI 每月額度",
                    mode=f"警戒 {API_USAGE_WARNING_PERCENT:.0f}%",
                    reset_at="下個月",
                )
            )
        else:
            st.info(f"本月已嘗試 {usage['attempted']} 次；尚未設定月額度上限。")
        active = db.list_monitors(active_only=True)
        projected = sum(
            _quota_forecast(str(item.get("quota_mode") or "full"), _monitor_runs_per_day(item))["monthly_requests"]
            for item in active
        )
        st.caption(f"依目前每筆監控策略粗估，每月最多約 {projected:,} 次供應商請求。")
        remainder = _remaining_month_projection(active)
        st.caption(
            f"從現在至月底尚有 {remainder['runs']:,} 個排程時段，"
            f"最多約需 {remainder['requests']:,} 次供應商請求。"
        )
    with right:
        st.html(ui.section_heading("服務狀態", "健康狀態來自實際設定與 heartbeat。", eyebrow="HEALTH"))
        c1, c2 = st.columns(2)
        with c1:
            st.html(
                ui.status_card(
                    "即時票價",
                    "可用" if flight_api_ready() else "未設定",
                    status="success" if flight_api_ready() else "danger",
                    description="SerpAPI",
                )
            )
        with c2:
            channels = configured_channels()
            external = [item for item in channels if "only" not in item.lower()]
            st.html(
                ui.status_card(
                    "外部通知",
                    "、".join(external) if external else "僅本機紀錄",
                    status="success" if external else "warning",
                    description="可靠佇列與失敗重試",
                )
            )

    st.html(ui.section_heading("最近執行", "查價、漏跑補查與手動檢查皆留下可追溯紀錄。", eyebrow="RUN LEDGER"))
    if not runs:
        st.html(ui.empty_state("尚無執行紀錄", "建立監控並執行一次查價後，這裡會顯示結果。", icon="○"))
    else:
        for run in runs:
            route = f"{run.get('origin') or '—'} → {run.get('destination') or '—'}"
            message = str(run.get("error_message") or f"API {int(run.get('api_calls') or 0)} 次 · {run.get('trigger') or 'unknown'}")
            st.html(
                ui.run_status(
                    str(run.get("status") or "unknown"),
                    title=route,
                    message=message,
                    timestamp=_format_datetime(run.get("started_at")),
                )
            )

    st.html(ui.section_heading("重點監控", "優先顯示已達標或最近檢查的航線。", eyebrow="MONITORS"))
    if not stats["top_monitors"]:
        st.html(ui.empty_state("尚未建立監控", "先到「搜尋與比較」完成一次即時查價並建立策略。", icon="✈"))
    else:
        for monitor in stats["top_monitors"]:
            st.html(
                ui.monitor_card(
                    monitor["origin"],
                    monitor["destination"],
                    outbound_date=monitor["outbound_date"],
                    return_date=monitor.get("return_date") or "",
                    target_price=monitor.get("target_price"),
                    current_price=monitor.get("last_price"),
                    active=bool(monitor.get("active")),
                    hit=bool(monitor.get("last_price") is not None and float(monitor["last_price"]) <= float(monitor["target_price"])),
                    monitor_id=monitor["id"],
                    schedule=_schedule_label(monitor.get("schedule_type"), monitor.get("schedule_value")),
                    notification_policy=NOTIFICATION_LABELS.get(str(monitor.get("notification_policy") or "always"), "—"),
                )
            )


def _render_search_results(result: dict[str, Any]) -> None:
    flights = result.get("flights") or []
    best = result.get("best_price")
    st.html(ui.section_heading("即時比較結果", format_flight_summary(result), eyebrow="LIVE FARES"))
    _metric_row(
        [
            {"label": "目前最低總價", "value": _currency(best), "caption": result.get("currency", "TWD"), "tone": "success", "icon": "↓"},
            {"label": "可比較組合", "value": len(flights), "caption": "同一次查詢結果", "tone": "primary", "icon": "≡"},
            {"label": "旅客", "value": result.get("adults", 1), "caption": CLASS_NAMES.get(int(result.get("travel_class") or 1), "經濟艙"), "tone": "neutral", "icon": "○"},
        ]
    )
    if not flights:
        st.warning("供應商目前沒有回傳可比較的航班。")
        return

    filter1, filter2, filter3 = st.columns(3)
    direct = filter1.checkbox("只顯示直飛", key="search_result_direct")
    max_stops = filter2.selectbox("最多轉機", [0, 1, 2, 3], index=2, disabled=direct, key="search_result_stops")
    sort = filter3.selectbox("排序", ["價格", "直飛優先", "出發時間"], key="search_result_sort")
    shown = [item for item in flights if int(item.get("stops") or 0) <= (0 if direct else int(max_stops))]
    if sort == "直飛優先":
        shown.sort(key=lambda item: (int(item.get("stops") or 0), float(item.get("price") or 10**12)))
    elif sort == "出發時間":
        shown.sort(key=lambda item: str(item.get("departure_time") or "99:99"))
    else:
        shown.sort(key=lambda item: float(item.get("price") or 10**12))
    paired: list[dict[str, Any]] = []
    if result.get("return_date"):
        paired = [item for item in shown if isinstance(item.get("return_flight") or item.get("returning"), dict)]
        national_codes = {AirlineCode(code) for code, _ in AIRLINES}
        other_pairs = [
            item
            for item in paired
            if normalize_airline(item.get("airline"), item.get("flight_number")) not in national_codes
        ]
        if other_pairs:
            st.html(
                ui.section_heading(
                    "其他完整來回航班組合",
                    "去程與回程屬於同一筆供應商行程總價；三大國籍航空在下方獨立比較。",
                    eyebrow="PAIRED ITINERARIES",
                )
            )
            for index, flight in enumerate(other_pairs[:6]):
                _render_flight_pair(result, flight, best=index == 0)
        elif not paired:
            st.warning("目前只有完整來回總價，尚未取得回程班次；完整模式將補查。")
    table = []
    for index, flight in enumerate(shown[:20], start=1):
        returning = flight.get("return_flight") or flight.get("returning")
        row = {
            "排序": index,
            "航空公司": flight.get("airline") or "未知",
            "去程航班": flight.get("flight_number") or "—",
            "去程出發": flight.get("departure_time") or "—",
            "去程抵達": flight.get("arrival_time") or "—",
            "轉機": int(flight.get("stops") or 0),
            "時間": flight.get("duration") or "—",
            "行程總價": float(flight.get("price") or 0),
        }
        if result.get("return_date"):
            row["回程航班"] = returning.get("flight_number") or "—" if isinstance(returning, dict) else "待補查"
            row["回程出發"] = returning.get("departure_time") or "—" if isinstance(returning, dict) else "待補查"
        table.append(row)

    def render_result_table() -> None:
        st.dataframe(
            _arrow_safe_frame(table),
            hide_index=True,
            width="stretch",
            column_config={"行程總價": st.column_config.NumberColumn(format="NT$ %.0f")},
        )

    if result.get("return_date") and paired:
        with st.expander("查看其他票價組合、去程摘要與回程資料狀態", expanded=False):
            st.caption("所有金額都是同一次查詢的完整來回行程總價；「待補查」不代表只有去程。")
            render_result_table()
    else:
        if result.get("return_date"):
            st.caption("所有金額都是完整來回行程總價；回程班次將依監控的額度模式補查。")
        render_result_table()
    national: list[dict[str, Any]] = []
    for code, name in AIRLINES:
        airline_code = AirlineCode(code)
        candidates = [
            item
            for item in shown
            if normalize_airline(item.get("airline"), item.get("flight_number")) == airline_code
        ]
        if candidates:
            best_flight = min(candidates, key=lambda item: float(item.get("price") or 10**12))
            national.append(
                {
                    "航空公司": name,
                    "最低行程總價": float(best_flight["price"]),
                    "可比較組合": len(candidates),
                    "best_flight": best_flight,
                }
            )
    st.html(ui.section_heading("三大國籍航空", "只以本次完整行程報價比較，不拼接不同查詢的單程價格。", eyebrow="TAIWAN CARRIERS"))
    if national:
        if result.get("return_date"):
            paired_national = [
                carrier
                for carrier in national
                if isinstance(
                    carrier["best_flight"].get("return_flight") or carrier["best_flight"].get("returning"),
                    dict,
                )
            ]
            unpaired_national = [carrier for carrier in national if carrier not in paired_national]
            for carrier in paired_national:
                st.markdown(f"**{carrier['航空公司']}**")
                _render_flight_pair(
                    result,
                    carrier["best_flight"],
                    best=float(carrier["best_flight"].get("price") or 10**12)
                    == min(float(item.get("price") or 10**12) for item in shown),
                )
            if unpaired_national:
                st.caption("下列航空公司已有完整來回總價，但回程班次仍待完整模式補查。")
                st.dataframe(
                    _arrow_safe_frame(
                        [
                            {
                                "航空公司": carrier["航空公司"],
                                "最低行程總價": carrier["最低行程總價"],
                                "可比較組合": carrier["可比較組合"],
                                "回程資料": "待完整模式補查",
                            }
                            for carrier in unpaired_national
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                    column_config={"最低行程總價": st.column_config.NumberColumn(format="NT$ %.0f")},
                )
        else:
            st.dataframe(
                _arrow_safe_frame([{key: value for key, value in item.items() if key != "best_flight"} for item in national]),
                hide_index=True,
                width="stretch",
                column_config={"最低行程總價": st.column_config.NumberColumn(format="NT$ %.0f")},
            )
    else:
        st.caption("本次回應沒有三大國籍航空的可用組合。")


def _create_monitor_from_result(result: dict[str, Any]) -> None:
    st.html(ui.section_heading("建立監控策略", "把查價結果轉成可獨立排程與通知的監控。", eyebrow="CORE FLOW"))
    flow_step = max(1, min(2, int(st.session_state.get("monitor_flow_step", 1))))
    st.html(ui.stepper(["完成查價", "設定條件", "確認排程"], current=flow_step))
    default_target = max(100, int(float(result.get("best_price") or 10000) * 0.9))
    with st.form("create_monitor_form"):
        general, quota_tab, airline_tab, delivery_tab = st.tabs(["目標", "額度", "航空條件", "排程與通知"])
        with general:
            target = st.number_input("一般目標總價", min_value=100.0, max_value=1_000_000.0, value=float(default_target), step=100.0)
            st.caption("一般目標價適用於所有航空公司；下方專屬規則可另外覆蓋三家國籍航空。")
        with quota_tab:
            quota_mode = st.radio(
                "額度模式",
                ["lean", "standard", "full"],
                index=1,
                format_func=lambda item: f"{QUOTA_LABELS[item]} · 每次最多 {QuotaPolicy(mode=item).max_requests_per_run} 個請求",
                key="create_quota_mode",
            )
            st.caption("精簡適合大量監控；完整模式可補查更多回程或開票細節，但消耗額度較快。")
        with airline_tab:
            airline_rules = _airline_rule_fields(
                "create",
                round_trip=bool(result.get("return_date")),
            )
        with delivery_tab:
            schedule_type, schedule_value, schedule = _schedule_fields("create")
            notification_strategy, quiet_hours = _notification_fields("create")
            forecast = _quota_forecast(quota_mode, schedule.runs_per_day)
            new_monitor_remainder = _remaining_month_projection(
                [
                    {
                        "active": True,
                        "quota_mode": quota_mode,
                        "schedule_type": schedule_type,
                        "schedule_value": schedule_value,
                        "next_run_at": schedule.next_run(_now()).isoformat(timespec="seconds"),
                    }
                ]
            )
            st.caption(
                f"完整月份最多約 {forecast['monthly_requests']:,} 次；從現在至月底新增這筆監控最多約需 "
                f"{new_monitor_remainder['requests']:,} 次。"
            )
            if forecast["remaining"] is not None:
                st.caption(f"SerpAPI 本月目前尚有 {forecast['remaining']:,} 次可用額度。")
        with st.container(border=True):
            st.markdown("**建立前確認摘要**")
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("航線", f"{result.get('origin') or '—'} → {result.get('destination') or '—'}")
            s2.metric("目標總價", _currency(target))
            s3.metric("額度模式", QUOTA_LABELS[quota_mode])
            s4.metric("完整月最多", f"{forecast['monthly_requests']:,} 次")
            st.caption(
                f"{result.get('outbound_date') or '—'}"
                f"{' → ' + str(result.get('return_date')) if result.get('return_date') else ' · 單程'} · "
                f"{_schedule_label(schedule_type, schedule_value)} · "
                f"{NOTIFICATION_LABELS[notification_strategy]} · "
                f"{len(airline_rules)} 家航空公司專屬條件"
            )
            confirmation = st.checkbox(
                "我已確認日期、目標價、額度成本、排程與通知策略",
                key="create_monitor_confirmation",
            )
        submitted = st.form_submit_button("建立監控", type="primary", width="stretch")
    if submitted:
        if not confirmation:
            st.session_state["monitor_flow_step"] = 1
            st.warning("請先確認摘要內容，再建立監控。")
            return
        try:
            rules = AirlineRuleSet.from_dict(airline_rules).to_dict()["rules"]
            NotificationPolicy.from_dict({"strategy": notification_strategy, "quiet_hours": quiet_hours or None})
            validated_schedule = _schedule_policy(schedule_type, schedule_value)
            next_run = validated_schedule.next_run(_now()).isoformat(timespec="seconds")
            saved = db.add_monitor(
                route_id=int(result["route_id"]),
                outbound_date=str(result["outbound_date"]),
                return_date=result.get("return_date") or None,
                target_price=float(target),
                adults=int(result.get("adults") or 1),
                travel_class=int(result.get("travel_class") or 1),
                trip_type="round_trip" if result.get("return_date") else "one_way",
                quota_mode=quota_mode,
                airline_rules=rules,
                schedule_type=schedule_type,
                schedule_value=schedule_value,
                notification_policy=notification_strategy,
                quiet_hours=quiet_hours,
                next_run_at=next_run,
            )
            st.session_state["monitor_flow_step"] = 2
            if saved.get("duplicate"):
                st.info(f"相同行程已存在；已更新監控 #{saved['id']} 的完整策略。")
            else:
                st.success(f"已建立監控 #{saved['id']}，下次預計 {_format_datetime(next_run)} 執行。")
        except (TypeError, ValueError) as exc:
            st.error(f"策略無法儲存：{exc}")


def page_search() -> None:
    _page_header(
        "搜尋與比較",
        "先用一次即時查詢取得完整行程價格，再建立監控策略。",
        eyebrow="SEARCH",
        badges=[{"text": "SerpAPI 即時驗證", "tone": "success" if flight_api_ready() else "danger"}],
    )
    st.html(
        ui.status_banner(
            "這個動作會消耗 1 次 SerpAPI 額度",
            "只有按下搜尋後才會連線；重新整理頁面不會重複查價。",
            status="info",
            label="成本提示",
        )
    )

    # 快捷熱門航線晶片
    st.caption("熱門航線快捷填入：")
    quick_routes = [
        ("🇹🇼 台北 (TPE) ⇄ 🇯🇵 東京成田 (NRT)", "TPE", "NRT"),
        ("🇹🇼 台北 (TPE) ⇄ 🇯🇵 大阪關西 (KIX)", "TPE", "KIX"),
        ("🇹🇼 台北 (TPE) ⇄ 🇹🇭 曼谷 (BKK)", "TPE", "BKK"),
        ("🇹🇼 台北 (TPE) ⇄ 🇰🇷 首爾仁川 (ICN)", "TPE", "ICN"),
        ("🇹🇼 高雄 (KHH) ⇄ 🇯🇵 東京成田 (NRT)", "KHH", "NRT"),
    ]
    chip_cols = st.columns(len(quick_routes))
    for col, (label, orig, dest) in zip(chip_cols, quick_routes, strict=True):
        with col:
            if st.button(label, key=f"chip_{orig}_{dest}", width="stretch"):
                st.session_state["search_origin_query"] = orig
                st.session_state["search_destination_query"] = dest
                st.rerun()

    orig_col, swap_col, dest_col = st.columns([1.2, 0.25, 1.2])
    with orig_col:
        default_orig = st.session_state.get("search_origin_val", "TPE")
        origin = _airport_input("出發機場", "search_origin", default_orig)
        st.session_state["search_origin_val"] = origin
    with swap_col:
        st.write("")
        st.write("")
        if st.button("⇄", help="對調出發地與目的地", key="swap_airports_btn", width="stretch"):
            curr_orig = st.session_state.get("search_origin_val", "TPE")
            curr_dest = st.session_state.get("search_destination_val", "KIX")
            st.session_state["search_origin_query"] = curr_dest
            st.session_state["search_destination_query"] = curr_orig
            st.session_state["search_origin_val"] = curr_dest
            st.session_state["search_destination_val"] = curr_orig
            st.rerun()
    with dest_col:
        default_dest = st.session_state.get("search_destination_val", "KIX")
        destination = _airport_input("目的機場", "search_destination", default_dest)
        st.session_state["search_destination_val"] = destination

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        trip_type = st.radio("行程", ["來回", "單程"], horizontal=True)
    with c2:
        outbound = st.date_input("出發日期", value=date.today() + timedelta(days=30), min_value=date.today())
    with c3:
        return_date = (
            st.date_input("回程日期", value=outbound + timedelta(days=7), min_value=outbound)
            if trip_type == "來回"
            else None
        )
    with c4:
        adults = int(st.number_input("成人", min_value=1, max_value=9, value=1))
        class_label = st.selectbox("艙等", list(CLASS_CODES))
    confirm = st.checkbox("我了解本次即時搜尋會使用 1 次 SerpAPI 額度", key="confirm_live_search")
    search_button_type = "secondary" if st.session_state.get("search_result") else "primary"
    if st.button("搜尋即時票價", type=search_button_type, width="stretch", disabled=not confirm):
        if origin == destination:
            st.error("出發地與目的地不可相同。")
        elif not flight_api_ready():
            st.error("尚未設定 SERPAPI_API_KEY，請先到系統設定確認。")
        else:
            try:
                with st.spinner("正在查詢即時票價並寫入歷史紀錄…"):
                    result = search_and_save(
                        origin=origin,
                        destination=destination,
                        outbound_date=outbound.isoformat(),
                        return_date=return_date.isoformat() if return_date else None,
                        adults=adults,
                        travel_class=CLASS_CODES[class_label],
                    )
                st.session_state["search_result"] = result
                st.session_state["monitor_flow_step"] = 1
                st.session_state["create_monitor_confirmation"] = False
                st.success("查價完成。")
            except FlightAPIError as exc:
                st.error(f"查價失敗：{exc}")
            except Exception as exc:
                st.error(f"查價流程發生未預期錯誤：{exc}")
    result = st.session_state.get("search_result")
    if not result:
        st.html(ui.empty_state("尚無查詢結果", "設定行程並確認額度成本後開始查價。", icon="✈"))
        return
    _render_search_results(result)
    _create_monitor_from_result(result)


def _render_monitor_summary(monitor: dict[str, Any]) -> None:
    rules = _json_list(monitor.get("airline_rules_json"))
    st.html(
        ui.monitor_card(
            monitor["origin"],
            monitor["destination"],
            outbound_date=monitor["outbound_date"],
            return_date=monitor.get("return_date") or "",
            target_price=monitor.get("target_price"),
            current_price=monitor.get("last_price"),
            active=bool(monitor.get("active")),
            hit=bool(monitor.get("last_price") is not None and float(monitor["last_price"]) <= float(monitor["target_price"])),
            monitor_id=monitor["id"],
            airlines=[dict(AIRLINES).get(str(item.get("airline")), str(item.get("airline"))) for item in rules],
            direct_only=any(bool(item.get("direct_only")) for item in rules),
            schedule=_schedule_label(monitor.get("schedule_type"), monitor.get("schedule_value")),
            notification_policy=NOTIFICATION_LABELS.get(str(monitor.get("notification_policy") or "always"), "—"),
        )
    )


def page_monitors() -> None:
    monitors = db.list_monitors(active_only=False)
    active_count = sum(bool(item.get("active")) for item in monitors)
    hit_count = sum(
        bool(item.get("last_price") is not None and float(item["last_price"]) <= float(item["target_price"]))
        for item in monitors if item.get("active")
    )
    _page_header(
        "監控策略",
        "每筆行程都有自己的航空條件、額度模式、排程與通知規則。",
        eyebrow="MONITOR POLICIES",
        badges=[
            {"text": f"{active_count} 筆啟用", "tone": "success"},
            {"text": f"{hit_count} 筆已達標", "tone": "primary" if hit_count else "neutral"},
            {"text": f"共 {len(monitors)} 筆", "tone": "neutral"},
        ],
    )
    if not monitors:
        st.html(ui.empty_state("尚未建立監控", "先到「搜尋與比較」完成即時查價，再建立第一筆策略。", icon="◎"))
        return

    # 批次操作與即時快篩工具列
    with st.expander("⚡ 批次管理與篩選工具", expanded=False):
        b1, b2, f1, f2 = st.columns([1, 1, 1.5, 1.5])
        with b1:
            if st.button("全部啟用", key="batch_enable_all", width="stretch"):
                for m in monitors:
                    db.set_monitor_active(int(m["id"]), True)
                st.success("已全部啟用所有監控項目。")
                st.rerun()
        with b2:
            if st.button("全部暫停", key="batch_pause_all", width="stretch"):
                for m in monitors:
                    db.set_monitor_active(int(m["id"]), False)
                st.info("已全部暫停所有監控項目。")
                st.rerun()
        with f1:
            status_filter = st.selectbox("狀態篩選", ["全部", "僅啟用中", "僅暫停中", "🎯 僅達標項目"], key="monitor_status_filter")
        with f2:
            search_query = st.text_input("航線搜尋", placeholder="例如 TPE 或 BKK", key="monitor_search_query").strip().upper()

    filtered_monitors = list(monitors)
    if status_filter == "僅啟用中":
        filtered_monitors = [m for m in filtered_monitors if m.get("active")]
    elif status_filter == "僅暫停中":
        filtered_monitors = [m for m in filtered_monitors if not m.get("active")]
    elif status_filter == "🎯 僅達標項目":
        filtered_monitors = [
            m for m in filtered_monitors
            if m.get("last_price") is not None and float(m["last_price"]) <= float(m["target_price"])
        ]

    if search_query:
        filtered_monitors = [
            m for m in filtered_monitors
            if search_query in str(m.get("origin", "")).upper() or search_query in str(m.get("destination", "")).upper()
        ]

    if not filtered_monitors:
        st.warning("沒有符合目前篩選條件的監控項目。")
        return

    selector = {
        f"#{item['id']} · {item['origin']} → {item['destination']} · {item['outbound_date']}" + (
            " 🎯 已達標" if item.get("last_price") is not None and float(item["last_price"]) <= float(item["target_price"]) else ""
        ) + (" (已暫停)" if not item.get("active") else ""): item
        for item in filtered_monitors
    }
    selected_label = st.selectbox("選擇要管理的監控", list(selector))
    monitor = db.get_monitor(int(selector[selected_label]["id"])) or selector[selected_label]
    # 將監控卡片與底部操作整合為一體化卡片
    with st.container(border=True):
        _render_monitor_summary(monitor)
        st.divider()
        footer_info, footer_toggle, footer_check = st.columns([2.6, 1, 1], vertical_alignment="center")
        with footer_info:
            quota_label = QUOTA_LABELS.get(str(monitor.get("quota_mode") or "full"), "完整")
            next_run_str = _format_datetime(monitor.get("next_run_at")) if monitor.get("active") else "已暫停"
            st.caption(f"🕒 下次排程：**{next_run_str}** · 額度模式：{quota_label}")
        with footer_toggle:
            desired_active = not bool(monitor.get("active"))
            label = "▶️ 啟用監控" if desired_active else "⏸️ 暫停監控"
            btn_type = "primary" if desired_active else "secondary"
            if st.button(label, type=btn_type, width="stretch", key=f"toggle_active_{monitor['id']}"):
                try:
                    db.set_monitor_active(int(monitor["id"]), desired_active)
                    st.success(f"監控 #{monitor['id']} 已{'啟用' if desired_active else '暫停'}。")
                    st.rerun()
                except Exception as exc:
                    st.error(f"狀態更新失敗：{exc}")
        with footer_check:
            max_calls = QuotaPolicy(mode=str(monitor.get("quota_mode") or "full")).max_requests_per_run
            with st.popover("⚡ 立即查價", width="stretch"):
                st.markdown(f"**確認執行監控 #{monitor['id']} 查價？**")
                st.caption(f"此手動檢查最多將消耗 **{max_calls} 次** SerpAPI 額度，並同步寫入歷史紀錄。")
                if st.button("確認開始查價", type="primary", width="stretch", key=f"confirm_check_pop_{monitor['id']}"):
                    try:
                        with st.spinner("正在執行即時查價…"):
                            result = check_one_monitor(monitor, trigger="manual")
                        st.session_state[f"monitor_result_{monitor['id']}"] = result
                        if result.get("ok"):
                            st.success(
                                f"檢查完成：{_currency(result.get('best_price'))}；"
                                f"通知狀態 {result.get('notify_reason') or '—'}。"
                            )
                        else:
                            st.error(f"檢查失敗：{result.get('error') or '未知錯誤'}")
                    except Exception as exc:
                        st.error(f"手動檢查失敗：{exc}")

    st.html(ui.section_heading("編輯策略", "只有排程欄位變更時才會重新計算下一次執行時間。", eyebrow="POLICY EDITOR"))
    existing_rules = _json_list(monitor.get("airline_rules_json"))
    existing_quiet = _json_dict(monitor.get("quiet_hours_json"))
    prefix = f"edit_{monitor['id']}"
    with st.form(f"edit_monitor_{monitor['id']}"):
        base_tab, quota_tab, airline_tab, schedule_tab = st.tabs(["目標", "額度", "航空條件", "排程與通知"])
        with base_tab:
            st.metric("一般目標總價", _currency(monitor.get("target_price")))
            st.caption(
                f"{int(monitor.get('adults') or 1)} 位成人 · "
                f"{CLASS_NAMES.get(int(monitor.get('travel_class') or 1), '經濟艙')}"
            )
            st.info("一般目標價屬於行程基本資料；請從「搜尋與比較」重新確認即時價格後更新。此頁只編輯已支援的策略欄位。")
        with quota_tab:
            modes = list(QUOTA_LABELS)
            current_mode = str(monitor.get("quota_mode") or "full")
            quota_mode = st.radio(
                "額度模式",
                modes,
                index=modes.index(current_mode) if current_mode in modes else 2,
                format_func=lambda item: f"{QUOTA_LABELS[item]} · 每次最多 {QuotaPolicy(mode=item).max_requests_per_run} 次",
                key=f"{prefix}_quota",
            )
        with airline_tab:
            airline_rules = _airline_rule_fields(
                prefix,
                existing_rules,
                round_trip=bool(monitor.get("return_date")),
            )
        with schedule_tab:
            schedule_type, schedule_value, schedule = _schedule_fields(
                prefix,
                str(monitor.get("schedule_type") or "global"),
                str(monitor.get("schedule_value") or ""),
            )
            notification_strategy, quiet_hours = _notification_fields(
                prefix,
                str(monitor.get("notification_policy") or "always"),
                existing_quiet,
            )
            forecast = _quota_forecast(quota_mode, schedule.runs_per_day)
            st.caption(f"這筆策略每月最多約 {forecast['monthly_requests']:,} 次供應商請求。")
        save = st.form_submit_button("儲存策略", type="primary", width="stretch")
    if save:
        try:
            validated_rules = AirlineRuleSet.from_dict(airline_rules).to_dict()["rules"]
            NotificationPolicy.from_dict({"strategy": notification_strategy, "quiet_hours": quiet_hours or None})
            schedule_updates, next_run = _schedule_update_fields(
                monitor,
                schedule_type,
                schedule_value,
            )
            db.update_monitor_policies(
                int(monitor["id"]),
                quota_mode=quota_mode,
                airline_rules=validated_rules,
                notification_policy=notification_strategy,
                quiet_hours=quiet_hours,
                **schedule_updates,
            )
            if schedule_updates:
                st.success(f"策略與排程已儲存；下次預計 {_format_datetime(next_run)} 執行。")
            else:
                st.success(f"策略已儲存；原排程維持不變（下次 {_format_datetime(next_run)}）。")
        except (TypeError, ValueError) as exc:
            st.error(f"策略驗證失敗：{exc}")
        except Exception as exc:
            st.error(f"策略儲存失敗：{exc}")

    with st.expander("刪除這筆監控"):
        st.warning("刪除監控不會刪除既有票價與執行紀錄，但此動作無法復原。")
        expected = f"DELETE #{monitor['id']}"
        confirmation = st.text_input(f"輸入 {expected} 確認", key=f"delete_monitor_{monitor['id']}")
        if st.button("永久刪除監控", disabled=confirmation != expected, key=f"delete_button_{monitor['id']}"):
            try:
                db.delete_monitor(int(monitor["id"]))
                st.success("監控已刪除。")
                st.rerun()
            except Exception as exc:
                st.error(f"刪除失敗：{exc}")


def _history_rows(route_id: int) -> list[dict[str, Any]]:
    with db.get_db() as connection:
        rows = connection.execute(
            """
            SELECT h.*, r.origin, r.destination
            FROM price_history h JOIN routes r ON r.id=h.route_id
            WHERE h.route_id=? ORDER BY h.recorded_at ASC
            """,
            (int(route_id),),
        ).fetchall()
        return [dict(row) for row in rows]


def page_price_intelligence() -> None:
    routes = db.list_routes()
    _page_header(
        "價格情報",
        "整合每日兩次定時查價履歷、價格走勢與 Travelpayouts 市場趨勢。",
        eyebrow="PRICE INTELLIGENCE",
        badges=[
            {"text": "定時查價履歷", "tone": "primary"},
            {"text": "快取月曆", "tone": "info" if travelpayouts_ready() else "neutral"},
        ],
    )
    history_tab, calendar_tab = st.tabs(["📊 每日查價走勢與履歷", "📅 低價月曆"])
    with history_tab:
        if not routes:
            st.html(ui.empty_state("尚無票價歷史", "先到搜尋頁完成一次即時查價並建立監控。", icon="↗"))
        else:
            route_map = {f"{item['origin']} → {item['destination']} · {item.get('origin_name') or ''}": item for item in routes}
            selected = route_map[st.selectbox("選擇航線", list(route_map), key="history_route")]
            rows = _history_rows(int(selected["id"]))
            if not rows:
                st.info("這條航線尚無可繪製的價格紀錄。")
            else:
                frame = pd.DataFrame(rows)
                frame["recorded_at_dt"] = pd.to_datetime(frame["recorded_at"], errors="coerce")
                frame = frame.dropna(subset=["recorded_at_dt", "price"])
                frame["行程"] = frame.apply(
                    lambda row: (
                        f"{row['outbound_date']} → {row['return_date']}"
                        if pd.notna(row.get("return_date")) and str(row.get("return_date") or "")
                        else str(row["outbound_date"])
                    ),
                    axis=1,
                )

                # 智慧整理行程選單（按紀錄筆數排序，包含最新價格與次數）
                itinerary_counts = frame.groupby("行程").size()
                sorted_itineraries = list(itinerary_counts.sort_values(ascending=False).index)

                # 取得該航線所有監控策略
                route_monitors = {
                    (f"{m.get('outbound_date')} → {m.get('return_date')}" if m.get('return_date') else str(m.get('outbound_date'))): m
                    for m in db.list_monitors(active_only=False)
                    if m.get("route_id") == selected["id"]
                }

                def format_itinerary_label(itin: str) -> str:
                    count = itinerary_counts.get(itin, 0)
                    latest_p = frame[frame["行程"] == itin]["price"].iloc[-1]
                    mon = route_monitors.get(itin)
                    target_str = f" · 目標 {_currency(mon.get('target_price'))}" if mon else ""
                    return f"{itin}（累積 {count} 次查價 · 最新 {_currency(latest_p)}{target_str}）"

                itinerary_label_map = {format_itinerary_label(itin): itin for itin in sorted_itineraries}
                chosen_label = st.selectbox("選擇監控行程與出發日期", list(itinerary_label_map), key="history_itinerary_label")
                chosen_itinerary = itinerary_label_map[chosen_label]

                shown = frame[frame["行程"] == chosen_itinerary].copy()
                shown = shown.sort_values("recorded_at_dt", ascending=True).reset_index(drop=True)

                if shown.empty:
                    st.info("所選行程沒有有效價格資料。")
                else:
                    # 關聯該行程的監控目標價與排程
                    matched_monitor = route_monitors.get(chosen_itinerary)
                    target_ref = float(matched_monitor["target_price"]) if matched_monitor else None

                    # 計算價差與時段
                    shown["prev_price"] = shown["price"].shift(1)
                    shown["diff"] = shown["price"] - shown["prev_price"]
                    shown["diff_pct"] = (shown["diff"] / shown["prev_price"]) * 100
                    shown["hour"] = shown["recorded_at_dt"].dt.hour
                    shown["slot_label"] = shown["hour"].apply(lambda h: "☀️ 早盤 (00:00~11:59)" if h < 12 else "🌙 晚盤 (12:00~23:59)")
                    shown["date_label"] = shown["recorded_at_dt"].dt.strftime("%Y/%m/%d")
                    shown["time_label"] = shown["recorded_at_dt"].dt.strftime("%H:%M")

                    # 統計指標計算
                    p_min = shown["price"].min()
                    p_min_row = shown.loc[shown["price"].idxmin()]
                    p_max = shown["price"].max()
                    p_avg = shown["price"].mean()
                    p_last = shown.iloc[-1]["price"]
                    p_last_row = shown.iloc[-1]
                    diff_from_prev = p_last_row["diff"]
                    diff_from_prev_pct = p_last_row["diff_pct"]

                    if pd.isna(diff_from_prev) or diff_from_prev == 0:
                        change_caption = "較上次持平"
                        change_tone = "neutral"
                    elif diff_from_prev < 0:
                        change_caption = f"較上次下跌 NT$ {abs(diff_from_prev):,.0f} ({diff_from_prev_pct:.1f}%)"
                        change_tone = "success"
                    else:
                        change_caption = f"較上次上漲 NT$ {diff_from_prev:,.0f} (+{diff_from_prev_pct:.1f}%)"
                        change_tone = "danger"

                    min_detail = f"{p_min_row.get('airline', '')} · {p_min_row['date_label']} {p_min_row['slot_label'][:2]}"

                    metric_cards = [
                        {"label": "歷史最低價", "value": _currency(p_min), "caption": min_detail, "tone": "success", "icon": "↓"},
                        {"label": "歷史最高價", "value": _currency(p_max), "caption": f"區間價差 NT$ {p_max - p_min:,.0f}", "tone": "danger", "icon": "↑"},
                        {"label": "平均總價", "value": _currency(p_avg), "caption": f"共 {len(shown)} 次查價紀錄", "tone": "neutral", "icon": "~"},
                        {"label": "最新紀錄價", "value": _currency(p_last), "caption": change_caption, "tone": change_tone, "icon": "•"},
                    ]
                    if target_ref:
                        hit_now = p_last <= target_ref
                        hit_diff = abs(target_ref - p_last)
                        hit_caption = f"已達標 (低於目標 NT$ {hit_diff:,.0f})" if hit_now else f"尚未達標 (距目標差 NT$ {hit_diff:,.0f})"
                        metric_cards.insert(0, {"label": "設定目標價", "value": _currency(target_ref), "caption": hit_caption, "tone": "success" if hit_now else "primary", "icon": "🎯"})

                    _metric_row(metric_cards[:5])

                    # 溫馨提示
                    if len(shown) == 1:
                        st.html(
                            ui.status_banner(
                                "目前累積 1 筆查價紀錄",
                                "系統背景排程預設每日 00:00 與 12:00（或依監控設定頻率）自動執行查價，隨時間推移將持續繪製出完整的多日早/晚價格波段圖。",
                                status="info",
                                label="排程追蹤中",
                            )
                        )

                    # Plotly 趨勢折線圖
                    st.html(ui.section_heading("📈 每日查價價格走勢圖", "滑鼠懸停可查看各查價時點之班機、飛行時間與價差變動。", eyebrow="TREND CHART"))

                    shown["hover_text"] = shown.apply(
                        lambda r: (
                            f"<b>{r.get('airline', '未知航空')} {r.get('flight_number', '')}</b><br>"
                            f"📅 紀錄時間：{r['date_label']} {r['time_label']} ({r['slot_label']})<br>"
                            f"💰 行程總價：NT$ {r['price']:,.0f}<br>"
                            f"⏱️ 飛行時間：{r.get('duration', '—')} · 轉機 {int(r.get('stops') or 0)} 次<br>"
                            f"📊 較上次：{_price_delta_label(r['diff'], r['diff_pct'])}"
                        ),
                        axis=1,
                    )

                    figure = px.line(
                        shown,
                        x="recorded_at_dt",
                        y="price",
                        color="airline",
                        markers=True,
                        custom_data=["hover_text"],
                        labels={"recorded_at_dt": "查價時間", "price": "行程總價 (TWD)", "airline": "航空公司"},
                    )
                    figure.update_traces(
                        hovertemplate="%{custom_data[0]}<extra></extra>",
                        marker=dict(size=8, symbol="circle"),
                        line=dict(width=2.5),
                    )

                    # 如果只有 1 筆或少數資料，強制擴展 X 軸範圍前後 12 小時，避免出現毫秒級 tick
                    if len(shown) == 1:
                        single_t = shown["recorded_at_dt"].iloc[0]
                        figure.update_layout(
                            xaxis=dict(
                                range=[single_t - timedelta(hours=12), single_t + timedelta(hours=12)],
                                tickformat="%m/%d %H:%M",
                            )
                        )
                    else:
                        figure.update_layout(
                            xaxis=dict(
                                tickformat="%m/%d %H:%M",
                            )
                        )

                    if target_ref:
                        figure.add_hline(
                            y=target_ref,
                            line_dash="dash",
                            line_color="#10B981",
                            annotation_text=f"🎯 目標價 NT$ {target_ref:,.0f}",
                            annotation_position="top left",
                            annotation_font=dict(color="#10B981", size=12),
                        )

                    figure.update_layout(
                        template="plotly_white",
                        margin=dict(l=10, r=10, t=30, b=10),
                        legend_title_text="航空公司",
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="#ffffff",
                        hovermode="closest",
                        yaxis=dict(
                            tickprefix="NT$ ",
                            tickformat=",.0f",
                            showgrid=True,
                            gridcolor="#F1F5F9",
                        ),
                        xaxis=dict(
                            showgrid=True,
                            gridcolor="#F1F5F9",
                        ),
                    )
                    st.plotly_chart(figure, width="stretch")

                    # ✨ 核心重點：每日早/晚查價歷程明細表 (Daily Twice-Check Ledger)
                    st.html(ui.section_heading("📋 每日早/晚查價歷程明細 (Daily Twice-Check Ledger)", "按查價時間降冪排列，記錄每次早盤與晚盤之即時報價、航空公司與價差動態。", eyebrow="CHECK LEDGER"))

                    ledger_df = shown.sort_values("recorded_at_dt", ascending=False).copy()

                    ledger_rows = []
                    for _, r in ledger_df.iterrows():
                        diff_val = r["diff"]
                        diff_p = r["diff_pct"]
                        if pd.isna(diff_val) or diff_val == 0:
                            change_tag = "➖ 持平"
                        elif diff_val < 0:
                            change_tag = f"🟢 -NT$ {abs(diff_val):,.0f} ({diff_p:.1f}%)"
                        else:
                            change_tag = f"🔴 +NT$ {diff_val:,.0f} (+{diff_p:.1f}%)"

                        hit_tag = "—"
                        if target_ref:
                            if float(r["price"]) <= target_ref:
                                hit_tag = "🎯 達標"
                            else:
                                hit_tag = f"差 {_currency(r['price'] - target_ref)}"

                        dep_arr = f"{r.get('departure_time', '')} → {r.get('arrival_time', '')}".strip()
                        if dep_arr == "→":
                            dep_arr = "—"

                        ledger_rows.append(
                            {
                                "查價日期": r["date_label"],
                                "時段": r["slot_label"][:2],
                                "查價時間": r["time_label"],
                                "行程總價": float(r["price"]),
                                "較上次變動": change_tag,
                                "達標狀態": hit_tag,
                                "推薦航空": r.get("airline") or "—",
                                "航班編號": r.get("flight_number") or "—",
                                "飛行時間": r.get("duration") or "—",
                                "轉機": "直飛" if int(r.get("stops") or 0) == 0 else f"{r.get('stops')} 次",
                                "出發/抵達": dep_arr,
                            }
                        )

                    st.dataframe(
                        _arrow_safe_frame(ledger_rows),
                        hide_index=True,
                        width="stretch",
                        column_config={
                            "行程總價": st.column_config.NumberColumn(format="NT$ %.0f"),
                        },
                    )

                    # 下載完整 CSV
                    export_columns = [
                        "recorded_at", "date_label", "slot_label", "time_label", "outbound_date", "return_date",
                        "price", "currency", "diff", "diff_pct", "airline", "flight_number", "stops", "duration",
                        "departure_time", "arrival_time"
                    ]
                    available_exp = [c for c in export_columns if c in shown.columns]
                    st.download_button(
                        "📥 下載完整每日查價歷史 CSV (含早晚盤與價差標籤)",
                        data=shown[available_exp].to_csv(index=False).encode("utf-8-sig"),
                        file_name=f"skywatch-price-ledger-{selected['origin']}-{selected['destination']}-{chosen_itinerary.replace(' ', '')}.csv",
                        mime="text/csv",
                    )

    with calendar_tab:
        st.html(
            ui.status_banner(
                "月曆是快取市場趨勢，不是可購買報價",
                "查詢月曆不消耗 SerpAPI；選定日期後仍須用 1 次 SerpAPI 即時驗證，才可建立監控或觸發通知。",
                status="warning",
                label="必須驗證",
            )
        )
        c1, c2 = st.columns(2)
        with c1:
            cal_origin = _airport_input("月曆出發機場", "calendar_origin", "TPE")
        with c2:
            cal_destination = _airport_input("月曆目的機場", "calendar_destination", "KIX")
        q1, q2, q3 = st.columns(3)
        with q1:
            month_date = st.date_input("查詢月份", value=(date.today() + timedelta(days=30)).replace(day=1), key="calendar_month")
        with q2:
            calendar_trip = st.radio("行程類型", ["單程", "來回"], horizontal=True, key="calendar_trip")
        with q3:
            calendar_return = (
                st.date_input("固定回程日期", value=month_date + timedelta(days=14), min_value=month_date, key="calendar_return")
                if calendar_trip == "來回"
                else None
            )
        st.caption("這是使用 Travelpayouts Data API 的唯讀快取查詢，不會自動建立通知。")
        if st.button("載入低價月曆", width="stretch", disabled=not travelpayouts_ready()):
            if cal_origin == cal_destination:
                st.error("出發地與目的地不可相同。")
            else:
                try:
                    with st.spinner("正在載入快取市場資料…"):
                        calendar_data = get_low_fare_calendar(
                            cal_origin,
                            cal_destination,
                            month_date.strftime("%Y-%m"),
                            return_date=calendar_return.isoformat() if calendar_return else None,
                        )
                    st.session_state["fare_calendar"] = calendar_data
                except TravelpayoutsError as exc:
                    st.error(f"月曆查詢失敗：{exc}")
                except Exception as exc:
                    st.error(f"月曆流程發生未預期錯誤：{exc}")
        if not travelpayouts_ready():
            st.info("尚未設定 TRAVELPAYOUTS_TOKEN；請在環境設定完成後重新啟動服務。")
        calendar_data = st.session_state.get("fare_calendar")
        if calendar_data and calendar_data.get("days"):
            days = pd.DataFrame(calendar_data["days"])

            # 航空公司代碼對照中文名稱
            airline_name_map = {
                "BR": "長榮航空 (BR)",
                "CI": "中華航空 (CI)",
                "JX": "星宇航空 (JX)",
                "IT": "台灣虎航 (IT)",
                "TG": "泰國航空 (TG)",
                "VZ": "泰越捷 (VZ)",
                "VJ": "越捷航空 (VJ)",
                "FD": "泰國亞航 (FD)",
                "AK": "亞洲航空 (AK)",
                "D7": "全亞航 (D7)",
                "MM": "樂桃航空 (MM)",
                "GK": "捷星日本 (GK)",
                "3K": "捷星亞洲 (3K)",
                "JL": "日本航空 (JL)",
                "NH": "全日空 (NH)",
                "CX": "國泰航空 (CX)",
                "HX": "香港航空 (HX)",
                "UO": "香港快運 (UO)",
                "TR": "酷航 (TR)",
                "SQ": "新加坡航空 (SQ)",
                "OZ": "韓亞航空 (OZ)",
                "KE": "大韓航空 (KE)",
                "7C": "濟州航空 (7C)",
                "LJ": "真航空 (LJ)",
                "TW": "德威航空 (TW)",
                "ZE": "易斯達航空 (ZE)",
                "BX": "釜山航空 (BX)",
                "MF": "廈門航空 (MF)",
                "CA": "中國國航 (CA)",
                "CZ": "南方航空 (CZ)",
                "MU": "東方航空 (MU)",
                "PR": "菲律賓航空 (PR)",
                "5J": "宿霧太平洋 (5J)",
                "MH": "馬來西亞航空 (MH)",
                "VN": "越南航空 (VN)",
                "QH": "越竹航空 (QH)",
                "SL": "泰獅航 (SL)",
                "OD": "巴澤航空 (OD)",
            }

            def _get_airline_display(code: Any) -> str:
                raw = str(code or "").strip().upper()
                return airline_name_map.get(raw, f"{raw} 航空" if raw else "未標示航空")

            def _get_airport_detail(orig: str, dest: str, code: Any) -> tuple[str, str]:
                c = str(code or "").strip().upper()
                d = dest.strip().upper()
                o = orig.strip().upper()
                if d in ("BKK", "DMK") or o in ("BKK", "DMK"):
                    if c in ("SL", "FD", "IT", "XJ", "DD"):
                        return "DMK", "廊曼 (DMK)"
                    elif c in ("BR", "CI", "JX", "TG", "VZ"):
                        return "BKK", "蘇凡納布 (BKK)"
                elif d in ("TYO", "NRT", "HND") or o in ("TYO", "NRT", "HND"):
                    if c in ("MM", "GK", "IT", "SL"):
                        return "NRT", "成田 (NRT)"
                    elif o == "TSA" or d == "TSA":
                        return "HND", "羽田 (HND)"
                return d, d

            days["airline_display"] = days["airline"].apply(_get_airline_display)
            days["target_airport_code"] = days["airline"].apply(lambda c: _get_airport_detail(str(calendar_data.get("origin", "")), str(calendar_data.get("destination", "")), c)[0])
            days["actual_airport"] = days["airline"].apply(lambda c: _get_airport_detail(str(calendar_data.get("origin", "")), str(calendar_data.get("destination", "")), c)[1])
            days["stops_display"] = days["transfers"].apply(lambda t: "直飛 (0 次轉機)" if t == 0 else f"{int(t)} 次轉機" if pd.notna(t) and str(t).isdigit() else "直飛")
            band_map = {"low": "低價區間", "mid": "中價區間", "high": "高價區間"}
            days["price_band_label"] = days["price_band"].apply(lambda b: band_map.get(str(b).lower(), "標準區間"))

            _metric_row(
                [
                    {"label": "快取最低價", "value": _currency(calendar_data.get("min_price")), "caption": "不可直接購買", "tone": "success"},
                    {"label": "觀察日期", "value": calendar_data.get("observations", 0), "caption": calendar_data.get("month", ""), "tone": "info"},
                    {"label": "即時驗證成本", "value": "1 次", "caption": "SerpAPI", "tone": "warning"},
                ]
            )

            # 都會區多機場過濾與提示
            is_bangkok = str(calendar_data.get("destination", "")).upper() in ("BKK", "DMK")
            if is_bangkok:
                st.html(
                    ui.status_banner(
                        "大曼谷都會區機場說明（BKK 蘇凡納布 vs DMK 廊曼）",
                        "Travelpayouts 將曼谷 (BKK) 視為大都市區域代碼。泰獅航 (SL)、泰國亞航 (FD) 等廉航實際起降於「廊曼機場 (DMK)」；長榮、華航、星宇、泰航、泰越捷則起降於「蘇凡納布機場 (BKK)」。下方可依需求篩選特定機場。",
                        status="info",
                        label="機場辨識",
                    )
                )

            v_col1, v_col2, v_col3 = st.columns([2, 1.2, 1.2], vertical_alignment="center")
            with v_col1:
                st.caption("💡 提示：長條已標註實際起降機場與航空公司，滑鼠懸停即可查看詳細資訊。")
            with v_col2:
                if is_bangkok:
                    airport_filter = st.selectbox("機場範圍", ["包含全都會區 (BKK + DMK)", "僅限蘇凡納布 (BKK)", "僅限廊曼 (DMK)"], key="cal_airport_filter")
                else:
                    airport_filter = "全部"
            with v_col3:
                color_mode = st.radio("著色方式", ["航空公司", "價格等級"], horizontal=True, key="cal_color_mode")

            # 依機場篩選
            chart_days = days.copy()
            if is_bangkok:
                if airport_filter == "僅限蘇凡納布 (BKK)":
                    chart_days = chart_days[chart_days["target_airport_code"] == "BKK"]
                elif airport_filter == "僅限廊曼 (DMK)":
                    chart_days = chart_days[chart_days["target_airport_code"] == "DMK"]

            if chart_days.empty:
                st.warning(f"目前快取中沒有符合「{airport_filter}」的航班資料。可切換為「包含全都會區 (BKK + DMK)」查看全部趨勢。")
                return

            if color_mode == "航空公司":
                chart = px.bar(
                    chart_days,
                    x="date",
                    y="price",
                    color="airline_display",
                    hover_name="airline_display",
                    hover_data={
                        "date": True,
                        "price": ":,.0f",
                        "actual_airport": True,
                        "airline_display": False,
                        "stops_display": True,
                        "price_band_label": True,
                    },
                    labels={
                        "date": "出發日期",
                        "price": "快取參考價 (NT$)",
                        "actual_airport": "實際起降機場",
                        "airline_display": "航空公司",
                        "stops_display": "轉機資訊",
                        "price_band_label": "價格等級",
                    },
                )
            else:
                chart = px.bar(
                    chart_days,
                    x="date",
                    y="price",
                    color="price_band_label",
                    color_discrete_map={"低價區間": "#16a394", "中價區間": "#3b82f6", "高價區間": "#d97706"},
                    hover_name="airline_display",
                    hover_data={
                        "date": True,
                        "price": ":,.0f",
                        "actual_airport": True,
                        "airline_display": True,
                        "stops_display": True,
                        "price_band_label": False,
                    },
                    labels={
                        "date": "出發日期",
                        "price": "快取參考價 (NT$)",
                        "actual_airport": "實際起降機場",
                        "airline_display": "航空公司",
                        "stops_display": "轉機資訊",
                        "price_band_label": "價格等級",
                    },
                )

            chart.update_layout(
                template="plotly_white",
                margin=dict(l=10, r=10, t=20, b=10),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="#ffffff",
                legend_title_text="航空公司" if color_mode == "航空公司" else "價格等級",
                hovermode="closest",
                yaxis=dict(
                    tickprefix="NT$ ",
                    tickformat=",.0f",
                    showgrid=True,
                    gridcolor="#F1F5F9",
                ),
                xaxis=dict(
                    showgrid=True,
                    gridcolor="#F1F5F9",
                ),
            )
            st.plotly_chart(chart, width="stretch")

            choices = {
                f"{row['date']} · {_currency(row['price'])} · {_get_airline_display(row.get('airline'))} · [實際降落 {row.get('actual_airport', '')}] · ({row.get('stops_display', '')})": row
                for _, row in chart_days.iterrows()
            }
            selected_day = choices[st.selectbox("選擇要即時驗證的日期", list(choices), key="calendar_selected_day")]
            verify_adults = int(st.number_input("驗證成人數", min_value=1, max_value=9, value=1, key="calendar_verify_adults"))
            verify_class_label = st.selectbox("驗證艙等", list(CLASS_CODES), key="calendar_verify_class")
            verify_confirm = st.checkbox("我了解即時驗證會使用 1 次 SerpAPI 額度", key="calendar_verify_confirm")
            if st.button("用 SerpAPI 驗證所選日期", type="primary", disabled=not verify_confirm, width="stretch"):
                if calendar_return and calendar_return < date.fromisoformat(str(selected_day["date"])):
                    st.error("固定回程日期早於所選出發日期，請調整後再驗證。")
                else:
                    try:
                        with st.spinner("正在驗證即時可用價格…"):
                            # 精準使用該航司實際起降機場進行 SerpAPI 驗證
                            target_dest = str(selected_day.get("target_airport_code") or calendar_data["destination"])
                            verified = search_and_save(
                                origin=str(calendar_data["origin"]),
                                destination=target_dest,
                                outbound_date=str(selected_day["date"]),
                                return_date=calendar_return.isoformat() if calendar_return else None,
                                adults=verify_adults,
                                travel_class=CLASS_CODES[verify_class_label],
                            )
                        st.session_state["search_result"] = verified
                        st.success(f"即時驗證完成：{format_flight_summary(verified)}。可到「搜尋與比較」建立監控。")
                    except FlightAPIError as exc:
                        st.error(f"即時驗證失敗：{exc}")
                    except Exception as exc:
                        st.error(f"驗證流程發生未預期錯誤：{exc}")
        elif calendar_data:
            if calendar_trip == "來回":
                st.warning("⚠️ 所選月份與固定回程日期目前沒有可用的快取市場觀察值。")
                st.markdown(
                    """
                    **💡 為什麼會這樣？**
                    - **快取命中限制**：Travelpayouts 為全球快取資料庫。當選擇「來回」並指定「固定回程日期」時，快取資料庫必須剛好有其他旅客搜尋過「完全相同回程日」的紀錄，因此特定日期的來回快取通常較少。
                    - **日期已過期**：出發日期若早於今天，歷史快取已過期失效。

                    **👉 建議解決方式：**
                    1. 將行程類型切換為 **「單程」**，即可立即查看該月份全月每天的低價出發趨勢。
                    2. 選擇**未來月份**（例如下個月或下下個月）以取得更充裕的市場快取。
                    3. 直接前往 **「搜尋與比較」** 頁面，使用 SerpAPI 查詢保證最新且可購買的即時來回報價。
                    """
                )
            else:
                st.info("所選月份目前沒有可用的快取市場觀察值。建議選擇未來月份（例如 9 月或 10 月）再試一次，或直接至「搜尋與比較」查詢即時票價。")


def page_runs_and_notifications() -> None:
    runs = db.list_check_runs(limit=200)
    outbox = db.list_notification_outbox(limit=200)
    notifications = db.list_notifications(limit=200)
    _page_header(
        "執行與通知",
        "從每次查價到 Telegram 外送，保留狀態、失敗原因與重試軌跡。",
        eyebrow="RUNS & DELIVERY",
        badges=[
            {"text": f"{len(runs)} 筆執行", "tone": "info"},
            {"text": f"{sum(item.get('status') == 'failed' for item in outbox)} 筆通知失敗", "tone": "danger" if any(item.get('status') == 'failed' for item in outbox) else "success"},
        ],
    )
    run_tab, outbox_tab, log_tab, scheduler_log_tab = st.tabs(["查價執行", "通知佇列", "外送紀錄", "📋 排程日誌"])
    with run_tab:
        if not runs:
            st.html(ui.empty_state("尚無執行紀錄", "查價、排程補跑與 Telegram /check 都會出現在這裡。", icon="○"))
        else:
            statuses = sorted({str(item.get("status") or "unknown") for item in runs})
            selected_statuses = st.multiselect(
                "篩選狀態",
                statuses,
                default=statuses,
                format_func=lambda status: _localized_status(status, RUN_STATUS_LABELS),
                key="run_status_filter",
            )
            shown = [item for item in runs if str(item.get("status") or "unknown") in selected_statuses]
            rows = []
            for item in shown:
                rows.append(
                    {
                        "開始時間": _format_datetime(item.get("started_at")),
                        "航線": f"{item.get('origin') or '—'} → {item.get('destination') or '—'}",
                        "觸發": _localized_status(item.get("trigger"), RUN_TRIGGER_LABELS),
                        "狀態": _localized_status(item.get("status"), RUN_STATUS_LABELS),
                        "API 次數": int(item.get("api_calls") or 0),
                        "耗時 ms": item.get("duration_ms") or "—",
                        "失敗階段": item.get("error_stage") or "—",
                        "補跑": "是" if item.get("backfill") else "否",
                        "ID": item.get("id"),
                    }
                )
            st.dataframe(_arrow_safe_frame(rows), hide_index=True, width="stretch")
            run_map = {
                f"{_format_datetime(item.get('started_at'))} · {item.get('origin') or '—'} → {item.get('destination') or '—'} · "
                f"{_localized_status(item.get('status'), RUN_STATUS_LABELS)}": item
                for item in shown
            }
            if run_map:
                detail = run_map[st.selectbox("查看執行細節", list(run_map), key="run_detail")]
                with st.container(border=True):
                    d1, d2, d3 = st.columns(3)
                    d1.metric("狀態", _localized_status(detail.get("status"), RUN_STATUS_LABELS))
                    d2.metric("API 次數", int(detail.get("api_calls") or 0))
                    d3.metric("耗時", f"{detail.get('duration_ms') or 0} ms")
                    if detail.get("error_message"):
                        st.error(f"{detail.get('error_stage') or 'unknown'}：{detail['error_message']}")
                    result = _json_dict(detail.get("result_json"))
                    if result:
                        st.json(result, expanded=False)
                    st.caption(f"Run ID：{detail.get('id')}")

    with outbox_tab:
        st.caption("失敗項目會依退避策略自動重試；手動放回佇列不會立刻連線。")
        if not outbox:
            st.html(ui.empty_state("通知佇列是空的", "排程器會先可靠寫入佇列，再嘗試外送。", icon="✓"))
        else:
            outbox_statuses = sorted({str(item.get("status") or "unknown") for item in outbox})
            status_filter = st.multiselect(
                "篩選佇列狀態",
                outbox_statuses,
                default=outbox_statuses,
                format_func=lambda status: _localized_status(status, OUTBOX_STATUS_LABELS),
                key="outbox_filter",
            )
            shown_outbox = [item for item in outbox if str(item.get("status") or "unknown") in status_filter]
            out_rows = [
                {
                    "建立時間": _format_datetime(item.get("created_at")),
                    "管道": item.get("channel") or "—",
                    "狀態": _localized_status(item.get("status"), OUTBOX_STATUS_LABELS),
                    "嘗試": f"{int(item.get('attempt_count') or 0)} / {int(item.get('max_attempts') or 0)}",
                    "下次嘗試": _format_datetime(item.get("next_attempt_at")),
                    "錯誤": item.get("last_error") or "—",
                    "監控": item.get("monitor_id") or "—",
                    "ID": item.get("id"),
                }
                for item in shown_outbox
            ]
            st.dataframe(_arrow_safe_frame(out_rows), hide_index=True, width="stretch")
            retryable = [item for item in outbox if item.get("status") in {"failed", "retry"}]
            if retryable:
                retry_map = {
                    f"#{item['id']} · {item.get('channel')} · {str(item.get('last_error') or '等待重試')[:80]}": item
                    for item in retryable
                }
                selected_retry = retry_map[st.selectbox("選擇要重新排隊的通知", list(retry_map), key="retry_outbox_item")]
                if st.button("重設嘗試次數並放回佇列", width="stretch"):
                    try:
                        db.retry_outbox_delivery(int(selected_retry["id"]))
                        st.success("已放回重試佇列；排程器會在下一個處理週期外送。")
                        st.rerun()
                    except (TypeError, ValueError) as exc:
                        st.error(f"無法重試：{exc}")
            st.divider()
            st.warning("「立即處理」可能馬上向 Telegram／Discord 外送佇列中的訊息。")
            process_confirm = st.checkbox("我確認要立即外送目前到期的通知", key="process_outbox_confirm")
            if st.button("立即處理到期通知", disabled=not process_confirm, type="primary", width="stretch"):
                try:
                    with st.spinner("正在處理通知佇列…"):
                        results = process_notification_outbox(limit=50)
                    succeeded = sum(bool(item.get("success")) for item in results)
                    st.success(f"已處理 {len(results)} 筆，其中 {succeeded} 筆成功。")
                    if results:
                        st.dataframe(_arrow_safe_frame(results), hide_index=True, width="stretch")
                except Exception as exc:
                    st.error(f"通知處理失敗：{exc}")

    with log_tab:
        if not notifications:
            st.html(ui.empty_state("尚無外送紀錄", "成功、失敗與本機備援紀錄都會保留。", icon="↗"))
        else:
            log_rows = [
                {
                    "時間": _format_datetime(item.get("created_at")),
                    "管道": item.get("channel") or "—",
                    "結果": "成功" if item.get("success") else "失敗",
                    "監控": item.get("monitor_id") or "—",
                    "訊息摘要": str(item.get("message") or "").replace("\n", " ")[:160],
                }
                for item in notifications
            ]
            st.dataframe(_arrow_safe_frame(log_rows), hide_index=True, width="stretch")

    with scheduler_log_tab:
        log_path = ROOT / "data" / "scheduler.log"
        st.caption("即時讀取背景排程器 (scheduler.py) 的執行日誌紀錄。")
        c_left, c_right = st.columns([1, 4])
        with c_left:
            if st.button("🔄 重新載入日誌", key="reload_scheduler_log", width="stretch"):
                st.rerun()
        if not log_path.exists():
            st.info("尚無排程日誌檔案（scheduler.log）。啟動 start_scheduler.bat 後會自動產生。")
        else:
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                recent_lines = lines[-80:] if len(lines) > 80 else lines
                st.code("\n".join(recent_lines), language="text")
                st.caption(f"顯示最近 {len(recent_lines)} 行 (共 {len(lines)} 行)")
            except Exception as exc:
                st.error(f"無法讀取日誌：{exc}")


def page_settings() -> None:
    health = get_scheduler_health()
    usage = quota_summaries()[0]
    monitors = db.list_monitors(active_only=True)
    telegram_runtime = _telegram_command_runtime()
    _page_header(
        "系統設定",
        "唯讀檢視目前環境、排程與 Telegram 指令安全設定。",
        eyebrow="SYSTEM",
        badges=[{"text": "環境變更需重啟", "tone": "warning"}],
    )
    st.html(
        ui.status_banner(
            "此頁不會直接寫入 .env",
            "金鑰與整合設定由本機環境管理；修改後請重新啟動 Streamlit、scheduler 與 telegram_bot。",
            status="info",
            label="唯讀",
        )
    )
    service_tab, quota_tab, telegram_tab, maintenance_tab = st.tabs(["服務狀態", "額度規劃", "Telegram 指令", "維護工具"])
    with service_tab:
        cards = [
            ("SerpAPI 即時查價", "已設定" if flight_api_ready() else "未設定", flight_api_ready(), "SERPAPI_API_KEY"),
            ("Travelpayouts 月曆", "已設定" if travelpayouts_ready() else "未設定", travelpayouts_ready(), "TRAVELPAYOUTS_TOKEN"),
            ("排程器", "正常" if health["healthy"] else "未回報", health["healthy"], _format_datetime(health["updated_at"])),
            (
                "Telegram 報價回報",
                "可用" if telegram_runtime["report_ready"] else "未設定",
                telegram_runtime["report_ready"],
                "由排程器直接外送",
            ),
            (
                "Telegram 指令",
                "執行中" if telegram_runtime["command_running"] else "未執行",
                telegram_runtime["command_running"],
                "/status · /quota · /check",
            ),
            (
                "排程心跳監督",
                "監控中" if telegram_runtime["watchdog_ready"] else "未就緒",
                telegram_runtime["watchdog_ready"],
                "由 Telegram Bot 獨立監督",
            ),
        ]
        cols = st.columns(2)
        for index, (label, value, ok, description) in enumerate(cards):
            with cols[index % 2]:
                st.html(ui.status_card(label, value, description=description, status="success" if ok else "warning"))
        st.markdown("**排程器執行狀態**")
        st.json(
            {
                "狀態": health["state"],
                "監督器": health["supervisor_state"],
                "最近完成": _format_datetime(health["last_run"]),
                "下次執行": _format_datetime(health["next_run"]),
                "漏跑回溯": f"{MISSED_RUN_LOOKBACK_HOURS} 小時",
                "最近錯誤": health["last_error"] or None,
                "最近檢查數": health.get("last_checked_count", 0),
                "命中目標": health.get("last_hit_count", 0),
                "成功率": health.get("last_success_rate", 0.0),
                "執行耗時(秒)": health.get("last_run_duration", 0.0),
            },
            expanded=False,
        )
        st.code("start_skywatch.bat\nstart_scheduler.bat\nstart_telegram_bot.bat", language="text")
        st.caption("環境變數變更後，請關閉舊程序並重新執行對應啟動檔。")

        st.divider()
        st.markdown("**🔔 即時通知管道測試**")
        st.caption("點擊下方按鈕將向目前已設定的通知管道 (Telegram / Discord) 發送一則安全測試通知。")
        if st.button("發送即時測試通知", key="test_notification_btn", type="secondary"):
            try:
                with st.spinner("正在發送測試通知…"):
                    test_msg = (
                        "🔔 【SkyWatch 系統通知測試】\n"
                        f"測試時間：{_now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        "您的通知管道連線正常！若有符合目標價的機票將會第一時間通知您。"
                    )
                    from src.notifier import send_all
                    test_results = send_all(test_msg, monitor_id=None)
                succeeded = [ch for ch, ok, _ in test_results if ok]
                failed = [f"{ch} ({detail})" for ch, ok, detail in test_results if not ok]
                if succeeded:
                    st.success(f"測試通知成功發送至：{'、'.join(succeeded)}")
                if failed:
                    st.error(f"部分管道發送失敗：{'；'.join(failed)}")
            except Exception as exc:
                st.error(f"發送測試通知時發生錯誤：{exc}")

    with quota_tab:
        if usage["limit"]:
            st.html(ui.quota_meter(usage["attempted"], usage["limit"], label="SerpAPI 本月嘗試次數", reset_at="下個月"))
        else:
            st.info(f"本月已嘗試 {usage['attempted']} 次；SERPAPI_MONTHLY_LIMIT 目前為 0（未知上限）。")
        st.markdown("**三種額度模式試算**")
        baseline_runs = max(0.1, scheduled_runs_per_day())
        monitor_count = max(1, len(monitors))
        rows = []
        for mode in QUOTA_LABELS:
            policy = QuotaPolicy(mode=mode, monthly_limit=SERPAPI_MONTHLY_LIMIT)
            forecast = policy.forecast(baseline_runs, monitor_count, used_requests=int(usage["attempted"]))
            rows.append(
                {
                    "模式": QUOTA_LABELS[mode],
                    "單次最多請求": policy.max_requests_per_run,
                    "每日預估": forecast.daily_requests,
                    "每月預估": forecast.monthly_requests,
                    "額度足夠": "未知" if forecast.fits_monthly_limit is None else "是" if forecast.fits_monthly_limit else "否",
                }
            )
        st.dataframe(_arrow_safe_frame(rows), hide_index=True, width="stretch")
        total_projection = sum(
            _quota_forecast(str(item.get("quota_mode") or "full"), _monitor_runs_per_day(item))["monthly_requests"]
            for item in monitors
        )
        remaining_projection = _remaining_month_projection(monitors)
        if usage["remaining"] is not None and remaining_projection["requests"] > usage["remaining"]:
            st.warning(
                f"從現在至月底的 {remaining_projection['runs']:,} 個排程時段最多約需 "
                f"{remaining_projection['requests']:,} 次，超過本月剩餘 {usage['remaining']:,} 次。"
                "建議降低頻率或改用精簡模式。"
            )
        else:
            remaining_text = (
                "未設定月上限"
                if usage["remaining"] is None
                else f"目前仍有 {usage['remaining']:,} 次可用"
            )
            st.success(
                f"從現在至月底的 {remaining_projection['runs']:,} 個排程時段最多約需 "
                f"{remaining_projection['requests']:,} 次（{remaining_text}）。"
            )
        st.caption(f"完整月份上限試算約 {total_projection:,} 次；此數字僅供下月規劃，不與本月剩餘額度直接比較。")

    with telegram_tab:
        _metric_row(
            [
                {
                    "label": "報價回報",
                    "value": "可用" if telegram_runtime["report_ready"] else "未設定",
                    "caption": "不依賴指令 Bot",
                    "tone": "success" if telegram_runtime["report_ready"] else "warning",
                },
                {
                    "label": "指令服務",
                    "value": "執行中" if telegram_runtime["command_running"] else "未執行",
                    "caption": f"PID {telegram_runtime['pid']}" if telegram_runtime["pid"] else "請啟動 telegram_bot",
                    "tone": "success" if telegram_runtime["command_running"] else "danger",
                },
                {
                    "label": "異常監督",
                    "value": "監控中" if telegram_runtime["watchdog_ready"] else "未就緒",
                    "caption": "排程 heartbeat",
                    "tone": "success" if telegram_runtime["watchdog_ready"] else "warning",
                },
                {"label": "允許的 Chat ID", "value": len(TELEGRAM_ALLOWED_CHAT_IDS), "caption": "僅顯示數量", "tone": "primary"},
            ]
        )
        st.markdown(
            """
            - `/status`：查看排程器健康、最近完成與下一次執行。
            - `/quota`：查看 SerpAPI 本月用量與剩餘額度。
            - `/check`：先取得短效一次性確認碼，再確認執行手動查價。
            """
        )
        if not TELEGRAM_COMMANDS_ENABLED:
            st.warning("TELEGRAM_COMMANDS_ENABLED 已關閉。")
        if not TELEGRAM_ALLOWED_CHAT_IDS:
            st.warning("尚未設定 TELEGRAM_ALLOWED_CHAT_IDS 或 TELEGRAM_CHAT_ID；所有指令都會被拒絕。")
        if telegram_runtime["command_configured"] and not telegram_runtime["command_running"]:
            st.error("Telegram 報價回報仍可使用，但指令與排程異常 watchdog 尚未執行。請啟動 start_telegram_bot.bat。")
        st.caption("安全設計：Chat ID 採允許清單；/check 確認碼短效、一次性且可防止重播。")

    with maintenance_tab:
        active = db.list_monitors(active_only=True)
        estimated_calls = sum(QuotaPolicy(mode=str(item.get("quota_mode") or "full")).max_requests_per_run for item in active)
        st.warning(
            f"立即檢查全部可能使用最多 {estimated_calls} 次 SerpAPI 額度，"
            "並可能依每筆策略外送 Telegram／Discord 通知。"
        )
        manual_confirmation = st.text_input("輸入 CHECK ALL 確認", key="check_all_confirmation")
        if st.button(
            "立即檢查全部啟用監控",
            type="primary",
            width="stretch",
            disabled=manual_confirmation != "CHECK ALL" or not active,
        ):
            try:
                with st.spinner("正在逐筆查價…"):
                    results = check_all_monitors(scheduled_report=False)
                ok = sum(bool(item.get("ok")) for item in results)
                st.success(f"已完成 {len(results)} 筆，其中 {ok} 筆成功。")
                if results:
                    summary = [
                        {
                            "監控": item.get("monitor_id") or "—",
                            "結果": "成功" if item.get("ok") else "失敗",
                            "最低價": _currency(item.get("best_price")),
                            "達標": "是" if item.get("hit") else "否",
                            "通知": item.get("notify_reason") or "—",
                            "API 次數": int(item.get("api_calls") or 0),
                            "錯誤": item.get("error") or "—",
                        }
                        for item in results
                    ]
                    st.dataframe(_arrow_safe_frame(summary), hide_index=True, width="stretch")
            except Exception as exc:
                st.error(f"批次檢查失敗：{exc}")
        st.divider()
        cleanup_days = int(st.number_input("保留最近幾天的價格歷史", min_value=30, max_value=3650, value=365, step=30))
        cleanup_confirm = st.checkbox("我確認刪除超過保留期限的價格歷史", key="cleanup_confirm")
        if st.button("清理舊價格紀錄", disabled=not cleanup_confirm, width="stretch"):
            try:
                removed = db.cleanup_old_records(cleanup_days)
                st.success(f"已移除 {removed} 筆舊價格紀錄。")
            except Exception as exc:
                st.error(f"清理失敗：{exc}")


def page_backup_restore() -> None:
    _page_header(
        "備份與復原",
        "匯出監控規則或完整應用資料，並以驗證、預覽與復原點降低操作風險。",
        eyebrow="DATA SAFETY",
        badges=[{"text": "不含任何憑證", "tone": "success"}, {"text": "復原前自動快照", "tone": "info"}],
    )
    st.html(
        ui.status_banner(
            "備份不包含任何憑證或整合設定",
            "完整備份包含應用資料庫與監控規則；不包含 .env、API Key、Telegram Token 或其他整合設定。",
            status="success",
            label="安全範圍",
        )
    )
    export_tab, import_tab, restore_tab = st.tabs(["匯出備份", "匯入監控", "完整復原"])
    with export_tab:
        left, right = st.columns(2)
        with left:
            st.markdown("**監控規則 JSON**")
            st.caption("適合跨環境搬移監控；不包含票價歷史與執行紀錄。")
            if st.button("產生監控規則匯出", width="stretch"):
                path = Path(tempfile.gettempdir()) / f"skywatch-monitors-{_now().strftime('%Y%m%d-%H%M%S')}.json"
                try:
                    report = export_monitors(path, DB_PATH)
                    st.session_state["monitor_export"] = {
                        "bytes": path.read_bytes(),
                        "name": path.name,
                        "preview": report["preview"],
                    }
                except BackupError as exc:
                    st.error(f"匯出失敗：{exc}")
                finally:
                    path.unlink(missing_ok=True)
            monitor_export = st.session_state.get("monitor_export")
            if monitor_export:
                st.json(monitor_export["preview"], expanded=False)
                st.download_button(
                    "下載監控規則 JSON",
                    data=monitor_export["bytes"],
                    file_name=monitor_export["name"],
                    mime="application/json",
                    width="stretch",
                )
        with right:
            st.markdown("**完整應用資料 ZIP**")
            st.caption("包含資料庫、票價歷史、監控、執行與通知紀錄；不含任何憑證或整合設定。")
            if st.button("產生完整備份", width="stretch"):
                path = Path(tempfile.gettempdir()) / f"skywatch-full-{_now().strftime('%Y%m%d-%H%M%S')}.zip"
                try:
                    report = create_full_backup(path, DB_PATH)
                    st.session_state["full_export"] = {
                        "bytes": path.read_bytes(),
                        "name": path.name,
                        "preview": report["preview"],
                        "sha256": report["sha256"],
                    }
                except BackupError as exc:
                    st.error(f"完整備份失敗：{exc}")
                finally:
                    path.unlink(missing_ok=True)
            full_export = st.session_state.get("full_export")
            if full_export:
                st.json(full_export["preview"], expanded=False)
                st.caption(f"SHA-256：{full_export['sha256']}")
                st.download_button(
                    "下載完整備份 ZIP",
                    data=full_export["bytes"],
                    file_name=full_export["name"],
                    mime="application/zip",
                    width="stretch",
                )

    with import_tab:
        st.caption("匯入前會驗證格式、預覽衝突，正式寫入前也會建立目前資料庫的復原點。")
        monitor_upload = st.file_uploader("選擇監控規則 JSON", type=["json"], key="monitor_import_upload")
        if monitor_upload:
            path = _upload_to_temp(monitor_upload, ".json")
            try:
                preview = preview_monitor_import(path, DB_PATH)
                st.success("檔案驗證通過。")
                _metric_row(
                    [
                        {"label": "監控總數", "value": preview["monitor_count"], "tone": "primary"},
                        {"label": "新增", "value": preview["new_count"], "tone": "success"},
                        {"label": "衝突", "value": preview["conflict_count"], "tone": "warning"},
                    ]
                )
                conflict = st.selectbox(
                    "衝突處理",
                    ["skip", "update", "duplicate"],
                    format_func=lambda item: {"skip": "略過既有", "update": "更新既有", "duplicate": "保留重複副本"}[item],
                )
                confirm = st.text_input("輸入 IMPORT MONITORS 確認匯入", key="import_confirmation")
                if st.button("匯入監控規則", type="primary", disabled=confirm != "IMPORT MONITORS", width="stretch"):
                    report = import_monitors(path, DB_PATH, conflict=conflict)
                    st.success(
                        f"匯入完成：新增 {report['created']}、更新 {report['updated']}、"
                        f"略過 {report['skipped']}；已建立復原點。"
                    )
                    st.caption(f"復原點：{report['restore_point']}")
            except (BackupError, BackupValidationError) as exc:
                st.error(f"無法匯入：{exc}")
            except Exception as exc:
                st.error(f"匯入流程發生未預期錯誤：{exc}")
            finally:
                path.unlink(missing_ok=True)

    with restore_tab:
        st.error("完整復原會以備份資料庫取代目前資料。操作前請停止 scheduler 與 telegram_bot，完成後再重新啟動所有服務。")
        backup_upload = st.file_uploader("選擇完整備份 ZIP", type=["zip"], key="full_restore_upload")
        if backup_upload:
            path = _upload_to_temp(backup_upload, ".zip")
            try:
                inspection = inspect_backup(path)
                st.success("備份結構、允許清單與 SHA-256 驗證通過。")
                st.json(
                    {
                        "建立時間": inspection.get("created_at"),
                        "內容預覽": inspection.get("preview"),
                        "檔案": inspection.get("members"),
                    },
                    expanded=False,
                )
                restore_confirm = st.text_input("輸入 RESTORE SKYWATCH 確認完整復原", key="restore_confirmation")
                acknowledge = st.checkbox("我已停止排程器與 Telegram 指令服務", key="restore_stopped_services")
                if st.button(
                    "取代目前資料並建立復原點",
                    type="primary",
                    disabled=restore_confirm != "RESTORE SKYWATCH" or not acknowledge,
                    width="stretch",
                ):
                    report = restore_full_backup(path, DB_PATH)
                    db.init_db()
                    st.success("完整復原成功。請重新啟動 Streamlit、scheduler 與 telegram_bot。")
                    st.caption(f"復原前快照：{report['restore_point']}")
            except (BackupError, BackupValidationError) as exc:
                st.error(f"備份無法使用：{exc}")
            except Exception as exc:
                st.error(f"復原流程發生未預期錯誤：{exc}")
            finally:
                path.unlink(missing_ok=True)


def page_help() -> None:
    _page_header(
        "使用說明",
        "從第一次查價、建立策略到排程補跑與 Telegram 控制的完整指南。",
        eyebrow="HELP",
        badges=[{"text": "繁體中文", "tone": "primary"}, {"text": "操作導向", "tone": "info"}],
    )
    st.html(ui.stepper(["即時查價", "建立策略", "自動執行", "通知與追蹤"], current=0, label="建議操作流程"))
    sections = [
        (
            "1. 如何開始監控？",
            "到「搜尋與比較」選擇航線、日期、人數與艙等。確認 1 次 SerpAPI 額度成本後查價，再設定一般目標價、額度模式、航空條件、排程和通知策略。",
        ),
        (
            "2. 精簡、標準、完整有什麼差別？",
            "三種模式分別限制單次監控最多 1、2、5 個供應商請求。精簡適合大量監控；完整可取得更多回程或開票細節。實際用量仍以執行紀錄為準。",
        ),
        (
            "3. 為什麼月曆價格不能直接通知？",
            "Travelpayouts Data API 是快取市場趨勢，不保證目前可購買。必須選定日期並使用 SerpAPI 即時驗證後，才能成為歷史資料或監控依據。",
        ),
        (
            "4. 漏跑如何處理？",
            f"排程器持續寫入 heartbeat，並檢查最近 {MISSED_RUN_LOOKBACK_HOURS} 小時的排程槽。偵測漏跑時最多補最近一個槽，且利用執行帳本避免同一槽重複執行。",
        ),
        (
            "5. 為什麼 Telegram 不會重複送相同訊息？",
            "通知會先寫入可靠佇列，再以唯一鍵防止相同管道、監控與執行內容重複入列；失敗則保留錯誤與退避重試時間。",
        ),
        (
            "6. Telegram 可以輸入哪些指令？",
            "/status 查看排程健康、/quota 查看額度、/check 以短效一次性確認碼啟動查價。只有允許清單內的 Chat ID 可以使用。",
        ),
        (
            "7. 備份會包含金鑰嗎？",
            "不會。完整備份包含應用資料庫與監控規則，但不包含 .env、API Key、Telegram Token 或任何整合設定。",
        ),
    ]
    for title, body in sections:
        with st.expander(title, expanded=title.startswith("1.")):
            st.write(body)
    st.info("所有時間均以 Asia/Taipei 顯示。若修改環境變數，請重新啟動對應服務。")


def sidebar() -> str:
    with st.sidebar:
        st.markdown("# ✈️ SkyWatch")
        st.caption(f"航班價格監控 · v{APP_VERSION}")
        pages = [
            "營運總覽",
            "搜尋與比較",
            "監控策略",
            "價格情報",
            "執行與通知",
            "系統設定",
            "備份與復原",
            "使用說明",
        ]
        page = st.radio(
            "主要功能",
            pages,
            key="main_page",
            label_visibility="collapsed",
        )
        st.divider()
        health = get_scheduler_health()
        st.caption("排程器")
        if health["healthy"]:
            st.success("運作正常")
        else:
            st.warning("需要檢查")
        st.caption(f"下次：{_format_datetime(health['next_run'])}")
        # 顯示簡易觀測數據
        if health.get("last_checked_count"):
            st.caption(f"最近檢查 {health.get('last_checked_count')} 筆，成功率 {health.get('last_success_rate', 0)*100:.0f}%")
        usage = db.get_api_usage_summary("serpapi", SERPAPI_MONTHLY_LIMIT)
        if usage["limit"]:
            st.progress(min(1.0, float(usage["attempted"]) / float(usage["limit"])), text=f"額度 {usage['attempted']} / {usage['limit']}")
        else:
            st.caption(f"本月 API 嘗試 {usage['attempted']} 次")
        st.caption("通知：" + "、".join(configured_channels()))
        return page


def main() -> None:
    page = sidebar()
    pages = {
        "營運總覽": page_dashboard,
        "搜尋與比較": page_search,
        "監控策略": page_monitors,
        "價格情報": page_price_intelligence,
        "執行與通知": page_runs_and_notifications,
        "系統設定": page_settings,
        "備份與復原": page_backup_restore,
        "使用說明": page_help,
    }
    pages[page]()


if __name__ == "__main__":
    main()
