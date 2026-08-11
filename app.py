"""
即時機票監控系統 - 商務企業級儀表板
啟動：streamlit run app.py
"""
from __future__ import annotations

import html
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import database as db
from src.api_usage import estimate_monthly_primary_searches, quota_summaries
from src.airports import get_label, options_for_select
from src.config import (
    API_USAGE_WARNING_PERCENT,
    ALWAYS_NOTIFY,
    APP_VERSION,
    MONITOR_FETCH_DETAILS_ON_HIT,
    NOTIFY_COOLDOWN_HOURS,
    SERPAPI_API_KEY,
)
from src.flight_api import FlightAPIError, format_flight_summary
from src.monitor import check_all_monitors, check_one_monitor, search_and_save
from src.notifier import configured_channels, send_all
from src.travelpayouts import (
    TravelpayoutsError,
    configured as travelpayouts_ready,
    get_market_reference,
)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

st.set_page_config(
    page_title="SkyWatch · 企業級機票監控",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="auto",
)

db.init_db()

try:
    with open("style.css", "r", encoding="utf-8") as f:
        st.html(f"<style>\n{f.read()}\n</style>")
except FileNotFoundError:
    pass


def _safe(value: object) -> str:
    """Escape dynamic values before rendering custom HTML."""
    return html.escape(str(value or ""), quote=True)


def flight_api_ready() -> bool:
    return bool(SERPAPI_API_KEY)


def get_scheduler_health() -> dict:
    """Read the independent scheduler heartbeat without starting another timer."""
    log_path = ROOT / "data" / "scheduler.log"
    status_path = ROOT / "data" / "scheduler_status.json"
    now = datetime.now(TAIPEI_TZ)
    next_candidates = [
        now.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
        for day_offset in (0, 1)
        for hour in (0, 12)
        if now.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=day_offset) > now
    ]
    result = {
        "healthy": False,
        "label": "尚無執行紀錄",
        "last_run": None,
        "schedule": "每日 00:00 / 12:00",
        "next_run": min(next_candidates) if next_candidates else None,
        "state": "unknown",
        "last_error": None,
    }

    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        completed_raw = status.get("last_completed_at")
        if completed_raw:
            last_run = datetime.fromisoformat(completed_raw)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=TAIPEI_TZ)
            age_hours = (now - last_run.astimezone(TAIPEI_TZ)).total_seconds() / 3600
            result.update(
                healthy=age_hours <= 14 and status.get("state") != "error",
                label=f"最近完成 {last_run.strftime('%m/%d %H:%M')}",
                last_run=last_run,
                schedule=status.get("schedule") or result["schedule"],
                state=status.get("state") or "unknown",
                last_error=status.get("last_error"),
            )
            return result
    except (FileNotFoundError, OSError, UnicodeError, ValueError, TypeError):
        pass

    if not log_path.exists():
        return result
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
        completed = next((line for line in reversed(lines) if "查價完成" in line), "")
        if completed.startswith("[") and "]" in completed:
            stamp = completed[1:completed.index("]")]
            last_run = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            last_run = last_run.replace(tzinfo=TAIPEI_TZ)
            age_hours = (now - last_run).total_seconds() / 3600
            result.update(
                healthy=age_hours <= 14,
                label=f"最近完成 {last_run.strftime('%m/%d %H:%M')}",
                last_run=last_run,
                state="healthy" if age_hours <= 14 else "stale",
            )
    except (OSError, UnicodeError, ValueError):
        result["label"] = "排程紀錄無法讀取"
    return result


def get_notification_health() -> dict:
    latest = next(iter(db.list_notifications(1)), None)
    result = {"latest": latest, "sent_today": False, "label": "尚無通知紀錄"}
    if not latest:
        return result
    try:
        sent_at = datetime.fromisoformat(latest["created_at"])
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=TAIPEI_TZ)
        result.update(
            sent_today=bool(latest.get("success")) and sent_at.date() == datetime.now(TAIPEI_TZ).date(),
            label=f"{sent_at.strftime('%m/%d %H:%M')} · {latest.get('channel', 'local')}",
            sent_at=sent_at,
        )
    except (KeyError, TypeError, ValueError):
        result["label"] = "通知紀錄格式異常"
    return result

def inject_hero(
    title: str,
    subtitle: str,
    badges: list[str] | None = None,
    *,
    kicker: str = "SKYWATCH ENTERPRISE",
):
    badges_html = ""
    if badges:
        badges_html = (
            '<div class="badge-row">'
            + "".join(f'<span class="badge">{b}</span>' for b in badges)
            + "</div>"
        )
    st.html(
        (
            f'<div class="hero">'
            f'<div class="hero-kicker">{kicker}</div>'
            f'<div class="hero-title">{title}</div>'
            f'<p class="hero-sub">{subtitle}</p>'
            f"{badges_html}"
            f"</div>"
        ))


def inject_cmd_bar(
    *,
    api_ok: bool,
    channels: list[str],
    cooldown_h: float,
    extra: str = "",
):
    api_txt = "ONLINE" if api_ok else "OFFLINE"
    ch_txt = " / ".join(channels) if channels else "未設定"
    extra_html = f'<span class="pill">{extra}</span>' if extra else ""
    st.html(
        (
            f'<div class="cmd-bar">'
            f'<div class="left">'
            f'<span class="pill">API <strong>{api_txt}</strong></span>'
            f'<span class="pill">通知 <strong>{ch_txt}</strong></span>'
            f'<span class="pill">排程 <strong>必送</strong></span>'
            f"{extra_html}"
            f"</div>"
            f'<div class="right">多來源票價 API · 免費額度優先排程</div>'
            f"</div>"
        ))


def render_kpi_row(items: list[dict]):
    """items: label/value/sub/trend/trend_cls/accent

    使用 st.columns 逐卡渲染，避免 Streamlit markdown 因多行/空行把 HTML 當純文字。
    """
    if not items:
        return
    cols = st.columns(len(items))
    for col, it in zip(cols, items):
        accent = it.get("accent") or "accent-navy"
        trend = it.get("trend")
        trend_cls = it.get("trend_cls") or ""
        trend_html = (
            f'<div class="kpi-trend {trend_cls}">{trend}</div>' if trend else ""
        )
        # 單行 HTML，避免空白行導致 markdown 中斷
        html = (
            f'<div class="kpi-card {accent}">'
            f'<div class="kpi-label">{it.get("label", "")}</div>'
            f'<div class="kpi-value">{it.get("value", "—")}</div>'
            f'<div class="kpi-sub">{it.get("sub", "")}</div>'
            f"{trend_html}"
            f"</div>"
        )
        with col:
            st.html(html)


