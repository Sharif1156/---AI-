"""ডেমো প্রোভাইডার + সার্ভিসের হিসাব ও অ্যালার্টের টেস্ট।"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from app.config import load_settings
from app.providers.demo import DemoProvider
from app.service import MeterService
from app.store import HistoryStore, SnapshotCache, StateStore
from app.tariff import Tariff

ROOT = Path(__file__).resolve().parent.parent


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        settings = load_settings()
        self.settings = replace(
            settings,
            provider="demo",
            account_no="12345678",
            data_dir=self.dir,
            low_balance_tk=300.0,
            critical_balance_tk=150.0,
            monthly_budget_tk=2500.0,
            cache_seconds=0,
        )
        self.tariff = Tariff(ROOT / "tariff.json")
        self.provider = DemoProvider(self.settings, self.tariff)
        self.service = MeterService(
            self.settings,
            self.tariff,
            self.provider,
            SnapshotCache(self.dir / "snapshot.json"),
            HistoryStore(self.dir / "history.json"),
            StateStore(self.dir / "state.json"),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_snapshot_has_realistic_data(self) -> None:
        snapshot = await self.service.snapshot()
        self.assertEqual(snapshot.provider, "demo")
        self.assertGreater(snapshot.balance_tk, 0)
        self.assertLess(snapshot.balance_tk, 2000)
        self.assertGreaterEqual(len(snapshot.daily), 60)
        self.assertTrue(snapshot.customer_name)
        self.assertIsNotNone(snapshot.live)

    async def test_snapshot_is_cached(self) -> None:
        self.service.settings = replace(self.service.settings, cache_seconds=300)
        first = await self.service.snapshot()
        second = await self.service.snapshot()
        self.assertIs(first, second)
        self.assertIsNotNone(self.service.cache.snapshot)
        # force=True দিলে নতুন করে আনে
        forced = await self.service.snapshot(force=True)
        self.assertIsNot(forced, first)

    async def test_history_persisted(self) -> None:
        await self.service.snapshot()
        history_file = self.dir / "history.json"
        self.assertTrue(history_file.exists())
        rows = self.service.history.series(days=30)
        self.assertGreater(len(rows), 20)

    async def test_analysis_and_forecast(self) -> None:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        usage = analysis["usage"]
        forecast = analysis["forecast"]
        self.assertGreater(usage["month_units"], 0)
        self.assertGreater(usage["avg7"], 0)
        self.assertGreater(forecast["days_left"], 0)
        self.assertIsNotNone(forecast["empty_on"])
        self.assertGreater(forecast["month_end_units"], usage["month_units"])
        self.assertGreater(forecast["month_end_bill"], 0)
        # মাস শেষে ইউনিট = এ পর্যন্ত + দৈনিক গড় × বাকি দিন
        self.assertAlmostEqual(
            forecast["month_end_units"],
            round(
                usage["month_units"]
                + forecast["avg_daily_units"] * (analysis["remaining_days"] + 1),
                2,
            ),
            delta=0.15,
        )

    async def test_days_left_matches_balance(self) -> None:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        expected = snapshot.balance_tk / analysis["forecast"]["avg_daily_cost"]
        self.assertAlmostEqual(analysis["forecast"]["days_left"], round(expected, 1), places=1)

    async def test_low_balance_alert(self) -> None:
        snapshot = await self.service.snapshot()
        alerts = self.service.build_alerts(snapshot)
        keys = {a["key"] for a in alerts}
        # ডেমো ব্যালেন্স (~২১৪৳) সতর্ক সীমার (৩০০৳) নিচে
        self.assertIn("balance_low", keys)
        self.assertTrue(all(a["title_bn"] and a["body_bn"] for a in alerts))

    async def test_critical_balance_and_days_left_alerts(self) -> None:
        snapshot = await self.service.snapshot()
        snapshot.balance_tk = 100.0  # ক্রিটিক্যাল সীমার নিচে
        alerts = self.service.build_alerts(snapshot)
        keys = {a["key"] for a in alerts}
        self.assertIn("balance_critical", keys)
        self.assertIn("days_left", keys)
        self.assertNotIn("balance_low", keys)

    async def test_no_balance_alert_when_rich(self) -> None:
        snapshot = await self.service.snapshot()
        snapshot.balance_tk = 5000
        keys = {a["key"] for a in self.service.build_alerts(snapshot)}
        self.assertNotIn("balance_low", keys)
        self.assertNotIn("balance_critical", keys)

    async def test_tips_are_helpful(self) -> None:
        snapshot = await self.service.snapshot()
        tips = self.service.build_tips(snapshot)
        self.assertGreaterEqual(len(tips), 3)
        for tip in tips:
            self.assertTrue(tip["title_bn"] and tip["detail_bn"])

    async def test_dashboard_payload(self) -> None:
        dashboard = await self.service.dashboard()
        for key in (
            "meta",
            "status",
            "customer",
            "balance",
            "usage",
            "forecast",
            "bill",
            "projected_bill",
            "daily",
            "monthly",
            "recharges",
            "alerts",
            "tips",
            "tariff",
        ):
            self.assertIn(key, dashboard)
        self.assertEqual(len(dashboard["daily"]), 30)
        self.assertLessEqual(len(dashboard["recharges"]), 8)
        self.assertTrue(dashboard["meta"]["demo"])

    async def test_anomaly_detected(self) -> None:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        anomaly = analysis["anomaly"]
        # ডেমোতে ইচ্ছাকৃতভাবে গড়ের ~২ গুণ দিন রাখা আছে
        self.assertIsNotNone(anomaly)
        assert anomaly is not None
        self.assertGreaterEqual(anomaly["ratio"], 1.5)
        self.assertGreater(anomaly["extra_tk"], 0)

    async def test_recharge_history_sorted(self) -> None:
        snapshot = await self.service.snapshot()
        self.assertGreater(len(snapshot.recharges), 3)
        dates = [r.dt for r in snapshot.recharges]
        self.assertEqual(dates, sorted(dates))
        self.assertIsNotNone(snapshot.newest_recharge())

    async def test_bill_for_units(self) -> None:
        bill = self.service.bill_for(250)
        self.assertAlmostEqual(bill.units, 250)
        self.assertGreater(bill.total, 1500)


class HistoryTest(unittest.IsolatedAsyncioTestCase):
    def test_cumulative_readings_are_spread_over_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / "history.json")
            today = date.today()
            store.record_cumulative(today - timedelta(days=3), 1000.0, source="desco")
            store.record_cumulative(today, 1030.0, source="desco")
            units = store.units_by_day()
            self.assertGreaterEqual(len(units), 2)
            self.assertAlmostEqual(sum(units.values()), 30.0, places=2)


if __name__ == "__main__":
    unittest.main()
