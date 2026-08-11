# 資料模型（參考用，目前未被其他模組引用）
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional

@dataclass
class FlightInfo:
    id: int
    route_id: int
    outbound_date: str
    outbound_price: float
    airline: str
    departure: str
    arrival: str
    departure_time: str
    arrival_time: str
    stops: int
    duration: str
    flight_number: str
    return_date: Optional[str] = None
    return_price: Optional[float] = None
    created_at: datetime = field(default_factory=datetime.now)

@dataclass
class Route:
    id: int
    origin: str
    destination: str
    origin_name: str
    destination_name: str

@dataclass
class PriceAlert:
    id: int
    route_id: int
    outbound_date: str
    target_price: float
    return_date: Optional[str] = None
    last_notified: Optional[datetime] = None
    active: bool = True

@dataclass
class PriceHistory:
    id: int
    route_id: int
    outbound_date: str
    price: float
    return_date: Optional[str] = None
    recorded_at: datetime = field(default_factory=datetime.now)

# 通知類型
NOTIFICATION_TYPES = {
    "discord": "Discord",
    "telegram": "Telegram",
    "email": "Email"
}