def render_flight_card(f: dict, rank: int = 0):
    stops = int(f.get("stops") or 0)
    stop_text = "直飛" if stops == 0 else f"{stops} 次轉機"
    stop_chip = "green" if stops == 0 else "amber"
    best_cls = " best" if rank == 0 else ""
    price = f.get("price")
    price_str = f"NT$ {price:,.0f}" if price is not None else "—"
    dep = (f.get("departure_time") or "").replace("T", " ")
    arr = (f.get("arrival_time") or "").replace("T", " ")
    # 只顯示時間部分若有日期
    dep_show = dep[-5:] if len(dep) >= 16 else dep
    arr_show = arr[-5:] if len(arr) >= 16 else arr
    dep_full = dep if len(dep) > 5 else ""
    arr_full = arr if len(arr) > 5 else ""

    price_sub = "最低價" if rank == 0 else f"選項 #{rank+1}"
    dep_city = f"{f.get('departure') or ''} {dep_full[:10] if dep_full else ''}".strip()
    arr_city = f"{f.get('arrival') or ''} {arr_full[:10] if arr_full else ''}".strip()
    airline_html = _safe(f.get("airline") or "未知航空")
    flight_number_html = _safe(f.get("flight_number") or "—")
    duration_html = _safe(f.get("duration") or "—")
    dep_city_html = _safe(dep_city)
    arr_city_html = _safe(arr_city)

    # 碳排放 chip
    carbon = f.get("carbon_emissions")
    carbon_html = ""
    if carbon is not None:
        carbon_kg = carbon / 1000 if carbon > 500 else carbon
        carbon_html = f'<span class="chip carbon">🌿 {carbon_kg:.0f} kg CO₂</span>'

    st.html(
        (
            f'<div class="flight-card{best_cls}">'
            f'<div class="fc-top">'
            f"<div>"
            f'<div class="fc-airline">{airline_html}</div>'
            f'<div class="fc-meta">'
            f'<span class="chip">{flight_number_html}</span>'
            f'<span class="chip {stop_chip}">{stop_text}</span>'
            f'<span class="chip">{duration_html}</span>'
            f'{carbon_html}'
            f"</div></div>"
            f"<div>"
            f'<div class="fc-price">{price_str}</div>'
            f'<div class="fc-price-sub">{price_sub}</div>'
            f"</div></div>"
            f'<div class="fc-timeline">'
            f'<div style="text-align:left;min-width:70px">'
            f'<div class="fc-time">{dep_show or "—"}</div>'
            f'<div class="fc-city">{dep_city_html}</div>'
            f'</div>'
            f'<div class="fc-line"><span class="fc-stops">{stop_text}</span></div>'
            f'<div style="text-align:right;min-width:70px">'
            f'<div class="fc-time">{arr_show or "—"}</div>'
            f'<div class="fc-city">{arr_city_html}</div>'
            f'</div>'
            f'</div></div>'
        ))


def pick_airport(label: str, key: str, default_iata: str = "TPE") -> str:
    """
    選擇機場的通用組件。回傳 IATA 代碼。

    功能：
    1. 文字搜尋框，支援城市/機場名/IATA 碼
    2. 若輸入 3 碼 IATA，提供快捷確認
    3. 下拉選單動態篩選
    """
    # 載入初始選項
    opts: dict[str, str] = {}
    try:
        opts = dict(options_for_select("", limit=80))
    except Exception:
        pass

    # 確保預設的 IATA 存在於選項中
    default_label = (get_label(default_iata) or default_iata)
    if default_label not in opts:
        opts[default_label] = default_iata

    labels = list(opts.keys())

    # 搜尋框
    q = st.text_input(
        f"搜尋{label}",
        value="",
        key=f"{key}_q",
        placeholder="城市、機場名或 IATA（如 東京 / NRT）",
    )

    # 3 碼 IATA 快捷
    manual = (q or "").strip().upper()
    if len(manual) == 3 and manual.isalpha():
        st.caption(f"IATA `{manual}`")
        if st.checkbox(f"直接使用 {manual}", value=True, key=f"{key}_manual"):
            return manual

    # 根據搜尋文字動態更新選項
    if q:
        try:
            fresh = options_for_select(q, limit=100)
            if fresh:
                opts = dict(fresh)
                labels = list(opts.keys())
        except Exception:
            pass

    # 確保選項不為空
    if not labels:
        labels = [default_label]
        opts = {default_label: default_iata}

    default_index = labels.index(default_label) if default_label in labels else 0
    chosen = st.selectbox(label, options=labels, index=default_index, key=f"{key}_sel")
    return opts.get(chosen, default_iata)


def sidebar():
    with st.sidebar:
        st.html(
            (
                '<div class="sb-brand">'
                '<div class="sb-logo">✈ Sky<span>Watch</span></div>'
                '<div class="sb-tag">航班價格監控中心</div>'
                "</div>"
            ))
        page = st.radio(
            "導覽",
            ["儀表板", "搜尋航班", "價格歷史", "監控管理", "通知與系統設定", "使用說明"],
        )
        st.markdown("---")
        channels = configured_channels()
        api_ok = flight_api_ready()
        api_label = "SerpAPI 已連線"
        api_row = (
            f'<div class="sb-status-row"><span class="status-dot dot-on"></span>{api_label}</div>'
            if api_ok
            else '<div class="sb-status-row"><span class="status-dot dot-off"></span>缺少票價 API</div>'
        )
        ch_rows = "".join(
            f'<div class="sb-status-row"><span class="status-dot dot-on"></span>{ch}</div>'
            for ch in channels
        ) or '<div class="sb-status-row"><span class="status-dot dot-off"></span>尚未設定通知</div>'
        st.html(
            (
                f'<div class="sb-status-card">'
                f'<div class="sb-status-title">系統狀態</div>'
                f"{api_row}"
                f"{ch_rows}"
                f"</div>"
            ))
        st.caption("固定回報 · 每日 00:00 / 12:00")
        st.caption(f"全球機場 · 約 8,800 個 IATA")
        st.markdown("---")
        st.caption(f"SkyWatch v{APP_VERSION} · {datetime.now().strftime('%Y-%m-%d')}")

        scheduler_health = get_scheduler_health()
        dot = "dot-on" if scheduler_health["healthy"] else "dot-warn"
        st.html(
            f'<div class="sb-status-card">'
            f'<div class="sb-status-title">背景排程</div>'
            f'<div class="sb-status-row"><span class="status-dot {dot}"></span>'
            f'{_safe(scheduler_health["label"])}</div>'
            f'<div class="sb-status-meta">{_safe(scheduler_health["schedule"])}</div>'
            f'</div>')

    return page


def _duration_minutes(duration: str | None) -> int:
    """將 '5h 30m' / '5h' / '90m' 轉成分鐘，供排序用。"""
    if not duration:
        return 10**9
    text = str(duration).lower().replace(" ", "")
    hours = mins = 0
    if "h" in text:
        try:
            hours = int(text.split("h")[0] or 0)
            rest = text.split("h", 1)[1]
            if rest.endswith("m"):
                mins = int(rest[:-1] or 0)
        except ValueError:
            return 10**9
    elif text.endswith("m"):
        try:
            mins = int(text[:-1] or 0)
        except ValueError:
            return 10**9
    else:
        return 10**9
    return hours * 60 + mins


