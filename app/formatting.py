"""সংখ্যা/টাকার বাংলা ফরম্যাটিং হেল্পার।"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
BN_MONTHS = {
    1: "জানুয়ারি",
    2: "ফেব্রুয়ারি",
    3: "মার্চ",
    4: "এপ্রিল",
    5: "মে",
    6: "জুন",
    7: "জুলাই",
    8: "আগস্ট",
    9: "সেপ্টেম্বর",
    10: "অক্টোবর",
    11: "নভেম্বর",
    12: "ডিসেম্বর",
}
BN_DAYS = {
    0: "সোমবার",
    1: "মঙ্গলবার",
    2: "বুধবার",
    3: "বৃহস্পতিবার",
    4: "শুক্রবার",
    5: "শনিবার",
    6: "রবিবার",
}


def tz(name: str = "Asia/Dhaka") -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - tzdata ছাড়া সিস্টেম
        return ZoneInfo("UTC")


def now_local(name: str = "Asia/Dhaka") -> datetime:
    return datetime.now(tz(name))


def bn(text: str | int | float) -> str:
    """ইংরেজি অঙ্ককে বাংলা অঙ্কে বদলায়।"""
    return str(text).translate(BN_DIGITS)


def num(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    if isinstance(value, int) or float(value).is_integer():
        return bn(f"{int(value):,}")
    return bn(f"{float(value):,.{digits}f}")


def tk(value: float | int | None, digits: int = 2) -> str:
    """৳১,২৪০.৫০ স্টাইলে টাকা (বাংলা অঙ্কে)।"""
    if value is None:
        return "৳—"
    return "৳" + bn(f"{float(value):,.{digits}f}")


def units(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    if float(value).is_integer():
        return bn(f"{int(value):,}")
    return bn(f"{float(value):,.{digits}f}")


def short_date(value: str | date | None) -> str:
    d = value if isinstance(value, date) else None
    if d is None and value:
        try:
            d = datetime.fromisoformat(str(value)).date()
        except ValueError:
            return str(value)
    if d is None:
        return "—"
    return f"{bn(d.day)} {BN_MONTHS[d.month]}"


def long_date(value: str | date | None) -> str:
    d = value if isinstance(value, date) else None
    if d is None and value:
        try:
            d = datetime.fromisoformat(str(value)).date()
        except ValueError:
            return str(value)
    if d is None:
        return "—"
    return f"{bn(d.day)} {BN_MONTHS[d.month]} {bn(d.year)}"


def day_name(value: str | date) -> str:
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return BN_DAYS.get(d.weekday(), "")


def relative_ago(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    current = now_local()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=current.tzinfo)
    delta = current - dt
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "এইমাত্র"
    if minutes < 60:
        return f"{bn(minutes)} মিনিট আগে"
    hours = minutes // 60
    if hours < 24:
        return f"{bn(hours)} ঘণ্টা আগে"
    days = hours // 24
    return f"{bn(days)} দিন আগে"
