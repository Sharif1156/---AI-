#!/usr/bin/env python3
"""বিদ্যুৎ বন্ধু চালু করার স্ক্রিপ্ট।

    python run.py                # http://localhost:8000
    python run.py --port 9000    # অন্য পোর্ট
    python run.py --reload       # ডেভেলপমেন্টে অটো-রিলোড
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from app.config import load_settings


def main() -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="বিদ্যুৎ বন্ধু — প্রিপেইড মিটার এজেন্ট")
    parser.add_argument("--host", default=settings.host, help=f"বাইন্ড অ্যাড্রেস (ডিফল্ট {settings.host})")
    parser.add_argument("--port", type=int, default=settings.port, help=f"পোর্ট (ডিফল্ট {settings.port})")
    parser.add_argument("--reload", action="store_true", help="কোড বদলালে অটো রিস্টার্ট")
    parser.add_argument("--log-level", default=settings.log_level.lower())
    args = parser.parse_args()

    print("⚡ বিদ্যুৎ বন্ধু")
    print(f"   প্রোভাইডার : {settings.provider_label}")
    print(f"   অ্যাকাউন্ট : {settings.account_no}")
    print(f"   ড্যাশবোর্ড : http://localhost:{args.port}")
    if settings.is_demo:
        print("   (ডেমো ডেটা — আসল মিটারের সাথে সম্পর্ক নেই)")
    if not settings.llm_enabled:
        print("   AI: বিল্ট-ইন বাংলা এজেন্ট (LLM_API_KEY দিলে আরও স্বাভাবিক উত্তর)")
    if settings.telegram_enabled and not settings.telegram_chat_ids:
        print("   ⚠️  টেলিগ্রাম বটকে /id পাঠিয়ে TELEGRAM_CHAT_IDS সেট করুন")

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
