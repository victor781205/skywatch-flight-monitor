"""
全球機場資料（IATA）
資料來源：OurAirports 公開資料
https://davidmegginson.github.io/ourairports-data/airports.csv
"""
from __future__ import annotations

import csv
import io
import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from .config import DATA_DIR

AIRPORTS_CSV = DATA_DIR / "airports_iata.csv"
AIRPORTS_JSON = DATA_DIR / "airports_iata.json"
OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

# 額外關鍵字（城市別名 → 可命中的 IATA）
CITY_KEYWORDS: Dict[str, List[str]] = {
    "tokyo": ["HND", "NRT"],
    "東京": ["HND", "NRT"],
    "osaka": ["KIX", "ITM", "UKB"],
    "大阪": ["KIX", "ITM", "UKB"],
    "seoul": ["ICN", "GMP"],
    "首爾": ["ICN", "GMP"],
    "bangkok": ["BKK", "DMK"],
    "曼谷": ["BKK", "DMK"],
    "taipei": ["TPE", "TSA"],
    "台北": ["TPE", "TSA"],
    "臺北": ["TPE", "TSA"],
    "kaohsiung": ["KHH"],
    "高雄": ["KHH"],
    "new york": ["JFK", "EWR", "LGA"],
    "紐約": ["JFK", "EWR", "LGA"],
    "london": ["LHR", "LGW", "STN", "LTN", "LCY"],
    "倫敦": ["LHR", "LGW", "STN", "LTN", "LCY"],
    "paris": ["CDG", "ORY", "BVA"],
    "巴黎": ["CDG", "ORY", "BVA"],
    "shanghai": ["PVG", "SHA"],
    "上海": ["PVG", "SHA"],
    "beijing": ["PEK", "PKX"],
    "北京": ["PEK", "PKX"],
    "milan": ["MXP", "LIN", "BGY"],
    "rome": ["FCO", "CIA"],
    "羅馬": ["FCO", "CIA"],
}

# 台灣與常見機場中文別名（補強英文資料）
ZH_ALIASES: Dict[str, str] = {
    "TPE": "台北桃園",
    "TSA": "台北松山",
    "KHH": "高雄",
    "RMQ": "台中",
    "HUN": "花蓮",
    "TTT": "台東",
    "KNH": "金門",
    "MZG": "馬公",
    "CYI": "嘉義",
    "NRT": "東京成田",
    "HND": "東京羽田",
    "KIX": "大阪關西",
    "ITM": "大阪伊丹",
    "NGO": "名古屋中部",
    "FUK": "福岡",
    "CTS": "札幌新千歲",
    "OKA": "沖繩那霸",
    "ICN": "首爾仁川",
    "GMP": "首爾金浦",
    "PUS": "釜山",
    "HKG": "香港",
    "MFM": "澳門",
    "BKK": "曼谷蘇凡納布",
    "DMK": "曼谷廊曼",
    "HKT": "普吉",
    "CNX": "清邁",
    "SIN": "新加坡",
    "KUL": "吉隆坡",
    "MNL": "馬尼拉",
    "SGN": "胡志明市",
    "HAN": "河內",
    "DAD": "峴港",
    "PVG": "上海浦東",
    "SHA": "上海虹橋",
    "PEK": "北京首都",
    "PKX": "北京大興",
    "CAN": "廣州",
    "SZX": "深圳",
    "CTU": "成都",
    "CKG": "重慶",
    "XIY": "西安",
    "HGH": "杭州",
    "NKG": "南京",
    "XMN": "廈門",
    "FOC": "福州",
    "WUH": "武漢",
    "CSX": "長沙",
    "KMG": "昆明",
    "TAO": "青島",
    "DLC": "大連",
    "SHE": "瀋陽",
    "LAX": "洛杉磯",
    "SFO": "舊金山",
    "JFK": "紐約 JFK",
    "EWR": "紐約紐瓦克",
    "LGA": "紐約拉瓜地亞",
    "ORD": "芝加哥歐海爾",
    "SEA": "西雅圖",
    "YVR": "溫哥華",
    "YYZ": "多倫多",
    "YUL": "蒙特婁",
    "LHR": "倫敦希斯洛",
    "LGW": "倫敦蓋威克",
    "CDG": "巴黎戴高樂",
    "ORY": "巴黎奧利",
    "FRA": "法蘭克福",
    "MUC": "慕尼黑",
    "AMS": "阿姆斯特丹",
    "MAD": "馬德里",
    "BCN": "巴塞隆納",
    "FCO": "羅馬",
    "MXP": "米蘭",
    "ZRH": "蘇黎世",
    "VIE": "維也納",
    "IST": "伊斯坦堡",
    "DXB": "杜拜",
    "AUH": "阿布達比",
    "DOH": "杜哈",
    "SIN": "新加坡",
    "SYD": "雪梨",
    "MEL": "墨爾本",
    "BNE": "布里斯本",
    "AKL": "奧克蘭",
    "CHC": "基督城",
    "NRT": "東京成田",
}


