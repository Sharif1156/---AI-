"""ডেমো প্রোভাইডার — আসল মিটার ছাড়াই পুরো অ্যাপ দেখা যায়।

ডেটা তারিখভিত্তিক ডিটারমিনিস্টিক, তাই বারবার রিস্টার্ট করলেও একই দিনে
একই রিপোর্ট আসে। গত ১২০ দিনের ব্যবহার, রিচার্জ হিস্ট্রি আর একটা
ওয়ালেট-লেজার সিমুলেট করা হয় যাতে ব্যালেন্স বাস্তবসম্মত দেখায়।
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date, datetime, time, timedelta

from ..config import Settings
from ..formatting import now_local, tz
from ..models import DailyUsage, LiveLoad, MonthlyUsage, Recharge, Snapshot
from ..tariff import Tariff
from .base import BaseProvider

DEMO_NAME = "মোঃ রফিকুল ইসলাম"
DEMO_ADDRESS = "বাসা ১২, রোড ৫, ধানমন্ডি, ঢাকা-১২০৫"
DEMO_METER = "D-8823147"
HISTORY_DAYS = 120
TARGET_BALANCE = 214.0


def _seed(*parts: object) -> int:
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:12], 16)


class DemoProvider(BaseProvider):
    name = "demo"
    label_bn = "ডেমো"
    supports_live = True

    def __init__(self, settings: Settings, tariff: Tariff):
        self.settings = settings
        self.tariff = tariff

    # ---------------- পাবলিক ----------------
    async def fetch(self) -> Snapshot:
        now = now_local(self.settings.timezone)
        today = now.date()
        last_complete = today - timedelta(days=1)
        first_day = last_complete - timedelta(days=HISTORY_DAYS - 1)

        days = self._usage_series(first_day, last_complete)
        recharges, start_balance, balance = self._ledger(days, last_complete)

        daily = [
            DailyUsage(date=d.isoformat(), units=round(units, 2), cost_tk=round(cost, 2))
            for d, units, cost in days
        ]
        monthly = self._monthly(days)
        total_used = round(sum(u for _, u, _ in days), 2)

        return Snapshot(
            provider=self.name,
            account_no=self.settings.account_no or "12345678",
            balance_tk=round(balance, 2),
            reading_date=last_complete.isoformat(),
            fetched_at=now.isoformat(timespec="seconds"),
            customer_name=self.settings.customer_name or DEMO_NAME,
            address=DEMO_ADDRESS,
            meter_no=DEMO_METER,
            tariff_type="Residential",
            sanctioned_load_kw=self.settings.sanction_load_kw,
            phase=self.settings.phase,
            total_used_units=round(18420.5 + total_used, 2),
            total_recharged_tk=round(24800 + sum(r.amount_tk for r in recharges), 2),
            daily=daily,
            monthly=monthly,
            recharges=recharges,
            live=self._live_load(now),
            extra={
                "demo": True,
                "start_balance_tk": round(start_balance, 2),
                "history_days": HISTORY_DAYS,
                "note_bn": "এটি ডেমো ডেটা — আপনার আসল মিটারের সাথে সম্পর্ক নেই।",
            },
        )

    # ---------------- ব্যবহার সিরিজ ----------------
    def _usage_series(
        self, first_day: date, last_day: date
    ) -> list[tuple[date, float, float]]:
        """প্রতিদিনের ইউনিট ও সেই দিনের আনুমানিক খরচ (টাকা)।"""

        # বাস্তবসম্মত স্পাইক — গড়ের ১.৫ গুণের বেশি, যাতে অ্যালার্ট দেখা যায়
        spike_offsets = {0: 14.8, 18: 13.4, 44: 12.9, 71: 13.8, 96: 12.6}

        rows: list[tuple[date, float, float]] = []
        month_units: dict[str, float] = {}
        cursor = first_day
        while cursor <= last_day:
            offset = (last_day - cursor).days
            base = 7.15
            # সাপ্তাহিক ছন্দ — ছুটির দিনে একটু বেশি
            weekday_factor = {0: 1.0, 1: 1.0, 2: 1.02, 3: 1.06, 4: 1.18, 5: 1.12, 6: 0.94}[
                cursor.weekday()
            ]
            # মৌসুমি প্রভাব (শীতে কম, গরমে বেশি)
            doy = cursor.timetuple().tm_yday
            seasonal = 1.0 + 0.16 * math.sin(2 * math.pi * (doy - 100) / 365.0)
            rng = random.Random(_seed(self.settings.account_no, cursor.isoformat()))
            noise = rng.uniform(0.86, 1.14)
            units = base * weekday_factor * seasonal * noise
            if offset in spike_offsets:
                units = spike_offsets[offset]
            units = round(max(1.8, min(24.0, units)), 2)

            key = cursor.strftime("%Y-%m")
            month_units[key] = month_units.get(key, 0.0) + units
            rate = self.tariff.marginal_rate(month_units[key])
            cost = round(units * rate, 2)
            rows.append((cursor, units, cost))
            cursor += timedelta(days=1)
        return rows

    def _monthly(self, days: list[tuple[date, float, float]]) -> list[MonthlyUsage]:
        buckets: dict[str, float] = {}
        for d, units, _ in days:
            key = d.strftime("%Y-%m")
            buckets[key] = buckets.get(key, 0.0) + units
        # মাস অনুযায়ী সাজিয়ে শেষ ৬টা
        keys = sorted(buckets)[-6:]
        return [MonthlyUsage(month=k, units=round(buckets[k], 2)) for k in keys]

    # ---------------- ওয়ালেট লেজার ----------------
    def _ledger(
        self, days: list[tuple[date, float, float]], last_day: date
    ) -> tuple[list[Recharge], float, float]:
        total_spend = sum(cost for _, _, cost in days)

        plan = [
            (118, 800.0, "bKash"),
            (104, 900.0, "Nagad"),
            (92, 700.0, "DESCO অ্যাপ"),
            (80, 800.0, "bKash"),
            (66, 900.0, "Rocket"),
            (53, 700.0, "Nagad"),
            (41, 800.0, "DESCO অ্যাপ"),
            (30, 900.0, "bKash"),
        ]
        fixed_total = sum(amount for _, amount, _ in plan)
        # শেষ রিচার্জটা এমনভাবে ধরুন যাতে আজকের ব্যালেন্স বাস্তবসম্মত হয়
        remainder = total_spend - fixed_total - 100.0
        remainder = max(300.0, round(remainder / 10.0) * 10.0)
        plan.append((10, remainder, "bKash"))

        recharges: list[Recharge] = []
        for offset, amount, method in plan:
            d = last_day - timedelta(days=offset)
            if d < days[0][0]:
                continue
            stamp = datetime.combine(d, time(11, 12), tzinfo=tz(self.settings.timezone))
            ref = hashlib.md5(f"{offset}-{amount}".encode()).hexdigest()[:10].upper()
            units = amount / max(1.0, self.tariff.marginal_rate(140.0))
            recharges.append(
                Recharge(
                    time=stamp.isoformat(timespec="seconds"),
                    amount_tk=amount,
                    method=method,
                    units=round(units, 2),
                    reference=f"TRX{ref}",
                )
            )
        recharges.sort(key=lambda r: r.dt or datetime.min)

        total_recharge = sum(r.amount_tk for r in recharges)
        start_balance = max(0.0, TARGET_BALANCE + total_spend - total_recharge)
        return recharges, start_balance, start_balance + total_recharge - total_spend

    # ---------------- লাইভ লোড (সিমুলেটেড) ----------------
    def _live_load(self, now: datetime) -> LiveLoad:
        hour = now.hour + now.minute / 60.0
        if 18 <= hour < 22.5:
            base = 1.55
        elif 22.5 <= hour or hour < 5:
            base = 0.34
        elif 5 <= hour < 9:
            base = 0.72
        elif 11 <= hour < 16:
            base = 0.46
        else:
            base = 0.62
        bucket = int(now.timestamp() // 30)
        rng = random.Random(bucket)
        load = max(0.05, base + rng.uniform(-0.18, 0.28) + 0.08 * math.sin(hour))
        voltage = 220 + rng.uniform(-5, 6)
        current = load * 1000.0 / voltage
        return LiveLoad(
            load_kw=round(load, 2),
            voltage=round(voltage, 1),
            current=round(current, 2),
            source="simulated",
        )
