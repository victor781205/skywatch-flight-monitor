"""Reusable, escaped HTML primitives for the SkyWatch Streamlit interface.

The functions in this module do not depend on Streamlit.  They return small,
semantic HTML fragments that can be passed to ``st.html``.  All public text
arguments are escaped before rendering so API responses and saved user input
cannot inject markup into the application.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape
from math import isfinite
from typing import Any
from urllib.parse import urlsplit


_TONES = frozenset({"primary", "info", "success", "warning", "danger", "neutral"})
_STEP_STATES = frozenset({"complete", "active", "pending", "error"})
_RUN_STATES: dict[str, tuple[str, str, str]] = {
    "queued": ("neutral", "等待中", "○"),
    "running": ("info", "執行中", "↻"),
    "succeeded": ("success", "已完成", "✓"),
    "partial": ("warning", "部分完成", "△"),
    "failed": ("danger", "失敗", "!"),
    "skipped": ("neutral", "已略過", "–"),
    "missed": ("warning", "偵測到漏跑", "!"),
    "backfilled": ("success", "已自動補跑", "✓"),
}
_RUN_STATE_ALIASES = {
    "success": "succeeded",
    "error": "failed",
    "retrying": "running",
    # Compatibility with the earlier presentation-only name.  The database
    # persists the real state as ``backfilled``.
    "recovered": "backfilled",
}


def _text(value: Any) -> str:
    """Return a safely escaped text representation."""

    if value is None:
        return ""
    return escape(str(value), quote=True)


def _tone(value: str | None, default: str = "neutral") -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in _TONES else default


def _finite_number(value: int | float, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _safe_href(value: str | None) -> str | None:
    """Allow normal web links and application-local paths only."""

    href = str(value or "").strip()
    if not href:
        return None
    if href.startswith(("/", "#", "./", "../")):
        return _text(href)
    parsed = urlsplit(href)
    if parsed.scheme.lower() in {"http", "https", "mailto"}:
        return _text(href)
    return None


def badge(text: Any, tone: str = "neutral", *, icon: Any | None = None) -> str:
    """Render a compact status/category badge."""

    safe_tone = _tone(tone)
    icon_html = (
        f'<span class="ui-badge__icon" aria-hidden="true">{_text(icon)}</span>'
        if icon not in (None, "")
        else ""
    )
    return (
        f'<span class="ui-badge ui-badge--{safe_tone}">'
        f"{icon_html}<span>{_text(text)}</span></span>"
    )


def _badge_from_spec(spec: Any) -> str:
    if isinstance(spec, Mapping):
        return badge(spec.get("text", ""), str(spec.get("tone", "neutral")), icon=spec.get("icon"))
    if isinstance(spec, tuple) and len(spec) >= 2:
        return badge(spec[0], str(spec[1]))
    return badge(spec, "primary")


def page_header(
    title: Any,
    subtitle: Any = "",
    *,
    eyebrow: Any = "SKYWATCH",
    badges: Sequence[Any] = (),
    meta: Any = "",
) -> str:
    """Render the light, product-level header used at the top of each page."""

    subtitle_html = f'<p class="ui-page-header__subtitle">{_text(subtitle)}</p>' if subtitle else ""
    badge_html = ""
    if badges:
        badge_html = '<div class="ui-page-header__badges">' + "".join(
            _badge_from_spec(item) for item in badges
        ) + "</div>"
    meta_html = f'<div class="ui-page-header__meta">{_text(meta)}</div>' if meta else ""
    return (
        '<header class="ui-page-header">'
        '<div class="ui-page-header__content">'
        f'<div class="ui-page-header__eyebrow">{_text(eyebrow)}</div>'
        f'<h1 class="ui-page-header__title">{_text(title)}</h1>'
        f"{subtitle_html}{badge_html}"
        f"</div>{meta_html}</header>"
    )


def section_heading(
    title: Any,
    description: Any = "",
    *,
    eyebrow: Any = "",
    meta: Any = "",
) -> str:
    """Render a clear section boundary with optional context."""

    eyebrow_html = f'<div class="ui-section-heading__eyebrow">{_text(eyebrow)}</div>' if eyebrow else ""
    description_html = (
        f'<p class="ui-section-heading__description">{_text(description)}</p>' if description else ""
    )
    meta_html = f'<div class="ui-section-heading__meta">{_text(meta)}</div>' if meta else ""
    return (
        '<div class="ui-section-heading">'
        '<div class="ui-section-heading__copy">'
        f"{eyebrow_html}<h2>{_text(title)}</h2>{description_html}"
        f"</div>{meta_html}</div>"
    )


def status_banner(
    title: Any,
    message: Any,
    *,
    status: str = "info",
    label: Any = "",
    details: Sequence[Any] = (),
) -> str:
    """Render an accessible, high-signal system status banner."""

    tone = _tone(status, "info")
    role = "alert" if tone == "danger" else "status"
    label_html = badge(label, tone) if label else ""
    details_html = ""
    if details:
        details_html = '<ul class="ui-status-banner__details">' + "".join(
            f"<li>{_text(item)}</li>" for item in details
        ) + "</ul>"
    return (
        f'<section class="ui-status-banner ui-status-banner--{tone}" role="{role}">'
        '<span class="ui-status-banner__mark" aria-hidden="true"></span>'
        '<div class="ui-status-banner__body">'
        f'<div class="ui-status-banner__top"><strong>{_text(title)}</strong>{label_html}</div>'
        f'<p>{_text(message)}</p>{details_html}</div></section>'
    )


def metric_card(
    label: Any,
    value: Any,
    *,
    caption: Any = "",
    trend: Any = "",
    tone: str = "primary",
    icon: Any = "",
) -> str:
    """Render a compact metric card for dashboard summaries."""

    safe_tone = _tone(tone, "primary")
    icon_html = f'<span class="ui-metric-card__icon" aria-hidden="true">{_text(icon)}</span>' if icon else ""
    caption_html = f'<p class="ui-metric-card__caption">{_text(caption)}</p>' if caption else ""
    trend_html = f'<div class="ui-metric-card__trend">{_text(trend)}</div>' if trend else ""
    return (
        f'<article class="ui-metric-card ui-metric-card--{safe_tone}">'
        f'<div class="ui-metric-card__top"><span class="ui-metric-card__label">{_text(label)}</span>{icon_html}</div>'
        f'<div class="ui-metric-card__value">{_text(value)}</div>{caption_html}{trend_html}</article>'
    )


def status_card(
    label: Any,
    value: Any,
    *,
    description: Any = "",
    status: str = "neutral",
    meta: Any = "",
) -> str:
    """Render an operational status card with a visible state indicator."""

    tone = _tone(status)
    description_html = f'<p class="ui-status-card__description">{_text(description)}</p>' if description else ""
    meta_html = f'<div class="ui-status-card__meta">{_text(meta)}</div>' if meta else ""
    return (
        f'<article class="ui-status-card ui-status-card--{tone}">'
        '<div class="ui-status-card__head">'
        f'<span class="ui-status-card__dot" aria-hidden="true"></span><span>{_text(label)}</span>'
        f'</div><strong class="ui-status-card__value">{_text(value)}</strong>'
        f"{description_html}{meta_html}</article>"
    )


def stepper(steps: Sequence[Any], current: int = 0, *, label: Any = "流程進度") -> str:
    """Render a responsive ordered stepper.

    A step can be a string or a mapping with ``label``, ``description`` and an
    optional explicit ``status`` (complete, active, pending or error).
    ``current`` is zero-based.
    """

    items: list[str] = []
    try:
        current_index = int(current)
    except (TypeError, ValueError):
        current_index = 0

    for index, raw_step in enumerate(steps):
        if isinstance(raw_step, Mapping):
            step_label = raw_step.get("label", "")
            description = raw_step.get("description", "")
            explicit_state = str(raw_step.get("status", "")).lower()
        else:
            step_label = raw_step
            description = ""
            explicit_state = ""
        state = explicit_state if explicit_state in _STEP_STATES else (
            "complete" if index < current_index else "active" if index == current_index else "pending"
        )
        marker = "✓" if state == "complete" else "!" if state == "error" else str(index + 1)
        current_attr = ' aria-current="step"' if state == "active" else ""
        description_html = (
            f'<span class="ui-stepper__description">{_text(description)}</span>' if description else ""
        )
        items.append(
            f'<li class="ui-stepper__item ui-stepper__item--{state}"{current_attr}>'
            f'<span class="ui-stepper__marker" aria-hidden="true">{marker}</span>'
            f'<span class="ui-stepper__copy"><strong>{_text(step_label)}</strong>{description_html}</span></li>'
        )
    return f'<ol class="ui-stepper" aria-label="{_text(label)}">{"".join(items)}</ol>'


def empty_state(
    title: Any,
    description: Any = "",
    *,
    icon: Any = "✦",
    action_label: Any = "",
    action_href: str | None = None,
) -> str:
    """Render a helpful empty state; unsafe action URLs are discarded."""

    description_html = f'<p>{_text(description)}</p>' if description else ""
    href = _safe_href(action_href)
    action_html = ""
    if action_label and href:
        action_html = f'<a class="ui-empty-state__action" href="{href}">{_text(action_label)}</a>'
    return (
        '<section class="ui-empty-state">'
        f'<div class="ui-empty-state__icon" aria-hidden="true">{_text(icon)}</div>'
        f'<h3>{_text(title)}</h3>{description_html}{action_html}</section>'
    )


def run_status(
    status: str,
    *,
    title: Any = "查價任務",
    message: Any = "",
    timestamp: Any = "",
    attempt: int | None = None,
) -> str:
    """Render one scheduler/run-ledger status row."""

    normalized = str(status or "").strip().lower()
    normalized = _RUN_STATE_ALIASES.get(normalized, normalized)
    tone, state_label, marker = _RUN_STATES.get(normalized, ("neutral", "狀態未知", "?"))
    message_html = f'<p class="ui-run-status__message">{_text(message)}</p>' if message else ""
    timestamp_html = f'<time class="ui-run-status__time">{_text(timestamp)}</time>' if timestamp else ""
    attempt_html = badge(f"第 {int(attempt)} 次", "warning") if attempt is not None else ""
    return (
        f'<article class="ui-run-status ui-run-status--{tone}" data-state="{_text(normalized)}" '
        f'aria-label="{_text(title)}：{_text(state_label)}">'
        f'<span class="ui-run-status__marker" aria-hidden="true">{marker}</span>'
        '<div class="ui-run-status__body"><div class="ui-run-status__top">'
        f'<strong>{_text(title)}</strong>{badge(state_label, tone)}{attempt_html}</div>'
        f"{message_html}</div>{timestamp_html}</article>"
    )


def quota_meter(
    used: int | float,
    limit: int | float,
    *,
    label: Any = "API 免費額度",
    unit: Any = "次",
    mode: Any = "",
    reset_at: Any = "",
) -> str:
    """Render an accessible quota meter with clamped, safe arithmetic."""

    used_number = max(0.0, _finite_number(used))
    limit_number = max(0.0, _finite_number(limit))
    percent = min(100.0, used_number / limit_number * 100.0) if limit_number else 0.0
    tone = "danger" if percent >= 90 else "warning" if percent >= 70 else "success"
    remaining = max(0.0, limit_number - used_number)
    mode_html = badge(mode, "primary") if mode else ""
    reset_html = f'<span>重置：{_text(reset_at)}</span>' if reset_at else ""
    return (
        f'<section class="ui-quota-meter ui-quota-meter--{tone}">'
        f'<div class="ui-quota-meter__top"><div><span class="ui-quota-meter__label">{_text(label)}</span>{mode_html}</div>'
        f'<strong>{used_number:,.0f} / {limit_number:,.0f} {_text(unit)}</strong></div>'
        f'<div class="ui-quota-meter__track" role="progressbar" aria-label="{_text(label)}" '
        f'aria-valuemin="0" aria-valuemax="100" aria-valuenow="{percent:.0f}">'
        f'<span style="width:{percent:.2f}%"></span></div>'
        f'<div class="ui-quota-meter__meta"><span>剩餘 {remaining:,.0f} {_text(unit)}</span>'
        f'<span>{percent:.0f}% 已使用</span>{reset_html}</div></section>'
    )


def _format_price(value: int | float | None, currency: Any) -> str:
    if value is None:
        return "尚無報價"
    number = _finite_number(value)
    return f"{_text(currency)} {number:,.0f}"


def _flight_leg(leg: Mapping[str, Any], direction: str) -> str:
    """Render one safely escaped leg inside :func:`flight_pair_card`."""

    airline = leg.get("airline", "航空公司待確認")
    flight_number = leg.get("flight_number", "")
    airline_label = _text(airline)
    if flight_number:
        airline_label += f" · {_text(flight_number)}"

    stops = leg.get("stops")
    if stops in (0, "0"):
        stop_label = "直飛"
    elif stops in (None, ""):
        stop_label = "轉機資訊待確認"
    else:
        try:
            stop_label = f"轉機 {max(0, int(stops))} 次"
        except (TypeError, ValueError):
            stop_label = str(stops)

    duration = leg.get("duration", "")
    meta_parts = [stop_label]
    if duration:
        meta_parts.append(str(duration))

    return (
        '<section class="ui-flight-pair__leg">'
        '<div class="ui-flight-pair__leg-head">'
        f'<span class="ui-flight-pair__direction">{_text(direction)}</span>'
        f'<time class="ui-flight-pair__date">{_text(leg.get("date", ""))}</time></div>'
        f'<div class="ui-flight-pair__airline">{airline_label}</div>'
        '<div class="ui-flight-pair__route">'
        '<div class="ui-flight-pair__airport">'
        f'<strong>{_text(leg.get("departure_time", "--:--"))}</strong>'
        f'<span>{_text(leg.get("origin", "---"))}</span></div>'
        '<span class="ui-flight-pair__line" aria-hidden="true"></span>'
        '<div class="ui-flight-pair__airport">'
        f'<strong>{_text(leg.get("arrival_time", "--:--"))}</strong>'
        f'<span>{_text(leg.get("destination", "---"))}</span></div></div>'
        f'<div class="ui-flight-pair__meta">{_text(" · ".join(meta_parts))}</div></section>'
    )


def flight_pair_card(
    outbound: Mapping[str, Any],
    inbound: Mapping[str, Any],
    *,
    total_price: int | float | None,
    currency: Any = "NT$",
    best: bool = False,
    price_label: Any = "來回含稅總價",
    price_note: Any = "每位旅客",
) -> str:
    """Render one complete round-trip result as outbound, inbound and total.

    The paired layout prevents an outbound-only quote from looking complete.
    Both mappings accept ``date``, ``airline``, ``flight_number``,
    ``departure_time``, ``arrival_time``, ``origin``, ``destination``,
    ``duration`` and ``stops``.  Unknown fields are deliberately ignored.
    """

    best_class = " ui-flight-pair--best" if best else ""
    best_badge = badge("最佳組合", "primary", icon="✓") if best else ""
    return (
        f'<article class="ui-flight-pair{best_class}" aria-label="完整來回航班組合">'
        f'{_flight_leg(outbound, "去程")}{_flight_leg(inbound, "回程")}'
        '<aside class="ui-flight-pair__total">'
        f'<span class="ui-flight-pair__total-label">{_text(price_label)}</span>'
        f'<strong>{_format_price(total_price, currency)}</strong>'
        f'<small>{_text(price_note)}</small>{best_badge}</aside></article>'
    )


def monitor_card(
    origin: Any,
    destination: Any,
    *,
    outbound_date: Any,
    return_date: Any = "",
    target_price: int | float | None = None,
    current_price: int | float | None = None,
    currency: Any = "NT$",
    active: bool = True,
    hit: bool = False,
    monitor_id: Any = "",
    airlines: Sequence[Any] = (),
    direct_only: bool = False,
    time_window: Any = "",
    schedule: Any = "",
    notification_policy: Any = "",
) -> str:
    """Render a full monitor summary without exposing unescaped saved data."""

    tone = "success" if hit else "primary" if active else "neutral"
    state_text = "已達目標價" if hit else "監控中" if active else "已暫停"
    trip_dates = _text(outbound_date)
    if return_date:
        trip_dates += f'<span aria-hidden="true"> → </span>{_text(return_date)}'
    id_html = f'<span class="ui-monitor-card__id">#{_text(monitor_id)}</span>' if monitor_id not in (None, "") else ""
    conditions: list[str] = []
    if airlines:
        conditions.append("航空公司：" + "、".join(_text(item) for item in airlines))
    if direct_only:
        conditions.append("僅限直飛")
    if time_window:
        conditions.append("時段：" + _text(time_window))
    if schedule:
        conditions.append("排程：" + _text(schedule))
    if notification_policy:
        conditions.append("通知：" + _text(notification_policy))
    conditions_html = ""
    if conditions:
        conditions_html = '<div class="ui-monitor-card__conditions">' + "".join(
            f'<span class="ui-monitor-card__condition">{item}</span>' for item in conditions
        ) + "</div>"
    target_html = _format_price(target_price, currency)
    current_html = _format_price(current_price, currency)
    gap_html = ""
    if current_price is not None and target_price is not None:
        gap = _finite_number(current_price) - _finite_number(target_price)
        gap_label = "低於目標" if gap <= 0 else "距離目標"
        gap_html = f'<span class="ui-monitor-card__gap">{gap_label} {_text(currency)} {abs(gap):,.0f}</span>'
    return (
        f'<article class="ui-monitor-card ui-monitor-card--{tone}" aria-label="{_text(origin)} 到 {_text(destination)} 航班監控">'
        '<div class="ui-monitor-card__main"><div class="ui-monitor-card__route-row">'
        f'{id_html}<h3><span>{_text(origin)}</span><span class="ui-monitor-card__arrow" aria-hidden="true">→</span>'
        f'<span>{_text(destination)}</span></h3>{badge(state_text, tone)}</div>'
        f'<div class="ui-monitor-card__dates">{trip_dates}</div>{conditions_html}</div>'
        '<div class="ui-monitor-card__prices">'
        f'<div><span>目前票價</span><strong>{current_html}</strong>{gap_html}</div>'
        f'<div><span>目標價</span><strong>{target_html}</strong></div></div></article>'
    )


__all__ = [
    "badge",
    "empty_state",
    "flight_pair_card",
    "metric_card",
    "monitor_card",
    "page_header",
    "quota_meter",
    "run_status",
    "section_heading",
    "status_banner",
    "status_card",
    "stepper",
]
