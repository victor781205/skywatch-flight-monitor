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
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apscheduler.schedulers.blocking import BlockingScheduler

from src import database as db
from src.config import MISSED_RUN_LOOKBACK_HOURS, SCHEDULER_TICK_MINUTES
from src.monitor import check_all_monitors
from src.notifier import process_notification_outbox, send_all_reliable
from src.policies import ScheduleMode, SchedulePolicy

LOCK_FILE = ROOT / "data" / "scheduler.lock"
STATUS_FILE = ROOT / "data" / "scheduler_status.json"
LOCK_STALE_SECONDS = 30 * 60
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def _now() -> datetime:
    return datetime.now(TAIPEI_TZ)


def _write_status(**updates) -> None:
    """Persist a machine-readable heartbeat without exposing secrets."""
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    try:
        current = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        pass
    current.update(updates)
    current["updated_at"] = _now().isoformat(timespec="seconds")
    temp_path = STATUS_FILE.with_suffix(".tmp")
    temp_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(STATUS_FILE)


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


def _acquire_job_lock() -> Optional[int]:
    """Acquire one cross-process lock; stale locks are recovered automatically."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} started={_now().isoformat()}".encode("utf-8"))
            return fd
        except FileExistsError:
            try:
                stale = time.time() - LOCK_FILE.stat().st_mtime > LOCK_STALE_SECONDS
            except FileNotFoundError:
                stale = False
            if attempt == 0 and stale:
                try:
                    LOCK_FILE.unlink()
                except FileNotFoundError:
                    pass
                continue
            return None
    return None


def _release_job_lock(fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
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
    send_all_reliable(
        f"⚠️ SkyWatch 排程異常\n{title}\n{detail}\n時間：{_now().strftime('%Y-%m-%d %H:%M')}（台北）",
        purpose=f"scheduler:{incident_key}",
    )


def recover_missed_runs(fallback: SchedulePolicy, *, now: Optional[datetime] = None) -> list[dict]:
    """Backfill at most one latest missed slot per monitor to protect quota."""
    recovered: list[dict] = []
    current = (now or _now()).astimezone(TAIPEI_TZ)
    incident_grace = timedelta(minutes=max(2, SCHEDULER_TICK_MINUTES * 2))
    for monitor in db.list_monitors(active_only=True):
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
        results = check_all_monitors(
            scheduled_report=True,
            scheduled_slot=key,
            backfill=is_backfill,
            monitor_ids=[int(monitor["id"])],
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
    lock_fd = _acquire_job_lock()
    if lock_fd is None:
        log_message("略過本次查價：另一個排程工作仍在執行。")
        _write_status(state="skipped_locked", last_skip_at=_now().isoformat(timespec="seconds"))
        return
    log_message("開始執行定時查價監控…")
    _write_status(state="running", last_started_at=_now().isoformat(timespec="seconds"), last_error=None)
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
        results = check_all_monitors(
            scheduled_report=True,
            scheduled_slot=slot,
            backfill=backfill,
            monitor_ids=[int(monitor["id"]) for monitor in due_monitors],
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
    except Exception as e:
        log_message(f"❌ 排程執行錯誤: {e}")
        _write_status(
            state="error",
            last_failed_at=_now().isoformat(timespec="seconds"),
            last_error=str(e)[:240],
        )
    finally:
        _release_job_lock(lock_fd)


def main():
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
            service_started_at=_now().isoformat(timespec="seconds"),
            timezone="Asia/Taipei",
            schedule=f"每 {minutes} 分鐘",
        )

    # A small supervisor tick catches sleep/reboot misses, runs durable
    # notification retries, and updates the heartbeat independently of fare jobs.
    def supervisor_tick():
        try:
            retry_results = process_notification_outbox(limit=20)
            recovered = recover_missed_runs(fallback_policy)
            _write_status(
                supervisor_state="healthy",
                supervisor_checked_at=_now().isoformat(timespec="seconds"),
                retry_count=len(retry_results),
                recovered_count=len(recovered),
                next_run_at=_next_due_monitor_time(fallback_policy).isoformat(timespec="minutes"),
            )
        except Exception as exc:
            log_message(f"排程監督器錯誤：{exc}")
            _write_status(supervisor_state="error", supervisor_error=str(exc)[:240])

    scheduler.add_job(
        supervisor_tick,
        "interval",
        minutes=SCHEDULER_TICK_MINUTES,
        id="scheduler_supervisor",
        replace_existing=True,
        next_run_time=_now() + timedelta(seconds=10),
    )

    log_message("=" * 50)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log_message("排程服務已停止。")


if __name__ == "__main__":
    main()
