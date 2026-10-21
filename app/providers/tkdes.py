"""DESCO/DPDC প্রিপেইড পোর্টালের সাধারণ ক্লায়েন্ট।

⚠️ এই এন্ডপয়েন্টগুলো অফিসিয়াল ডকুমেন্টেড নয় — পোর্টাল/অ্যাপ যেভাবে ডেটা
নেয় সেটারই অনুকরণ। কোনো দিন পোর্টাল বদলালে এখানে পাথ ঠিক করতে হবে।
প্রতিটি রেসপন্স "যতটা পাওয়া যায়" সেইভাবে পার্স করা হয়, তাই সব ফিল্ড
না মিললেও অ্যাপ চলবে।
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Iterable

import httpx

from ..config import Settings
from ..formatting import now_local
from ..models import DailyUsage, MonthlyUsage, Recharge, Snapshot, parse_date
from .base import BaseProvider, ProviderError

log = logging.getLogger(__name__)

BALANCE_KEYS = (
    "balance",
    "availableBalance",
    "currentBalance",
    "remainingBalance",
    "balanceAmount",
    "prepaidBalance",
    "remaining_amount",
    "balance_tk",
)
NAME_KEYS = ("customerName", "accountName", "name", "userName", "customer_name")
ADDRESS_KEYS = ("address", "customerAddress", "installationAddress", "location")
METER_KEYS = ("meterNo", "meterNumber", "meterSerial", "meter_no")
LOAD_KEYS = ("sanctionedLoad", "sanctionLoad", "load", "sanctionedLoadKW", "approvedLoad")
TARIFF_KEYS = ("tariffType", "tariff", "category", "tariffCategory")
DATE_KEYS = ("readingDate", "meterReadingDate", "lastReadingDate", "lastUpdated", "updatedAt")
USED_KEYS = ("totalUsedUnits", "totalConsumption", "consumedUnit", "totalUnit", "usedUnits")
RECHARGED_KEYS = ("totalRecharged", "totalRecharge", "rechargedAmount", "totalRechargeAmount")

DAILY_LIST_KEYS = (
    "dailyConsumption",
    "consumptionList",
    "usageHistory",
    "dailyUsage",
    "daily",
    "history",
    "consumptionHistory",
)
MONTHLY_LIST_KEYS = ("monthlyConsumption", "monthlyUsage", "monthly")
RECHARGE_LIST_KEYS = (
    "rechargeHistory",
    "rechargeList",
    "transactions",
    "payments",
    "recharges",
)


def _flatten(payload: Any, depth: int = 0) -> dict[str, Any]:
    """নেস্টেড JSON থেকে সব key এক লেভেলে এনে দেয় (list/intact রাখে)।"""
    flat: dict[str, Any] = {}
    if not isinstance(payload, dict) or depth > 3:
        return flat
    for key, value in payload.items():
        if isinstance(value, dict):
            for sub_key, sub_value in _flatten(value, depth + 1).items():
                flat.setdefault(sub_key, sub_value)
        else:
            flat.setdefault(key, value)
    return flat


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("৳", "").replace("Tk", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _pick(flat: dict[str, Any], keys: Iterable[str]) -> Any:
    lowered = {str(k).lower(): v for k, v in flat.items()}
    for key in keys:
        if key in flat:
            return flat[key]
        hit = lowered.get(key.lower())
        if hit is not None:
            return hit
    return None


def _pick_list(payload: Any, keys: Iterable[str], depth: int = 0) -> list[dict[str, Any]]:
    """JSON-এর ভেতরে যেখানেই হোক, নাম মিলিয়ে লিস্ট খুঁজে বের করে।"""
    if depth > 4:
        return []
    if isinstance(payload, dict):
        lowered = {str(k).lower(): v for k, v in payload.items()}
        for key in keys:
            value = payload.get(key) or lowered.get(key.lower())
            if isinstance(value, list) and any(isinstance(v, dict) for v in value):
                return [v for v in value if isinstance(v, dict)]
        for value in payload.values():
            found = _pick_list(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        if payload and all(isinstance(v, dict) for v in payload):
            sample = {str(k).lower() for k in payload[0]}
            if {"date", "day", "readingdate"} & sample or {"units", "consumption"} & sample:
                return payload
        for value in payload:
            found = _pick_list(value, keys, depth + 1)
            if found:
                return found
    return []


class TkdesProvider(BaseProvider):
    """DESCO/DPDC-র মতো প্রিপেইড পোর্টালের বেস ক্লায়েন্ট।"""

    name = "tkdes"
    label_bn = "ইউটিলিটি"
    supports_live = False

    balance_paths: tuple[str, ...] = ()
    usage_paths: tuple[str, ...] = ()
    recharge_paths: tuple[str, ...] = ()

    def __init__(self, settings: Settings):
        self.settings = settings
        self.account_no = settings.account_no

    # ---- সাবক্লাস ঠিক করে দেবে ----
    @property
    def base_url(self) -> str:  # pragma: no cover - সাবক্লাসে
        return ""

    @property
    def referer(self) -> str:
        return self.base_url + "/"

    # ---------------- মূল কাজ ----------------
    async def fetch(self) -> Snapshot:
        if not self.account_no:
            raise ProviderError(
                "ACCOUNT_NO সেট করা নেই।",
                kind="config",
                hint=".env-এ ACCOUNT_NO দিন (DESCO Account No. / DPDC Customer No.)।",
            )

        attempts: list[str] = []
        payloads: list[Any] = []
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": self.referer,
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124 Mobile Safari/537.36"
            ),
            "Accept-Language": "bn-BD,bn;q=0.9,en;q=0.8",
        }

        timeout = httpx.Timeout(8.0, connect=6.0)
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            for path in self.balance_paths:
                data, note = await self._try_endpoint(client, path)
                attempts.append(f"{path} → {note}")
                if data is not None:
                    payloads.append(data)
                    break
            # ব্যবহারের ডেটা আলাদা এন্ডপয়েন্টে থাকতে পারে — না পেলেও চলবে
            for path in self.usage_paths:
                if path in self.balance_paths and payloads:
                    continue
                data, note = await self._try_endpoint(client, path)
                attempts.append(f"{path} → {note}")
                if data is not None:
                    payloads.append(data)
                    break

        if not payloads:
            raise ProviderError(
                f"{self.label_bn} পোর্টাল থেকে ডেটা পাওয়া যায়নি "
                f"({len(attempts)}টি রিকোয়েস্ট চেষ্টা করা হয়েছে)।",
                kind="upstream",
                hint=(
                    "পোর্টালের API অফিসিয়াল নয়, তাই মাঝে মাঝে বদলায় বা "
                    "IP ব্লক করে। নেটওয়ার্ক/অ্যাকাউন্ট নম্বর যাচাই করুন, "
                    "অথবা PROVIDER=demo দিয়ে অ্যাপ দেখে নিন।"
                ),
            )

        merged: dict[str, Any] = {}
        for payload in payloads:
            merged.update({k: v for k, v in payload.items() if k not in merged})

        snapshot = self._build_snapshot(payloads[0], merged)
        log.info(
            "%s snapshot: balance=%.2f reading=%s (চেষ্টা: %s)",
            self.name,
            snapshot.balance_tk,
            snapshot.reading_date,
            "; ".join(attempts),
        )
        return snapshot

    async def _try_endpoint(
        self, client: httpx.AsyncClient, path: str
    ) -> tuple[dict[str, Any] | None, str]:
        params = {"accountNo": self.account_no, "account_no": self.account_no}
        try:
            response = await client.get(path, params=params)
            if response.status_code == 200 and "json" in response.headers.get(
                "content-type", ""
            ):
                return _as_dict(response.text), "200 ✓"
            if response.status_code in {401, 403}:
                return None, f"{response.status_code} (অনুমতি নেই)"
            if response.status_code == 200:
                # HTML এলে POST ফর্ম দিয়ে আবার চেষ্টা
                post = await client.post(
                    path, data={"accountNo": self.account_no, "account_no": self.account_no}
                )
                if post.status_code == 200 and "json" in post.headers.get(
                    "content-type", ""
                ):
                    return _as_dict(post.text), "200 (POST) ✓"
                return None, "200 কিন্তু JSON নয়"
            return None, str(response.status_code)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return None, f"নেটওয়ার্ক সমস্যা ({type(exc).__name__})"
        except Exception as exc:  # pragma: no cover - অপ্রত্যাশিত
            return None, f"{type(exc).__name__}: {exc}"

    # ---------------- পার্সিং ----------------
    def _build_snapshot(self, primary: Any, merged: dict[str, Any]) -> Snapshot:
        flat = _flatten(primary)
        flat.update({k: v for k, v in _flatten(merged).items() if k not in flat})

        balance = _to_float(_pick(flat, BALANCE_KEYS))
        if balance is None:
            raise ProviderError(
                f"{self.label_bn} রেসপন্সে ব্যালেন্স পাওয়া যায়নি।",
                kind="parse",
                hint="পোর্টালের JSON ফরম্যাট বদলেছে। PROVIDER=demo দিয়ে অ্যাপ চালু রাখুন।",
            )

        reading_raw = _pick(flat, DATE_KEYS)
        reading = parse_date(str(reading_raw)) if reading_raw else None
        reading_date = reading or now_local(self.settings.timezone).date()

        daily = self._parse_daily(primary)
        monthly = self._parse_monthly(primary, daily)
        recharges = self._parse_recharges(primary)

        load = _to_float(_pick(flat, LOAD_KEYS))
        return Snapshot(
            provider=self.name,
            account_no=self.account_no,
            balance_tk=balance,
            reading_date=reading_date.isoformat() if hasattr(reading_date, "isoformat") else str(reading_date),
            fetched_at=now_local(self.settings.timezone).isoformat(timespec="seconds"),
            customer_name=str(_pick(flat, NAME_KEYS) or self.settings.customer_name or ""),
            address=str(_pick(flat, ADDRESS_KEYS) or ""),
            meter_no=str(_pick(flat, METER_KEYS) or ""),
            tariff_type=str(_pick(flat, TARIFF_KEYS) or "Residential"),
            sanctioned_load_kw=load if load is not None else self.settings.sanction_load_kw,
            phase=self.settings.phase,
            total_used_units=_to_float(_pick(flat, USED_KEYS)),
            total_recharged_tk=_to_float(_pick(flat, RECHARGED_KEYS)),
            daily=daily,
            monthly=monthly,
            recharges=recharges,
            live=None,  # ইউটিলিটির API লাইভ লোড দেয় না
            extra={
                "upstream_fields": len(flat),
                "unofficial_api": True,
                "note_bn": "এই ডেটা ইউটিলিটির পোর্টাল API থেকে আসছে (অফিসিয়াল ডকুমেন্টেড নয়)।",
            },
        )

    def _parse_daily(self, payload: Any) -> list[DailyUsage]:
        rows = _pick_list(payload, DAILY_LIST_KEYS)
        out: list[DailyUsage] = []
        for row in rows:
            lowered = {str(k).lower(): v for k, v in row.items()}
            raw_date = (
                row.get("date")
                or row.get("readingDate")
                or row.get("day")
                or lowered.get("date")
                or lowered.get("readingdate")
            )
            raw_units = (
                row.get("units")
                or row.get("consumption")
                or row.get("usage")
                or row.get("consumedUnit")
                or lowered.get("units")
                or lowered.get("consumption")
            )
            day = parse_date(str(raw_date)) if raw_date else None
            value = _to_float(raw_units)
            if day and value is not None:
                out.append(DailyUsage(date=day.isoformat(), units=value))
        return sorted(out, key=lambda d: d.date)

    def _parse_monthly(
        self, payload: Any, daily: list[DailyUsage]
    ) -> list[MonthlyUsage]:
        rows = _pick_list(payload, MONTHLY_LIST_KEYS)
        out: list[MonthlyUsage] = []
        for row in rows:
            lowered = {str(k).lower(): v for k, v in row.items()}
            raw_month = row.get("month") or row.get("monthName") or lowered.get("month")
            value = _to_float(
                row.get("units") or row.get("consumption") or lowered.get("consumption")
            )
            if raw_month and value is not None:
                month = str(raw_month)[:7].replace("/", "-")
                if len(month) == 7:
                    out.append(MonthlyUsage(month=month, units=value))
        if out:
            return sorted(out, key=lambda m: m.month)[-6:]

        buckets: dict[str, float] = {}
        for row in daily:
            key = row.date[:7]
            buckets[key] = buckets.get(key, 0.0) + row.units
        keys = sorted(buckets)[-6:]
        return [MonthlyUsage(month=k, units=round(buckets[k], 2)) for k in keys]

    def _parse_recharges(self, payload: Any) -> list[Recharge]:
        rows = _pick_list(payload, RECHARGE_LIST_KEYS)
        out: list[Recharge] = []
        for row in rows:
            lowered = {str(k).lower(): v for k, v in row.items()}
            amount = _to_float(
                row.get("amount")
                or row.get("rechargeAmount")
                or row.get("paidAmount")
                or lowered.get("amount")
            )
            if amount is None:
                continue
            raw_time = (
                row.get("time")
                or row.get("date")
                or row.get("rechargeDate")
                or row.get("transactionDate")
                or lowered.get("date")
            )
            stamp = self._normalize_time(raw_time)
            method = str(
                row.get("method")
                or row.get("paymentMethod")
                or row.get("channel")
                or row.get("type")
                or ""
            )
            out.append(
                Recharge(
                    time=stamp,
                    amount_tk=amount,
                    method=method,
                    reference=str(row.get("reference") or row.get("trxId") or ""),
                )
            )
        return sorted(out, key=lambda r: r.time)

    def _normalize_time(self, raw: Any) -> str:
        if not raw:
            return now_local(self.settings.timezone).isoformat(timespec="seconds")
        text = str(raw).strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(
                timespec="seconds"
            )
        except ValueError:
            pass
        for fmt in ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:16], fmt).isoformat(timespec="seconds")
            except ValueError:
                continue
        day = parse_date(text)
        return (
            datetime.combine(day, datetime.min.time()).isoformat(timespec="seconds")
            if day
            else now_local(self.settings.timezone).isoformat(timespec="seconds")
        )


def _as_dict(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"data": data}
    return None


async def gather_with_timeout(coro, seconds: float):  # pragma: no cover - helper
    return await asyncio.wait_for(coro, timeout=seconds)
