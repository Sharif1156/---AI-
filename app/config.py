"""পরিবেশ ভেরিয়েবল থেকে কনফিগ লোড করার মডিউল।"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # pragma: no cover - dotenv ঐচ্ছিক
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent.parent

if load_dotenv is not None:
    load_dotenv(ROOT / ".env")


def _str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip()


def _float(key: str, default: float) -> float:
    raw = _str(key)
    if not raw:
        return default
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return default


def _int(key: str, default: int) -> int:
    raw = _str(key)
    if not raw:
        return default
    try:
        return int(float(raw.replace(",", "")))
    except ValueError:
        return default


def _bool(key: str, default: bool = False) -> bool:
    raw = _str(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "হ্যাঁ"}


def _chat_ids(key: str) -> list[int]:
    raw = _str(key)
    ids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            continue
    return ids


@dataclass
class Settings:
    """অ্যাপের সব সেটিংস এক জায়গায়।"""

    # মিটার / প্রোভাইডার
    provider: str = "demo"
    account_no: str = "12345678"
    sanction_load_kw: float = 2.0
    phase: str = "single"  # single | three
    customer_name: str = ""  # ডেমো/API নাম না দিলে দেখানোর জন্য

    # সতর্কতার সীমা
    low_balance_tk: float = 300.0
    critical_balance_tk: float = 150.0
    monthly_budget_tk: float = 2500.0
    anomaly_multiplier: float = 1.5
    days_left_warning: float = 3.0
    budget_warning_pct: float = 80.0

    # পোলিং / রিপোর্ট
    poll_minutes: int = 30
    report_hour: int = 9
    timezone: str = "Asia/Dhaka"
    cache_seconds: int = 300
    stale_after_minutes: int = 180

    # টেলিগ্রাম
    telegram_bot_token: str = ""
    telegram_chat_ids: list[int] = field(default_factory=list)

    # LLM (OpenAI-compatible)
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout: float = 30.0
    llm_max_rounds: int = 5

    # সার্ভার
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # পাথ
    data_dir: Path = ROOT / "data"
    tariff_path: Path = ROOT / "tariff.json"
    static_dir: Path = ROOT / "static"
    desco_base_url: str = "https://prepaid.desco.org.bd"
    dpdc_base_url: str = "https://prepaid.dpdc.org.bd"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def provider_label(self) -> str:
        return {"desco": "DESCO", "dpdc": "DPDC", "demo": "ডেমো"}.get(
            self.provider.lower(), self.provider.upper()
        )

    @property
    def is_demo(self) -> bool:
        return self.provider.lower() == "demo"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    """প্রতিবার ফাইল/পরিবেশ থেকে নতুন Settings বানায় (টেস্টে সুবিধার জন্য)।"""

    provider = (_str("PROVIDER", "demo") or "demo").lower()
    if provider not in {"desco", "dpdc", "demo"}:
        provider = "demo"

    phase = (_str("PHASE", "single") or "single").lower()
    if phase.startswith("three") or phase.startswith("3"):
        phase = "three"
    else:
        phase = "single"

    data_dir_raw = _str("DATA_DIR")
    tariff_raw = _str("TARIFF_PATH")

    return Settings(
        provider=provider,
        account_no=_str("ACCOUNT_NO", "12345678") or "12345678",
        sanction_load_kw=_float("SANCTION_LOAD_KW", 2.0),
        phase=phase,
        customer_name=_str("CUSTOMER_NAME"),
        low_balance_tk=_float("LOW_BALANCE_TK", 300.0),
        critical_balance_tk=_float("CRITICAL_BALANCE_TK", 150.0),
        monthly_budget_tk=_float("MONTHLY_BUDGET_TK", 2500.0),
        anomaly_multiplier=_float("ANOMALY_MULTIPLIER", 1.5),
        days_left_warning=_float("DAYS_LEFT_WARNING", 3.0),
        budget_warning_pct=_float("BUDGET_WARNING_PCT", 80.0),
        poll_minutes=max(1, _int("POLL_MINUTES", 30)),
        report_hour=min(23, max(0, _int("REPORT_HOUR", 9))),
        timezone=_str("TIMEZONE", "Asia/Dhaka") or "Asia/Dhaka",
        cache_seconds=max(0, _int("CACHE_SECONDS", 300)),
        stale_after_minutes=max(10, _int("STALE_AFTER_MINUTES", 180)),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_ids=_chat_ids("TELEGRAM_CHAT_IDS"),
        llm_api_key=_str("LLM_API_KEY") or _str("OPENAI_API_KEY"),
        llm_base_url=_str("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_model=_str("LLM_MODEL", "gpt-4o-mini"),
        llm_timeout=_float("LLM_TIMEOUT", 30.0),
        llm_max_rounds=max(1, _int("LLM_MAX_ROUNDS", 5)),
        host=_str("HOST", "0.0.0.0") or "0.0.0.0",
        port=_int("PORT", 8000),
        log_level=(_str("LOG_LEVEL", "INFO") or "INFO").upper(),
        data_dir=Path(data_dir_raw) if data_dir_raw else ROOT / "data",
        tariff_path=Path(tariff_raw) if tariff_raw else ROOT / "tariff.json",
        static_dir=ROOT / "static",
        desco_base_url=_str("DESCO_BASE_URL", "https://prepaid.desco.org.bd"),
        dpdc_base_url=_str("DPDC_BASE_URL", "https://prepaid.dpdc.org.bd"),
    )


settings = load_settings()