def _filter_sort_flights(
    flights: list[dict],
    *,
    direct_only: bool,
    max_stops: int,
    sort_by: str,
) -> list[dict]:
    items = list(flights or [])
    if direct_only:
        items = [f for f in items if int(f.get("stops") or 0) == 0]
    else:
        items = [f for f in items if int(f.get("stops") or 0) <= max_stops]

    if sort_by == "價格由低到高":
        items.sort(key=lambda f: (f.get("price") is None, f.get("price") or 10**12))
    elif sort_by == "時長由短到長":
        items.sort(key=lambda f: (_duration_minutes(f.get("duration")), f.get("price") or 10**12))
    elif sort_by == "出發時間":
        items.sort(key=lambda f: (f.get("departure_time") or "9999", f.get("price") or 10**12))
    elif sort_by == "直飛優先":
        items.sort(
            key=lambda f: (
                int(f.get("stops") or 0),
                f.get("price") if f.get("price") is not None else 10**12,
            )
        )
    return items


def render_price_insight(stats: dict, current_price: float | None) -> None:
    """價格洞察：相對歷史最低 / 平均。"""
    if not stats or not stats.get("count"):
        st.caption("尚無足夠歷史資料做比價洞察，再查幾次後會更準。")
        return

    min_p = stats.get("min_price")
    avg_p = stats.get("avg_price")
    max_p = stats.get("max_price")
    scope = "同日期" if stats.get("scope") == "exact" else "同航線"
    chips = []
    if current_price is not None and min_p is not None:
        if current_price <= min_p * 1.01:
            chips.append('<span class="chip green">歷史低點附近</span>')
        elif avg_p and current_price <= avg_p * 0.95:
            chips.append('<span class="chip green">低於歷史均價</span>')
        elif avg_p and current_price >= avg_p * 1.08:
            chips.append('<span class="chip amber">高於歷史均價</span>')
        else:
            chips.append('<span class="chip">接近歷史均價</span>')

    vs_min = ""
    vs_avg = ""
    if current_price is not None and min_p:
        diff = current_price - min_p
        pct = (diff / min_p) * 100 if min_p else 0
        vs_min = f"+{diff:,.0f}（+{pct:.0f}%）" if diff > 0 else "持平或更低"
    if current_price is not None and avg_p:
        diff = current_price - avg_p
        pct = (diff / avg_p) * 100 if avg_p else 0
        vs_avg = f"{diff:+,.0f}（{pct:+.0f}%）"

    chips_html = "".join(chips)
    st.html(
        (
            f'<div class="stat-card" style="margin-bottom:0.85rem;border-left:3px solid #3b82f6">'
            f'<div class="k">價格洞察 · {scope} · {stats.get("count", 0)} 筆</div>'
            f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:0.45rem;margin-top:0.55rem">'
            f"<div>"
            f'<div style="font-size:0.66rem;color:#64748b;font-weight:700;letter-spacing:0.06em;text-transform:uppercase">歷史最低</div>'
            f'<div style="font-weight:700;color:#0f766e;font-family:IBM Plex Mono,monospace">NT$ {min_p:,.0f}</div>'
            f"</div><div>"
            f'<div style="font-size:0.66rem;color:#64748b;font-weight:700;letter-spacing:0.06em;text-transform:uppercase">歷史均價</div>'
            f'<div style="font-weight:700;color:#1e293b;font-family:IBM Plex Mono,monospace">NT$ {avg_p:,.0f}</div>'
            f"</div><div>"
            f'<div style="font-size:0.66rem;color:#64748b;font-weight:700;letter-spacing:0.06em;text-transform:uppercase">歷史最高</div>'
            f'<div style="font-weight:700;color:#92400e;font-family:IBM Plex Mono,monospace">NT$ {max_p:,.0f}</div>'
            f"</div></div>"
            f'<div style="margin-top:0.55rem;font-size:0.82rem;color:#64748b;border-top:1px solid #e8edf3;padding-top:0.45rem">'
            f'相對最低：<b style="color:#1e293b">{vs_min or "—"}</b>'
            f'&nbsp;｜&nbsp;相對均價：<b style="color:#1e293b">{vs_avg or "—"}</b>'
            f"</div>"
            f'<div style="margin-top:0.5rem">{chips_html}</div>'
            f"</div>"
        ))


def page_dashboard():
    inject_hero(
        "儀表板",
        "掌握監控任務、價格變化與通知狀態",
        ["即時狀態", "自動查價", "TWD"],
        kicker="OVERVIEW",
    )
    channels = configured_channels()
    notification_health = get_notification_health()
    inject_cmd_bar(
        api_ok=flight_api_ready(),
        channels=channels,
        cooldown_h=NOTIFY_COOLDOWN_HOURS,
        extra=f"最近推播 {notification_health['label']}",
    )
    stats = db.get_dashboard_stats()
    avg = stats.get("avg_recent_price")
    hit_ratio = 0
    if stats.get("active_monitors"):
        hit_ratio = round((stats.get("hit_count") or 0) / max(stats["active_monitors"], 1) * 100)

    render_kpi_row(
        [
            {
                "label": "啟用監控",
                "value": f"{stats['active_monitors']}",
                "sub": f"總檔案 {stats['total_monitors']} 筆",
                "trend": "ACTIVE WATCH",
                "trend_cls": "",
                "accent": "accent-navy",
            },
            {
                "label": "已達標",
                "value": f"{stats['hit_count']}",
                "sub": f"命中率 {hit_ratio}%",
                "trend": "可考慮下單" if stats["hit_count"] else "持續追蹤",
                "trend_cls": "" if stats["hit_count"] else "neutral",
                "accent": "accent-gold",
            },
            {
                "label": "追蹤航線",
                "value": f"{stats['routes']}",
                "sub": "已建檔 Origin → Destination",
                "trend": "NETWORK",
                "trend_cls": "neutral",
                "accent": "accent-navy",
            },
            {
                "label": "24h 通知",
                "value": f"{stats['notifications_24h']}",
                "sub": f"近 20 筆均價 {('NT$ ' + format(avg, ',.0f')) if avg else '—'}",
                "trend": "ALERT STREAM",
                "trend_cls": "warn" if stats["notifications_24h"] else "neutral",
                "accent": "accent-teal",
            },
        ]
    )

    c1, c2 = st.columns([1.05, 1])
    with c1:
        st.html('<div class="section-label">目前監控</div>')
        tops = stats.get("top_monitors") or []
        if not tops:
            st.html(
                '<div class="empty-state"><div class="icon">📡</div><div>尚無啟用監控，先到「搜尋航班」建立第一筆任務</div></div>')
        else:
            for m in tops:
                last_p = m.get("last_price")
                target = m.get("target_price") or 0
                hit = last_p is not None and last_p <= target
                last_txt = f"NT$ {last_p:,.0f}" if last_p is not None else "尚未檢查"
                trip = m["outbound_date"]
                if m.get("return_date"):
                    trip += f" ⇄ {m['return_date']}"
                gap = ""
                if last_p is not None and target:
                    diff = last_p - target
                    gap = f"距目標 {diff:+,.0f}"
                card_cls = "mon-hit" if hit else "mon-active"
                gap_chip = f'<span class="chip gold">{gap}</span>' if gap else ""
                hit_chip = "green" if hit else "amber"
                hit_txt = "已達標" if hit else "追蹤中"
                st.html(
                    (
                        f'<div class="mon-card {card_cls}">'
                        f'<div style="display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap">'
                        f"<div>"
                        f'<div style="font-weight:700;color:#1e293b;font-size:1.02rem">{m["origin"]} → {m["destination"]}</div>'
                        f'<div style="color:#64748b;font-size:0.8rem;margin-top:0.22rem">{trip}</div>'
                        f'<div style="margin-top:0.5rem">'
                        f'<span class="chip">目標 ≤ NT$ {target:,.0f}</span>'
                        f'<span class="chip {hit_chip}">{hit_txt}</span>'
                        f"{gap_chip}"
                        f"</div></div>"
                        f'<div style="text-align:right">'
                        f'<div style="color:#64748b;font-size:0.7rem;letter-spacing:0.06em;text-transform:uppercase;font-weight:650">Last Price</div>'
                        f'<div style="font-weight:700;color:#087443;font-size:1.25rem;font-family:IBM Plex Mono,monospace">{last_txt}</div>'
                        f"</div></div></div>"
                    ))
    with c2:
        st.html('<div class="section-label">最近查價紀錄</div>')
        recent = stats.get("recent_history") or []
        if not recent:
            st.html(
                '<div class="empty-state"><div class="icon">📈</div><div>尚無價格歷史，完成一次查價後會顯示於此</div></div>')
        else:
            rows = []
            for r in recent:
                rows.append(
                    {
                        "時間": r.get("recorded_at"),
                        "航線": f"{r.get('origin')} → {r.get('destination')}",
                        "出發日": r.get("outbound_date"),
                        "價格": r.get("price"),
                        "航空": r.get("airline") or "—",
                    }
                )
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=360)

    st.html('<div class="section-label">快速操作</div>')
    b1, b2, b3 = st.columns([1.2, 1, 1])
    with b1:
        if st.button("立即檢查全部監控", type="primary", use_container_width=True):
            with st.spinner("檢查中（會呼叫 SerpAPI）…"):
                st.session_state["check_results"] = check_all_monitors()
            st.success("檢查完成，可到「監控中心」查看細節")
    with b2:
        st.html(
            (
                f'<div class="stat-card">'
                f'<div class="k">通知策略</div>'
                f'<div class="v" style="font-size:1.05rem">每次排程回報</div>'
                f"</div>"
            ))
    with b3:
        avg_txt = f"NT$ {avg:,.0f}" if avg else "—"
        st.html(
            (
                f'<div class="stat-card">'
                f'<div class="k">近 20 筆均價</div>'
                f'<div class="v" style="font-size:1.05rem">{avg_txt}</div>'
                f"</div>"
            ))


