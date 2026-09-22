"""Execution must validate, fence duplicates and fail closed on any gateway problem."""
from sqlalchemy import select

from app.core.config import Settings
from app.core.schemas import SignalAction
from app.database.base import AuditRecord, ExecutionGuardRecord, SignalRecord, TradeRecord
from app.execution.service import ExecutionService
from conftest import FakeAccount, FakeGateway, FakeOrderResult, FakeSymbolInfo, make_intent, make_signal


def service(settings, gateway) -> ExecutionService:
    return ExecutionService(settings, gateway)


def rows(db, model):
    return list(db.scalars(select(model)))


def test_order_without_stop_loss_is_rejected(settings, gateway, db):
    result = service(settings, gateway).submit(db, make_intent(signal=make_signal(stop_loss=None, take_profit=None)))
    assert result.status == "REJECTED" and not result.accepted
    assert "stop loss is required and must be a positive price" in result.reasons
    assert gateway.requests == []
    assert result.record.status == "REJECTED"
    assert rows(db, ExecutionGuardRecord) == []  # a pre-send rejection does not burn the idempotency key
    assert rows(db, TradeRecord)[0].status == "REJECTED"


def test_stop_loss_on_the_wrong_side_is_rejected(settings, gateway, db):
    buy = service(settings, gateway).submit(db, make_intent(signal=make_signal(entry=1.1, stop_loss=1.11, take_profit=1.12)))
    assert buy.status == "REJECTED" and "BUY stop loss must be below the entry price" in buy.reasons
    sell = service(settings, gateway).submit(db, make_intent(signal=make_signal(action=SignalAction.SELL, entry=1.1, stop_loss=1.09, take_profit=1.08), trade_id="trade-2"))
    assert sell.status == "REJECTED" and "SELL stop loss must be above the entry price" in sell.reasons
    assert gateway.requests == []


def test_stop_loss_inside_the_broker_minimum_distance_is_rejected(settings, db):
    gateway = FakeGateway(symbols={"EURUSD": FakeSymbolInfo(trade_stops_level=20)})
    result = service(settings, gateway).submit(db, make_intent(signal=make_signal(entry=1.1, stop_loss=1.0999, take_profit=1.1002)))
    assert result.status == "REJECTED" and "stop loss is closer than the broker minimum stop distance" in result.reasons
    assert gateway.requests == []


def test_take_profit_violating_risk_reward_is_rejected(settings, gateway, db):
    result = service(settings, gateway).submit(db, make_intent(signal=make_signal(entry=1.1, stop_loss=1.09, take_profit=1.105)))
    assert result.status == "REJECTED"
    assert any("risk/reward" in reason for reason in result.reasons)
    assert gateway.requests == []


def test_take_profit_is_optional(settings, gateway, db):
    result = service(settings, gateway).submit(db, make_intent(signal=make_signal(take_profit=None)))
    assert result.status == "EXECUTED" and gateway.requests[0]["tp"] == 0.0


def test_successful_order_builds_the_request_and_persists_the_outcome(settings, gateway, db):
    db.add(SignalRecord(signal_id="sig-1", symbol="EURUSD", strategy="trend_following", timeframe="M15", direction="BUY", confidence=0.8, score=80, status="NEW"))
    db.commit()
    result = service(settings, gateway).submit(db, make_intent(signal=make_signal(), signal_id="sig-1"))

    assert result.status == "EXECUTED" and result.accepted and result.order_ticket == "55501"
    assert result.retcode == 10009
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request == {
        "action": 1, "symbol": "EURUSD", "volume": 0.05, "type": 0, "price": 1.1, "sl": 1.09, "tp": 1.12,
        "deviation": settings.order_deviation_points, "magic": settings.magic_number,
        "comment": "bot-trade-1", "type_time": 0, "type_filling": 0,
    }

    trade = rows(db, TradeRecord)[0]
    assert (trade.status, trade.mt5_order_ticket, trade.mt5_position_ticket, trade.executed_price, trade.volume) == ("EXECUTED", "55501", "55502", 1.1, 0.05)
    assert trade.open_time is not None and trade.stop_loss == 1.09
    signal_row = db.scalar(select(SignalRecord).where(SignalRecord.signal_id == "sig-1"))
    assert (signal_row.executed, signal_row.status, signal_row.order_ticket) == (True, "EXECUTED", "55501")
    assert db.scalar(select(ExecutionGuardRecord)).status == "EXECUTED"
    assert [event.event_type for event in rows(db, AuditRecord)] == ["TRADE_SUBMITTED"]


def test_sell_order_uses_the_sell_type(settings, gateway, db):
    signal = make_signal(action=SignalAction.SELL, entry=1.1, stop_loss=1.11, take_profit=1.08)
    result = service(settings, gateway).submit(db, make_intent(signal=signal, price=1.1))
    assert result.status == "EXECUTED"
    assert gateway.requests[0]["type"] == 1 and gateway.requests[0]["sl"] == 1.11


