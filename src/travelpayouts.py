"""Travelpayouts cached market context for user-initiated searches.

This adapter intentionally uses the Data API, not the restricted real-time
Search API. Its prices are market references and must never trigger alerts.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

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
    if len(origin) != 3 or len(destination) != 3 or origin == destination:
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

    if not payload.get("success"):
        raise TravelpayoutsError("Travelpayouts 暫時沒有可用資料")
    observations = payload.get("data") or {}
    candidates = []
    for observed_date, item in observations.items():
        if not isinstance(item, dict):
            continue
        try:
            price = float(item["price"])
            observed = date.fromisoformat(observed_date)
        except (KeyError, TypeError, ValueError):
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