def ensure_airport_data(force: bool = False) -> Path:
    """確保本機有機場清單；若無則下載 OurAirports 並篩出有 IATA 的機場。"""
    if AIRPORTS_CSV.exists() and AIRPORTS_JSON.exists() and not force:
        return AIRPORTS_CSV

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("正在下載全球機場資料（OurAirports）…")
    resp = requests.get(OURAIRPORTS_URL, timeout=120)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    rows: List[Dict[str, str]] = []
    reader = csv.DictReader(io.StringIO(resp.text))
    for r in reader:
        iata = (r.get("iata_code") or "").strip().upper()
        if not iata or len(iata) != 3:
            continue
        # 排除已關閉、水上機場等較不適合機票查詢的類型可選擇性保留
        # 保留 large/medium/small + 有 scheduled 服務的
        ap_type = (r.get("type") or "").strip()
        if ap_type not in ("large_airport", "medium_airport", "small_airport"):
            continue
        name = (r.get("name") or "").strip()
        city = (r.get("municipality") or "").strip()
        country = (r.get("iso_country") or "").strip().upper()
        lat = r.get("latitude_deg") or ""
        lon = r.get("longitude_deg") or ""
        zh = ZH_ALIASES.get(iata, "")
        label = _make_label(iata, name, city, country, zh)
        rows.append(
            {
                "iata": iata,
                "name": name,
                "city": city,
                "country": country,
                "type": ap_type,
                "lat": lat,
                "lon": lon,
                "zh": zh,
                "label": label,
            }
        )

    # 同 IATA 可能多筆：優先 large > medium > small
    priority = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}
    best: Dict[str, Dict[str, str]] = {}
    for row in rows:
        code = row["iata"]
        if code not in best or priority.get(row["type"], 9) < priority.get(best[code]["type"], 9):
            best[code] = row

    ordered = sorted(best.values(), key=lambda x: (x["country"], x["city"], x["iata"]))

    with AIRPORTS_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["iata", "name", "city", "country", "type", "lat", "lon", "zh", "label"],
        )
        writer.writeheader()
        writer.writerows(ordered)

    # 精簡 JSON 供快速載入
    slim = {r["iata"]: r["label"] for r in ordered}
    AIRPORTS_JSON.write_text(json.dumps(slim, ensure_ascii=False), encoding="utf-8")
    print(f"已建立 {len(ordered)} 個 IATA 機場 → {AIRPORTS_CSV}")
    return AIRPORTS_CSV


def _make_label(iata: str, name: str, city: str, country: str, zh: str = "") -> str:
    if zh:
        base = f"{zh} ({iata})"
    elif city:
        base = f"{city} · {name} ({iata})"
    else:
        base = f"{name} ({iata})"
    if country:
        return f"{base} · {country}"
    return base


@lru_cache(maxsize=1)
def load_airports() -> List[Dict[str, str]]:
    ensure_airport_data()
    items: List[Dict[str, str]] = []
    with AIRPORTS_CSV.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            items.append(dict(r))
    return items


@lru_cache(maxsize=1)
def airport_label_map() -> Dict[str, str]:
    """iata -> label"""
    ensure_airport_data()
    if AIRPORTS_JSON.exists():
        return json.loads(AIRPORTS_JSON.read_text(encoding="utf-8"))
    return {a["iata"]: a["label"] for a in load_airports()}


def get_label(iata: str) -> str:
    code = (iata or "").upper().strip()
    return airport_label_map().get(code, code)


def search_airports(query: str, limit: int = 80) -> List[Dict[str, str]]:
    """依關鍵字搜尋機場（IATA / 城市 / 名稱 / 中文別名 / 國家）"""
    q = (query or "").strip().lower()
    airports = load_airports()
    by_code = {a["iata"]: a for a in airports}
    if not q:
        preferred = [
            "TPE", "TSA", "KHH", "RMQ", "NRT", "HND", "KIX", "ICN", "HKG", "MFM",
            "BKK", "SIN", "KUL", "MNL", "SGN", "PVG", "PEK", "LAX", "SFO", "JFK",
            "LHR", "CDG", "FRA", "AMS", "SYD", "MEL", "DXB", "DOH", "IST",
        ]
        result = [by_code[c] for c in preferred if c in by_code]
        for a in airports:
            if a["type"] == "large_airport" and a not in result:
                result.append(a)
            if len(result) >= limit:
                break
        return result[:limit]

    # 城市別名優先展開
    boosted_codes = set()
    for key, codes in CITY_KEYWORDS.items():
        if q == key or key in q or q in key:
            boosted_codes.update(codes)

    scored: List[Tuple[float, Dict[str, str]]] = []
    seen = set()

    for code in boosted_codes:
        a = by_code.get(code)
        if a:
            scored.append((-1.0, a))  # 最高優先
            seen.add(code)

    for a in airports:
        if a["iata"] in seen:
            continue
        iata = a["iata"].lower()
        name = (a.get("name") or "").lower()
        city = (a.get("city") or "").lower()
        country = (a.get("country") or "").lower()
        zh = (a.get("zh") or "").lower()
        label = (a.get("label") or "").lower()

        if q == iata:
            score = 0.0
        elif iata.startswith(q):
            score = 1.0
        elif q in iata:
            score = 2.0
        elif zh and q in zh:
            score = 3.0
        elif city.startswith(q) or q in city:
            score = 4.0
        elif q in name:
            score = 5.0
        elif q in country or q in label:
            score = 6.0
        else:
            continue

        if a.get("type") == "large_airport":
            score -= 0.5
        elif a.get("type") == "medium_airport":
            score -= 0.2
        scored.append((score, a))
        seen.add(a["iata"])

    scored.sort(key=lambda x: (x[0], x[1].get("city") or "", x[1]["iata"]))
    return [a for _, a in scored[:limit]]


def options_for_select(query: str = "", limit: int = 80) -> Dict[str, str]:
    """
    回傳 {label: iata} 供 Streamlit selectbox 使用
    """
    items = search_airports(query, limit=limit)
    return {a["label"]: a["iata"] for a in items}


# 相容舊版 AIRPORTS dict
def as_airports_dict() -> Dict[str, str]:
    return airport_label_map()
