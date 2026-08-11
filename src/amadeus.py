"""Minimal Amadeus Self-Service Flight Offers Search adapter."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from .config import AMADEUS_CLIENT_ID, AMADEUS_CLIENT_SECRET, AMADEUS_ENV


class AmadeusError(Exception):
    pass


_TOKEN_CACHE: Dict[str, Any] = {"token": None, "expires_at": None}


def configured() -> bool:
    return bool(AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET)


def _base_url() -> str:
    return "https://api.amadeus.com" if AMADEUS_ENV == "production" else "https://test.api.amadeus.com"


def _access_token() -> str:
    now = datetime.now(timezone.utc)
    token = _TOKEN_CACHE.get("token")
    expires_at = _TOKEN_CACHE.get("expires_at")
    if token and expires_at and expires_at > now + timedelta(seconds=30):
        return str(token)
    if not configured():
        raise AmadeusError("未設定 AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET")
    try:
        response = requests.post(
            f"{_base_url()}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": AMADEUS_CLIENT_ID,
                "client_secret": AMADEUS_CLIENT_SECRET,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AmadeusError("Amadeus 未回傳 access token")
        lifetime = max(60, int(payload.get("expires_in") or 1800))
        _TOKEN_CACHE.update(token=token, expires_at=now + timedelta(seconds=lifetime))
        return str(token)
    except requests.Timeout as exc:
        raise AmadeusError("Amadeus 驗證逾時") from exc
    except requests.RequestException as exc:
        raise AmadeusError("Amadeus 驗證失敗") from exc
    except (TypeError, ValueError) as exc:
        raise AmadeusError("Amadeus 驗證資料格式錯誤") from exc


def search_flights(
    origin: str,
    destination: str,
    outbound_date: str,
    return_date: Optional[str],
    adults: int,
    travel_class: int,
    currency: str,
    max_results: int,
) -> Dict[str, Any]:
    cabin = {1: "ECONOMY", 2: "PREMIUM_ECONOMY", 3: "BUSINESS", 4: "FIRST"}[travel_class]
    params: Dict[str, Any] = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": outbound_date,
        "adults": adults,
        "travelClass": cabin,
        "currencyCode": currency,
        "max": max(1, min(int(max_results), 50)),
    }
    if return_date:
        params["returnDate"] = return_date
    try:
        response = requests.get(
            f"{_base_url()}/v2/shopping/flight-offers",
            headers={"Authorization": f"Bearer {_access_token()}"},
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout as exc:
        raise AmadeusError("Amadeus Flight Offers Search 逾時") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "未知"
        raise AmadeusError(f"Amadeus Flight Offers Search 失敗（HTTP {status}）") from exc
    except requests.RequestException as exc:
        raise AmadeusError("Amadeus Flight Offers Search 連線失敗") from exc
    except ValueError as exc:
        raise AmadeusError("Amadeus 回傳了無法解析的資料") from exc

    carriers = (payload.get("dictionaries") or {}).get("carriers") or {}
    flights = [
        parsed
        for offer in payload.get("data") or []
        if (parsed := _parse_offer(offer, carriers)) is not None
    ]
    flights.sort(key=lambda item: item["price"])
    flights = flights[:max_results]
    return {
        "origin": origin,
        "destination": destination,
        "outbound_date": outbound_date,
        "return_date": return_date,
        "currency": currency,
        "best_price": flights[0]["price"] if flights else None,
        "flights": flights,
        "raw": payload,
        "provider": "amadeus",
        "provider_environment": AMADEUS_ENV,
    }


def _parse_offer(offer: Dict[str, Any], carriers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    itineraries = offer.get("itineraries") or []
    if not itineraries:
        return None
    try:
        price = float((offer.get("price") or {}).get("grandTotal") or (offer.get("price") or {})["total"])
    except (KeyError, TypeError, ValueError):
        return None
    outbound = _parse_itinerary(itineraries[0], carriers)
    if outbound is None:
        return None
    outbound.update(
        price=price,
        type="Amadeus Flight Offer",
        departure_token=None,
        booking_token=None,
        carbon_emissions=None,
    )
    if len(itineraries) > 1:
        outbound["return_flight"] = _parse_itinerary(itineraries[1], carriers)
    return outbound


def _parse_itinerary(itinerary: Dict[str, Any], carriers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = itinerary.get("segments") or []
    if not segments:
        return None
    first = segments[0]
    last = segments[-1]
    codes: List[str] = []
    numbers: List[str] = []
    planes: List[str] = []
    for segment in segments:
        code = str(segment.get("carrierCode") or "")
        if code and code not in codes:
            codes.append(code)
        number = f"{code}{segment.get('number') or ''}".strip()
        if number:
            numbers.append(number)
        plane = str((segment.get("aircraft") or {}).get("code") or "")
        if plane and plane not in planes:
            planes.append(plane)
    return {
        "airline": " / ".join(carriers.get(code, code) for code in codes) or "未知航空",
        "flight_number": ", ".join(numbers),
        "airplane": " / ".join(planes),
        "departure": str((first.get("departure") or {}).get("iataCode") or ""),
        "arrival": str((last.get("arrival") or {}).get("iataCode") or ""),
        "departure_time": _display_time((first.get("departure") or {}).get("at")),
        "arrival_time": _display_time((last.get("arrival") or {}).get("at")),
        "stops": max(0, len(segments) - 1),
        "duration": str(itinerary.get("duration") or "").removeprefix("PT").lower(),
    }


def _display_time(value: Any) -> str:
    text = str(value or "")
    return text.replace("T", " ")[:16] if text else ""
