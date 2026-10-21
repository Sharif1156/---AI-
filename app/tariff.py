"""BERC ট্যারিফ অনুযায়ী প্রিপেইড বিল ক্যালকুলেটর।

রেট থাকে ``tariff.json``-এ — নতুন আদেশ এলে শুধু ওই ফাইল বদলালেই হবে।
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class TariffError(RuntimeError):
    pass


@dataclass
class SlabRow:
    label: str
    units: float
    rate: float
    amount: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "units": round(self.units, 2),
            "rate": round(self.rate, 3),
            "amount": round(self.amount, 2),
        }


@dataclass
class BillResult:
    units: float
    energy_charge: float
    slab_details: list[SlabRow] = field(default_factory=list)
    demand_charge: float = 0.0
    meter_rent: float = 0.0
    rebate: float = 0.0
    subtotal: float = 0.0
    vat_percent: float = 0.0
    vat: float = 0.0
    total: float = 0.0
    marginal_rate: float = 0.0
    effective_rate: float = 0.0
    category: str = "residential"
    prorated_days: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "units": round(self.units, 2),
            "energy_charge": round(self.energy_charge, 2),
            "slabs": [row.to_dict() for row in self.slab_details],
            "demand_charge": round(self.demand_charge, 2),
            "meter_rent": round(self.meter_rent, 2),
            "rebate": round(self.rebate, 2),
            "subtotal": round(self.subtotal, 2),
            "vat_percent": round(self.vat_percent, 2),
            "vat": round(self.vat, 2),
            "total": round(self.total, 2),
            "marginal_rate": round(self.marginal_rate, 3),
            "effective_rate": round(self.effective_rate, 3),
            "category": self.category,
            "prorated_days": self.prorated_days,
        }


def _round(value: float, digits: int = 2) -> float:
    return float(f"{value:.{digits}f}")


class Tariff:
    """``tariff.json`` পড়ে স্ল্যাব-ভিত্তিক বিল বের করে।"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise TariffError(f"ট্যারিফ ফাইল পাওয়া যায়নি: {self.path}")
        with self.path.open("r", encoding="utf-8") as fh:
            self.data = json.load(fh)
        if "categories" not in self.data:
            raise TariffError("tariff.json-এ 'categories' নেই")
        self.vat_percent = float(self.data.get("vat_percent", 5.0))

    # ---------------- মূল তথ্য ----------------
    def category(self, name: str = "residential") -> dict[str, Any]:
        cats = self.data["categories"]
        if name in cats:
            return cats[name]
        return cats.get("residential", next(iter(cats.values())))

    def slabs(self, category: str = "residential") -> list[dict[str, Any]]:
        return list(self.category(category).get("slabs", []))

    def meta(self) -> dict[str, Any]:
        return self.data.get("meta", {})

    def categories(self) -> dict[str, Any]:
        return {
            key: {"label_bn": val.get("label_bn", key)}
            for key, val in self.data["categories"].items()
        }

    # ---------------- হিসাব ----------------
    def slab_rows(self, units: float, category: str = "residential") -> list[SlabRow]:
        units = max(0.0, float(units))
        rows: list[SlabRow] = []
        previous = 0.0
        for slab in self.slabs(category):
            cap = slab.get("up_to")
            upper = float("inf") if cap in (None, "", 0) else float(cap)
            if units <= previous:
                break
            qty = min(units, upper) - previous
            if qty <= 0:
                previous = upper
                continue
            rows.append(
                SlabRow(
                    label=self._label(slab, previous, upper),
                    units=qty,
                    rate=float(slab.get("rate", 0.0)),
                    amount=qty * float(slab.get("rate", 0.0)),
                )
            )
            previous = upper
        return rows

    @staticmethod
    def _label(slab: dict[str, Any], lower: float, upper: float) -> str:
        if slab.get("label_bn"):
            return str(slab["label_bn"])
        if upper == float("inf"):
            return f"{int(lower)} ইউনিটের পরে"
        return f"{int(lower + 1)}–{int(upper)} ইউনিট"

    def energy_charge(self, units: float, category: str = "residential") -> float:
        return round(sum(row.amount for row in self.slab_rows(units, category)), 2)

    def marginal_rate(self, units: float, category: str = "residential") -> float:
        """পরের ইউনিটের দাম (অ্যানোমালি/সেভিংস বোঝাতে দরকার)।"""
        units = max(0.0, float(units))
        for slab in self.slabs(category):
            cap = slab.get("up_to")
            upper = float("inf") if cap in (None, "", 0) else float(cap)
            if units < upper:
                return float(slab.get("rate", 0.0))
        slabs = self.slabs(category)
        return float(slabs[-1].get("rate", 0.0)) if slabs else 0.0

    def effective_rate(self, units: float, category: str = "residential") -> float:
        if units <= 0:
            return self.marginal_rate(0, category)
        return round(self.energy_charge(units, category) / units, 3)

    def next_slab(self, units: float, category: str = "residential") -> dict[str, Any] | None:
        """বর্তমান স্ল্যাব কখন শেষ, তারপরের রেট কত — সতর্ক করার জন্য।"""
        slabs = self.slabs(category)
        for index, slab in enumerate(slabs):
            cap = slab.get("up_to")
            if cap in (None, "", 0):
                return None
            upper = float(cap)
            if units < upper:
                upcoming = slabs[index + 1] if index + 1 < len(slabs) else None
                if upcoming is None or upcoming.get("up_to") in (None, "", 0):
                    return None
                return {
                    "remaining_units": round(upper - units, 2),
                    "boundary": upper,
                    "current_rate": float(slab.get("rate", 0.0)),
                    "next_rate": float(upcoming.get("rate", 0.0)),
                    "next_label": str(upcoming.get("label_bn") or ""),
                }
        return None

    def calculate(
        self,
        units: float,
        *,
        category: str = "residential",
        phase: str = "single",
        sanctioned_load_kw: float = 0.0,
        prorate_days: int | None = None,
        days_in_month: int = 30,
        include_fixed: bool = True,
    ) -> BillResult:
        """এক মাসের বিল। ``prorate_days`` দিলে ডিমান্ড চার্জ/মিটার ভাড়া
        ওই কত দিনের অনুপাতে ধরা হয় (মাসের মাঝপথে "এখন পর্যন্ত বিল" দেখাতে)।"""

        cat = self.category(category)
        units = max(0.0, float(units))
        rows = self.slab_rows(units, category)
        energy = round(sum(r.amount for r in rows), 2)

        demand = 0.0
        rent = 0.0
        rebate = 0.0
        if include_fixed:
            factor = 1.0
            if prorate_days is not None:
                factor = max(0.0, min(1.0, prorate_days / max(1, days_in_month)))
            demand = float(cat.get("demand_charge_per_kw", 0.0)) * max(
                0.0, float(sanctioned_load_kw)
            )
            rent = float((cat.get("meter_rent") or {}).get(phase, 0.0))
            demand = round(demand * factor, 2)
            rent = round(rent * factor, 2)
            rebate = round(units * float(cat.get("prepaid_rebate_per_unit", 0.0)), 2)

        subtotal = round(energy + demand + rent - rebate, 2)
        minimum = float(cat.get("minimum_bill_tk") or 0.0)
        if minimum and subtotal < minimum:
            subtotal = minimum
        vat = round(subtotal * self.vat_percent / 100.0, 2)
        total = round(subtotal + vat, 2)

        return BillResult(
            units=units,
            energy_charge=energy,
            slab_details=rows,
            demand_charge=demand,
            meter_rent=rent,
            rebate=rebate,
            subtotal=subtotal,
            vat_percent=self.vat_percent,
            vat=vat,
            total=total,
            marginal_rate=self.marginal_rate(units, category),
            effective_rate=round(total / units, 3) if units > 0 else 0.0,
            category=category,
            prorated_days=prorate_days,
        )

    def summary(self, category: str = "residential") -> dict[str, Any]:
        cat = self.category(category)
        return {
            "category": category,
            "label_bn": cat.get("label_bn", category),
            "meta": self.meta(),
            "vat_percent": self.vat_percent,
            "demand_charge_per_kw": cat.get("demand_charge_per_kw", 0.0),
            "meter_rent": cat.get("meter_rent", {}),
            "prepaid_rebate_per_unit": cat.get("prepaid_rebate_per_unit", 0.0),
            "slabs": [
                {
                    "label": self._label(
                        s,
                        0 if i == 0 else float(self._prev_cap(category, i)),
                        float("inf") if s.get("up_to") in (None, "", 0) else float(s["up_to"]),
                    ),
                    "up_to": s.get("up_to"),
                    "rate": s.get("rate"),
                }
                for i, s in enumerate(self.slabs(category))
            ],
            "categories": self.categories(),
        }

    def _prev_cap(self, category: str, index: int) -> float:
        if index == 0:
            return 0.0
        slabs = self.slabs(category)
        if index - 1 < len(slabs):
            cap = slabs[index - 1].get("up_to")
            return 0.0 if cap in (None, "", 0) else float(cap)
        return 0.0
