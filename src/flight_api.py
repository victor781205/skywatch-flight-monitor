"""Quota-aware Google Flights search through SerpAPI."""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, List, Optional

import requests

from . import database as db
from .config import SERPAPI_API_KEY


class FlightAPIError(Exception):
    pass


def _record_usage(provider: str, operation: str, success: bool, detail: str = "") -> None:
    try:
        db.record_api_usage(provider, operation, success=success, detail=detail)
    except Exception:
        # Usage telemetry must never break fare searches.
        pass


def _validate_search(
    origin: str,
    destination: str,
    outbound_date: str,
    return_date: Optional[str],
    adults: int,
    travel_class: int,
) -> None:
    if len(origin) != 3 or not origin.isalpha():
        raise FlightAPIError("出發機場必須是 3 碼 IATA 代碼")
    if len(destination) != 3 or not destination.isalpha():
        raise FlightAPIError("目的機場必須是 3 碼 IATA 代碼")
    if origin == destination:
        raise FlightAPIError("出發地與目的地不可相同")
    try:
        outbound = date.fromisoformat(outbound_date)
        returning = date.fromisoformat(return_date) if return_date else None
    except ValueError as exc:
        raise FlightAPIError("日期格式必須為 YYYY-MM-DD") from exc
    if returning and returning < outbound:
        raise FlightAPIError("回程日期不可早於出發日期")
    if not 1 <= int(adults) <= 9:
        raise FlightAPIError("成人數必須介於 1 到 9")
    if int(travel_class) not in (1, 2, 3, 4):
        raise FlightAPIError("不支援的艙等")


def search_flights(
    origin: str,
    destination: str,
    outbound_date: str,
    return_date: Optional[str] = None,
    adults: int = 1,
    travel_class: int = 1,
    currency: str = "TWD",
    hl: str = "zh-tw",
    gl: str = "tw",
    max_results: int = 10,
) -> Dict[str, Any]:
    """Validate and query SerpAPI once for a route."""
    origin = origin.upper().strip()
    destination = destination.upper().strip()
    _validate_search(origin, destination, outbound_date, return_date, adults, travel_class)
    if not SERPAPI_API_KEY:
        raise FlightAPIError("未設定 SERPAPI_API_KEY，請在 .env 填入金鑰")
    """
    查詢 Google Flights（經 SerpAPI）。
    outbound_date / return_date 格式: YYYY-MM-DD
    travel_class: 1=經濟, 2=豪經, 3=商務, 4=頭等
    """
    params: Dict[str, Any] = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": outbound_date,
        "currency": currency,
        "hl": hl,
        "gl": gl,
        "adults": adults,
        "travel_class": travel_class,
        "api_key": SERPAPI_API_KEY,
        "type": 1 if return_date else 2,  # 1=來回, 2=單程
    }
    if return_date:
        params["return_date"] = return_date

    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except requests.Timeout as exc:
        _record_usage("serpapi", "flight_search", False, "timeout")
        raise FlightAPIError("SerpAPI 回應逾時，請稍後再試") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "未知"
        _record_usage("serpapi", "flight_search", False, f"http_{status}")
        raise FlightAPIError(f"SerpAPI 回應失敗（HTTP {status}）") from exc
    except requests.RequestException as exc:
        _record_usage("serpapi", "flight_search", False, "connection")
        raise FlightAPIError("SerpAPI 連線失敗，請檢查網路後再試") from exc
    except ValueError as exc:
        _record_usage("serpapi", "flight_search", False, "invalid_json")
        raise FlightAPIError("SerpAPI 回傳了無法解析的資料") from exc

    if "error" in data:
        _record_usage("serpapi", "flight_search", False, str(data["error"]))
        raise FlightAPIError(str(data["error"]))

    _record_usage("serpapi", "flight_search", True)

    flights = _parse_flights(data, max_results=max_results)
    best_price = flights[0]["price"] if flights else None

    return {
        "origin": origin,
        "destination": destination,
        "outbound_date": outbound_date,
        "return_date": return_date,
        "currency": currency,
        "best_price": best_price,
        "flights": flights,
        "raw": data,
        "provider": "serpapi",
    }