def page_search():
    inject_hero(
        "搜尋航班",
        "設定航線與行程條件，快速比較可用票價",
        ["多來源票價", "價格比較", "目標價監控"],
        kicker="FLIGHT SEARCH",
    )
    inject_cmd_bar(
        api_ok=flight_api_ready(),
        channels=configured_channels(),
        cooldown_h=NOTIFY_COOLDOWN_HOURS,
        extra="免費額度優先",
    )

    st.html('<div class="section-label">搜尋條件</div>')

    # 交換機塲用 session 預設
    if "origin_default" not in st.session_state:
        st.session_state["origin_default"] = "TPE"
    if "dest_default" not in st.session_state:
        st.session_state["dest_default"] = "KIX"

    st.html('<div class="form-shell">')
    c1, c_swap, c2 = st.columns([1, 0.18, 1])
    with c1:
        origin = pick_airport("出發地", "origin", st.session_state["origin_default"])
    with c_swap:
        st.write("")
        st.write("")
        if st.button("⇄", help="交換出發/目的地", use_container_width=True, key="swap_airports"):
            st.session_state["origin_default"], st.session_state["dest_default"] = (
                st.session_state.get("dest_default", "KIX"),
                st.session_state.get("origin_default", "TPE"),
            )
            # 清掉 select 狀態，讓預設生效
            for k in list(st.session_state.keys()):
                if k.startswith("origin_") or k.startswith("dest_"):
                    if k not in ("origin_default", "dest_default"):
                        del st.session_state[k]
            st.rerun()
    with c2:
        destination = pick_airport("目的地", "dest", st.session_state["dest_default"])

    # 記住目前選擇，方便下次交換
    st.session_state["origin_default"] = origin
    st.session_state["dest_default"] = destination

    r1, r2, r3, r4, r5 = st.columns([1.2, 1.2, 1, 1, 1.2])
    with r1:
        trip_type = st.radio("行程類型", ["單程", "來回"], horizontal=True)
    with r2:
        outbound = st.date_input(
            "出發日期",
            value=date.today() + timedelta(days=30),
            min_value=date.today(),
        )
    with r3:
        if trip_type == "來回":
            return_d = st.date_input(
                "回程日期",
                value=date.today() + timedelta(days=37),
                min_value=outbound,
            )
        else:
            return_d = None
            st.write("")
            st.caption("單程無需回程")
    with r4:
        adults = st.number_input("成人", min_value=1, max_value=9, value=1)
    with r5:
        travel_class_str = st.selectbox("艙等", ["經濟艙", "豪華經濟艙", "商務艙", "頭等艙"])
        class_map = {"經濟艙": 1, "豪華經濟艙": 2, "商務艙": 3, "頭等艙": 4}
        travel_class = class_map[travel_class_str]

    st.write("")
    search_btn = st.button("搜尋最佳機票", type="primary", use_container_width=True)
    st.html("</div>")

    if search_btn:
        if origin == destination:
            st.error("出發地與目的地不可相同")
            return
        with st.spinner("正在向票價資料來源查詢價格…"):
            try:
                result = search_and_save(
                    origin=origin,
                    destination=destination,
                    outbound_date=outbound.isoformat(),
                    return_date=return_d.isoformat() if return_d else None,
                    adults=int(adults),
                    travel_class=travel_class,
                )
                st.session_state["last_result"] = result
                st.session_state["last_adults"] = int(adults)
                st.session_state["last_market_reference"] = None
                if travelpayouts_ready() and int(adults) == 1 and travel_class == 1:
                    try:
                        st.session_state["last_market_reference"] = get_market_reference(
                            origin=origin,
                            destination=destination,
                            outbound_date=outbound.isoformat(),
                            return_date=return_d.isoformat() if return_d else None,
                        )
                    except TravelpayoutsError as market_error:
                        st.session_state["last_market_reference"] = {
                            "available": False,
                            "error": str(market_error),
                        }
                elif travelpayouts_ready():
                    st.session_state["last_market_reference"] = {
                        "available": False,
                        "unsupported_fare_basis": True,
                    }
            except FlightAPIError as e:
                st.error(f"查詢失敗：{e}")
                return
            except Exception as e:
                st.error(f"發生錯誤：{e}")
                return

    result = st.session_state.get("last_result")
    if not result:
        st.html(
            (
                '<div class="empty-state">'
                '<div class="icon">🛫</div>'
                '<div style="font-weight:600;color:#64748b;margin-bottom:0.3rem">尚未查詢</div>'
                "<div>選擇航線與日期，按下搜尋即可取得即時企業級報價</div>"
                "</div>"
            ))
        return

    best = result.get("best_price")
    flights = result.get("flights") or []
    o, d = result["origin"], result["destination"]
    o_label = get_label(o)
    d_label = get_label(d)
    date_txt = result["outbound_date"]
    if result.get("return_date"):
        date_txt += f"  →  {result['return_date']}"
    adults_saved = int(result.get("adults") or st.session_state.get("last_adults") or adults)
    travel_class_saved = int(result.get("travel_class") or 1)
    travel_class_name = {1: "經濟艙", 2: "豪華經濟艙", 3: "商務艙", 4: "頭等艙"}.get(
        travel_class_saved, "經濟艙"
    )

    # 價格洞察
    insight = db.get_route_price_stats(
        result["route_id"],
        outbound_date=result["outbound_date"],
        return_date=result.get("return_date"),
    )

    st.html('<div class="section-label">查詢結果</div>')

    left, right = st.columns([1.12, 1.88])
    with left:
        price_display = f"{best:,.0f}" if best is not None else "—"
        st.html(
            f'<div class="deal-price-card">'
            f'<div class="deal-price-label">目前最低價</div>'
            f'<div class="deal-price-value">'
            f'<span>TWD</span><strong>{price_display}</strong>'
            f'</div>'
            f'<div class="deal-price-route">{_safe(o)} → {_safe(d)} · {_safe(date_txt)}</div>'
            f'<div class="deal-price-meta">{adults_saved} 位成人 · {_safe(travel_class_name)}</div>'
            f'</div>'
        )
        render_price_insight(insight, best)
        market_reference = st.session_state.get("last_market_reference")
        st.html('<div class="section-label">市場趨勢參考</div>')
        if not travelpayouts_ready():
            st.caption("Travelpayouts Data API 未設定；目前僅顯示 SerpAPI 即時報價。")
        elif market_reference and market_reference.get("error"):
            st.warning(f"趨勢資料暫時無法取得：{market_reference['error']}")
        elif market_reference and market_reference.get("unsupported_fare_basis"):
            st.caption("為避免錯誤比價，快取趨勢只顯示於 1 位成人、經濟艙的搜尋。")
        elif not market_reference or not market_reference.get("available"):
            st.caption("Travelpayouts 快取中沒有這條航線／月份的可用觀察值。")
        else:
            reference_price = float(market_reference["price"])
            reference_label = (
                "同日快取觀察價"
                if market_reference.get("match") == "exact_date"
                else "本月快取低價"
            )
            price_gap = float(best) - reference_price if best is not None else None
            market_col1, market_col2 = st.columns(2)
            market_col1.metric(reference_label, f"NT$ {reference_price:,.0f}")
            market_col2.metric(
                "即時價相對差額",
                f"NT$ {price_gap:+,.0f}" if price_gap is not None else "—",
            )
            st.caption(
                f"觀察日期 {market_reference['departure_date']} · "
                f"樣本 {market_reference['observations']} 筆 · 快取資料，不能視為目前可購買價格"
            )
        st.html(
            (
                f'<div class="stat-grid">'
                f'<div class="stat-card"><div class="k">航班</div><div class="v">{len(flights)}</div></div>'
                f'<div class="stat-card"><div class="k">出發</div><div class="v" style="font-size:1rem">{o}</div></div>'
                f'<div class="stat-card"><div class="k">抵達</div><div class="v" style="font-size:1rem">{d}</div></div>'
                f"</div>"
            ))
        st.caption(f"{o_label}")
        st.caption(f"{d_label}")

        # Google Flights 快速連結
        gf_url = f"https://www.google.com/travel/flights?q=Flights+to+{d}+from+{o}+on+{result['outbound_date']}"
        if result.get("return_date"):
            gf_url += f"+returning+{result['return_date']}"
        st.html(
            f'<a href="{gf_url}" target="_blank" class="gf-link">'
            f'✈️ 在 Google Flights 查看並訂購</a>')
        st.write("")

        st.html('<div class="section-label">建立價格監控</div>')
        default_target = int(best * 0.9) if best else 5000
        target = st.number_input(
            "目標價格（低於此價通知）",
            min_value=100,
            max_value=500000,
            value=default_target,
            step=100,
        )
        if st.button("加入監控", use_container_width=True):
            res = db.add_monitor(
                route_id=result["route_id"],
                outbound_date=result["outbound_date"],
                return_date=result.get("return_date"),
                target_price=float(target),
                adults=adults_saved,
                travel_class=travel_class_saved,
                trip_type="round_trip" if result.get("return_date") else "one_way",
            )
            mid = res["id"]
            if res.get("duplicate"):
                st.info(f"已有相同監控 #{mid}，已更新目標價為 NT$ {target:,.0f}")
            else:
                st.success(f"已加入監控 #{mid} · 目標 NT$ {target:,.0f}")

    with right:
        st.html('<div class="section-label">航班選項</div>')
        f1, f2, f3 = st.columns([1, 1, 1.2])
        with f1:
            direct_only = st.checkbox("只看直飛", value=False)
        with f2:
            max_stops = st.selectbox("最多轉機", [0, 1, 2, 3], index=2, disabled=direct_only)
        with f3:
            sort_by = st.selectbox(
                "排序",
                ["價格由低到高", "直飛優先", "時長由短到長", "出發時間"],
                index=0,
            )

        shown = _filter_sort_flights(
            flights,
            direct_only=direct_only,
            max_stops=int(max_stops),
            sort_by=sort_by,
        )
        st.caption(f"顯示 {len(shown)} / {len(flights)} 個選項")

        if not shown:
            st.info("沒有符合篩選條件的航班，請放寬條件")
        else:
            for i, f in enumerate(shown[:12]):
                render_flight_card(f, rank=i)


