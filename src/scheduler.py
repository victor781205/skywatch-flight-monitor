"""
舊版內建背景排程模組。

正式環境請使用專案根目錄的 scheduler.py。這個模組不再自動啟動，
避免 Streamlit 與獨立排程同時執行造成重複查價與重複通知。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from . import database as db
from .monitor import check_all_monitors


class BackgroundScheduler:
    """輕量背景排程，使用 threading.Timer 實現"""

    def __init__(self):
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.RLock()
        self._running = False
        self._interval_minutes = 60
        self._fixed_hours: Optional[List[int]] = None
        self._next_run: Optional[datetime] = None
        self._last_result: List[Dict[str, Any]] = []
        self._last_error: Optional[str] = None

    # ── properties ──

    @property
    def running(self) -> bool:
        return self._running

    @property
    def interval_minutes(self) -> int:
        return self._interval_minutes

    @property
    def fixed_hours(self) -> Optional[List[int]]:
        return self._fixed_hours

    @property
    def next_run(self) -> Optional[datetime]:
        return self._next_run

    @property
    def last_result(self) -> List[Dict[str, Any]]:
        return self._last_result

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    # ── control ──

    def start(self, interval_minutes: int = 60, fixed_hours: Optional[List[int]] = None):
        with self._lock:
            if self._running:
                self._cancel_timer()
            self._interval_minutes = interval_minutes
            self._fixed_hours = fixed_hours
            self._running = True
            self._schedule_next()

    def stop(self):
        with self._lock:
            self._running = False
            self._cancel_timer()

    def restart(self, interval_minutes: int, fixed_hours: Optional[List[int]] = None):
        self.stop()
        self.start(interval_minutes, fixed_hours=fixed_hours)

    # ── internal ──

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule_next(self):
        with self._lock:
            if not self._running:
                return
            now = datetime.now()
            if self._fixed_hours:
                candidates = []
                for day_offset in (0, 1):
                    d = now.date() + timedelta(days=day_offset)
                    for h in self._fixed_hours:
                        t = datetime(d.year, d.month, d.day, h, 0, 0)
                        if t > now:
                            candidates.append(t)
                self._next_run = min(candidates) if candidates else (now + timedelta(minutes=self._interval_minutes))
                delay_sec = max(1.0, (self._next_run - now).total_seconds())
            else:
                self._next_run = now + timedelta(minutes=self._interval_minutes)
                delay_sec = self._interval_minutes * 60.0

            self._timer = threading.Timer(
                delay_sec,
                self._run_job,
            )
            self._timer.daemon = True
            self._timer.start()

    def _run_job(self):
        db.init_db()
        try:
            results = check_all_monitors()
            self._last_result = results
            self._last_error = None
        except Exception as e:
            self._last_error = str(e)
            self._last_result = []
        finally:
            # 排程下次執行
            if self._running:
                self._schedule_next()


# 全域單例
_scheduler: Optional[BackgroundScheduler] = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler()
    return _scheduler
