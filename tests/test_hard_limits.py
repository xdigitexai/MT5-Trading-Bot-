"""The hard server-side limits: 3 USD capital, 0.10 USD per trade, 0.30 USD per session, 3 trades, 1 position.

These tests use the *shipped* defaults (``Settings(_env_file=None)``), real broker-style symbol
properties and the persisted risk state, so they prove the limits an operator gets without any
extra configuration. Nothing here relaxes a limit to make a trade fit.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.schemas import Signal, SignalAction, SignalStatus
from app.database.base import Base, SignalRecord, TradeRecord
from app.execution.service import ExecutionService
from app.mt5.account import TRADE_MODE_REAL, AccountProfile, account_profile
from app.risk.engine import EntryFacts, RiskEngine
from app.risk.sizing import SymbolSpec, position_risk
from app.risk.state import RiskStateStore
from app.services.bot import BotService
from app.services.scheduler import MarketScheduler
from conftest import FakeAccount, FakeGateway, make_intent, make_signal, market_gateway

HARD = Settings(_env_file=None)
# The account this deployment is attached to: 11.56 USD of real money on a 1:400 account.
REAL_ACCOUNT = dict(balance=11.56, equity=11.56, margin_free=11.56, leverage=400.0)
# EURUSDm as a broker publishes it: a 100 000 unit contract, one point (0.00001) = 1.00 USD per lot.
BROKER = SymbolSpec(volume_min=0.01, volume_max=100.0, volume_step=0.01, tick_value=1.0, tick_size=0.00001, point=0.00001, contract_size=100_000.0, digits=5)
# A one-thousand-unit contract: one point = 0.01 USD per lot, so 0.10 USD is expressible.
MICRO = SymbolSpec(volume_min=0.01, volume_max=100.0, volume_step=0.01, tick_value=0.01, tick_size=0.00001, point=0.00001, contract_size=1_000.0, digits=5)
# The same symbol, but with a margin requirement that cannot fit inside the 3 USD allocation.
MARGIN_HEAVY = SymbolSpec(volume_min=0.01, volume_max=100.0, volume_step=0.01, tick_value=0.01, tick_size=0.00001, point=0.00001, contract_size=1_000.0, margin_per_lot=500.0, digits=5)
ONE_DAY = timedelta(days=1)


def signal(**overrides) -> Signal:
    values = dict(action=SignalAction.BUY, confidence=.9, score=90, symbol="EURUSDm", entry=1.10000, stop_loss=1.09975, take_profit=1.10050, strategy="trend_following", timeframe="M15")
    values.update(overrides)
    return Signal(timestamp=datetime.now(timezone.utc), **values)


def account(trade_mode: int = 0, **overrides) -> AccountProfile:
    values = dict(login=134693538, server="ExnessKE-MT5Real9", company="Exness (KE) Limited", currency="USD", balance=11.56, equity=11.56, free_margin=11.56, leverage=400.0, trade_mode=trade_mode)
    values.update(overrides)
    return AccountProfile(**values)


def facts(**overrides) -> EntryFacts:
    base = dict(
        spec=MICRO, connected=True, account=account(0), algo_trading_enabled=True, data_age_seconds=2.0,
        momentum_action="HOLD", spread_points=1.0, open_positions=0, symbol_positions=0, exposure=0.0,
        margin_level=None, duplicate_exists=False, emergency_locked=False, trading_enabled=True,
        free_margin=11.56, equity=11.56, leverage=400.0,
    )
    base.update(overrides)
    return EntryFacts(**base)


def assess(state=None, sig=None, settings=None, day=None, **overrides):
    return RiskEngine(settings or HARD).assess(sig or signal(), facts(**overrides), state=state, day=day)


def failures(result) -> set[str]:
    return set(result.failed_checks)


def minutes_now() -> date:
    return datetime.now(timezone.utc).date()


# --------------------------------------------------------------------------- the five limits

def test_the_shipped_defaults_are_the_five_hard_limits():
    assert (HARD.max_bot_capital_usd, HARD.max_loss_per_trade_usd, HARD.max_session_loss_usd, HARD.max_daily_trades, HARD.max_open_positions) == (3.0, 0.10, 0.30, 3, 1)


def test_bot_capital_is_the_allocation_ceiling_not_the_equity():
    # Equity and free margin are far larger than the allocation; the 3 USD ceiling is what blocks.
    result = assess(spec=MARGIN_HEAVY, equity=10_000.0, free_margin=10_000.0)

    assert not result.approved and "margin_within_capital" in failures(result)
    # 0.10 USD / (25 points x 0.01 USD) = 0.40 lots, needing 200 USD of margin.
    assert result.volume == pytest.approx(0.40) and result.margin_usd == pytest.approx(200.0)
    assert "exceeds the 3.00 USD bot capital allocation" in result.reason
    # The same trade inside the allocation passes the capital check.
    assert assess().approved and assess().margin_usd == pytest.approx(1.1)


def test_max_loss_per_trade_is_never_exceeded_by_the_derived_volume():
    for distance, spec in ((0.00025, MICRO), (0.0005, MICRO), (0.001, BROKER), (0.003, BROKER)):
        entry = 1.10000
        stop = entry - distance
        result = assess(sig=signal(entry=entry, stop_loss=stop, take_profit=entry + 2 * distance), spec=spec)
        if result.volume is None:
            # No volume at all is the correct answer when the broker minimum would risk more.
            assert result.min_lot_risk_usd > HARD.max_loss_per_trade_usd
            continue
        assert result.risk_usd == pytest.approx(position_risk(result.volume, entry, stop, spec))
        assert result.risk_usd <= HARD.max_loss_per_trade_usd + 1e-9
        # The next step up the volume ladder would break the budget, so the floor is the maximum.
        assert position_risk(result.volume + spec.volume_step, entry, stop, spec) > HARD.max_loss_per_trade_usd


def test_a_minimum_lot_above_the_budget_is_rejected_and_never_traded(session_factory, db):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now, account=FakeAccount(**REAL_ACCOUNT))
    scheduler = MarketScheduler(Settings(_env_file=None, symbols=["EURUSD"], news_fail_closed=False), gateway, session_factory)

    summary = scheduler.run_once(now=now)

    assert gateway.requests == []  # not one order, not even a rejected one
    assert summary["executions"] == 0 and summary["candidates"] == 1
    detail = summary["symbols"]["EURUSD"]["candidates_detail"][0]
    assert detail["status"] == SignalStatus.RISK_REJECTED.value
    assert "broker minimum volume 0.01 would risk" in detail["reason"]
    assert "above the 0.10 USD per-trade limit" in detail["reason"]
    assert detail["risk"]["volume"] is None and detail["risk"]["min_lot_risk_usd"] > 0.10
    assert list(db.scalars(select(TradeRecord))) == []
    row = db.scalar(select(SignalRecord))
    assert row.status == SignalStatus.RISK_REJECTED.value
    # The bot refuses rather than tightening the stop to make the minimum lot fit.
    assert row.stop_loss < row.entry_price


def test_max_session_loss_blocks_at_the_boundary_and_is_persisted(db):
    store = RiskStateStore(db)
    equity = 1_000.0  # keeps the percentage guard out of the way so the dollar limit is isolated
    store.add_realized_pnl(-0.29)
    assert assess(state=store, equity=equity, free_margin=equity).approved

    store.add_realized_pnl(-0.01)  # exactly 0.30 USD of realized session loss
    boundary = assess(state=store, equity=equity, free_margin=equity)
    assert not boundary.approved and "loss_limits" in failures(boundary)
    assert "session loss limit reached (0.30 USD of 0.30 USD)" in boundary.reason
    assert boundary.session_loss_usd == pytest.approx(0.30)


def test_max_daily_trades_allows_three_and_rejects_the_fourth(db):
    store = RiskStateStore(db)
    for opened in range(HARD.max_daily_trades):
        result = assess(state=store)
        assert result.approved and result.trades_opened == opened
        store.register_trade_opened()

    fourth = assess(state=store)
    assert not fourth.approved and "loss_limits" in failures(fourth)
    assert "daily trade limit reached (3 of 3 trades)" in fourth.reason


def test_max_open_positions_is_one(db):
    store = RiskStateStore(db)
    assert assess(state=store, open_positions=0).approved
    second = assess(state=store, open_positions=1)
    assert not second.approved and "single_position" in failures(second)
    assert "maximum simultaneous positions reached (1 open, limit 1)" in second.reason


# --------------------------------------------------------------------------- restart durability

def risk_database(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'hard_limits.db').as_posix()}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def test_a_restart_does_not_reset_the_session_loss_or_the_trade_counter(tmp_path):
    engine_one, session_one = risk_database(tmp_path)
    with session_one() as db:
        store = RiskStateStore(db)
        store.add_realized_pnl(-0.30)
        for _ in range(HARD.max_daily_trades):
            store.register_trade_opened()
        first = assess(state=store, equity=1_000.0, free_margin=1_000.0)
        assert not first.approved and "loss_limits" in failures(first)
        assert first.session_loss_usd == pytest.approx(0.30) and first.trades_opened == 3
    engine_one.dispose()

    # A restart rebuilds every object with zero in-memory counters; the database remembers.
    engine_two, session_two = risk_database(tmp_path)
    with session_two() as db:
        store = RiskStateStore(db)
        assert store.snapshot().realized_pnl == pytest.approx(-0.30)
        assert store.trades_opened() == 3
        restarted = assess(state=store, equity=1_000.0, free_margin=1_000.0)
        assert not restarted.approved and restarted.trades_opened == 3
        assert "session loss limit reached" in restarted.reason
        assert "daily trade limit reached" in restarted.reason
    engine_two.dispose()


def test_the_trade_counter_starts_fresh_on_a_new_day(db):
    store = RiskStateStore(db)
    for _ in range(HARD.max_daily_trades):
        store.register_trade_opened()
    assert not assess(state=store).approved

    tomorrow = minutes_now() + ONE_DAY
    assert assess(state=store, day=tomorrow).approved
    assert store.trades_opened(tomorrow) == 0


# --------------------------------------------------------------------------- real-account detection

def test_a_real_broker_account_is_detected_and_reported_as_real():
    profile = account_profile(FakeAccount(trade_mode=2, server="ExnessKE-MT5Real9", company="Exness (KE) Limited"))

    assert profile is not None and profile.trade_mode == TRADE_MODE_REAL
    assert profile.is_real is True and profile.is_demo is False and profile.trade_mode_label == "REAL"
    assert (profile.server, profile.company, profile.currency) == ("ExnessKE-MT5Real9", "Exness (KE) Limited", "USD")
    # A demo bot setting never downgrades a real broker account.
    assert HARD.trading_mode.value == "demo" and HARD.live_orders_permitted is False


def test_a_real_account_rejects_every_entry_even_when_the_env_says_demo():
    demo_setting = Settings(_env_file=None, trading_mode="demo", live_trading_enabled=False)
    result = assess(settings=demo_setting, account=account(2))

    assert not result.approved and "account_authorized" in failures(result)
    assert "the connected broker account is REAL (trade_mode=2)" in result.reason
    assert "real-account orders are refused" in result.reason
    # A demo account on the same settings is authorized; an unreadable mode is not.
    assert assess(settings=demo_setting, account=account(0)).approved
    assert "account_authorized" in failures(assess(settings=demo_setting, account=account(None)))
    assert account_profile(None) is None


def test_the_bot_status_reports_a_real_account_prominently(session_factory, db):
    settings = Settings(_env_file=None, symbols=[], news_fail_closed=False)
    gateway = FakeGateway(account=FakeAccount(trade_mode=2, server="ExnessKE-MT5Real9"))
    service = BotService(settings, gateway, session_factory=session_factory, scheduler=MarketScheduler(settings, gateway, session_factory))
    assert service.start()[0] is True
    try:
        status = service.status()
    finally:
        service.stop()

    assert status["mode"] == "demo" and status["account_trade_mode"] == "REAL"
    assert status["account"]["is_real"] is True and status["account"]["server"] == "ExnessKE-MT5Real9"
    assert status["account_state"].startswith("BLOCKED:") and "REAL" in status["account_state"]
    assert status["state"] == "DEGRADED" and "REAL" in status["detail"]


def test_the_execution_layer_refuses_a_real_account_even_in_demo_mode(session_factory, db):
    gateway = FakeGateway(account=FakeAccount(trade_mode=2))
    service = ExecutionService(HARD, gateway)
    intent = make_intent(signal=make_signal(symbol="EURUSD"), trade_id="hard-limit-1", key="hard-limit-1")

    outcome = service.submit(db, intent)

    assert outcome.status == "REJECTED" and "REAL" in outcome.reason
    # Nothing reached the broker: only the local rejection record exists.
    assert gateway.requests == []
    recorded = list(db.scalars(select(TradeRecord)))
    assert [record.status for record in recorded] == ["REJECTED"]


def test_the_scheduler_never_trades_a_real_account(session_factory, db):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now, account=FakeAccount(trade_mode=2, **REAL_ACCOUNT))
    settings = Settings(_env_file=None, symbols=["EURUSD"], news_fail_closed=False)
    summary = MarketScheduler(settings, gateway, session_factory).run_once(now=now)

    assert gateway.requests == [] and summary["executions"] == 0
    detail = summary["symbols"]["EURUSD"]["candidates_detail"][0]
    assert detail["status"] == SignalStatus.RISK_REJECTED.value and "REAL" in detail["reason"]