def test_duplicate_idempotency_key_does_not_send_a_second_order(settings, gateway, db):
    execution = service(settings, gateway)
    first = execution.submit(db, make_intent(trade_id="trade-1", key="key-1"))
    second = execution.submit(db, make_intent(trade_id="trade-2", key="key-1"))
    third = execution.submit(db, make_intent(trade_id="trade-1", key="key-1"))
    assert first.status == "EXECUTED"
    assert second.status == "DUPLICATE" and "duplicate idempotency key" in second.reasons
    assert third.status == "DUPLICATE" and "duplicate trade_id" in third.reasons
    assert len(gateway.requests) == 1
    assert len(rows(db, TradeRecord)) == 1


def test_derived_idempotency_key_blocks_a_repeat_of_the_same_signal(settings, gateway, db):
    execution = service(settings, gateway)
    signal = make_signal()
    assert execution.submit(db, make_intent(signal=signal, trade_id="trade-1", key="")).status == "EXECUTED"
    repeat = execution.submit(db, make_intent(signal=signal, trade_id="trade-2", key=""))
    assert repeat.status == "DUPLICATE" and len(gateway.requests) == 1


def test_gateway_returning_nothing_fails_closed_and_is_recorded(settings, db):
    gateway = FakeGateway(result=None)
    execution = service(settings, gateway)
    result = execution.submit(db, make_intent())
    assert result.status == "FAILED" and not result.accepted
    assert "order state unknown" in result.reason
    assert len(gateway.requests) == 1
    assert rows(db, TradeRecord)[0].status == "FAILED"
    assert db.scalar(select(ExecutionGuardRecord)).status == "FAILED"
    assert rows(db, AuditRecord)[0].event_type == "TRADE_FAILED"
    # Never retried blindly: the same key can no longer produce a second order.
    assert execution.submit(db, make_intent(trade_id="trade-2")).status == "DUPLICATE"
    assert len(gateway.requests) == 1


def test_gateway_exception_fails_closed(settings, db):
    gateway = FakeGateway(error=RuntimeError("terminal lost"))
    result = service(settings, gateway).submit(db, make_intent())
    assert result.status == "FAILED" and "RuntimeError" in result.reason
    assert rows(db, TradeRecord)[0].status == "FAILED"


def test_broker_rejection_is_recorded_with_the_retcode(settings, db):
    gateway = FakeGateway(result=FakeOrderResult(retcode=10016, comment="invalid stops"))
    result = service(settings, gateway).submit(db, make_intent())
    assert result.status == "REJECTED" and "10016" in result.reason
    assert rows(db, TradeRecord)[0].status == "REJECTED"


def test_pending_order_is_recorded_as_submitted(settings, db):
    gateway = FakeGateway(result=FakeOrderResult(retcode=10008))
    assert service(settings, gateway).submit(db, make_intent()).status == "SUBMITTED"


def test_preflight_blocks_without_reaching_the_broker(db, gateway):
    paper = Settings(_env_file=None, trading_mode="paper")
    result = service(paper, gateway).submit(db, make_intent())
    assert result.status == "REJECTED" and "non-MT5 mode blocked broker execution" in result.reasons

    disconnected = FakeGateway(connected=False)
    result = service(Settings(_env_file=None), disconnected).submit(db, make_intent(trade_id="trade-2"))
    assert result.status == "REJECTED" and any("MT5 unavailable" in reason for reason in result.reasons)

    unknown_symbol = FakeGateway(symbols={"EURUSD": FakeSymbolInfo()})
    result = service(Settings(_env_file=None), unknown_symbol).submit(db, make_intent(signal=make_signal(symbol="GBPUSD"), trade_id="trade-3"))
    assert result.status == "REJECTED" and "symbol specification unavailable" in result.reasons

    unlisted = FakeGateway()
    result = service(Settings(_env_file=None), unlisted).submit(db, make_intent(signal=make_signal(symbol="XAUUSD"), trade_id="trade-4"))
    assert result.status == "REJECTED" and any("not in the configured trading universe" in reason for reason in result.reasons)
    assert gateway.requests == unlisted.requests == unknown_symbol.requests == disconnected.requests == []


def test_volume_and_margin_fail_closed(settings, gateway, db):
    result = service(settings, gateway).submit(db, make_intent(volume=0.005))
    assert result.status == "REJECTED" and "volume is invalid or below the broker minimum" in result.reasons

    poor = FakeGateway(account=FakeAccount(margin_free=10.0))
    result = service(settings, poor).submit(db, make_intent(trade_id="trade-2"))
    assert result.status == "REJECTED" and "insufficient free margin" in result.reasons
    assert poor.requests == []