def page_history():
    inject_hero(
        "價格歷史",
        "用歷史資料判斷目前價格所處區間",
        ["航線比較", "價格區間", "CSV 匯出"],
        kicker="PRICE INTELLIGENCE",
    )

    routes = db.list_routes()
    if not routes:
        st.html(
            '<div class="empty-state"><div class="icon">📈</div><div>尚無歷史資料，請先查價一次</div></div>')
        return

    route_map = {
        f"{r['origin']} → {r['destination']}  ·  {r.get('origin_name', '')}": r for r in routes
    }

    # 支援多航線比較
    selected_labels = st.multiselect("航線（可多選比較）", list(route_map.keys()), default=[list(route_map.keys())[0]])
    if not selected_labels:
        st.info("請選擇至少一條航線")
        return
    selected_routes = [route_map[l] for l in selected_labels]

    all_data = []
    for route in selected_routes:
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM price_history WHERE route_id=? ORDER BY recorded_at ASC",
                (route["id"],),
            ).fetchall()
        for r in rows:
            d = dict(r)
            d["_route_label"] = f"{route['origin']}→{route['destination']}"
            all_data.append(d)

    if not all_data:
        st.warning("所選航線尚無價格紀錄")
        return

    df = pd.DataFrame(all_data)
    df["recorded_at"] = pd.to_datetime(df["recorded_at"])
    df["label"] = df["_route_label"] + " · " + df["outbound_date"] + df["return_date"].fillna("").apply(
        lambda x: f" / {x}" if x else ""
    )

    dates = sorted(df["outbound_date"].unique().tolist())
    selected = st.multiselect("篩選出發日", dates, default=dates[-min(5, len(dates)) :])
    if selected:
        df = df[df["outbound_date"].isin(selected)]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("紀錄筆數", f"{len(df)}")
    m2.metric("最低價", f"NT$ {df['price'].min():,.0f}")
    m3.metric("最高價", f"NT$ {df['price'].max():,.0f}")
    m4.metric("平均價", f"NT$ {df['price'].mean():,.0f}")

    def render_gf_insight(current_price: float, min_p: float, max_p: float, avg_p: float):
        if current_price < avg_p * 0.95:
            level_txt = "較低價位"
            pos_pct = 15
        elif current_price > avg_p * 1.05:
            level_txt = "較高價位"
            pos_pct = 85
        else:
            level_txt = "一般價位"
            pos_pct = 50

        low_bound = int((min_p + avg_p) / 2)
        high_bound = int((avg_p + max_p) / 2)
        if low_bound == high_bound:
            low_bound = int(min_p)
            high_bound = int(max_p)

        st.html(f"""
        <div class="price-insight">
            <div class="price-insight-eyebrow">目前價格定位</div>
            <div class="price-insight-title">NT$ {current_price:,.0f} <span>{level_txt}</span></div>
            <div class="price-insight-copy">歷史常見區間約為 NT$ {low_bound:,.0f}－{high_bound:,.0f}</div>
            <div class="price-range">
                <div class="price-range-low"></div>
                <div class="price-range-mid"></div>
                <div class="price-range-high"></div>
                <div class="price-range-marker" style="left:{pos_pct}%">
                    <div class="price-range-dot"></div>
                </div>
            </div>
            <div class="price-range-labels"><span>偏低</span><span>一般</span><span>偏高</span></div>
        </div>
        """)

    if len(selected_routes) == 1 and len(df) > 0:
        render_gf_insight(df['price'].iloc[-1], df['price'].min(), df['price'].max(), df['price'].mean())

    st.html('<div class="section-label">價格變化</div>')

    fig = go.Figure()
    palette = ["#087443", "#287d78", "#5f6d66", "#b7791f", "#7a4f87"]
    grouped = list(df.groupby("label"))
    for index, (name, g) in enumerate(grouped):
        color = palette[index % len(palette)]
        fig.add_trace(
            go.Scatter(
                x=g["recorded_at"],
                y=g["price"],
                mode="lines+markers",
                name=str(name),
                line=dict(width=2.6, color=color),
                marker=dict(size=6, color=color, line=dict(width=2, color="#ffffff")),
                fill="tozeroy" if len(grouped) == 1 else None,
                fillcolor="rgba(37, 99, 235, 0.08)",
                hoverinfo="x+y+name",
            )
        )

    xaxis_opts = dict(gridcolor="rgba(0,0,0,0)", title="", color="#64748b", showline=True, linewidth=1, linecolor="#dbe4ee", tickformat="%m-%d %H:%M")
    yaxis_opts = dict(gridcolor="#e7edf5", title="", color="#64748b", rangemode="tozero", tickprefix="NT$ ", showline=False, zeroline=False)

    if len(df) == 1:
        single_date = df["recorded_at"].iloc[0]
        xaxis_opts["range"] = [single_date - pd.Timedelta(hours=12), single_date + pd.Timedelta(hours=12)]

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        height=360,
        margin=dict(l=10, r=10, t=22, b=10),
        showlegend=len(grouped) > 1,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        xaxis=xaxis_opts,
        yaxis=yaxis_opts,
        font=dict(family="Inter, 'Noto Sans TC', sans-serif", color="#475569"),
    )
    st.plotly_chart(fig, use_container_width=True)

    st.html('<div class="section-label">明細紀錄</div>')
    show = df[
        ["recorded_at", "_route_label", "outbound_date", "return_date", "price", "airline", "stops", "duration"]
    ].sort_values("recorded_at", ascending=False)
    show.columns = ["記錄時間", "航線", "出發日", "回程日", "價格", "航空", "轉機", "時長"]
    st.dataframe(show, use_container_width=True, hide_index=True)

    # CSV 匯出
    csv_data = show.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "📥 匯出 CSV",
        data=csv_data,
        file_name=f"skywatch_price_history_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )


