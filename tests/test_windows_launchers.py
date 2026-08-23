from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WindowsLauncherTests(unittest.TestCase):
    def test_launchers_enable_utf8_without_overriding_schedule(self) -> None:
        for name in ("start_skywatch.bat", "start_scheduler.bat", "start_telegram_bot.bat"):
            content = (ROOT / name).read_text(encoding="utf-8").lower()
            self.assertIn("set pythonutf8=1", content, name)

        scheduler = (ROOT / "start_scheduler.bat").read_text(encoding="utf-8").lower()
        self.assertNotIn("set schedule_hours=", scheduler)
        self.assertNotIn("set check_interval_minutes=", scheduler)

    def test_task_wrappers_wait_for_the_child_process(self) -> None:
        for name in (
            "start_skywatch_silent.vbs",
            "start_scheduler_silent.vbs",
            "start_telegram_bot_silent.vbs",
        ):
            content = (ROOT / name).read_text(encoding="utf-8").lower()
            self.assertIn(", 0, true", content, name)
            self.assertNotIn(", 0, false", content, name)

    def test_installer_registers_all_services_and_ignores_duplicates(self) -> None:
        content = (ROOT / "scripts" / "install_windows_tasks.ps1").read_text(encoding="utf-8")
        for task in (
            "SkyWatch_AutoStart",
            "SkyWatch_Scheduler_AutoStart",
            "SkyWatch_TelegramBot_AutoStart",
        ):
            self.assertIn(task, content)
        self.assertIn("-MultipleInstances IgnoreNew", content)
        self.assertIn("-StartWhenAvailable", content)


if __name__ == "__main__":
    unittest.main()
