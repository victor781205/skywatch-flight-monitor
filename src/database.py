"""SQLite 資料庫：航線、價格歷史、監控提醒"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .config import DB_PATH


_OUTBOX_TIMEZONE = ZoneInfo("Asia/Taipei")


def _outbox_time(value: Optional[datetime] = None) -> datetime:
    """Normalise queue clocks to Taipei local-naive for sortable SQLite text."""
    current = value or datetime.now(_OUTBOX_TIMEZONE)
    if current.tzinfo is None:
        return current
    return current.astimezone(_OUTBOX_TIMEZONE).replace(tzinfo=None)


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

            CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                operation TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 1,
                detail TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_quota_warnings (
                provider TEXT NOT NULL,
                period TEXT NOT NULL,
                threshold INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (provider, period, threshold)
            );

            CREATE TABLE IF NOT EXISTS check_runs (
                id TEXT PRIMARY KEY,
                monitor_id INTEGER,
                trigger TEXT NOT NULL DEFAULT 'scheduled',
                scheduled_slot TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_ms INTEGER,
                api_calls INTEGER NOT NULL DEFAULT 0,
                error_stage TEXT,
                error_message TEXT,
                backfill INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(monitor_id) REFERENCES monitors(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                monitor_id INTEGER,
                channel TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 4,
                next_attempt_at TEXT NOT NULL,
                last_error TEXT,
                dedupe_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                FOREIGN KEY(run_id) REFERENCES check_runs(id) ON DELETE SET NULL,
                FOREIGN KEY(monitor_id) REFERENCES monitors(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS carrier_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                monitor_id INTEGER,
                route_id INTEGER NOT NULL,
                carrier_code TEXT,
                carrier_name TEXT,
                total_price REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'TWD',
                outbound_json TEXT NOT NULL DEFAULT '{}',
                return_json TEXT NOT NULL DEFAULT '{}',
                stops INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES check_runs(id) ON DELETE SET NULL,
                FOREIGN KEY(monitor_id) REFERENCES monitors(id) ON DELETE SET NULL,
                FOREIGN KEY(route_id) REFERENCES routes(id) ON DELETE CASCADE
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
            CREATE INDEX IF NOT EXISTS idx_api_usage_provider_created
                ON api_usage(provider, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_check_runs_monitor_slot
                ON check_runs(monitor_id, scheduled_slot)
                WHERE monitor_id IS NOT NULL AND scheduled_slot IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_check_runs_started
                ON check_runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_outbox_due
                ON notification_outbox(status, next_attempt_at);
            CREATE INDEX IF NOT EXISTS idx_carrier_quotes_monitor_recorded
                ON carrier_quotes(monitor_id, recorded_at DESC);
            """
        )

        # Legacy databases created before v2.1 do not contain travel_class.
        _ensure_column(conn, "price_history", "travel_class", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "monitors", "travel_class", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "monitors", "quota_mode", "TEXT NOT NULL DEFAULT 'full'")
        _ensure_column(conn, "monitors", "airline_rules_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "monitors", "schedule_type", "TEXT NOT NULL DEFAULT 'global'")
        _ensure_column(conn, "monitors", "schedule_value", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "monitors", "notification_policy", "TEXT NOT NULL DEFAULT 'always'")
        _ensure_column(conn, "monitors", "quiet_hours_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "monitors", "next_run_at", "TEXT")
        _ensure_column(conn, "monitors", "last_run_id", "TEXT")
        _ensure_column(conn, "monitors", "schedule_effective_at", "TEXT")
        conn.execute(
            "UPDATE monitors SET schedule_effective_at=COALESCE(schedule_effective_at, created_at)"
        )
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '7') "
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
    quota_mode: str = "full",
    airline_rules: Optional[List[Dict[str, Any]]] = None,
    schedule_type: str = "global",
    schedule_value: str = "",
    notification_policy: str = "always",
    quiet_hours: Optional[Dict[str, Any]] = None,
    next_run_at: Optional[str] = None,
) -> Dict[str, Any]:
    """
    新增監控。預設防重複：
    - 若已有相同 active 監控 → 更新目標價並回傳 existing
    - 否則新增
    回傳 {"id": int, "created": bool, "updated": bool, "duplicate": bool}
    """
    if quota_mode not in {"lean", "standard", "full"}:
        raise ValueError("quota_mode must be lean, standard, or full")
    if schedule_type not in {"global", "fixed_times", "daily", "interval"}:
        raise ValueError("unsupported schedule_type")
    if notification_policy not in {"always", "target_only", "price_drop", "digest"}:
        raise ValueError("unsupported notification_policy")
    now_stamp = datetime.now().isoformat(timespec="seconds")
    if not allow_duplicate:
        existing = find_duplicate_monitor(route_id, outbound_date, return_date, adults, travel_class, active_only=True)
        if existing:
            schedule_changed = (
                str(existing.get("schedule_type") or "global") != schedule_type
                or str(existing.get("schedule_value") or "") != schedule_value
            )
            schedule_effective_at = (
                now_stamp
                if schedule_changed
                else existing.get("schedule_effective_at") or existing.get("created_at") or now_stamp
            )
            with get_db() as conn:
                conn.execute(
                    """
                    UPDATE monitors SET target_price=?, trip_type=?, quota_mode=?,
                        airline_rules_json=?, schedule_type=?, schedule_value=?,
                        notification_policy=?, quiet_hours_json=?, next_run_at=?,
                        schedule_effective_at=?
                    WHERE id=?
                    """,
                    (
                        target_price,
                        trip_type,
                        quota_mode,
                        json.dumps(airline_rules or [], ensure_ascii=False),
                        schedule_type,
                        schedule_value,
                        notification_policy,
                        json.dumps(quiet_hours or {}, ensure_ascii=False),
                        next_run_at,
                        schedule_effective_at,
                        existing["id"],
                    ),
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
                adults, trip_type, active, travel_class, created_at,
                quota_mode, airline_rules_json, schedule_type, schedule_value,
                notification_policy, quiet_hours_json, next_run_at,
                schedule_effective_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                route_id,
                outbound_date,
                return_date,
                target_price,
                adults,
                trip_type,
                travel_class,
                now_stamp,
                quota_mode,
                json.dumps(airline_rules or [], ensure_ascii=False),
                schedule_type,
                schedule_value,
                notification_policy,
                json.dumps(quiet_hours or {}, ensure_ascii=False),
                next_run_at,
                now_stamp,
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
            WHERE created_at >= ? AND success=1 AND channel != 'local'
            """,
            (cutoff_24h,),
        ).fetchone()["c"]
        notification_attempts_24h = conn.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE created_at >= ?",
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
            "notification_attempts_24h": int(notification_attempts_24h),
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


def record_api_usage(
    provider: str,
    operation: str,
    success: bool = True,
    detail: str = "",
    created_at: Optional[datetime] = None,
) -> None:
    """Record one billable flight-data request without storing credentials or query parameters."""
    stamp = (created_at or datetime.now()).isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO api_usage (provider, operation, success, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (provider.lower(), operation, 1 if success else 0, detail[:160], stamp),
        )


def get_api_usage_summary(
    provider: str,
    monthly_limit: int = 0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
    period = current.strftime("%Y-%m")
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS successful,
                   SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failed
            FROM api_usage
            WHERE provider=? AND created_at>=? AND created_at<?
            """,
            (provider.lower(), start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
        ).fetchone()
    attempted = int(row["total"] or 0)
    # A timeout or provider error is not proof that a request was free. Treat
    # every network attempt as quota consumption so the guard fails safe near
    # a free-tier limit. The successful/failed split remains diagnostic.
    used = attempted
    limit = max(0, int(monthly_limit))
    return {
        "provider": provider.lower(),
        "period": period,
        "used": used,
        "attempted": attempted,
        "successful": int(row["successful"] or 0),
        "failed": int(row["failed"] or 0),
        "limit": limit,
        "remaining": max(0, limit - used) if limit else None,
        "percent": (used / limit * 100.0) if limit else None,
    }


def claim_api_quota_warning(provider: str, period: str, threshold: int) -> bool:
    """Allow one warning per provider, month, and configured threshold."""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO api_quota_warnings (provider, period, threshold, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (provider.lower(), period, int(threshold), datetime.now().isoformat(timespec="seconds")),
        )
        return cur.rowcount == 1


def release_api_quota_warning(provider: str, period: str, threshold: int) -> None:
    """Release a warning claim after every configured delivery channel fails."""
    with get_db() as conn:
        conn.execute(
            """
            DELETE FROM api_quota_warnings
            WHERE provider=? AND period=? AND threshold=?
            """,
            (provider.lower(), period, int(threshold)),
        )


# ---------------------------------------------------------------------------
# Monitor policies, run ledger, carrier quotes, and reliable notification queue
# ---------------------------------------------------------------------------


def get_monitor(monitor_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT m.*, r.origin, r.destination, r.origin_name, r.destination_name
            FROM monitors m JOIN routes r ON r.id=m.route_id
            WHERE m.id=?
            """,
            (int(monitor_id),),
        ).fetchone()
        return dict(row) if row else None


def update_monitor_policies(
    monitor_id: int,
    *,
    target_price: Optional[float] = None,
    quota_mode: Optional[str] = None,
    airline_rules: Optional[List[Dict[str, Any]]] = None,
    schedule_type: Optional[str] = None,
    schedule_value: Optional[str] = None,
    notification_policy: Optional[str] = None,
    quiet_hours: Optional[Dict[str, Any]] = None,
    next_run_at: Optional[str] = None,
) -> None:
    """Update only supplied policy fields for one monitor."""
    allowed_modes = {"lean", "standard", "full"}
    allowed_schedules = {"global", "fixed_times", "daily", "interval"}
    allowed_notifications = {"always", "target_only", "price_drop", "digest"}
    updates: List[str] = []
    values: List[Any] = []
    if target_price is not None:
        price = float(target_price)
        if price <= 0:
            raise ValueError("target_price must be greater than zero")
        updates.append("target_price=?")
        values.append(price)
    if quota_mode is not None:
        if quota_mode not in allowed_modes:
            raise ValueError("quota_mode must be lean, standard, or full")
        updates.append("quota_mode=?")
        values.append(quota_mode)
    if airline_rules is not None:
        updates.append("airline_rules_json=?")
        values.append(json.dumps(airline_rules, ensure_ascii=False, separators=(",", ":")))
    if schedule_type is not None:
        if schedule_type not in allowed_schedules:
            raise ValueError("unsupported schedule_type")
        updates.append("schedule_type=?")
        values.append(schedule_type)
    if schedule_value is not None:
        updates.append("schedule_value=?")
        values.append(schedule_value.strip())
    if notification_policy is not None:
        if notification_policy not in allowed_notifications:
            raise ValueError("unsupported notification_policy")
        updates.append("notification_policy=?")
        values.append(notification_policy)
    if quiet_hours is not None:
        updates.append("quiet_hours_json=?")
        values.append(json.dumps(quiet_hours, ensure_ascii=False, separators=(",", ":")))
    if next_run_at is not None:
        updates.append("next_run_at=?")
        values.append(next_run_at)
    if not updates:
        return
    with get_db() as conn:
        existing = conn.execute(
            "SELECT schedule_type, schedule_value, schedule_effective_at, created_at "
            "FROM monitors WHERE id=?",
            (int(monitor_id),),
        ).fetchone()
        if not existing:
            raise ValueError(f"monitor {monitor_id} not found")
        if schedule_type is not None or schedule_value is not None:
            next_schedule_type = (
                schedule_type
                if schedule_type is not None
                else str(existing["schedule_type"] or "global")
            )
            next_schedule_value = (
                schedule_value.strip()
                if schedule_value is not None
                else str(existing["schedule_value"] or "")
            )
            if (
                next_schedule_type != str(existing["schedule_type"] or "global")
                or next_schedule_value != str(existing["schedule_value"] or "")
            ):
                updates.append("schedule_effective_at=?")
                values.append(datetime.now().isoformat(timespec="seconds"))
        values.append(int(monitor_id))
        cur = conn.execute(f"UPDATE monitors SET {', '.join(updates)} WHERE id=?", values)
        if cur.rowcount != 1:
            raise ValueError(f"monitor {monitor_id} not found")


def set_monitor_next_run(monitor_id: int, next_run_at: Optional[str], run_id: Optional[str] = None) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE monitors SET next_run_at=?, last_run_id=COALESCE(?, last_run_id) WHERE id=?",
            (next_run_at, run_id, int(monitor_id)),
        )


def _check_run_lease_expired(
    started_at: object,
    *,
    now: Optional[datetime] = None,
    lease_timeout_minutes: int = 30,
) -> bool:
    """Return whether an unfinished run has exceeded its recovery lease."""
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    current = _outbox_time(now)
    started_local = _outbox_time(started)
    return current - started_local >= timedelta(
        minutes=max(5, int(lease_timeout_minutes))
    )


def create_check_run(
    monitor_id: Optional[int],
    *,
    trigger: str = "scheduled",
    scheduled_slot: Optional[str] = None,
    backfill: bool = False,
    started_at: Optional[datetime] = None,
    lease_timeout_minutes: int = 30,
) -> Dict[str, Any]:
    """Create or reclaim one idempotent scheduled run lease.

    A process may die after inserting the ``running`` row.  Once that lease is
    stale, the same row is atomically reclaimed so the unique scheduled-slot
    constraint still prevents concurrent execution without suppressing the
    slot forever.
    """
    current = started_at or datetime.now()
    stamp = current.isoformat(timespec="seconds")
    run_id = str(uuid.uuid4())
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO check_runs (
                id, monitor_id, trigger, scheduled_slot, status, started_at,
                api_calls, backfill, created_at
            ) VALUES (?, ?, ?, ?, 'running', ?, 0, ?, ?)
            """,
            (run_id, monitor_id, trigger, scheduled_slot, stamp, 1 if backfill else 0, stamp),
        )
        if cur.rowcount == 1:
            return {"id": run_id, "created": True, "status": "running"}
        row = conn.execute(
            "SELECT * FROM check_runs WHERE monitor_id=? AND scheduled_slot=?",
            (monitor_id, scheduled_slot),
        ).fetchone()
        if not row:
            raise RuntimeError("run ledger conflict without an existing row")
        if str(row["status"]) == "running" and _check_run_lease_expired(
            row["started_at"],
            now=current,
            lease_timeout_minutes=lease_timeout_minutes,
        ):
            reclaimed = conn.execute(
                """
                UPDATE check_runs
                SET trigger=?, status='running', started_at=?, completed_at=NULL,
                    duration_ms=NULL, api_calls=0, error_stage=NULL,
                    error_message=NULL, backfill=?, result_json=NULL, created_at=?
                WHERE id=? AND status='running' AND started_at=?
                """,
                (
                    trigger,
                    stamp,
                    1 if backfill else 0,
                    stamp,
                    row["id"],
                    row["started_at"],
                ),
            )
            if reclaimed.rowcount == 1:
                return {
                    "id": str(row["id"]),
                    "created": True,
                    "status": "running",
                    "reclaimed": True,
                }
            row = conn.execute(
                "SELECT * FROM check_runs WHERE monitor_id=? AND scheduled_slot=?",
                (monitor_id, scheduled_slot),
            ).fetchone()
        result = dict(row)
        result["created"] = False
        return result


def finish_check_run(
    run_id: str,
    *,
    status: str,
    api_calls: int = 0,
    error_stage: str = "",
    error_message: str = "",
    result: Optional[Dict[str, Any]] = None,
    completed_at: Optional[datetime] = None,
) -> None:
    if status not in {"succeeded", "partial", "failed", "skipped", "missed", "backfilled"}:
        raise ValueError("unsupported run status")
    completed = completed_at or datetime.now()
    with get_db() as conn:
        row = conn.execute("SELECT started_at FROM check_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError(f"run {run_id} not found")
        try:
            started = datetime.fromisoformat(str(row["started_at"]))
            duration_ms = max(0, int((completed - started).total_seconds() * 1000))
        except (TypeError, ValueError):
            duration_ms = None
        conn.execute(
            """
            UPDATE check_runs
            SET status=?, completed_at=?, duration_ms=?, api_calls=?,
                error_stage=?, error_message=?, result_json=?
            WHERE id=?
            """,
            (
                status,
                completed.isoformat(timespec="seconds"),
                duration_ms,
                max(0, int(api_calls)),
                error_stage[:80] or None,
                error_message[:500] or None,
                json.dumps(result or {}, ensure_ascii=False, default=str, separators=(",", ":")),
                run_id,
            ),
        )


def list_check_runs(limit: int = 100, monitor_id: Optional[int] = None) -> List[Dict[str, Any]]:
    with get_db() as conn:
        sql = """
            SELECT cr.*, r.origin, r.destination
            FROM check_runs cr
            LEFT JOIN monitors m ON m.id=cr.monitor_id
            LEFT JOIN routes r ON r.id=m.route_id
        """
        params: List[Any] = []
        if monitor_id is not None:
            sql += " WHERE cr.monitor_id=?"
            params.append(int(monitor_id))
        sql += " ORDER BY cr.started_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def get_check_run(run_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM check_runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def slot_has_run(
    monitor_id: int,
    scheduled_slot: str,
    *,
    now: Optional[datetime] = None,
    lease_timeout_minutes: int = 30,
) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, started_at FROM check_runs "
            "WHERE monitor_id=? AND scheduled_slot=? LIMIT 1",
            (int(monitor_id), scheduled_slot),
        ).fetchone()
        if not row:
            return False
        return not (
            str(row["status"]) == "running"
            and _check_run_lease_expired(
                row["started_at"],
                now=now,
                lease_timeout_minutes=lease_timeout_minutes,
            )
        )


