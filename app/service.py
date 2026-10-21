"""মিটার সার্ভিস — ক্যাশিং, ব্যবহার বিশ্লেষণ, পূর্বাভাস, অ্যালার্ট আর টিপস।"""

from __future__ import annotations

import asyncio
import logging
import statistics
from datetime import date, datetime, timedelta
from typing import Any

from .config import Settings
from .formatting import (
    bn,
    day_name,
    long_date,
    now_local,
    relative_ago,
    short_date,
    tk,
    units as fmt_units,
)
from .models import DailyUsage, Snapshot, parse_date
from .providers.base import BaseProvider, ProviderError
from .store import HistoryStore, SnapshotCache, StateStore
from .tariff import BillResult, Tariff

log = logging.getLogger(__name__)


def _mean(values: list[float]) -> float:
    clean = [v for v in values if v is not None]
    return float(statistics.fmean(clean)) if clean else 0.0


class MeterService:
    """সব ফিচারের কেন্দ্র — API, এজেন্ট আর মনিটর এটাই ব্যবহার করে।"""

    def __init__(
        self,
        settings: Settings,
        tariff: Tariff,
        provider: BaseProvider,
        cache: SnapshotCache,
        history: HistoryStore,
        state: StateStore,
    ):
        self.settings = settings
        self.tariff = tariff
        self.provider = provider
        self.cache = cache
        self.history = history
        self.state = state
        self._lock = asyncio.Lock()
        self._last_error: dict[str, Any] | None = None
        self._last_attempt: datetime | None = None
        self._fetched_at_monotonic: float | None = None

    # ================= ডেটা আনা =================
    @property
    def last_error(self) -> dict[str, Any] | None:
        return self._last_error

    async def snapshot(self, force: bool = False) -> Snapshot:
        """ক্যাশ থেকে দেয়; মেয়াদ শেষ হলে বা force=True হলে প্রোভাইডারকে জিজ্ঞাসা করে।"""

        async with self._lock:
            cached = self.cache.snapshot
            if not force and cached is not None and self._is_fresh(cached):
                return cached

            self._last_attempt = now_local(self.settings.timezone)
            try:
                fresh = await self.provider.fetch()
            except ProviderError as exc:
                self._last_error = {**exc.to_dict(), "at": self._last_attempt.isoformat()}
                log.error("প্রোভাইডার এরর: %s", exc)
                if cached is not None:
                    return cached
                raise
            except Exception as exc:  # pragma: no cover - অপ্রত্যাশিত
                self._last_error = {
                    "message": f"{type(exc).__name__}: {exc}",
                    "kind": "unknown",
                    "hint": "প্রোভাইডার কোডে সমস্যা। PROVIDER=demo দিয়ে চালিয়ে দেখুন।",
                    "at": self._last_attempt.isoformat(),
                }
                log.exception("প্রোভাইডার থেকে ডেটা আনতে ব্যর্থ")
                if cached is not None:
                    return cached
                raise

            self._last_error = None
            self.cache.set(fresh)
            self.history.sync_snapshot(fresh)
            self._fetched_at_monotonic = asyncio.get_event_loop().time()
            log.info(
                "নতুন রিডিং: ব্যালেন্স %s, তারিখ %s",
                tk(fresh.balance_tk),
                fresh.reading_date,
            )
            return fresh

    def _is_fresh(self, snapshot: Snapshot) -> bool:
        if self.settings.cache_seconds <= 0:
            return False
        try:
            fetched = snapshot.fetched_dt
        except Exception:
            return False
        age = (now_local(self.settings.timezone) - fetched.astimezone()).total_seconds()
        return 0 <= age < self.settings.cache_seconds

    async def refresh(self) -> Snapshot:
        return await self.snapshot(force=True)

    # ================= ব্যবহারের সিরিজ =================
    def daily_series(self, snapshot: Snapshot, days: int = 60) -> list[DailyUsage]:
        """স্ন্যাপশট + হিস্ট্রি মিলিয়ে দিনভিত্তিক সিরিজ (পুরনো ডেটা হারায় না)।"""

        merged: dict[str, float] = {}
        for row in self.history.series(days=days + 5, end=self._reading_day(snapshot)):
            merged[row.date] = row.units
        for row in snapshot.daily_sorted():
            merged[row.date] = row.units

        rows = [
            DailyUsage(date=key, units=round(value, 2))
            for key, value in merged.items()
            if parse_date(key)
        ]
        rows.sort(key=lambda r: r.date)
        cutoff = self._reading_day(snapshot) - timedelta(days=days - 1)
        return [r for r in rows if (parse_date(r.date) or cutoff) >= cutoff]

    def _reading_day(self, snapshot: Snapshot) -> date:
        return parse_date(snapshot.reading_date) or now_local(self.settings.timezone).date()

    # ================= মূল বিশ্লেষণ =================
    def analyze(self, snapshot: Snapshot) -> dict[str, Any]:
        tz_name = self.settings.timezone
        today = now_local(tz_name).date()
        reading_day = self._reading_day(snapshot)
        series = self.daily_series(snapshot, days=90)
        units_map = {r.date: r.units for r in series}
        values = list(units_map.values())

        avg7 = _mean(values[-7:])
        avg30 = _mean(values[-30:])
        # দীর্ঘমেয়াদি গড়ে বেশি ওজন, কিন্তু সাম্প্রতিক পরিবর্তনও ধরা পড়ে
        basis = 0.6 * avg7 + 0.4 * avg30 if values else 0.0

        month_start = today.replace(day=1)
        month_rows = [
            (parse_date(k), v) for k, v in units_map.items() if (parse_date(k) or today) >= month_start
        ]
        month_units = round(sum(v for _, v in month_rows), 2)
        month_days = max(1, len(month_rows))
        days_elapsed = max(1, min(today.day, (reading_day - month_start).days + 1))

        prev_month_end = month_start - timedelta(days=1)
        prev_month_start = prev_month_end.replace(day=1)
        prev_units = round(
            sum(
                v
                for k, v in units_map.items()
                if prev_month_start <= (parse_date(k) or today) <= prev_month_end
            ),
            2,
        )
        prev_same_period = round(
            sum(
                v
                for k, v in units_map.items()
                if prev_month_start
                <= (parse_date(k) or today)
                <= min(prev_month_end, prev_month_start + timedelta(days=today.day - 1))
            ),
            2,
        )
        change_pct = (
            round((month_units - prev_same_period) / prev_same_period * 100, 1)
            if prev_same_period > 0
            else None
        )

        peak_key = max(units_map, key=units_map.get) if units_map else None
        low_key = min(units_map, key=units_map.get) if units_map else None

        usage = {
            "today": units_map.get(today.isoformat()),
            "today_known": today.isoformat() in units_map,
            "last_day": {
                "date": reading_day.isoformat(),
                "units": round(units_map.get(reading_day.isoformat(), 0.0), 2),
            },
            "avg7": round(avg7, 2),
            "avg30": round(avg30, 2),
            "month_units": month_units,
            "month_days": month_days,
            "prev_month_units": prev_units,
            "prev_same_period_units": prev_same_period,
            "change_pct": change_pct,
            "peak": (
                {"date": peak_key, "units": round(units_map[peak_key], 2)} if peak_key else None
            ),
            "low": ({"date": low_key, "units": round(units_map[low_key], 2)} if low_key else None),
            "days_tracked": len(values),
            "total_units_lifetime": snapshot.total_used_units,
        }

        # ---- বিল ----
        marginal = self.tariff.marginal_rate(month_units)
        bill = self.tariff.calculate(
            month_units,
            phase=snapshot.phase or self.settings.phase,
            sanctioned_load_kw=snapshot.sanctioned_load_kw or self.settings.sanction_load_kw,
            prorate_days=days_elapsed,
            days_in_month=self._days_in_month(today),
        )

        # ---- পূর্বাভাস ----
        days_in_month = self._days_in_month(today)
        remaining_days = max(0, days_in_month - today.day)
        daily_cost = basis * marginal if basis else 0.0
        days_left = snapshot.balance_tk / daily_cost if daily_cost > 0 else None
        empty_on = (
            (reading_day + timedelta(days=int(days_left))) if days_left is not None else None
        )
        month_end_units = round(month_units + basis * (remaining_days + 1), 2)
        projected = self.tariff.calculate(
            month_end_units,
            phase=snapshot.phase or self.settings.phase,
            sanctioned_load_kw=snapshot.sanctioned_load_kw or self.settings.sanction_load_kw,
        )

        confidence = (
            "উচ্চ"
            if len(values) >= 14
            else "মধ্যম"
            if len(values) >= 5
            else "কম"
            if values
            else "নেই"
        )

        forecast = {
            "avg_daily_units": round(basis, 2),
            "avg_daily_cost": round(daily_cost, 2),
            "per_unit_rate": round(marginal, 2),
            "days_left": round(days_left, 1) if days_left is not None else None,
            "empty_on": empty_on.isoformat() if empty_on else None,
            "empty_on_label": long_date(empty_on) if empty_on else "—",
            "month_end_units": month_end_units,
            "month_end_bill": projected.total,
            "month_end_balance": round(
                max(0.0, snapshot.balance_tk - daily_cost * (remaining_days + 1)), 2
            ),
            "confidence": confidence,
            "basis_days": len(values),
        }

        # ---- ত্রুটি শনাক্ত ----
        anomaly = self._detect_anomaly(series)

        return {
            "usage": usage,
            "forecast": forecast,
            "bill": bill,
            "projected_bill": projected,
            "anomaly": anomaly,
            "series": series,
            "units_map": units_map,
            "marginal_rate": marginal,
            "reading_day": reading_day,
            "today": today,
            "remaining_days": remaining_days,
            "days_in_month": days_in_month,
        }

    @staticmethod
    def _days_in_month(day: date) -> int:
        if day.month == 12:
            return 31
        return (day.replace(month=day.month + 1, day=1) - timedelta(days=1)).day

    def _detect_anomaly(self, series: list[DailyUsage]) -> dict[str, Any] | None:
        if len(series) < 4:
            return None
        latest = series[-1]
        history = [r.units for r in series[-15:-1]]
        if len(history) < 3:
            return None
        base = _mean(history)
        if base <= 0:
            return None
        ratio = latest.units / base
        if ratio < self.settings.anomaly_multiplier:
            return None
        extra_units = latest.units - base
        return {
            "date": latest.date,
            "units": round(latest.units, 2),
            "baseline": round(base, 2),
            "ratio": round(ratio, 2),
            "extra_units": round(extra_units, 2),
            "extra_tk": round(extra_units * self.tariff.marginal_rate(latest.units), 2),
        }

    # ================= ব্যালেন্স-সংক্রান্ত অবস্থা =================
    def balance_state(self, snapshot: Snapshot) -> dict[str, Any]:
        low = self.settings.low_balance_tk
        critical = min(self.settings.critical_balance_tk, low)
        level = "ok"
        if snapshot.balance_tk <= critical:
            level = "critical"
        elif snapshot.balance_tk <= low:
            level = "low"
        return {
            "tk": round(snapshot.balance_tk, 2),
            "level": level,
            "low_threshold": low,
            "critical_threshold": critical,
            "remaining_to_low": round(snapshot.balance_tk - low, 2),
        }

    # ================= অ্যালার্ট =================
    def build_alerts(
        self, snapshot: Snapshot, analysis: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        analysis = analysis or self.analyze(snapshot)
        tz_name = self.settings.timezone
        now = now_local(tz_name)
        reading_day = analysis["reading_day"]
        forecast = analysis["forecast"]
        balance = self.balance_state(snapshot)
        alerts: list[dict[str, Any]] = []

        # ১) কম ব্যালেন্স
        if balance["level"] == "critical":
            alerts.append(
                {
                    "key": "balance_critical",
                    "severity": "critical",
                    "icon": "🪫",
                    "title_bn": "ব্যালেন্স প্রায় শেষ!",
                    "body_bn": (
                        f"মাত্র {tk(snapshot.balance_tk)} বাকি — "
                        f"{fmt_units(max(0.0, forecast['days_left'] or 0))} দিনের বেশি চলবে না। "
                        "এখনই রিচার্জ করুন, নাহলে মিটার কেটে যাবে।"
                    ),
                }
            )
        elif balance["level"] == "low":
            alerts.append(
                {
                    "key": "balance_low",
                    "severity": "warning",
                    "icon": "⚠️",
                    "title_bn": "ব্যালেন্স কমে গেছে",
                    "body_bn": (
                        f"ব্যালেন্স {tk(snapshot.balance_tk)}, যা সীমার ({tk(balance['low_threshold'])}) "
                        f"নিচে। আনুমানিক {fmt_units(forecast['days_left'])} দিন চলবে।"
                    ),
                }
            )

        # ২) কয়েকদিনের কম বাকি
        days_left = forecast.get("days_left")
        if days_left is not None and days_left <= self.settings.days_left_warning:
            alerts.append(
                {
                    "key": "days_left",
                    "severity": "warning",
                    "icon": "⏳",
                    "title_bn": f"আর মাত্র {fmt_units(max(0.0, days_left))} দিনের ব্যালেন্স",
                    "body_bn": (
                        f"এই খরচের ধারায় {tk(snapshot.balance_tk)} দিয়ে "
                        f"প্রায় {fmt_units(max(0.0, days_left))} দিন চলবে — "
                        f"{forecast['empty_on_label']}-এর দিকে শেষ হয়ে যাবে।"
                    ),
                }
            )

        # ৩) অস্বাভাবিক খরচ
        anomaly = analysis["anomaly"]
        if anomaly:
            alerts.append(
                {
                    "key": "anomaly",
                    "severity": "warning",
                    "icon": "📈",
                    "title_bn": "আজ খরচ অস্বাভাবিক বেশি",
                    "body_bn": (
                        f"{short_date(anomaly['date'])}-এ {fmt_units(anomaly['units'])} ইউনিট "
                        f"খরচ হয়েছে — গড়ের {bn(anomaly['ratio'])} গুণ "
                        f"(গড় {fmt_units(anomaly['baseline'])} ইউনিট)। "
                        f"বাড়তি খরচ প্রায় {tk(anomaly['extra_tk'])}।"
                    ),
                }
            )

        # ৪) মাসিক বাজেট
        budget = self.settings.monthly_budget_tk
        spent = analysis["bill"].total
        if budget > 0:
            pct = spent / budget * 100.0
            if pct >= 100:
                alerts.append(
                    {
                        "key": "budget_exceeded",
                        "severity": "warning",
                        "icon": "💸",
                        "title_bn": "মাসিক বাজেট পার হয়ে গেছে",
                        "body_bn": (
                            f"এই মাসে এখন পর্যন্ত {tk(spent)} খরচ হয়েছে, "
                            f"বাজেট ছিল {tk(budget)}। মাস শেষে দাঁড়াতে পারে "
                            f"{tk(analysis['projected_bill'].total)}-এ।"
                        ),
                    }
                )
            elif pct >= self.settings.budget_warning_pct:
                alerts.append(
                    {
                        "key": "budget_warning",
                        "severity": "info",
                        "icon": "📊",
                        "title_bn": "বাজেটের {:.0f}% খরচ হয়ে গেছে".format(pct),
                        "body_bn": (
                            f"{tk(spent)} / {tk(budget)} শেষ। মাস শেষে "
                            f"{tk(analysis['projected_bill'].total)} হতে পারে।"
                        ),
                    }
                )

        # ৫) ডেটা পুরনো
        fetched = snapshot.fetched_dt
        age_minutes = (now - fetched.astimezone()).total_seconds() / 60.0
        if age_minutes > self.settings.stale_after_minutes:
            alerts.append(
                {
                    "key": "stale_data",
                    "severity": "info",
                    "icon": "🕓",
                    "title_bn": "রিডিং পুরনো",
                    "body_bn": (
                        f"সর্বশেষ সফল রিডিং {relative_ago(fetched)} — "
                        f"{bn(int(age_minutes))} মিনিট আগে। ইউটিলিটি সার্ভার সাধারণত "
                        "দিনে একবার ডেটা দেয়।"
                    ),
                }
            )

        if self._last_error:
            alerts.append(
                {
                    "key": "provider_error",
                    "severity": "warning",
                    "icon": "🔌",
                    "title_bn": "মিটার থেকে ডেটা আসছে না",
                    "body_bn": (
                        f"{self._last_error.get('message', '')} — পুরনো ডেটা দেখানো হচ্ছে। "
                        f"{self._last_error.get('hint', '')}"
                    ).strip(),
                }
            )

        # ৬) আজ রিডিং আসেনি (সাধারণত স্বাভাবিক, তাই শুধু জানানো)
        if (now.date() - reading_day).days >= 2 and not any(
            a["key"] == "stale_data" for a in alerts
        ):
            alerts.append(
                {
                    "key": "reading_gap",
                    "severity": "info",
                    "icon": "📅",
                    "title_bn": "রিডিং আপডেট হয়নি",
                    "body_bn": (
                        f"সর্বশেষ রিডিং {long_date(reading_day)} তারিখের "
                        f"({short_date(reading_day)}, {day_name(reading_day)})। "
                        "পোর্টালে এখনো নতুন ডেটা ওঠেনি।"
                    ),
                }
            )

        return alerts

    # ================= টিপস =================
    def build_tips(
        self, snapshot: Snapshot, analysis: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        analysis = analysis or self.analyze(snapshot)
        tips: list[dict[str, Any]] = []
        usage = analysis["usage"]
        forecast = analysis["forecast"]
        marginal = analysis["marginal_rate"]
        month_units = usage["month_units"]

        # ১) স্ল্যাব সীমা
        nxt = self.tariff.next_slab(month_units)
        if nxt:
            current_rate = nxt.get("current_rate", marginal)
            next_rate = nxt["next_rate"]
            if 0 < nxt["remaining_units"] <= 30:
                tips.append(
                    {
                        "icon": "🚦",
                        "title_bn": "স্ল্যাব বদলের খুব কাছে",
                        "detail_bn": (
                            f"আর {fmt_units(nxt['remaining_units'])} ইউনিট খরচ করলেই "
                            f"প্রতি ইউনিটের দাম {tk(current_rate)} থেকে "
                            f"{tk(next_rate)} হয়ে যাবে — পুরো বিলের হিসাবই বদলে যাবে। "
                            "তাই এই মাসে আরেকটু সাবধানে চললে বিল কম পড়বে।"
                        ),
                        "saving_tk": round(
                            nxt["remaining_units"] * (next_rate - current_rate), 2
                        ),
                    }
                )
            else:
                tips.append(
                    {
                        "icon": "🚦",
                        "title_bn": "পরের স্ল্যাব",
                        "detail_bn": (
                            f"আর {fmt_units(nxt['remaining_units'])} ইউনিট খরচ হলে "
                            f"রেট {tk(next_rate)}/ইউনিটে ওঠবে "
                            f"(এখন {tk(current_rate)})।"
                        ),
                        "saving_tk": None,
                    }
                )

        # ২) সাশ্রয়ের হিসাব
        if month_units > 0:
            save_units = month_units * 0.1
            tips.append(
                {
                    "icon": "💡",
                    "title_bn": "১০% কমালেই মাসে সাশ্রয়",
                    "detail_bn": (
                        f"গড় ব্যবহার ১০% কমলে এই মাসে প্রায় "
                        f"{fmt_units(save_units)} ইউনিট কমবে — আনুমানিক "
                        f"{tk(save_units * marginal)} বাঁচবে।"
                    ),
                    "saving_tk": round(save_units * marginal, 2),
                }
            )

        # ৩) পিক আওয়ার
        tips.append(
            {
                "icon": "🌙",
                "title_bn": "সন্ধ্যার পিক আওয়ার এড়ান",
                "detail_bn": (
                    "সন্ধ্যা ৬টা–১১টা লোড সবচেয়ে বেশি থাকে। ইস্ত্রি, ওয়াটার হিটার "
                    "বা চার্জিং এই সময়ের বাইরে করলে মিটারে চাপ কমে।"
                ),
                "saving_tk": None,
            }
        )

        # ৪) বেশি খরচ হলে যন্ত্রপাতি
        if usage["avg7"] >= 10:
            tips.append(
                {
                    "icon": "❄️",
                    "title_bn": "বড় লোডগুলো দেখে নিন",
                    "detail_bn": (
                        f"দৈনিক গড় {fmt_units(usage['avg7'])} ইউনিট — একটু বেশি। "
                        "এসি ২৬°C-এ রাখলে, ফ্রিজের দরজার সিল ঠিক থাকলে আর "
                        "পুরনো টিউবলাইট বদলে LED করলে পার্থক্য বোঝা যায়।"
                    ),
                    "saving_tk": None,
                }
            )
        elif usage["avg7"] > 0:
            tips.append(
                {
                    "icon": "🌱",
                    "title_bn": "খরচ নিয়ন্ত্রণে আছে",
                    "detail_bn": (
                        f"দৈনিক গড় {fmt_units(usage['avg7'])} ইউনিট — "
                        f"আনুমানিক {tk(forecast['avg_daily_cost'])}/দিন। "
                        "এই ধারা ধরে রাখলে বিল predictable থাকবে।"
                    ),
                    "saving_tk": None,
                }
            )

        # ৫) রিচার্জের অভ্যাস
        days_left = forecast.get("days_left")
        if days_left is not None and days_left <= 7:
            topup = max(
                self.settings.low_balance_tk * 2,
                round(forecast["avg_daily_cost"] * 20 / 50) * 50,
            )
            tips.append(
                {
                    "icon": "🔋",
                    "title_bn": f"এখনই {tk(topup, 0)} রিচার্জ করলে ২০ দিন নিশ্চিন্ত",
                    "detail_bn": (
                        "শেষ মুহূর্তে রিচার্ক করলে মিটার কেটে যাওয়ার ঝুঁকি থাকে। "
                        "বড় অঙ্কে কম বার রিচার্জ করলে হিসাবও পরিষ্কার থাকে।"
                    ),
                    "saving_tk": None,
                }
            )

        # ৬) অস্বাভাবিক খরচ
        anomaly = analysis["anomaly"]
        if anomaly:
            tips.append(
                {
                    "icon": "🔍",
                    "title_bn": "হঠাৎ বাড়তি খরচের কারণ",
                    "detail_bn": (
                        f"{short_date(anomaly['date'])}-এ {fmt_units(anomaly['units'])} ইউনিট — "
                        f"গড়ের {bn(anomaly['ratio'])} গুণ। ওই দিন কোনো নতুন যন্ত্র "
                        "চালু হয়েছিল? ফ্রিজ/এসি/হিটার একবার চেক করে নিন।"
                    ),
                    "saving_tk": None,
                }
            )

        # ৭) বাজেট
        budget = self.settings.monthly_budget_tk
        projected = analysis["projected_bill"].total
        if budget > 0 and projected > budget:
            tips.append(
                {
                    "icon": "🎯",
                    "title_bn": "বাজেট ছাড়িয়ে যাচ্ছে",
                    "detail_bn": (
                        f"এই ধারায় মাস শেষে বিল {tk(projected)} হতে পারে — "
                        f"বাজেটের চেয়ে {tk(projected - budget)} বেশি। "
                        "দিনে ১ ইউনিট কমালেই প্রায় "
                        f"{tk(30 * marginal)} সাশ্রয় হবে।"
                    ),
                    "saving_tk": round(projected - budget, 2),
                }
            )

        return tips

    # ================= ড্যাশবোর্ড পেলোড =================
    async def dashboard(self, force: bool = False) -> dict[str, Any]:
        snapshot = await self.snapshot(force=force)
        analysis = self.analyze(snapshot)
        tz_name = self.settings.timezone
        now = now_local(tz_name)
        month_units = analysis["usage"]["month_units"]
        forecast = analysis["forecast"]
        budget = self.settings.monthly_budget_tk
        bill = analysis["bill"]

        daily_rows = self.daily_series(snapshot, days=30)
        daily_payload = []
        for row in daily_rows:
            day = parse_date(row.date)
            cost = row.cost_tk
            if cost is None and day:
                cost = round(row.units * self.tariff.marginal_rate(month_units), 2)
            daily_payload.append(
                {
                    "date": row.date,
                    "units": round(row.units, 2),
                    "cost_tk": round(cost, 2) if cost is not None else None,
                    "weekday_bn": day_name(day) if day else "",
                }
            )

        monthly = [m.to_dict() for m in snapshot.monthly]
        if not monthly:
            buckets: dict[str, float] = {}
            for row in daily_rows:
                buckets[row.date[:7]] = buckets.get(row.date[:7], 0.0) + row.units
            monthly = [
                {"month": key, "units": round(value, 2)}
                for key, value in sorted(buckets.items())[-6:]
            ]

        fetched = snapshot.fetched_dt
        age_minutes = max(0.0, (now - fetched.astimezone()).total_seconds() / 60.0)

        return {
            "meta": {
                "app_name": "বিদ্যুৎ বন্ধু",
                "version": "1.0.0",
                "provider": snapshot.provider,
                "provider_label": self.settings.provider_label,
                "account_no": snapshot.account_no,
                "demo": self.settings.is_demo,
                "llm_enabled": self.settings.llm_enabled,
                "telegram_enabled": self.settings.telegram_enabled and bool(
                    self.settings.telegram_chat_ids
                ),
                "generated_at": now.isoformat(timespec="seconds"),
                "timezone": tz_name,
                "date_label": long_date(now.date()),
            },
            "status": {
                "ok": self._last_error is None,
                "error": self._last_error,
                "stale": age_minutes > self.settings.stale_after_minutes,
                "cached": not self._is_fresh(snapshot),
                "reading_age_minutes": round(age_minutes, 1),
                "reading_age_label": relative_ago(fetched),
            },
            "customer": {
                "name": snapshot.customer_name,
                "address": snapshot.address,
                "meter_no": snapshot.meter_no,
                "tariff_type": snapshot.tariff_type,
                "sanctioned_load_kw": snapshot.sanctioned_load_kw,
                "phase": snapshot.phase,
            },
            "reading": {
                "date": snapshot.reading_date,
                "date_label": long_date(snapshot.reading_date),
                "fetched_at": snapshot.fetched_at,
            },
            "balance": self.balance_state(snapshot),
            "usage": analysis["usage"],
            "forecast": forecast,
            "calendar": {
                "days_in_month": analysis["days_in_month"],
                "remaining_days": analysis["remaining_days"],
                "today": analysis["today"].isoformat(),
            },
            "bill": bill.to_dict(),
            "projected_bill": analysis["projected_bill"].to_dict(),
            "budget": {
                "limit_tk": budget,
                "spent_tk": round(bill.total, 2),
                "pct": round(bill.total / budget * 100, 1) if budget > 0 else None,
                "remaining_tk": round(max(0.0, budget - bill.total), 2),
            },
            "live": snapshot.live.to_dict() if snapshot.live else None,
            "daily": daily_payload,
            "monthly": monthly,
            "recharges": [r.to_dict() for r in snapshot.recharges[-8:]][::-1],
            "alerts": self.build_alerts(snapshot, analysis),
            "tips": self.build_tips(snapshot, analysis),
            "tariff": self.tariff.summary(),
            "next_slab": self.tariff.next_slab(month_units),
        }

    # ================= এজেন্ট/বটের জন্য টেক্সট =================
    async def summary_text(self) -> str:
        snapshot = await self.snapshot()
        analysis = self.analyze(snapshot)
        forecast = analysis["forecast"]
        usage = analysis["usage"]
        lines = [
            f"⚡ বিদ্যুৎ বন্ধু — {self.settings.provider_label}",
            f"ব্যালেন্স: {tk(snapshot.balance_tk)}",
            f"আজ/সর্বশেষ ({short_date(usage['last_day']['date'])}): "
            f"{fmt_units(usage['last_day']['units'])} ইউনিট",
            f"দৈনিক গড়: {fmt_units(usage['avg7'])} ইউনিট",
        ]
        if forecast["days_left"] is not None:
            lines.append(
                f"এই ব্যালেন্সে চলবে: প্রায় {fmt_units(forecast['days_left'])} দিন "
                f"({forecast['empty_on_label']})"
            )
        lines.append(
            f"মাসে এ পর্যন্ত: {fmt_units(usage['month_units'])} ইউনিট "
            f"(আনুমানিক বিল {tk(analysis['bill'].total)})"
        )
        return "\n".join(line.strip() for line in lines if line.strip())

    def bill_for(self, units: float) -> BillResult:
        snapshot = self.cache.snapshot
        phase = snapshot.phase if snapshot else self.settings.phase
        load = (snapshot.sanctioned_load_kw if snapshot else None) or self.settings.sanction_load_kw
        return self.tariff.calculate(units, phase=phase, sanctioned_load_kw=load)
