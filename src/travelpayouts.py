"""Travelpayouts cached market context for user-initiated searches.

This adapter intentionally uses the Data API, not the restricted real-time
Search API. Its prices are market references and must never trigger alerts.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Optional

import requests

from .config import TRAVELPAYOUTS_TOKEN


CALENDAR_URL = "https://api.travelpayouts.com/v1/prices/calendar"


class TravelpayoutsError(Exception):
    pass


def configured() -> bool:
    return bool(TRAVELPAYOUTS_TOKEN.strip())


def get_market_reference(
    origin: str,
    destination: str,
    outbound_date: str,
    return_date: Optional[str] = None,
    currency: str = "TWD",
) -> Dict[str, Any]:
    """Return an exact-date cached observation or the month's cached low."""
    if not configured():
        raise TravelpayoutsError("未設定 TRAVELPAYOUTS_TOKEN")

    origin = origin.strip().upper()
    destination = destination.strip().upper()
    try:
        outbound = date.fromisoformat(outbound_date)
        returning = date.fromisoformat(return_date) if return_date else None
    except ValueError as exc:
        raise TravelpayoutsError("日期格式必須為 YYYY-MM-DD") from exc
    if (
        len(origin) != 3
        or len(destination) != 3
        or not origin.isascii()
        or not destination.isascii()
        or not origin.isalpha()
        or not destination.isalpha()
        or origin == destination
    ):
        raise TravelpayoutsError("航線 IATA 代碼無效")
    if returning and returning < outbound:
        raise TravelpayoutsError("回程日期不可早於出發日期")

    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": outbound.strftime("%Y-%m"),
        "calendar_type": "departure_date",
        "currency": currency.lower(),
    }
    if returning:
        params["return_date"] = returning.isoformat()

    try:
        response = requests.get(
            CALENDAR_URL,
            params=params,
            headers={
                "X-Access-Token": TRAVELPAYOUTS_TOKEN,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout as exc:
        raise TravelpayoutsError("Travelpayouts 回應逾時") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "未知"
        raise TravelpayoutsError(f"Travelpayouts 回應失敗（HTTP {status}）") from exc
    except requests.RequestException as exc:
        raise TravelpayoutsError("Travelpayouts 連線失敗") from exc
    except ValueError as exc:
        raise TravelpayoutsError("Travelpayouts 回傳資料無法解析") from exc

    if not isinstance(payload, Mapping):
        raise TravelpayoutsError("Travelpayouts 回傳格式無效")
    if not payload.get("success"):
        raise TravelpayoutsError("Travelpayouts 暫時沒有可用資料")
    observations = payload.get("data")
    if observations is None:
        observations = {}
    if not isinstance(observations, Mapping):
        raise TravelpayoutsError("Travelpayouts 回傳格式無效")
    candidates = []
    for observed_date, item in observations.items():
        if not isinstance(item, dict):
            continue
        try:
            price = float(item["price"])
            observed = date.fromisoformat(observed_date)
        except (KeyError, TypeError, ValueError):
            continue
        if (observed.year, observed.month) != (outbound.year, outbound.month):
            continue
        if price <= 0:
            continue
        candidates.append((observed, price, item))

    if not candidates:
        return {
            "available": False,
            "source": "travelpayouts_data_api",
            "cached": True,
            "currency": currency.upper(),
        }

    exact = next((row for row in candidates if row[0] == outbound), None)
    selected = exact or min(candidates, key=lambda row: row[1])
    observed, price, item = selected
    return {
        "available": True,
        "source": "travelpayouts_data_api",
        "cached": True,
        "match": "exact_date" if exact else "monthly_low",
        "price": price,
        "currency": currency.upper(),
        "departure_date": observed.isoformat(),
        "return_date": item.get("return_at") or return_date,
        "airline": item.get("airline") or "",
        "transfers": item.get("transfers"),
        "expires_at": item.get("expires_at") or "",
        "observations": len(candidates),
    }


def get_low_fare_calendar(
    origin: str,
    destination: str,
    month: str,
    *,
    return_date: Optional[str] = None,
    currency: str = "TWD",
) -> Dict[str, Any]:
    """Return cached daily observations for a flexible-date calendar.

    These observations are trend intelligence only. Callers must make the
    user explicitly verify a selected date with the live SerpAPI search.
    """
    if not configured():
        raise TravelpayoutsError("未設定 TRAVELPAYOUTS_TOKEN")
    origin = origin.strip().upper()
    destination = destination.strip().upper()
    try:
        first_day = date.fromisoformat(f"{month}-01")
        returning = date.fromisoformat(return_date) if return_date else None
    except ValueError as exc:
        raise TravelpayoutsError("月份格式必須為 YYYY-MM") from exc
    if (
        len(origin) != 3
        or len(destination) != 3
        or not origin.isascii()
        or not destination.isascii()
        or not origin.isalpha()
        or not destination.isalpha()
        or origin == destination
    ):
        raise TravelpayoutsError("航線 IATA 代碼無效")
    if returning and returning < first_day:
        raise TravelpayoutsError("回程日期不可早於查詢月份")

    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": month,
        "calendar_type": "departure_date",
        "currency": currency.lower(),
    }
    if returning:
        params["return_date"] = returning.isoformat()
    try:
        response = requests.get(
            CALENDAR_URL,
            params=params,
            headers={
                "X-Access-Token": TRAVELPAYOUTS_TOKEN,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout as exc:
        raise TravelpayoutsError("Travelpayouts 回應逾時") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "未知"
        raise TravelpayoutsError(f"Travelpayouts 回應失敗（HTTP {status}）") from exc
    except requests.RequestException as exc:
        raise TravelpayoutsError("Travelpayouts 連線失敗") from exc
    except ValueError as exc:
        raise TravelpayoutsError("Travelpayouts 回傳資料無法解析") from exc

    if not isinstance(payload, Mapping):
        raise TravelpayoutsError("Travelpayouts 回傳格式無效")
    if not payload.get("success"):
        raise TravelpayoutsError("Travelpayouts 暫時沒有可用資料")
    raw_days = payload.get("data")
    if raw_days is None:
        raw_days = {}
    if not isinstance(raw_days, Mapping):
        raise TravelpayoutsError("Travelpayouts 回傳格式無效")
    days: List[Dict[str, Any]] = []
    for observed_date, item in raw_days.items():
        if not isinstance(item, dict):
            continue
        try:
            observed = date.fromisoformat(observed_date)
            price = float(item["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if (observed.year, observed.month) != (first_day.year, first_day.month) or price <= 0:
            continue
        days.append(
            {
                "date": observed.isoformat(),
                "price": price,
                "currency": currency.upper(),
                "airline": str(item.get("airline") or ""),
                "transfers": item.get("transfers"),
                "return_date": item.get("return_at") or return_date,
                "expires_at": str(item.get("expires_at") or ""),
            }
        )
    days.sort(key=lambda item: item["date"])
    prices = [float(item["price"]) for item in days]
    minimum = min(prices) if prices else None
    maximum = max(prices) if prices else None
    for item in days:
        if minimum is None or maximum is None or maximum == minimum:
            score = 0.0
        else:
            score = (float(item["price"]) - minimum) / (maximum - minimum)
        item["price_band"] = "low" if score <= 0.33 else "mid" if score <= 0.66 else "high"

    return {
        "available": bool(days),
        "source": "travelpayouts_data_api",
        "cached": True,
        "bookable": False,
        "month": month,
        "origin": origin,
        "destination": destination,
        "currency": currency.upper(),
        "days": days,
        "observations": len(days),
        "min_price": minimum,
        "max_price": maximum,
        "verify_provider": "serpapi",
        "verify_query_cost": 1,
        "disclaimer": "快取市場趨勢，非即時可購買票價；請選擇日期後用 SerpAPI 驗證。",
    }
