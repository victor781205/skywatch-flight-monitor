"""
背景監控排程
用法：
  python scheduler.py
環境變數：
  SCHEDULE_HOURS=0,12        (預設每日 00:00 與 12:00 固定執行)
  CHECK_INTERVAL_MINUTES=60  (若顯式設定此項，則改用間隔模式)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from src import database as db
from src.backup import create_full_backup, inspect_backup
from src.config import DB_PATH, MISSED_RUN_LOOKBACK_HOURS, SCHEDULER_TICK_MINUTES
from src.monitor import check_all_monitors
from src.notifier import process_notification_outbox, send_all_reliable
from src.policies import ScheduleMode, SchedulePolicy
from src.runtime_lock import ProcessLock, maintenance_active

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DISPATCH_LOCK_FILE = DATA_DIR / "scheduler-dispatch.lock"
STATUS_FILE = DATA_DIR / "scheduler_status.json"
_STATUS_WRITE_LOCK = threading.Lock()
_AUTO_BACKUP_LAST_TARGET: Optional[Path] = None
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def _now() -> datetime:
    return datetime.now(TAIPEI_TZ)


def _next_aligned_supervisor_tick(
    now: Optional[datetime] = None,
    tick_minutes: Optional[int] = None,
) -> datetime:
    """Return the next wall-clock boundary for the supervisor interval.

    APScheduler intervals otherwise inherit the service start offset.  A
    process started at 11:49:25 would consequently tick at 11:59:25 and
    12:04:25, delaying a 12:00 monitor until the latter tick.  Anchoring the
    interval to epoch-aligned minute boundaries keeps 5-minute supervision on
    :00, :05, :10, ... regardless of restarts.
    """

    current = (now or _now()).astimezone(TAIPEI_TZ)
    minutes = max(
        1,
        int(SCHEDULER_TICK_MINUTES if tick_minutes is None else tick_minutes),
    )
    interval_seconds = minutes * 60
    next_epoch = ((int(current.timestamp()) // interval_seconds) + 1) * interval_seconds
    return datetime.fromtimestamp(next_epoch, tz=TAIPEI_TZ)


def _write_status(**updates) -> None:
    """Persist a machine-readable heartbeat without exposing secrets."""
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # A fixed ``scheduler_status.tmp`` lets two close writes replace/unlink the
    # same temporary file.  One in-process mutex plus a unique same-directory
    # file keeps read/merge/replace atomic for the service and for Windows.
    with _STATUS_WRITE_LOCK:
        current = {}
        try:
            current = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            pass
        current.update(updates)
        current["updated_at"] = _now().isoformat(timespec="seconds")
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(STATUS_FILE.parent),
                prefix=f".{STATUS_FILE.name}.{os.getpid()}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                json.dump(current, temp_file, ensure_ascii=False, indent=2)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, STATUS_FILE)
            temp_path = None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass


def log_message(msg: str):
    timestamp = _now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{timestamp}] {msg}"
    try:
        print(full_msg, flush=True)
    except UnicodeEncodeError:
        # Traditional Windows consoles commonly use CP950 and cannot encode
        # every status symbol. Console rendering must never abort a monitor job.
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_message = full_msg.encode(encoding, errors="replace").decode(encoding)
        print(safe_message, flush=True)
    log_file = ROOT / "data" / "scheduler.log"
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if log_file.exists() and log_file.stat().st_size > 1_000_000:
            rotated = log_file.with_suffix(".log.1")
            if rotated.exists():
                rotated.unlink()
            log_file.replace(rotated)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(full_msg + "\n")
    except Exception:
        pass


def _slot_key(moment: datetime) -> str:
    return moment.astimezone(TAIPEI_TZ).isoformat(timespec="minutes")


def _schedule_moment(value: object) -> Optional[datetime]:
    """Parse persisted local/offset timestamps into the scheduler timezone."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TAIPEI_TZ)
    return parsed.astimezone(TAIPEI_TZ)


