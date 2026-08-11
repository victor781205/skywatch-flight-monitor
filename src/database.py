"""SQLite 資料庫：航線、價格歷史、監控提醒"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DB_PATH


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                origin_name TEXT,
                destination_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(origin, destination)
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id INTEGER NOT NULL,
                outbound_date TEXT NOT NULL,
                return_date TEXT,
                price REAL NOT NULL,
                currency TEXT DEFAULT 'TWD',
                airline TEXT,
                stops INTEGER DEFAULT 0,
                duration TEXT,
                departure_time TEXT,
                arrival_time TEXT,
                flight_number TEXT,
                raw_json TEXT,
                travel_class INTEGER NOT NULL DEFAULT 1,
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(route_id) REFERENCES routes(id)
            );

            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id INTEGER NOT NULL,
                outbound_date TEXT NOT NULL,
                return_date TEXT,
                target_price REAL NOT NULL,
                adults INTEGER DEFAULT 1,
                trip_type TEXT DEFAULT 'one_way',
                active INTEGER DEFAULT 1,
                travel_class INTEGER NOT NULL DEFAULT 1,
                last_checked TEXT,
                last_price REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(route_id) REFERENCES routes(id)
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER,
                message TEXT NOT NULL,
                channel TEXT,
                success INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(monitor_id) REFERENCES monitors(id)
            );

            CREATE TABLE IF NOT EXISTS notification_claims (
                dedupe_key TEXT PRIMARY KEY,
                monitor_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_price_route_date
                ON price_history(route_id, outbound_date, return_date);
            CREATE INDEX IF NOT EXISTS idx_monitors_active
                ON monitors(active);
            CREATE INDEX IF NOT EXISTS idx_notification_claims_created
                ON notification_claims(created_at);
            """
        )

        # Legacy databases created before v2.1 do not contain travel_class.
        _ensure_column(conn, "price_history", "travel_class", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "monitors", "travel_class", "INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '2') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add one backward-compatible column when upgrading a legacy database."""
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def upsert_route(origin: str, destination: str, origin_name: str = "", destination_name: str = "") -> int:
    origin = origin.upper().strip()
    destination = destination.upper().strip()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO routes (origin, destination, origin_name, destination_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(origin, destination) DO UPDATE SET
                origin_name=excluded.origin_name,
                destination_name=excluded.destination_name
            """,
            (origin, destination, origin_name or origin, destination_name or destination),
        )
        row = conn.execute(
            "SELECT id FROM routes WHERE origin=? AND destination=?",
            (origin, destination),
        ).fetchone()
        return int(row["id"])


def add_price_record(
    route_id: int,
    outbound_date: str,
    price: float,
    return_date: Optional[str] = None,
    currency: str = "TWD",
    airline: str = "",
    stops: int = 0,
    duration: str = "",
    departure_time: str = "",
    arrival_time: str = "",
    flight_number: str = "",
    raw_json: str = "",
    travel_class: int = 1,
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO price_history (
                route_id, outbound_date, return_date, price, currency,
                airline, stops, duration, departure_time, arrival_time,
                flight_number, raw_json, travel_class, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route_id,
                outbound_date,
                return_date,
                price,
                currency,
                airline,
                stops,
                duration,
                departure_time,
                arrival_time,
                flight_number,
                raw_json,
                travel_class,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return int(cur.lastrowid)


def get_price_history(
    route_id: int,
    outbound_date: str,
    return_date: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    with get_db() as conn:
        if return_date:
            rows = conn.execute(
                """
                SELECT * FROM price_history
                WHERE route_id=? AND outbound_date=? AND return_date=?
                ORDER BY recorded_at ASC
                LIMIT ?
                """,
                (route_id, outbound_date, return_date, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM price_history
                WHERE route_id=? AND outbound_date=? AND (return_date IS NULL OR return_date='')
                ORDER BY recorded_at ASC
                LIMIT ?
                """,
                (route_id, outbound_date, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def get_latest_price(route_id: int, outbound_date: str, return_date: Optional[str] = None) -> Optional[float]:
    history = get_price_history(route_id, outbound_date, return_date, limit=1)
    # get_price_history orders ASC; for latest use DESC query
    with get_db() as conn:
        if return_date:
            row = conn.execute(
                """
                SELECT price FROM price_history
                WHERE route_id=? AND outbound_date=? AND return_date=?
                ORDER BY recorded_at DESC LIMIT 1
                """,
                (route_id, outbound_date, return_date),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT price FROM price_history
                WHERE route_id=? AND outbound_date=? AND (return_date IS NULL OR return_date='')
                ORDER BY recorded_at DESC LIMIT 1
                """,
                (route_id, outbound_date),
            ).fetchone()
        return float(row["price"]) if row else None


def find_duplicate_monitor(
    route_id: int,
    outbound_date: str,
    return_date: Optional[str] = None,
    adults: int = 1,
    travel_class: int = 1,
    active_only: bool = True,
) -> Optional[Dict[str, Any]]:
    """同一航線 + 日期 + 人數 + 艙等視為重複監控。"""
    with get_db() as conn:
        sql = """
            SELECT m.*, r.origin, r.destination, r.origin_name, r.destination_name
            FROM monitors m
            JOIN routes r ON r.id = m.route_id
            WHERE m.route_id=? AND m.outbound_date=? AND m.adults=? AND m.travel_class=?
        """
        params: List[Any] = [route_id, outbound_date, adults, travel_class]
        if return_date:
            sql += " AND m.return_date=?"
            params.append(return_date)
        else:
            sql += " AND (m.return_date IS NULL OR m.return_date='')"
        if active_only:
            sql += " AND m.active=1"
        sql += " ORDER BY m.created_at DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def add_monitor(
    route_id: int,
    outbound_date: str,
    target_price: float,
    return_date: Optional[str] = None,
    adults: int = 1,
    travel_class: int = 1,
    trip_type: str = "one_way",
    allow_duplicate: bool = False,
) -> Dict[str, Any]:
    """
    新增監控。預設防重複：
    - 若已有相同 active 監控 → 更新目標價並回傳 existing
    - 否則新增
    回傳 {"id": int, "created": bool, "updated": bool, "duplicate": bool}
    """
    if not allow_duplicate:
        existing = find_duplicate_monitor(route_id, outbound_date, return_date, adults, travel_class, active_only=True)
        if existing:
            with get_db() as conn:
                conn.execute(
                    "UPDATE monitors SET target_price=?, trip_type=? WHERE id=?",
                    (target_price, trip_type, existing["id"]),
                )
            return {
                "id": int(existing["id"]),
                "created": False,
                "updated": True,
                "duplicate": True,
            }

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO monitors (
                route_id, outbound_date, return_date, target_price,
                adults, trip_type, active, travel_class, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                route_id,
                outbound_date,
                return_date,
                target_price,
                adults,
                trip_type,
                travel_class,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return {
            "id": int(cur.lastrowid),
            "created": True,
            "updated": False,
            "duplicate": False,
        }


def list_monitors(active_only: bool = False) -> List[Dict[str, Any]]:
    with get_db() as conn:
        sql = """
            SELECT m.*, r.origin, r.destination, r.origin_name, r.destination_name
            FROM monitors m
            JOIN routes r ON r.id = m.route_id
        """
        if active_only:
            sql += " WHERE m.active=1"
        sql += " ORDER BY m.created_at DESC"
        return [dict(r) for r in conn.execute(sql).fetchall()]


def update_monitor_check(monitor_id: int, last_price: Optional[float]) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE monitors
            SET last_checked=?, last_price=?
            WHERE id=?
            """,
            (datetime.now().isoformat(timespec="seconds"), last_price, monitor_id),
        )


def set_monitor_active(monitor_id: int, active: bool) -> None:
    with get_db() as conn:
        conn.execute("UPDATE monitors SET active=? WHERE id=?", (1 if active else 0, monitor_id))


def delete_monitor(monitor_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM notification_claims WHERE monitor_id=?", (monitor_id,))
        conn.execute("DELETE FROM notifications WHERE monitor_id=?", (monitor_id,))
        conn.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))


def claim_notification(monitor_id: int, dedupe_key: str, ttl_hours: float = 24.0) -> bool:
    """Atomically claim a notification window across processes."""
    cutoff = (datetime.now() - timedelta(hours=max(ttl_hours, 1.0))).isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute("DELETE FROM notification_claims WHERE created_at < ?", (cutoff,))
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO notification_claims (dedupe_key, monitor_id, created_at)
            VALUES (?, ?, ?)
            """,
            (dedupe_key, monitor_id, datetime.now().isoformat(timespec="seconds")),
        )
        return cur.rowcount == 1


def release_notification_claim(dedupe_key: str) -> None:
    """Release a failed delivery claim so the next run can retry."""
    with get_db() as conn:
        conn.execute("DELETE FROM notification_claims WHERE dedupe_key=?", (dedupe_key,))


def log_notification(monitor_id: Optional[int], message: str, channel: str, success: bool = True) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO notifications (monitor_id, message, channel, success, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                monitor_id,
                message,
                channel,
                1 if success else 0,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def recent_success_notification(
    monitor_id: int,
    hours: float = 6.0,
    channels: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """查詢冷卻時間內是否已有成功推播（用於通知去重）。"""
    if hours <= 0:
        return None
    with get_db() as conn:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        sql = """
            SELECT * FROM notifications
            WHERE monitor_id=? AND success=1
              AND channel != 'local'
              AND created_at >= ?
        """
        params: List[Any] = [monitor_id, cutoff]
        if channels:
            placeholders = ",".join("?" for _ in channels)
            sql += f" AND channel IN ({placeholders})"
            params.extend(channels)
        sql += " ORDER BY created_at DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def list_notifications(limit: int = 50) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM notifications
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_routes() -> List[Dict[str, Any]]:
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM routes ORDER BY id DESC").fetchall()]


def get_dashboard_stats() -> Dict[str, Any]:
    """總覽儀表板用統計。"""
    with get_db() as conn:
        active_monitors = conn.execute(
            "SELECT COUNT(*) AS c FROM monitors WHERE active=1"
        ).fetchone()["c"]
        total_monitors = conn.execute("SELECT COUNT(*) AS c FROM monitors").fetchone()["c"]
        routes = conn.execute("SELECT COUNT(*) AS c FROM routes").fetchone()["c"]
        history = conn.execute("SELECT COUNT(*) AS c FROM price_history").fetchone()["c"]
        hits = conn.execute(
            """
            SELECT COUNT(*) AS c FROM monitors
            WHERE last_price IS NOT NULL AND last_price <= target_price AND active=1
            """
        ).fetchone()["c"]
        latest_prices = conn.execute(
            """
            SELECT price FROM price_history
            ORDER BY recorded_at DESC LIMIT 20
            """
        ).fetchall()
        avg_recent = None
        if latest_prices:
            avg_recent = sum(float(r["price"]) for r in latest_prices) / len(latest_prices)
        cutoff_24h = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        notif_24h = conn.execute(
            """
            SELECT COUNT(*) AS c FROM notifications
            WHERE created_at >= ?
            """,
            (cutoff_24h,),
        ).fetchone()["c"]
        recent_history = conn.execute(
            """
            SELECT h.price, h.recorded_at, h.outbound_date, h.airline,
                   r.origin, r.destination
            FROM price_history h
            JOIN routes r ON r.id = h.route_id
            ORDER BY h.recorded_at DESC
            LIMIT 8
            """
        ).fetchall()
        monitor_rows = conn.execute(
            """
            SELECT m.*, r.origin, r.destination, r.origin_name, r.destination_name
            FROM monitors m
            JOIN routes r ON r.id = m.route_id
            WHERE m.active=1
            ORDER BY
                CASE WHEN m.last_price IS NOT NULL AND m.last_price <= m.target_price THEN 0 ELSE 1 END,
                m.last_checked DESC
            LIMIT 6
            """
        ).fetchall()
        return {
            "active_monitors": int(active_monitors),
            "total_monitors": int(total_monitors),
            "routes": int(routes),
            "history_count": int(history),
            "hit_count": int(hits),
            "avg_recent_price": avg_recent,
            "notifications_24h": int(notif_24h),
            "recent_history": [dict(r) for r in recent_history],
            "top_monitors": [dict(r) for r in monitor_rows],
        }


def get_route_price_stats(
    route_id: int,
    outbound_date: Optional[str] = None,
    return_date: Optional[str] = None,
) -> Dict[str, Any]:
    """價格洞察：相對歷史最低/平均/最新。"""
    with get_db() as conn:
        sql = "SELECT price, recorded_at FROM price_history WHERE route_id=?"
        params: List[Any] = [route_id]
        if outbound_date:
            sql += " AND outbound_date=?"
            params.append(outbound_date)
            if return_date:
                sql += " AND return_date=?"
                params.append(return_date)
            else:
                sql += " AND (return_date IS NULL OR return_date='')"
        sql += " ORDER BY recorded_at ASC"
        rows = conn.execute(sql, params).fetchall()
        scope = "exact" if outbound_date else "route"
        if not rows:
            rows = conn.execute(
                """
                SELECT price, recorded_at FROM price_history
                WHERE route_id=? ORDER BY recorded_at ASC
                """,
                (route_id,),
            ).fetchall()
            scope = "route"

        if not rows:
            return {
                "count": 0,
                "min_price": None,
                "max_price": None,
                "avg_price": None,
                "latest_price": None,
                "scope": scope,
            }

        prices = [float(r["price"]) for r in rows]
        return {
            "count": len(prices),
            "min_price": min(prices),
            "max_price": max(prices),
            "avg_price": sum(prices) / len(prices),
            "latest_price": prices[-1],
            "scope": scope,
            "first_recorded": rows[0]["recorded_at"],
            "last_recorded": rows[-1]["recorded_at"],
        }



def expire_monitors() -> int:
    """將出發日已過的 active 監控自動設為 inactive。回傳受影響筆數。"""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE monitors SET active=0
            WHERE active=1 AND outbound_date < ?
            """,
            (today,),
        )
        return cur.rowcount


def cleanup_old_records(days: int = 90) -> int:
    """刪除 N 天前的 price_history 紀錄，回傳刪除筆數。"""
    with get_db() as conn:
        cur = conn.execute(
            """
            DELETE FROM price_history
            WHERE datetime(recorded_at) < datetime('now', ?)
            """,
            (f"-{days} days",),
        )
        return cur.rowcount


def deduplicate_exact_history() -> Dict[str, int]:
    """Remove byte-for-byte duplicate history rows while preserving the oldest ID."""
    deleted: Dict[str, int] = {}
    with get_db() as conn:
        for table in ("notifications", "price_history"):
            columns = [
                row["name"]
                for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                if row["name"] != "id"
            ]
            grouped_columns = ", ".join(f'"{column}"' for column in columns)
            cursor = conn.execute(
                f'''
                DELETE FROM "{table}"
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM "{table}"
                    GROUP BY {grouped_columns}
                )
                '''
            )
            deleted[table] = cursor.rowcount
    return deleted
