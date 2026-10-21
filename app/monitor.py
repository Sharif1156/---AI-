"""ব্যাকগ্রাউন্ড মনিটর — নিয়মিত পোলিং, অ্যালার্ট পাঠানো আর দৈনিক রিপোর্ট।"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Protocol

from .config import Settings
from .formatting import bn, long_date, now_local, relative_ago, short_date, tk, units as fmt_units
from .service import MeterService

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send(self, text: str, chat_ids: list[int] | None = None) -> bool: ...


class Monitor:
    """প্রতি ৩০ মিনিটে ব্যালেন্স চেক + দিনে একবার সারাংশ।"""

    def __init__(
        self,
        service: MeterService,
        settings: Settings,
        notifier: Notifier | None = None,
    ):
        self.service = service
        self.settings = settings
        self.notifier = notifier
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_poll: datetime | None = None
        self.last_result: dict[str, Any] | None = None

    # ---------------- লাইফসাইকেল ----------------
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="monitor-loop")
        log.info("মনিটর চালু — প্রতি %s মিনিটে চেক", self.settings.poll_minutes)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(3)  # সার্ভার পুরো চালু হতে সময় দিন
        while not self._stop.is_set():
            try:
                now = now_local(self.settings.timezone)
                if self._report_due(now):
                    await self.send_daily_report(now)
                if self._poll_due(now):
                    await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("মনিটর লুপে সমস্যা — ৩০ সেকেন্ড পর আবার")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                continue

    def _poll_due(self, now: datetime) -> bool:
        if self._last_poll is None:
            return True
        elapsed = (now - self._last_poll).total_seconds() / 60.0
        return elapsed >= self.settings.poll_minutes

    def _report_due(self, now: datetime) -> bool:
        """নির্ধারিত সময়ের ৩ ঘণ্টার জানালায় একবার — দেরিতে চালু হলেও মিস হয় না,
        কিন্তু রাত ১১টায় "শুভ সকাল" চলে যায় না।"""

        hour = self.settings.report_hour
        if hour > 23:
            return False
        if not (hour <= now.hour < hour + 3):
            return False
        return self.service.state.last_report != now.date().isoformat()

    # ---------------- একবার চেক ----------------
    async def poll_once(self, send_alerts: bool = True) -> dict[str, Any]:
        self._last_poll = now_local(self.settings.timezone)
        result: dict[str, Any] = {
            "at": self._last_poll.isoformat(timespec="seconds"),
            "alerts": [],
            "sent": [],
            "recharge": None,
            "ok": True,
        }
        try:
            snapshot = await self.service.refresh()
        except Exception as exc:
            result["ok"] = False
            result["error"] = str(exc)
            log.error("পোলিং ব্যর্থ: %s", exc)
            self.last_result = result
            return result

        analysis = self.service.analyze(snapshot)
        alerts = self.service.build_alerts(snapshot, analysis)
        result["alerts"] = alerts
        result["balance_tk"] = snapshot.balance_tk
        result["reading_date"] = snapshot.reading_date

        # ব্যালেন্স ভালো হয়ে গেলে পুরনো সতর্কতা রিসেট
        if not any(a["key"].startswith("balance") for a in alerts):
            self.service.state.clear_alert("balance_low")
            self.service.state.clear_alert("balance_critical")
        if not any(a["key"] == "days_left" for a in alerts):
            self.service.state.clear_alert("days_left")

        # নতুন রিচার্জ?
        recharge = self._detect_recharge(snapshot)
        if recharge:
            result["recharge"] = recharge
            if send_alerts:
                await self._notify(
                    "✅ রিচার্জ সফল\n"
                    f"{tk(recharge['amount_tk'])} যোগ হয়েছে "
                    f"({recharge.get('method') or 'অজানা মাধ্যম'})।\n"
                    f"এখন ব্যালেন্স {tk(snapshot.balance_tk)} — "
                    f"আনুমানিক {fmt_units(analysis['forecast']['days_left'] or 0)} দিন চলবে।",
                    key="recharge_success",
                )

        today = self._last_poll.date().isoformat()
        for alert in alerts:
            if not send_alerts:
                continue
            if self.service.state.alert_sent_on(alert["key"]) == today:
                continue
            ok = await self._notify(self.format_alert(alert), key=alert["key"])
            if ok:
                result["sent"].append(alert["key"])
                self.service.state.mark_alert(alert["key"], on=self._last_poll.date())

        result["fresh_data"] = True
        self.last_result = result
        log.info(
            "চেক সম্পন্ন: ব্যালেন্স %s, %sটি অ্যালার্ট, %sটি পাঠানো হয়েছে",
            tk(snapshot.balance_tk),
            len(alerts),
            len(result["sent"]),
        )
        return result

    def _detect_recharge(self, snapshot) -> dict[str, Any] | None:
        latest = snapshot.newest_recharge()
        if latest is None:
            return None
        identifier = f"{latest.time}|{latest.amount_tk}"
        previous = self.service.state.last_recharge_id
        self.service.state.set_last_recharge_id(identifier)
        if previous is None or previous == identifier:
            return None
        return latest.to_dict()

    # ---------------- অ্যালার্ট পাঠানো ----------------
    @staticmethod
    def format_alert(alert: dict[str, Any]) -> str:
        return f"{alert.get('icon', '🔔')} {alert['title_bn']}\n{alert['body_bn']}"

    async def _notify(self, text: str, key: str = "") -> bool:
        if not self.notifier or not self.settings.telegram_chat_ids:
            log.info("[%s] %s", key or "তথ্য", text.replace("\n", " | "))
            return False
        try:
            ok = await self.notifier.send(text)
            if ok:
                log.info("টেলিগ্রামে পাঠানো হয়েছে: %s", key or "তথ্য")
            return bool(ok)
        except Exception as exc:  # pragma: no cover
            log.error("টেলিগ্রামে পাঠানো যায়নি: %s", exc)
            return False

    # ---------------- দৈনিক রিপোর্ট ----------------
    async def send_daily_report(self, now: datetime | None = None) -> str:
        now = now or now_local(self.settings.timezone)
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        text = self.build_daily_report(snapshot, analysis)
        await self._notify(text, key="daily_report")
        self.service.state.mark_report(now.date())
        log.info("দৈনিক রিপোর্ট তৈরি হয়েছে (%s)", long_date(now.date()))
        return text

    def build_daily_report(self, snapshot, analysis) -> str:
        usage = analysis["usage"]
        forecast = analysis["forecast"]
        last = usage["last_day"]
        trend = ""
        if usage["change_pct"] is not None:
            direction = "বেশি" if usage["change_pct"] > 0 else "কম"
            trend = f" (গত মাসের একই সময়ের চেয়ে {bn(abs(usage['change_pct']))}% {direction})"
        lines = [
            f"🌅 শুভ সকাল! — {long_date(now_local(self.settings.timezone).date())}",
            f"ব্যালেন্স: {tk(snapshot.balance_tk)}",
            f"গতকাল ({short_date(last['date'])}): {fmt_units(last['units'])} ইউনিট",
            f"৭ দিনের গড়: {fmt_units(usage['avg7'])} ইউনিট/দিন",
        ]
        if forecast["days_left"] is not None:
            lines.append(
                f"ব্যালেন্স থাকবে: প্রায় {fmt_units(forecast['days_left'])} দিন "
                f"({forecast['empty_on_label']})"
            )
        lines.append(
            f"এই মাসে: {fmt_units(usage['month_units'])} ইউনিট ≈ {tk(analysis['bill'].total)}{trend}"
        )
        lines.append(f"মাস শেষে আনুমানিক বিল: {tk(forecast['month_end_bill'])}")

        anomaly = analysis["anomaly"]
        if anomaly:
            lines.append(
                f"📈 {short_date(anomaly['date'])}-এ খরচ গড়ের {bn(anomaly['ratio'])} গুণ ছিল।"
            )
        if snapshot.reading_date:
            lines.append(f"🕓 সর্বশেষ রিডিং: {relative_ago(snapshot.fetched_dt)}")
        return "\n".join(lines)