def get_return_flight_info(
    departure_token: str,
    origin: str,
    dest: str,
    outbound_date: str,
    return_date: str,
    target_airline: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    adults: int = 1,
    travel_class: int = 1,
    currency: str = "TWD",
    hl: str = "zh-tw",
    gl: str = "tw",
) -> Optional[Dict[str, Any]]:
    """使用 departure_token 查詢回程航班資訊（限來回機票的第二段）"""
    if not SERPAPI_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://serpapi.com/search",
            params={
                "engine": "google_flights",
                "departure_token": departure_token,
                "departure_id": origin,
                "arrival_id": dest,
                "outbound_date": outbound_date,
                "return_date": return_date,
                "adults": adults,
                "travel_class": travel_class,
                "currency": currency,
                "hl": hl,
                "gl": gl,
                "api_key": SERPAPI_API_KEY,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            _record_usage("serpapi", "return_details", False, str(data["error"]))
            return None
        _record_usage("serpapi", "return_details", True)
        flights = _parse_flights(data, max_results=30)
        if flights:
            if keywords:
                for f in flights:
                    f_air = f.get("airline", "").lower()
                    if any(k.lower() in f_air for k in keywords):
                        return f
            elif target_airline:
                for f in flights:
                    f_air = f.get("airline", "").lower()
                    if target_airline.lower() in f_air or f_air in target_airline.lower():
                        return f
            return flights[0]
    except Exception as exc:
        _record_usage("serpapi", "return_details", False, type(exc).__name__)
    return None


def _parse_flights(data: Dict[str, Any], max_results: int = 10) -> List[Dict[str, Any]]:
    """解析 best_flights + other_flights"""
    items: List[Dict[str, Any]] = []
    for key in ("best_flights", "other_flights"):
        for item in data.get(key) or []:
            parsed = _parse_one_flight(item)
            if parsed:
                items.append(parsed)

    # 去重並依價格排序
    seen = set()
    unique: List[Dict[str, Any]] = []
    for f in items:
        sig = (f.get("price"), f.get("airline"), f.get("departure_time"), f.get("stops"))
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(f)

    unique.sort(key=lambda x: (x.get("price") is None, x.get("price") or 10**12))
    return unique[:max_results]


def _parse_one_flight(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    flights = item.get("flights") or []
    if not flights:
        return None

    first = flights[0]
    last = flights[-1]
    price = item.get("price")
    if price is None:
        return None

    airlines = []
    flight_numbers = []
    airplanes = []
    for leg in flights:
        name = leg.get("airline") or ""
        if name and name not in airlines:
            airlines.append(name)
        num = leg.get("flight_number") or ""
        if num:
            flight_numbers.append(num)
        plane = leg.get("airplane") or ""
        if plane and plane not in airplanes:
            airplanes.append(plane)

    dep_airport = (first.get("departure_airport") or {}).get("id") or ""
    arr_airport = (last.get("arrival_airport") or {}).get("id") or ""
    dep_time = (first.get("departure_airport") or {}).get("time") or ""
    arr_time = (last.get("arrival_airport") or {}).get("time") or ""

    total_duration = item.get("total_duration")
    if isinstance(total_duration, int):
        hours, mins = divmod(total_duration, 60)
        duration_str = f"{hours}h {mins}m"
    else:
        duration_str = str(total_duration or "")

    stops = max(0, len(flights) - 1)
    # layovers 也可能提供
    if item.get("layovers"):
        stops = max(stops, len(item.get("layovers") or []))

    return {
        "price": float(price),
        "airline": " / ".join(airlines) if airlines else "未知航空",
        "flight_number": ", ".join(flight_numbers),
        "airplane": " / ".join(airplanes),
        "departure": dep_airport,
        "arrival": arr_airport,
        "departure_time": dep_time,
        "arrival_time": arr_time,
        "stops": stops,
        "duration": duration_str,
        "type": item.get("type") or "",
        "departure_token": item.get("departure_token"),
        "booking_token": item.get("booking_token"),
        "carbon_emissions": (item.get("carbon_emissions") or {}).get("this_flight"),
    }


def get_booking_provider(
    booking_token: str,
    origin: str,
    dest: str,
    outbound_date: str,
    return_date: Optional[str] = None,
    adults: int = 1,
    travel_class: int = 1,
    currency: str = "TWD",
) -> Optional[str]:
    """使用 booking_token 查詢該機票組合的開票平台 (如 Trip.com、長榮官網等)"""
    if not SERPAPI_API_KEY or not booking_token:
        return None
    try:
        params = {
            "engine": "google_flights",
            "booking_token": booking_token,
            "departure_id": origin,
            "arrival_id": dest,
            "outbound_date": outbound_date,
            "adults": adults,
            "travel_class": travel_class,
            "currency": currency,
            "hl": "zh-tw",
            "gl": "tw",
            "api_key": SERPAPI_API_KEY,
        }
        if return_date:
            params["return_date"] = return_date

        resp = requests.get("https://serpapi.com/search", params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            _record_usage("serpapi", "booking_options", False, str(data["error"]))
            return None
        _record_usage("serpapi", "booking_options", True)
        opts = data.get("booking_options") or []
        if opts:
            first_opt = opts[0]
            if first_opt.get("together"):
                return first_opt["together"].get("book_with")
            elif first_opt.get("departing") and first_opt.get("returning"):
                dep_provider = first_opt["departing"].get("book_with", "")
                ret_provider = first_opt["returning"].get("book_with", "")
                return f"{dep_provider} / {ret_provider}"
            elif first_opt.get("departing"):
                return first_opt["departing"].get("book_with")
    except Exception as exc:
        _record_usage("serpapi", "booking_options", False, type(exc).__name__)
    return None


def format_flight_summary(result: Dict[str, Any]) -> str:
    origin = result.get("origin")
    dest = result.get("destination")
    date = result.get("outbound_date")
    ret = result.get("return_date")
    price = result.get("best_price")
    cur = result.get("currency", "TWD")
    trip = f"{date} → {ret}" if ret else date
    if price is None:
        return f"✈️ {origin} → {dest}（{trip}）目前查無價格"
    return f"✈️ {origin} → {dest}（{trip}）最低價：{cur} {price:,.0f}"


def dump_result_json(result: Dict[str, Any]) -> str:
    """存歷史用，去掉過大的 raw"""
    slim = {k: v for k, v in result.items() if k != "raw"}
    return json.dumps(slim, ensure_ascii=False)