def page_monitors():
    inject_hero(
        "監控管理",
        "管理目標價、啟用狀態與通知結果",
        ["目標價", "排程查價", "通知追蹤"],
        kicker="MONITORING",
    )
    inject_cmd_bar(
        api_ok=flight_api_ready(),
        channels=configured_channels(),
        cooldown_h=NOTIFY_COOLDOWN_HOURS,
        extra="LIVE CHECK",
    )

    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("立即檢查全部", type="primary", use_container_width=True):
            with st.spinner("檢查中（會呼叫 SerpAPI）…"):
                st.session_state["check_results"] = check_all_monitors()

    # 自動停用已過出發日的監控
    expired_count = db.expire_monitors()
    if expired_count:
        st.info(f"已自動停用 {expired_count} 筆過期監控（出發日已過）")

    monitors = db.list_monitors(active_only=False)
    if not monitors:
        st.html(
            '<div class="empty-state"><div class="icon">🔔</div><div>尚無監控。查價後可設定目標價加入。</div></div>')
    else:
        st.html('<div class="section-label">監控清單</div>')
        for m in monitors:
            active = bool(m.get("active"))
            cls = "mon-active" if active else "mon-inactive"
            trip = m["outbound_date"]
            if m.get("return_date"):
                trip += f"  ⇄  {m['return_date']}"
            last_p = m.get("last_price")
            last_price_txt = f"NT$ {last_p:,.0f}" if last_p else "尚未檢查"
            status = "啟用中" if active else "已停用"
            hit = last_p is not None and last_p <= (m.get("target_price") or 0)
            if hit:
                cls = "mon-hit"

            active_chip = "green" if active else "amber"
            st.html(
                (
                    f'<div class="mon-card {cls}">'
                    f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap">'
                    f"<div>"
                    f'<div style="font-weight:700;font-size:1.05rem;color:#1e293b">#{m["id"]}  {m["origin"]} → {m["destination"]}</div>'
                    f'<div style="color:#64748b;font-size:0.85rem;margin-top:0.25rem">{trip}</div>'
                    f'<div style="margin-top:0.55rem">'
                    f'<span class="chip">目標 ≤ NT$ {m["target_price"]:,.0f}</span>'
                    f'<span class="chip {active_chip}">{status}</span>'
                    f"</div></div>"
                    f'<div style="text-align:right">'
                    f'<div style="color:#64748b;font-size:0.75rem">上次價格</div>'
                    f'<div style="font-weight:700;color:#087443;font-size:1.2rem">{last_price_txt}</div>'
                    f'<div style="color:#94a3b8;font-size:0.72rem">{m.get("last_checked") or "—"}</div>'
                    f"</div></div></div>"
                ))
            b1, b2, b3, b4 = st.columns([1, 1, 1.15, 1.15])
            confirm_key = f"confirm_delete_{m['id']}"
            with b1:
                if st.button("檢查", key=f"chk_{m['id']}", use_container_width=True):
                    with st.spinner("查詢中…"):
                        r = check_one_monitor(m)
                    if r.get("ok"):
                        if r.get("best_price") is not None:
                            st.success(f"目前最低 NT$ {r['best_price']:,.0f}")
                        else:
                            st.info("查無價格")
                        if r.get("hit"):
                            if r.get("skipped_notify"):
                                st.info("已達目標價；同一瞬間的重複通知已略過")
                            elif r.get("notified"):
                                st.warning("已達目標價，已發送通知")
                            else:
                                st.warning("已達目標價（通知可能失敗，請看通知紀錄）")
                        elif r.get("notified"):
                            st.success("本次報價已發送通知")
                        elif r.get("notify_reason") in ("cooldown", "deduplicated"):
                            st.info("已完成查價；防重複機制略過本次通知")
                    else:
                        st.error(r.get("error"))
            with b2:
                if active:
                    if st.button("停用", key=f"off_{m['id']}", use_container_width=True):
                        db.set_monitor_active(m["id"], False)
                        st.rerun()
                else:
                    if st.button("啟用", key=f"on_{m['id']}", use_container_width=True):
                        db.set_monitor_active(m["id"], True)
                        st.rerun()
            with b3:
                if not st.session_state.get(confirm_key):
                    if st.button("刪除", key=f"del_{m['id']}", use_container_width=True):
                        st.session_state[confirm_key] = True
                        st.rerun()
                elif st.button("確認刪除", key=f"del_ok_{m['id']}", use_container_width=True, type="primary"):
                    db.delete_monitor(m["id"])
                    st.session_state.pop(confirm_key, None)
                    st.rerun()
            with b4:
                if st.session_state.get(confirm_key):
                    if st.button("取消", key=f"del_cancel_{m['id']}", use_container_width=True):
                        st.session_state.pop(confirm_key, None)
                        st.rerun()

    if st.session_state.get("check_results"):
        st.html('<div class="section-label">最近一次批次結果</div>')
        for r in st.session_state["check_results"]:
            if not r.get("ok"):
                st.error(f"#{r['monitor_id']}: {r.get('error')}")
            else:
                if r.get("hit") and r.get("skipped_notify"):
                    hit = "達標（冷卻去重，未重送）"
                elif r.get("hit") and r.get("notified"):
                    hit = "達標並通知"
                elif r.get("hit"):
                    hit = "達標（通知失敗或僅本機紀錄）"
                elif r.get("notified"):
                    hit = "未達標，已回報最新價格"
                elif r.get("notify_reason") in ("cooldown", "deduplicated"):
                    hit = "未達標（防重複略過通知）"
                else:
                    hit = "未達標"
                st.write(
                    f"#{r['monitor_id']} · 現價 {r.get('best_price')} / 目標 {r.get('target_price')} → {hit}"
                )

    st.markdown("---")
    st.html('<div class="section-label">通知紀錄</div>')
    notes = db.list_notifications(30)
    if notes:
        ndf = pd.DataFrame(notes)[["created_at", "channel", "success", "message"]]
        ndf.columns = ["時間", "管道", "成功", "訊息"]
        st.dataframe(ndf, use_container_width=True, hide_index=True)
    else:
        st.caption("尚無通知紀錄")


