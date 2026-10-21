"""মনিটরের অ্যালার্ট-ডিডুপ্লিকেশন আর দৈনিক রিপোর্টের টেস্ট।"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.config import load_settings
from app.monitor import Monitor
from app.store import StateStore
from tests.helpers import demo_service


class MonitorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.service = demo_service(self.dir)
        self.settings = load_settings()
        self.monitor = Monitor(self.service, self.settings, notifier=None)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def test_report_window(self) -> None:
        hour = self.settings.report_hour
        day = datetime(2026, 10, 21)
        self.assertFalse(self.monitor._report_due(day.replace(hour=max(0, hour - 1))))
        self.assertTrue(self.monitor._report_due(day.replace(hour=hour, minute=0)))
        self.assertTrue(self.monitor._report_due(day.replace(hour=hour + 2, minute=59)))
        self.assertFalse(self.monitor._report_due(day.replace(hour=hour + 3)))
        # একবার পাঠানোর পর আবার যাবে না
        self.service.state.mark_report(day.date())
        self.assertFalse(self.monitor._report_due(day.replace(hour=hour)))

    async def test_poll_without_notifier(self) -> None:
        result = await self.monitor.poll_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["sent"], [])  # টেলিগ্রাম নেই, তাই কিছু পাঠানো হয়নি
        self.assertGreaterEqual(len(result["alerts"]), 1)
        self.assertIn("balance_tk", result)

    async def test_alerts_marked_only_when_sent(self) -> None:
        class FakeNotifier:
            def __init__(self) -> None:
                self.messages: list[str] = []

            async def send(self, text: str, chat_ids=None) -> bool:
                self.messages.append(text)
                return True

        fake = FakeNotifier()
        settings = load_settings()
        settings.telegram_chat_ids = [12345]
        monitor = Monitor(self.service, settings, notifier=fake)
        first = await monitor.poll_once()
        self.assertGreater(len(first["sent"]), 0)
        self.assertGreater(len(fake.messages), 0)
        self.assertIn("ব্যালেন্স", " ".join(fake.messages))

        # একই দিনে দ্বিতীয়বার একই অ্যালার্ট যাবে না
        second = await monitor.poll_once()
        self.assertEqual(second["sent"], [])

    async def test_daily_report_text(self) -> None:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        text = self.monitor.build_daily_report(snapshot, analysis)
        for word in ("শুভ সকাল", "ব্যালেন্স", "ইউনিট", "মাস শেষে"):
            self.assertIn(word, text)

    def test_state_dedupe(self) -> None:
        state = StateStore(self.dir / "state.json")
        self.assertIsNone(state.alert_sent_on("balance_low"))
        state.mark_alert("balance_low")
        self.assertEqual(state.alert_sent_on("balance_low"), datetime.now().date().isoformat())
        state.clear_alert("balance_low")
        self.assertIsNone(state.alert_sent_on("balance_low"))

    def test_alert_formatting(self) -> None:
        text = Monitor.format_alert(
            {"icon": "⚠️", "title_bn": "শিরোনাম", "body_bn": "বিস্তারিত"}
        )
        self.assertEqual(text, "⚠️ শিরোনাম\nবিস্তারিত")


if __name__ == "__main__":
    unittest.main()
