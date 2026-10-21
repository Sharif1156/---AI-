"""FastAPI সার্ভার — ড্যাশবোর্ড, REST API আর চ্যাট।"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .agent import Agent
from .config import Settings, load_settings
from .formatting import long_date, now_local
from .monitor import Monitor
from .providers import build_provider
from .providers.base import ProviderError
from .service import MeterService
from .store import HistoryStore, SnapshotCache, StateStore
from .tariff import Tariff
from .telegram import TelegramBot

log = logging.getLogger(__name__)


@dataclass
class Context:
    settings: Settings
    tariff: Tariff
    service: MeterService
    agent: Agent
    monitor: Monitor
    bot: TelegramBot | None
    provider: object


def build_context(settings: Settings | None = None) -> Context:
    settings = settings or load_settings()
    settings.ensure_dirs()

    tariff = Tariff(settings.tariff_path)
    provider = build_provider(settings, tariff)
    cache = SnapshotCache(settings.data_dir / "snapshot.json")
    history = HistoryStore(settings.data_dir / "history.json")
    state = StateStore(settings.data_dir / "state.json")
    service = MeterService(settings, tariff, provider, cache, history, state)
    agent = Agent(service, settings)
    bot = TelegramBot(settings, agent, service) if settings.telegram_enabled else None
    monitor = Monitor(service, settings, notifier=bot)
    return Context(
        settings=settings,
        tariff=tariff,
        service=service,
        agent=agent,
        monitor=monitor,
        bot=bot,
        provider=provider,
    )


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    history: list[dict] | None = None


def create_app(context: Context | None = None) -> FastAPI:
    ctx = context or build_context()
    settings = ctx.settings

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info(
            "⚡ বিদ্যুৎ বন্ধু চালু — প্রোভাইডার: %s, LLM: %s, টেলিগ্রাম: %s",
            settings.provider_label,
            "চালু" if settings.llm_enabled else "বন্ধ (অফলাইন এজেন্ট)",
            "চালু" if settings.telegram_enabled else "বন্ধ",
        )
        if settings.telegram_enabled and not settings.telegram_chat_ids:
            log.warning(
                "TELEGRAM_CHAT_IDS খালি — বটকে /id পাঠিয়ে chat id নিন, নাহলে অ্যালার্ট যাবে না।"
            )
        ctx.monitor.start()
        if ctx.bot:
            ctx.bot.start()
        # শুরুতেই একবার ডেটা আনার চেষ্টা (ব্যর্থ হলেও সার্ভার চলবে)
        try:
            await ctx.service.snapshot()
        except Exception as exc:
            log.warning("প্রাথমিক ডেটা লোড ব্যর্থ: %s", exc)
        yield
        await ctx.monitor.stop()
        if ctx.bot:
            await ctx.bot.stop()

    app = FastAPI(
        title="বিদ্যুৎ বন্ধু",
        description="DESCO/DPDC প্রিপেইড মিটারের বাংলা AI এজেন্ট",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.ctx = ctx

    @app.exception_handler(ProviderError)
    async def provider_error_handler(request: Request, exc: ProviderError):
        return JSONResponse(
            status_code=503,
            content={
                "error": str(exc),
                "kind": exc.kind,
                "hint": exc.hint,
                "provider": settings.provider_label,
            },
        )

    # ================= পেজ =================
    @app.get("/", include_in_schema=False)
    async def index():
        page = settings.static_dir / "index.html"
        if not page.exists():
            raise HTTPException(status_code=500, detail="static/index.html পাওয়া যায়নি")
        return FileResponse(page, media_type="text/html; charset=utf-8")

    @app.get("/api/health")
    async def health():
        snapshot = ctx.service.cache.snapshot
        return {
            "ok": True,
            "app": "বিদ্যুৎ বন্ধু",
            "version": "1.0.0",
            "provider": settings.provider,
            "demo": settings.is_demo,
            "llm": settings.llm_enabled,
            "telegram": settings.telegram_enabled,
            "has_data": snapshot is not None,
            "server_time": now_local(settings.timezone).isoformat(timespec="seconds"),
        }

    # ================= মূল ডেটা =================
    @app.get("/api/dashboard")
    async def dashboard(force: bool = Query(False, description="ক্যাশ বাদ দিয়ে নতুন করে আনুন")):
        try:
            return await ctx.service.dashboard(force=force)
        except ProviderError:
            raise
        except Exception as exc:
            log.exception("ড্যাশবোর্ড তৈরি ব্যর্থ")
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    @app.post("/api/refresh")
    async def refresh():
        await ctx.service.refresh()
        return await ctx.service.dashboard()

    @app.get("/api/status")
    async def status():
        snapshot = await ctx.service.snapshot()
        analysis = ctx.service.analyze(snapshot)
        return {
            "balance_tk": round(snapshot.balance_tk, 2),
            "balance_level": ctx.service.balance_state(snapshot)["level"],
            "reading_date": snapshot.reading_date,
            "reading_date_label": long_date(snapshot.reading_date),
            "days_left": analysis["forecast"]["days_left"],
            "avg_daily_units": analysis["forecast"]["avg_daily_units"],
            "month_units": analysis["usage"]["month_units"],
            "month_bill_tk": round(analysis["bill"].total, 2),
            "alerts": ctx.service.build_alerts(snapshot, analysis),
            "error": ctx.service.last_error,
        }

    @app.get("/api/usage")
    async def usage(days: int = Query(30, ge=1, le=120)):
        snapshot = await ctx.service.snapshot()
        analysis = ctx.service.analyze(snapshot)
        series = ctx.service.daily_series(snapshot, days=days)
        return {
            "days": days,
            "series": [
                {"date": r.date, "units": round(r.units, 2), "cost_tk": r.cost_tk} for r in series
            ],
            "summary": analysis["usage"],
            "anomaly": analysis["anomaly"],
        }

    @app.get("/api/bill")
    async def bill(units: float | None = Query(None, ge=0, le=100000)):
        snapshot = await ctx.service.snapshot()
        analysis = ctx.service.analyze(snapshot)
        target = units if units is not None else analysis["usage"]["month_units"]
        result = ctx.service.tariff.calculate(
            target,
            phase=snapshot.phase or settings.phase,
            sanctioned_load_kw=snapshot.sanctioned_load_kw or settings.sanctioned_load_kw,
        )
        return {
            "bill": result.to_dict(),
            "month_to_date": analysis["bill"].to_dict(),
            "projected": analysis["projected_bill"].to_dict(),
        }

    @app.get("/api/forecast")
    async def forecast():
        snapshot = await ctx.service.snapshot()
        analysis = ctx.service.analyze(snapshot)
        return {
            "forecast": analysis["forecast"],
            "balance_tk": round(snapshot.balance_tk, 2),
            "projected_bill": analysis["projected_bill"].to_dict(),
            "budget": {
                "limit_tk": settings.monthly_budget_tk,
                "spent_tk": round(analysis["bill"].total, 2),
            },
        }

    @app.get("/api/tariff")
    async def tariff(category: str = Query("residential")):
        return ctx.tariff.summary(category)

    @app.get("/api/alerts")
    async def alerts():
        snapshot = await ctx.service.snapshot()
        return {"alerts": ctx.service.build_alerts(snapshot), "last_error": ctx.service.last_error}

    @app.get("/api/recharges")
    async def recharges(limit: int = Query(10, ge=1, le=50)):
        snapshot = await ctx.service.snapshot()
        rows = snapshot.recharges[-limit:][::-1]
        return {
            "recharges": [r.to_dict() for r in rows],
            "total_recharged_tk": snapshot.total_recharged_tk,
        }

    @app.get("/api/history")
    async def history(days: int = Query(60, ge=1, le=400)):
        rows = ctx.service.history.series(days=days)
        return {"days": days, "series": [r.to_dict() for r in rows]}

    @app.get("/api/live")
    async def live():
        snapshot = await ctx.service.snapshot()
        return {
            "live": snapshot.live.to_dict() if snapshot.live else None,
            "balance_tk": round(snapshot.balance_tk, 2),
            "note": (
                "DESCO/DPDC-র API লাইভ লোড দেয় না। সেন্সর যোগ করলে এখানেই আসবে।"
                if not snapshot.live
                else "ডেমো মোডে লোড সিমুলেট করা হচ্ছে।"
            ),
        }

    # ================= চ্যাট =================
    @app.post("/api/chat")
    async def chat(payload: ChatRequest):
        question = payload.message.strip()
        if not question:
            raise HTTPException(status_code=400, detail="প্রশ্ন খালি")
        try:
            result = await ctx.agent.answer(question, payload.history)
        except ProviderError:
            raise
        except Exception as exc:
            log.exception("চ্যাট ব্যর্থ")
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
        return result

    # ================= ম্যানুয়াল চেক =================
    @app.post("/api/check")
    async def check(send: bool = Query(True)):
        result = await ctx.monitor.poll_once(send_alerts=send)
        return result

    @app.get("/api/summary")
    async def summary():
        return {"text": await ctx.service.summary_text()}

    @app.get("/api/config")
    async def config():
        """UI-তে দেখানোর মতো সেটিংস — কোনো গোপন কী এখানে নেই।"""
        return {
            "provider": settings.provider,
            "provider_label": settings.provider_label,
            "demo": settings.is_demo,
            "account_no": settings.account_no,
            "low_balance_tk": settings.low_balance_tk,
            "critical_balance_tk": settings.critical_balance_tk,
            "monthly_budget_tk": settings.monthly_budget_tk,
            "anomaly_multiplier": settings.anomaly_multiplier,
            "poll_minutes": settings.poll_minutes,
            "report_hour": settings.report_hour,
            "timezone": settings.timezone,
            "llm_enabled": settings.llm_enabled,
            "llm_model": settings.llm_model if settings.llm_enabled else None,
            "telegram_enabled": settings.telegram_enabled,
            "telegram_configured": bool(settings.telegram_chat_ids),
            "tariff_effective_from": ctx.tariff.meta().get("effective_from"),
            "app_name": "বিদ্যুৎ বন্ধু",
            "version": "1.0.0",
        }

    return app


def _mount_static(app: FastAPI, settings: Settings) -> None:
    from fastapi.staticfiles import StaticFiles

    if settings.static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


app = create_app()
_mount_static(app, app.state.ctx.settings)
