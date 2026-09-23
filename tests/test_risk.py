"""The pre-trade gate: every check is named, any failure means NO TRADE, limits come from the store."""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.schemas import Signal, SignalAction
from app.database.base import Base
from app.mt5.account import AccountProfile
from app.risk.engine import CHECK_ORDER, EntryFacts, RiskEngine
from app.risk.sizing import SymbolSpec
from app.risk.state import RiskStateStore
from conftest import fresh_store

# A standard contract: one point (0.00001) is worth 1.00 USD per lot, so a 70-point technical stop
# on the broker's 0.01 minimum lot is exactly the 0.70 USD per-trade budget.
MICRO = SymbolSpec(volume_min=0.01, volume_max=100.0, volume_step=0.01, tick_value=1.0, tick_size=0.00001, point=0.00001, contract_size=100_000.0)


def signal(**overrides) -> Signal:
    values = dict(action=SignalAction.BUY, confidence=.8, score=80, symbol="EURUSDm", entry=1.10000, stop_loss=1.09930, take_profit=1.10140, strategy="test", timeframe="M15")
    values.update(overrides)
    return Signal(timestamp=datetime.now(timezone.utc), **values)


def account(trade_mode=0) -> AccountProfile:
    return AccountProfile(login=1, server="Test", company="Test", currency="USD", balance=11.56, equity=11.56, free_margin=11.56, leverage=400.0, trade_mode=trade_mode)


def facts(**overrides) -> EntryFacts:
    base = dict(
        spec=MICRO, connected=True, account=account(), algo_trading_enabled=True, data_age_seconds=5.0,
        momentum_action="HOLD", spread_points=1.0, open_positions=0, symbol_positions=0, exposure=0.0,
        margin_level=None, duplicate_exists=False, emergency_locked=False, trading_enabled=True,
        free_margin=11.56, equity=11.56, leverage=400.0,
    )
    base.update(overrides)
    return EntryFacts(**base)


def engine(settings=None) -> RiskEngine:
    return RiskEngine(settings or Settings(_env_file=None))


def assess(sig=None, settings=None, state=None, **overrides):
    # The gate fails closed without a readable risk state, so every call here gets a private empty
    # one unless the test supplies its own. test_risk_state_is_required proves the refusal.
    return engine(settings).assess(sig or signal(), facts(**overrides), state=state if state is not None else fresh_store())


def failures(result) -> set[str]:
    return set(result.failed_checks)


def test_the_chain_runs_the_documented_order():
    result = assess()
    assert [check.name for check in result.checks] == list(CHECK_ORDER)
    assert CHECK_ORDER[:12] == (
        "mt5_connected", "account_authorized", "account_matches_expected", "algo_trading_enabled",
        "fresh_market_data", "trend_signal", "momentum_confirmed", "spread_within_limit",
        "valid_stop_loss", "valid_take_profit", "risk_reward", "position_sizing",
    )


def test_an_approved_trade_is_sized_from_the_loss_budget_and_the_technical_stop():
    result = assess()

    assert result.approved and result.failures == []
    # 0.70 USD / (70 points x 1.00 USD per point per lot) = 0.01 lots, exactly the lot cap.
    assert result.volume == pytest.approx(0.01)
    assert result.risk_usd == pytest.approx(0.70)
    assert result.risk_usd <= 0.70 + 1e-9
    assert result.stop_distance_points == pytest.approx(70.0)
    assert result.margin_usd == pytest.approx(2.75, abs=1e-6)
    assert result.risk_reward == pytest.approx(2.0)


def test_the_broker_minimum_volume_may_not_exceed_the_per_trade_budget():
    # A 300-point stop: the broker's smallest 0.01 lot would lose 3.00 USD against the 0.70 budget.
    result = assess(signal(stop_loss=1.09700, take_profit=1.10600))

    assert not result.approved and "position_sizing" in failures(result)
    assert "broker minimum volume 0.01 would risk 3.00 USD" in result.reason
    assert result.volume is None


def test_missing_stop_loss_is_never_traded():
    result = assess(signal(stop_loss=None))
    assert not result.approved and "valid_stop_loss" in failures(result)
    assert "no stop loss" in result.reason


def test_a_stop_on_the_wrong_side_is_rejected():
    assert "valid_stop_loss" in failures(assess(signal(action=SignalAction.BUY, stop_loss=1.11000)))
    assert "valid_stop_loss" in failures(assess(signal(action=SignalAction.SELL, stop_loss=1.09000)))


def test_missing_take_profit_and_below_two_to_one_are_rejected():
    assert "valid_take_profit" in failures(assess(signal(take_profit=None)))
    assert "risk_reward" in failures(assess(signal(take_profit=1.10130)))
    assert assess(signal(take_profit=1.10140)).approved  # exactly 1:2 is enough


def test_spread_position_and_duplicate_limits_block():
    assert "spread_within_limit" in failures(assess(spread_points=26))
    assert "single_position" in failures(assess(open_positions=1))
    assert "symbol_position_limit" in failures(assess(symbol_positions=1))
    assert "no_duplicate" in failures(assess(duplicate_exists=True))
    assert "single_position" in failures(assess(open_positions=None))


