"""Continuous trading bot lifecycle.

Runs a 5-second cycle that:
  - manages open positions every cycle
  - evaluates new entries only on a newly closed M15 candle
  - routes every candidate through RiskEngine before ExecutionService
"""
from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import pandas as pd

from app.core.config import Settings, TradingMode
from app.core.schemas import Signal, SignalAction, TradeIntent
from app.execution.service import ExecutionService
from app.mt5.gateway import MT5Gateway, mt5
from app.portfolio.manager import PositionManager
from app.risk.engine import RiskEngine, SymbolSpec
from app.strategies import ensemble, trend_following
from app.news.provider import NewsFilter

log = logging.getLogger("bot")

TIMEFRAME_M15 = 15
TIMEFRAME_H1 = 16385
TIMEFRAME_H4 = 16388
if mt5 is not None:
    TIMEFRAME_M15 = mt5.TIMEFRAME_M15
    TIMEFRAME_H1 = mt5.TIMEFRAME_H1
    TIMEFRAME_H4 = mt5.TIMEFRAME_H4


@dataclass
class BotState:
    running: bool = False
    emergency_locked: bool = False
    shutdown_requested: bool = False


class BotService:
    def __init__(self, settings: Settings, gateway: MT5Gateway):
        self.settings = settings
        self.gateway = gateway
        self.state = BotState()
        self.risk = RiskEngine(settings)
        self.execution = ExecutionService(settings, gateway)
        self.position_manager = PositionManager(gateway)
        self.news = NewsFilter(fail_closed=settings.news_fail_closed)
        self._last_candle: dict[str, int] = {}
        self._resolved_symbols: dict[str, str] = {}

    def start(self) -> tuple[bool, str]:
        if self.state.emergency_locked:
            return False, "emergency reset required"
        if self.settings.trading_mode is TradingMode.LIVE and not self.settings.live_trading_enabled:
            return False, "LIVE trading refused: LIVE_TRADING_ENABLED=false"
        if self.settings.trading_mode is TradingMode.LIVE and self.settings.live_trading_enabled:
            return False, "LIVE trading refused by hard safety guard in this build"

        health = self.gateway.initialize()
        if not health.connected:
            return False, health.detail
        login = self.gateway.login()
        if not login.connected:
            self.gateway.shutdown()
            return False, login.detail

        for canonical in self.settings.symbols:
            resolved = self.gateway.discover_symbol(canonical)
            if resolved:
                self._resolved_symbols[canonical] = resolved
            else:
                log.warning("Symbol %s not found on broker", canonical)

        self.state.running = True
        self.state.shutdown_requested = False
        return True, "started after MT5 validation"

    def stop(self) -> None:
        self.state.running = False
        self.state.shutdown_requested = True

    def emergency_stop(self) -> None:
        self.state.running = False
        self.state.emergency_locked = True
        self.state.shutdown_requested = True

    def emergency_reset(self) -> None:
        self.state.emergency_locked = False

    def run_forever(self, cycle_seconds: float = 5.0) -> None:
        print("MT5: CONNECTING...", flush=True)
        ok, detail = self.start()
        if not ok:
            log.error("Bot refused to start: %s", detail)
            print(f"MT5: FAILED — {detail}", flush=True)
            print(f"BOT REFUSED TO START: {detail}", flush=True)
            raise SystemExit(1)

        print("MT5: CONNECTED", flush=True)
        print("Database: using configured DATABASE_URL", flush=True)
        print("BotService: STARTED", flush=True)
        print("Scanner: RUNNING", flush=True)
        print("Position manager: RUNNING", flush=True)
        print(f"Demo volume cap: {getattr(self.settings, 'max_demo_volume', 0.10)}", flush=True)
        print("=" * 40, flush=True)
        log.info("Bot entered continuous loop (cycle=%.1fs)", cycle_seconds)

        def _handle_sigint(signum, frame):
            log.info("Shutdown requested (signal %s)", signum)
            print("\nShutdown requested", flush=True)
            self.state.shutdown_requested = True

        prev_handler = signal.signal(signal.SIGINT, _handle_sigint)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handle_sigint)

        try:
            while not self.state.shutdown_requested:
                cycle_start = time.monotonic()
                try:
                    self.run_cycle()
                except Exception:
                    log.exception("Bot cycle failed")
                elapsed = time.monotonic() - cycle_start
                sleep_for = max(0.0, cycle_seconds - elapsed)
                end = time.monotonic() + sleep_for
                while time.monotonic() < end and not self.state.shutdown_requested:
                    time.sleep(min(0.2, end - time.monotonic()))
        finally:
            print("Stopping bot...", flush=True)
            print("Disconnecting MT5...", flush=True)
            self.state.running = False
            try:
                self.gateway.shutdown()
            except Exception:
                log.exception("MT5 shutdown error")
            signal.signal(signal.SIGINT, prev_handler)
            print("Bot stopped cleanly", flush=True)
            log.info("Bot stopped cleanly")

    def run_cycle(self) -> None:
        if not self.gateway.health().connected:
            log.error("MT5 disconnected — attempting reconnect")
            health = self.gateway.initialize()
            if health.connected:
                login = self.gateway.login()
                if login.connected:
                    log.info("MT5 reconnected")
                else:
                    log.error("MT5 login failed after reconnect: %s", login.detail)
                    self._heartbeat(0)
                    return
            else:
                log.error("MT5 reconnect failed: %s", health.detail)
                self._heartbeat(0)
                return

        positions = list(self.gateway.positions() or [])
        bot_positions = [p for p in positions if getattr(p, "magic", 0) == self.settings.magic_number]
        self._manage_positions(bot_positions)
        self._heartbeat(len(bot_positions))

        for canonical, broker_symbol in self._resolved_symbols.items():
            self._evaluate_symbol(canonical, broker_symbol, bot_positions)

    def _heartbeat(self, n_positions: int) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"[{now}] heartbeat | positions={n_positions}", flush=True)

    def _manage_positions(self, positions: list) -> None:
        for pos in positions:
            try:
                ticket = int(pos.ticket)
                side = "BUY" if pos.type == 0 else "SELL"
                entry = float(pos.price_open)
                volume = float(pos.volume)
                sl = float(pos.sl or 0)
                tp = float(pos.tp or 0)
                profit = float(getattr(pos, "profit", 0) or 0)
                symbol = pos.symbol
                tick = self.gateway.tick(symbol)
                if tick is not None:
                    current = float(tick.bid if pos.type == 0 else tick.ask)
                else:
                    current = float(getattr(pos, "price_current", 0) or entry)
                sign = "+" if profit >= 0 else ""
                print(
                    f"POSITION STATUS\n"
                    f"ticket={ticket}\n"
                    f"symbol={symbol}\n"
                    f"side={side}\n"
                    f"volume={volume}\n"
                    f"entry={entry}\n"
                    f"current={current}\n"
                    f"SL={sl}\n"
                    f"TP={tp}\n"
                    f"profit={sign}{profit:.2f}",
                    flush=True,
                )
                if tick is None:
                    continue
                price = current
                rates = self.gateway.rates(symbol, TIMEFRAME_M15, 50)
                if rates is None or len(rates) < 20:
                    continue
                df = self._rates_to_df(rates)
                completed = df.iloc[:-1] if len(df) > 1 else df
                from app.indicators.technical import atr
                atr_val = float(atr(completed).iloc[-1]) if len(completed) >= 14 else 0.0
                info = mt5.symbol_info(symbol) if mt5 else None
                point = float(info.point) if info else 0.00001
                self.position_manager.manage(pos, price, atr_val, point)
            except Exception:
                log.exception("Position manage failed for ticket %s", getattr(pos, "ticket", "?"))

    def _evaluate_symbol(self, canonical: str, broker_symbol: str, bot_positions: list) -> None:
        rates_m15 = self.gateway.rates(broker_symbol, TIMEFRAME_M15, 250)
        rates_h1 = self.gateway.rates(broker_symbol, TIMEFRAME_H1, 250)
        rates_h4 = self.gateway.rates(broker_symbol, TIMEFRAME_H4, 250)
        if rates_m15 is None or rates_h1 is None or rates_h4 is None:
            return
        if len(rates_m15) < 30 or len(rates_h1) < 30 or len(rates_h4) < 30:
            return

        m15 = self._rates_to_df(rates_m15)
        h1 = self._rates_to_df(rates_h1)
        h4 = self._rates_to_df(rates_h4)

        m15_closed = m15.iloc[:-1]
        h1_closed = h1.iloc[:-1]
        h4_closed = h4.iloc[:-1]
        if len(m15_closed) < 30:
            return

        closed_ts = int(m15_closed.iloc[-1]["time"])
        forming_ts = int(m15.iloc[-1]["time"])
        closed_dt = datetime.fromtimestamp(closed_ts, tz=timezone.utc)
        forming_dt = datetime.fromtimestamp(forming_ts, tz=timezone.utc)

        last = self._last_candle.get(canonical)
        if last == closed_ts:
            return

        self._last_candle[canonical] = closed_ts
        print(f"[{datetime.now().strftime('%H:%M:%S')}] NEW M15 CANDLE", flush=True)
        print(
            f"{canonical} | closed={closed_dt.strftime('%Y-%m-%d %H:%M')} | "
            f"forming={forming_dt.strftime('%Y-%m-%d %H:%M')}",
            flush=True,
        )

        symbol_positions = [p for p in bot_positions if p.symbol == broker_symbol]
        if symbol_positions:
            print(f"NO ACTION {canonical} | reason=existing_position", flush=True)
            return

        try:
            signal = trend_following.evaluate(broker_symbol, h4_closed, h1_closed, m15_closed)
            signal = ensemble.combine([signal])
        except Exception:
            log.exception("Strategy evaluation failed for %s", canonical)
            return

        if signal.action == SignalAction.HOLD or signal.score < self.settings.min_signal_score:
            print(f"NO ACTION {canonical}", flush=True)
            return

        print(f"SIGNAL {canonical} {signal.action} score={signal.score} strategy={signal.strategy}", flush=True)

        account = self.gateway.account_info()
        if account is None:
            print(f"REJECT {canonical} {signal.action} | reason=account_unavailable", flush=True)
            return

        equity = float(account.equity)
        balance = float(account.balance)
        daily_pnl = float(getattr(account, "profit", 0) or 0)
        balance_peak = max(balance, equity)

        tick = self.gateway.tick(broker_symbol)
        if tick is None:
            print(f"REJECT {canonical} {signal.action} | reason=invalid_tick", flush=True)
            return
        info = mt5.symbol_info(broker_symbol) if mt5 else None
        if info is None:
            print(f"REJECT {canonical} {signal.action} | reason=symbol_invalid", flush=True)
            return
        spread_points = (tick.ask - tick.bid) / info.point if info.point else 999

        spec = SymbolSpec(
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            tick_value=float(info.trade_tick_value),
            tick_size=float(info.trade_tick_size),
            point=float(info.point),
            trade_stops_level=int(info.trade_stops_level),
        )

        open_positions = len(bot_positions)
        symbol_pos_count = len(symbol_positions)
        news_clear = self.news.can_trade(canonical)

        decision = self.risk.approve(
            signal,
            equity=equity,
            balance_peak=balance_peak,
            daily_pnl=daily_pnl,
            open_positions=open_positions,
            symbol_positions=symbol_pos_count,
            spread_points=spread_points,
            spec=spec,
            trading_enabled=self.state.running and not self.state.emergency_locked,
            news_clear=news_clear,
            account_available=True,
            symbol_valid=True,
            market_open=True,
            tick_valid=True,
            margin_sufficient=True,
            duplicate_exists=False,
            correlation_allowed=True,
        )

        audit = self.risk.size_audit(equity, signal.entry or 0, signal.stop_loss or 0, spec)
        max_demo = float(getattr(self.settings, "max_demo_volume", 0.10) or 0.10)
        volume_cap_check = "FAIL" if (
            audit.reject_reason == "demo_volume_cap"
            or audit.raw_volume > max_demo
            or (audit.normalized_volume > max_demo)
        ) else "PASS"

        mt5_loss_at_sl = None
        try:
            if mt5 is not None and signal.entry and signal.stop_loss:
                side = mt5.ORDER_TYPE_BUY if signal.action == SignalAction.BUY else mt5.ORDER_TYPE_SELL
                calc1 = mt5.order_calc_profit(
                    side, broker_symbol, 1.0, float(signal.entry), float(signal.stop_loss)
                )
                if calc1 is not None:
                    mt5_loss_at_sl = abs(float(calc1)) * max(audit.raw_volume, 0.0)
        except Exception as exc:
            log.debug("order_calc_profit diagnostic failed: %s", exc)

        print(
            f"RISK AUDIT\n"
            f"symbol={canonical}\n"
            f"equity={equity:.2f}\n"
            f"balance={balance:.2f}\n"
            f"risk_pct={audit.risk_pct:.2f}%\n"
            f"risk_amount={audit.risk_amount:.4f}\n"
            f"entry={audit.entry}\n"
            f"SL={audit.stop}\n"
            f"SL_distance_price={audit.sl_distance}\n"
            f"tick_size={audit.tick_size}\n"
            f"tick_value={audit.tick_value}\n"
            f"volume_min={spec.volume_min}\n"
            f"volume_max={spec.volume_max}\n"
            f"volume_step={spec.volume_step}\n"
            f"calculated_volume={audit.raw_volume:.6f}\n"
            f"normalized_volume={audit.normalized_volume}\n"
            f"estimated_loss_at_SL={audit.estimated_loss_at_sl:.4f}\n"
            f"mt5_scaled_loss_at_SL={mt5_loss_at_sl}\n"
            f"demo_volume_cap={max_demo}\n"
            f"risk_check={audit.risk_check}\n"
            f"volume_cap_check={volume_cap_check}"
            + (f"\nreject_reason={audit.reject_reason}" if audit.reject_reason else ""),
            flush=True,
        )

        if not decision.approved:
            reason = decision.reasons[0] if decision.reasons else "risk_rejected"
            print(f"REJECT {canonical} {signal.action} | reason={reason}", flush=True)
            return

        if audit.risk_check != "PASS" or not decision.volume or decision.volume <= 0:
            reason = audit.reject_reason or "position_size_exceeds_risk"
            print(f"REJECT {canonical} {signal.action} | reason={reason}", flush=True)
            return

        volume = decision.volume
        price = float(tick.ask if signal.action == SignalAction.BUY else tick.bid)
        trade_id = str(uuid4())
        intent = TradeIntent(
            trade_id=trade_id,
            signal=signal.model_copy(update={"symbol": broker_symbol}),
            volume=volume,
            requested_price=price,
            idempotency_key=trade_id,
        )

        db = None
        try:
            from app.database.session import _ensure_engine
            Session = _ensure_engine()
            db = Session()
        except Exception as _db_exc:
            print(
                f"EXECUTION BLOCKED reason=database_idempotency_check_failed error={_db_exc}",
                flush=True,
            )
            print(f"REJECT {canonical} {signal.action} | reason=database_idempotency_check_failed", flush=True)
            return

        try:
            record = self.execution.submit(db, intent)
            status = getattr(record, "status", "REJECTED")
            ticket = getattr(record, "mt5_order_ticket", None) or getattr(record, "mt5_position_ticket", None)
            retcode = getattr(record, "retcode", None)
            comment = getattr(record, "comment", None) or getattr(record, "detail", "")
            if status in ("EXECUTED", "SUBMITTED"):
                print(
                    f"ORDER RESULT status={status} retcode={retcode} ticket={ticket} "
                    f"volume={volume} entry={price} sl={signal.stop_loss} tp={signal.take_profit}",
                    flush=True,
                )
            else:
                print(
                    f"REJECT {canonical} {signal.action} | reason=execution_{status} "
                    f"retcode={retcode} comment={comment}",
                    flush=True,
                )
        except Exception:
            log.exception("Execution failed for %s", canonical)
            print(f"REJECT {canonical} {signal.action} | reason=execution_error", flush=True)
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    @staticmethod
    def _rates_to_df(rates) -> pd.DataFrame:
        df = pd.DataFrame(rates)
        if "time" in df.columns:
            df["time"] = df["time"].astype(int)
        return df