def enqueue_notification(
    *,
    channel: str,
    message: str,
    dedupe_key: str,
    monitor_id: Optional[int] = None,
    run_id: Optional[str] = None,
    max_attempts: int = 4,
    next_attempt_at: Optional[datetime] = None,
    replace_pending: bool = False,
) -> Dict[str, Any]:
    now = _outbox_time()
    due = _outbox_time(next_attempt_at) if next_attempt_at is not None else now
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO notification_outbox (
                run_id, monitor_id, channel, message, status, attempt_count,
                max_attempts, next_attempt_at, dedupe_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                monitor_id,
                channel.lower(),
                message,
                max(1, int(max_attempts)),
                due.isoformat(timespec="seconds"),
                dedupe_key,
                now.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            ),
        )
        if cur.rowcount == 0 and replace_pending:
            conn.execute(
                """
                UPDATE notification_outbox
                SET run_id=?, monitor_id=?, channel=?, message=?, status='pending',
                    attempt_count=0, max_attempts=?, next_attempt_at=?,
                    last_error=NULL, updated_at=?, sent_at=NULL
                WHERE dedupe_key=? AND status IN ('pending', 'retry', 'failed')
                """,
                (
                    run_id,
                    monitor_id,
                    channel.lower(),
                    message,
                    max(1, int(max_attempts)),
                    due.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                    dedupe_key,
                ),
            )
        row = conn.execute(
            "SELECT * FROM notification_outbox WHERE dedupe_key=?",
            (dedupe_key,),
        ).fetchone()
        result = dict(row)
        result["created"] = cur.rowcount == 1
        return result


