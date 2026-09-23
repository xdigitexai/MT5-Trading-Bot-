"""The market loop must execute once per closed candle, never overlap, and fail closed."""
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import select

from app.core.clock import as_utc
from app.core.config import Settings
from app.core.schemas import Signal, SignalStatus
from app.database.base import AuditRecord, ExecutionGuardRecord, RiskStateRecord, SignalRecord, TradeRecord
from app.database.runtime import SchedulerLockRecord
from app.news.provider import NewsEvent, StaticNewsProvider
from app.risk.state import RiskStateStore
from app.services.scheduler import MarketScheduler
from conftest import FakeGateway, FakeTick, market_frames, market_gateway


def trading_settings(**overrides) -> Settings:
    """Narrow settings so one cycle only ever considers EURUSD.

    An unavailable news source is only tolerated when the policy does not require protection, so
    the default here states that policy explicitly; tests that need a stricter policy override it.

    The fixture models a demo account that can fund a 10 000 USD balance, so the dollar risk
    limits are scaled to it. The shipped hard limits (3 USD capital, 0.70 USD per trade, 1.40 USD
    per session, 3 trades, 1 position, a 0.01 lot cap) are exercised against real broker properties
    in tests/test_hard_limits.py.
    """
    values = {
        "symbols": ["EURUSD"], "news_fail_closed": False,
        "max_bot_capital_usd": 1_000.0, "max_loss_per_trade_usd": 50.0,
        "max_session_loss_usd": 200.0, "max_daily_trades": 3, "max_lots_per_position": 1.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def scheduler_for(settings, gateway, session_factory, **kwargs) -> MarketScheduler:
    return MarketScheduler(settings, gateway, session_factory, **kwargs)


@dataclass
class BlockingGateway(FakeGateway):
    """FakeGateway whose candle fetch blocks until the test releases it."""

    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def rates(self, symbol, timeframe, count):
        self.entered.set()
        self.release.wait(10)
        return super().rates(symbol, timeframe, count)


def rows(db, model):
    return list(db.scalars(select(model)))


def expire_lease(db, seconds: float = 10_000) -> None:
    """Age the market-loop lease past its TTL, exactly as an abandoned process leaves it."""
    row = db.scalar(select(SchedulerLockRecord))
    if row is not None:
        row.heartbeat_at = as_utc(row.heartbeat_at) - timedelta(seconds=seconds)
        db.commit()


def process_cycle(scheduler: MarketScheduler, now: datetime) -> dict:
    """One cycle as a standalone process would run it, releasing the lease on the way out.

    A test that builds several schedulers in a row is modelling several processes; each one has to
    give the loop back before the next may lead it.
    """
    try:
        return scheduler.run_once(now=now)
    finally:
        scheduler.stop()


def test_cycle_executes_one_risk_sized_order_and_persists_the_signal(settings, session_factory, db):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now)
    scheduler = scheduler_for(trading_settings(), gateway, session_factory)

    summary = scheduler.run_once(now=now)

    assert summary["executions"] == 1 and summary["signals"] == 1 and summary["blocked"] == []
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert (request["symbol"], request["type"], request["magic"]) == ("EURUSD", 0, 260825)

    signal_row = db.scalar(select(SignalRecord))
    assert signal_row.signal_id.startswith("sig-")
    assert (signal_row.symbol, signal_row.strategy, signal_row.timeframe, signal_row.direction) == ("EURUSD", "trend_following", "M15", "BUY")
    assert signal_row.score == 90 and signal_row.confidence == pytest.approx(0.9)
    assert signal_row.executed is True and signal_row.status == SignalStatus.EXECUTED.value
    assert signal_row.order_ticket == "55501" and signal_row.reason
    # created_at is the close of the candle the signal came from: the last completed M15 bar.
    assert as_utc(signal_row.created_at) == pd.Timestamp(now).floor("15min").to_pydatetime()

    trade = db.scalar(select(TradeRecord))
    assert trade.trade_id == signal_row.signal_id and trade.status == "EXECUTED"
    assert trade.volume == request["volume"] == pytest.approx(0.34)
    # The order is sent at the live ask, while the trade records the broker's reported fill price.
    assert request["price"] == pytest.approx(1.10005) and trade.executed_price == pytest.approx(1.1)
    assert trade.stop_loss == request["sl"] and trade.take_profit == request["tp"]
    # The strategy's geometry survives re-anchoring to the executable price, within one tick.
    risk = request["price"] - request["sl"]
    assert (request["tp"] - request["price"]) / risk == pytest.approx(2.0, abs=1e-2)

    guard = db.scalar(select(ExecutionGuardRecord))
    assert (guard.idempotency_key, guard.status, guard.signal_id) == (signal_row.signal_id, "EXECUTED", signal_row.signal_id)
    assert db.scalar(select(RiskStateRecord)).peak_equity == pytest.approx(10_000.0)

    assert (scheduler.cycles, scheduler.running) == (1, False)
    assert scheduler.last_scan_at == now and scheduler.last_signal_at == now and scheduler.last_execution_at == now
    assert scheduler.last_reconciliation_at == now and scheduler.last_error is None


