"""Read-only dry run: proves what the bot would do, without sending anything.

    python -m app.dry_run            # human-readable report, exit 0 on PASS / 1 on REJECT
    python -m app.dry_run --json     # the same report as JSON

This module is deliberately unable to trade. It connects to the terminal the gateway is configured
with, reads the account and the market, and then runs the *real* code paths - the strategy, the
bracket re-anchoring used by the market loop, the sizing functions and the ordered risk gate - and
prints their verdict. The gateway is wrapped in ``ReadOnlyGateway``, which raises on every MT5
write call, so a bug in this file cannot become an order: the report ends with the number of orders
submitted, and it is structurally zero.

The broker's symbol names are discovered at runtime (``EURUSDm`` for a broker whose majors are
suffixed), never assumed. The risk state is read from the configured database and from nowhere
else: a local SQLite fallback would report another store's allowance as if it were this bot's, so
an unreachable database is reported as DOWN and judged as a REJECT instead.
"""
import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.clock import utcnow
from app.core.config import Settings
from app.core.schemas import SignalAction
from app.mt5.account import AccountProfile, account_matches, account_profile
from app.mt5.gateway import MT5Gateway
from app.news.provider import build_news_provider
from app.risk.engine import EntryFacts, RiskEngine
from app.risk.sizing import (
    min_stop_distance,
    minimum_volume_risk,
    required_margin,
    risk_per_lot,
    size_for_loss_budget,
    spec_from_symbol_info,
    value_per_point_per_lot,
)
from app.risk.state import RiskStateStore
from app.services.market import completed_candles, tick_snapshot
from app.services.scheduler import MarketScheduler, SymbolContext
from app.strategies import STRATEGIES, momentum, strategy_timeframe

logger = logging.getLogger("app.dry_run")

# Instruments the owner asked this tiny account to avoid: they are skipped as candidates rather
# than being used to prove that something traded.
VOLATILE_EXCLUSIONS = ("XNGUSD", "XAUUSD", "XAGUSD", "XTIUSD", "USOIL", "UKOIL", "BTCUSD", "ETHUSD")
MAJORS = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY")
# The schema this code expects. tests/test_migrations.py asserts it is the head of the chain, so
# the dry run reports a stale database as "not migrated" instead of guessing from a missing table.
EXPECTED_ALEMBIC_REVISION = "0006_news_calendar"


class ReadOnlyGateway:
    """Every read path is forwarded; every write path raises. The dry run cannot submit an order."""

    WRITE_METHODS = ("order_send", "modify_position", "close_position", "cancel_order")

    def __init__(self, inner):
        self._inner = inner
        self.attempted_writes: list[str] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _refuse(self, name: str):
        self.attempted_writes.append(name)
        raise RuntimeError(f"{name} is not available in a dry run: no order may leave this process")

    def order_send(self, request): self._refuse("order_send")
    def modify_position(self, ticket, symbol, stop_loss, take_profit): self._refuse("modify_position")
    def close_position(self, position): self._refuse("close_position")
    def cancel_order(self, order): self._refuse("cancel_order")


@dataclass
class SymbolEvaluation:
    """Everything one symbol contributed to the report, plus the gate's own verdict."""

    symbol: str
    status: str = "UNEVALUATED"
    blocked: list[str] = field(default_factory=list)
    signal: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)
    spec_info: dict = field(default_factory=dict)
    news: dict = field(default_factory=dict)
    assessment: dict | None = None

    @property
    def failed(self) -> list[str]:
        return (self.assessment or {}).get("failed_checks", [])

    @property
    def reason(self) -> str:
        return (self.assessment or {}).get("reason", "") or (self.blocked[0] if self.blocked else "")


def state_session_factory(settings: Settings):
    """A session factory for the persisted risk state; an unreachable database is fatal here.

    Failing closed is the point: the dry run exists to report the allowance the *bot* has, so it
    must read the configured database. Falling back to a local SQLite file would silently report a
    different store's session loss and trade count as if they were the bot's, which is exactly the
    kind of unverifiable input every other layer refuses.
    """
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with engine.connect():
        pass
    return sessionmaker(bind=engine), f"configured database ({engine.dialect.name})"


