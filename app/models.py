"""ডেটা মডেল — প্রোভাইডার, সার্ভিস আর API সবাই এগুলো ব্যবহার করে।"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    # 2026-10-20 / 2026/10/20 / 20-10-2026
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt != "%Y%m%d" else text[:8], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


@dataclass
class DailyUsage:
    date: str  # YYYY-MM-DD
    units: float
    cost_tk: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"date": self.date, "units": round(self.units, 3)}
        if self.cost_tk is not None:
            data["cost_tk"] = round(self.cost_tk, 2)
        return data


@dataclass
class Recharge:
    time: str  # ISO datetime (যতটা পাওয়া যায়)
    amount_tk: float
    method: str = ""
    units: float | None = None
    reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "time": self.time,
            "amount_tk": round(self.amount_tk, 2),
            "method": self.method or "—",
        }
        if self.units is not None:
            data["units"] = round(self.units, 2)
        if self.reference:
            data["reference"] = self.reference
        return data

    @property
    def dt(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.time.replace("Z", "+00:00"))
        except ValueError:
            d = parse_date(self.time)
            return datetime.combine(d, datetime.min.time()) if d else None


@dataclass
class MonthlyUsage:
    month: str  # YYYY-MM
    units: float

    def to_dict(self) -> dict[str, Any]:
        return {"month": self.month, "units": round(self.units, 3)}


@dataclass
class LiveLoad:
    """প্রিপেইড মিটার API সাধারণত লাইভ লোড দেয় না — তাই ঐচ্ছিক।"""

    load_kw: float
    voltage: float | None = None
    current: float | None = None
    source: str = "simulated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "load_kw": round(self.load_kw, 3),
            "voltage": round(self.voltage, 1) if self.voltage is not None else None,
            "current": round(self.current, 2) if self.current is not None else None,
            "source": self.source,
        }


@dataclass
class Snapshot:
    """একটা নির্দিষ্ট সময়ে মিটার থেকে পাওয়া কাঁচা ছবি।"""

    provider: str
    account_no: str
    balance_tk: float
    reading_date: str
    fetched_at: str = field(default_factory=iso_now)
    customer_name: str = ""
    address: str = ""
    meter_no: str = ""
    tariff_type: str = "Residential"
    sanctioned_load_kw: float | None = None
    phase: str = "single"
    total_used_units: float | None = None
    total_recharged_tk: float | None = None
    daily: list[DailyUsage] = field(default_factory=list)
    monthly: list[MonthlyUsage] = field(default_factory=list)
    recharges: list[Recharge] = field(default_factory=list)
    live: LiveLoad | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # ---- ব্যবহারিক হেল্পার ----
    def daily_sorted(self) -> list[DailyUsage]:
        rows = [d for d in self.daily if parse_date(d.date)]
        rows.sort(key=lambda d: parse_date(d.date) or date.min)
        # একই তারিখ একবার
        dedup: dict[str, DailyUsage] = {}
        for row in rows:
            dedup[row.date] = row
        return sorted(dedup.values(), key=lambda d: parse_date(d.date) or date.min)

    def units_between(self, start: date, end: date) -> float:
        total = 0.0
        for row in self.daily:
            d = parse_date(row.date)
            if d and start <= d <= end:
                total += row.units
        return total

    def newest_recharge(self) -> Recharge | None:
        rows = [r for r in self.recharges if r.dt]
        if not rows:
            return None
        rows.sort(key=lambda r: r.dt or datetime.min)
        return rows[-1]

    def to_dict(self, include_daily: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "provider": self.provider,
            "account_no": self.account_no,
            "balance_tk": round(self.balance_tk, 2),
            "reading_date": self.reading_date,
            "fetched_at": self.fetched_at,
            "customer_name": self.customer_name,
            "address": self.address,
            "meter_no": self.meter_no,
            "tariff_type": self.tariff_type,
            "sanctioned_load_kw": self.sanctioned_load_kw,
            "phase": self.phase,
            "total_used_units": self.total_used_units,
            "total_recharged_tk": self.total_recharged_tk,
            "monthly": [m.to_dict() for m in self.monthly],
            "recharges": [r.to_dict() for r in self.recharges],
            "live": self.live.to_dict() if self.live else None,
        }
        if include_daily:
            data["daily"] = [d.to_dict() for d in self.daily_sorted()]
        if self.extra:
            data["extra"] = self.extra
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        return cls(
            provider=data.get("provider", "unknown"),
            account_no=str(data.get("account_no", "")),
            balance_tk=float(data.get("balance_tk") or 0.0),
            reading_date=str(data.get("reading_date") or date.today().isoformat()),
            fetched_at=str(data.get("fetched_at") or iso_now()),
            customer_name=data.get("customer_name", "") or "",
            address=data.get("address", "") or "",
            meter_no=data.get("meter_no", "") or "",
            tariff_type=data.get("tariff_type", "Residential") or "Residential",
            sanctioned_load_kw=data.get("sanctioned_load_kw"),
            phase=data.get("phase", "single") or "single",
            total_used_units=data.get("total_used_units"),
            total_recharged_tk=data.get("total_recharged_tk"),
            daily=[
                DailyUsage(
                    date=str(d.get("date")),
                    units=float(d.get("units") or 0.0),
                    cost_tk=d.get("cost_tk"),
                )
                for d in data.get("daily", [])
            ],
            monthly=[
                MonthlyUsage(month=str(m.get("month")), units=float(m.get("units") or 0.0))
                for m in data.get("monthly", [])
            ],
            recharges=[
                Recharge(
                    time=str(r.get("time")),
                    amount_tk=float(r.get("amount_tk") or 0.0),
                    method=str(r.get("method") or ""),
                    units=r.get("units"),
                    reference=str(r.get("reference") or ""),
                )
                for r in data.get("recharges", [])
            ],
            live=(
                LiveLoad(
                    load_kw=float(data["live"].get("load_kw") or 0.0),
                    voltage=data["live"].get("voltage"),
                    current=data["live"].get("current"),
                    source=str(data["live"].get("source") or "unknown"),
                )
                if data.get("live")
                else None
            ),
            extra=data.get("extra", {}) or {},
        )

    @property
    def fetched_dt(self) -> datetime:
        try:
            return datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now().astimezone()


def daterange(start: date, end: date) -> list[date]:
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(max(0, days) + 1)]


def as_jsonable(obj: Any) -> Any:
    """dataclass/dict/list নির্বিশেষে JSON-এ দেওয়া যায় এমন কিছু ফেরত দেয়।"""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: as_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [as_jsonable(v) for v in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return as_jsonable(asdict(obj))
    return obj