def test_the_same_closed_candle_never_orders_twice_across_a_restart(settings, session_factory, db):
    now = datetime.now(timezone.utc)
    first_gateway = market_gateway(now)
    first = scheduler_for(trading_settings(), first_gateway, session_factory)
    assert first.run_once(now=now)["executions"] == 1
    # A restart is a clean stop plus a new process: the lease is released on the way out.
    assert first.stop() is True
    assert db.scalar(select(SchedulerLockRecord)) is None

    # A restart rebuilds every object: only the database remembers what happened.
    second_gateway = market_gateway(now)
    second = scheduler_for(trading_settings(), second_gateway, session_factory)
    summary = second.run_once(now=now)

    assert second_gateway.requests == []
    assert summary["executions"] == 0 and summary["duplicates"] == 1
    assert summary["symbols"]["EURUSD"]["candidates_detail"][0]["status"] == SignalStatus.DUPLICATE.value
    assert len(rows(db, TradeRecord)) == 1 and len(rows(db, SignalRecord)) == 1
    assert len(rows(db, ExecutionGuardRecord)) == 1

    # Even with the persisted signal removed, the order is still refused: the gate sees the trade
    # and the reserved idempotency key the first cycle left behind.
    second.stop()
    db.delete(db.scalar(select(SignalRecord)))
    db.commit()
    third_gateway = market_gateway(now)
    third = scheduler_for(trading_settings(), third_gateway, session_factory).run_once(now=now)
    assert third_gateway.requests == [] and third["executions"] == 0
    assert len(rows(db, TradeRecord)) == 1
    row = db.scalar(select(SignalRecord))
    assert row.status == SignalStatus.RISK_REJECTED.value and "no_duplicate" in (row.reason or "")


def test_overlapping_runs_are_impossible_within_one_process(settings, session_factory):
    now = datetime.now(timezone.utc)
    gateway = BlockingGateway(bar_frames=market_frames(now), tick_value=FakeTick(time=now.timestamp()))
    scheduler = scheduler_for(trading_settings(), gateway, session_factory)

    worker = threading.Thread(target=scheduler.run_once, kwargs={"now": now})
    worker.start()
    assert gateway.entered.wait(10)
    second = scheduler.run_once(now=now)
    assert second["skipped"] is True and "already running" in second["reason"]
    gateway.release.set()
    worker.join(10)

    assert scheduler.state.skipped_overlaps == 1
    assert len(gateway.requests) == 1


def test_a_second_scheduler_cannot_take_a_held_database_lock(settings, session_factory, db):
    now = datetime.now(timezone.utc)
    owner = scheduler_for(trading_settings(), market_gateway(now), session_factory)
    holder = db
    assert owner._acquire_db_lock(holder, now) is True

    challenger_gateway = market_gateway(now)
    challenger = scheduler_for(trading_settings(), challenger_gateway, session_factory)
    blocked = challenger.run_once(now=now)
    assert blocked["skipped"] is True and "lock" in blocked["reason"]
    assert blocked["leader"] is False and challenger.standby is True and challenger.leader is False
    assert challenger_gateway.requests == []
    assert challenger.state.cycles == 0  # a standby never scans a symbol at all

    owner._release_db_lock(holder)
    assert challenger.run_once(now=now)["executions"] == 1
    assert challenger.leader is True
    assert len(challenger_gateway.requests) == 1


