#!/usr/bin/env python3
"""মিটার কানেকশন যাচাই করার স্ক্রিপ্ট।

    python check.py                # .env-এর সেটিংস দিয়ে টেস্ট
    python check.py --demo         # ডেমো প্রোভাইডার দিয়ে
    python check.py --provider dpdc
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace

from app.config import load_settings
from app.formatting import bn, long_date, now_local, tk, units as fmt_units
from app.providers import build_provider
from app.providers.base import ProviderError
from app.tariff import Tariff

OK = "✅"
BAD = "❌"
WARN = "⚠️ "


def line(char: str = "─", width: int = 58) -> str:
    return char * width


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.provider:
        settings = replace(settings, provider=args.provider.lower())
    if args.demo:
        settings = replace(settings, provider="demo")

    print(line("═"))
    print("⚡ বিদ্যুৎ বন্ধু — কানেকশন চেক")
    print(line("═"))
    print(f"প্রোভাইডার      : {settings.provider_label} ({settings.provider})")
    print(f"অ্যাকাউন্ট      : {settings.account_no}")
    print(f"স্যাংকশন লোড   : {bn(settings.sanction_load_kw)} kW ({settings.phase})")
    print(f"কম ব্যালেন্স    : {tk(settings.low_balance_tk)}")
    print(f"মাসিক বাজেট     : {tk(settings.monthly_budget_tk)}")
    print(f"LLM             : {'চালু — ' + settings.llm_model if settings.llm_enabled else 'বন্ধ (অফলাইন এজেন্ট)'}")
    print(f"টেলিগ্রাম        : {'চালু' if settings.telegram_enabled else 'বন্ধ'}"
          + (f" ({len(settings.telegram_chat_ids)} chat)" if settings.telegram_chat_ids else ""))
    print(line())

    # ---- ট্যারিফ ----
    try:
        tariff = Tariff(settings.tariff_path)
        print(f"{OK} ট্যারিফ লোড হয়েছে — {tariff.meta().get('effective_from', 'তারিখ নেই')} থেকে কার্যকর")
        for units in (100, 250, 400):
            result = tariff.calculate(units, sanctioned_load_kw=settings.sanction_load_kw)
            print(
                f"   {fmt_units(units):>4} ইউনিট → {tk(result.total):>12} "
                f"(প্রতি ইউনিট {tk(result.total / units)}/ইউনিট)"
            )
    except Exception as exc:
        print(f"{BAD} ট্যারিফ লোড ব্যর্থ: {exc}")
        return 1

    print(line())

    # ---- প্রোভাইডার ----
    provider = build_provider(settings, tariff)
    started = now_local(settings.timezone)
    try:
        snapshot = await provider.fetch()
    except ProviderError as exc:
        print(f"{BAD} মিটার থেকে ডেটা পাওয়া যায়নি")
        print(f"   কারণ: {exc}")
        if exc.hint:
            print(f"   পরামর্শ: {exc.hint}")
        print(line())
        print(f"{WARN}আসল মিটার ছাড়া অ্যাপ দেখতে: python check.py --demo")
        return 1
    except Exception as exc:  # pragma: no cover
        print(f"{BAD} অপ্রত্যাশিত সমস্যা: {type(exc).__name__}: {exc}")
        return 1

    elapsed = (now_local(settings.timezone) - started).total_seconds()
    print(f"{OK} ডেটা এসেছে ({bn(round(elapsed, 1))} সেকেন্ডে)")
    print(f"   গ্রাহক        : {snapshot.customer_name or '—'}")
    print(f"   মিটার নম্বর   : {snapshot.meter_no or '—'}")
    print(f"   ঠিকানা        : {snapshot.address or '—'}")
    print(f"   ব্যালেন্স      : {tk(snapshot.balance_tk)}")
    print(f"   সর্বশেষ রিডিং : {long_date(snapshot.reading_date)}")
    print(f"   দিনভিত্তিক রেকর্ড: {len(snapshot.daily)} দিন")
    if snapshot.monthly:
        recent = ", ".join(f"{m.month}: {fmt_units(m.units)}" for m in snapshot.monthly[-3:])
        print(f"   মাসিক ব্যবহার   : {recent}")
    if snapshot.recharges:
        last = snapshot.recharges[-1]
        print(f"   শেষ রিচার্জ     : {tk(last.amount_tk)} ({last.method or '—'}) {last.time[:10]}")
    if snapshot.live:
        print(
            f"   লাইভ লোড       : {bn(round(snapshot.live.load_kw, 2))} kW "
            f"({snapshot.live.source})"
        )

    print(line())

    # ---- যাচাই ----
    checks: list[tuple[bool, str]] = [
        (snapshot.balance_tk >= 0, "ব্যালেন্স একটি বৈধ সংখ্যা"),
        (bool(snapshot.reading_date), "রিডিংয়ের তারিখ পাওয়া গেছে"),
        (bool(snapshot.daily), "দিনভিত্তিক ব্যবহার পাওয়া গেছে"),
        (snapshot.fetched_at is not None, "কখন আনা হয়েছে তা 기록 হয়েছে"),
    ]
    for ok, label in checks:
        print(f"{OK if ok else WARN}{label}")

    if snapshot.daily:
        print(f"{OK} প্রতিদিনের গড়: {fmt_units(sum(d.units for d in snapshot.daily[-7:]) / min(7, len(snapshot.daily)))} ইউনিট")
    else:
        print(f"{WARN}দিনভিত্তিক সিরিজ নেই — অ্যাপ হিস্ট্রি জমিয়ে নিজেই গ্রাফ বানাবে")

    if settings.is_demo:
        print(f"{WARN}এটি ডেমো ডেটা, আসল মিটার নয়")

    print(line("═"))
    print(f"সব প্রস্তুত! এখন চালান:  python run.py  →  http://localhost:{settings.port}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="মিটার কানেকশন টেস্ট")
    parser.add_argument("--demo", action="store_true", help="ডেমো ডেটা দিয়ে টেস্ট")
    parser.add_argument("--provider", choices=["desco", "dpdc", "demo"], help="প্রোভাইডার বদলান")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nবাতিল করা হয়েছে।")
        return 130


if __name__ == "__main__":
    sys.exit(main())
