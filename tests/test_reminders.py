"""Reminder persistence, scheduling and chat-scoped tools; no Telegram login."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import tools
from bot import reminder_loop
from memory import Memory


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.db"
        self.memory = Memory(str(self.path))
        self.addCleanup(self.memory.close)
        tools.set_memory(self.memory)
        self.addCleanup(tools.set_memory, None)

    async def tool(self, name: str, chat_id: int = 11, **arguments) -> str:
        return await tools.call(name, json.dumps(arguments), chat_id)

    async def test_daily_rolls_past_start_forward_in_tehran(self) -> None:
        now = int(datetime(2030, 1, 1, 12, tzinfo=tools.TEHRAN).timestamp())
        with patch("time.time", return_value=now):
            result = await self.tool(
                "set_reminder", text="ورزش", repeat="daily", iso_time="2030-01-01T09:00"
            )
        self.assertIn("تنظیم شد", result)
        self.assertIn("هر روز", result)
        self.assertIn("2030-01-02 09:00 (تهران)", result)
        rid, text, due, period = self.memory.list_reminders(11)[0]
        self.assertEqual((text, period), ("ورزش", 1440))
        self.assertEqual(datetime.fromtimestamp(due, tools.TEHRAN).hour, 9)
        self.memory.reopen()
        self.assertEqual(self.memory.list_reminders(11), [(rid, text, due, period)])

    async def test_weekly_accepts_explicit_timezone(self) -> None:
        now = int(datetime(2030, 1, 1, tzinfo=tools.TEHRAN).timestamp())
        with patch("time.time", return_value=now):
            result = await self.tool(
                "set_reminder", text="برنامهٔ هفته", repeat="weekly",
                iso_time="2030-01-04T14:30:00Z",
            )
        self.assertIn("هر هفته", result)
        self.assertIn("2030-01-04 18:00", result)
        rid, _, due, period = self.memory.list_reminders(11)[0]
        self.assertEqual(period, 10080)
        with patch("time.time", return_value=due + 21):
            self.memory.mark_reminder_sent(rid)
        self.assertEqual(self.memory.list_reminders(11)[0][2], due + 7 * 86400)

    async def test_interval_defaults_to_one_period_from_now(self) -> None:
        now = 1_900_000_000
        with patch("time.time", return_value=now):
            result = await self.tool(
                "set_reminder", text="آب", repeat="interval", interval_minutes=120
            )
        self.assertIn("هر 2 ساعت", result)
        rid, _, due, period = self.memory.list_reminders(11)[0]
        self.assertEqual((due, period), (now + 7200, 120))
        listing = await self.tool("list_reminders")
        self.assertIn(f"#{rid}", listing)
        self.assertIn("نوبت بعد", listing)
        self.assertIn("هر 2 ساعت", listing)
        self.assertNotIn(f"#{rid}", await self.tool("list_reminders", chat_id=22))

    async def test_one_shot_remains_one_shot(self) -> None:
        now = 1_900_000_000
        with patch("time.time", return_value=now):
            result = await self.tool("set_reminder", text="قرار", minutes_from_now=10)
        self.assertIn("یک‌بار", result)
        rid, _, due, period = self.memory.list_reminders(11)[0]
        self.assertEqual((due, period), (now + 600, 0))
        with patch("time.time", return_value=due):
            self.assertEqual(self.memory.due_reminders(), [(rid, 11, "قرار")])
            self.memory.mark_reminder_sent(rid)
            self.assertEqual(self.memory.due_reminders(), [])
        self.assertEqual(self.memory.list_reminders(11), [])

    async def test_downtime_skips_missed_slots_without_schedule_drift(self) -> None:
        due = int(datetime(2030, 1, 1, 9, tzinfo=tools.TEHRAN).timestamp())
        rid = self.memory.add_reminder(11, "ورزش", due, repeat_minutes=1440)
        with patch("time.time", return_value=due + 3 * 86400 + 5 * 3600):
            self.memory.mark_reminder_sent(rid)
            self.assertEqual(self.memory.due_reminders(), [])
        self.assertEqual(self.memory.list_reminders(11)[0][2], due + 4 * 86400)
        # Landing exactly on a missed slot still advances to a future slot.
        with patch("time.time", return_value=due + 4 * 86400):
            self.memory.mark_reminder_sent(rid)
            self.assertEqual(self.memory.due_reminders(), [])
        self.assertEqual(self.memory.list_reminders(11)[0][2], due + 5 * 86400)

    async def test_cancel_is_chat_scoped_and_does_not_reactivate(self) -> None:
        rid = self.memory.add_reminder(11, "ورزش", 1, repeat_minutes=1440)
        result = await self.tool("cancel_reminder", chat_id=22, reminder_id=rid)
        self.assertNotIn("لغو شد", result)
        self.assertEqual(len(self.memory.list_reminders(11)), 1)
        self.assertIn("لغو شد", await self.tool("cancel_reminder", reminder_id=rid))
        self.memory.mark_reminder_sent(rid)
        self.memory.reopen()
        self.assertEqual(self.memory.list_reminders(11), [])
        self.assertEqual(self.memory.due_reminders(), [])

    async def test_invalid_schedules_do_not_create_reminders(self) -> None:
        cases = [
            {"repeat": "daily"},  # No invented clock time.
            {"repeat": "monthly", "minutes_from_now": 1},
            {"repeat": False, "minutes_from_now": 1},
            {"repeat": "interval"},
            *({"repeat": "interval", "interval_minutes": value}
              for value in (0, -1, 1.5, True, "120", 525601)),
            {"repeat": "daily", "interval_minutes": 120, "minutes_from_now": 1},
            {"minutes_from_now": True},
            {"minutes_from_now": float("inf")},
            {"minutes_from_now": 1e300},
            {"minutes_from_now": -1},
            {"iso_time": "invalid"},
            {"iso_time": "2000-01-01T09:00"},  # Past one-shot.
            {"iso_time": "2030-01-01T09:00", "minutes_from_now": 1},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = await self.tool("set_reminder", text="تست", **arguments)
                self.assertTrue(result.startswith("خطا:"), result)
        self.assertEqual(self.memory.list_reminders(11), [])

    async def test_migrates_old_database_without_losing_reminders(self) -> None:
        legacy_path = Path(self.tmp.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as db:
            db.executescript("""
                CREATE TABLE reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL, due_at INTEGER NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
                );
                INSERT INTO reminders VALUES (1, 11, 'قدیمی', 1900000000, 0, 1);
                INSERT INTO reminders VALUES (2, 11, 'ارسال‌شده', 1800000000, 1, 1);
            """)
        legacy = Memory(str(legacy_path))
        try:
            self.assertEqual(legacy.list_reminders(11), [(1, "قدیمی", 1900000000, 0)])
            legacy.reopen()  # Migration is safe to repeat, including after restore.
            rid = legacy.add_reminder(11, "روتین", 1900000100, repeat_minutes=120)
            self.assertEqual(rid, 3)
            self.assertEqual(legacy.list_reminders(11)[1][3], 120)
        finally:
            legacy.close()

    async def test_delivery_loop_reschedules_success_and_retries_failure(self) -> None:
        now = 1_900_000_000
        once = self.memory.add_reminder(11, "قرار", now - 10)
        routine = self.memory.add_reminder(11, "آب", now - 3700, repeat_minutes=60)
        failed = self.memory.add_reminder(22, "ناموفق", now - 5, repeat_minutes=1440)
        stop = asyncio.Event()
        delivered = []

        class Client:
            async def send_message(self, chat_id, text, **kwargs):
                if chat_id == 22:
                    stop.set()
                    raise RuntimeError("simulated delivery failure")
                delivered.append((chat_id, text))

        with patch("time.time", return_value=now), self.assertLogs("angel", level="ERROR"):
            await reminder_loop(Client(), self.memory, stop)
        self.assertEqual(len(delivered), 2)
        self.assertEqual(self.memory.list_reminders(11), [(routine, "آب", now + 3500, 60)])
        self.assertEqual(self.memory.list_reminders(22), [(failed, "ناموفق", now - 5, 1440)])
        self.assertEqual(self.memory.db.execute("SELECT sent FROM reminders WHERE id = ?", (once,)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
