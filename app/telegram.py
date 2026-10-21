"""টেলিগ্রাম বট — লং পোলিং, কোনো webhook/পাবলিক URL দরকার হয় না।"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .agent import Agent
from .config import Settings
from .formatting import bn, tk
from .service import MeterService

log = logging.getLogger(__name__)

MAX_MESSAGE = 3800  # টেলিগ্রামের সীমা ৪০৯৬ — একটু নিচে রাখা

COMMANDS = {
    "/start": "শুরু",
    "/help": "সব কমান্ড",
    "/balance": "ব্যালেন্স",
    "/today": "আজকের ব্যবহার",
    "/bill": "বিলের হিসাব",
    "/days": "আর কত দিন চলবে",
    "/tips": "সাশ্রয়ের টিপস",
    "/recharge": "রিচার্জ হিস্ট্রি",
    "/monthly": "মাসিক সারাংশ",
    "/tariff": "ট্যারিফ রেট",
    "/live": "লাইভ লোড",
    "/status": "সারসংক্ষেপ",
    "/id": "আপনার chat id",
}


class TelegramBot:
    """send() দিয়ে অ্যালার্ট পাঠায়, run() দিয়ে কমান্ড শোনে।"""

    def __init__(
        self,
        settings: Settings,
        agent: Agent,
        service: MeterService,
        client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.agent = agent
        self.service = service
        self._client = client  # টেস্টে ইনজেক্ট করার জন্য
        self.token = settings.telegram_bot_token
        self.base = f"https://api.telegram.org/bot{self.token}"
        self._offset: int = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ---------------- পাঠানো ----------------
    async def send(self, text: str, chat_ids: list[int] | None = None) -> bool:
        targets = chat_ids if chat_ids is not None else self.settings.telegram_chat_ids
        if not targets:
            log.warning("TELEGRAM_CHAT_IDS খালি — কাউকে পাঠানো হয়নি। বটকে /id পাঠান।")
            return False
        ok_all = True
        if self._client is not None:
            return await self._push(self._client, targets, text)
        async with httpx.AsyncClient(timeout=20.0) as client:
            ok_all = await self._push(client, targets, text)
        return ok_all

    async def _push(
        self, client: httpx.AsyncClient, targets: list[int], text: str
    ) -> bool:
        ok_all = True
        for chat_id in targets:
            for chunk in self._split(text):
                try:
                    response = await client.post(
                        f"{self.base}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": chunk,
                            "disable_web_page_preview": True,
                        },
                    )
                    if response.status_code >= 400:
                        ok_all = False
                        log.error(
                            "টেলিগ্রাম sendMessage %s: %s",
                            response.status_code,
                            response.text[:200],
                        )
                except httpx.HTTPError as exc:
                    ok_all = False
                    log.error("টেলিগ্রামে পাঠানো যায়নি: %s", exc)
        return ok_all

    @staticmethod
    def _split(text: str) -> list[str]:
        if len(text) <= MAX_MESSAGE:
            return [text]
        chunks: list[str] = []
        current = ""
        for line in text.splitlines(keepends=True):
            if len(current) + len(line) > MAX_MESSAGE:
                chunks.append(current)
                current = ""
            current += line
        if current:
            chunks.append(current)
        return chunks or [text[:MAX_MESSAGE]]

    # ---------------- শোনা ----------------
    def start(self) -> None:
        if not self.token:
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="telegram-poll")
        log.info("টেলিগ্রাম বট চালু হয়েছে")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def run(self) -> None:
        backoff = 3
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
            while not self._stop.is_set():
                try:
                    response = await client.get(
                        f"{self.base}/getUpdates",
                        params={
                            "offset": self._offset,
                            "timeout": 25,
                            "allowed_updates": '["message"]',
                        },
                    )
                    if response.status_code == 401:
                        log.error("টেলিগ্রাম টোকেন ভুল — TELEGRAM_BOT_TOKEN চেক করুন।")
                        return
                    if response.status_code >= 400:
                        log.warning("getUpdates %s: %s", response.status_code, response.text[:200])
                        await asyncio.sleep(backoff)
                        backoff = min(60, backoff * 2)
                        continue
                    backoff = 3
                    data = response.json()
                    for update in data.get("result", []):
                        self._offset = max(self._offset, update.get("update_id", 0) + 1)
                        await self._handle(update, client)
                except asyncio.CancelledError:
                    raise
                except httpx.HTTPError as exc:
                    log.warning("টেলিগ্রাম নেটওয়ার্ক সমস্যা: %s", exc)
                    await asyncio.sleep(backoff)
                    backoff = min(60, backoff * 2)
                except Exception:  # pragma: no cover
                    log.exception("টেলিগ্রাম লুপে সমস্যা")
                    await asyncio.sleep(backoff)
                    backoff = min(60, backoff * 2)

    async def _handle(self, update: dict[str, Any], client: httpx.AsyncClient) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return

        allowed = self.settings.telegram_chat_ids
        if allowed and chat_id not in allowed:
            log.info("অননুমোদিত chat %s থেকে মেসেজ — /id ছাড়া কিছু পাঠানো হবে না", chat_id)
            if not text.lower().startswith("/id"):
                return

        if text.lower().startswith("/id"):
            await self._reply(
                client,
                chat_id,
                f"আপনার chat id: {chat_id}\n"
                "এটা .env-এর TELEGRAM_CHAT_IDS-এ দিন, তারপর সার্ভার রিস্টার্ট করুন।",
            )
            return

        log.info("টেলিগ্রাম কমান্ড [%s]: %s", chat_id, text[:80])

        command = text.split()[0].lower().split("@")[0]
        if command == "/live":
            await self._reply(client, chat_id, await self._live_text())
            return
        if command == "/help" or command == "/start":
            await self._reply(client, chat_id, self._help_text())
            return
        if command in COMMANDS:
            result = await self.agent.offline.command(command)
            await self._reply(client, chat_id, result["answer"])
            return

        result = await self.agent.answer(text)
        await self._reply(client, chat_id, result["answer"])

    async def _live_text(self) -> str:
        snapshot = await self.service.snapshot()
        if not snapshot.live:
            return (
                "🔌 লাইভ লোড পাওয়া যায়নি।\n"
                "DESCO/DPDC-র API এই মুহূর্তে কত ওয়াট চলছে তা দেয় না। "
                "ESP32 + PZEM-004T সেন্সর যোগ করলে সেকেন্ড-বাই-সেকেন্ড লোড দেখা যাবে।\n"
                f"ব্যালেন্স: {tk(snapshot.balance_tk)}"
            )
        live = snapshot.live
        source = "সিমুলেটেড (ডেমো)" if live.source == "simulated" else live.source
        parts = [f"⚡ লাইভ লোড: {bn(round(live.load_kw, 2))} kW"]
        if live.voltage:
            parts.append(f"ভোল্টেজ: {bn(round(live.voltage))} V")
        if live.current:
            parts.append(f"কারেন্ট: {bn(round(live.current, 2))} A")
        parts.append(f"সোর্স: {source}")
        return "\n".join(parts)

    def _help_text(self) -> str:
        lines = ["⚡ বিদ্যুৎ বন্ধু — কমান্ড:"]
        for cmd, desc in COMMANDS.items():
            if cmd in {"/start"}:
                continue
            lines.append(f"{cmd} — {desc}")
        lines.append("\nসরাসরি প্রশ্নও লিখতে পারেন, যেমন: “আর কত দিন চলবে?”")
        return "\n".join(lines)

    async def _reply(self, client: httpx.AsyncClient, chat_id: int, text: str) -> None:
        for chunk in self._split(text):
            try:
                await client.post(
                    f"{self.base}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": chunk,
                        "disable_web_page_preview": True,
                    },
                )
            except httpx.HTTPError as exc:  # pragma: no cover
                log.error("উত্তর পাঠানো যায়নি: %s", exc)
                return
