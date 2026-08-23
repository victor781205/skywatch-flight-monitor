"""Safe backup, monitor import/export, and restore utilities.

The archive format intentionally uses a three-file allowlist.  Configuration and
credential files are never discovered or copied into an export.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from .config import DB_PATH
from .policies import AirlineRuleSet, NotificationPolicy, SchedulePolicy
from .runtime_lock import ProcessLock, lock_owner_alive

PathLike = Union[str, os.PathLike[str]]

ARCHIVE_SCHEMA_VERSION = 1
MONITOR_SCHEMA_VERSION = 1
ARCHIVE_KIND = "skywatch-full-backup"
ARCHIVE_MEMBERS = frozenset({"manifest.json", "database.sqlite", "monitors.json"})
CONFLICT_POLICIES = frozenset({"skip", "update", "duplicate"})
MAX_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 768 * 1024 * 1024

_IATA_RE = re.compile(r"^[A-Z]{3}$")
_SENSITIVE_NAME_RE = re.compile(
    r"(^|[._-])(env|token|secret|secrets|credential|credentials|api[_-]?key)([._-]|$)",
    re.IGNORECASE,
)


class BackupError(RuntimeError):
    """Base exception for backup operations."""


class BackupValidationError(BackupError):
    """Raised when an import or archive is malformed or unsafe."""


class BackupRestoreError(BackupError):
    """Raised when a database restore cannot be completed safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolved(path: PathLike) -> Path:
    return Path(path).expanduser().resolve()


def _fsync_file(path: Path) -> None:
    # Windows requires a writable descriptor for FlushFileBuffers/os.fsync.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _temporary_path(parent: Path, suffix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=".skywatch-", suffix=suffix, dir=str(parent))
    os.close(descriptor)
    return Path(raw_path)


def _validate_sqlite(path: Path) -> None:
    try:
        with closing(sqlite3.connect(str(path), timeout=30.0)) as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupValidationError(f"Invalid SQLite database: {exc}") from exc
    if not result or str(result[0]).lower() != "ok":
        detail = str(result[0]) if result else "no quick_check result"
        raise BackupValidationError(f"SQLite quick_check failed: {detail}")


def snapshot_database(source_db: PathLike = DB_PATH, destination: Optional[PathLike] = None) -> Path:
    """Create a transactionally consistent SQLite snapshot using the backup API.

    The destination is replaced atomically only after ``PRAGMA quick_check`` succeeds.
    """

    source = _resolved(source_db)
    if not source.is_file():
        raise BackupValidationError(f"Database does not exist: {source}")
    if destination is None:
        backup_dir = source.parent / "backups"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = backup_dir / f"snapshot-{stamp}.sqlite"
    else:
        target = _resolved(destination)
    if source == target:
        raise BackupValidationError("Snapshot destination must differ from the live database")

    candidate = _temporary_path(target.parent, ".sqlite.tmp")
    try:
        with closing(sqlite3.connect(str(source), timeout=30.0)) as source_conn:
            with closing(sqlite3.connect(str(candidate), timeout=30.0)) as target_conn:
                source_conn.backup(target_conn)
                target_conn.commit()
        _validate_sqlite(candidate)
        _fsync_file(candidate)
        os.replace(candidate, target)
        return target
    except (BackupError, OSError, sqlite3.Error) as exc:
        candidate.unlink(missing_ok=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"Unable to create database snapshot: {exc}") from exc