def database_report(settings: Settings) -> dict:
    """Read-only statement of the risk-state store: reachable, dialect and migration revision."""
    report = {"expected_revision": EXPECTED_ALEMBIC_REVISION, "revision": None, "dialect": None, "status": "DOWN", "migrated": False, "error": None}
    engine = None
    try:
        engine = create_engine(settings.database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            report["dialect"] = engine.dialect.name
            report["revision"] = connection.execute(text("select version_num from alembic_version")).scalar()
            report["status"] = "UP"
            report["migrated"] = report["revision"] == EXPECTED_ALEMBIC_REVISION
            if not report["migrated"]:
                report["error"] = f"the database is at migration {report['revision']}, not {EXPECTED_ALEMBIC_REVISION}"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if engine is not None:
            engine.dispose()
    return report


def candidate_symbols(settings: Settings, gateway) -> list[str]:
    """The broker's own names for the configured majors, resolved at runtime."""
    configured = [name.strip().upper() for name in settings.symbols if name.strip()] or list(MAJORS)
    ordered = list(dict.fromkeys(list(settings.symbols) + list(MAJORS)))
    resolved: list[str] = []
    for canonical in ordered:
        name = str(canonical).strip().upper()
        if not name or any(name.startswith(excluded) for excluded in VOLATILE_EXCLUSIONS):
            continue
        real = gateway.discover_symbol(name)
        if real is None:
            real = gateway.discover_symbol(name[:6]) if len(name) > 6 else None
        if real and real not in resolved:
            resolved.append(real)
    logger.info("symbols_configured=%s symbols_discovered=%s", configured, resolved)
    return resolved


def technical_preview(spec, frames: dict, tick, settings: Settings, leverage: float) -> dict:
    """The sizing arithmetic at the strategy's own ATR stop for the current EMA bias.

    This is informational: it shows what the dollar limits do at *this* market's technical stop
    distance, on the side the higher timeframes lean towards. It never creates a signal, and it is
    reported separately from the risk chain, which is what decides.
    """
    import pandas as pd

    from app.indicators.technical import atr, ema
    from app.risk.sizing import position_risk

    m15 = frames[strategy_timeframe(settings, "trend_following")]
    h4, h1 = frames.get("H4"), frames.get("H1")
    if m15 is None or h4 is None or h1 is None or len(m15) < 210:
        return {}
    volatility = atr(m15).iloc[-1]
    if pd.isna(volatility) or float(volatility) <= 0:
        return {}
    distance = float(volatility) * 1.8  # trend_following's own stop multiple
    upper = ema(h4.close, 50).iloc[-1] > ema(h4.close, 200).iloc[-1] and ema(h1.close, 50).iloc[-1] > ema(h1.close, 200).iloc[-1]
    side = "BUY" if upper else "SELL"
    entry = tick.ask if side == "BUY" else tick.bid
    stop = entry - distance if side == "BUY" else entry + distance
    target = entry + 2 * distance if side == "BUY" else entry - 2 * distance
    cap = float(settings.max_lots_per_position)
    volume = size_for_loss_budget(settings.max_loss_per_trade_usd, entry, stop, spec)
    reason = ""
    if volume is not None and volume > cap:
        reason = f"the risk-derived volume {volume:g} exceeds the {cap:g} lot hard cap, so the setup would be refused rather than resized"
    return {
        "note": "illustrative arithmetic at the strategy's ATR stop, on the side the H4/H1 EMAs lean towards; not a signal",
        "side": side,
        "atr_price": float(volatility),
        "stop_multiple": 1.8,
        "entry": entry,
        "stop_loss": stop,
        "take_profit": target,
        "stop_distance_price": distance,
        "stop_distance_points": distance / spec.point if spec.point else None,
        "risk_per_lot_usd": risk_per_lot(entry, stop, spec),
        "minimum_volume_risk_usd": minimum_volume_risk(entry, stop, spec),
        "volume": volume,
        "volume_risk_usd": position_risk(volume, entry, stop, spec) if volume else None,
        "margin_usd": required_margin(volume, spec, entry, leverage) if volume else None,
        "minimum_lot_margin_usd": required_margin(spec.volume_min, spec, entry, leverage),
        "risk_reward": 2.0,
        "max_loss_per_trade_usd": settings.max_loss_per_trade_usd,
        "max_bot_capital_usd": settings.max_bot_capital_usd,
        "max_lots_per_position": cap,
        "within_cap": bool(volume) and volume <= cap,
        "reason": reason,
        "within_budget": bool(volume) and volume <= cap and position_risk(volume, entry, stop, spec) <= settings.max_loss_per_trade_usd,
    }


def evaluate_symbol(gateway, scheduler, risk: RiskEngine, settings: Settings, symbol: str, now: datetime, store: RiskStateStore) -> SymbolEvaluation:
    evaluation = SymbolEvaluation(symbol=symbol)
    info = gateway.symbol_info(symbol)
    spec = spec_from_symbol_info(info)
    if spec is None:
        evaluation.status = "BLOCKED"
        evaluation.blocked.append("symbol specification is unavailable")
        return evaluation
    evaluation.spec_info = {
        "digits": spec.digits,
        "point": spec.point,
        "tick_size": spec.tick_size,
        "tick_value": spec.tick_value,
        "contract_size": spec.contract_size,
        "volume_min": spec.volume_min,
        "volume_max": spec.volume_max,
        "volume_step": spec.volume_step,
        "margin_per_lot": spec.margin_per_lot,
        "min_stop_distance": min_stop_distance(spec),
        "value_per_point_per_lot": value_per_point_per_lot(spec),
        "broker_description": str(getattr(info, "description", "") or ""),
    }
    tick, tick_reason = tick_snapshot(gateway.tick(symbol), spec, now, max_age_seconds=settings.market_data_max_age_seconds)
    if tick is None:
        evaluation.status = "BLOCKED"
        evaluation.blocked.append(tick_reason)
        return evaluation
    evaluation.levels.update({"bid": tick.bid, "ask": tick.ask, "spread_points": tick.spread_points(spec.point), "spread_price": tick.ask - tick.bid, "tick_time": tick.time.isoformat() if tick.time else None, "age_seconds": (now - tick.time).total_seconds() if tick.time else None})

    frames = {}
    for timeframe in sorted({"H4", "H1", strategy_timeframe(settings, "trend_following"), strategy_timeframe(settings, "momentum")}):
        frame, reason = completed_candles(gateway, symbol, timeframe, settings.scheduler_candle_count, now=now, max_age_seconds=settings.market_data_max_age_seconds)
        if frame is None:
            evaluation.status = "BLOCKED"
            evaluation.blocked.append(f"candles unavailable for {timeframe}: {reason}")
            return evaluation
        frames[timeframe] = frame

    strategy = STRATEGIES["trend_following"]
    signal = strategy(symbol, frames["H4"], frames["H1"], frames[strategy_timeframe(settings, "trend_following")], rr=settings.min_risk_reward_ratio)
    evaluation.signal = {
        "strategy": signal.strategy,
        "action": str(signal.action),
        "score": signal.score,
        "confidence": signal.confidence,
        "timeframe": strategy_timeframe(settings, "trend_following"),
        "reasons": list(signal.reasons),
        "raw_entry": signal.entry,
        "raw_stop_loss": signal.stop_loss,
        "raw_take_profit": signal.take_profit,
        "tech_stop_distance": abs(signal.entry - signal.stop_loss) if signal.entry and signal.stop_loss else None,
    }
    momentum_signal = momentum.evaluate(symbol, frames["H4"], frames["H1"], frames[strategy_timeframe(settings, "momentum")], rr=settings.min_risk_reward_ratio)
    evaluation.signal["momentum_action"] = str(momentum_signal.action)

    context = SymbolContext(
        symbol=symbol, now=now, account=gateway.account_info(), spec=spec, tick=tick,
        equity=float(getattr(gateway.account_info(), "equity", 0.0) or 0.0),
        free_margin=getattr(gateway.account_info(), "margin_free", None),
        leverage=float(getattr(gateway.account_info(), "leverage", 0.0) or 0.0),
        margin_level=None, exposure=0.0, open_positions=0, symbol_positions=0, store=store,
        profile=account_profile(gateway.account_info()),
        algo_trading_enabled=None, data_age_seconds=(now - tick.time).total_seconds() if tick.time else None,
        momentum_action=str(momentum_signal.action), frames=frames, candle_closes={},
    )
    levels, level_reasons = scheduler._levels(signal, context) if signal.action is not SignalAction.HOLD else (None, ["the strategy returned HOLD: there is no setup to bracket"])
    if levels is None:
        evaluation.levels["bracket_reasons"] = list(level_reasons)
        final = signal
    else:
        entry, stop_loss, take_profit = levels
        evaluation.levels.update({
            "entry": entry, "stop_loss": stop_loss, "take_profit": take_profit,
            "stop_distance_price": abs(entry - stop_loss),
            "stop_distance_points": abs(entry - stop_loss) / spec.point if spec.point else None,
            "target_distance_price": abs(take_profit - entry),
        })
        final = signal.model_copy(update={"entry": entry, "stop_loss": stop_loss, "take_profit": take_profit})

    positions = gateway.positions()
    managed = None if positions is None else [p for p in positions if getattr(p, "magic", settings.magic_number) == settings.magic_number]
    terminal = gateway.terminal_info()
    news = build_news_provider(settings).decision(symbol, now)
    evaluation.news = {"available": news.available, "allowed": news.allowed, "reason": news.reason}
    evaluation.levels["preview"] = technical_preview(spec, frames, tick, settings, context.leverage)
    evaluation.levels["open_positions_total"] = None if positions is None else len(positions)
    evaluation.levels["open_positions_managed"] = None if managed is None else len(managed)

    facts = EntryFacts(
        spec=spec, connected=True, account=context.profile,
        algo_trading_enabled=None if terminal is None else bool(getattr(terminal, "trade_allowed", False)),
        data_age_seconds=context.data_age_seconds, momentum_action=str(momentum_signal.action),
        spread_points=tick.spread_points(spec.point), open_positions=None if managed is None else len(managed),
        symbol_positions=None if managed is None else sum(1 for p in managed if str(getattr(p, "symbol", "")).upper() == symbol.upper()),
        exposure=0.0, margin_level=None,
        duplicate_exists=False, emergency_locked=bool(store.load().emergency_locked), trading_enabled=True,
        free_margin=context.free_margin, equity=context.equity, leverage=context.leverage,
    )
    assessment = risk.assess(final, facts, state=store)
    evaluation.assessment = assessment.as_dict()
    evaluation.status = "PASS" if assessment.approved else "REJECT"
    return evaluation


def select(evaluations: list[SymbolEvaluation]) -> SymbolEvaluation | None:
    """The symbol to report in detail: the best candidate, PASS first and then signal quality."""
    if not evaluations:
        return None
    order = {"PASS": 0, "REJECT": 1, "BLOCKED": 2, "UNEVALUATED": 3}
    def key(item: SymbolEvaluation):
        tradeable = str(item.signal.get("action", "HOLD"))
        return (order.get(item.status, 9), 0 if tradeable in ("BUY", "SELL") else 1, -int(item.signal.get("score", 0) or 0), int(item.levels.get("spread_points", 999) or 999))
    return min(evaluations, key=key)


def money(value, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def build_report(settings: Settings, gateway, now: datetime) -> dict:
    profile: AccountProfile | None = account_profile(gateway.account_info())
    matched, mismatch = account_matches(profile, settings.mt5_login, settings.mt5_server)
    risk = RiskEngine(settings)
    database = database_report(settings)
    session_factory = None
    state_detail = f"unavailable ({database['error']})"
    row = None
    session_realized, trades_opened, emergency_locked = 0.0, 0, False
    session_started_at, kill_switch_reason = None, None
    evaluations: list[SymbolEvaluation] = []
    try:
        session_factory, state_detail = state_session_factory(settings)
        db = session_factory()
        try:
            store = RiskStateStore(db)
            row = store.load()
            session_realized = float(row.realized_pnl or 0.0)
            trades_opened = int(row.trades_opened or 0)
            emergency_locked = bool(row.emergency_locked)
            session_started_at = row.session_started_at
            kill_switch_reason = row.kill_switch_reason
            scheduler = MarketScheduler(settings, gateway, session_factory, news=build_news_provider(settings))
            symbols = candidate_symbols(settings, gateway)
            evaluations = [evaluate_symbol(gateway, scheduler, risk, settings, symbol, now, store) for symbol in symbols]
        finally:
            db.close()
    except Exception as error:  # no risk state means no allowance, and the report says so
        logger.error("dry_run_risk_state_unavailable error=%s", type(error).__name__)
        database["status"] = "DOWN"
        database["error"] = f"{type(error).__name__}: {error}"
        state_detail = f"unavailable ({type(error).__name__})"

    chosen = select(evaluations)
    assessment = (chosen.assessment if chosen else None) or {}
    terminal = gateway.terminal_info()
    report = {
        "generated_at": now.isoformat(),
        "dry_run": True,
        "orders_submitted": 0,
        "write_calls_attempted": list(getattr(gateway, "attempted_writes", [])),
        "bot": {
            "trading_mode": settings.trading_mode.value,
            "live_trading_enabled": settings.live_trading_enabled,
            "live_orders_permitted": settings.live_orders_permitted,
            "enabled_strategies": list(settings.enabled_strategies),
            "primary_strategy": "trend_following",
            "confirmation_strategy": "momentum (confirmation only)",
            "magic_number": settings.magic_number,
        },
        "live_gates": {
            "trading_mode": settings.trading_mode.value,
            "live_trading_enabled": settings.live_trading_enabled,
            "live_orders_permitted": settings.live_orders_permitted,
            "orders_authorized": False,
            "changes_applied": [],
            "note": "these two values are what a live run would need; this run changed neither, and enabling them requires explicit operator authorization",
        },
        "database": database,
        "hard_limits": {
            "max_bot_capital_usd": settings.max_bot_capital_usd,
            "max_loss_per_trade_usd": settings.max_loss_per_trade_usd,
            "max_session_loss_usd": settings.max_session_loss_usd,
            "max_daily_trades": settings.max_daily_trades,
            "max_open_positions": settings.max_open_positions,
            "max_lots_per_position": settings.max_lots_per_position,
        },
        "account": {
            **(profile.as_dict() if profile else {}),
            "trade_mode_label": profile.trade_mode_label if profile else "UNKNOWN",
            "is_real": bool(profile and profile.is_real),
            "authorized_for_orders": bool(profile and profile.is_real and settings.live_orders_permitted) or bool(profile and not profile.is_real),
            "algo_trading_enabled": None if terminal is None else bool(getattr(terminal, "trade_allowed", False)),
            "company": profile.company if profile else None,
            "expected_login": settings.mt5_login,
            "expected_server": settings.mt5_server,
            "matches_expected": bool(matched),
            "mismatch_reason": mismatch,
        },
        "session": {
            "state_source": state_detail,
            "available": session_factory is not None,
            "day": row.day.isoformat() if row is not None else None,
            "session_started_at": session_started_at.isoformat() if session_started_at is not None else None,
            "realized_pnl_usd": session_realized,
            "session_loss_usd": max(0.0, -session_realized),
            "trades_opened": trades_opened,
            "max_daily_trades": settings.max_daily_trades,
            "max_session_loss_usd": settings.max_session_loss_usd,
            "emergency_locked": emergency_locked,
            "kill_switch_reason": kill_switch_reason,
        },
        "symbols_evaluated": [
            {
                "symbol": item.symbol,
                "status": item.status,
                "action": item.signal.get("action"),
                "score": item.signal.get("score"),
                "bid": item.levels.get("bid"),
                "ask": item.levels.get("ask"),
                "spread_points": item.levels.get("spread_points"),
                "reason": item.reason,
                "failed_checks": item.failed,
            }
            for item in evaluations
        ],
        "selected": chosen.symbol if chosen else None,
        "selection": {
            "symbol": chosen.symbol if chosen else None,
            "symbol_spec": chosen.spec_info if chosen else {},
            "signal": chosen.signal if chosen else {},
            "levels": chosen.levels if chosen else {},
            "news": chosen.news if chosen else {},
            "risk": assessment,
            "minimum_lot_risk_usd": assessment.get("min_lot_risk_usd"),
            "required_margin_usd": assessment.get("margin_usd"),
            "bot_capital_usd": settings.max_bot_capital_usd,
            "max_lots_per_position": settings.max_lots_per_position,
        },
        "verdict": "REJECT",
        "reason": "no symbol could be evaluated",
        "guards": [],
    }

    guard_reason = None
    if not matched:
        guard_reason = f"the terminal account could not be verified: {mismatch}"
    elif database["status"] != "UP":
        guard_reason = f"the risk-state database is unreachable, so no allowance can be verified ({database['error']})"
    elif not database["migrated"]:
        guard_reason = f"the risk-state schema is not at {EXPECTED_ALEMBIC_REVISION} ({database['error']})"
    elif chosen is None:
        guard_reason = "no configured symbol could be resolved, priced and analysed"
    elif chosen.assessment is None:
        guard_reason = chosen.reason or "the symbol could not be evaluated"
    elif not assessment.get("approved"):
        guard_reason = assessment.get("reason")
    elif chosen.news and not chosen.news.get("allowed"):
        guard_reason = f"news policy (app-level guard outside the ordered checks): {chosen.news.get('reason')}"

    if guard_reason is None:
        report["verdict"] = "PASS"
        report["reason"] = "every check in the ordered risk chain passed; the trade would be eligible for submission"
    else:
        report["verdict"] = "REJECT"
        report["reason"] = guard_reason
    return report


def render(report: dict) -> str:
    account, limits = report["account"], report["hard_limits"]
    session, selection = report["session"], report["selection"]
    database = report.get("database") or {}
    live = report.get("live_gates") or report["bot"]
    risk, signal, levels, spec = selection["risk"], selection["signal"], selection["levels"], selection["symbol_spec"]
    preview = levels.get("preview") or {}
    cap = limits.get("max_lots_per_position")
    lines = [
        "MT5 HARD-LIMIT DRY RUN (read-only: no order is ever sent)",
        "=" * 68,
        f"account / server        : {account.get('login')} / {account.get('server')}",
        f"broker / currency       : {account.get('company')} / {account.get('currency')}",
        f"balance / equity        : {money(account.get('balance'), 2)} / {money(account.get('equity'), 2)} {account.get('currency') or ''}",
        f"free margin / leverage  : {money(account.get('free_margin'), 2)} / 1:{money(account.get('leverage'), 0)}",
        f"detected trade mode     : {account.get('trade_mode_label')} (trade_mode={account.get('trade_mode')})"
        + ("  <-- REAL MONEY ACCOUNT" if account.get("is_real") else ""),
        f"expected account        : {account.get('expected_login')} / {account.get('expected_server')} -> matches_expected={account.get('matches_expected')}",
        f"bot mode / live gate    : {report['bot']['trading_mode']} / live_orders_permitted={report['bot']['live_orders_permitted']}",
        f"terminal algo trading   : {account.get('algo_trading_enabled')}",
        "",
        "DATABASE / MIGRATIONS",
        f"  status                : {database.get('status')} ({database.get('dialect') or 'n/a'})",
        f"  alembic revision      : {database.get('revision')} (expected {database.get('expected_revision')}, migrated={database.get('migrated')})",
        f"  detail                : {database.get('error') or 'ok'}",
        "",
        "HARD LIMITS (server-side ceilings, never targets)",
        f"  bot capital allocation: {money(limits['max_bot_capital_usd'], 2)} USD (allocation ceiling, not equity)",
        f"  max loss per trade    : {money(limits['max_loss_per_trade_usd'], 2)} USD",
        f"  max session loss      : {money(limits['max_session_loss_usd'], 2)} USD",
        f"  max trades per session: {limits['max_daily_trades']}",
        f"  max simultaneous pos. : {limits['max_open_positions']}",
        f"  max lots per position : {money(cap, 4)}",
        f"session realized P/L    : {money(session.get('realized_pnl_usd'), 4)} USD (loss {money(session.get('session_loss_usd'), 4)} USD)",
        f"session trades / start  : {session.get('trades_opened')} of {session.get('max_daily_trades')} | started {session.get('session_started_at')} (day {session.get('day')})",
        f"kill switch / lock      : {session.get('kill_switch_reason') or 'not latched'} | emergency lock {session.get('emergency_locked')}",
        f"session state source    : {session.get('state_source')}",
        "",
        f"strategy enabled        : {report['bot']['enabled_strategies']} (primary {report['bot']['primary_strategy']})",
        f"symbol selected         : {selection['symbol']}",
        f"  broker description    : {spec.get('broker_description')}",
        f"  point / tick size     : {spec.get('point')} / {spec.get('tick_size')} (tick value {spec.get('tick_value')} per lot)",
        f"  value per point / lot : {money(spec.get('value_per_point_per_lot'), 6)} {account.get('currency') or ''}",
        f"  broker minimum lot    : {money(spec.get('volume_min'), 4)} (step {money(spec.get('volume_step'), 4)}, max {money(spec.get('volume_max'), 2)})",
        f"bid / ask               : {money(levels.get('bid'), 5)} / {money(levels.get('ask'), 5)}",
        f"spread                  : {money(levels.get('spread_points'), 1)} points",
        f"strategy signal         : {signal.get('action')} score {signal.get('score')} (momentum confirmation: {signal.get('momentum_action')})",
        f"  signal reasons        : {signal.get('reasons')}",
        f"  trend timeframe       : {signal.get('timeframe')} (data age {money(levels.get('age_seconds'), 1)}s)",
        f"technical stop distance : {money(signal.get('tech_stop_distance'), 5)} price ({money(levels.get('stop_distance_points'), 1)} points, ATR-based from the strategy)",
        f"entry (executable)      : {money(levels.get('entry'), 5)}",
        f"technical SL            : {money(levels.get('stop_loss'), 5)}",
        f"take profit             : {money(levels.get('take_profit'), 5)}",
        f"risk/reward             : {money(risk.get('risk_reward'), 3)} : 1 (target only, not a profit guarantee)",
        f"calculated safe lot     : {money(risk.get('volume'), 4)} (hard cap {money(risk.get('max_lots_per_position') or cap, 4)})",
        f"expected loss at SL     : {money(risk.get('risk_usd'), 4)} USD (limit {money(limits['max_loss_per_trade_usd'], 2)} USD)",
        f"broker-minimum-lot risk : {money(risk.get('min_lot_risk_usd'), 4)} USD at the same technical stop",
        f"required margin         : {money(selection.get('required_margin_usd'), 4)} USD (allocation {money(selection.get('bot_capital_usd'), 2)} USD)",
        f"news guard (app level)  : {selection['news'].get('allowed')} - {selection['news'].get('reason')}",
        "",
        "SIZING AT THE TECHNICAL STOP (illustrative; the strategy above is not signalling)",
        f"  side / ATR / multiple : {preview.get('side')} / {money(preview.get('atr_price'), 5)} / {preview.get('stop_multiple')} x ATR",
        f"  illustrative entry    : {money(preview.get('entry'), 5)}  SL {money(preview.get('stop_loss'), 5)}  TP {money(preview.get('take_profit'), 5)}",
        f"  stop distance         : {money(preview.get('stop_distance_points'), 1)} points ({money(preview.get('stop_distance_price'), 5)} price)",
        f"  risk per lot          : {money(preview.get('risk_per_lot_usd'), 4)} USD",
        f"  broker minimum lot    : {money(spec.get('volume_min'), 4)} -> {money(preview.get('minimum_volume_risk_usd'), 4)} USD at this stop"
        + (f"  <-- EXCEEDS the {money(limits['max_loss_per_trade_usd'], 2)} USD budget, so no trade" if (preview.get("minimum_volume_risk_usd") or 0) > limits["max_loss_per_trade_usd"] else ""),
        f"  calculated safe lot   : {money(preview.get('volume'), 4)} -> loss {money(preview.get('volume_risk_usd'), 4)} USD, margin {money(preview.get('margin_usd'), 4)} USD",
        f"  hard lot cap          : {money(preview.get('max_lots_per_position'), 4)} -> within_cap={preview.get('within_cap')} {preview.get('reason') or ''}".rstrip(),
        f"  minimum-lot margin    : {money(preview.get('minimum_lot_margin_usd'), 4)} USD (allocation {money(limits['max_bot_capital_usd'], 2)} USD)",
        f"  inside the hard limits: {preview.get('within_budget')}",
        f"  open positions        : {levels.get('open_positions_managed')} bot-managed / {levels.get('open_positions_total')} total on the account",
        "",
        "ORDERED FILTER CHAIN",
    ]
    for index, check in enumerate(risk.get("checks", []), start=1):
        mark = "ok  " if check["ok"] else "FAIL"
        lines.append(f"  {index:2d}. {check['name']:<24} {mark} {'' if check['ok'] else check['reason']}".rstrip())
    lines += [
        "",
        "SYMBOLS CONSIDERED",
    ]
    for item in report["symbols_evaluated"]:
        lines.append(f"  {item['symbol']:<10} {item['status']:<8} {str(item['action']):<5} score={item['score']} spread={money(item['spread_points'], 1)}pt  {item['reason']}")
    lines += [
        "",
        "LIVE GATES (read-only statement: this run changed nothing)",
        f"  current  : TRADING_MODE={live.get('trading_mode')}  LIVE_TRADING_ENABLED={str(live.get('live_trading_enabled')).lower()}"
        f"  -> live_orders_permitted={live.get('live_orders_permitted')}",
        "  proposed : TRADING_MODE=live  LIVE_TRADING_ENABLED=true  -> live_orders_permitted=True",
        "  action   : REQUIRES EXPLICIT AUTHORIZATION. Neither variable was changed by this run;",
        "             every order below is refused while the gates read demo/false.",
        "",
        "=" * 68,
        f"VERDICT: {report['verdict']}",
        f"REASON : {report['reason']}",
        f"orders submitted: {report['orders_submitted']} (write calls attempted: {report['write_calls_attempted'] or 'none'})",
        "=" * 68,
        "This is a target relationship and a risk report, not a promise of profit.",
        "No order, stop or target was submitted, modified or cancelled by this run.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.dry_run", description="read-only MT5 hard-limit dry run")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    settings = Settings()
    now = utcnow()
    gateway = ReadOnlyGateway(MT5Gateway(settings))
    health = gateway.initialize()
    if not health.connected:
        print(json.dumps({"dry_run": True, "orders_submitted": 0, "verdict": "REJECT", "reason": f"MT5 initialization failed: {health.detail}"}) if args.json else f"MT5 initialization failed: {health.detail}")
        return 1
    try:
        login = gateway.login()
        if not login.connected:
            print(json.dumps({"dry_run": True, "orders_submitted": 0, "verdict": "REJECT", "reason": f"MT5 login failed: {login.detail}"}) if args.json else f"MT5 login failed: {login.detail}")
            return 1
        report = build_report(settings, gateway, now)
    finally:
        gateway.shutdown()

    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
