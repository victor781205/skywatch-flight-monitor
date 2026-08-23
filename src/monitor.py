"""背景監控：檢查所有 active monitors，降價則通知"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import database as db
from .airports import get_label
from .api_usage import maybe_send_quota_warnings
from .config import (
    ALWAYS_NOTIFY,
    DATA_DIR,
    MONITOR_FETCH_DETAILS_ON_HIT,
    NOTIFY_COOLDOWN_HOURS,
    SERPAPI_MONTHLY_LIMIT,
)
from .flight_api import (
    FlightAPIError,
    dump_result_json,
    get_booking_provider,
    get_return_flight_info,
    search_flights,
)
from .notifier import DUPLICATE_SUPPRESSED, enqueue_all, send_all, send_all_reliable
from .policies import (
    AirlineCode,
    AirlineRuleSet,
    Itinerary,
    NotificationPolicy,
    QuotaPolicy,
    normalize_airline,
)
from .runtime_lock import maintenance_active


def clean_time(t_str: str) -> str:
    """去除 '2026-09-01 13:55' 中的年月日，僅保留 '13:55'"""
    if not t_str:
        return ""
    parts = t_str.strip().split()
    return parts[-1] if len(parts) > 1 else t_str


class _RequestBudget:
    """Reserve every attempted SerpAPI request before it leaves this process."""

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def reserve(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _notification_claim_scope(
    *,
    scheduled_report: bool,
    run_id: Optional[str],
    scheduled_slot: Optional[str] = None,
    now: datetime,
    message: str = "",
) -> str:
    """Return a durable scheduled scope or an exact-payload manual scope."""
    if scheduled_report:
        scheduled_identity = scheduled_slot or run_id or now.strftime("%Y-%m-%d-%H-%M")
        return f"scheduled:{scheduled_identity}"
    if message.strip():
        payload = hashlib.sha256(message.strip().encode("utf-8")).hexdigest()
        return f"manual-payload:{payload}"
    if run_id:
        return f"manual:{run_id}"
    window_seconds = max(300, int(NOTIFY_COOLDOWN_HOURS * 3600))
    window_id = int(now.timestamp() // window_seconds)
    return f"window:{window_id}"


def _deferred_delivery(
    monitor: Dict[str, Any],
    policy: NotificationPolicy,
    *,
    now: datetime,
    quiet: bool,
    digest: bool = False,
) -> tuple[datetime, str]:
    """Choose a durable delivery instant and stable dedupe scope."""
    raw = str(monitor.get("next_run_at") or "").strip()
    scheduled_due = None
    if raw:
        try:
            scheduled_due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            zone = ZoneInfo(policy.timezone)
            if scheduled_due.tzinfo is None:
                scheduled_due = scheduled_due.replace(tzinfo=zone)
            else:
                scheduled_due = scheduled_due.astimezone(zone)
        except ValueError:
            scheduled_due = None
    if quiet:
        due = policy.next_allowed_time(now)
    else:
        due = scheduled_due
        if due is None or due <= now:
            due = now + timedelta(days=1)
        due = policy.next_allowed_time(due)
    scope_moment = scheduled_due if digest and scheduled_due is not None else due
    scope = scope_moment.isoformat(timespec="minutes")
    # Database timestamps elsewhere are local-naive. Preserve that ordering
    # convention while retaining the offset in the dedupe scope above.
    return due.replace(tzinfo=None), scope


def _check_one_monitor_impl(
    monitor: Dict[str, Any],
    save_history: bool = True,
    scheduled_report: bool = False,
    run_id: Optional[str] = None,
    delivery_scope: Optional[str] = None,
    lease_token: Optional[str] = None,
) -> Dict[str, Any]:
    """檢查單一監控項目，回傳結果摘要"""
    if maintenance_active(DATA_DIR):
        return {
            "monitor_id": int(monitor.get("id") or 0),
            "run_id": run_id,
            "ok": False,
            "skipped": True,
            "error": "系統正在維護或復原，已安全取消查價",
            "error_stage": "maintenance",
            "notified": False,
            "skipped_notify": True,
            "notify_reason": "maintenance_active",
            "quota_calls_used": 0,
        }
    origin = monitor["origin"]
    destination = monitor["destination"]
    outbound_date = monitor["outbound_date"]
    return_date = monitor.get("return_date") or None
    target = float(monitor["target_price"])
    adults = int(monitor.get("adults") or 1)
    travel_class = int(monitor.get("travel_class") or 1)
    monitor_id = int(monitor["id"])
    route_id = int(monitor["route_id"])
    quota_mode = str(monitor.get("quota_mode") or "full").lower()
    quota_max_calls = {"lean": 1, "standard": 2, "full": 5}.get(quota_mode, 5)
    request_budget = _RequestBudget(quota_max_calls)
    notification_policy = str(monitor.get("notification_policy") or "always").lower()

    def assert_owner() -> None:
        if run_id and lease_token:
            db.assert_monitor_run_lease(monitor_id, run_id, lease_token)

    ownership = (
        {
            "monitor_id": monitor_id,
            "run_id": run_id,
            "lease_token": lease_token,
        }
        if run_id and lease_token
        else {}
    )

    try:
        usage = db.get_api_usage_summary("serpapi", SERPAPI_MONTHLY_LIMIT)
        quota_guard = QuotaPolicy(
            mode=quota_mode,
            monthly_limit=SERPAPI_MONTHLY_LIMIT,
        ).guard(int(usage["attempted"]), requested=quota_max_calls)
    except Exception:
        return {
            "monitor_id": monitor_id,
            "run_id": run_id,
            "ok": False,
            "skipped": True,
            "error": "無法讀取 API 額度帳本，已安全取消查價",
            "error_stage": "quota_ledger",
            "notified": False,
            "skipped_notify": True,
            "notify_reason": "quota_ledger_unavailable",
            "quota_mode": quota_mode,
            "quota_max_calls": quota_max_calls,
            "quota_calls_used": 0,
        }
    if quota_guard is not None and not quota_guard.allowed:
        return {
            "monitor_id": monitor_id,
            "run_id": run_id,
            "ok": False,
            "skipped": True,
            "error": "本月 SerpAPI 額度不足，已安全略過本次查價",
            "error_stage": "quota_guard",
            "notified": False,
            "skipped_notify": True,
            "notify_reason": quota_guard.reason,
            "quota_mode": quota_mode,
            "quota_max_calls": quota_max_calls,
            "quota_calls_used": request_budget.used,
            "quota_guard": quota_guard.to_dict(),
        }

    # The primary fare query is mandatory and consumes the first request in
    # every mode. Follow-up return/booking lookups must reserve from the same
    # local budget so the advertised per-run cap remains a true hard limit.
    if not request_budget.reserve():
        raise RuntimeError("fare search request budget must allow the primary query")
    try:
        result = search_flights(
            origin=origin,
            destination=destination,
            outbound_date=outbound_date,
            return_date=return_date if return_date else None,
            adults=adults,
            travel_class=travel_class,
            max_results=30 if quota_mode == "full" else 15 if quota_mode == "standard" else 10,
        )
    except FlightAPIError as e:
        assert_owner()
        db.update_monitor_check(
            monitor_id,
            monitor.get("last_price"),
            **(
                {"run_id": run_id, "lease_token": lease_token}
                if run_id and lease_token
                else {}
            ),
        )
        return {
            "monitor_id": monitor_id,
            "ok": False,
            "error": str(e),
            "error_stage": "flight_search",
            "quota_mode": quota_mode,
            "quota_max_calls": quota_max_calls,
            "quota_calls_used": request_budget.used,
            "notified": False,
            "skipped_notify": False,
            "notify_reason": "search_failed",
        }

    best = result.get("best_price")
    assert_owner()
    db.update_monitor_check(
        monitor_id,
        best,
        **(
            {"run_id": run_id, "lease_token": lease_token}
            if run_id and lease_token
            else {}
        ),
    )

    if save_history and best is not None and result.get("flights"):
        top = result["flights"][0]
        db.add_price_record(
            route_id=route_id,
            outbound_date=outbound_date,
            return_date=return_date,
            price=float(best),
            currency=result.get("currency", "TWD"),
            airline=top.get("airline", ""),
            stops=int(top.get("stops") or 0),
            duration=top.get("duration", ""),
            departure_time=top.get("departure_time", ""),
            arrival_time=top.get("arrival_time", ""),
            flight_number=top.get("flight_number", ""),
            raw_json=dump_result_json(result),
            travel_class=travel_class,
            **ownership,
        )

    notified = False
    skipped_notify = False
    notify_reason = "not_required"
    notify_results: List = []

    hit = best is not None and best <= target
    last_price = monitor.get("last_price")
    price_changed = last_price is not None and best is not None and best != last_price

    return_details_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def fetch_return_details(
        flight: Dict[str, Any],
        *,
        target_airline: str,
        keywords: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Enrich one outbound candidate once while respecting the run cap."""
        existing = flight.get("return_flight")
        token = str(flight.get("departure_token") or "").strip()
        if existing:
            if token:
                return_details_cache.setdefault(token, existing)
            return existing
        if not token:
            return None
        if token in return_details_cache:
            return return_details_cache[token]
        if not request_budget.reserve():
            return None
        details = get_return_flight_info(
            token,
            origin,
            destination,
            outbound_date,
            return_date,
            target_airline=target_airline,
            keywords=keywords,
            adults=adults,
            travel_class=travel_class,
        )
        return_details_cache[token] = details
        if details:
            flight["return_flight"] = details
        return details

    carrier_rule_matches: List[Dict[str, Any]] = []
    rule_set = AirlineRuleSet()
    try:
        rules_raw = json.loads(str(monitor.get("airline_rules_json") or "[]"))
        rule_set = AirlineRuleSet.from_dict(rules_raw)

        # Carrier rules may depend on the return airline, stop count, or return
        # departure window.  Enrich the configured carrier candidates before
        # evaluating the policy; otherwise a valid same-airline round trip is
        # incorrectly rejected simply because SerpAPI exposes its return leg
        # behind a departure token.  Standard/full still enforce the same
        # strict 2/5 request caps through ``request_budget``.
        carrier_lookup = {
            AirlineCode.EVA: ("EVA Air", ["EVA", "長榮"]),
            AirlineCode.CHINA_AIRLINES: (
                "China Airlines",
                ["China Airlines", "中華航空", "華航"],
            ),
            AirlineCode.STARLUX: ("STARLUX Airlines", ["STARLUX", "星宇"]),
        }
        can_enrich_carrier_rules = bool(
            rule_set.rules
            and return_date
            and MONITOR_FETCH_DETAILS_ON_HIT
            and quota_mode in {"standard", "full"}
            and result.get("provider") == "serpapi"
        )
        if can_enrich_carrier_rules:
            for rule in rule_set.rules:
                candidate = next(
                    (
                        flight
                        for flight in result.get("flights", [])
                        if normalize_airline(
                            flight.get("airline"), flight.get("flight_number")
                        )
                        == rule.airline
                    ),
                    None,
                )
                if candidate is None:
                    continue
                label, keywords = carrier_lookup[rule.airline]
                fetch_return_details(
                    candidate,
                    target_airline=label,
                    keywords=keywords,
                )

        itineraries = []
        for flight in result.get("flights", []):
            payload = dict(flight)
            payload["adults"] = adults
            payload["currency"] = result.get("currency", "TWD")
            itineraries.append(Itinerary.from_dict(payload))
        for itinerary in itineraries:
            for match in rule_set.evaluate(itinerary):
                carrier_rule_matches.append(match.to_dict())
        hit = hit or any(match.get("target_hit") for match in carrier_rule_matches)
    except (TypeError, ValueError, json.JSONDecodeError):
        carrier_rule_matches = []

    try:
        quiet_raw = json.loads(str(monitor.get("quiet_hours_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        quiet_raw = {}
    notification_rule = NotificationPolicy.from_dict(
        {"strategy": notification_policy, "quiet_hours": quiet_raw or None}
    )
    notification_now = datetime.now(ZoneInfo(notification_rule.timezone))
    decision = notification_rule.evaluate(
        current_price=float(best) if best is not None else None,
        target_price=target,
        previous_price=float(last_price) if last_price is not None else None,
        now=notification_now,
        digest_due=scheduled_report,
        target_hit=hit,
    )
    should_notify = decision.send_now
    if notification_policy == "always" and not ALWAYS_NOTIFY and not scheduled_report and not hit and not price_changed:
        should_notify = False
    if decision.quiet:
        skipped_notify = True
        notify_reason = "quiet_hours"

    if should_notify or decision.queue_digest:
        recent = None if scheduled_report or decision.queue_digest else (
            db.recent_success_notification(monitor_id, hours=NOTIFY_COOLDOWN_HOURS)
            if NOTIFY_COOLDOWN_HOURS > 0
            else None
        )
        if recent:
            skipped_notify = True
            notify_reason = "cooldown"
        else:
            origin_name = get_label(origin)
            dest_name = get_label(destination)

            if hit:
                title = "🔔 機票達標降價提醒！"
            elif price_changed and last_price is not None and best > last_price:
                title = f"📈 機票價格上漲提醒！ (上漲 TWD {best - last_price:,.0f})"
            elif price_changed and last_price is not None and best < last_price:
                title = f"📉 機票價格下跌提醒！ (下跌 TWD {last_price - best:,.0f})"
            else:
                title = "📊 定期查價報告 · 機票最新回報"

            class_name = {1: "經濟艙", 2: "豪華經濟艙", 3: "商務艙", 4: "頭等艙"}.get(travel_class, "經濟艙")
            per_person = f"(單人 TWD {best // adults:,.0f})" if adults > 1 else ""

            premium_airlines = [
                {"code": "BR", "name": "長榮航空", "keywords": ["長榮", "EVA"]},
                {
                    "code": "CI",
                    "name": "中華航空",
                    "keywords": ["中華", "華航", "China Airlines"],
                },
                {"code": "JX", "name": "星宇航空", "keywords": ["星宇", "STARLUX"]},
            ]

            premium_matches: List[tuple[Dict[str, Any], Optional[Dict[str, Any]]]] = []
            for airline_spec in premium_airlines:
                code = str(airline_spec["code"])
                keywords = list(airline_spec["keywords"])
                matched = next(
                    (
                        flight
                        for flight in result.get("flights", [])
                        if any(
                            keyword.lower() in str(flight.get("airline") or "").lower()
                            for keyword in keywords
                        )
                        or str(flight.get("flight_number") or "")
                        .upper()
                        .replace(" ", "")
                        .startswith(code)
                    ),
                    None,
                )
                premium_matches.append((airline_spec, matched))

            # 1. 最推薦組合。All follow-up calls share one local budget:
            # top return first, then BR/CI/JX returns, and booking only with
            # whatever remains. A repeated departure token never costs twice.
            top_flight = result["flights"][0] if result.get("flights") else None
            top_airline = top_flight.get("airline", "未知航空") if top_flight else ""
            top_dep_num = top_flight.get("flight_number", "") if top_flight else ""
            top_dep_plane = f" ({top_flight.get('airplane')})" if top_flight and top_flight.get("airplane") else ""
            top_dep_time = clean_time(top_flight.get("departure_time", "")) if top_flight else ""
            top_arr_time = clean_time(top_flight.get("arrival_time", "")) if top_flight else ""

            top_ret_str = ""
            top_provider_str = ""
            details_enabled = bool(
                (hit or scheduled_report or rule_set.rules)
                and MONITOR_FETCH_DETAILS_ON_HIT
                and quota_mode in {"standard", "full"}
                and result.get("provider") == "serpapi"
                and return_date
            )

            ret_info = top_flight.get("return_flight") if top_flight else None
            if details_enabled and top_flight:
                ret_info = fetch_return_details(
                    top_flight,
                    target_airline=top_airline,
                )

            # Full mode guarantees the requested national-carrier detail gets
            # the remaining slots before the optional booking-provider lookup.
            if details_enabled and quota_mode == "full":
                for airline_spec, matched in premium_matches:
                    if matched:
                        fetch_return_details(
                            matched,
                            target_airline=str(airline_spec["name"]),
                            keywords=list(airline_spec["keywords"]),
                        )

            if ret_info:
                r_air = ret_info.get("airline", "")
                r_num = ret_info.get("flight_number", "")
                r_plane = f" ({ret_info.get('airplane')})" if ret_info.get("airplane") else ""
                r_dep = clean_time(ret_info.get("departure_time", ""))
                r_arr = clean_time(ret_info.get("arrival_time", ""))
                top_ret_str = f"   回程：{r_air} {r_num}{r_plane} {r_dep} - {r_arr}\n"

                b_token = (
                    ret_info.get("booking_token")
                    if hit and details_enabled and quota_mode == "full"
                    else None
                )
                if b_token and request_budget.reserve():
                    booking_provider = get_booking_provider(
                        b_token,
                        origin,
                        destination,
                        outbound_date,
                        return_date,
                        adults=adults,
                        travel_class=travel_class,
                    )
                    if booking_provider:
                        top_provider_str = f"   🎫 開票渠道：{booking_provider}\n"

            top_msg = (
                f"🌟 【最推薦組合】\n"
                f"{'來回總價' if return_date else '單程總價'}：TWD {best:,.0f} {per_person}\n"
                f"   去程：{top_airline} {top_dep_num}{top_dep_plane} {top_dep_time} - {top_arr_time}\n"
                f"{top_ret_str}"
                f"{top_provider_str}"
            )

            pure_blocks = []
            if result.get("flights"):
                for pa, matched in premium_matches:
                    name = pa["name"]
                    if matched:
                        p_total = matched.get("price")
                        p_person_str = f"(單人 TWD {p_total // adults:,.0f})" if adults > 1 else ""
                        f_num = matched.get("flight_number", "")
                        f_plane = f" ({matched.get('airplane')})" if matched.get("airplane") else ""
                        dep_t = clean_time(matched.get("departure_time", ""))
                        arr_t = clean_time(matched.get("arrival_time", ""))
                        block = (
                            f"✈️ {name}\n"
                            f"{'來回總價' if return_date else '單程總價'}：TWD {p_total:,.0f} {p_person_str}\n"
                            f"   去程：{f_num}{f_plane} {dep_t} - {arr_t}\n"
                        )
                        matched_return = matched.get("return_flight")
                        if matched_return:
                            r_num = matched_return.get("flight_number", "")
                            r_dep = clean_time(matched_return.get("departure_time", ""))
                            r_arr = clean_time(matched_return.get("arrival_time", ""))
                            block += f"   回程：{r_num} {r_dep} - {r_arr}\n"
                        elif return_date:
                            block += "   回程：目前查無同航空公司組合\n"
                        pure_blocks.append(block)
                    else:
                        pure_blocks.append(f"✈️ {name}：未找到同航空公司來回組合\n")

            msg = (
                f"{title}\n"
                + (f"排程回報：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n" if scheduled_report else "")
                + f"路線：{origin_name} → {dest_name}\n"
                f"日期：{outbound_date}" + (f" / 回程 {return_date}" if return_date else "") + "\n"
                f"條件：{adults} 位成人 · {class_name}\n"
            )
            if last_price:
                msg += f"上次紀錄價：TWD {last_price:,.0f} | "
            msg += f"目標價：TWD {target:,.0f}\n"
            carrier_hits = [
                str(match.get("airline"))
                for match in carrier_rule_matches
                if match.get("target_hit")
            ]
            if carrier_hits:
                msg += f"航空公司專屬目標已達標：{', '.join(dict.fromkeys(carrier_hits))}\n"
            msg += "\n"
            msg += f"{top_msg}\n"
            msg += "🏆 【三大國籍航空】\n\n"
            msg += "\n".join(pure_blocks) + "\n\n"
            source_name = "Google Flights / SerpAPI"
            if not hit:
                if scheduled_report and return_date:
                    msg += "固定回報已補查最推薦與國籍航空回程；開票平台達標時才查。\n"
                else:
                    msg += "額度節省模式：固定排程或達標時才補查回程。\n"
            msg += f"資料來源：{source_name}。下單前請再次確認最終價格。"

            # Return/booking enrichment above may have yielded the process for
            # minutes. Revalidate ownership immediately before any durable
            # notification is created or sent.
            assert_owner()

            if decision.queue_digest:
                due, deferred_scope = _deferred_delivery(
                    monitor,
                    notification_rule,
                    now=notification_now,
                    quiet=decision.quiet,
                    digest=notification_policy == "digest",
                )
                queued = enqueue_all(
                    msg,
                    monitor_id=monitor_id,
                    run_id=run_id,
                    purpose="digest" if notification_policy == "digest" else "quiet_hours",
                    dedupe_scope=deferred_scope,
                    next_attempt_at=due,
                    replace_pending=True,
                    run_lease_token=lease_token,
                )
                notify_results = [
                    (
                        str(item.get("channel") or "local"),
                        False,
                        "queued_for_quiet_end" if decision.quiet else "queued_for_digest",
                    )
                    for item in queued
                ]
                skipped_notify = True
                notify_reason = "quiet_hours_queued" if decision.quiet else "digest_queued"
            else:
                # Prevent two scheduler processes from delivering the same report at once.
                # The durable run id distinguishes multiple valid slots within
                # one hour; slot-ledger idempotency still blocks duplicates.
                claim_scope = _notification_claim_scope(
                    scheduled_report=scheduled_report,
                    run_id=run_id,
                    scheduled_slot=delivery_scope,
                    now=notification_now,
                    message=msg,
                )
                dedupe_key = hashlib.sha256(
                    f"monitor:{monitor_id}:{claim_scope}".encode("utf-8")
                ).hexdigest()
                if not db.claim_notification(
                    monitor_id,
                    dedupe_key,
                    ttl_hours=max(24.0, NOTIFY_COOLDOWN_HOURS * 2),
                ):
                    skipped_notify = True
                    notify_reason = "deduplicated"
                else:
                    if notification_policy == "digest" and run_id:
                        _, digest_scope = _deferred_delivery(
                            monitor,
                            notification_rule,
                            now=notification_now,
                            quiet=False,
                            digest=True,
                        )
                        notify_results = send_all_reliable(
                            msg,
                            monitor_id=monitor_id,
                            run_id=run_id,
                            purpose="digest",
                            dedupe_scope=digest_scope,
                            next_attempt_at=notification_now.replace(tzinfo=None),
                            replace_pending=True,
                            run_lease_token=lease_token,
                        )
                    else:
                        notify_results = (
                            send_all_reliable(
                                msg,
                                monitor_id=monitor_id,
                                run_id=run_id,
                                dedupe_scope=claim_scope,
                                run_lease_token=lease_token,
                            )
                            if run_id
                            else send_all(msg, monitor_id=monitor_id)
                        )
                    duplicate_suppressed = any(
                        detail == DUPLICATE_SUPPRESSED for _, _, detail in notify_results
                    )
                    notified = any(ok for _, ok, _ in notify_results)
                    skipped_notify = duplicate_suppressed and not notified
                    notify_reason = (
                        "sent" if notified else "deduplicated" if duplicate_suppressed else "delivery_failed"
                    )
                    if not notified and not run_id:
                        db.release_notification_claim(dedupe_key)

    quote_candidates: List[Dict[str, Any]] = []
    seen_quote_ids = set()
    all_flights = list(result.get("flights", []))
    # Keep the cheapest set plus the first BR/CI/JX candidate. Otherwise a
    # top-five slice can discard a requested full-service carrier when several
    # low-cost itineraries are cheaper.
    for flight in all_flights[:5]:
        identity = id(flight)
        quote_candidates.append(flight)
        seen_quote_ids.add(identity)
    for code in ("BR", "CI", "JX"):
        for flight in all_flights:
            airline = str(flight.get("airline") or "")
            upper = airline.upper()
            number = str(flight.get("flight_number") or "").upper().replace(" ", "")
            matches = (
                (code == "BR" and ("EVA" in upper or "長榮" in airline or number.startswith("BR")))
                or (code == "CI" and ("CHINA AIRLINES" in upper or "中華" in airline or "華航" in airline or number.startswith("CI")))
                or (code == "JX" and ("STARLUX" in upper or "星宇" in airline or number.startswith("JX")))
            )
            identity = id(flight)
            if matches and identity not in seen_quote_ids:
                quote_candidates.append(flight)
                seen_quote_ids.add(identity)
                break

    quotes = []
    for flight in quote_candidates:
        airline = str(flight.get("airline") or "")
        upper = airline.upper()
        number = str(flight.get("flight_number") or "").upper().replace(" ", "")
        carrier_code = (
            "BR" if "EVA" in upper or "長榮" in airline or number.startswith("BR")
            else "CI" if "CHINA AIRLINES" in upper or "中華" in airline or "華航" in airline or number.startswith("CI")
            else "JX" if "STARLUX" in upper or "星宇" in airline or number.startswith("JX")
            else ""
        )
        quotes.append(
            {
                "carrier_code": carrier_code,
                "carrier_name": airline,
                "total_price": flight.get("price"),
                "currency": result.get("currency", "TWD"),
                "stops": flight.get("stops", 0),
                "outbound": {
                    key: flight.get(key)
                    for key in (
                        "flight_number", "departure_time", "arrival_time", "duration",
                        "airplane", "stops",
                    )
                },
                "return": flight.get("return_flight") or {},
            }
        )
    try:
        assert_owner()
        db.save_carrier_quotes(
            route_id=route_id,
            monitor_id=monitor_id,
            run_id=run_id,
            quotes=quotes,
            run_lease_token=lease_token,
        )
    except Exception:
        # Price checks must still return if an old/test database has not yet
        # been migrated; the next normal startup runs init_db and restores the ledger.
        pass

    return {
        "monitor_id": monitor_id,
        "run_id": run_id,
        "quota_mode": quota_mode,
        "quota_max_calls": quota_max_calls,
        "quota_calls_used": request_budget.used,
        "quota_guard": quota_guard.to_dict() if quota_guard is not None else None,
        "carrier_rule_matches": carrier_rule_matches,
        "ok": True,
        "best_price": best,
        "target_price": target,
        "hit": hit,
        "notified": notified,
        "skipped_notify": skipped_notify,
        "notify_reason": notify_reason,
        "notify_results": notify_results,
        "flights": result.get("flights", [])[:5],
    }


def check_one_monitor(
    monitor: Dict[str, Any],
    save_history: bool = True,
    scheduled_report: bool = False,
    *,
    trigger: str = "scheduled",
    scheduled_slot: Optional[str] = None,
    backfill: bool = False,
) -> Dict[str, Any]:
    """Run one monitor with an idempotent, query-counted audit ledger."""
    monitor_id = int(monitor["id"])
    if maintenance_active(DATA_DIR):
        return {
            "monitor_id": monitor_id,
            "ok": False,
            "skipped": True,
            "error": "系統正在維護或復原，已安全取消查價",
            "error_stage": "maintenance",
            "notified": False,
            "skipped_notify": True,
            "notify_reason": "maintenance_active",
            "quota_calls_used": 0,
        }
    try:
        run = db.create_check_run(
            monitor_id,
            trigger=trigger,
            scheduled_slot=scheduled_slot,
            backfill=backfill,
        )
    except Exception as exc:
        # Ledger creation is a safety boundary. Never spend provider quota
        # when ownership cannot be recorded durably.
        return {
            "monitor_id": monitor_id,
            "ok": False,
            "skipped": True,
            "error": f"無法建立查價執行帳本：{type(exc).__name__}",
            "error_stage": "run_ledger",
            "notified": False,
            "skipped_notify": True,
            "notify_reason": "run_ledger_unavailable",
            "quota_calls_used": 0,
        }
    if not run.get("created"):
        return {
            "monitor_id": monitor_id,
            "run_id": run["id"],
            "ok": True,
            "notified": False,
            "skipped_notify": True,
            "notify_reason": "slot_already_processed",
            "ledger_status": run.get("status"),
        }

    run_id = str(run["id"])
    before = db.get_api_usage_summary("serpapi", SERPAPI_MONTHLY_LIMIT)["attempted"]
    try:
        result = _check_one_monitor_impl(
            monitor,
            save_history=save_history,
            scheduled_report=scheduled_report,
            run_id=run_id,
            delivery_scope=scheduled_slot,
            lease_token=str(run.get("lease_token") or ""),
        )
        after = db.get_api_usage_summary("serpapi", SERPAPI_MONTHLY_LIMIT)["attempted"]
        api_calls = max(0, int(after) - int(before))
        result["api_calls"] = api_calls
        expected_deferred = {
            DUPLICATE_SUPPRESSED,
            "queued_for_quiet_end",
            "queued_for_digest",
        }
        delivery_failed = result.get("notify_reason") == "delivery_failed" or any(
            not delivered and detail not in expected_deferred
            for _, delivered, detail in result.get("notify_results") or []
        )
        status = (
            "skipped"
            if result.get("skipped")
            else "backfilled"
            if result.get("ok") and backfill
            else "partial"
            if result.get("ok") and delivery_failed
            else "succeeded"
            if result.get("ok")
            else "failed"
        )
        error_stage = (
            str(result.get("error_stage") or "quota_guard")
            if result.get("skipped")
            else "notification"
            if status == "partial"
            else ""
            if result.get("ok")
            else str(result.get("error_stage") or "flight_search")
        )
        db.finish_check_run(
            run_id,
            status=status,
            api_calls=api_calls,
            error_stage=error_stage,
            error_message=str(result.get("error") or ""),
            result={
                "best_price": result.get("best_price"),
                "target_price": result.get("target_price"),
                "hit": result.get("hit"),
                "notified": result.get("notified"),
                "notify_reason": result.get("notify_reason"),
            },
            lease_token=str(run.get("lease_token") or ""),
        )
        return result
    except Exception as exc:
        after = db.get_api_usage_summary("serpapi", SERPAPI_MONTHLY_LIMIT)["attempted"]
        try:
            db.finish_check_run(
                run_id,
                status="failed",
                api_calls=max(0, int(after) - int(before)),
                error_stage="monitor_pipeline",
                error_message=str(exc),
                lease_token=str(run.get("lease_token") or ""),
            )
        except (PermissionError, RuntimeError):
            # Pause/delete/policy changes deliberately revoke ownership. The
            # transaction that revoked it already closed the run and cancelled
            # queued notifications; the stale worker must not overwrite that.
            pass
        raise


def check_all_monitors(
    scheduled_report: bool = False,
    *,
    scheduled_slot: Optional[str] = None,
    backfill: bool = False,
    monitor_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    db.expire_monitors()  # 自動停用已過出發日的監控，節省 API 額度
    monitors = db.list_monitors(active_only=True)
    if monitor_ids is not None:
        selected = {int(item) for item in monitor_ids}
        monitors = [monitor for monitor in monitors if int(monitor["id"]) in selected]
    results = [
        check_one_monitor(
            monitor,
            scheduled_report=scheduled_report,
            trigger="backfill" if backfill else "scheduled" if scheduled_report else "manual",
            scheduled_slot=scheduled_slot,
            backfill=backfill,
        )
        for monitor in monitors
    ]
    maybe_send_quota_warnings(send_all)
    return results


def search_and_save(
    origin: str,
    destination: str,
    outbound_date: str,
    return_date: Optional[str] = None,
    adults: int = 1,
    travel_class: int = 1,
) -> Dict[str, Any]:
    """儀表板即時查詢並寫入歷史"""
    if maintenance_active(DATA_DIR):
        raise FlightAPIError("系統正在維護或復原，已安全取消查價")
    origin_name = get_label(origin.upper())
    dest_name = get_label(destination.upper())
    route_id = db.upsert_route(origin, destination, origin_name, dest_name)

    result = search_flights(
        origin=origin,
        destination=destination,
        outbound_date=outbound_date,
        return_date=return_date,
        adults=adults,
        travel_class=travel_class,
    )

    if result.get("best_price") is not None and result.get("flights"):
        top = result["flights"][0]
        db.add_price_record(
            route_id=route_id,
            outbound_date=outbound_date,
            return_date=return_date,
            price=float(result["best_price"]),
            currency=result.get("currency", "TWD"),
            airline=top.get("airline", ""),
            stops=int(top.get("stops") or 0),
            duration=top.get("duration", ""),
            departure_time=top.get("departure_time", ""),
            arrival_time=top.get("arrival_time", ""),
            flight_number=top.get("flight_number", ""),
            raw_json=dump_result_json(result),
            travel_class=travel_class,
        )

    result["route_id"] = route_id
    result["adults"] = adults
    result["travel_class"] = travel_class
    return result