def _latest_run_status() -> dict[str, object]:
    """Build user-facing batch metrics from the durable run ledger."""

    terminal = {
        "succeeded",
        "partial",
        "failed",
        "skipped",
        "missed",
        "backfilled",
        "cancelled",
    }
    runs = [
        run
        for run in db.list_check_runs(limit=200)
        if str(run.get("status") or "") in terminal and run.get("completed_at")
    ]
    if not runs:
        return {}

    latest = runs[0]
    slot = str(latest.get("scheduled_slot") or "")
    batch = (
        [run for run in runs if str(run.get("scheduled_slot") or "") == slot]
        if slot
        else [latest]
    )
    starts = [
        moment
        for moment in (_schedule_moment(run.get("started_at")) for run in batch)
        if moment is not None
    ]
    completions = [
        moment
        for moment in (_schedule_moment(run.get("completed_at")) for run in batch)
        if moment is not None
    ]
    if not completions:
        return {}

    results: list[dict] = []
    for run in batch:
        raw_result = run.get("result_json")
        if isinstance(raw_result, dict):
            results.append(raw_result)
            continue
        try:
            parsed = json.loads(str(raw_result or "{}"))
        except (TypeError, ValueError):
            parsed = {}
        results.append(parsed if isinstance(parsed, dict) else {})

    completed_at = max(completions)
    started_at = min(starts) if starts else completed_at
    successful = sum(
        1 for run in batch if str(run.get("status") or "") in {"succeeded", "backfilled"}
    )
    status: dict[str, object] = {
        "state": "healthy",
        "last_started_at": started_at.isoformat(timespec="seconds"),
        "last_completed_at": completed_at.isoformat(timespec="seconds"),
        "last_result_count": len(batch),
        "last_notification_count": sum(1 for result in results if result.get("notified")),
        "last_backfill": any(bool(run.get("backfill")) for run in batch),
        "last_hit_count": sum(1 for result in results if result.get("hit")),
        "last_run_duration": max(0.0, (completed_at - started_at).total_seconds()),
        "last_success_rate": successful / len(batch),
        "last_checked_count": len(batch),
        "last_error": None,
    }
    if slot:
        status["last_slot"] = slot
    return status


def _global_schedule(hours_env: str, interval_env: str) -> SchedulePolicy:
    if interval_env:
        return SchedulePolicy.from_dict(
            {"mode": "interval", "interval_minutes": max(5, int(interval_env))}
        )
    hours = sorted({int(value.strip()) for value in hours_env.split(",") if value.strip()})
    return SchedulePolicy.from_dict(
        {"mode": "fixed_times", "fixed_times": [f"{hour:02d}:00" for hour in hours]}
    )


def _monitor_schedule(monitor: dict, fallback: SchedulePolicy) -> SchedulePolicy:
    schedule_type = str(monitor.get("schedule_type") or "global")
    schedule_value = str(monitor.get("schedule_value") or "").strip()
    if schedule_type == "global" or not schedule_value:
        return fallback
    if schedule_type == "fixed_times":
        return SchedulePolicy.from_dict({"mode": "fixed_times", "times": schedule_value})
    if schedule_type == "daily":
        return SchedulePolicy.from_dict({"mode": "daily", "time": schedule_value})
    if schedule_type == "interval":
        return SchedulePolicy.from_dict({"mode": "interval", "minutes": int(schedule_value)})
    return fallback