def test_connection_account_and_terminal_state_block():
    assert "mt5_connected" in failures(assess(connected=False))
    assert "algo_trading_enabled" in failures(assess(algo_trading_enabled=False))
    assert "algo_trading_enabled" in failures(assess(algo_trading_enabled=None))
    assert "account_authorized" in failures(assess(account=None))
    assert "account_authorized" in failures(assess(account=account(trade_mode=None)))


def test_stale_data_weak_signal_and_contradicting_momentum_block():
    assert "fresh_market_data" in failures(assess(data_age_seconds=None))
    assert "fresh_market_data" in failures(assess(data_age_seconds=100_000))
    assert "trend_signal" in failures(assess(signal(action=SignalAction.HOLD)))
    assert "trend_signal" in failures(assess(signal(score=10)))
    assert "momentum_confirmed" in failures(assess(momentum_action="SELL"))
    assert "momentum_confirmed" in failures(assess(momentum_action=None))


def test_exposure_and_margin_level_guards_are_retained():
    assert "total_exposure" in failures(assess(exposure=30_000.0))
    assert "margin_level" in failures(assess(margin_level=100.0))
    assert assess(margin_level=1_000.0, exposure=19.0).approved


def test_emergency_state_blocks():
    assert "emergency_stop_clear" in failures(assess(emergency_locked=True))
    assert "emergency_stop_clear" in failures(assess(trading_enabled=False))


# --------------------------------------------------------------------------- fail closed

class UnreadableStore:
    """A risk state whose database is down: every read raises, as a dropped connection would."""

    def load(self, day=None): raise RuntimeError("the risk-state database is unavailable")
    def observe_equity(self, equity, day=None): raise RuntimeError("the risk-state database is unavailable")


def test_every_unverifiable_input_fails_closed():
    """Each condition the operator named is a refusal, and each is named in the chain."""
    conditions = {
        "mt5_connected": {"connected": False},
        "algo_trading_enabled": {"algo_trading_enabled": None},
        "fresh_market_data": {"data_age_seconds": None},
        "spread_within_limit": {"spread_points": None},
        "single_position": {"open_positions": None},
        "valid_stop_loss": {"spec": None},
        "margin_within_capital": {"free_margin": None},
        "risk_state_available": {"state": UnreadableStore()},
    }
    for expected, overrides in conditions.items():
        result = assess(**overrides)
        assert not result.approved, f"{expected} should refuse the trade"
        assert expected in failures(result), f"{expected} not among {result.failed_checks}"
        assert result.reason


def test_a_trade_is_refused_without_a_readable_risk_state():
    """No risk state means no verified loss or trade allowance, never a zero-loss session."""
    engine = RiskEngine(Settings(_env_file=None))
    missing = engine.assess(signal(), facts())

    assert not missing.approved and "risk_state_available" in failures(missing)
    assert "risk state is unavailable" in missing.reason
    assert missing.session_loss_usd == 0.0 and missing.trades_opened == 0

    unreadable = engine.assess(signal(), facts(), state=UnreadableStore())
    assert not unreadable.approved and "risk_state_available" in failures(unreadable)
    assert "could not be read (RuntimeError)" in unreadable.reason


def test_an_invalid_symbol_specification_blocks_on_every_price_check():
    result = assess(spec=None)

    assert not result.approved
    assert {"valid_stop_loss", "position_sizing", "margin_within_capital"}.issubset(failures(result))
    assert "symbol specification is unavailable" in result.reason


def test_daily_loss_breach_is_read_from_the_store_and_survives_a_restart(tmp_path):
    settings = Settings(_env_file=None)
    engine_one, session_one = risk_database(tmp_path)
    with session_one() as db:
        store = RiskStateStore(db)
        store.observe_equity(10_000.0)
        store.add_realized_pnl(-250.0)
        first = engine(settings).assess(signal(), facts(equity=10_000.0, free_margin=10_000.0), state=store)
        assert not first.approved and "loss_limits" in failures(first)
    engine_one.dispose()

    # Simulated restart: new engine objects, new session, zero in-memory counters, same database.
    engine_two, session_two = risk_database(tmp_path)
    with session_two() as db:
        second = engine(settings).assess(signal(), facts(equity=10_000.0, free_margin=10_000.0), state=RiskStateStore(db))
        assert not second.approved and "loss_limits" in failures(second)
    engine_two.dispose()


def test_drawdown_peak_and_emergency_lock_survive_a_restart(tmp_path):
    settings = Settings(_env_file=None)
    engine_one, session_one = risk_database(tmp_path)
    with session_one() as db:
        store = RiskStateStore(db)
        store.observe_equity(12_000.0)
        first = engine(settings).assess(signal(), facts(equity=9_000.0, free_margin=9_000.0), state=store)
        assert not first.approved and "loss_limits" in failures(first)
        assert store.load().emergency_locked is True
    engine_one.dispose()

    engine_two, session_two = risk_database(tmp_path)
    with session_two() as db:
        store = RiskStateStore(db)
        second = engine(settings).assess(signal(), facts(equity=9_000.0, free_margin=9_000.0), state=store)
        assert not second.approved
        assert {"loss_limits", "emergency_stop_clear"}.issubset(failures(second))
        # The lock is not cleared by a new trading day either.
        assert store.load(date(2999, 1, 1)).emergency_locked is True
    engine_two.dispose()


def risk_database(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'risk.db').as_posix()}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)
