"""টেলিগ্রাম বটের লজিক — আসল টেলিগ্রাম API ছাড়াই ফেক ক্লায়েন্ট দিয়ে।"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.agent import Agent
from app.config import load_settings
from app.telegram import TelegramBot
from tests.helpers import demo_service


class FakeResponse:
    status_code = 200
    text = '{"ok":true,"result":[]}'
    headers = {"content-type": "application/json"}

    def __init__(self, payload=None):
        self._payload = payload or {"ok": True, "result": []}

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, json=None, **kwargs):
        self.posts.append((url, json or {}))
        return FakeResponse()

    async def get(self, url: str, params=None, **kwargs):
        return FakeResponse()


def update(chat_id: int, text: str) -> dict:
    return {
        "update_id": 1,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


class TelegramTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = demo_service(Path(self.tmp.name))
        base = load_settings()
        self.settings = replace(
            base, telegram_bot_token="123:TEST", telegram_chat_ids=[555]
        )
        self.agent = Agent(self.service, self.settings)
        self.client = FakeClient()
        self.bot = TelegramBot(self.settings, self.agent, self.service, client=self.client)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_id_command_works_for_unknown_chat(self) -> None:
        await self.bot._handle(update(777, "/id"), self.client)
        self.assertEqual(len(self.client.posts), 1)
        text = self.client.posts[0][1]["text"]
        self.assertIn("777", text)
        self.assertIn("TELEGRAM_CHAT_IDS", text)

    async def test_unknown_chat_is_ignored(self) -> None:
        await self.bot._handle(update(777, "ব্যালেন্স কত?"), self.client)
        self.assertEqual(self.client.posts, [])

    async def test_balance_command(self) -> None:
        await self.bot._handle(update(555, "/balance"), self.client)
        text = self.client.posts[0][1]["text"]
        self.assertIn("৳", text)

    async def test_commands_have_replies(self) -> None:
        for command in ("/bill", "/days", "/tips", "/recharge", "/monthly", "/tariff", "/today"):
            self.client.posts.clear()
            await self.bot._handle(update(555, command), self.client)
            self.assertEqual(len(self.client.posts), 1, msg=command)
            self.assertTrue(self.client.posts[0][1]["text"].strip(), msg=command)

    async def test_help_and_start(self) -> None:
        await self.bot._handle(update(555, "/start"), self.client)
        text = self.client.posts[0][1]["text"]
        self.assertIn("/balance", text)
        self.assertIn("বিদ্যুৎ বন্ধু", text)

    async def test_free_text_question(self) -> None:
        await self.bot._handle(update(555, "আর কত দিন চলবে?"), self.client)
        text = self.client.posts[0][1]["text"]
        self.assertIn("দিন", text)

    async def test_live_command(self) -> None:
        await self.bot._handle(update(555, "/live"), self.client)
        text = self.client.posts[0][1]["text"]
        self.assertIn("kW", text)
        self.assertIn("সিমুলেটেড", text)

    async def test_send_to_all_chats(self) -> None:
        self.settings.telegram_chat_ids = [555, 666]
        ok = await self.bot.send("হ্যালো")
        self.assertTrue(ok)
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual({p[1]["chat_id"] for p in self.client.posts}, {555, 666})

    async def test_send_long_message_is_split(self) -> None:
        long_text = "\n".join(f"লাইন {i} — কিছু বাংলা লেখা" for i in range(400))
        chunks = TelegramBot._split(long_text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 3800 for c in chunks))

    async def test_send_without_chat_ids(self) -> None:
        self.settings.telegram_chat_ids = []
        self.assertFalse(await self.bot.send("হ্যালো"))
        self.assertEqual(self.client.posts, [])


if __name__ == "__main__":
    unittest.main()