def _database_schema_version(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None


def _monitor_document(db_path: PathLike) -> Dict[str, Any]:
    database = _resolved(db_path)
    if not database.is_file():
        raise BackupValidationError(f"Database does not exist: {database}")
    try:
        with closing(sqlite3.connect(str(database), timeout=30.0)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT m.outbound_date, m.return_date, m.target_price, m.adults,
                       m.trip_type, m.active, m.travel_class, m.last_checked,
                       m.last_price, m.created_at, m.quota_mode,
                       m.airline_rules_json, m.schedule_type, m.schedule_value,
                       m.notification_policy, m.quiet_hours_json, m.next_run_at,
                       r.origin, r.destination,
                       r.origin_name, r.destination_name
                FROM monitors m
                JOIN routes r ON r.id=m.route_id
                ORDER BY m.id ASC
                """
            ).fetchall()
            database_schema = _database_schema_version(conn)
    except sqlite3.Error as exc:
        raise BackupValidationError(f"Unable to read monitors: {exc}") from exc

    monitors = [dict(row) for row in rows]
    routes = {(row["origin"], row["destination"]) for row in monitors}
    preview = {
        "monitor_count": len(monitors),
        "active_count": sum(1 for row in monitors if bool(row["active"])),
        "route_count": len(routes),
        "database_schema_version": database_schema,
    }
    return {
        "schema_version": MONITOR_SCHEMA_VERSION,
        "kind": "skywatch-monitors",
        "exported_at": _utc_now(),
        "preview": preview,
        "monitors": monitors,
    }


def _json_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_bytes_atomic(destination: Path, content: bytes, suffix: str) -> None:
    candidate = _temporary_path(destination.parent, suffix)
    try:
        with candidate.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(candidate, destination)
    except OSError:
        candidate.unlink(missing_ok=True)
        raise


def export_monitors(destination: PathLike, db_path: PathLike = DB_PATH) -> Dict[str, Any]:
    """Export portable monitor rules as UTF-8 JSON without credentials."""

    target = _resolved(destination)
    document = _monitor_document(db_path)
    try:
        _write_bytes_atomic(target, _json_bytes(document), ".json.tmp")
    except OSError as exc:
        raise BackupError(f"Unable to export monitors: {exc}") from exc
    return {
        "path": target,
        "schema_version": MONITOR_SCHEMA_VERSION,
        "preview": document["preview"],
    }


def _as_text(value: Any, field: str, maximum: int = 200, allow_empty: bool = True) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BackupValidationError(f"{field} must be text")
    value = value.strip()
    if not allow_empty and not value:
        raise BackupValidationError(f"{field} is required")
    if len(value) > maximum:
        raise BackupValidationError(f"{field} is too long")
    return value


def _as_date(value: Any, field: str, optional: bool = False) -> Optional[str]:
    text = _as_text(value, field, maximum=10, allow_empty=optional)
    if optional and not text:
        return None
    try:
        date.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise BackupValidationError(f"{field} must use YYYY-MM-DD") from exc
    return text


def _as_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        number = int(value)
    elif isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        number = int(value)
    else:
        raise BackupValidationError(f"{field} must be an integer")
    if not minimum <= number <= maximum:
        raise BackupValidationError(f"{field} must be between {minimum} and {maximum}")
    return number


def _as_price(value: Any, field: str, optional: bool = False) -> Optional[float]:
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool):
        raise BackupValidationError(f"{field} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BackupValidationError(f"{field} must be a number") from exc
    if not math.isfinite(number) or number < 0:
        raise BackupValidationError(f"{field} must be a finite non-negative number")
    return number


def _normalise_monitor(raw: Any, index: int) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise BackupValidationError(f"monitors[{index}] must be an object")
    prefix = f"monitors[{index}]"
    origin = _as_text(raw.get("origin"), f"{prefix}.origin", 3, allow_empty=False).upper()
    destination = _as_text(
        raw.get("destination"), f"{prefix}.destination", 3, allow_empty=False
    ).upper()
    if not _IATA_RE.fullmatch(origin) or not _IATA_RE.fullmatch(destination):
        raise BackupValidationError(f"{prefix} origin and destination must be 3-letter IATA codes")
    if origin == destination:
        raise BackupValidationError(f"{prefix} origin and destination must differ")
    outbound_date = _as_date(raw.get("outbound_date"), f"{prefix}.outbound_date")
    return_date = _as_date(raw.get("return_date"), f"{prefix}.return_date", optional=True)
    if return_date and return_date < str(outbound_date):
        raise BackupValidationError(f"{prefix}.return_date cannot precede outbound_date")
    created_at = _as_text(raw.get("created_at"), f"{prefix}.created_at", 64)
    last_checked = _as_text(raw.get("last_checked"), f"{prefix}.last_checked", 64)
    quota_mode = _as_text(raw.get("quota_mode", "full"), f"{prefix}.quota_mode", 16)
    if quota_mode not in {"lean", "standard", "full"}:
        raise BackupValidationError(f"{prefix}.quota_mode is unsupported")
    schedule_type = _as_text(
        raw.get("schedule_type", "global"), f"{prefix}.schedule_type", 24
    )
    if schedule_type not in {"global", "fixed_times", "daily", "interval"}:
        raise BackupValidationError(f"{prefix}.schedule_type is unsupported")
    notification_policy = _as_text(
        raw.get("notification_policy", "always"), f"{prefix}.notification_policy", 24
    )
    if notification_policy not in {"always", "target_only", "price_drop", "digest"}:
        raise BackupValidationError(f"{prefix}.notification_policy is unsupported")
    try:
        airline_rules = json.loads(str(raw.get("airline_rules_json") or "[]"))
        quiet_hours = json.loads(str(raw.get("quiet_hours_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise BackupValidationError(f"{prefix} policy JSON is invalid") from exc
    if not isinstance(airline_rules, list) or not isinstance(quiet_hours, dict):
        raise BackupValidationError(f"{prefix} policy JSON has an invalid shape")
    schedule_value = _as_text(raw.get("schedule_value"), f"{prefix}.schedule_value", 120)
    try:
        AirlineRuleSet.from_dict(airline_rules)
        NotificationPolicy.from_dict(
            {"strategy": notification_policy, "quiet_hours": quiet_hours or None}
        )
        if schedule_type == "fixed_times" and schedule_value:
            SchedulePolicy.from_dict({"mode": "fixed_times", "times": schedule_value})
        elif schedule_type == "daily" and schedule_value:
            SchedulePolicy.from_dict({"mode": "daily", "time": schedule_value})
        elif schedule_type == "interval" and schedule_value:
            SchedulePolicy.from_dict({"mode": "interval", "minutes": int(schedule_value)})
    except (TypeError, ValueError) as exc:
        raise BackupValidationError(f"{prefix} contains an invalid policy: {exc}") from exc
    return {
        "origin": origin,
        "destination": destination,
        "origin_name": _as_text(raw.get("origin_name"), f"{prefix}.origin_name") or origin,
        "destination_name": _as_text(raw.get("destination_name"), f"{prefix}.destination_name")
        or destination,
        "outbound_date": outbound_date,
        "return_date": return_date,
        "target_price": _as_price(raw.get("target_price"), f"{prefix}.target_price"),
        "adults": _as_int(raw.get("adults", 1), f"{prefix}.adults", 1, 9),
        "trip_type": "round_trip" if return_date else "one_way",
        "active": _as_int(raw.get("active", 1), f"{prefix}.active", 0, 1),
        "travel_class": _as_int(raw.get("travel_class", 1), f"{prefix}.travel_class", 1, 4),
        "last_checked": last_checked or None,
        "last_price": _as_price(raw.get("last_price"), f"{prefix}.last_price", optional=True),
        "created_at": created_at or _utc_now(),
        "quota_mode": quota_mode,
        "airline_rules_json": json.dumps(airline_rules, ensure_ascii=False, separators=(",", ":")),
        "schedule_type": schedule_type,
        "schedule_value": schedule_value,
        "notification_policy": notification_policy,
        "quiet_hours_json": json.dumps(quiet_hours, ensure_ascii=False, separators=(",", ":")),
        "next_run_at": _as_text(raw.get("next_run_at"), f"{prefix}.next_run_at", 64) or None,
    }


def _load_monitor_document(source: PathLike) -> Tuple[Dict[str, Any], list[Dict[str, Any]]]:
    path = _resolved(source)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise BackupValidationError(f"Unable to read monitor export: {exc}") from exc
    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupValidationError(f"Monitor export is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise BackupValidationError("Monitor export root must be an object")
    if document.get("schema_version") != MONITOR_SCHEMA_VERSION:
        raise BackupValidationError(
            f"Unsupported monitor schema_version: {document.get('schema_version')!r}"
        )
    if document.get("kind") not in (None, "skywatch-monitors"):
        raise BackupValidationError("Monitor export kind is not supported")
    raw_monitors = document.get("monitors")
    if not isinstance(raw_monitors, list):
        raise BackupValidationError("Monitor export must contain a monitors array")
    monitors = [_normalise_monitor(item, index) for index, item in enumerate(raw_monitors)]
    return document, monitors


def _require_monitor_schema(conn: sqlite3.Connection) -> None:
    required = {
        "routes": {"id", "origin", "destination", "origin_name", "destination_name"},
        "monitors": {
            "id",
            "route_id",
            "outbound_date",
            "return_date",
            "target_price",
            "adults",
            "trip_type",
            "active",
            "travel_class",
            "last_checked",
            "last_price",
            "created_at",
            "quota_mode",
            "airline_rules_json",
            "schedule_type",
            "schedule_value",
            "notification_policy",
            "quiet_hours_json",
            "next_run_at",
        },
    }
    for table, expected in required.items():
        try:
            actual = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
        except sqlite3.Error as exc:
            raise BackupValidationError(f"Unable to inspect {table}: {exc}") from exc
        missing = expected - actual
        if missing:
            raise BackupValidationError(f"Database table {table} is missing: {', '.join(sorted(missing))}")


def _find_conflict(conn: sqlite3.Connection, monitor: Mapping[str, Any]) -> Optional[int]:
    row = conn.execute(
        """
        SELECT m.id
        FROM monitors m JOIN routes r ON r.id=m.route_id
        WHERE r.origin=? AND r.destination=? AND m.outbound_date=?
          AND COALESCE(m.return_date, '')=? AND m.adults=? AND m.travel_class=?
        ORDER BY m.id ASC LIMIT 1
        """,
        (
            monitor["origin"],
            monitor["destination"],
            monitor["outbound_date"],
            monitor["return_date"] or "",
            monitor["adults"],
            monitor["travel_class"],
        ),
    ).fetchone()
    return int(row[0]) if row else None


def preview_monitor_import(source: PathLike, db_path: PathLike = DB_PATH) -> Dict[str, Any]:
    """Validate a monitor JSON export and report conflicts without changing data."""

    document, monitors = _load_monitor_document(source)
    database = _resolved(db_path)
    if not database.is_file():
        raise BackupValidationError(f"Database does not exist: {database}")
    conflicts = 0
    try:
        with closing(sqlite3.connect(str(database), timeout=30.0)) as conn:
            _require_monitor_schema(conn)
            conflicts = sum(1 for monitor in monitors if _find_conflict(conn, monitor) is not None)
    except sqlite3.Error as exc:
        raise BackupValidationError(f"Unable to preview monitor import: {exc}") from exc
    return {
        "schema_version": document["schema_version"],
        "monitor_count": len(monitors),
        "active_count": sum(monitor["active"] for monitor in monitors),
        "conflict_count": conflicts,
        "new_count": len(monitors) - conflicts,
    }


def _restore_point_path(database: Path, restore_dir: Optional[PathLike], operation: str) -> Path:
    directory = _resolved(restore_dir) if restore_dir is not None else database.parent / "backups"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return directory / f"pre-{operation}-{stamp}-{uuid.uuid4().hex[:8]}.sqlite"


def import_monitors(
    source: PathLike,
    db_path: PathLike = DB_PATH,
    conflict: str = "skip",
    restore_dir: Optional[PathLike] = None,
) -> Dict[str, Any]:
    """Import monitor rules with ``skip``, ``update``, or ``duplicate`` conflict handling.

    A consistent restore point is created before the transaction starts.  Any import
    error rolls back the entire transaction.
    """

    policy = str(conflict).lower().strip()
    if policy not in CONFLICT_POLICIES:
        raise BackupValidationError(f"Unsupported conflict policy: {conflict!r}")
    _, monitors = _load_monitor_document(source)
    database = _resolved(db_path)
    restore_point = snapshot_database(
        database, _restore_point_path(database, restore_dir, "monitor-import")
    )
    report: Dict[str, Any] = {
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "duplicates": 0,
        "total": len(monitors),
        "conflict_policy": policy,
        "restore_point": restore_point,
    }
    schedule_effective_at = _utc_now()
    try:
        with closing(sqlite3.connect(str(database), timeout=30.0)) as conn:
            _require_monitor_schema(conn)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            for monitor in monitors:
                conn.execute(
                    """
                    INSERT INTO routes (origin, destination, origin_name, destination_name)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(origin, destination) DO UPDATE SET
                        origin_name=excluded.origin_name,
                        destination_name=excluded.destination_name
                    """,
                    (
                        monitor["origin"],
                        monitor["destination"],
                        monitor["origin_name"],
                        monitor["destination_name"],
                    ),
                )
                route_id = int(
                    conn.execute(
                        "SELECT id FROM routes WHERE origin=? AND destination=?",
                        (monitor["origin"], monitor["destination"]),
                    ).fetchone()[0]
                )
                existing_id = _find_conflict(conn, monitor)
                if existing_id is not None and policy == "skip":
                    report["skipped"] += 1
                    continue
                if existing_id is not None and policy == "update":
                    conn.execute(
                        """
                        UPDATE monitors SET route_id=?, target_price=?, trip_type=?, active=?,
                            last_checked=?, last_price=?, created_at=?, quota_mode=?,
                            airline_rules_json=?, schedule_type=?, schedule_value=?,
                            notification_policy=?, quiet_hours_json=?, next_run_at=?,
                            schedule_effective_at=?
                        WHERE id=?
                        """,
                        (
                            route_id,
                            monitor["target_price"],
                            monitor["trip_type"],
                            monitor["active"],
                            monitor["last_checked"],
                            monitor["last_price"],
                            monitor["created_at"],
                            monitor["quota_mode"],
                            monitor["airline_rules_json"],
                            monitor["schedule_type"],
                            monitor["schedule_value"],
                            monitor["notification_policy"],
                            monitor["quiet_hours_json"],
                            monitor["next_run_at"],
                            schedule_effective_at,
                            existing_id,
                        ),
                    )
                    report["updated"] += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO monitors (
                        route_id, outbound_date, return_date, target_price, adults,
                        trip_type, active, travel_class, last_checked, last_price, created_at
                        , quota_mode, airline_rules_json, schedule_type, schedule_value,
                        notification_policy, quiet_hours_json, next_run_at,
                        schedule_effective_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        route_id,
                        monitor["outbound_date"],
                        monitor["return_date"],
                        monitor["target_price"],
                        monitor["adults"],
                        monitor["trip_type"],
                        monitor["active"],
                        monitor["travel_class"],
                        monitor["last_checked"],
                        monitor["last_price"],
                        monitor["created_at"],
                        monitor["quota_mode"],
                        monitor["airline_rules_json"],
                        monitor["schedule_type"],
                        monitor["schedule_value"],
                        monitor["notification_policy"],
                        monitor["quiet_hours_json"],
                        monitor["next_run_at"],
                        schedule_effective_at,
                    ),
                )
                report["created"] += 1
                if existing_id is not None:
                    report["duplicates"] += 1
            conn.commit()
    except (BackupError, sqlite3.Error, OSError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"Monitor import failed and was rolled back: {exc}") from exc
    return report


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_zip_info(name: str, content: bytes) -> Tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, date_time=datetime.now().timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info, content


def create_full_backup(destination: PathLike, db_path: PathLike = DB_PATH) -> Dict[str, Any]:
    """Create an atomic, credential-free ZIP containing a DB snapshot and monitor JSON."""

    target = _resolved(destination)
    with tempfile.TemporaryDirectory(prefix="skywatch-backup-") as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        snapshot = snapshot_database(db_path, temp_dir / "database.sqlite")
        monitors = _monitor_document(snapshot)
        monitor_bytes = _json_bytes(monitors)
        database_hash = _sha256_file(snapshot)
        monitor_hash = _sha256_bytes(monitor_bytes)
        manifest = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "database_schema_version": monitors["preview"].get("database_schema_version"),
            "kind": ARCHIVE_KIND,
            "created_at": _utc_now(),
            "files": {"database": "database.sqlite", "monitors": "monitors.json"},
            "sha256": {"database.sqlite": database_hash, "monitors.json": monitor_hash},
            "preview": monitors["preview"],
        }
        manifest_bytes = _json_bytes(manifest)
        candidate = _temporary_path(target.parent, ".zip.tmp")
        try:
            with zipfile.ZipFile(candidate, "w") as archive:
                info, content = _safe_zip_info("manifest.json", manifest_bytes)
                archive.writestr(info, content)
                database_info = zipfile.ZipInfo(
                    "database.sqlite", date_time=datetime.now().timetuple()[:6]
                )
                database_info.compress_type = zipfile.ZIP_DEFLATED
                database_info.external_attr = 0o600 << 16
                with snapshot.open("rb") as source, archive.open(database_info, "w") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                info, content = _safe_zip_info("monitors.json", monitor_bytes)
                archive.writestr(info, content)
            _fsync_file(candidate)
            os.replace(candidate, target)
        except (OSError, zipfile.BadZipFile) as exc:
            candidate.unlink(missing_ok=True)
            raise BackupError(f"Unable to create full backup: {exc}") from exc
    return {
        "path": target,
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "preview": manifest["preview"],
        "sha256": _sha256_file(target),
    }


def _validate_archive_member(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not name or "\\" in name or name.startswith(("/", "\\")):
        raise BackupValidationError(f"Unsafe archive path: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or ":" in pure.parts[0]:
        raise BackupValidationError(f"Unsafe archive path: {name!r}")
    if len(pure.parts) != 1:
        raise BackupValidationError(f"Nested archive paths are not allowed: {name!r}")
    if _SENSITIVE_NAME_RE.search(pure.name):
        raise BackupValidationError(f"Sensitive file names are not allowed: {name!r}")
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if (unix_mode & 0o170000) == 0o120000:
        raise BackupValidationError(f"Symbolic links are not allowed: {name!r}")
    if info.flag_bits & 0x1:
        raise BackupValidationError(f"Encrypted archive members are not supported: {name!r}")
    if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
        raise BackupValidationError(f"Archive member is too large: {name!r}")
    return pure.name


def _read_archive_member(archive: zipfile.ZipFile, name: str, expected_size: int) -> bytes:
    with archive.open(name, "r") as handle:
        content = handle.read(expected_size + 1)
    if len(content) != expected_size or len(content) > MAX_ARCHIVE_MEMBER_BYTES:
        raise BackupValidationError(f"Archive member size mismatch: {name!r}")
    return content


def _inspect_open_archive(archive: zipfile.ZipFile) -> Tuple[Dict[str, Any], Dict[str, zipfile.ZipInfo]]:
    infos = archive.infolist()
    names: Dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        name = _validate_archive_member(info)
        if name in names:
            raise BackupValidationError(f"Duplicate archive member: {name!r}")
        names[name] = info
        total += info.file_size
    if total > MAX_ARCHIVE_TOTAL_BYTES:
        raise BackupValidationError("Archive expands beyond the allowed size")
    if set(names) != ARCHIVE_MEMBERS:
        unexpected = sorted(set(names) - ARCHIVE_MEMBERS)
        missing = sorted(ARCHIVE_MEMBERS - set(names))
        detail = []
        if unexpected:
            detail.append(f"unexpected={unexpected}")
        if missing:
            detail.append(f"missing={missing}")
        raise BackupValidationError("Archive member allowlist mismatch: " + ", ".join(detail))

    manifest_bytes = _read_archive_member(
        archive, "manifest.json", names["manifest.json"].file_size
    )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupValidationError(f"Invalid manifest.json: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BackupValidationError("manifest.json must contain an object")
    if manifest.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        raise BackupValidationError(
            f"Unsupported archive schema_version: {manifest.get('schema_version')!r}"
        )
    if manifest.get("kind") != ARCHIVE_KIND:
        raise BackupValidationError("Archive kind is not supported")
    if manifest.get("files") != {"database": "database.sqlite", "monitors": "monitors.json"}:
        raise BackupValidationError("Archive file map is invalid")
    hashes = manifest.get("sha256")
    if not isinstance(hashes, dict):
        raise BackupValidationError("Archive checksums are missing")
    for name in ("database.sqlite", "monitors.json"):
        expected = hashes.get(name)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise BackupValidationError(f"Archive checksum is invalid for {name}")
    monitor_bytes = _read_archive_member(
        archive, "monitors.json", names["monitors.json"].file_size
    )
    if _sha256_bytes(monitor_bytes) != hashes["monitors.json"]:
        raise BackupValidationError("monitors.json checksum mismatch")
    try:
        monitor_document = json.loads(monitor_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupValidationError(f"Invalid monitors.json: {exc}") from exc
    if not isinstance(monitor_document, dict) or monitor_document.get("schema_version") != MONITOR_SCHEMA_VERSION:
        raise BackupValidationError("monitors.json schema is not supported")
    raw_monitors = monitor_document.get("monitors")
    if not isinstance(raw_monitors, list):
        raise BackupValidationError("monitors.json must contain a monitors array")
    for index, monitor in enumerate(raw_monitors):
        _normalise_monitor(monitor, index)
    return manifest, names


def inspect_backup(source: PathLike) -> Dict[str, Any]:
    """Validate an archive allowlist, schema, monitor JSON, and SHA-256 checksums."""

    archive_path = _resolved(source)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            manifest, names = _inspect_open_archive(archive)
            digest = hashlib.sha256()
            with archive.open("database.sqlite", "r") as database_member:
                for block in iter(lambda: database_member.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != manifest["sha256"]["database.sqlite"]:
                raise BackupValidationError("database.sqlite checksum mismatch")
    except BackupError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise BackupValidationError(f"Unable to inspect backup archive: {exc}") from exc
    return {
        "path": archive_path,
        "schema_version": manifest["schema_version"],
        "created_at": manifest.get("created_at"),
        "preview": manifest.get("preview", {}),
        "members": sorted(names),
    }


def _remove_sqlite_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)


def _replace_from_file(source: Path, destination: Path) -> None:
    candidate = _temporary_path(destination.parent, ".sqlite.rollback")
    try:
        with source.open("rb") as input_handle, candidate.open("wb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.replace(candidate, destination)
    except OSError:
        candidate.unlink(missing_ok=True)
        raise


def _read_database_schema_version(database: Path) -> Optional[str]:
    try:
        with closing(sqlite3.connect(str(database), timeout=30.0)) as conn:
            return _database_schema_version(conn)
    except sqlite3.Error as exc:
        raise BackupValidationError(f"Unable to read database schema version: {exc}") from exc


def _database_schema_signature(database: Path) -> Dict[str, Tuple[str, ...]]:
    """Describe application tables exactly enough to reject partial migrations."""

    try:
        with closing(sqlite3.connect(str(database), timeout=30.0)) as conn:
            tables = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            return {
                table: tuple(
                    str(row[1])
                    for row in conn.execute(
                        f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
                    )
                )
                for table in tables
            }
    except sqlite3.Error as exc:
        raise BackupValidationError(f"Unable to inspect database schema: {exc}") from exc


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{escaped}")')}


def _quarantine_restored_work(database: Path) -> None:
    """Prevent historical work from being delivered after a point-in-time restore."""

    timestamp = _utc_now()
    try:
        with closing(sqlite3.connect(str(database), timeout=30.0)) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            conn.execute("BEGIN IMMEDIATE")

            if "check_runs" in tables:
                columns = _table_columns(conn, "check_runs")
                assignments = ["status='failed'"]
                values: list[Any] = []
                if "completed_at" in columns:
                    assignments.append("completed_at=?")
                    values.append(timestamp)
                if "error_stage" in columns:
                    assignments.append("error_stage='restore'")
                if "error_message" in columns:
                    assignments.append("error_message='Cancelled by database restore'")
                if "lease_token" in columns:
                    assignments.append("lease_token=NULL")
                if "lease_expires_at" in columns:
                    assignments.append("lease_expires_at=NULL")
                conn.execute(
                    f"UPDATE check_runs SET {', '.join(assignments)} WHERE status='running'",
                    values,
                )

            if "notification_outbox" in tables:
                columns = _table_columns(conn, "notification_outbox")
                assignments = ["status='cancelled'"]
                values = []
                if "updated_at" in columns:
                    assignments.append("updated_at=?")
                    values.append(timestamp)
                if "last_error" in columns:
                    assignments.append("last_error='Cancelled by database restore'")
                if "lease_token" in columns:
                    assignments.append("lease_token=NULL")
                if "lease_expires_at" in columns:
                    assignments.append("lease_expires_at=NULL")
                conn.execute(
                    f"UPDATE notification_outbox SET {', '.join(assignments)} "
                    "WHERE status IN ('pending', 'retry', 'sending')",
                    values,
                )

            if "monitor_run_leases" in tables:
                conn.execute("DELETE FROM monitor_run_leases")

            if "api_quota_reservations" in tables:
                columns = _table_columns(conn, "api_quota_reservations")
                assignments = ["status='released'"]
                values = []
                if "reconciled_at" in columns:
                    assignments.append("reconciled_at=?")
                    values.append(timestamp)
                if "detail" in columns:
                    assignments.append("detail='Released by database restore'")
                conn.execute(
                    f"UPDATE api_quota_reservations SET {', '.join(assignments)} "
                    "WHERE status='reserved'",
                    values,
                )

            if "monitors" in tables:
                columns = _table_columns(conn, "monitors")
                assignments = []
                values = []
                if "next_run_at" in columns:
                    assignments.append("next_run_at=NULL")
                if "schedule_effective_at" in columns:
                    assignments.append("schedule_effective_at=?")
                    values.append(timestamp)
                if assignments:
                    conn.execute(f"UPDATE monitors SET {', '.join(assignments)}", values)
            conn.commit()
    except sqlite3.Error as exc:
        raise BackupRestoreError(f"Unable to quarantine restored work: {exc}") from exc


def restore_full_backup(
    source: PathLike,
    db_path: PathLike = DB_PATH,
    restore_dir: Optional[PathLike] = None,
) -> Dict[str, Any]:
    """Restore a validated full backup after automatically snapshotting current data.

    The candidate database is checksum- and SQLite-validated before atomic replacement.
    If a post-replacement validation fails, the pre-restore snapshot is copied back.
    """

    archive_path = _resolved(source)
    database = _resolved(db_path)
    if not database.is_file():
        raise BackupValidationError(f"Database does not exist: {database}")
    candidate = _temporary_path(database.parent, ".sqlite.restore")
    manifest: Dict[str, Any]
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            manifest, names = _inspect_open_archive(archive)
            database_info = names["database.sqlite"]
            digest = hashlib.sha256()
            written = 0
            with archive.open(database_info, "r") as input_handle, candidate.open("wb") as output_handle:
                while True:
                    block = input_handle.read(1024 * 1024)
                    if not block:
                        break
                    written += len(block)
                    if written > database_info.file_size or written > MAX_ARCHIVE_MEMBER_BYTES:
                        raise BackupValidationError("database.sqlite expands beyond its declared size")
                    digest.update(block)
                    output_handle.write(block)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            if written != database_info.file_size:
                raise BackupValidationError("database.sqlite size mismatch")
            if digest.hexdigest() != manifest["sha256"]["database.sqlite"]:
                raise BackupValidationError("database.sqlite checksum mismatch")
        _validate_sqlite(candidate)
        try:
            with closing(sqlite3.connect(str(candidate), timeout=30.0)) as conn:
                _require_monitor_schema(conn)
        except sqlite3.Error as exc:
            raise BackupValidationError(f"Backup database schema is invalid: {exc}") from exc
    except BackupError:
        candidate.unlink(missing_ok=True)
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        candidate.unlink(missing_ok=True)
        raise BackupValidationError(f"Unable to prepare backup restore: {exc}") from exc

    candidate_schema = _read_database_schema_version(candidate)
    if candidate_schema is None:
        candidate.unlink(missing_ok=True)
        raise BackupValidationError("Backup database schema version is missing")
    declared_schema = manifest.get("database_schema_version")
    if declared_schema is None:
        declared_schema = (manifest.get("preview") or {}).get("database_schema_version")
    if declared_schema is not None and str(declared_schema) != candidate_schema:
        candidate.unlink(missing_ok=True)
        raise BackupValidationError("Backup database schema version does not match its manifest")

    maintenance_lock = ProcessLock(database.parent / "maintenance.lock", "backup_restore")
    if not maintenance_lock.acquire():
        candidate.unlink(missing_ok=True)
        raise BackupRestoreError("Another maintenance or restore operation is already active")

    restore_point: Optional[Path] = None
    replaced = False
    try:
        active_workers = [
            name
            for name in ("scheduler.lock", "telegram_bot.lock")
            if lock_owner_alive(database.parent / name)
        ]
        if active_workers:
            raise BackupRestoreError(
                "Stop active workers before restore: " + ", ".join(active_workers)
            )

        live_schema = _read_database_schema_version(database)
        if live_schema is None or live_schema != candidate_schema:
            raise BackupValidationError(
                f"Database schema mismatch: live={live_schema!r}, backup={candidate_schema!r}"
            )
        if _database_schema_signature(database) != _database_schema_signature(candidate):
            raise BackupValidationError(
                "Database schema mismatch: backup tables or columns differ from the live schema"
            )

        restore_point = snapshot_database(
            database, _restore_point_path(database, restore_dir, "full-restore")
        )
        os.replace(candidate, database)
        replaced = True
        _remove_sqlite_sidecars(database)
        _validate_sqlite(database)
        _quarantine_restored_work(database)
        _validate_sqlite(database)
    except (BackupError, OSError, sqlite3.Error) as exc:
        candidate.unlink(missing_ok=True)
        rollback_error: Optional[Exception] = None
        if replaced and restore_point is not None:
            try:
                _replace_from_file(restore_point, database)
                _remove_sqlite_sidecars(database)
                _validate_sqlite(database)
            except (BackupError, OSError) as rollback_exc:
                rollback_error = rollback_exc
        detail = f"Restore failed: {exc}"
        if rollback_error is not None:
            detail += f"; automatic rollback also failed: {rollback_error}"
        raise BackupRestoreError(detail) from exc
    finally:
        candidate.unlink(missing_ok=True)
        maintenance_lock.release()
    assert restore_point is not None
    return {
        "path": database,
        "source": archive_path,
        "restore_point": restore_point,
        "schema_version": manifest["schema_version"],
        "preview": manifest.get("preview", {}),
    }


# Explicit aliases keep integrations readable without duplicating behaviour.
create_sqlite_snapshot = snapshot_database
export_monitors_json = export_monitors
import_monitors_json = import_monitors
create_backup_archive = create_full_backup
restore_backup_archive = restore_full_backup


__all__ = [
    "ARCHIVE_SCHEMA_VERSION",
    "MONITOR_SCHEMA_VERSION",
    "CONFLICT_POLICIES",
    "BackupError",
    "BackupValidationError",
    "BackupRestoreError",
    "snapshot_database",
    "create_sqlite_snapshot",
    "export_monitors",
    "export_monitors_json",
    "preview_monitor_import",
    "import_monitors",
    "import_monitors_json",
    "create_full_backup",
    "create_backup_archive",
    "inspect_backup",
    "restore_full_backup",
    "restore_backup_archive",
]
