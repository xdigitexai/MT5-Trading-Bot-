"""MT5 is the source of truth for what a trade actually did; reconciliation must say so.

Every broker record below is a plain dataclass, so the whole reconciliation path runs without a
terminal, and the assertions pin the exact mapping from broker deals onto the local row.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.clock import utcnow
from app.database.base import AuditRecord, ExecutionGuardRecord, SignalRecord, TradeRecord
from app.risk.state import RiskStateStore
from app.services.reconciliation import Reconciler, order_comment
from conftest import FakeDeal, FakeGateway


def trade(db, **overrides) -> TradeRecord:
    values = {
        "trade_id": "t1", "symbol": "EURUSD", "side": "BUY", "volume": 0.05, "status": "EXECUTED",
        "stop_loss": 1.09, "take_profit": 1.12, "signal_score": 80, "strategy": "trend_following",
        "requested_price": 1.1, "executed_price": 1.1, "mt5_order_ticket": "55501",
    }
    values.update(overrides)
    row = TradeRecord(**values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def closed_deals(now: datetime, volume: float = 0.05, entry_price: float = 1.1, exit_price: float = 1.105, profit: float = 25.0, close_volume: float | None = None):
    opened, closed_at = now - timedelta(hours=2), now - timedelta(hours=1)
    return (
        FakeDeal(ticket=9001, order=7001, position_id=7001, entry=0, volume=volume, price=entry_price, commission=-1.0, swap=0.0, time=opened.timestamp(), comment="bot-t1"),
        FakeDeal(ticket=9002, order=7002, position_id=7001, entry=1, type=1, volume=volume if close_volume is None else close_volume, price=exit_price, profit=profit, commission=-1.0, swap=-0.5, reason=5, time=closed_at.timestamp(), comment="bot-t1"),
    )


def test_a_closed_broker_position_overwrites_the_local_row_and_realizes_pnl(db):
    now = utcnow()
    deals = closed_deals(now)
    engine = Reconciler(Settings(_env_file=None), FakeGateway(deals=deals))
    row = trade(db, mt5_position_ticket="9001", open_time=now - timedelta(hours=2))

    summary = engine.run(db, now=now)

    assert summary.matched == 1 and summary.matched_by_ticket == 1 and summary.updated == 1
    assert summary.errors == [] and summary.unmatched_broker == 0 and summary.unmatched_local == 0
    db.refresh(row)
    assert row.status == "CLOSED"
    assert row.executed_price == pytest.approx(1.1) and row.exit_price == pytest.approx(1.105)
    assert row.volume_closed == pytest.approx(0.05)
    assert row.profit == pytest.approx(25.0) and row.commission == pytest.approx(-2.0) and row.swap == pytest.approx(-0.5)
    assert row.close_reason == "take_profit"
    assert row.mt5_deal_ticket == "9002" and row.mt5_position_ticket == "7001"
    assert row.close_time is not None and row.open_time is not None
    assert row.reconciled_at is not None
    # Net realized P/L (profit + commission + swap) is booked on the day the position closed.
    assert summary.pnl_applied == pytest.approx(22.5)
    assert RiskStateStore(db).daily_pnl(row.close_time.date()) == pytest.approx(22.5)


def test_reconciliation_is_idempotent_and_never_double_counts_pnl(db):
    now = utcnow()
    engine = Reconciler(Settings(_env_file=None), FakeGateway(deals=closed_deals(now)))
    row = trade(db, mt5_position_ticket="9001")

    first = engine.run(db, now=now)
    db.refresh(row)
    stamped, balance = row.reconciled_at, RiskStateStore(db).daily_pnl(row.close_time.date())
    second = engine.run(db, now=now)
    db.refresh(row)

    assert first.updated == 1 and first.pnl_applied == pytest.approx(22.5)
    assert second.updated == 0 and second.unchanged == 1 and second.matched == 1
    assert second.pnl_applied == 0.0
    assert balance == pytest.approx(22.5)
    assert RiskStateStore(db).daily_pnl(row.close_time.date()) == pytest.approx(22.5)
    assert row.reconciled_at == stamped


def test_a_trade_without_tickets_is_matched_by_symbol_time_and_volume(db):
    now = utcnow()
    engine = Reconciler(Settings(_env_file=None), FakeGateway(deals=closed_deals(now)))
    row = trade(db, status="FAILED", created_at=now - timedelta(hours=2), mt5_order_ticket=None)

    summary = engine.run(db, now=now)

    db.refresh(row)
    assert summary.matched_by_fallback == 1 and summary.matched_by_ticket == 0
    assert row.status == "CLOSED" and row.exit_price == pytest.approx(1.105)


def test_a_partial_close_is_recorded_as_partially_closed(db):
    now = utcnow()
    engine = Reconciler(Settings(_env_file=None), FakeGateway(deals=closed_deals(now, volume=0.10, close_volume=0.05, profit=12.0)))
    row = trade(db, volume=0.10, mt5_position_ticket="9001")

    summary = engine.run(db, now=now)

    db.refresh(row)
    assert row.status == "PARTIALLY_CLOSED"
    assert row.volume_closed == pytest.approx(0.05) and row.profit == pytest.approx(12.0)
    assert summary.pnl_applied == pytest.approx(12.0 - 2.0 - 0.5)


def test_unmatched_records_are_reported_and_never_invented(db):
    now = utcnow()
    foreign = FakeDeal(ticket=9101, order=7101, position_id=7101, magic=0, comment="manual trade")
    unknown_broker = FakeDeal(ticket=9201, order=7201, position_id=7201, symbol="GBPUSD", volume=0.02, comment="bot-other", time=(now - timedelta(hours=3)).timestamp())
    engine = Reconciler(Settings(_env_file=None), FakeGateway(deals=(foreign, unknown_broker)))
    local = trade(db, trade_id="local-1", mt5_position_ticket="9901")

    summary = engine.run(db, now=now)

    assert summary.ignored_foreign == 1
    assert summary.unmatched_broker == 1 and "7201" in summary.details[-1]
    assert summary.unmatched_local == 1 and "local-1" in summary.details[0]
    assert len(list(db.scalars(select(TradeRecord)))) == 1  # no row is invented for a foreign position
    db.refresh(local)
    assert local.status == "EXECUTED" and local.profit is None


def test_history_unavailable_fails_closed_and_touches_nothing(db):
    now = utcnow()
    stale_guard = ExecutionGuardRecord(idempotency_key="key-1", signal_id="sig-1", symbol="EURUSD", side="BUY", volume=0.05, status="PENDING", created_at=now - timedelta(hours=2))
    db.add(stale_guard)
    db.commit()
    engine = Reconciler(Settings(_env_file=None), FakeGateway(history_fails=True))

    summary = engine.run(db, now=now)

    db.refresh(stale_guard)
    assert summary.errors and "history is unavailable" in summary.errors[0]
    assert summary.pnl_applied == 0.0 and summary.updated == 0
    assert stale_guard.status == "PENDING"


def test_open_orders_or_positions_being_unreadable_keeps_the_guards(db):
    now = utcnow()
    guard = ExecutionGuardRecord(idempotency_key="key-1", signal_id="sig-1", symbol="EURUSD", side="BUY", volume=0.05, status="PENDING", created_at=now - timedelta(hours=2))
    db.add(guard)
    db.commit()
    gateway = FakeGateway()
    gateway.orders = lambda: None
    engine = Reconciler(Settings(_env_file=None), gateway)

    summary = engine.run(db, now=now)

    db.refresh(guard)
    assert summary.errors and "left untouched" in summary.errors[0]
    assert guard.status == "PENDING" and summary.guards_retired == 0


def test_a_stale_pending_guard_is_retired_only_when_the_broker_has_no_order(db):
    now = utcnow()
    signal_id = "sig-abcdefghijklmnopqrstuvwx"
    stale = ExecutionGuardRecord(idempotency_key=signal_id, signal_id=signal_id, symbol="EURUSD", side="BUY", volume=0.05, status="PENDING", created_at=now - timedelta(hours=2))
    fresh = ExecutionGuardRecord(idempotency_key="fresh-key", signal_id="sig-fresh", symbol="EURUSD", side="BUY", volume=0.05, status="PENDING", created_at=now)
    db.add_all([stale, fresh])
    db.add(SignalRecord(signal_id=signal_id, symbol="EURUSD", strategy="trend_following", timeframe="M15", direction="BUY", confidence=0.9, score=90, status="NEW"))
    db.commit()
    engine = Reconciler(Settings(_env_file=None), FakeGateway())

    summary = engine.run(db, now=now)

    db.refresh(stale)
    db.refresh(fresh)
    assert summary.guards_retired == 1 and summary.guards_kept == 0
    assert stale.status == "RETIRED" and stale.signal_id == signal_id
    assert fresh.status == "PENDING"  # nothing inside the staleness window is touched
    assert db.scalar(select(SignalRecord)).status == "ABANDONED"
    assert [event.event_type for event in db.scalars(select(AuditRecord))].count("GUARD_RETIRED") == 1


def test_a_stale_pending_guard_is_kept_when_the_broker_shows_an_order(db):
    now = utcnow()
    signal_id = "sig-abcdefghijklmnopqrstuvwx"
    guard = ExecutionGuardRecord(idempotency_key=signal_id, signal_id=signal_id, symbol="EURUSD", side="BUY", volume=0.05, status="PENDING", created_at=now - timedelta(hours=2))
    db.add(guard)
    db.commit()
    evidence = FakeDeal(ticket=9301, order=7301, position_id=7301, comment=order_comment(signal_id), time=(now - timedelta(hours=2)).timestamp())
    engine = Reconciler(Settings(_env_file=None), FakeGateway(deals=(evidence,)))

    summary = engine.run(db, now=now)

    db.refresh(guard)
    assert summary.guards_retired == 0 and summary.guards_kept == 1
    assert guard.status == "PENDING" and guard.order_ticket == "9301"


def test_a_foreign_deal_never_matches_a_local_trade(db):
    now = utcnow()
    foreign = tuple(FakeDeal(ticket=ticket, order=order, position_id=7001, magic=0, entry=entry, volume=0.05, price=1.1, time=(now - timedelta(hours=hours)).timestamp()) for ticket, order, entry, hours in ((9001, 7001, 0, 2), (9002, 7002, 1, 1)))
    engine = Reconciler(Settings(_env_file=None), FakeGateway(deals=foreign))
    row = trade(db, mt5_position_ticket="9001")

    summary = engine.run(db, now=now)

    db.refresh(row)
    assert summary.ignored_foreign == 2 and summary.matched == 0
    assert summary.unmatched_local == 1
    assert row.status == "EXECUTED" and row.profit is None and row.exit_price is None


def test_a_stale_pending_guard_with_a_local_trade_is_confirmed(db):
    now = utcnow()
    guard = ExecutionGuardRecord(idempotency_key="key-1", signal_id="t1", symbol="EURUSD", side="BUY", volume=0.05, status="PENDING", created_at=now - timedelta(hours=2))
    db.add(guard)
    db.commit()
    trade(db, trade_id="t1", mt5_order_ticket="55501")
    engine = Reconciler(Settings(_env_file=None), FakeGateway())

    summary = engine.run(db, now=now)

    db.refresh(guard)
    assert summary.guards_kept == 1 and guard.status == "EXECUTED" and guard.order_ticket == "55501"
    assert summary.guards_retired == 0