def page_settings():
    inject_hero(
        "通知與系統設定",
        "檢查資料來源、免費額度、通知策略與背景排程",
        ["額度節省", "API 備援", "Telegram"],
        kicker="SYSTEM",
    )

    active_monitors = db.list_monitors(active_only=True)
    col1, col2 = st.columns(2)
    with col1:
        st.html('<div class="section-label">連線狀態</div>')
        st.write(f"**SerpAPI：** {'已設定' if SERPAPI_API_KEY else '未設定'}")
        st.write(f"**Travelpayouts 趨勢：** {'已設定' if travelpayouts_ready() else '未設定（可選）'}")
        st.write("**查詢策略：** 單次主查詢 → 達標後補查詳細資料")
        st.write("**通知管道：** " + "、".join(configured_channels()))
        st.markdown(
            f"""
            在專案 `.env` 設定：

            ```env
            SERPAPI_API_KEY=...
            TRAVELPAYOUTS_TOKEN=...
            DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
            TELEGRAM_BOT_TOKEN=...
            TELEGRAM_CHAT_ID=...
            ALWAYS_NOTIFY={'true' if ALWAYS_NOTIFY else 'false'}
            NOTIFY_COOLDOWN_HOURS={NOTIFY_COOLDOWN_HOURS:g}
            ```

            目前策略：**{'每次查價均通知' if ALWAYS_NOTIFY else '僅達標或價格變動時通知'}**
            固定回報：**每日 00:00 / 12:00 必送**
            手動冷卻：**{NOTIFY_COOLDOWN_HOURS:g} 小時**
            詳細補查：**{'僅達標時啟用' if MONITOR_FETCH_DETAILS_ON_HIT else '停用'}**
            """
        )
    with col2:
        st.html('<div class="section-label">測試通知</div>')
        msg = st.text_input("訊息內容", value="SkyWatch 測試通知 · 機票監控系統運作正常")
        if st.button("發送測試", type="primary", use_container_width=True):
            for ch, ok, detail in send_all(msg):
                if ok:
                    st.success(f"{ch} · 成功")
                else:
                    st.error(f"{ch} · 失敗 — {detail}")

    st.html('<div class="section-label">API 免費額度</div>')
    summaries = quota_summaries()
    serp_summary = summaries[0]
    estimated_monthly = estimate_monthly_primary_searches(len(active_monitors))
    qc1, qc2, qc3, qc4 = st.columns(4)
    qc1.metric(
        "SerpAPI 本月",
        f"{serp_summary['used']} / {serp_summary['limit'] or '—'}",
        f"嘗試 {serp_summary['attempted']} 次",
    )
    qc2.metric(
        "免費剩餘",
        f"{serp_summary['remaining']} 次" if serp_summary["remaining"] is not None else "未設定",
    )
    qc3.metric("排程月預估", f"{estimated_monthly} 次", f"{len(active_monitors)} 個啟用監控")
    usage_percent = serp_summary.get("percent") or 0.0
    qc4.metric("額度狀態", "注意" if usage_percent >= API_USAGE_WARNING_PERCENT else "正常", f"{usage_percent:.0f}%")
    if serp_summary["limit"]:
        st.progress(min(1.0, usage_percent / 100.0), text=f"SerpAPI 免費額度已使用 {usage_percent:.1f}%")
    if usage_percent >= API_USAGE_WARNING_PERCENT:
        st.warning(
            f"SerpAPI 已超過 {API_USAGE_WARNING_PERCENT:g}% 警戒線；系統每月只推播一次額度警告。"
        )
    elif estimated_monthly > (serp_summary["limit"] or 10**9):
        st.info("目前排程的整月預估會超過免費額度；建議減少監控項目或改為每日一次。")
    st.caption("統計只記錄成功請求與操作類型，不保存航線、日期、API key 或旅遊資料。")

    st.html('<div class="section-label">獨立背景排程</div>')
    scheduler_health = get_scheduler_health()
    notification_health = get_notification_health()
    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("排程健康", "正常" if scheduler_health["healthy"] else "待確認")
    sc2.metric("監控項目", f"{len(active_monitors)} 個")
    last_run = scheduler_health.get("last_run")
    sc3.metric("最近完成", last_run.strftime("%m/%d %H:%M") if last_run else "—")
    sc4.metric("最近 Telegram", notification_health["label"])

    if not active_monitors:
        st.info("尚無啟用中的監控項目，請先在「監控中心」設定目標價")
    elif not scheduler_health["healthy"]:
        st.warning("超過 14 小時沒有成功紀錄，請確認 start_scheduler_silent.vbs 是否仍在執行。")
    else:
        st.success("獨立排程運作正常 · 每日 00:00 與 12:00 自動查價")
    next_run = scheduler_health.get("next_run")
    if notification_health["sent_today"]:
        st.info(f"今天已有成功通知：{notification_health['label']}。")
    elif notification_health.get("latest"):
        st.warning(f"今天尚無成功通知；最近一次為 {notification_health['label']}。")
    if scheduler_health.get("last_error"):
        st.error(f"最近排程錯誤：{scheduler_health['last_error']}")
    st.caption(
        "下一次預定執行："
        + (next_run.strftime("%m/%d %H:%M") if next_run else "待確認")
        + " · 網頁程序不啟動第二套排程；完全相同的報價內容也會依管道防重。"
    )

    st.html('<div class="section-label">通知設定教學</div>')
    t1, t2 = st.columns(2)
    with t1:
        st.markdown(
            """
            **Discord**
            1. 伺服器設定 → 整合 → Webhooks
            2. 新增 Webhook，複製 URL
            3. 貼到 `.env` 的 `DISCORD_WEBHOOK_URL`
            """
        )
    with t2:
        st.markdown(
            """
            **Telegram**
            1. 找 @BotFather 建立 bot
            2. 傳訊息給 bot
            3. 用 getUpdates 查 `chat_id`
            4. 填入 token 與 chat_id
            """
        )

    # ═══ 資料庫維護 ═══
    st.html('<div class="section-label">資料維護與匯出</div>')
    from src.config import DB_PATH
    import os
    db_size = os.path.getsize(DB_PATH) if DB_PATH.exists() else 0
    db_mb = db_size / (1024 * 1024)

    col_db, col_export = st.columns(2)

    with col_db:
        with st.container(border=True):
            st.markdown("#### 資料庫清理")
            st.caption("管理價格歷史資料，不影響監控任務設定。")
            sub1, sub2 = st.columns([1, 1.2])
            with sub1:
                st.metric("資料庫大小", f"{db_mb:.2f} MB")
            with sub2:
                cleanup_days = st.number_input(
                    "保留最近天數", min_value=7, max_value=365, value=90, step=30
                )

            confirm_cleanup = st.checkbox(
                f"確認刪除 {cleanup_days} 天前的價格紀錄",
                key="confirm_cleanup_history",
            )
            if st.button("清理舊紀錄", use_container_width=True, disabled=not confirm_cleanup):
                deleted = db.cleanup_old_records(days=cleanup_days)
                st.success(f"已清理 {deleted} 筆舊價格紀錄")

    with col_export:
        with st.container(border=True):
            st.markdown("#### 監控設定匯出")
            st.caption("將航線、日期、目標價與啟用狀態匯出為 CSV 備份。")
            monitors_export = db.list_monitors(active_only=False)
            if monitors_export:
                mdf = pd.DataFrame(monitors_export)
                csv_m = mdf.to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "下載 CSV 備份",
                    data=csv_m,
                    file_name=f"skywatch_monitors_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
            else:
                st.caption("目前尚無監控資料可匯出")


