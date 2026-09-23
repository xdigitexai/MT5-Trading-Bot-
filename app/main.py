"""HTTP API.

Every trading-touching route requires the bearer token; only ``/api/health`` answers without one,
and it never touches the database. The signal, performance and statistics routes read the rows the
market loop persisted, so a demo run shows real data instead of a placeholder.
"""
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import as_utc, isoformat
from app.core.config import Settings, get_settings
from app.core.schemas import EmergencyRequest
from app.database.base import AuditRecord, SignalRecord, TradeRecord
from app.database.session import SessionLocal, get_db
from app.execution.service import ExecutionService
from app.mt5.account import account_matches
from app.mt5.gateway import MT5Gateway
from app.news.provider import SqlNewsCache, build_news_provider
from app.risk.authorization import authorization_state
from app.risk.state import RiskStateStore
from app.services.analytics import performance as performance_summary
from app.services.analytics import statistics as statistics_summary
from app.services.bot import BotService
from app.services.reconciliation import Reconciler
from app.services.scheduler import MarketScheduler
from app.strategies import ENSEMBLE, STRATEGIES, STRATEGY_NAMES, enabled_strategies, ensemble_enabled, strategy_timeframe

settings = get_settings()
gateway = MT5Gateway(settings)
news = build_news_provider(settings, SqlNewsCache(SessionLocal))
reconciler = Reconciler(settings, gateway)
scheduler = MarketScheduler(settings, gateway, SessionLocal, news=news, reconciler=reconciler)
execution = ExecutionService(settings, gateway)
bot = BotService(settings, gateway, session_factory=SessionLocal, scheduler=scheduler)
bearer = HTTPBearer()


def current_settings() -> Settings:
    return settings


def current_bot() -> BotService:
    return bot


def current_scheduler() -> MarketScheduler:
    return scheduler


def auth(credentials: HTTPAuthorizationCredentials = Depends(bearer), config: Settings = Depends(current_settings)):
    if credentials.credentials != config.api_token.get_secret_value():
        raise HTTPException(401, "invalid token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    bot.stop()
    gateway.shutdown()


app = FastAPI(title="MT5 Forex Bot", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])


def signal_payload(row: SignalRecord) -> dict:
    return {
        "signal_id": row.signal_id,
        "symbol": row.symbol,
        "strategy": row.strategy,
        "timeframe": row.timeframe,
        "direction": row.direction,
        "confidence": row.confidence,
        "score": row.score,
        "entry_price": row.entry_price,
        "stop_loss": row.stop_loss,
        "take_profit": row.take_profit,
        "reason": row.reason,
        "created_at": isoformat(row.created_at),
        "executed": bool(row.executed),
        "order_ticket": row.order_ticket,
        "status": row.status,
    }


@app.get("/api/health")
def health():
    h = gateway.health()
    return {
        "status": "ok" if h.connected else "degraded",
        "mt5": h.detail,
        "mode": settings.trading_mode.value,
        "live_orders_permitted": settings.live_orders_permitted,
        "bot_state": bot.state.state.value,
    }


@app.get("/api/account", dependencies=[Depends(auth)])
def account():
    return {"account": str(gateway.account_info()) if gateway.health().connected else None}


@app.get("/api/positions", dependencies=[Depends(auth)])
def positions():
    return {"positions": [str(x) for x in gateway.positions() or ()]}


@app.get("/api/orders", dependencies=[Depends(auth)])
def orders():
    return {"orders": [str(x) for x in gateway.orders() or ()]}


@app.get("/api/trades", dependencies=[Depends(auth)])
def trades(db: Session = Depends(get_db)):
    return [{"trade_id": x.trade_id, "symbol": x.symbol, "status": x.status} for x in db.scalars(select(TradeRecord).order_by(TradeRecord.id.desc()).limit(200))]


