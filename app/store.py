"""ছোট JSON-ভিত্তিক পারসিস্টেন্স — কোনো ডেটাবেস লাগে না।

``data/`` ফোল্ডারে থাকে:
  snapshot.json  — শেষ সফল মিটার স্ন্যাপশট (রিস্টার্টেও ড্যাশবোর্ড খালি হয় না)
  history.json   — দিনভিত্তিক ইউনিটের রোলিং হিস্ট্রি
  state.json     — কোন অ্যালার্ট কখন পাঠানো হয়েছে, শেষ রিপোর্টের তারিখ
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import DailyUsage, Snapshot, parse_date

log = logging.getLogger(__name__)

HISTORY_KEEP_DAYS = 400


class JsonFile:
    """অ্যাটমিক রাইট সহ ছোট JSON ফাইল।"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self, default: Any) -> Any:
        with self._lock:
            if not self.path.exists():
                return default
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("%s পড়া যায়নি (%s) — ডিফল্ট ব্যবহার হচ্ছে", self.path, exc)
                return default

    def save(self, data: Any) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                with tmp.open("w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except OSError as exc:  # pragma: no cover - ডিস্ক সমস্যা
                log.error("%s লেখা যায়নি: %s", self.path, exc)
                tmp.unlink(missing_ok=True)


class HistoryStore:
    """দিনভিত্তিক ব্যবহার জমানো — API দিনে একবার আপডেট হলেও গ্রাফ ভরে যায়।"""

    def __init__(self, path: Path):
        self.file = JsonFile(path)
        self._data: dict[str, Any] = self.file.load({"version": 1, "days": {}, "cumulative": None})
        self._data.setdefault("days", {})
        self._data.setdefault("cumulative", None)

    # ---------------- পড়া ----------------
    @property
    def days(self) -> dict[str, dict[str, Any]]:
        return self._data["days"]

    def units_by_day(self) -> dict[str, float]:
        return {
            key: float(value.get("units") or 0.0)
            for key, value in self.days.items()
            if parse_date(key)
        }

    def series(self, days: int = 30, end: date | None = None) -> list[DailyUsage]:
        end = end or date.today()
        start = end - timedelta(days=days - 1)
        rows = [
            DailyUsage(date=key, units=float(val.get("units") or 0.0))
            for key, val in self.days.items()
            if (d := parse_date(key)) and start <= d <= end
        ]
        return sorted(rows, key=lambda r: r.date)

    # ---------------- লেখা ----------------
    def upsert(self, day: date, units: float, *, source: str = "reading") -> None:
        if units is None or units < 0:
            return
        key = day.isoformat()
        self.days[key] = {
            "units": round(float(units), 3),
            "source": source,
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def sync_snapshot(self, snapshot: Snapshot) -> None:
        """স্ন্যাপশটে দিনভিত্তিক সিরিজ থাকলে সেটাই সবচেয়ে নির্ভরযোগ্য।"""
        rows = snapshot.daily_sorted()
        if rows:
            for row in rows:
                day = parse_date(row.date)
                if day:
                    self.upsert(day, row.units, source=snapshot.provider)
        elif snapshot.total_used_units is not None:
            self.record_cumulative(
                parse_date(snapshot.reading_date) or date.today(),
                float(snapshot.total_used_units),
                source=snapshot.provider,
            )
        self.prune()
        self.flush()

    def record_cumulative(self, reading_day: date, cumulative: float, *, source: str) -> None:
        """মিটার মোট রিডিং থেকে দৈনিক ব্যবহার বের করে (দিনে একবার আপডেট হলে দরকার)।"""
        state = self._data.get("cumulative")
        self._data["cumulative"] = {
            "units": float(cumulative),
            "date": reading_day.isoformat(),
            "source": source,
        }
        if not state:
            return
        try:
            prev_units = float(state["units"])
            prev_day = parse_date(state.get("date"))
        except (KeyError, TypeError, ValueError):
            return
        if prev_day is None or reading_day <= prev_day or cumulative < prev_units:
            return
        delta = cumulative - prev_units
        gap = max(1, (reading_day - prev_day).days)
        per_day = delta / gap
        # মিটার রিডিং (আগের দিন, আজকের দিন] পরিসরের ব্যবহার দেখায়
        for offset in range(1, gap + 1):
            day = prev_day + timedelta(days=offset)
            if day > reading_day:
                break
            self.upsert(day, per_day, source="derived")

    def prune(self) -> None:
        cutoff = date.today() - timedelta(days=HISTORY_KEEP_DAYS)
        for key in list(self.days):
            day = parse_date(key)
            if day is None or day < cutoff:
                self.days.pop(key, None)

    def flush(self) -> None:
        self.file.save(self._data)


class StateStore:
    """অ্যালার্ট ডিডুপ্লিকেশন আর দৈনিক রিপোর্টের অবস্থা।"""

    def __init__(self, path: Path):
        self.file = JsonFile(path)
        self._data: dict[str, Any] = self.file.load(
            {"version": 1, "alerts": {}, "last_report": None, "last_recharge_id": None}
        )
        self._data.setdefault("alerts", {})
        self._data.setdefault("last_report", None)
        self._data.setdefault("last_recharge_id", None)

    # ---- অ্যালার্ট ----
    def alert_sent_on(self, key: str) -> str | None:
        entry = self._data["alerts"].get(key)
        return entry.get("date") if isinstance(entry, dict) else None

    def mark_alert(self, key: str, *, on: date | None = None) -> None:
        day = (on or date.today()).isoformat()
        self._data["alerts"][key] = {
            "date": day,
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self.flush()

    def clear_alert(self, key: str) -> None:
        if key in self._data["alerts"]:
            self._data["alerts"].pop(key, None)
            self.flush()

    # ---- রিপোর্ট ----
    @property
    def last_report(self) -> str | None:
        return self._data.get("last_report")

    def mark_report(self, day: date | None = None) -> None:
        self._data["last_report"] = (day or date.today()).isoformat()
        self.flush()

    # ---- রিচার্জ ----
    @property
    def last_recharge_id(self) -> str | None:
        return self._data.get("last_recharge_id")

    def set_last_recharge_id(self, value: str | None) -> None:
        if value != self._data.get("last_recharge_id"):
            self._data["last_recharge_id"] = value
            self.flush()

    def flush(self) -> None:
        self.file.save(self._data)

    def generic_get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def generic_set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.flush()


class SnapshotCache:
    """শেষ সফল স্ন্যাপশট ডিস্কে রাখে, যাতে রিস্টার্টে ডেটা হারিয়ে না যায়।"""

    def __init__(self, path: Path):
        self.file = JsonFile(path)
        self._snapshot: Snapshot | None = None
        raw = self.file.load(None)
        if isinstance(raw, dict) and raw.get("balance_tk") is not None:
            try:
                self._snapshot = Snapshot.from_dict(raw)
            except Exception as exc:  # pragma: no cover
                log.warning("ক্যাশ করা স্ন্যাপশট পড়া যায়নি: %s", exc)

    @property
    def snapshot(self) -> Snapshot | None:
        return self._snapshot

    def set(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot
        self.file.save(snapshot.to_dict(include_daily=True))
