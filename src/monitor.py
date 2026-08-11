"""背景監控：檢查所有 active monitors，降價則通知"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import database as db
from .api_usage import maybe_send_quota_warnings
from .airports import get_label
from .config import ALWAYS_NOTIFY, MONITOR_FETCH_DETAILS_ON_HIT, NOTIFY_COOLDOWN_HOURS
from .flight_api import (
    FlightAPIError,
    dump_result_json,
    format_flight_summary,
    get_booking_provider,
    get_return_flight_info,
    search_flights,
)
from .notifier import DUPLICATE_SUPPRESSED, send_all


def clean_time(t_str: str) -> str:
    """去除 '2026-09-01 13:55' 中的年月日，僅保留 '13:55'"""
    if not t_str:
        return ""
    parts = t_str.strip().split()
    return parts[-1] if len(parts) > 1 else t_str


def check_one_monitor(monitor: Dict[str, Any], save_history: bool = True) -> Dict[str, Any]:
    """檢查單一監控項目，回傳結果摘要"""
    origin = monitor["origin"]
    destination = monitor["destination"]
    outbound_date = monitor["outbound_date"]
    return_date = monitor.get("return_date") or None
    target = float(monitor["target_price"])
    adults = int(monitor.get("adults") or 1)
    travel_class = int(monitor.get("travel_class") or 1)
    monitor_id = int(monitor["id"])
    route_id = int(monitor["route_id"])

    try:
        result = search_flights(
            origin=origin,
            destination=destination,
            outbound_date=outbound_date,
            return_date=return_date if return_date else None,
            adults=adults,
            travel_class=travel_class,
        )
    except FlightAPIError as e:
        db.update_monitor_check(monitor_id, monitor.get("last_price"))
        return {
            "monitor_id": monitor_id,
            "ok": False,
            "error": str(e),
            "notified": False,
            "skipped_notify": False,
            "notify_reason": "search_failed",
        }

    best = result.get("best_price")
    db.update_monitor_check(monitor_id, best)

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
        )

    notified = False
    skipped_notify = False
    notify_reason = "not_required"
    notify_results: List = []

    hit = best is not None and best <= target
    last_price = monitor.get("last_price")
    price_changed = last_price is not None and best is not None and best != last_price

    # 只要設定 ALWAYS_NOTIFY=True，或者達標、或價格變動，就發送通知
    should_notify = ALWAYS_NOTIFY or hit or price_changed

    if should_notify:
        recent = (
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

            # 1. 最推薦組合
            top_flight = result["flights"][0] if result.get("flights") else None
            top_airline = top_flight.get("airline", "未知航空") if top_flight else ""
            top_dep_num = top_flight.get("flight_number", "") if top_flight else ""
            top_dep_plane = f" ({top_flight.get('airplane')})" if top_flight and top_flight.get("airplane") else ""
            top_dep_time = clean_time(top_flight.get("departure_time", "")) if top_flight else ""
            top_arr_time = clean_time(top_flight.get("arrival_time", "")) if top_flight else ""

            top_ret_str = ""
            top_provider_str = ""
            ret_info = top_flight.get("return_flight") if top_flight else None
            should_fetch_details = bool(
                hit
                and MONITOR_FETCH_DETAILS_ON_HIT
                and result.get("provider") == "serpapi"
                and return_date
                and top_flight
                and top_flight.get("departure_token")
            )
            if should_fetch_details:
                ret_info = get_return_flight_info(
                    top_flight["departure_token"],
                    origin,
                    destination,
                    outbound_date,
                    return_date,
                    adults=adults,
                    travel_class=travel_class,
                )
            if ret_info:
                r_air = ret_info.get("airline", "")
                r_num = ret_info.get("flight_number", "")
                r_plane = f" ({ret_info.get('airplane')})" if ret_info.get("airplane") else ""
                r_dep = clean_time(ret_info.get("departure_time", ""))
                r_arr = clean_time(ret_info.get("arrival_time", ""))
                top_ret_str = f"   回程：{r_air} {r_num}{r_plane} {r_dep} - {r_arr}\n"

                b_token = ret_info.get("booking_token") if should_fetch_details else None
                if b_token:
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
                f"總價：TWD {best:,.0f} {per_person}\n"
                f"   去程：{top_airline} {top_dep_num}{top_dep_plane} {top_dep_time} - {top_arr_time}\n"
                f"{top_ret_str}"
                f"{top_provider_str}"
            )

            # 2~4. 三大國籍航空直接使用主搜尋結果，避免排程逐家補查。
            premium_airlines = [
                {"name": "長榮航空", "keywords": ["長榮", "EVA"]},
                {"name": "中華航空", "keywords": ["中華", "華航", "China Airlines"]},
                {"name": "星宇航空", "keywords": ["星宇", "STARLUX"]},
            ]

            pure_blocks = []
            if result.get("flights"):
                for pa in premium_airlines:
                    name = pa["name"]
                    keywords = pa["keywords"]
                    matched = next(
                        (
                            flight
                            for flight in result["flights"]
                            if any(k.lower() in flight.get("airline", "").lower() for k in keywords)
                        ),
                        None,
                    )
                    if matched:
                        p_total = matched.get("price")
                        p_person_str = f"(單人 TWD {p_total // adults:,.0f})" if adults > 1 else ""
                        f_num = matched.get("flight_number", "")
                        f_plane = f" ({matched.get('airplane')})" if matched.get("airplane") else ""
                        dep_t = clean_time(matched.get("departure_time", ""))
                        arr_t = clean_time(matched.get("arrival_time", ""))
                        block = (
                            f"✈️ {name}\n"
                            f"總價：TWD {p_total:,.0f} {p_person_str}\n"
                            f"   去程：{f_num}{f_plane} {dep_t} - {arr_t}\n"
                        )
                        matched_return = matched.get("return_flight")
                        if matched_return:
                            r_num = matched_return.get("flight_number", "")
                            r_dep = clean_time(matched_return.get("departure_time", ""))
                            r_arr = clean_time(matched_return.get("arrival_time", ""))
                            block += f"   回程：{r_num} {r_dep} - {r_arr}\n"
                        pure_blocks.append(block)
                    else:
                        pure_blocks.append(f"✈️ {name}：未找到同航空公司來回組合\n")

            msg = (
                f"{title}\n"
                f"路線：{origin_name} → {dest_name}\n"
                f"日期：{outbound_date}" + (f" / 回程 {return_date}" if return_date else "") + "\n"
                f"條件：{adults} 位成人 · {class_name}\n"
            )
            if last_price:
                msg += f"上次紀錄價：TWD {last_price:,.0f} | "
            msg += f"目標價：TWD {target:,.0f}\n\n"
            msg += f"{top_msg}\n"
            msg += f"🏆 【三大國籍航空】\n\n"
            msg += "\n".join(pure_blocks) + "\n\n"
            source_name = "Google Flights / SerpAPI"
            if not hit:
                msg += "額度節省模式：達標時才補查回程與開票平台。\n"
            msg += f"資料來源：{source_name}。下單前請再次確認最終價格。"

            # Prevent two scheduler processes from delivering the same report at once.
            window_seconds = max(300, int(NOTIFY_COOLDOWN_HOURS * 3600))
            window_id = int(datetime.now().timestamp() // window_seconds)
            dedupe_key = hashlib.sha256(
                f"monitor:{monitor_id}:window:{window_id}".encode("utf-8")
            ).hexdigest()
            if not db.claim_notification(
                monitor_id,
                dedupe_key,
                ttl_hours=max(24.0, NOTIFY_COOLDOWN_HOURS * 2),
            ):
                skipped_notify = True
                notify_reason = "deduplicated"
            else:
                notify_results = send_all(msg, monitor_id=monitor_id)
                duplicate_suppressed = any(
                    detail == DUPLICATE_SUPPRESSED for _, _, detail in notify_results
                )
                notified = any(ok for _, ok, _ in notify_results)
                skipped_notify = duplicate_suppressed and not notified
                notify_reason = (
                    "sent" if notified else "deduplicated" if duplicate_suppressed else "delivery_failed"
                )
                if not notified:
                    db.release_notification_claim(dedupe_key)

    return {
        "monitor_id": monitor_id,
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


def check_all_monitors() -> List[Dict[str, Any]]:
    db.expire_monitors()  # 自動停用已過出發日的監控，節省 API 額度
    monitors = db.list_monitors(active_only=True)
    results = [check_one_monitor(m) for m in monitors]
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