def test_the_lease_is_held_across_cycles_and_released_only_on_a_deliberate_stop(settings, session_factory, db):
    """The lock is a lease, not a per-cycle mutex: leadership survives between cycles."""
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now)
    leader = scheduler_for(trading_settings(), gateway, session_factory)

    assert leader.run_once(now=now)["leader"] is True
    row = db.scalar(select(SchedulerLockRecord))
    assert row is not None and row.owner == leader.owner and as_utc(row.heartbeat_at) == now

    later = now + timedelta(seconds=60)
    assert leader.run_once(now=later)["leader"] is True
    db.refresh(row)
    assert as_utc(row.heartbeat_at) == later  # the heartbeat moved with the cycle
    assert leader.leader is True and leader.standby is False

    assert leader.stop() is True
    assert db.scalar(select(SchedulerLockRecord)) is None  # a clean stop frees the loop at once


def test_two_scheduler_instances_can_never_both_turn_a_candle_into_an_order(settings, session_factory, db):
    """Two independent schedulers, one database: only the leader may scan, and only one order exists."""
    now = datetime.now(timezone.utc)
    first_gateway = market_gateway(now)
    second_gateway = market_gateway(now)
    first = scheduler_for(trading_settings(), first_gateway, session_factory)
    second = scheduler_for(trading_settings(), second_gateway, session_factory)

    assert first.run_once(now=now)["executions"] == 1
    assert len(first_gateway.requests) == 1

    # The second process keeps running, but it is a standby: it never fetches candles, never
    # evaluates a strategy and never reaches the broker.
    standby = second.run_once(now=now)
    assert standby["skipped"] is True and standby["reason"] == "another scheduler owns the market loop lock"
    assert second_gateway.requests == [] and second_gateway.rates_calls == []
    assert len(rows(db, TradeRecord)) == 1 and len(rows(db, ExecutionGuardRecord)) == 1

    # Handing the loop over (the leader stopped) does not create a second order either: the closed
    # candle was already turned into a signal, so the successor sees a duplicate.
    assert first.stop() is True
    successor = second.run_once(now=now)
    assert successor["leader"] is True and successor["executions"] == 0 and successor["duplicates"] == 1
    assert second_gateway.requests == [] and len(rows(db, TradeRecord)) == 1


def test_a_stale_database_lock_is_taken_over_after_a_crash(settings, session_factory, db):
    now = datetime.now(timezone.utc)
    db.add(SchedulerLockRecord(lock_key="market-loop", owner="crashed-process", acquired_at=now - timedelta(hours=1), heartbeat_at=now - timedelta(hours=1)))
    db.commit()

    gateway = market_gateway(now)
    scheduler = scheduler_for(trading_settings(), gateway, session_factory)
    summary = scheduler.run_once(now=now)

    assert summary["skipped"] is False and summary["executions"] == 1
    row = db.scalar(select(SchedulerLockRecord))  # the lease moved to the new leader
    assert row is not None and row.owner == scheduler.owner and as_utc(row.heartbeat_at) == now


def test_fail_closed_when_mt5_is_not_connected(settings, session_factory, db):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now, connected=False)
    summary = scheduler_for(trading_settings(), gateway, session_factory).run_once(now=now)

    assert gateway.requests == [] and rows(db, SignalRecord) == [] and rows(db, TradeRecord) == []
    symbol = summary["symbols"]["EURUSD"]
    assert symbol["status"] == "BLOCKED" and "MT5 is not connected" in symbol["blocked"][0]
    assert summary["candidates"] == 0