def _scheduled_slots(
    policy: SchedulePolicy,
    start: datetime,
    end: datetime,
    *,
    interval_anchor: Optional[datetime] = None,
) -> list[datetime]:
    if end < start:
        return []
    if policy.mode == ScheduleMode.INTERVAL:
        interval = timedelta(minutes=policy.interval_minutes)
        candidate = (interval_anchor or (start + interval)).astimezone(TAIPEI_TZ)
        if candidate < start:
            elapsed = start - candidate
            candidate += interval * int(elapsed // interval)
            if candidate < start:
                candidate += interval
        slots: list[datetime] = []
        while candidate <= end and len(slots) < 1000:
            slots.append(candidate)
            candidate += interval
        return slots

    cursor = start - timedelta(seconds=1)
    slots: list[datetime] = []
    guard = 0
    while guard < 1000:
        candidate = policy.next_run(cursor)
        if candidate > end:
            break
        slots.append(candidate)
        cursor = candidate
        guard += 1
    return slots


def _monitor_slots(
    monitor: dict,
    fallback: SchedulePolicy,
    start: datetime,
    end: datetime,
) -> list[datetime]:
    policy = _monitor_schedule(monitor, fallback)
    effective = _schedule_moment(
        monitor.get("schedule_effective_at") or monitor.get("created_at")
    )
    if effective is not None and effective > start:
        start = effective
    if end < start:
        return []
    if policy.mode != ScheduleMode.INTERVAL:
        return _scheduled_slots(policy, start, end)

    # ``next_run_at`` is the durable interval anchor. Older records are
    # bootstrapped from creation time so the grid cannot shift on each tick.
    anchor = _schedule_moment(monitor.get("next_run_at"))
    if anchor is None:
        effective = _schedule_moment(
            monitor.get("schedule_effective_at") or monitor.get("created_at")
        ) or start
        anchor = effective + timedelta(minutes=policy.interval_minutes)
    return _scheduled_slots(policy, start, end, interval_anchor=anchor)


def _next_monitor_run(
    monitor: dict,
    fallback: SchedulePolicy,
    *,
    after: datetime,
) -> datetime:
    policy = _monitor_schedule(monitor, fallback)
    current = after.astimezone(TAIPEI_TZ)
    if policy.mode != ScheduleMode.INTERVAL:
        return policy.next_run(current)

    interval = timedelta(minutes=policy.interval_minutes)
    candidate = _schedule_moment(monitor.get("next_run_at"))
    if candidate is None:
        effective = _schedule_moment(
            monitor.get("schedule_effective_at") or monitor.get("created_at")
        ) or current
        candidate = effective + interval
    if candidate <= current:
        elapsed = current - candidate
        candidate += interval * (int(elapsed // interval) + 1)
    return candidate


def _persist_next_monitor_run(
    monitor: dict,
    fallback: SchedulePolicy,
    *,
    after: datetime,
) -> str:
    value = _next_monitor_run(monitor, fallback, after=after).isoformat(timespec="seconds")
    if str(monitor.get("next_run_at") or "") != value:
        db.set_monitor_next_run(int(monitor["id"]), value)
        monitor["next_run_at"] = value
    return value


def _next_due_monitor_time(
    fallback: SchedulePolicy, *, now: Optional[datetime] = None
) -> datetime:
    """Return the earliest durable monitor slot for heartbeat reporting."""
    current = (now or _now()).astimezone(TAIPEI_TZ)
    monitors = db.list_monitors(active_only=True)
    candidates = [
        _next_monitor_run(monitor, fallback, after=current) for monitor in monitors
    ]
    return min(candidates) if candidates else fallback.next_run(current)


def _mark_missed_slot(monitor_id: int, slot: datetime, *, now: datetime) -> None:
    """Close an older missed slot so it is audited but not backfilled later."""
    run = db.create_check_run(
        monitor_id,
        trigger="missed",
        scheduled_slot=_slot_key(slot),
        started_at=now,
    )
    if run.get("created"):
        db.finish_check_run(
            str(run["id"]),
            status="missed",
            error_stage="scheduler",
            error_message="superseded by latest-slot backfill",
            result={"scheduled_slot": _slot_key(slot)},
            completed_at=now,
        )


def detect_missed_slots(
    monitor: dict,
    fallback: SchedulePolicy,
    *,
    now: Optional[datetime] = None,
    lookback_hours: int = MISSED_RUN_LOOKBACK_HOURS,
) -> list[datetime]:
    """Return past due slots without a ledger row, newest first."""
    current = (now or _now()).astimezone(TAIPEI_TZ)
    start = current - timedelta(hours=max(1, int(lookback_hours)))
    # Legacy releases updated ``last_checked`` without a run-ledger row.  On
    # the first 3.x restart, treating those already-fresher checks as missed
    # would spend quota and send a duplicate report during migration.  Limit
    # this bridge to monitors that have never recorded a v3 run: a later
    # manual check sets ``last_run_id`` and must not mask a missed schedule.
    legacy_last_checked = None
    if not monitor.get("last_run_id"):
        legacy_last_checked = _schedule_moment(monitor.get("last_checked"))
    missing = [
        slot
        for slot in _monitor_slots(monitor, fallback, start, current)
        if not db.slot_has_run(int(monitor["id"]), _slot_key(slot))
        and not (legacy_last_checked is not None and legacy_last_checked >= slot)
    ]
    return list(reversed(missing))


def _send_scheduler_incident(title: str, detail: str, incident_key: str) -> None:
    """Queue a durable scheduler incident without letting alerts stop work."""
    try:
        send_all_reliable(
            f"⚠️ SkyWatch 排程異常\n{title}\n{detail}\n時間：{_now().strftime('%Y-%m-%d %H:%M')}（台北）",
            purpose=f"scheduler:{incident_key}",
            dedupe_scope=incident_key,
        )
    except Exception as exc:
        # A database/network outage may affect both the fare pipeline and the
        # incident channel.  Preserve the original failure and let the durable
        # supervisor try the outbox again on its next tick.
        log_message(f"排程異常通知建立失敗：{type(exc).__name__}")


def _retry_policy() -> tuple[int, float]:
    """Return a deliberately small transient-failure retry budget.

    One retry is enough to absorb a short provider timeout without allowing a
    bad upstream to consume the free monthly quota in a tight loop.  Operators
    may tune it, but hard caps remain enforced here.
    """

    try:
        retries = int(os.getenv("SCHEDULER_SHORT_RETRIES", "1"))
    except ValueError:
        retries = 1
    try:
        delay = float(os.getenv("SCHEDULER_RETRY_DELAY_SECONDS", "30"))
    except ValueError:
        delay = 30.0
    return min(2, max(0, retries)), min(120.0, max(1.0, delay))


def _auto_backup_settings() -> tuple[bool, int, int, int]:
    enabled = os.getenv("AUTO_BACKUP_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    raw_hour = os.getenv("AUTO_BACKUP_HOUR", "03:30").strip() or "03:30"
    try:
        if ":" in raw_hour:
            hour_text, minute_text = raw_hour.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        else:
            hour, minute = int(raw_hour), 0
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
    except ValueError:
        hour, minute = 3, 30
    try:
        retention = int(os.getenv("AUTO_BACKUP_RETENTION_DAYS", "14"))
    except ValueError:
        retention = 14
    return enabled, hour, minute, min(365, max(1, retention))


def _auto_backup_date(path: Path):
    name = path.name
    prefix, suffix = "skywatch-auto-", ".zip"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    stamp = name[len(prefix) : -len(suffix)]
    if len(stamp) != 8 or not stamp.isdigit():
        return None
    try:
        return datetime.strptime(stamp, "%Y%m%d").date()
    except ValueError:
        return None


def _prune_auto_backups(
    backup_dir: Path,
    *,
    current: datetime,
    retention_days: int,
    newest_valid: Path,
) -> list[Path]:
    """Delete only verified, expired auto archives and always keep the newest."""

    valid: list[tuple[Path, object]] = []
    for path in backup_dir.glob("skywatch-auto-*.zip"):
        backup_date = _auto_backup_date(path)
        if backup_date is None:
            continue
        if path != newest_valid:
            try:
                inspect_backup(path)
            except Exception:
                # Unknown/corrupt files are left for explicit operator review;
                # retention must never guess and delete the latest valid copy.
                continue
        valid.append((path, backup_date))
    if not valid:
        return []
    protected = max(valid, key=lambda item: (item[1], item[0].name))[0]
    cutoff = current.date() - timedelta(days=max(1, retention_days) - 1)
    removed: list[Path] = []
    for path, backup_date in valid:
        if path == protected or backup_date >= cutoff:
            continue
        try:
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log_message(f"舊自動備份清理失敗：{path.name} ({type(exc).__name__})")
    return removed


def _maybe_create_auto_backup(
    *,
    now: Optional[datetime] = None,
    backup_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Create at most one credential-free full backup per Taipei calendar day."""

    global _AUTO_BACKUP_LAST_TARGET
    enabled, hour, minute, retention = _auto_backup_settings()
    current = (now or _now()).astimezone(TAIPEI_TZ)
    if not enabled:
        return {"state": "disabled"}
    if (current.hour, current.minute) < (hour, minute):
        return {"state": "not_due"}

    directory = Path(backup_dir) if backup_dir is not None else DATA_DIR / "backups"
    database = Path(db_path) if db_path is not None else DB_PATH
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"skywatch-auto-{current.strftime('%Y%m%d')}.zip"
    resolved_target = target.resolve()
    if _AUTO_BACKUP_LAST_TARGET == resolved_target and target.is_file():
        return {"state": "current", "path": str(target), "removed": 0}
    if target.is_file():
        try:
            inspect_backup(target)
            removed = _prune_auto_backups(
                directory,
                current=current,
                retention_days=retention,
                newest_valid=target,
            )
            _AUTO_BACKUP_LAST_TARGET = resolved_target
            return {"state": "current", "path": str(target), "removed": len(removed)}
        except Exception:
            # Atomic create_full_backup safely replaces an invalid same-day
            # artifact; older valid backups remain untouched until it succeeds.
            pass

    report = create_full_backup(target, database)
    removed = _prune_auto_backups(
        directory,
        current=current,
        retention_days=retention,
        newest_valid=target,
    )
    _AUTO_BACKUP_LAST_TARGET = resolved_target
    return {
        "state": "created",
        "path": str(report.get("path") or target),
        "removed": len(removed),
    }


def _retry_slot(slot: str, attempt: int) -> str:
    """Give retries an auditable ledger key without reopening the failed row."""

    return f"{slot}#retry-{attempt}"


def _result_failure(results: list[dict]) -> Optional[str]:
    """Return a retryable pipeline/API failure summary, if present."""

    failures = [
        result
        for result in results
        if not result.get("ok") and not result.get("skipped")
    ]
    if not failures:
        return None
    first = failures[0]
    stage = str(first.get("error_stage") or "flight_search")
    detail = str(first.get("error") or "unknown provider error").strip()
    return f"{stage}: {detail}"[:360]


def _check_monitor_with_retry(
    monitor: dict,
    *,
    scheduled_slot: str,
    backfill: bool,
    heartbeat: Optional[Callable[..., None]] = None,
    announce_recovery: bool = True,
) -> list[dict]:
    """Run one scheduled monitor and briefly retry transient pipeline errors.

    Each retry has its own ledger suffix because the original slot is already
    terminal once ``check_one_monitor`` records a provider failure.  Only this
    invocation can emit its recovery message, so a second scheduler that loses
    process ownership cannot misreport another process as recovered.
    """

    monitor_id = int(monitor["id"])
    route = f"{monitor.get('origin', '?')} → {monitor.get('destination', '?')}"
    retries, base_delay = _retry_policy()
    failure_announced = False
    last_results: list[dict] = []
    last_error = ""

    for attempt in range(retries + 1):
        attempt_slot = (
            scheduled_slot if attempt == 0 else _retry_slot(scheduled_slot, attempt)
        )
        if heartbeat is not None:
            heartbeat(
                "monitor_attempt",
                monitor_id=monitor_id,
                attempt=attempt + 1,
                total_attempts=retries + 1,
            )
        try:
            results = check_all_monitors(
                scheduled_report=True,
                scheduled_slot=attempt_slot,
                backfill=backfill,
                monitor_ids=[monitor_id],
            )
            last_results = results
            last_error = _result_failure(results) or ""
            # An empty result means the monitor expired or was disabled between
            # listing and dispatch; a quota skip is intentional, not a provider
            # retry signal.
            if not last_error:
                if failure_announced and announce_recovery:
                    _send_scheduler_incident(
                        "查價服務已恢復",
                        f"監控 #{monitor_id} · {route}\n排程時段：{scheduled_slot}\n第 {attempt + 1} 次嘗試成功",
                        f"pipeline-recovered:{monitor_id}:{scheduled_slot}",
                    )
                return results
        except Exception as exc:
            last_results = []
            last_error = f"monitor_pipeline: {type(exc).__name__}: {exc}"[:360]

        if not failure_announced:
            _send_scheduler_incident(
                "查價管線失敗，將進行短期重試",
                f"監控 #{monitor_id} · {route}\n排程時段：{scheduled_slot}\n原因：{last_error}",
                f"pipeline-failed:{monitor_id}:{scheduled_slot}",
            )
            failure_announced = True

        if attempt < retries:
            delay = min(120.0, base_delay * (attempt + 1))
            if heartbeat is not None:
                heartbeat(
                    "retry_wait",
                    monitor_id=monitor_id,
                    attempt=attempt + 2,
                    retry_in_seconds=delay,
                )
            time.sleep(delay)

    _send_scheduler_incident(
        "查價短期重試仍失敗",
        f"監控 #{monitor_id} · {route}\n排程時段：{scheduled_slot}\n原因：{last_error}",
        f"pipeline-exhausted:{monitor_id}:{scheduled_slot}",
    )
    if last_results:
        return last_results
    return [
        {
            "monitor_id": monitor_id,
            "ok": False,
            "error": last_error,
            "error_stage": "scheduler_retry",
            "notified": False,
        }
    ]


def recover_missed_runs(
    fallback: SchedulePolicy,
    *,
    now: Optional[datetime] = None,
    heartbeat: Optional[Callable[..., None]] = None,
) -> list[dict]:
    """Backfill at most one latest missed slot per monitor to protect quota."""
    recovered: list[dict] = []
    current = (now or _now()).astimezone(TAIPEI_TZ)
    incident_grace = timedelta(minutes=max(2, SCHEDULER_TICK_MINUTES * 2))
    monitors = db.list_monitors(active_only=True)
    total = len(monitors)
    for index, monitor in enumerate(monitors, start=1):
        if heartbeat is not None:
            heartbeat(
                "monitor_batch",
                monitor_id=int(monitor["id"]),
                batch_index=index,
                batch_total=total,
            )
        missed = detect_missed_slots(monitor, fallback, now=current)
        if not missed:
            _persist_next_monitor_run(monitor, fallback, after=current)
            continue
        latest = missed[0]
        for superseded in missed[1:]:
            _mark_missed_slot(int(monitor["id"]), superseded, now=current)
        key = _slot_key(latest)
        is_backfill = current - latest > incident_grace
        if is_backfill:
            _send_scheduler_incident(
                "偵測到排程漏跑，正在安全補跑",
                f"監控 #{monitor['id']} · {monitor['origin']} → {monitor['destination']}\n漏跑時段：{key}",
                f"missed:{monitor['id']}:{key}",
            )
        results = _check_monitor_with_retry(
            monitor,
            scheduled_slot=key,
            backfill=is_backfill,
            heartbeat=heartbeat,
            announce_recovery=not is_backfill,
        )
        recovered.extend(results)
        _persist_next_monitor_run(monitor, fallback, after=current)
        ok = bool(results and results[0].get("ok"))
        if is_backfill:
            _send_scheduler_incident(
                "排程補跑已恢復" if ok else "排程補跑失敗",
                f"監控 #{monitor['id']} · {key} · {'完成' if ok else '請查看執行紀錄'}",
                f"recovery:{monitor['id']}:{key}:{'ok' if ok else 'failed'}",
            )
    return recovered


def job(
    *,
    scheduled_slot: Optional[str] = None,
    backfill: bool = False,
    fallback_policy: Optional[SchedulePolicy] = None,
):
    if maintenance_active(DATA_DIR):
        log_message("略過本次查價：系統正在備份或復原維護。")
        return []
    dispatch_lock = ProcessLock(DISPATCH_LOCK_FILE, "scheduler-dispatch")
    if not dispatch_lock.acquire():
        log_message("略過本次查價：另一個排程工作仍在執行。")
        # A losing process must not overwrite the real owner's heartbeat; that
        # previously caused a false watchdog incident/recovery pair.
        return []
    log_message("開始執行定時查價監控…")
    _write_status(state="running", last_started_at=_now().isoformat(timespec="seconds"), last_error=None)
    results: list[dict] = []
    try:
        slot = scheduled_slot or _slot_key(_now().replace(second=0, microsecond=0))
        slot_moment = _schedule_moment(slot) or _now()
        fallback = fallback_policy or _global_schedule(
            os.getenv("SCHEDULE_HOURS", "0,12").strip() or "0,12",
            os.getenv("CHECK_INTERVAL_MINUTES", "").strip(),
        )
        # The global trigger only owns monitors using the global policy.
        # Custom policies are dispatched from their exact durable slots by the
        # supervisor, so an unrelated 00:00/12:00 job cannot query them.
        due_monitors = [
            monitor
            for monitor in db.list_monitors(active_only=True)
            if str(monitor.get("schedule_type") or "global") == "global"
            or not str(monitor.get("schedule_value") or "").strip()
        ]
        for monitor in due_monitors:
            results.extend(
                _check_monitor_with_retry(
                    monitor,
                    scheduled_slot=slot,
                    backfill=backfill,
                )
            )
        for monitor in due_monitors:
            _persist_next_monitor_run(monitor, fallback, after=slot_moment)
        for r in results:
            if not r.get("ok"):
                log_message(f"  ❌ #{r.get('monitor_id')} 失敗: {r.get('error')}")
            else:
                flag = "🎯 達標" if r.get("hit") else "OK"
                log_message(
                    f"  ✅ #{r.get('monitor_id')} 現價=TWD {r.get('best_price')} "
                    f"目標=TWD {r.get('target_price')} [{flag}] "
                    f"notified={r.get('notified')} reason={r.get('notify_reason')}"
                )
        log_message(f"查價完成，共檢查 {len(results)} 筆監控。")
        _write_status(
            state="healthy",
            last_completed_at=_now().isoformat(timespec="seconds"),
            last_result_count=len(results),
            last_notification_count=sum(1 for result in results if result.get("notified")),
            last_slot=slot,
            last_backfill=bool(backfill),
            last_error=None,
        )
        return results
    except Exception as e:
        log_message(f"❌ 排程執行錯誤: {e}")
        _write_status(
            state="error",
            last_failed_at=_now().isoformat(timespec="seconds"),
            last_error=str(e)[:240],
        )
        _send_scheduler_incident(
            "排程批次執行失敗",
            f"排程時段：{scheduled_slot or '當前時段'}\n原因：{type(e).__name__}: {str(e)[:240]}",
            f"batch:{scheduled_slot or _slot_key(_now())}",
        )
        return results
    finally:
        dispatch_lock.release()


def _supervisor_tick(fallback_policy: SchedulePolicy, *, owner_token: str) -> None:
    """Run one durable dispatch/notification/heartbeat supervision batch."""

    if maintenance_active(DATA_DIR):
        # Restore/backup owns the complete data directory while this flag is
        # active.  Do not even refresh the JSON heartbeat during that window.
        return

    dispatch_lock = ProcessLock(DISPATCH_LOCK_FILE, "scheduler-dispatch")
    if not dispatch_lock.acquire():
        log_message("排程監督器略過競爭批次；現有工作繼續執行。")
        return

    def heartbeat(phase: str, **details: object) -> None:
        _write_status(
            supervisor_state="running",
            supervisor_phase=phase,
            supervisor_heartbeat_at=_now().isoformat(timespec="seconds"),
            owner_token=owner_token,
            **details,
        )

    # The heartbeat is written before any potentially slow outbox/API work and
    # again between monitors/retries through the callback below.
    heartbeat("starting", batch_index=0, batch_total=0)
    try:
        retry_results = process_notification_outbox(limit=20)
        heartbeat("notifications_processed", retry_count=len(retry_results))
        recovered = recover_missed_runs(
            fallback_policy,
            heartbeat=heartbeat,
        )
        heartbeat("fare_batch_complete", recovered_count=len(recovered))
        try:
            backup_report = _maybe_create_auto_backup()
            backup_state = str(backup_report.get("state") or "unknown")
            backup_updates: dict[str, object] = {
                "auto_backup_checked_at": _now().isoformat(timespec="seconds"),
                "owner_token": owner_token,
            }
            if backup_state != "not_due":
                backup_updates.update(
                    {
                        "auto_backup_state": backup_state,
                        "auto_backup_path": backup_report.get("path"),
                        "auto_backup_removed": backup_report.get("removed", 0),
                    }
                )
            if backup_state in {"created", "current", "disabled"}:
                backup_updates["auto_backup_error"] = None
            _write_status(**backup_updates)
        except Exception as backup_exc:
            backup_report = {"state": "error"}
            log_message(f"每日自動備份失敗：{backup_exc}")
            _write_status(
                auto_backup_state="error",
                auto_backup_error=str(backup_exc)[:240],
                auto_backup_checked_at=_now().isoformat(timespec="seconds"),
                owner_token=owner_token,
            )
            _send_scheduler_incident(
                "每日自動備份失敗",
                f"原因：{type(backup_exc).__name__}: {str(backup_exc)[:240]}\n現有最新有效備份已保留，未執行清理。",
                f"auto-backup:{_now().strftime('%Y-%m-%d')}",
            )
        heartbeat(
            "finalizing",
            recovered_count=len(recovered),
            auto_backup_state=backup_report.get("state"),
        )
        final_status: dict[str, object] = {
            "supervisor_state": "healthy",
            "supervisor_phase": "waiting",
            "supervisor_checked_at": _now().isoformat(timespec="seconds"),
            "supervisor_heartbeat_at": _now().isoformat(timespec="seconds"),
            "owner_token": owner_token,
            "retry_count": len(retry_results),
            "recovered_count": len(recovered),
            "next_run_at": _next_due_monitor_time(fallback_policy).isoformat(
                timespec="minutes"
            ),
            "supervisor_error": None,
        }
        final_status.update(_latest_run_status())
        _write_status(
            **final_status,
        )
    except Exception as exc:
        log_message(f"排程監督器錯誤：{exc}")
        _write_status(
            supervisor_state="error",
            supervisor_phase="failed",
            supervisor_heartbeat_at=_now().isoformat(timespec="seconds"),
            supervisor_error=str(exc)[:240],
            owner_token=owner_token,
        )
        _send_scheduler_incident(
            "排程監督器執行失敗",
            f"原因：{type(exc).__name__}: {str(exc)[:240]}",
            f"supervisor:{_now().strftime('%Y-%m-%dT%H')}",
        )
    finally:
        dispatch_lock.release()


def _run_owned_service(service_lock: ProcessLock) -> int:
    db.init_db()
    scheduler = BlockingScheduler(
        timezone=TAIPEI_TZ,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 86400},
    )

    hours_env = os.getenv("SCHEDULE_HOURS", "").strip()
    interval_env = os.getenv("CHECK_INTERVAL_MINUTES", "").strip()

    # 明確設定間隔時優先；否則預設每日 00:00 與 12:00。
    if interval_env:
        hours_env = ""
    if not hours_env and not interval_env:
        hours_env = "0,12"
    fallback_policy = _global_schedule(hours_env, interval_env)

    log_message("=" * 50)
    log_message("SkyWatch 航班監控系統 - 獨立背景排程服務")

    if hours_env:
        try:
            hours_list = sorted({int(h.strip()) for h in hours_env.split(",") if h.strip()})
            if not hours_list or any(hour < 0 or hour > 23 for hour in hours_list):
                raise ValueError("小時必須介於 0 到 23")
            hours_str = ",".join(map(str, hours_list))
            # Fixed schedules share the same durable supervisor owner as
            # interval and per-monitor schedules.  An APScheduler cron misfire
            # after sleep otherwise receives the wake-up time (for example
            # 12:30) while recovery also dispatches the real 12:00 slot,
            # bypassing the unique slot ledger and sending twice.
            log_message(f"排程模式：持久化固定時間點 (每日 {hours_str}:00)")
            _write_status(
                state="waiting",
                pid=os.getpid(),
                owner_token=service_lock.owner.token,
                service_started_at=_now().isoformat(timespec="seconds"),
                timezone="Asia/Taipei",
                schedule=f"每日 {hours_str}:00",
            )
        except Exception as e:
            log_message(f"解析 SCHEDULE_HOURS 失敗 ({e})，改用 interval 模式")
            hours_env = ""

    if not hours_env and interval_env:
        minutes = int(interval_env)
        if minutes < 5:
            raise ValueError("CHECK_INTERVAL_MINUTES 不可小於 5")
        # Interval slots are owned exclusively by the durable supervisor grid.
        # Registering a second APScheduler interval from service-start time
        # would create a shifted slot after every restart and double-query
        # global monitors (for example 13:00 and 13:10).
        log_message(f"排程模式：持久化固定間隔 (每 {minutes} 分鐘檢查一次)")
        _write_status(
            state="waiting",
            pid=os.getpid(),
            owner_token=service_lock.owner.token,
            service_started_at=_now().isoformat(timespec="seconds"),
            timezone="Asia/Taipei",
            schedule=f"每 {minutes} 分鐘",
        )

    # Capture the wall-clock boundary before startup recovery.  If recovery
    # crosses that boundary, APScheduler treats it as a coalesced misfire and
    # runs once immediately after start, so no exact slot is skipped.
    first_aligned_tick = _next_aligned_supervisor_tick()
    log_message("啟動排程補漏檢查…")
    _supervisor_tick(
        fallback_policy=fallback_policy,
        owner_token=service_lock.owner.token,
    )

    scheduler.add_job(
        _supervisor_tick,
        "interval",
        minutes=SCHEDULER_TICK_MINUTES,
        id="scheduler_supervisor",
        replace_existing=True,
        next_run_time=first_aligned_tick,
        kwargs={
            "fallback_policy": fallback_policy,
            "owner_token": service_lock.owner.token,
        },
    )
    log_message(
        f"排程監督器已對齊時鐘；下次巡檢 {first_aligned_tick.strftime('%H:%M:%S')}"
    )

    log_message("=" * 50)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log_message("排程服務已停止。")
    return 0


def main() -> int:
    # The lifetime lock is acquired before database migration or heartbeat
    # writes. Its PID + process-start fingerprint + nonce allow safe stale
    # ownership recovery without relying on file modification time.
    waiting_logged = False
    while True:
        # Maintenance always owns the outer lock order. Never hold the
        # scheduler lifetime lock while waiting for backup/restore, otherwise a
        # restore that checks worker locks can deadlock with scheduler startup.
        while maintenance_active(DATA_DIR):
            if not waiting_logged:
                print("SkyWatch scheduler is waiting for maintenance to finish.", flush=True)
                waiting_logged = True
            time.sleep(1.0)

        service_lock = ProcessLock(DATA_DIR / "scheduler.lock", "scheduler")
        if not service_lock.acquire():
            print("SkyWatch scheduler is already running; duplicate process exited.", flush=True)
            return 2

        # Close the check/acquire race: if maintenance appeared between those
        # operations, yield ownership and retry in canonical lock order.
        if maintenance_active(DATA_DIR):
            service_lock.release()
            time.sleep(0.25)
            continue
        try:
            return _run_owned_service(service_lock)
        finally:
            service_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
