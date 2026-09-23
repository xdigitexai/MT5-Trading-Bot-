"""Market loop: the runtime that turns enabled strategies into (demo) orders.

One cycle walks the whole pipeline for every configured symbol:

    MT5 health -> completed candles -> indicators/strategies -> ensemble -> persist candidate
    -> risk engine -> market conditions -> position size -> SL/TP -> execute -> persist outcome

and then, periodically, monitors open positions and reconciles MT5 history.

Design rules:

- **One runner.** A process-wide lock plus a ``scheduler_locks`` row make a second concurrent
  cycle impossible. A cycle that cannot take the lock returns immediately instead of scanning.
- **One order per signal, ever.** The signal identity is
  ``symbol|strategy|timeframe|closed-candle close|direction``; it is used as the signal id, the
  trade id and the execution idempotency key. After a restart the same identity is recomputed, the
  persisted signal is found and nothing is re-sent; if the signal row is missing, the execution
  layer's idempotency fence still refuses the second order. A candidate left as NEW by a crash is
  retried only after ``stale_guard_seconds``, and reconciliation retires the guard it left behind.
- **Fail closed.** Health, account, symbol specification, tick/spread, news policy, candle
  completeness, exposure, margin level and the risk state are verified before an order is sent.
  Any unverifiable input blocks that symbol for the cycle and is logged with its reason; the bot
  never trades on a guess.
- **Completed candles only.** See app.services.market: a bar is used only once its own period has
  ended by the feed's newest timestamp, so the still-forming bar is never traded.
- **Executable prices.** The strategy's levels come from the last closed candle, but the order is
  sent at the price the market shows now: the strategy's stop and target *distances* are re-anchored
  to the current ask/bid, rounded away from the entry, so the risk/reward geometry it chose is
  preserved and the request stays executable. Barriers keep the strategy's own level.
- **No secrets, no silence.** Logs carry symbols, strategies, statuses, counts and reasons only, and
  every failure path is logged with its exception type.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from hashlib import sha256
import logging
import threading
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import as_utc, isoformat, utcnow
from app.core.config import Settings
from app.core.schemas import Signal, SignalAction, SignalStatus, TradeIntent
from app.database.base import ExecutionGuardRecord, SignalRecord, TradeRecord
from app.database.runtime import SchedulerLockRecord
from app.execution.service import ExecutionService
from app.execution.validation import validate_stops
from app.mt5.account import AccountProfile, account_profile
from app.mt5.gateway import MT5Gateway
from app.news.provider import NewsProvider, build_news_provider
from app.risk.engine import EntryFacts, RiskEngine
from app.risk.sizing import SymbolSpec, spec_from_symbol_info
from app.risk.state import RiskStateStore
from app.services.market import TickSnapshot, candle_close_time, completed_candles, tick_snapshot
from app.services.reconciliation import Reconciler
from app.strategies import STRATEGIES, combine, enabled_strategies, ensemble_enabled, strategy_timeframe

logger = logging.getLogger(__name__)

LOCK_KEY = "market-loop"


def signal_identity(signal: Signal, candle_time: datetime) -> str:
    """Stable identity of one strategy decision on one closed candle."""
    payload = "|".join((
        str(signal.symbol).upper(),
        str(signal.strategy),
        str(signal.timeframe),
        as_utc(candle_time).isoformat(),
        str(signal.action).upper(),
    ))
    return f"sig-{sha256(payload.encode()).hexdigest()[:40]}"


def _round_step(value: float, step: float, *, up: bool) -> float:
    """Round to the symbol tick away from the entry, so a stop or target never shrinks."""
    quantum = Decimal(str(step))
    if quantum <= 0:
        return float(value)
    units = (Decimal(str(value)) / quantum).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return float(units * quantum)


@dataclass
class SchedulerState:
    running: bool = False
    cycles: int = 0
    skipped_overlaps: int = 0
    last_scan_at: datetime | None = None
    last_signal_at: datetime | None = None
    last_execution_at: datetime | None = None
    last_reconciliation_at: datetime | None = None
    last_error: str | None = None
    last_cycle: dict | None = None


@dataclass
class SymbolContext:
    """Everything the per-symbol pipeline verified, so the candidate step does not re-check it."""

    symbol: str
    now: datetime
    account: object
    spec: SymbolSpec
    tick: TickSnapshot
    equity: float
    free_margin: float | None
    leverage: float
    margin_level: float | None
    exposure: float
    open_positions: int
    symbol_positions: int
    store: RiskStateStore
    profile: AccountProfile | None
    algo_trading_enabled: bool | None
    data_age_seconds: float | None
    momentum_action: str | None
    frames: dict = field(default_factory=dict)
    candle_closes: dict = field(default_factory=dict)


class MarketScheduler:
    def __init__(
        self,
        settings: Settings,
        gateway: MT5Gateway,
        session_factory,
        *,
        execution: ExecutionService | None = None,
        risk: RiskEngine | None = None,
        news: NewsProvider | None = None,
        reconciler: Reconciler | None = None,
        strategies: dict | None = None,
        clock=utcnow,
    ):
        self.settings, self.gateway, self.session_factory = settings, gateway, session_factory
        self.execution = execution or ExecutionService(settings, gateway)
        self.risk = risk or RiskEngine(settings)
        self.news = news if news is not None else build_news_provider(settings)
        self.reconciler = reconciler if reconciler is not None else Reconciler(settings, gateway)
        self.strategies = dict(STRATEGIES if strategies is None else strategies)
        self.clock = clock
        self.owner = f"{uuid.uuid4().hex}"
        self.state = SchedulerState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_reconciliation_at: datetime | None = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self.state.running

    @property
    def thread_alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    @property
    def last_scan_at(self) -> datetime | None:
        return self.state.last_scan_at

    @property
    def last_signal_at(self) -> datetime | None:
        return self.state.last_signal_at

    @property
    def last_execution_at(self) -> datetime | None:
        return self.state.last_execution_at

    @property
    def last_reconciliation_at(self) -> datetime | None:
        return self.state.last_reconciliation_at

    @property
    def last_error(self) -> str | None:
        return self.state.last_error

    @property
    def cycles(self) -> int:
        return self.state.cycles

    def start(self) -> tuple[bool, str]:
        if self.state.running:
            return False, "scheduler is already running"
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="market-scheduler", daemon=True)
        self.state.running = True
        self._thread.start()
        logger.info("scheduler_started interval_s=%s symbols=%s", self.settings.scheduler_interval_seconds, len(self.settings.symbols))
        return True, "scheduler started"

    def stop(self, timeout: float = 10.0) -> bool:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        alive = bool(thread is not None and thread.is_alive())
        self.state.running = False
        if alive:
            self.state.last_error = f"the scheduler thread did not stop within {timeout}s"
            logger.error("scheduler_stop_timeout timeout_s=%s", timeout)
            return False
        logger.info("scheduler_stopped cycles=%s", self.state.cycles)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(max(1, int(self.settings.scheduler_interval_seconds)))

    def status(self) -> dict:
        return {
            "running": self.running,
            "thread_alive": self.thread_alive,
            "cycles": self.state.cycles,
            "skipped_overlaps": self.state.skipped_overlaps,
            "interval_seconds": self.settings.scheduler_interval_seconds,
            "last_scan_at": isoformat(self.state.last_scan_at),
            "last_signal_at": isoformat(self.state.last_signal_at),
            "last_execution_at": isoformat(self.state.last_execution_at),
            "last_reconciliation_at": isoformat(self.state.last_reconciliation_at),
            "last_error": self.state.last_error,
            "symbols": list(self.settings.symbols),
            "last_cycle": self.state.last_cycle,
        }

    # ------------------------------------------------------------------ cycle

    def run_once(self, now: datetime | None = None) -> dict:
        """One full cycle; returns a JSON-friendly summary and never raises."""
        if not self._lock.acquire(blocking=False):
            self.state.skipped_overlaps += 1
            logger.warning("scheduler_cycle_skipped reason=cycle_already_running skipped_overlaps=%s", self.state.skipped_overlaps)
            return {"skipped": True, "reason": "a scheduler cycle is already running in this process", "symbols": {}}
        db = None
        try:
            db = self.session_factory()
            return self._cycle(db, as_utc(now) or self.clock())
        except Exception as error:  # a cycle failure must be visible, never swallowed
            self.state.last_error = f"{type(error).__name__}: {error}"
            logger.exception("scheduler_cycle_failed error=%s", type(error).__name__)
            return {"skipped": False, "errors": [self.state.last_error], "symbols": {}}
        finally:
            if db is not None:
                db.close()
            self._lock.release()

    def _cycle(self, db: Session, now: datetime) -> dict:
        summary = {"skipped": False, "started_at": now.isoformat(), "symbols": {}, "candidates": 0, "signals": 0, "executions": 0, "duplicates": 0, "blocked": [], "errors": [], "positions": None, "reconciliation": None}
        if not self._acquire_db_lock(db, now):
            summary.update(skipped=True, reason="another scheduler owns the market loop lock")
        else:
            try:
                self._run_cycle(db, summary, now)
            finally:
                self._release_db_lock(db)
        summary["finished_at"] = self.clock().isoformat()
        self.state.last_cycle = summary
        return summary

    def _run_cycle(self, db: Session, summary: dict, now: datetime) -> None:
        store = RiskStateStore(db)
        if store.load().emergency_locked:
            summary["blocked"].append("the emergency lock is persisted: no scan was performed")
            logger.warning("scheduler_cycle_blocked reason=emergency_locked")
            return
        for symbol in self.settings.symbols:
            result = self._scan_symbol(db, symbol, now, store, summary)
            summary["symbols"][symbol] = result
            summary["candidates"] += result.get("candidates", 0)
            summary["signals"] += result.get("signals", 0)
            summary["executions"] += result.get("executions", 0)
            summary["duplicates"] += result.get("duplicates", 0)
        self.state.last_scan_at = now
        self.state.cycles += 1
        summary["positions"] = self._monitor_positions()
        if self._reconciliation_due(now):
            summary["reconciliation"] = self._reconcile(db, now)

    def _monitor_positions(self) -> dict:
        positions = self.gateway.positions()
        if positions is None:
            logger.warning("position_monitor_unavailable reason=positions_unreadable")
            return {"error": "open positions could not be read from MT5"}
        managed = self._managed_positions(positions)
        by_symbol: dict[str, int] = {}
        for position in managed:
            name = str(getattr(position, "symbol", "") or "UNKNOWN")
            by_symbol[name] = by_symbol.get(name, 0) + 1
        return {"managed": len(managed), "by_symbol": by_symbol}

    def _reconciliation_due(self, now: datetime) -> bool:
        return self._next_reconciliation_at is None or now >= self._next_reconciliation_at

    def _reconcile(self, db: Session, now: datetime) -> dict:
        summary = self.reconciler.run(db, now=now)
        self.state.last_reconciliation_at = now
        self._next_reconciliation_at = now + timedelta(seconds=self.settings.reconciliation_interval_seconds)
        if summary.errors:
            self.state.last_error = "; ".join(summary.errors)
        return summary.as_dict()

    # ------------------------------------------------------------------ per symbol

    def _scan_symbol(self, db: Session, symbol: str, now: datetime, store: RiskStateStore, cycle: dict) -> dict:
        result = {"symbol": symbol, "status": "SCANNED", "candidates": 0, "signals": 0, "executions": 0, "duplicates": 0, "blocked": [], "candidates_detail": []}
        health = self.gateway.health()
        if not health.connected:
            return self._block(result, cycle, f"MT5 is not connected ({health.detail})")
        account = self.gateway.account_info()
        if account is None:
            return self._block(result, cycle, "account information is unavailable")
        equity = getattr(account, "equity", None)
        if equity is None or float(equity) <= 0:
            return self._block(result, cycle, "account equity is unavailable")
        spec = spec_from_symbol_info(self.gateway.symbol_info(symbol))
        if spec is None:
            return self._block(result, cycle, "symbol specification is unavailable")
        tick, tick_reason = tick_snapshot(self.gateway.tick(symbol), spec, now, max_age_seconds=self.settings.market_data_max_age_seconds)
        if tick is None:
            return self._block(result, cycle, tick_reason)
        news = self.news.decision(symbol, now)
        result["news"] = {"available": news.available, "reason": news.reason}
        if not news.allowed:
            return self._block(result, cycle, f"news policy blocks trading: {news.reason}")
        positions = self.gateway.positions()
        if positions is None:
            return self._block(result, cycle, "open positions cannot be read from MT5")
        managed = self._managed_positions(positions)
        spec_cache = {symbol: spec}
        exposure = self._exposure(managed, spec_cache)
        if exposure is None:
            return self._block(result, cycle, "total exposure cannot be verified")
        enabled = [name for name in enabled_strategies(self.settings) if name in self.strategies]
        if not enabled and not ensemble_enabled(self.settings):
            return self._block(result, cycle, "no enabled strategy is implemented in the registry")
        frames, closes, frame_reason = self._frames(symbol, enabled, now)
        if frames is None:
            return self._block(result, cycle, frame_reason)
        votes, vote_reason = self._votes(symbol, enabled, frames)
        if votes is None:
            return self._block(result, cycle, vote_reason)
        candidates = self._candidates(votes, symbol)
        profile = account_profile(account)
        terminal = self.gateway.terminal_info() if hasattr(self.gateway, "terminal_info") else None
        context = SymbolContext(
            symbol=symbol, now=now, account=account, spec=spec, tick=tick, equity=float(equity),
            free_margin=getattr(account, "margin_free", None), leverage=float(getattr(account, "leverage", 0) or 0),
            margin_level=self._margin_level(account), exposure=exposure, open_positions=len(managed),
            symbol_positions=sum(1 for position in managed if str(getattr(position, "symbol", "")).upper() == symbol.upper()),
            store=store, profile=profile,
            algo_trading_enabled=None if terminal is None else bool(getattr(terminal, "trade_allowed", False)),
            data_age_seconds=(now - tick.time).total_seconds() if tick.time is not None else None,
            momentum_action=self._momentum_action(symbol, frames),
            frames=frames, candle_closes=closes,
        )
        if profile is not None and profile.is_real:
            logger.critical(
                "account_is_real login=%s server=%s trade_mode=%s live_orders_permitted=%s trading_mode=%s",
                profile.login, profile.server, profile.trade_mode, self.settings.live_orders_permitted, self.settings.trading_mode.value,
            )
        result["candidates"] = len(candidates)
        for signal in candidates:
            candle_time = closes.get(signal.timeframe)
            if candle_time is None:
                result["blocked"].append(f"the closed candle time for {signal.timeframe} is unknown")
                logger.error("signal_candle_time_unknown symbol=%s timeframe=%s strategy=%s", symbol, signal.timeframe, signal.strategy)
                continue
            detail = self._submit_candidate(db, signal, candle_time, context)
            result["candidates_detail"].append(detail)
            result["signals"] += 1 if detail.get("persisted") else 0
            result["executions"] += 1 if detail.get("status") in (SignalStatus.EXECUTED, SignalStatus.SUBMITTED) else 0
            result["duplicates"] += 1 if detail.get("status") == SignalStatus.DUPLICATE else 0
        return result

    def _block(self, result: dict, cycle: dict, reason: str) -> dict:
        result["status"] = "BLOCKED"
        result["blocked"].append(reason)
        cycle["blocked"].append(f"{result.get('symbol')}: {reason}")
        logger.warning("symbol_blocked symbol=%s reason=%s", result.get("symbol"), reason)
        return result

    def _frames(self, symbol: str, enabled: list[str], now: datetime) -> tuple[dict | None, dict | None, str]:
        timeframes = {"H4", "H1"} | {strategy_timeframe(self.settings, name) for name in enabled}
        frames, closes = {}, {}
        for timeframe in sorted(timeframes):
            frame, reason = completed_candles(
                self.gateway, symbol, timeframe, self.settings.scheduler_candle_count,
                now=now, max_age_seconds=self.settings.market_data_max_age_seconds,
            )
            if frame is None:
                return None, None, f"candles unavailable for {timeframe}: {reason}"
            close_time = candle_close_time(frame, timeframe)
            if close_time is None:
                return None, None, f"the closed candle time for {timeframe} could not be derived"
            frames[timeframe], closes[timeframe] = frame, close_time
        return frames, closes, ""

    def _votes(self, symbol: str, enabled: list[str], frames: dict) -> tuple[list[Signal] | None, str]:
        votes = []
        for name in enabled:
            timeframe = strategy_timeframe(self.settings, name)
            frame = frames.get(timeframe)
            if frame is None:
                return None, f"{name} needs {timeframe} candles, which were not loaded"
            try:
                signal = self.strategies[name](symbol, frames["H4"], frames["H1"], frame, rr=self.settings.min_risk_reward_ratio)
                signal = signal.model_copy(update={"timeframe": timeframe})
            except Exception as error:  # a broken strategy must block the symbol, not the cycle
                logger.exception("strategy_failed symbol=%s strategy=%s error=%s", symbol, name, type(error).__name__)
                return None, f"strategy {name} raised {type(error).__name__}: the decision set is incomplete"
            logger.debug("strategy_vote symbol=%s strategy=%s timeframe=%s action=%s score=%s reasons=%s", symbol, name, timeframe, signal.action, signal.score, signal.reasons)
            votes.append(signal)
        return votes, ""

    def _candidates(self, votes: list[Signal], symbol: str) -> list[Signal]:
        if ensemble_enabled(self.settings):
            combined = combine(votes, self.settings)
            if combined.action is SignalAction.HOLD:
                logger.info("ensemble_hold symbol=%s reasons=%s", symbol, combined.reasons)
                return []
            return [combined]
        return [vote for vote in votes if vote.action is not SignalAction.HOLD]

    def _momentum_action(self, symbol: str, frames: dict) -> str | None:
        """Momentum as confirmation only: it never creates a candidate, and an unreadable value blocks."""
        evaluate = self.strategies.get("momentum") or STRATEGIES.get("momentum")
        frame = frames.get(strategy_timeframe(self.settings, "momentum"))
        if evaluate is None or frame is None:
            logger.warning("momentum_confirmation_unavailable symbol=%s", symbol)
            return None
        try:
            signal = evaluate(symbol, frames["H4"], frames["H1"], frame, rr=self.settings.min_risk_reward_ratio)
        except Exception as error:
            logger.error("momentum_confirmation_failed symbol=%s error=%s", symbol, type(error).__name__)
            return None
        return str(signal.action)

    def _duplicate_order_exists(self, db: Session, signal_id: str) -> bool:
        """True when this signal already produced an order or reserved an idempotency key."""
        trade = db.scalar(select(TradeRecord).where(TradeRecord.trade_id == signal_id))
        guard = db.scalar(select(ExecutionGuardRecord).where(ExecutionGuardRecord.idempotency_key == signal_id))
        return trade is not None or guard is not None

    # ------------------------------------------------------------------ candidate

    def _submit_candidate(self, db: Session, signal: Signal, candle_time: datetime, context: SymbolContext) -> dict:
        signal_id = signal_identity(signal, candle_time)
        existing = db.scalar(select(SignalRecord).where(SignalRecord.signal_id == signal_id))
        if existing is not None and not self._retryable(existing, context.now):
            logger.info("signal_duplicate signal_id=%s symbol=%s strategy=%s status=%s executed=%s", signal_id, signal.symbol, signal.strategy, existing.status, existing.executed)
            return {"signal_id": signal_id, "status": SignalStatus.DUPLICATE.value, "reason": f"signal {signal_id} is already recorded as {existing.status}"}
        row, persisted = self._persist_signal(db, signal, candle_time, signal_id, existing)
        self.state.last_signal_at = context.now
        levels, level_reasons = self._levels(signal, context)
        if levels is None:
            return self._reject_signal(db, row, level_reasons, "invalid_bracket", persisted)
        entry, stop_loss, take_profit = levels
        final = signal.model_copy(update={"entry": entry, "stop_loss": stop_loss, "take_profit": take_profit})
        row.entry_price, row.stop_loss, row.take_profit = entry, stop_loss, take_profit
        db.commit()
        decision = self.risk.assess(
            final,
            EntryFacts(
                spec=context.spec,
                connected=True,
                account=context.profile,
                algo_trading_enabled=context.algo_trading_enabled,
                data_age_seconds=context.data_age_seconds,
                momentum_action=context.momentum_action,
                spread_points=context.tick.spread_points(context.spec.point),
                open_positions=context.open_positions,
                symbol_positions=context.symbol_positions,
                exposure=context.exposure,
                margin_level=context.margin_level,
                duplicate_exists=self._duplicate_order_exists(db, signal_id),
                emergency_locked=False,
                trading_enabled=True,
                free_margin=context.free_margin,
                equity=context.equity,
                leverage=context.leverage,
            ),
            state=context.store,
        )
        result = decision.as_dict()
        if not decision.approved or not decision.volume:
            reasons = [f"{check.name}: {check.reason}" for check in decision.failures] or ["the risk engine did not return a usable volume"]
            rejected = self._reject_signal(db, row, reasons, "risk_rejected", persisted)
            return {**rejected, "risk": result}
        intent = TradeIntent(
            trade_id=signal_id, signal=final, volume=float(decision.volume), requested_price=entry,
            idempotency_key=signal_id, signal_id=signal_id,
        )
        outcome = self.execution.submit(db, intent)
        if row.status == SignalStatus.NEW.value:
            # The execution layer returns early for a duplicate trade_id or guard without touching
            # the signal, so the persisted status is completed here instead of staying NEW.
            row.status = outcome.status
            row.reason = f"{row.reason or ''} | {outcome.reason}".strip(" |")
            row.order_ticket = outcome.order_ticket or row.order_ticket
            db.commit()
        if outcome.accepted:
            self.state.last_execution_at = context.now
            # The counter lives in the database, so the next process cannot start with a fresh
            # allowance of trades after a restart.
            context.store.register_trade_opened()
        logger.info(
            "signal_outcome signal_id=%s symbol=%s strategy=%s status=%s volume=%s ticket=%s retcode=%s reason=%s",
            signal_id, signal.symbol, signal.strategy, outcome.status, decision.volume, outcome.order_ticket, outcome.retcode, outcome.reason,
        )
        return {"signal_id": signal_id, "status": outcome.status, "reason": outcome.reason, "order_ticket": outcome.order_ticket, "persisted": persisted, "volume": float(decision.volume), "risk": result}

    def _persist_signal(self, db: Session, signal: Signal, candle_time: datetime, signal_id: str, existing: SignalRecord | None) -> tuple[SignalRecord, bool]:
        if existing is not None:
            logger.info("signal_retry signal_id=%s symbol=%s status=%s created_at=%s", signal_id, signal.symbol, existing.status, isoformat(existing.created_at))
            return existing, False
        row = SignalRecord(
            signal_id=signal_id, symbol=signal.symbol, strategy=signal.strategy, timeframe=signal.timeframe,
            direction=str(signal.action), confidence=signal.confidence, score=signal.score,
            entry_price=signal.entry, stop_loss=signal.stop_loss, take_profit=signal.take_profit,
            reason="; ".join(signal.reasons) or None, created_at=candle_time, executed=False,
            status=SignalStatus.NEW.value,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        logger.info("signal_persisted signal_id=%s symbol=%s strategy=%s timeframe=%s direction=%s score=%s confidence=%s candle=%s", signal_id, signal.symbol, signal.strategy, signal.timeframe, signal.action, signal.score, signal.confidence, candle_time.isoformat())
        return row, True

    def _reject_signal(self, db: Session, row: SignalRecord, reasons: list[str], event: str, persisted: bool) -> dict:
        row.status = (SignalStatus.RISK_REJECTED if event == "risk_rejected" else SignalStatus.REJECTED).value
        rejected = "; ".join(reasons)
        row.reason = f"{row.reason} | {rejected}" if row.reason else rejected
        db.commit()
        logger.warning("signal_rejected signal_id=%s symbol=%s event=%s reasons=%s", row.signal_id, row.symbol, event, reasons)
        return {"signal_id": row.signal_id, "status": row.status, "reason": row.reason, "persisted": persisted}

    def _retryable(self, row: SignalRecord, now: datetime) -> bool:
        if row.executed or row.status != SignalStatus.NEW.value:
            return False
        created = as_utc(row.created_at) or now
        return (now - created) > timedelta(seconds=self.settings.stale_guard_seconds)

    def _levels(self, signal: Signal, context: SymbolContext) -> tuple[tuple[float, float, float] | None, list[str]]:
        """Re-anchor the strategy's stop/target distances to the price the market shows now."""
        is_buy = signal.action is SignalAction.BUY
        if not is_buy and signal.action is not SignalAction.SELL:
            return None, ["the signal direction is not tradable"]
        if signal.stop_loss is None:
            return None, ["the strategy supplied no stop loss, so the order was refused"]
        reference = float(signal.entry) if signal.entry else context.tick.mid
        risk = abs(reference - float(signal.stop_loss))
        if risk <= 0:
            return None, ["stop loss distance must be greater than zero"]
        steps = Decimal(1).scaleb(-context.spec.digits)
        ratio = float(self.settings.min_risk_reward_ratio)
        if signal.take_profit is not None:
            implied = abs(float(signal.take_profit) - reference) / risk
            if implied < ratio - 1e-9:
                return None, [f"the strategy risk/reward {implied:.2f} is below the required {ratio:g}:1"]
            ratio = implied
        entry = float(Decimal(str(context.tick.ask if is_buy else context.tick.bid)).quantize(steps))
        stop_loss = _round_step(entry - risk if is_buy else entry + risk, float(steps), up=not is_buy)
        distance = abs(entry - stop_loss)
        target = entry + distance * ratio if is_buy else entry - distance * ratio
        take_profit = _round_step(target, float(steps), up=is_buy)
        validation = validate_stops(signal.action, entry, stop_loss, take_profit, context.spec, min_risk_reward=self.settings.min_risk_reward_ratio)
        if not validation.ok:
            return None, validation.reasons
        return (entry, stop_loss, take_profit), []

    # ------------------------------------------------------------------ helpers

    def _managed_positions(self, positions) -> list:
        """Bot-managed positions only. An unreadable magic is treated as ours, so it still counts."""
        magic = self.settings.magic_number
        return [position for position in positions if getattr(position, "magic", magic) == magic]

    def _margin_level(self, account) -> float | None:
        """MT5 reports 0 when no position is open, which is 'undefined', not 'below the minimum'."""
        value = getattr(account, "margin_level", None)
        try:
            level = float(value)
        except (TypeError, ValueError):
            return None
        return level if level > 0 else None

    def _exposure(self, positions: list, spec_cache: dict) -> float | None:
        """Notional exposure of bot-managed positions, or None when it cannot be verified."""
        total = 0.0
        for position in positions:
            name = str(getattr(position, "symbol", "") or "")
            if name not in spec_cache:
                spec_cache[name] = spec_from_symbol_info(self.gateway.symbol_info(name)) if name else None
            spec = spec_cache[name]
            if spec is None or spec.contract_size <= 0:
                logger.warning("exposure_unverifiable symbol=%s reason=missing_contract_size", name)
                return None
            price = getattr(position, "price_open", None)
            volume = getattr(position, "volume", None)
            if price is None or volume is None:
                logger.warning("exposure_unverifiable symbol=%s reason=missing_position_fields", name)
                return None
            total += abs(float(volume)) * float(spec.contract_size) * float(price)
        return total

    def _acquire_db_lock(self, db: Session, now: datetime) -> bool:
        row = db.scalar(select(SchedulerLockRecord).where(SchedulerLockRecord.lock_key == LOCK_KEY))
        if row is None:
            db.add(SchedulerLockRecord(lock_key=LOCK_KEY, owner=self.owner, acquired_at=now, heartbeat_at=now))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                logger.warning("scheduler_lock_race owner=%s", self.owner)
                return False
            return True
        if row.owner == self.owner:
            row.heartbeat_at = now
            db.commit()
            return True
        heartbeat = as_utc(row.heartbeat_at) or as_utc(row.acquired_at)
        if heartbeat is not None and (now - heartbeat) <= timedelta(seconds=self.settings.scheduler_lock_ttl_seconds):
            logger.warning("scheduler_lock_held owner=%s age_s=%.0f", row.owner, (now - heartbeat).total_seconds())
            return False
        logger.warning("scheduler_lock_taken_over stale_owner=%s", row.owner)
        row.owner, row.acquired_at, row.heartbeat_at = self.owner, now, now
        db.commit()
        return True

    def _release_db_lock(self, db: Session) -> None:
        try:
            row = db.scalar(select(SchedulerLockRecord).where(SchedulerLockRecord.lock_key == LOCK_KEY, SchedulerLockRecord.owner == self.owner))
            if row is not None:
                db.delete(row)
                db.commit()
        except Exception as error:
            db.rollback()
            logger.error("scheduler_lock_release_failed error=%s", type(error).__name__)
