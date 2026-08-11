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
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apscheduler.schedulers.blocking import BlockingScheduler

from src import database as db
from src.monitor import check_all_monitors

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


def job():
    lock_fd = _acquire_job_lock()
    if lock_fd is None:
        log_message("略過本次查價：另一個排程工作仍在執行。")
        _write_status(state="skipped_locked", last_skip_at=_now().isoformat(timespec="seconds"))
        return
    log_message("開始執行定時查價監控…")
    _write_status(state="running", last_started_at=_now().isoformat(timespec="seconds"), last_error=None)
    try:
        results = check_all_monitors(scheduled_report=True)
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
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900},
    )

    hours_env = os.getenv("SCHEDULE_HOURS", "").strip()
    interval_env = os.getenv("CHECK_INTERVAL_MINUTES", "").strip()

    # 明確設定間隔時優先；否則預設每日 00:00 與 12:00。
    if interval_env:
        hours_env = ""
    if not hours_env and not interval_env:
        hours_env = "0,12"

    log_message("=" * 50)
    log_message("SkyWatch 航班監控系統 - 獨立背景排程服務")

    if hours_env:
        try:
            hours_list = sorted({int(h.strip()) for h in hours_env.split(",") if h.strip()})
            if not hours_list or any(hour < 0 or hour > 23 for hour in hours_list):
                raise ValueError("小時必須介於 0 到 23")
            hours_str = ",".join(map(str, hours_list))
            log_message(f"排程模式：固定時間點每日執行 (每日 {hours_str}:00)")
            scheduler.add_job(
                job,
                "cron",
                hour=hours_str,
                minute=0,
                id="flight_check_cron",
                replace_existing=True,
            )
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
        log_message(f"排程模式：固定時間間隔 (每 {minutes} 分鐘檢查一次)")
        scheduler.add_job(
            job,
            "interval",
            minutes=minutes,
            id="flight_check_interval",
            replace_existing=True,
        )
        _write_status(
            state="waiting",
            pid=os.getpid(),
            service_started_at=_now().isoformat(timespec="seconds"),
            timezone="Asia/Taipei",
            schedule=f"每 {minutes} 分鐘",
        )

    log_message("=" * 50)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log_message("排程服務已停止。")


if __name__ == "__main__":
    main()
