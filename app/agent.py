"""AI এজেন্ট — LLM টুল-কলিং, আর API key না থাকলে বিল্ট-ইন বাংলা এজেন্ট।

দুটোই একই টুল/সার্ভিস ব্যবহার করে, তাই উত্তরের ভিত্তি সবসময় আসল ডেটা।
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any

import httpx

from .config import Settings
from .formatting import bn, day_name, long_date, short_date, tk, units as fmt_units
from .service import MeterService

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """তুমি "বিদ্যুৎ বন্ধু" — বাংলাদেশের DESCO/DPDC প্রিপেইড বিদ্যুৎ মিটারের একজন সহকারী।

নিয়ম:
- ব্যবহারকারী যে ভাষায় লিখবে (বাংলা, Banglish বা English) সেই ভাষাতেই উত্তর দাও। ডিফল্ট বাংলা।
- সব সংখ্যা টুল থেকে নাও। নিজে থেকে কোনো ইউনিট, টাকা বা তারিখ বানিয়ে বলো না।
- প্রশ্নের সাথে সম্পর্কিত টুল কল করো; একাধিক তথ্য লাগলে একাধিক টুল।
- উত্তর ছোট ও সরাসরি রাখো (সাধারণত ২–৫ লাইন)। টাকার আগে ৳ চিহ্ন দাও।
- দরকার হলে হিসাবের ভাঙা অংশ দেখাও (যেমন স্ল্যাব বা ভ্যাট), কিন্তু বেশি লম্বা কোরো না।
- ডেটা পুরনো হলে বা ইউটিলিটির ডেটা দিনে একবার আসে বলে জানানো দরকার হলে সংক্ষেপে বলো।
- ডেমো মোডে চললে (টুলের ফলাফলে demo=true) একবার জানিয়ে দাও যে এটা নমুনা ডেটা।
- মিটার কেটে যাওয়ার ঝুঁকি থাকলে স্পষ্টভাবে সতর্ক করো এবং রিচার্জের পরামর্শ দাও।
"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "বর্তমান ব্যালেন্স, সর্বশেষ রিডিংয়ের তারিখ, ব্যালেন্স কত দিন চলবে, বাজেট ও সতর্কতা।",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_usage",
            "description": "দিনভিত্তিক বিদ্যুৎ ব্যবহার (ইউনিট), গড়, সর্বোচ্চ/সর্বনিম্ন দিন।",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "কত দিনের ডেটা (ডিফল্ট ১৪, সর্বোচ্চ ৯০)",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bill",
            "description": (
                "BERC ট্যারিফ অনুযায়ী বিলের হিসাব — স্ল্যাব, ডিমান্ড চার্জ, মিটার ভাড়া, "
                "রিবেট, ভ্যাট। units না দিলে এই মাসের এ পর্যন্ত ব্যবহার ধরে হিসাব হয়।"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "units": {
                        "type": "number",
                        "description": "হিসাব করতে চাইলে ইউনিট সংখ্যা (যেমন ২৫০)",
                    },
                    "month": {
                        "type": "string",
                        "description": "ঐচ্ছিক YYYY-MM; দিলে ওই মাসের ব্যবহার ধরা হয়।",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_forecast",
            "description": "পূর্বাভাস — ব্যালেন্স কবে শেষ হবে, মাস শেষে কত ইউনিট ও কত টাকার বিল হতে পারে।",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recharges",
            "description": "সাম্প্রতিক রিচার্জের তালিকা (তারিখ, টাকা, মাধ্যম)।",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "সর্বোচ্চ কতটি (ডিফল্ট ৫)"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tariff",
            "description": "BERC ট্যারিফ — স্ল্যাব রেট, ডিমান্ড চার্জ, মিটার ভাড়া, ভ্যাট, রিবেট।",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tips",
            "description": "ব্যবহারের ডেটা দেখে সাশ্রয়ের পরামর্শ।",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_monthly",
            "description": "শেষ কয়েক মাসের মোট ব্যবহার (ইউনিট) এবং এই মাসের তুলনা।",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


class AgentError(RuntimeError):
    pass


# ==================================================================
#  টুল রানার — LLM আর অফলাইন এজেন্ট দুজনেই এটা ব্যবহার করে
# ==================================================================
class ToolRunner:
    def __init__(self, service: MeterService):
        self.service = service
        self.calls: list[str] = []

    async def run(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        tool = getattr(self, f"_tool_{name}", None)
        if tool is None:
            return {"error": f"অজানা টুল: {name}"}
        self.calls.append(name)
        try:
            return await tool(**args)
        except TypeError as exc:
            return {"error": f"ভুল প্যারামিটার: {exc}"}
        except Exception as exc:  # pragma: no cover
            log.exception("টুল %s ব্যর্থ", name)
            return {"error": f"{type(exc).__name__}: {exc}"}

    async def _ctx(self):
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        return snapshot, analysis

    async def _tool_get_status(self) -> dict[str, Any]:
        snapshot, analysis = await self._ctx()
        return {
            "provider": self.service.settings.provider_label,
            "account_no": snapshot.account_no,
            "demo": self.service.settings.is_demo,
            "customer_name": snapshot.customer_name,
            "balance_tk": round(snapshot.balance_tk, 2),
            "balance_level": self.service.balance_state(snapshot)["level"],
            "low_balance_threshold_tk": self.service.settings.low_balance_tk,
            "reading_date": snapshot.reading_date,
            "reading_date_label": long_date(snapshot.reading_date),
            "data_age_minutes": round(
                max(
                    0.0,
                    (
                        datetime.now().astimezone() - snapshot.fetched_dt.astimezone()
                    ).total_seconds()
                    / 60.0,
                ),
                1,
            ),
            "days_left": analysis["forecast"]["days_left"],
            "empty_on": analysis["forecast"]["empty_on_label"],
            "avg_daily_units": analysis["forecast"]["avg_daily_units"],
            "avg_daily_cost_tk": analysis["forecast"]["avg_daily_cost"],
            "month_units": analysis["usage"]["month_units"],
            "month_bill_tk": round(analysis["bill"].total, 2),
            "alerts": [a["title_bn"] for a in self.service.build_alerts(snapshot, analysis)],
        }

    async def _tool_get_usage(self, days: int = 14) -> dict[str, Any]:
        days = max(1, min(90, int(days or 14)))
        snapshot, analysis = await self._ctx()
        series = self.service.daily_series(snapshot, days=days)
        rows = [
            {"date": r.date, "units": round(r.units, 2), "weekday": day_name(r.date)} for r in series
        ]
        total = sum(r.units for r in series)
        return {
            "days": days,
            "daily": rows,
            "total_units": round(total, 2),
            "avg_units": round(total / len(series), 2) if series else 0,
            "today": analysis["usage"]["today"],
            "last_day": analysis["usage"]["last_day"],
            "avg7": analysis["usage"]["avg7"],
            "avg30": analysis["usage"]["avg30"],
            "peak": analysis["usage"]["peak"],
            "lowest": analysis["usage"]["low"],
            "month_units": analysis["usage"]["month_units"],
            "change_vs_last_month_pct": analysis["usage"]["change_pct"],
            "anomaly": analysis["anomaly"],
        }

    async def _tool_get_bill(
        self, units: float | None = None, month: str | None = None
    ) -> dict[str, Any]:
        snapshot, analysis = await self._ctx()
        target_units = units
        basis = "এই মাসে এ পর্যন্ত ব্যবহার"
        if target_units is None and month:
            total = sum(
                v for k, v in analysis["units_map"].items() if k.startswith(month[:7])
            )
            if total > 0:
                target_units = total
                basis = f"{month} মাসের ব্যবহার"
        if target_units is None:
            target_units = analysis["usage"]["month_units"]
        bill = self.service.tariff.calculate(
            float(target_units),
            phase=snapshot.phase or self.service.settings.phase,
            sanctioned_load_kw=snapshot.sanctioned_load_kw
            or self.service.settings.sanction_load_kw,
        )
        data = bill.to_dict()
        data.update(
            {
                "basis": basis,
                "sanctioned_load_kw": snapshot.sanctioned_load_kw
                or self.service.settings.sanction_load_kw,
                "phase": snapshot.phase,
                "note": "ডিমান্ড চার্জ/মিটার ভাড়া পূর্ণ মাসের হিসেবে ধরা হয়েছে।",
            }
        )
        return data

    async def _tool_get_forecast(self) -> dict[str, Any]:
        snapshot, analysis = await self._ctx()
        forecast = dict(analysis["forecast"])
        forecast.update(
            {
                "balance_tk": round(snapshot.balance_tk, 2),
                "remaining_days_in_month": analysis["remaining_days"],
                "projected_bill_breakdown": analysis["projected_bill"].to_dict(),
                "budget_tk": self.service.settings.monthly_budget_tk,
                "confidence": forecast.get("confidence"),
            }
        )
        return forecast

    async def _tool_get_recharges(self, limit: int = 5) -> dict[str, Any]:
        snapshot, _ = await self._ctx()
        limit = max(1, min(20, int(limit or 5)))
        rows = snapshot.recharges[-limit:][::-1]
        return {
            "count": len(rows),
            "recharges": [r.to_dict() for r in rows],
            "total_recharged_tk": snapshot.total_recharged_tk,
        }

    async def _tool_get_tariff(self) -> dict[str, Any]:
        snapshot, _ = await self._ctx()
        summary = self.service.tariff.summary()
        analysis = self.service.analyze(snapshot)
        summary["current_month_units"] = analysis["usage"]["month_units"]
        summary["current_marginal_rate"] = self.service.tariff.marginal_rate(
            analysis["usage"]["month_units"]
        )
        return summary

    async def _tool_get_tips(self) -> dict[str, Any]:
        snapshot, analysis = await self._ctx()
        return {"tips": self.service.build_tips(snapshot, analysis)}

    async def _tool_get_monthly(self) -> dict[str, Any]:
        snapshot, analysis = await self._ctx()
        series = self.service.daily_series(snapshot, days=90)
        buckets: dict[str, float] = {}
        for row in series:
            buckets[row.date[:7]] = buckets.get(row.date[:7], 0.0) + row.units
        monthly = [
            {
                "month": key,
                "units": round(value, 2),
                "bill_tk": round(self.service.tariff.calculate(value).total, 2),
            }
            for key, value in sorted(buckets.items())
        ]
        return {
            "monthly": monthly,
            "current_month_units": analysis["usage"]["month_units"],
            "previous_month_units": analysis["usage"]["prev_month_units"],
            "change_pct": analysis["usage"]["change_pct"],
        }


# ==================================================================
#  অফলাইন বাংলা এজেন্ট (কোনো API key ছাড়াই)
# ==================================================================
# ক্রমটাই গুরুত্বপূর্ণ — আগে মেলা নিয়মটাই জেতে (নির্দিষ্ট প্রশ্ন আগে, সাধারণ পরে)
INTENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "help",
        ("সাহায্য", "help", "কী করতে পার", "কি করতে পার", "কমান্ড", "command", "কীভাবে চাল"),
    ),
    (
        "recharge",
        (
            "রিচার্জ",
            "recharge",
            "টপ আপ",
            "টপআপ",
            "topup",
            "top up",
            "পেমেন্ট",
            "payment",
            "লেনদেন",
            "transaction",
            "টাকা ঢুক",
            "টাকা দিলাম",
        ),
    ),
    (
        "tariff",
        ("ট্যারিফ", "tariff", "রেট", "rate", "স্ল্যাব", "slab", "প্রতি ইউনিট", "per unit", "দাম কত"),
    ),
    (
        "tips",
        ("টিপস", "tips", "পরামর্শ", "সাশ্রয়", "সাশ্রয়ী", "কমানোর", "কমানো", "কমাব", "save", "savings"),
    ),
    (
        "days",
        (
            "কত দিন",
            "কয়দিন",
            "কতদিন",
            "দিন চলবে",
            "শেষ হবে",
            "কবে শেষ",
            "kobe ses",
            "days left",
            "how many days",
            "কবে",
            "শেষ হয়ে যাবে",
        ),
    ),
    (
        "bill",
        (
            "বিল",
            "bill",
            "চার্জ",
            "charge",
            "হিসাব",
            "hisab",
            "কত লাগবে",
            "koto lagbe",
            "মোট কত টাকা",
        ),
    ),
    (
        "today",
        ("আজ", "আজকে", "আজকের", "today", "ajker", "aj koto", "ajke"),
    ),
    (
        "balance",
        (
            "ব্যালেন্স",
            "ব্যালেন",
            "balance",
            "কত টাকা আছে",
            "টাকা আছে",
            "টাকা বাকি",
            "taka ache",
            "wallet",
            "জমা",
        ),
    ),
    (
        "monthly",
        ("মাসিক", "monthly", "মাস শেষে", "মাসশেষে", "চলতি মাস", "this month", "মাসে কত", "গত মাস"),
    ),
    (
        "forecast",
        ("পূর্বাভাস", "forecast", "projection", "অনুমান", "আন্দাজ", "predict", "হবে কত"),
    ),
    (
        "usage",
        (
            "ব্যবহার",
            "ইউনিট",
            "unit",
            "খরচ",
            "khoroch",
            "consumption",
            "usage",
            "গ্রাফ",
            "chart",
            "history",
            "হিস্টোরি",
            "হিস্ট্রি",
        ),
    ),
    (
        "status",
        ("অবস্থা", "status", "সারসংক্ষেপ", "summary", "overview", "কেমন", "কী খবর", "কি খবর"),
    ),
]


def detect_intent(text: str) -> str:
    """বাংলা/Banglish/English — প্রথম যেই নিয়ম মেলে সেই ইনটেন্ট।"""

    lowered = f" {(text or '').lower().strip()} "
    for intent, keywords in INTENT_RULES:
        for kw in keywords:
            if kw in lowered:
                return intent
    return "status"


class OfflineAgent:
    """regex/কীওয়ার্ড ভিত্তিক, কিন্তু উত্তরগুলো আসল ডেটা থেকেই আসে।"""

    def __init__(self, service: MeterService):
        self.service = service
        self.tools = ToolRunner(service)

    async def answer(self, question: str) -> dict[str, Any]:
        text = (question or "").strip()
        if not text:
            return self._reply("কিছু লিখুন — যেমন: “ব্যালেন্স কত?” বা “আর কত দিন চলবে?”")

        if re.search(
            r"^(/start|/help|হ্যালো|hello|hi\b|assalamu|আসসালামু|সালাম)", text.lower()
        ):
            return self._reply(await self._help_text())

        intent = detect_intent(text)
        handler = getattr(self, f"_{intent}", None)
        if handler is None:
            handler = self._status
        return self._reply(await handler())

    def _reply(self, answer: str) -> dict[str, Any]:
        return {
            "answer": answer.strip(),
            "engine": "offline",
            "tools_used": self.tools.calls,
            "provider": self.service.settings.provider_label,
        }

    # ---------------- উত্তরগুলো ----------------
    async def _help_text(self) -> str:
        return (
            "⚡ বিদ্যুৎ বন্ধু — আমি যা জানি:\n"
            "• ব্যালেন্স কত আছে\n"
            "• আর কত দিন চলবে / কবে শেষ হবে\n"
            "• বিল কত হবে (BERC ট্যারিফে স্ল্যাব ধরে)\n"
            "• আজ বা এই মাসে কত ইউনিট খরচ\n"
            "• রিচার্জের হিস্ট্রি\n"
            "• সাশ্রয়ের টিপস\n"
            "বাংলা, Banglish বা English — যেভাবে খুশি লিখুন।"
        )

    async def _status(self) -> str:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        balance = snapshot.balance_tk
        forecast = analysis["forecast"]
        usage = analysis["usage"]
        mood = {
            "ok": "😊 ভালো আছে",
            "low": "⚠️ কমে এসেছে",
            "critical": "🪫 প্রায় শেষ",
        }[self.service.balance_state(snapshot)["level"]]
        parts = [
            f"⚡ ব্যালেন্স {tk(balance)} — {mood}",
            f"সর্বশেষ রিডিং: {short_date(snapshot.reading_date)} "
            f"({fmt_units(usage['last_day']['units'])} ইউনিট)",
            f"দৈনিক গড়: {fmt_units(usage['avg7'])} ইউনিট ≈ {tk(forecast['avg_daily_cost'])}",
        ]
        if forecast["days_left"] is not None:
            parts.append(f"এই ব্যালেন্সে চলবে প্রায় {fmt_units(forecast['days_left'])} দিন")
        parts.append(f"এই মাসে: {fmt_units(usage['month_units'])} ইউনিট, বিল ≈ {tk(analysis['bill'].total)}")
        return "\n".join(parts)

    async def _balance(self) -> str:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        state = self.service.balance_state(snapshot)
        forecast = analysis["forecast"]
        note = {
            "ok": "সীমার ওপরে আছেন।",
            "low": f"সতর্ক সীমা {tk(state['low_threshold'])}-এর নিচে।",
            "critical": "খুব কম — দ্রুত রিচার্জ করুন, নাহলে মিটার কেটে যেতে পারে।",
        }[state["level"]]
        line = f"💳 আপনার ব্যালেন্স {tk(snapshot.balance_tk)} — {note}"
        if forecast["days_left"] is not None:
            line += (
                f"\nগড় খরচ {tk(forecast['avg_daily_cost'])}/দিন হিসাবে আরও প্রায় "
                f"{fmt_units(forecast['days_left'])} দিন চলবে ({forecast['empty_on_label']})।"
            )
        return line

    async def _days(self) -> str:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        forecast = analysis["forecast"]
        if forecast["days_left"] is None:
            return (
                f"⏳ ব্যালেন্স {tk(snapshot.balance_tk)} আছে, কিন্তু খরচের হিসাব "
                "করার মতো যথেষ্ট ডেটা এখনো জমেনি।"
            )
        return (
            f"⏳ {tk(snapshot.balance_tk)} ব্যালেন্সে আনুমানিক "
            f"{fmt_units(forecast['days_left'])} দিন চলবে — "
            f"{forecast['empty_on_label']} নাগাদ শেষ হতে পারে।\n"
            f"(দৈনিক গড় {fmt_units(forecast['avg_daily_units'])} ইউনিট × "
            f"{tk(forecast['per_unit_rate'])} = {tk(forecast['avg_daily_cost'])})"
        )

    async def _bill(self) -> str:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        bill = analysis["bill"]
        rows = [
            f"🔹 এনার্জি চার্জ ({fmt_units(bill.units)} ইউনিট): {tk(bill.energy_charge)}",
            f"🔹 ডিমান্ড চার্জ: {tk(bill.demand_charge)}",
            f"🔹 মিটার ভাড়া: {tk(bill.meter_rent)}",
        ]
        if bill.rebate:
            rows.append(f"🔹 প্রিপেইড রিবেট: −{tk(bill.rebate)}")
        rows.append(f"🔹 ভ্যাট {bill.vat_percent:.0f}%: {tk(bill.vat)}")
        return (
            f"🧾 এই মাসে ({fmt_units(bill.units)} ইউনিট) এ পর্যন্ত আনুমানিক বিল "
            f"{tk(bill.total)}\n" + "\n".join(rows) + "\n"
            f"মাস শেষে দাঁড়াতে পারে প্রায় {tk(analysis['projected_bill'].total)} "
            f"({fmt_units(analysis['forecast']['month_end_units'])} ইউনিট ধরে)।"
        )

    async def _today(self) -> str:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        usage = analysis["usage"]
        if usage["today"] is None:
            last = usage["last_day"]
            avg = usage["avg7"] or 1
            diff = last["units"] - avg
            word = "বেশি" if diff > 0.2 else "কম" if diff < -0.2 else "প্রায় সমান"
            return (
                f"📅 আজকের রিডিং এখনো আসেনি — ইউটিলিটি সাধারণত দিনে একবার আপডেট করে।\n"
                f"সর্বশেষ ({short_date(last['date'])}): {fmt_units(last['units'])} ইউনিট, "
                f"গড়ের চেয়ে {fmt_units(abs(diff))} {word}।"
            )
        rate = analysis["marginal_rate"]
        return (
            f"📅 আজ ({short_date(analysis['today'].isoformat())}) "
            f"{fmt_units(usage['today'])} ইউনিট খরচ হয়েছে — আনুমানিক "
            f"{tk(usage['today'] * rate)}।\n"
            f"দৈনিক গড় {fmt_units(usage['avg7'])} ইউনিট, এই মাসে এ পর্যন্ত "
            f"{fmt_units(usage['month_units'])} ইউনিট।"
        )

    async def _usage(self, days: int = 7) -> str:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        series = self.service.daily_series(snapshot, days=7)
        usage = analysis["usage"]
        recent = " · ".join(f"{short_date(r.date)}: {fmt_units(r.units)}" for r in series[-5:])
        return (
            f"📊 দৈনিক গড় (৭ দিন): {fmt_units(usage['avg7'])} ইউনিট\n"
            f"শেষ ৫ দিন — {recent}\n"
            f"এই মাসে: {fmt_units(usage['month_units'])} ইউনিট"
            + (
                f" (গত মাসের একই সময়ের চেয়ে {bn(round(abs(usage['change_pct'])))}% "
                + ("বেশি)" if (usage["change_pct"] or 0) > 0 else "কম)")
                if usage["change_pct"] is not None
                else ""
            )
        )

    async def _recharge(self) -> str:
        snapshot = await self.service.snapshot()
        rows = snapshot.recharges[-5:][::-1]
        if not rows:
            return "🧾 কোনো রিচার্জের তথ্য পাওয়া যায়নি।"
        lines = [f"🧾 শেষ {len(rows)}টি রিচার্জ:"]
        for row in rows:
            stamp = row.time[:10]
            lines.append(f"• {short_date(stamp)} — {tk(row.amount_tk)} ({row.method or '—'})")
        total = sum(r.amount_tk for r in rows)
        lines.append(f"সর্বমোট: {tk(total)}")
        return "\n".join(lines)

    async def _tips(self) -> str:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        tips = self.service.build_tips(snapshot, analysis)[:3]
        lines = ["💡 আপনার ডেটা দেখে পরামর্শ:"]
        for tip in tips:
            lines.append(f"{tip['icon']} {tip['title_bn']} — {tip['detail_bn']}")
        return "\n".join(lines)

    async def _tariff(self) -> str:
        summary = self.service.tariff.summary()
        lines = [
            f"📋 {summary['label_bn']} ট্যারিফ (ভ্যাট {bn(summary['vat_percent'])}%, "
            f"ডিমান্ড {tk(summary['demand_charge_per_kw'])}/kW):"
        ]
        for slab in summary["slabs"]:
            upto = slab["up_to"]
            label = slab["label"] if upto else "শেষ স্ল্যাব (৬০০+)"
            lines.append(f"• {label}: {tk(slab['rate'])}/ইউনিট")
        lines.append(
            f"প্রিপেইড রিবেট: {tk(summary['prepaid_rebate_per_unit'])}/ইউনিট · "
            f"মিটার ভাড়া: {tk(summary['meter_rent'].get('single', 0))}"
        )
        return "\n".join(lines)

    async def _monthly(self) -> str:
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        forecast = analysis["forecast"]
        usage = analysis["usage"]
        return (
            f"📆 এই মাসে এ পর্যন্ত {fmt_units(usage['month_units'])} ইউনিট, "
            f"বিল ≈ {tk(analysis['bill'].total)}।\n"
            f"মাস শেষে আনুমানিক {fmt_units(forecast['month_end_units'])} ইউনিট — "
            f"বিল ≈ {tk(forecast['month_end_bill'])}।\n"
            f"গত মাসে (পূর্ণ): {fmt_units(usage['prev_month_units'])} ইউনিট।"
        )

    async def _forecast(self) -> str:
        return await self._monthly()

    # টেলিগ্রাম কমান্ড
    async def command(self, name: str) -> dict[str, Any]:
        mapping = {
            "balance": self._balance,
            "today": self._today,
            "bill": self._bill,
            "days": self._days,
            "tips": self._tips,
            "recharge": self._recharge,
            "monthly": self._monthly,
            "tariff": self._tariff,
            "status": self._status,
            "usage": self._usage,
        }
        handler = mapping.get(name.strip("/").lower())
        if handler is None:
            return self._reply(self._help_text())
        return self._reply(await handler())


# ==================================================================
#  LLM এজেন্ট (OpenAI-compatible tool calling)
# ==================================================================
class LlmClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.url = settings.llm_base_url.rstrip("/") + "/chat/completions"

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": 0.3,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout) as client:
            response = await client.post(self.url, json=payload, headers=headers)
        if response.status_code >= 400:
            detail = response.text[:300]
            raise AgentError(f"LLM API {response.status_code}: {detail}")
        return response.json()


class Agent:
    """বাইরের দুনিয়ার সাথে একমাত্র ইন্টারফেস: ``await agent.answer(...)``।"""

    def __init__(self, service: MeterService, settings: Settings):
        self.service = service
        self.settings = settings
        self.offline = OfflineAgent(service)
        self.tools = ToolRunner(service)
        self.client = LlmClient(settings) if settings.llm_enabled else None

    async def answer(
        self, question: str, history: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        started = time.perf_counter()
        if not self.client:
            result = await self.offline.answer(question)
            result["latency_ms"] = int((time.perf_counter() - started) * 1000)
            return result

        try:
            answer, used = await self._llm_answer(question, history or [])
            if not answer:
                raise AgentError("খালি উত্তর এসেছে")
            return {
                "answer": answer.strip(),
                "engine": "llm",
                "model": self.settings.llm_model,
                "tools_used": used,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "provider": self.service.settings.provider_label,
            }
        except Exception as exc:
            log.warning("LLM ব্যর্থ (%s) — অফলাইন এজেন্টে ফিরে আসছি", exc)
            result = await self.offline.answer(question)
            result["engine"] = "offline-fallback"
            result["llm_error"] = str(exc)[:200]
            result["latency_ms"] = int((time.perf_counter() - started) * 1000)
            return result

    async def _llm_answer(
        self, question: str, history: list[dict[str, str]]
    ) -> tuple[str, list[str]]:
        assert self.client is not None
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in history[-8:]:
            role = turn.get("role")
            content = turn.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": str(content)[:2000]})
        messages.append({"role": "user", "content": question})

        used: list[str] = []
        for _ in range(self.settings.llm_max_rounds):
            data = await self.client.chat(messages, TOOLS)
            choices = data.get("choices") or []
            if not choices:
                raise AgentError("LLM রেসপন্সে choices নেই")
            message = choices[0].get("message") or {}
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = fn.get("name", "")
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = {}
                    result = await self.tools.run(name, args if isinstance(args, dict) else {})
                    used.append(name)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "name": name,
                            "content": json.dumps(result, ensure_ascii=False, default=str)[:8000],
                        }
                    )
                continue
            content = (message.get("content") or "").strip()
            return content, used

        # রাউন্ড শেষ, তবু টুল-কল চলছিল — শেষ অবস্থা থেকে সংক্ষিপ্ত উত্তর
        snapshot = await self.service.snapshot()
        analysis = self.service.analyze(snapshot)
        return (
            f"⚡ ব্যালেন্স {tk(snapshot.balance_tk)}, এই মাসে "
            f"{fmt_units(analysis['usage']['month_units'])} ইউনিট "
            f"(≈ {tk(analysis['bill'].total)})।",
            used,
        )