def claim_due_notifications(
    limit: int = 20,
    now: Optional[datetime] = None,
    *,
    lease_timeout_minutes: int = 10,
) -> List[Dict[str, Any]]:
    """Atomically lease due rows and recover abandoned sending leases."""
    current = _outbox_time(now)
    stamp = current.isoformat(timespec="seconds")
    stale_before = (
        current - timedelta(minutes=max(1, int(lease_timeout_minutes)))
    ).isoformat(timespec="seconds")
    claimed: List[Dict[str, Any]] = []
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE notification_outbox
            SET status='retry', next_attempt_at=?, updated_at=?,
                last_error=COALESCE(last_error, 'delivery lease expired')
            WHERE status='sending' AND updated_at<=?
              AND attempt_count < max_attempts
            """,
            (stamp, stamp, stale_before),
        )
        conn.execute(
            """
            UPDATE notification_outbox
            SET status='failed', updated_at=?,
                last_error=COALESCE(last_error, 'delivery lease expired')
            WHERE status='sending' AND updated_at<=?
              AND attempt_count >= max_attempts
            """,
            (stamp, stale_before),
        )
        rows = conn.execute(
            """
            SELECT * FROM notification_outbox
            WHERE status IN ('pending', 'retry') AND next_attempt_at<=?
              AND attempt_count < max_attempts
            ORDER BY next_attempt_at ASC, id ASC LIMIT ?
            """,
            (stamp, max(1, min(int(limit), 100))),
        ).fetchall()
        for row in rows:
            cur = conn.execute(
                """
                UPDATE notification_outbox
                SET status='sending', attempt_count=attempt_count+1, updated_at=?
                WHERE id=? AND status IN ('pending', 'retry')
                """,
                (stamp, int(row["id"])),
            )
            if cur.rowcount == 1:
                current = conn.execute(
                    "SELECT * FROM notification_outbox WHERE id=?", (int(row["id"]),)
                ).fetchone()
                claimed.append(dict(current))
    return claimed


def complete_outbox_delivery(
    outbox_id: int,
    *,
    success: bool,
    error: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = _outbox_time(now)
    with get_db() as conn:
        row = conn.execute(
            "SELECT attempt_count, max_attempts FROM notification_outbox WHERE id=?",
            (int(outbox_id),),
        ).fetchone()
        if not row:
            raise ValueError(f"outbox item {outbox_id} not found")
        attempts = int(row["attempt_count"])
        max_attempts = int(row["max_attempts"])
        if success:
            status = "sent"
            next_attempt = current
            sent_at = current.isoformat(timespec="seconds")
        elif attempts >= max_attempts:
            status = "failed"
            next_attempt = current
            sent_at = None
        else:
            status = "retry"
            delay_minutes = min(60, 2 ** max(0, attempts - 1) * 5)
            next_attempt = current + timedelta(minutes=delay_minutes)
            sent_at = None
        conn.execute(
            """
            UPDATE notification_outbox
            SET status=?, next_attempt_at=?, last_error=?, updated_at=?, sent_at=?
            WHERE id=?
            """,
            (
                status,
                next_attempt.isoformat(timespec="seconds"),
                error[:500] or None,
                current.isoformat(timespec="seconds"),
                sent_at,
                int(outbox_id),
            ),
        )
        updated = conn.execute("SELECT * FROM notification_outbox WHERE id=?", (int(outbox_id),)).fetchone()
        return dict(updated)


def retry_outbox_delivery(outbox_id: int) -> None:
    now = _outbox_time().isoformat(timespec="seconds")
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE notification_outbox
            SET status='retry', attempt_count=0, next_attempt_at=?,
                last_error=NULL, updated_at=?, sent_at=NULL
            WHERE id=? AND status IN ('failed', 'retry')
            """,
            (now, now, int(outbox_id)),
        )
        if cur.rowcount != 1:
            raise ValueError("only failed or retrying deliveries can be retried")


