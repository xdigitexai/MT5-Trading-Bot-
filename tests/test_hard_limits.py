"""The hard server-side limits: 3 USD capital, 0.70 USD per trade, 1.40 USD per session, 3 trades,
1 position and a 0.01 lot volume cap.

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
from app.mt5.account import TRADE_MODE_REAL, AccountProfile, account_matches, account_profile
from app.risk.engine import EntryFacts, RiskEngine
from app.risk.sizing import SymbolSpec
from app.risk.state import RiskStateStore
from app.services.bot import BotService
from app.services.scheduler import MarketScheduler
from conftest import FakeAccount, FakeGateway, FakeTick, fresh_store, make_intent, make_signal, market_frames, market_gateway

HARD = Settings(_env_file=None)
# The account this deployment is attached to: 11.56 USD of real money on a 1:400 account.
REAL_ACCOUNT = dict(balance=11.56, equity=11.56, margin_free=11.56, leverage=400.0)
# EURUSDm as the broker publishes it: a 100 000 unit contract, so one point (0.00001) is 1.00 USD
# per lot and a *technical* 70-point stop is exactly 0.70 USD on the broker's minimum 0.01 lot.
BROKER = SymbolSpec(volume_min=0.01, volume_max=100.0, volume_step=0.01, tick_value=1.0, tick_size=0.00001, point=0.00001, contract_size=100_000.0, digits=5)
# The same symbol, but with a margin requirement that cannot fit inside the 3 USD allocation.
MARGIN_HEAVY = SymbolSpec(volume_min=0.01, volume_max=100.0, volume_step=0.01, tick_value=1.0, tick_size=0.00001, point=0.00001, contract_size=100_000.0, margin_per_lot=500.0, digits=5)
# 70 points on the broker contract: the widest stop whose minimum lot still fits the 0.70 USD budget.
BUDGET_POINTS = 70
ONE_DAY = timedelta(days=1)


def signal(**overrides) -> Signal:
    values = dict(action=SignalAction.BUY, confidence=.9, score=90, symbol="EURUSDm", entry=1.10000, stop_loss=1.09930, take_profit=1.10140, strategy="trend_following", timeframe="M15")
    values.update(overrides)
    return Signal(timestamp=datetime.now(timezone.utc), **values)


def account(trade_mode: int = 0, **overrides) -> AccountProfile:
    values = dict(login=134693538, server="ExnessKE-MT5Real9", company="Exness (KE) Limited", currency="USD", balance=11.56, equity=11.56, free_margin=11.56, leverage=400.0, trade_mode=trade_mode)
    values.update(overrides)
    return AccountProfile(**values)


def facts(**overrides) -> EntryFacts:
    base = dict(
        spec=BROKER, connected=True, account=account(0), algo_trading_enabled=True, data_age_seconds=2.0,
        momentum_action="HOLD", spread_points=1.0, open_positions=0, symbol_positions=0, exposure=0.0,
        margin_level=None, duplicate_exists=False, emergency_locked=False, trading_enabled=True,
        free_margin=11.56, equity=11.56, leverage=400.0,
    )
    base.update(overrides)
    return EntryFacts(**base)


def assess(state=None, sig=None, settings=None, day=None, **overrides):
    # A test that does not supply a store is stating "empty session"; test_risk_state_unavailable
    # covers the refusal that happens when there is no state at all.
    return RiskEngine(settings or HARD).assess(sig or signal(), facts(**overrides), state=state if state is not None else fresh_store(), day=day)


def failures(result) -> set[str]:
    return set(result.failed_checks)


def minutes_now() -> date:
    return datetime.now(timezone.utc).date()


def stop_signal(points: int, **overrides) -> Signal:
    """A BUY signal whose technical stop is ``points`` points below the entry, target at 1:2."""
    distance = points * BROKER.point
    values = dict(entry=1.10000, stop_loss=1.10000 - distance, take_profit=1.10000 + 2 * distance)
    values.update(overrides)
    return signal(**values)


# --------------------------------------------------------------------------- the hard limits

def test_the_shipped_defaults_are_the_hard_limits():
    assert (HARD.max_bot_capital_usd, HARD.max_loss_per_trade_usd, HARD.max_session_loss_usd, HARD.max_daily_trades, HARD.max_open_positions, HARD.max_lots_per_position) == (3.0, 0.70, 1.40, 3, 1, 0.01)


def test_bot_capital_is_the_allocation_ceiling_not_the_equity():
    # Equity and free margin are far larger than the allocation; the 3 USD ceiling is what blocks.
    result = assess(spec=MARGIN_HEAVY, equity=10_000.0, free_margin=10_000.0)

    assert not result.approved and "margin_within_capital" in failures(result)
    # 0.70 USD / (70 points x 1.00 USD) = 0.01 lots, needing 5.00 USD of margin at the broker rate.
    assert result.volume == pytest.approx(0.01) and result.margin_usd == pytest.approx(5.0)
    assert "exceeds the 3.00 USD bot capital allocation" in result.reason
    # The same trade inside the allocation passes the capital check.
    assert assess().approved and assess().margin_usd == pytest.approx(2.75)


def test_the_volume_is_derived_from_the_technical_stop_and_capped_at_one_hundredth_of_a_lot():
    approved = assess(sig=stop_signal(BUDGET_POINTS))

    assert approved.approved and approved.volume == pytest.approx(0.01)
    assert approved.risk_usd == pytest.approx(0.70) and approved.risk_usd <= HARD.max_loss_per_trade_usd + 1e-9
    assert approved.stop_distance_points == pytest.approx(BUDGET_POINTS)
    assert approved.risk_reward == pytest.approx(2.0)

    # A tighter stop would derive a *bigger* volume from the same budget; the cap refuses it instead
    # of sizing up to it, and the stop is left exactly where the strategy put it.
    tighter = assess(sig=stop_signal(25))
    assert not tighter.approved and "position_sizing" in failures(tighter)
    assert "exceeds the 0.01 lot hard cap" in tighter.reason
    assert tighter.volume == pytest.approx(0.02)
    assert tighter.volume > HARD.max_lots_per_position
    assert tighter.stop_distance_points == pytest.approx(25.0)


def test_no_approved_result_ever_exceeds_the_lot_cap_or_the_loss_budget():
    for points in (20, 25, 40, 60, 70, 100, 147, 300, 600):
        result = assess(sig=stop_signal(points))
        volume = result.volume
        if not result.approved:
            # A refusal is always acceptable; assert *why* so a silent allowance cannot hide here.
            assert failures(result) & {"position_sizing", "margin_within_capital"}
            continue
        assert volume <= HARD.max_lots_per_position + 1e-9
        assert result.risk_usd <= HARD.max_loss_per_trade_usd + 1e-9
        assert result.margin_usd <= HARD.max_bot_capital_usd + 1e-9


def test_a_minimum_lot_that_would_exceed_the_budget_is_rejected():
    # 300 points at 1.00 USD per point per lot: the broker's smallest 0.01 lot would lose 3.00 USD.
    result = assess(sig=stop_signal(300))

    assert not result.approved and "position_sizing" in failures(result)
    assert result.volume is None and result.min_lot_risk_usd == pytest.approx(3.0)
    assert "broker minimum volume 0.01 would risk 3.00 USD" in result.reason
    assert "above the 0.70 USD per-trade limit" in result.reason
    # The bot refuses rather than tightening the stop to make the minimum lot fit.
    assert result.stop_distance_points == pytest.approx(300.0)


def test_max_session_loss_blocks_at_the_boundary_and_is_persisted(db):
    store = RiskStateStore(db)
    equity = 1_000.0  # keeps the percentage guard out of the way so the dollar limit is isolated
    store.add_realized_pnl(-1.39)
    assert assess(state=store, equity=equity, free_margin=equity).approved

    store.add_realized_pnl(-0.01)  # exactly 1.40 USD of realized session loss
    boundary = assess(state=store, equity=equity, free_margin=equity)
    assert not boundary.approved and "loss_limits" in failures(boundary)
    assert "session loss limit reached (1.40 USD of 1.40 USD)" in boundary.reason
    assert boundary.session_loss_usd == pytest.approx(1.40)
    # The kill switch is latched in the database, not only in the returned assessment.
    assert store.load().kill_switch_reason.startswith("session loss limit reached")


def test_max_daily_trades_allows_three_and_rejects_the_fourth(db):
    store = RiskStateStore(db)
    for opened in range(HARD.max_daily_trades):
        result = assess(state=store)
        assert result.approved and result.trades_opened == opened
        store.register_trade_opened()

    fourth = assess(state=store)
    assert not fourth.approved and "loss_limits" in failures(fourth)
    assert "daily trade limit reached (3 of 3 trades)" in fourth.reason
    assert "emergency_stop_clear" in failures(fourth)
    assert store.load().kill_switch_reason == "daily trade limit reached (3 of 3 trades)"


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


def test_a_restart_does_not_reset_the_session_loss_the_trade_counter_or_the_kill_switch(tmp_path):
    engine_one, session_one = risk_database(tmp_path)
    with session_one() as db:
        store = RiskStateStore(db)
        store.add_realized_pnl(-1.40)
        for _ in range(HARD.max_daily_trades):
            store.register_trade_opened()
        first = assess(state=store, equity=1_000.0, free_margin=1_000.0)
        assert not first.approved and "loss_limits" in failures(first)
        assert first.session_loss_usd == pytest.approx(1.40) and first.trades_opened == 3
        started_at = store.load().session_started_at
        assert started_at is not None and store.load().kill_switch_reason
    engine_one.dispose()

    # A restart rebuilds every object with zero in-memory counters; the database remembers.
    engine_two, session_two = risk_database(tmp_path)
    with session_two() as db:
        store = RiskStateStore(db)
        snapshot = store.snapshot()
        assert snapshot.realized_pnl == pytest.approx(-1.40)
        assert snapshot.trades_opened == 3 and snapshot.kill_switch_active is True
        assert snapshot.session_started_at is not None
        restarted = assess(state=store, equity=1_000.0, free_margin=1_000.0)
        assert not restarted.approved and restarted.trades_opened == 3
        assert "session loss limit reached" in restarted.reason
        assert "daily trade limit reached" in restarted.reason
        assert "emergency_stop_clear" in failures(restarted)
        assert restarted.kill_switch_active is True
    engine_two.dispose()


def test_the_trade_counter_starts_fresh_on_a_new_day(db):
    store = RiskStateStore(db)
    for _ in range(HARD.max_daily_trades):
        store.register_trade_opened()
    assert not assess(state=store).approved

    # A new session gets a fresh allowance, but the *previous* session's row keeps its kill switch:
    # the latch closes one session, it does not follow the bot forever.
    tomorrow = minutes_now() + ONE_DAY
    assert assess(state=store, day=tomorrow).approved
    assert store.trades_opened(tomorrow) == 0
    assert store.load(tomorrow).kill_switch_reason is None
    assert store.load().kill_switch_reason is not None


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
    assert status["hard_limits"]["max_lots_per_position"] == pytest.approx(0.01)


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


# --------------------------------------------------------------------------- account identity lock

PINNED = Settings(_env_file=None, mt5_login=134693538, mt5_server="ExnessKE-MT5Real9", symbols=["EURUSD"], news_fail_closed=False)


def other_account(**overrides) -> AccountProfile:
    values = dict(login=99999999, server="OtherBroker-Demo")
    values.update(overrides)
    return account(**values)


def test_account_matches_reports_a_pinned_account_a_mismatch_and_an_unconfigured_expectation():
    pinned = account()
    assert account_matches(pinned, 134693538, "ExnessKE-MT5Real9") == (True, "")
    # The server comparison is case-insensitive; MT5 reports the login as an integer.
    assert account_matches(pinned, 134693538, "exnesske-mt5real9") == (True, "")

    changed_login, login_reason = account_matches(other_account(), 134693538, "ExnessKE-MT5Real9")
    assert changed_login is False and "not the expected 134693538 @ ExnessKE-MT5Real9" in login_reason
    changed_server, server_reason = account_matches(account(server="Sneaky-Server"), 134693538, "ExnessKE-MT5Real9")
    assert changed_server is False and "refusing to trade" in server_reason
    assert account_matches(None, 134693538, "ExnessKE-MT5Real9")[0] is False
    # With neither value configured there is nothing to compare, and nothing has "changed".
    assert account_matches(pinned, None, None) == (True, "")


def test_an_unexpected_account_change_blocks_entry_and_persists_a_lock(db):
    store = RiskStateStore(db)
    result = RiskEngine(PINNED).assess(signal(), facts(account=other_account()), state=store)

    assert not result.approved
    assert {"account_matches_expected", "emergency_stop_clear"}.issubset(failures(result))
    assert "not the expected 134693538 @ ExnessKE-MT5Real9" in result.reason
    # A log line is not enough: the lock is written to the risk state, so the next cycle and the
    # next process see it.
    row = store.load()
    assert row.emergency_locked is True and "134693538" in row.kill_switch_reason

    # Simulated restart: a brand-new store over the same database still refuses.
    restarted = RiskEngine(PINNED).assess(signal(), facts(account=other_account()), state=RiskStateStore(db))
    assert not restarted.approved and "account_matches_expected" in failures(restarted)
    assert RiskStateStore(db).load().emergency_locked is True


def test_the_matching_account_is_not_locked_out(session_factory, db):
    result = RiskEngine(PINNED).assess(signal(), facts(account=account()), state=RiskStateStore(db))

    assert result.approved is True and not RiskStateStore(db).load().emergency_locked


def test_an_unreadable_account_refuses_without_latching_a_manual_reset_lock(db):
    """A transient read failure must stop the trade without needing an operator to unlock the bot."""
    store = RiskStateStore(db)
    result = RiskEngine(PINNED).assess(signal(), facts(account=None), state=store)

    assert not result.approved
    assert {"account_authorized", "account_matches_expected"}.issubset(failures(result))
    assert "the broker account could not be read" in [check.reason for check in result.failures if check.name == "account_matches_expected"][0]
    row = store.load()
    assert row.emergency_locked is False and row.kill_switch_reason is None


def test_the_scheduler_stops_and_persists_the_lock_when_the_account_changed(session_factory, db):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now, account=FakeAccount(login=99999999, server="OtherBroker-Demo", **REAL_ACCOUNT))
    summary = MarketScheduler(PINNED, gateway, session_factory).run_once(now=now)

    assert gateway.requests == [] and summary["executions"] == 0
    assert any("not the expected 134693538" in reason for reason in summary["blocked"])
    row = RiskStateStore(db).load()
    assert row.emergency_locked is True and "134693538" in row.kill_switch_reason
    # The next cycle does not even scan: the persisted lock stops the loop.
    later = MarketScheduler(PINNED, market_gateway(now + ONE_DAY), session_factory).run_once(now=now + ONE_DAY)
    assert later["skipped"] is False and later["symbols"] == {} and "emergency lock is persisted" in later["blocked"][0]


def test_the_bot_refuses_to_start_and_persists_the_lock_on_an_account_mismatch(session_factory, db):
    settings = Settings(_env_file=None, mt5_login=134693538, mt5_server="ExnessKE-MT5Real9", symbols=[])
    gateway = FakeGateway(account=FakeAccount(login=99999999, server="OtherBroker-Demo"))
    service = BotService(settings, gateway, session_factory=session_factory, scheduler=MarketScheduler(settings, gateway, session_factory))

    ok, detail = service.start()

    assert ok is False and "not the expected" in detail
    assert RiskStateStore(db).load().emergency_locked is True


def test_the_execution_layer_refuses_an_unexpected_account(session_factory, db):
    settings = Settings(_env_file=None, mt5_login=134693538, mt5_server="ExnessKE-MT5Real9")
    gateway = FakeGateway(account=FakeAccount(login=99999999, server="OtherBroker-Demo"))
    outcome = ExecutionService(settings, gateway).submit(db, make_intent(trade_id="mismatch-1", key="mismatch-1"))

    assert outcome.status == "REJECTED" and "not the expected" in outcome.reason
    assert gateway.requests == []


# --------------------------------------------------------------------------- the scheduler path

def test_the_scheduler_never_trades_a_minimum_lot_above_the_budget(session_factory, db):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now, account=FakeAccount(**REAL_ACCOUNT))
    scheduler = MarketScheduler(Settings(_env_file=None, symbols=["EURUSD"], news_fail_closed=False), gateway, session_factory)

    summary = scheduler.run_once(now=now)

    assert gateway.requests == []  # not one order, not even a rejected one
    assert summary["executions"] == 0 and summary["candidates"] == 1
    detail = summary["symbols"]["EURUSD"]["candidates_detail"][0]
    assert detail["status"] == SignalStatus.RISK_REJECTED.value
    assert "broker minimum volume 0.01 would risk" in detail["reason"]
    assert "above the 0.70 USD per-trade limit" in detail["reason"]
    assert detail["risk"]["volume"] is None and detail["risk"]["min_lot_risk_usd"] > HARD.max_loss_per_trade_usd
    assert list(db.scalars(select(TradeRecord))) == []
    row = db.scalar(select(SignalRecord))
    assert row.status == SignalStatus.RISK_REJECTED.value
    # The bot refuses rather than tightening the stop to make the minimum lot fit.
    assert row.stop_loss < row.entry_price


def test_the_scheduler_stops_after_three_executed_trades_across_a_restart(session_factory):
    """The 3-trade session limit is read from the database, so a restart cannot reset it."""
    now = datetime.now(timezone.utc)
    settings = Settings(_env_file=None, symbols=["EURUSD"], news_fail_closed=False, max_bot_capital_usd=1_000.0, max_loss_per_trade_usd=50.0, max_session_loss_usd=200.0, max_lots_per_position=1.0)

    def cycle_gateway(moment) -> FakeGateway:
        """A demo-sized fixture whose only job is to produce one executable signal per closed candle."""
        return FakeGateway(account=FakeAccount(), tick_value=FakeTick(time=moment.timestamp()), bar_frames=market_frames(moment))

    for cycle in range(3):
        moment = now + timedelta(minutes=15 * cycle)
        summary = MarketScheduler(settings, cycle_gateway(moment), session_factory).run_once(now=moment)
        assert summary["executions"] == 1, summary["symbols"]["EURUSD"]

    assert RiskStateStore(session_factory()).trades_opened() == 3
    # A fresh process and a fourth signal: refused, and no order is prepared.
    moment = now + timedelta(minutes=45)
    fourth_gateway = cycle_gateway(moment)
    fourth = MarketScheduler(settings, fourth_gateway, session_factory).run_once(now=moment)

    assert fourth["executions"] == 0 and fourth_gateway.requests == []
    reasons = "; ".join(row["reason"] for row in fourth["symbols"]["EURUSD"]["candidates_detail"])
    assert "daily trade limit reached (3 of 3 trades)" in reasons