def test_fail_closed_on_a_stale_tick_or_missing_candles(settings, session_factory, db):
    now = datetime.now(timezone.utc)
    stale_tick = FakeGateway(bar_frames=market_frames(now), tick_value=FakeTick(time=(now - timedelta(hours=5)).timestamp()))
    stale = process_cycle(scheduler_for(trading_settings(), stale_tick, session_factory), now)
    assert stale_tick.requests == [] and "old" in stale["symbols"]["EURUSD"]["blocked"][0]

    no_candles = FakeGateway(tick_value=FakeTick(time=now.timestamp()))
    empty = process_cycle(scheduler_for(trading_settings(), no_candles, session_factory), now)
    assert no_candles.requests == [] and "candles unavailable" in empty["symbols"]["EURUSD"]["blocked"][0]

    no_symbol = FakeGateway(symbols={}, bar_frames=market_frames(now), tick_value=FakeTick(time=now.timestamp()))
    missing = process_cycle(scheduler_for(trading_settings(), no_symbol, session_factory), now)
    assert no_symbol.requests == [] and "symbol specification" in missing["symbols"]["EURUSD"]["blocked"][0]

    no_account = market_gateway(now, account=None)
    unreadable = process_cycle(scheduler_for(trading_settings(), no_account, session_factory), now)
    assert no_account.requests == [] and "account information" in unreadable["symbols"]["EURUSD"]["blocked"][0]
    assert rows(db, SignalRecord) == []


def test_fail_closed_when_the_risk_state_cannot_be_read(settings, session_factory, db, monkeypatch):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now)
    scheduler = scheduler_for(trading_settings(), gateway, session_factory)

    def broken(self, day=None):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(RiskStateStore, "load", broken)
    summary = scheduler.run_once(now=now)

    assert gateway.requests == [] and rows(db, SignalRecord) == []
    assert summary["errors"] and "RuntimeError" in scheduler.last_error


def test_fail_closed_when_the_news_policy_requires_protection_that_is_unavailable(settings, session_factory, db):
    now = datetime.now(timezone.utc)
    strict = trading_settings(news_fail_closed=True)
    gateway = market_gateway(now)
    summary = scheduler_for(strict, gateway, session_factory).run_once(now=now)

    assert gateway.requests == [] and rows(db, SignalRecord) == []
    symbol = summary["symbols"]["EURUSD"]
    assert "news" in symbol["blocked"][0] and "unavailable" in symbol["blocked"][0]
    assert symbol["news"]["available"] is False


def test_a_configured_news_window_blocks_and_a_clear_window_allows_trading(settings, session_factory, db):
    now = datetime.now(timezone.utc)
    event = NewsEvent(when=now, currency="EUR", impact="high", title="ECB press conference")
    blocked_gateway = market_gateway(now)
    news = StaticNewsProvider([event], fail_closed=True, window_minutes=30)
    blocked = process_cycle(scheduler_for(trading_settings(), blocked_gateway, session_factory, news=news), now)
    assert blocked_gateway.requests == [] and "ECB press conference" in blocked["symbols"]["EURUSD"]["blocked"][0]

    clear_gateway = market_gateway(now)
    later = StaticNewsProvider([NewsEvent(when=now + timedelta(hours=6), currency="EUR", impact="high")], fail_closed=True, window_minutes=30)
    executed = process_cycle(scheduler_for(trading_settings(), clear_gateway, session_factory, news=later), now)
    assert executed["executions"] == 1 and len(clear_gateway.requests) == 1


def test_the_persisted_emergency_lock_stops_the_cycle(settings, session_factory, db):
    now = datetime.now(timezone.utc)
    RiskStateStore(db).set_emergency_locked(True)
    gateway = market_gateway(now)
    summary = scheduler_for(trading_settings(), gateway, session_factory).run_once(now=now)

    assert gateway.requests == [] and rows(db, SignalRecord) == []
    assert "emergency lock" in summary["blocked"][0]
    assert summary["symbols"] == {}


def test_the_still_forming_candle_is_never_passed_to_a_strategy(settings, session_factory):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now)
    seen = {}

    def recorder(symbol, h4, h1, m15, rr=2.0):
        seen["m15"] = m15.copy()
        seen["h4_rows"] = len(h4)
        return Signal(symbol=symbol, confidence=0, score=0, strategy="trend_following", timeframe="M15", reasons=["recorded"], timestamp=now)

    scheduler = scheduler_for(trading_settings(), gateway, session_factory, strategies={"trend_following": recorder})
    summary = scheduler.run_once(now=now)

    assert summary["candidates"] == 0 and gateway.requests == []
    raw = gateway.bar_frames["M15"]
    assert len(seen["m15"]) == len(raw) - 1
    assert seen["m15"]["time"].iloc[-1] + timedelta(minutes=15) <= raw["time"].iloc[-1]
    assert raw["time"].iloc[-1] not in set(seen["m15"]["time"])


