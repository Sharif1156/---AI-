"""টেস্টের জন্য ডেমো সার্ভিস বানানোর হেল্পার।"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.config import load_settings
from app.providers.demo import DemoProvider
from app.service import MeterService
from app.store import HistoryStore, SnapshotCache, StateStore
from app.tariff import Tariff

ROOT = Path(__file__).resolve().parent.parent


def demo_service(data_dir: Path) -> MeterService:
    settings = replace(
        load_settings(),
        provider="demo",
        account_no="12345678",
        data_dir=data_dir,
        cache_seconds=0,
        monthly_budget_tk=2500.0,
        low_balance_tk=300.0,
        critical_balance_tk=150.0,
    )
    tariff = Tariff(ROOT / "tariff.json")
    return MeterService(
        settings,
        tariff,
        DemoProvider(settings, tariff),
        SnapshotCache(data_dir / "snapshot.json"),
        HistoryStore(data_dir / "history.json"),
        StateStore(data_dir / "state.json"),
    )