def list_notification_outbox(limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db() as conn:
        sql = "SELECT * FROM notification_outbox"
        params: List[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def save_carrier_quotes(
    *,
    route_id: int,
    quotes: List[Dict[str, Any]],
    monitor_id: Optional[int] = None,
    run_id: Optional[str] = None,
    recorded_at: Optional[datetime] = None,
) -> int:
    stamp = (recorded_at or datetime.now()).isoformat(timespec="seconds")
    inserted = 0
    with get_db() as conn:
        for quote in quotes:
            price = quote.get("total_price", quote.get("price"))
            if price is None:
                continue
            conn.execute(
                """
                INSERT INTO carrier_quotes (
                    run_id, monitor_id, route_id, carrier_code, carrier_name,
                    total_price, currency, outbound_json, return_json, stops, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    monitor_id,
                    int(route_id),
                    str(quote.get("carrier_code") or "")[:12],
                    str(quote.get("carrier_name") or quote.get("airline") or "")[:120],
                    float(price),
                    str(quote.get("currency") or "TWD")[:8],
                    json.dumps(quote.get("outbound") or {}, ensure_ascii=False, default=str),
                    json.dumps(quote.get("return") or quote.get("return_flight") or {}, ensure_ascii=False, default=str),
                    max(0, int(quote.get("stops") or 0)),
                    stamp,
                ),
            )
            inserted += 1
    return inserted


def list_carrier_quotes(monitor_id: int, limit: int = 200) -> List[Dict[str, Any]]:
    with get_db() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM carrier_quotes
                WHERE monitor_id=? ORDER BY recorded_at DESC LIMIT ?
                """,
                (int(monitor_id), max(1, min(int(limit), 1000))),
            ).fetchall()
        ]
