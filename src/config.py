"""應用程式設定"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ALLOWED_CHAT_IDS = {
    value.strip()
    for value in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", TELEGRAM_CHAT_ID).split(",")
    if value.strip()
}


def _positive_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return min(maximum, max(minimum, float(os.getenv(name, str(default)))))
    except ValueError:
        return default


SERPAPI_MONTHLY_LIMIT = _positive_int("SERPAPI_MONTHLY_LIMIT", 250)
API_USAGE_WARNING_PERCENT = _bounded_float("API_USAGE_WARNING_PERCENT", 80.0, 1.0, 100.0)
MONITOR_FETCH_DETAILS_ON_HIT = os.getenv("MONITOR_FETCH_DETAILS_ON_HIT", "true").lower() in (
    "true",
    "1",
    "yes",
)

# 預設每次排程回報一次；跨程序鎖與通知 claim 負責防止重複推播。
ALWAYS_NOTIFY = os.getenv("ALWAYS_NOTIFY", "true").lower() in ("true", "1", "yes")

# 可選的手動檢查冷卻時間；固定排程不受此設定影響。
try:
    NOTIFY_COOLDOWN_HOURS = max(0.0, float(os.getenv("NOTIFY_COOLDOWN_HOURS", "0")))
except ValueError:
    NOTIFY_COOLDOWN_HOURS = 0.0

SCHEDULER_TICK_MINUTES = max(1, _positive_int("SCHEDULER_TICK_MINUTES", 5))
SCHEDULER_HEARTBEAT_STALE_MINUTES = max(
    3,
    _positive_int(
        "SCHEDULER_HEARTBEAT_STALE_MINUTES",
        max(15, SCHEDULER_TICK_MINUTES * 3),
    ),
)
MISSED_RUN_LOOKBACK_HOURS = max(1, _positive_int("MISSED_RUN_LOOKBACK_HOURS", 24))
OUTBOX_MAX_ATTEMPTS = max(1, _positive_int("OUTBOX_MAX_ATTEMPTS", 4))
TELEGRAM_COMMANDS_ENABLED = os.getenv("TELEGRAM_COMMANDS_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# 完全相同的報價內容即使被不同程序同時觸發，也只允許每個管道送出一次。
try:
    MESSAGE_DEDUPE_HOURS = max(1.0, float(os.getenv("MESSAGE_DEDUPE_HOURS", "24")))
except ValueError:
    MESSAGE_DEDUPE_HOURS = 24.0

APP_VERSION = "3.1.1"

DB_PATH = BASE_DIR / "data" / "flights.db"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 向後相容：AIRPORTS 改為全球 IATA 清單（延遲載入）
def _load_airports_dict():
    try:
        from .airports import as_airports_dict
        return as_airports_dict()
    except Exception:
        # 若尚未下載資料，先回傳熱門機場
        return {
            "TPE": "台北桃園 (TPE) · TW",
            "TSA": "台北松山 (TSA) · TW",
            "KHH": "高雄 (KHH) · TW",
            "RMQ": "台中 (RMQ) · TW",
            "NRT": "東京成田 (NRT) · JP",
            "HND": "東京羽田 (HND) · JP",
            "KIX": "大阪關西 (KIX) · JP",
            "ICN": "首爾仁川 (ICN) · KR",
            "HKG": "香港 (HKG) · HK",
            "BKK": "曼谷蘇凡納布 (BKK) · TH",
            "SIN": "新加坡 (SIN) · SG",
            "LAX": "洛杉磯 (LAX) · US",
            "SFO": "舊金山 (SFO) · US",
            "JFK": "紐約 JFK (JFK) · US",
            "LHR": "倫敦希斯洛 (LHR) · GB",
            "CDG": "巴黎戴高樂 (CDG) · FR",
            "SYD": "雪梨 (SYD) · AU",
        }


class _AirportsProxy(dict):
    """dict 介面，首次存取時載入全球機場"""

    _loaded = False

    def _ensure(self):
        if not self._loaded:
            self.clear()
            self.update(_load_airports_dict())
            self._loaded = True

    def __getitem__(self, key):
        self._ensure()
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._ensure()
        return super().get(key, default)

    def __contains__(self, key):
        self._ensure()
        return super().__contains__(key)

    def items(self):
        self._ensure()
        return super().items()

    def keys(self):
        self._ensure()
        return super().keys()

    def values(self):
        self._ensure()
        return super().values()

    def __len__(self):
        self._ensure()
        return super().__len__()


AIRPORTS = _AirportsProxy()
