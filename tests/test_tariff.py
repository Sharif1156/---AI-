"""BERC ট্যারিফ হিসাবের টেস্ট।"""

from __future__ import annotations

import unittest
from pathlib import Path

from app.tariff import Tariff

ROOT = Path(__file__).resolve().parent.parent


class TariffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tariff = Tariff(ROOT / "tariff.json")

    def test_slab_energy_charge(self) -> None:
        # ৫০×৪.৬৩ + ২৫×৫.২৬ + ২৫×৬.২০ = ২৩১.৫ + ১৩১.৫ + ১৫৫
        self.assertAlmostEqual(self.tariff.energy_charge(100), 518.0, places=2)
        self.assertAlmostEqual(self.tariff.energy_charge(0), 0.0, places=2)

    def test_bill_includes_fixed_charges_and_vat(self) -> None:
        bill = self.tariff.calculate(100, sanctioned_load_kw=2.0, phase="single")
        # এনার্জি ৫১৮ + ডিমান্ড ৮৪ + ভাড়া ৪০ − রিবেট ২৫ = ৬১৭
        self.assertAlmostEqual(bill.subtotal, 617.0, places=1)
        self.assertAlmostEqual(bill.vat, 30.85, places=2)
        self.assertAlmostEqual(bill.total, 647.85, places=2)

    def test_prorated_fixed_charges(self) -> None:
        full = self.tariff.calculate(100, sanctioned_load_kw=2.0)
        half = self.tariff.calculate(
            100, sanctioned_load_kw=2.0, prorate_days=15, days_in_month=30
        )
        self.assertAlmostEqual(half.demand_charge, 42.0, places=2)
        self.assertAlmostEqual(half.meter_rent, 20.0, places=2)
        self.assertLess(half.total, full.total)

    def test_marginal_rate_follows_slabs(self) -> None:
        self.assertAlmostEqual(self.tariff.marginal_rate(0), 4.63, places=2)
        self.assertAlmostEqual(self.tariff.marginal_rate(50), 5.26, places=2)
        self.assertAlmostEqual(self.tariff.marginal_rate(199), 8.36, places=2)
        self.assertAlmostEqual(self.tariff.marginal_rate(650), 13.05, places=2)

    def test_next_slab_points_to_the_following_rate(self) -> None:
        nxt = self.tariff.next_slab(163.4)
        self.assertIsNotNone(nxt)
        assert nxt is not None
        self.assertAlmostEqual(nxt["remaining_units"], 36.6, places=1)
        self.assertAlmostEqual(nxt["current_rate"], 8.36, places=2)
        self.assertAlmostEqual(nxt["next_rate"], 9.34, places=2)
        self.assertIsNone(self.tariff.next_slab(650))  # শেষ স্ল্যাবে কিছু নেই

    def test_slab_rows_are_contiguous(self) -> None:
        rows = self.tariff.slab_rows(650)
        self.assertEqual(round(sum(r.units for r in rows), 2), 650.0)
        self.assertEqual(rows[-1].label, "৬০০ ইউনিটের পরে")
        self.assertGreater(rows[-1].rate, rows[0].rate)

    def test_bill_grows_with_units(self) -> None:
        totals = [self.tariff.calculate(u).total for u in (50, 100, 250, 400, 700)]
        self.assertEqual(totals, sorted(totals))
        # স্ল্যাব সিস্টেমে প্রতি ইউনিটের গড় দাম বাড়ে
        averages = [t / u for t, u in zip(totals, (50, 100, 250, 400, 700))]
        self.assertEqual(averages, sorted(averages))

    def test_summary_shape(self) -> None:
        summary = self.tariff.summary()
        self.assertIn("slabs", summary)
        self.assertGreaterEqual(len(summary["slabs"]), 5)
        self.assertIn("residential", summary["categories"])


if __name__ == "__main__":
    unittest.main()