def test_ensemble_decides_when_it_is_enabled(settings, session_factory, db):
    now = datetime.now(timezone.utc)
    quiet_gateway = market_gateway(now)
    quiet = scheduler_for(trading_settings(enabled_strategies=["trend_following", "ensemble"]), quiet_gateway, session_factory)
    assert process_cycle(quiet, now)["candidates"] == 0 and quiet_gateway.requests == []

    loud_gateway = market_gateway(now)
    loud = scheduler_for(trading_settings(enabled_strategies=["trend_following", "ensemble"], ensemble_min_votes=1), loud_gateway, session_factory)
    summary = process_cycle(loud, now)

    assert summary["executions"] == 1
    row = db.scalar(select(SignalRecord))
    assert row.strategy == "ensemble" and row.direction == "BUY" and row.executed is True


def test_a_signal_left_new_by_a_crash_is_retried_only_after_the_staleness_window(settings, session_factory, db, monkeypatch):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now)
    scheduler = scheduler_for(trading_settings(stale_guard_seconds=3600), gateway, session_factory)

    original = scheduler._levels

    def boom(signal, context):
        raise RuntimeError("simulated crash before the order was prepared")

    monkeypatch.setattr(scheduler, "_levels", boom)
    crashed = scheduler.run_once(now=now)
    assert crashed["errors"] and "RuntimeError" in scheduler.last_error
    assert len(rows(db, SignalRecord)) == 1 and db.scalar(select(SignalRecord)).status == SignalStatus.NEW.value
    assert gateway.requests == []

    # The crashed process is gone, so its lease has expired by the time the replacement starts.
    expire_lease(db)

    monkeypatch.setattr(scheduler, "_levels", original)
    fresh = scheduler.run_once(now=now)
    assert fresh["leader"] is True
    assert fresh["duplicates"] == 1 and fresh["executions"] == 0  # still inside the staleness window

    row = db.scalar(select(SignalRecord))
    row.created_at = now - timedelta(hours=2)
    db.commit()
    expire_lease(db)
    retried_gateway = market_gateway(now)
    retried = scheduler_for(trading_settings(stale_guard_seconds=3600), retried_gateway, session_factory).run_once(now=now)

    assert retried["executions"] == 1 and retried["signals"] == 0
    assert len(retried_gateway.requests) == 1 and len(rows(db, TradeRecord)) == 1


def test_start_and_stop_leave_no_thread_behind(settings, session_factory):
    scheduler = scheduler_for(trading_settings(symbols=[], scheduler_interval_seconds=1), FakeGateway(), session_factory)
    started, detail = scheduler.start()

    assert started is True and "started" in detail
    assert scheduler.running is True and scheduler.thread_alive is True
    assert scheduler.stop() is True
    assert scheduler.running is False and scheduler.thread_alive is False
    assert scheduler.stop() is True  # stopping twice is harmless


def test_an_empty_configured_universe_is_not_an_error(settings, session_factory):
    scheduler = scheduler_for(trading_settings(symbols=[]), FakeGateway(), session_factory)
    summary = scheduler.run_once(now=datetime.now(timezone.utc))
    assert summary["skipped"] is False and summary["symbols"] == {} and summary["executions"] == 0


def test_the_cycle_reconciles_on_its_own_schedule(settings, session_factory, db):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now)
    scheduler = scheduler_for(trading_settings(), gateway, session_factory)

    first = scheduler.run_once(now=now)
    assert first["reconciliation"] is not None and scheduler.last_reconciliation_at == now

    second = scheduler.run_once(now=now + timedelta(seconds=5))
    assert second["reconciliation"] is None  # not due yet

    third = scheduler.run_once(now=now + timedelta(seconds=settings.reconciliation_interval_seconds + 1))
    assert third["reconciliation"] is not None
    assert [event.event_type for event in rows(db, AuditRecord)].count("RECONCILIATION") == 2