@app.get("/api/signals", dependencies=[Depends(auth)])
def signals(
    symbol: str | None = None,
    strategy: str | None = None,
    status: str | None = None,
    executed: bool | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Persisted signals with filters and pagination; newest closed candle first."""
    filters = []
    if symbol:
        filters.append(func.upper(SignalRecord.symbol) == symbol.upper())
    if strategy:
        filters.append(SignalRecord.strategy == strategy)
    if status:
        filters.append(SignalRecord.status == status)
    if executed is not None:
        filters.append(SignalRecord.executed.is_(executed))
    if date_from is not None:
        filters.append(SignalRecord.created_at >= as_utc(date_from))
    if date_to is not None:
        filters.append(SignalRecord.created_at <= as_utc(date_to))
    total = db.scalar(select(func.count()).select_from(SignalRecord).where(*filters)) or 0
    rows = list(db.scalars(
        select(SignalRecord).where(*filters).order_by(SignalRecord.created_at.desc(), SignalRecord.id.desc()).limit(limit).offset(offset)
    ))
    return {"signals": [signal_payload(row) for row in rows], "count": len(rows), "total": total, "limit": limit, "offset": offset}


@app.get("/api/performance", dependencies=[Depends(auth)])
def performance(db: Session = Depends(get_db)):
    """Realized performance of the reconciled closed trades."""
    return performance_summary(db)


@app.get("/api/statistics", dependencies=[Depends(auth)])
def statistics(db: Session = Depends(get_db)):
    """Win/loss statistics with per-strategy and per-symbol breakdowns."""
    return statistics_summary(db)


@app.get("/api/strategies", dependencies=[Depends(auth)])
def strategies(config: Settings = Depends(current_settings)):
    """Derived from the strategy registry, so an unimplemented name can never be advertised."""
    return {
        "strategies": list(STRATEGY_NAMES),
        "details": [
            {
                "name": name,
                "kind": "combiner" if name == ENSEMBLE else "strategy",
                "implemented": name in STRATEGIES or name == ENSEMBLE,
                "enabled": name in config.enabled_strategies,
                "timeframe": strategy_timeframe(config, name),
            }
            for name in STRATEGY_NAMES
        ],
        "enabled": enabled_strategies(config),
        "ensemble_enabled": ensemble_enabled(config),
    }


@app.get("/api/risk", dependencies=[Depends(auth)])
def risk(db: Session = Depends(get_db)):
    store = RiskStateStore(db)
    row = store.load()
    return {
        "risk_per_trade_pct": settings.risk_per_trade_pct,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "max_drawdown_pct": settings.max_drawdown_pct,
        "max_open_positions": settings.max_open_positions,
        "max_bot_capital_usd": settings.max_bot_capital_usd,
        "max_loss_per_trade_usd": settings.max_loss_per_trade_usd,
        "max_session_loss_usd": settings.max_session_loss_usd,
        "max_daily_trades": settings.max_daily_trades,
        "emergency_locked": bool(row.emergency_locked),
        "day": row.day.isoformat(),
        "realized_pnl_today": float(row.realized_pnl or 0.0),
        "session_loss_usd": max(0.0, -float(row.realized_pnl or 0.0)),
        "trades_opened": int(row.trades_opened or 0),
        "starting_equity": row.starting_equity,
        "peak_equity": row.peak_equity,
        "mode": settings.trading_mode.value,
        "live_orders_permitted": settings.live_orders_permitted,
        "one_trade_authorization": authorization_state(settings, store).as_dict(),
    }


@app.get("/api/bot/status", dependencies=[Depends(auth)])
def status(service: BotService = Depends(current_bot)):
    return service.status()


def _database_healthy(db: Session) -> tuple[bool, str]:
    from sqlalchemy import text as sql_text
    try:
        db.execute(sql_text("SELECT 1"))
        return True, ""
    except Exception as error:
        return False, f"the database is unreachable ({type(error).__name__})"


@app.get("/api/news/status", dependencies=[Depends(auth)])
def news_status():
    """Calendar provider health, data age and the gate's current verdict shape; never a credential."""
    return news.status()


@app.get("/api/status", dependencies=[Depends(auth)])
def operational_status(db: Session = Depends(get_db)):
    """One authenticated answer for every operational question, and no credential anywhere in it."""
    health = gateway.health()
    profile = bot.account_profile()
    database_ok, database_detail = _database_healthy(db)
    risk = None
    risk_ok, risk_detail = False, "the persisted risk state could not be read"
    store = RiskStateStore(db)
    try:
        row = store.load()
        risk_ok = True
        risk_detail = ""
        risk = {
            "day": row.day.isoformat(),
            "realized_pnl": float(row.realized_pnl or 0.0),
            "session_loss_usd": max(0.0, -float(row.realized_pnl or 0.0)),
            "trades_opened": int(row.trades_opened or 0),
            "max_daily_trades": settings.max_daily_trades,
            "session_started_at": isoformat(row.session_started_at),
            "emergency_locked": bool(row.emergency_locked),
            "kill_switch_reason": row.kill_switch_reason,
            "kill_switch_active": bool((row.kill_switch_reason or "").strip()),
        }
    except Exception as error:
        risk_detail = f"the persisted risk state could not be read ({type(error).__name__})"
    positions = gateway.positions()
    managed = None if positions is None else [p for p in positions if getattr(p, "magic", settings.magic_number) == settings.magic_number]
    scheduler_status = scheduler.status()
    account_ok, account_detail = account_matches(profile, settings.mt5_login, settings.mt5_server)
    return {
        "engine": {"running": bot.state.running, "state": bot.state.state.value, "detail": bot.state.detail},
        "mt5": {"connected": health.connected, "detail": health.detail},
        "account": {
            "matches_expected": account_ok,
            "mismatch_reason": account_detail,
            "login": None if profile is None else profile.login,
            "server": None if profile is None else profile.server,
            "expected_login": settings.mt5_login,
            "expected_server": settings.mt5_server,
            "account_type": "UNKNOWN" if profile is None else profile.trade_mode_label,
            "is_real": None if profile is None else profile.is_real,
            "balance": None if profile is None else profile.balance,
            "currency": None if profile is None else profile.currency,
        },
        "database": {"healthy": database_ok, "detail": database_detail},
        "risk_store": {"healthy": risk_ok, "detail": risk_detail},
        "calendar": news.status(),
        "scheduler": {
            "running": scheduler_status["running"],
            "leader": scheduler_status["leader"],
            "standby": scheduler_status["standby"],
            "owner": scheduler_status["owner"],
            "heartbeat_at": scheduler_status["last_scan_at"],
            "cycles": scheduler_status["cycles"],
            "interval_seconds": scheduler_status["interval_seconds"],
            "lock_ttl_seconds": scheduler_status["lock_ttl_seconds"],
            "last_error": scheduler_status["last_error"],
        },
        "live_orders_permitted": settings.live_orders_permitted,
        "trading_mode": settings.trading_mode.value,
        # The effective live-ordering state of the order gate: the configuration above *and* the
        # owner's single unspent live trade. `trading_mode` stays `live` either way, so
        # reconciliation, analytics, the MT5 monitor and the dashboard keep working after the one
        # authorized trade has been reserved.
        "one_trade_authorization": authorization_state(settings, store).as_dict(),
        "positions": {
            "open": None if managed is None else len(managed),
            "readable": positions is not None,
            "universe_open": None if positions is None else len(positions),
        },
        "risk": risk,
        "hard_limits": {
            "max_bot_capital_usd": settings.max_bot_capital_usd,
            "max_loss_per_trade_usd": settings.max_loss_per_trade_usd,
            "max_session_loss_usd": settings.max_session_loss_usd,
            "max_daily_trades": settings.max_daily_trades,
            "max_open_positions": settings.max_open_positions,
            "max_lots_per_position": settings.max_lots_per_position,
        },
    }


@app.post("/api/bot/start", dependencies=[Depends(auth)])
def start(service: BotService = Depends(current_bot)):
    ok, detail = service.start()
    if not ok:
        raise HTTPException(409, detail)
    return {"status": detail, "state": service.state.state.value}


@app.post("/api/bot/stop", dependencies=[Depends(auth)])
def stop(service: BotService = Depends(current_bot)):
    _, detail = service.stop()
    return {"status": detail, "state": service.state.state.value}


@app.post("/api/trading/emergency-stop", dependencies=[Depends(auth)])
def emergency(request: EmergencyRequest, service: BotService = Depends(current_bot), db: Session = Depends(get_db)):
    report = service.emergency_stop(close_positions=request.close_positions, cancel_pending_orders=request.cancel_pending_orders)
    execution.emergency_stop(db, request.close_positions, request.cancel_pending_orders)
    return {"status": "emergency locked", "state": service.state.state.value, **report}


@app.post("/api/trading/emergency-reset", dependencies=[Depends(auth)])
def reset(service: BotService = Depends(current_bot), db: Session = Depends(get_db)):
    ok, detail = service.emergency_reset()
    if not ok:
        raise HTTPException(409, detail)
    db.add(AuditRecord(event_type="EMERGENCY_RESET", severity="WARNING", message="emergency lock cleared by an authenticated operator"))
    db.commit()
    return {"status": detail, "state": service.state.state.value}