def page_help():
    inject_hero(
        "使用說明",
        "從第一次查價到建立自動通知的完整流程",
        ["快速開始", "功能說明", "常見問題"],
        kicker="GUIDE",
    )

    with st.expander("快速開始", expanded=True):
        st.markdown(
            """
            1. 確認 `.env` 有 `SERPAPI_API_KEY`（到 [serpapi.com](https://serpapi.com) 註冊取得）
            2. 安裝依賴：`.venv\\Scripts\\python.exe -m pip install -r requirements.txt`
            3. 啟動：`start_skywatch.bat`
            4. 瀏覽器開啟 `http://localhost:8501`
            5. （可選）設定 Discord / Telegram 通知
            6. （可選）`python scheduler.py` 背景監控
            """
        )

    with st.expander("功能一覽"):
        st.markdown(
            """
            | 功能 | 說明 |
            |------|------|
            | 儀表板 | 營運狀態、達標數、最近查價一覽 |
            | 搜尋航班 | 交換機場、篩選排序、價格洞察、碳排放顯示 |
            | 價格歷史 | 多航線比較、價格區間、CSV 匯出 |
            | 監控管理 | 目標價通知、防重複、冷卻去重、過期自動停用 |
            | 通知與系統設定 | 背景排程、通知測試、資料清理與匯出 |
            | 全球機場 | 約 8,800 個 IATA，支援中英文搜尋 |
            """
        )

    with st.expander("常見問題"):
        st.markdown(
            """
            **Q: SerpAPI 免費額度多少？**
            > 免費方案目前每月 250 次成功查詢。SkyWatch 會統計用量，達 80% 時只提醒一次。

            **Q: 為什麼查不到價格？**
            > 可能是日期太近或航線無直飛。試試放寬日期範圍或換難度較低的航線。

            **Q: 通知沒有收到？**
            > 1. 確認 `.env` 中的 token/webhook 正確
            > 2. 到「通知與系統設定」發送測試通知確認
            > 3. 檢查通知冷卻時間（預設 6h 內不重複推播）

            **Q: 過期監控會浪費 API 額度嗎？**
            > 不會！系統會自動停用出發日已過的監控。

            **Q: 可以同時監控多少條航線？**
            > 每日查兩次時，一個監控約 60 次／月；250 次免費額度建議最多 4 個啟用監控。
            """
        )

    with st.expander("使用注意事項"):
        st.markdown(
            """
            - 平常排程每個監控只做一次主查詢，達標時才補查回程與開票平台
            - SerpAPI 免費額度有限，建議固定每日 1～2 次，不要使用高頻間隔
            - 價格僅供參考，下單前請至航空公司或 OTA 再確認
            - 勿將 `.env` 上傳公開儲存庫
            - 介面採一致的淺色主題，確保長時間閱讀清楚舒適
            - 資料庫定期清理可在「通知與系統設定」執行
            """
        )


def main():

    page = sidebar()
    if page == "儀表板":
        page_dashboard()
    elif page == "搜尋航班":
        page_search()
    elif page == "價格歷史":
        page_history()
    elif page == "監控管理":
        page_monitors()
    elif page == "通知與系統設定":
        page_settings()
    else:
        page_help()


if __name__ == "__main__":
    main()
