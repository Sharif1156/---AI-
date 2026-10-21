"""এজেন্টের ইনটেন্ট আর অফলাইন উত্তরের টেস্ট।"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agent import Agent, ToolRunner, detect_intent
from app.config import load_settings
from tests.helpers import demo_service


class IntentTest(unittest.TestCase):
    def test_bengali_intents(self) -> None:
        cases = {
            "ব্যালেন্স কত?": "balance",
            "আর কত দিন চলবে?": "days",
            "এই মাসে বিল কত হবে": "bill",
            "আজ কত ইউনিট খরচ হলো": "today",
            "রিচার্জ হিস্ট্রি দেখাও": "recharge",
            "টিপস দাও": "tips",
            "ট্যারিফ রেট কত": "tariff",
            "মাস শেষে বিল কত হবে": "bill",
            "গত মাসে কত ইউনিট ছিল": "monthly",
        }
        for text, expected in cases.items():
            self.assertEqual(detect_intent(text), expected, msg=text)

    def test_banglish_and_english(self) -> None:
        self.assertEqual(detect_intent("balance koto"), "balance")
        self.assertEqual(detect_intent("how many days left?"), "days")
        self.assertEqual(detect_intent("tariff rate please"), "tariff")
        self.assertEqual(detect_intent("show me my bill"), "bill")

    def test_unknown_falls_back_to_status(self) -> None:
        self.assertEqual(detect_intent("কিছু একটা বলো"), "status")


class OfflineAgentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = demo_service(Path(self.tmp.name))
        self.agent = Agent(self.service, load_settings())

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_answers_have_real_numbers(self) -> None:
        questions = [
            "ব্যালেন্স কত?",
            "আর কত দিন চলবে?",
            "আজ কত ইউনিট খরচ হলো?",
            "এই মাসে বিল কত?",
            "রিচার্জ দেখাও",
            "টিপস দাও",
            "ট্যারিফ রেট কত?",
            "মাসিক হিসাব দাও",
        ]
        for q in questions:
            result = await self.agent.answer(q)
            self.assertEqual(result["engine"], "offline")
            self.assertTrue(result["answer"].strip(), msg=q)
            self.assertNotIn("Traceback", result["answer"])

    async def test_balance_answer_mentions_actual_balance(self) -> None:
        from app.formatting import bn, tk

        snapshot = await self.service.snapshot()
        result = await self.agent.answer("ব্যালেন্স কত?")
        self.assertIn("৳", result["answer"])
        self.assertIn(bn(int(snapshot.balance_tk)), result["answer"])
        self.assertIn(tk(snapshot.balance_tk), result["answer"])

    async def test_greeting_routes_to_help(self) -> None:
        result = await self.agent.answer("হ্যালো")
        self.assertIn("বিদ্যুৎ বন্ধু", result["answer"])
        self.assertIn("ব্যালেন্স", result["answer"])

    async def test_commands(self) -> None:
        for command in ("/balance", "/bill", "/days", "/tips", "/recharge", "/tariff", "/monthly"):
            result = await self.agent.offline.command(command)
            self.assertTrue(result["answer"].strip(), msg=command)

    async def test_empty_question(self) -> None:
        result = await self.agent.answer("   ")
        self.assertIn("লিখুন", result["answer"])

    async def test_llm_falls_back_to_offline(self) -> None:
        from dataclasses import replace

        broken = replace(
            load_settings(),
            llm_api_key="test-key",
            llm_base_url="http://127.0.0.1:9/v1",
            llm_model="test",
            llm_timeout=1.0,
        )
        agent = Agent(self.service, broken)
        self.assertTrue(agent.client is not None)
        result = await agent.answer("ব্যালেন্স কত?")
        self.assertEqual(result["engine"], "offline-fallback")
        self.assertIn("llm_error", result)
        self.assertTrue(result["answer"])


class ToolRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = demo_service(Path(self.tmp.name))
        self.tools = ToolRunner(self.service)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_status_tool(self) -> None:
        data = await self.tools.run("get_status")
        self.assertIn("balance_tk", data)
        self.assertEqual(data["provider"], "ডেমো")
        self.assertTrue(data["demo"])

    async def test_bill_tool_with_units(self) -> None:
        data = await self.tools.run("get_bill", {"units": 250})
        self.assertAlmostEqual(data["units"], 250)
        self.assertGreater(data["total"], 1500)

    async def test_usage_tool(self) -> None:
        data = await self.tools.run("get_usage", {"days": 10})
        self.assertEqual(len(data["daily"]), 10)
        self.assertGreater(data["total_units"], 0)

    async def test_tariff_tool(self) -> None:
        data = await self.tools.run("get_tariff")
        self.assertIn("slabs", data)
        self.assertGreater(data["current_marginal_rate"], 0)

    async def test_bad_tool_name(self) -> None:
        data = await self.tools.run("no_such_tool")
        self.assertIn("error", data)

    async def test_tools_are_declared_for_the_llm(self) -> None:
        from app.agent import TOOLS

        names = {tool["function"]["name"] for tool in TOOLS}
        for name in (
            "get_status",
            "get_usage",
            "get_bill",
            "get_forecast",
            "get_recharges",
            "get_tariff",
            "get_tips",
            "get_monthly",
        ):
            self.assertIn(name, names)


if __name__ == "__main__":
    unittest.main()
