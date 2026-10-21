"""FastAPI এন্ডপয়েন্টের টেস্ট।"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import build_context, create_app
from app.models import Snapshot
from app.providers.base import BaseProvider, ProviderError


def demo_app(tmp: Path):
    settings = replace(
        load_settings(),
        provider="demo",
        account_no="12345678",
        data_dir=tmp,
        cache_seconds=0,
        telegram_bot_token="",
        telegram_chat_ids=[],
        llm_api_key="",
    )
    ctx = build_context(settings)
    return create_app(ctx)


class FailingProvider(BaseProvider):
    name = "broken"
    label_bn = "ভাঙা"

    async def fetch(self) -> Snapshot:
        raise ProviderError(
            "পোর্টালে সংযোগ হয়নি", kind="network", hint="নেটওয়ার্ক চেক করুন"
        )


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app = demo_app(Path(self.tmp.name))
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    # ---------------- পেজ ও হেলথ ----------------
    def test_index_page(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("বিদ্যুৎ বন্ধু", response.text)
        self.assertIn("/api/dashboard", response.text)

    def test_health(self) -> None:
        data = self.client.get("/api/health").json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["provider"], "demo")
        self.assertTrue(data["demo"])

    # ---------------- ডেটা এন্ডপয়েন্ট ----------------
    def test_dashboard(self) -> None:
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("balance", data)
        self.assertIn("forecast", data)
        self.assertGreater(len(data["daily"]), 0)
        self.assertEqual(data["meta"]["provider_label"], "ডেমো")

    def test_refresh(self) -> None:
        response = self.client.post("/api/refresh")
        self.assertEqual(response.status_code, 200)
        self.assertIn("usage", response.json())

    def test_status(self) -> None:
        data = self.client.get("/api/status").json()
        self.assertGreater(data["balance_tk"], 0)
        self.assertIn("alerts", data)

    def test_usage(self) -> None:
        data = self.client.get("/api/usage?days=7").json()
        self.assertEqual(len(data["series"]), 7)
        self.assertGreater(data["summary"]["avg7"], 0)

    def test_bill_with_units(self) -> None:
        data = self.client.get("/api/bill?units=250").json()
        self.assertAlmostEqual(data["bill"]["units"], 250)
        self.assertGreater(data["bill"]["total"], 1500)
        self.assertIn("month_to_date", data)
        self.assertIn("projected", data)

    def test_forecast(self) -> None:
        data = self.client.get("/api/forecast").json()
        self.assertIsNotNone(data["forecast"]["days_left"])

    def test_tariff(self) -> None:
        data = self.client.get("/api/tariff").json()
        self.assertIn("slabs", data)
        self.assertGreater(len(data["slabs"]), 4)

    def test_alerts_and_live(self) -> None:
        alerts = self.client.get("/api/alerts").json()["alerts"]
        self.assertTrue(alerts)
        live = self.client.get("/api/live").json()
        self.assertIsNotNone(live["live"])
        self.assertEqual(live["live"]["source"], "simulated")

    def test_recharges_and_history(self) -> None:
        data = self.client.get("/api/recharges?limit=3").json()
        self.assertLessEqual(len(data["recharges"]), 3)
        history = self.client.get("/api/history?days=10").json()
        self.assertGreater(len(history["series"]), 0)

    def test_manual_check_does_not_send(self) -> None:
        result = self.client.post("/api/check?send=false").json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["sent"], [])

    # ---------------- চ্যাট ----------------
    def test_chat(self) -> None:
        response = self.client.post("/api/chat", json={"message": "ব্যালেন্স কত?"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["engine"], "offline")
        self.assertIn("৳", data["answer"])

    def test_chat_rejects_empty_message(self) -> None:
        response = self.client.post("/api/chat", json={"message": ""})
        self.assertEqual(response.status_code, 422)

    # ---------------- কনফিগ ----------------
    def test_config_hides_secrets(self) -> None:
        data = self.client.get("/api/config").json()
        self.assertNotIn("llm_api_key", data)
        self.assertNotIn("telegram_bot_token", data)
        self.assertEqual(data["provider"], "demo")

    def test_summary_text(self) -> None:
        data = self.client.get("/api/summary").json()
        self.assertIn("ব্যালেন্স", data["text"])


class ProviderFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app = demo_app(Path(self.tmp.name))
        ctx = self.app.state.ctx
        ctx.provider = FailingProvider()
        ctx.service.provider = ctx.provider
        ctx.service.cache._snapshot = None  # কোনো পুরনো ডেটাও নেই
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_dashboard_returns_503_with_hint(self) -> None:
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 503)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["kind"], "network")
        self.assertIn("নেটওয়ার্ক", data["hint"])

    def test_health_still_works(self) -> None:
        self.assertEqual(self.client.get("/api/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
