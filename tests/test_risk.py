from datetime import date, datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import Settings
from app.core.schemas import Signal, SignalAction
from app.database.base import Base
from app.risk.engine import RiskEngine, SymbolSpec
from app.risk.state import RiskStateStore

def signal(): return Signal(action=SignalAction.BUY, confidence=.8, score=80, symbol="EURUSD", entry=1.1, stop_loss=1.09, take_profit=1.12, strategy="test", timeframe="M15", timestamp=datetime.now(timezone.utc))
def spec(): return SymbolSpec(.01, 100, .01, 1, .00001, .00001, 0)
def test_lot_size_uses_risk_and_rounds_down(): assert RiskEngine(Settings()).lot_size(10_000, 1.1, 1.09, spec()) == .05
def test_daily_loss_blocks_trade():
    result=RiskEngine(Settings()).approve(signal(), equity=10_000, balance_peak=10_000, daily_pnl=-201, open_positions=0, symbol_positions=0, spread_points=1, spec=spec(), trading_enabled=True, news_clear=True)
    assert not result.approved and "daily loss limit reached" in result.reasons
def test_stop_loss_is_mandatory():
    result=RiskEngine(Settings()).approve(signal().model_copy(update={"stop_loss":None}), equity=10_000,balance_peak=10_000,daily_pnl=0,open_positions=0,symbol_positions=0,spread_points=1,spec=spec(),trading_enabled=True,news_clear=True)
    assert not result.approved
def test_invalid_market_conditions_and_duplicate_reject():
    result=RiskEngine(Settings()).approve(signal(), equity=10_000,balance_peak=10_000,daily_pnl=0,open_positions=0,symbol_positions=0,spread_points=1,spec=spec(),trading_enabled=True,news_clear=True,market_open=False,tick_valid=False,margin_sufficient=False,duplicate_exists=True)
    assert not result.approved
    assert {"market closed","invalid tick","insufficient margin","duplicate position or order exists"}.issubset(result.reasons)


def engine(): return RiskEngine(Settings(_env_file=None))
def approve(**overrides):
    sig = overrides.pop("signal", signal())
    kwargs = dict(equity=10_000.0, balance_peak=10_000.0, daily_pnl=0.0, open_positions=0, symbol_positions=0, spread_points=1.0, spec=spec(), trading_enabled=True, news_clear=True)
    kwargs.update(overrides)
    return engine().approve(sig, **kwargs)


def test_approved_trade_passes_with_a_derived_volume():
    result = approve()
    assert result.approved and result.reasons == [] and result.volume == 0.05


def test_position_and_spread_limits_block():
    assert "maximum simultaneous positions reached" in approve(open_positions=5).reasons
    assert "maximum positions per symbol reached" in approve(symbol_positions=1).reasons
    assert "spread protection triggered" in approve(spread_points=26).reasons
    assert "maximum drawdown emergency stop" in approve(equity=8_000.0, balance_peak=10_000.0).reasons
    assert "news filter blocks trading" in approve(news_clear=False).reasons


def test_total_exposure_margin_level_and_risk_guard_limits():
    assert "total exposure limit reached" in approve(exposure=30_000.0).reasons
    assert "margin level below minimum" in approve(margin_level=100.0).reasons
    assert approve(margin_level=1_000.0, exposure=19_000.0).approved
    guarded = RiskEngine(Settings(_env_file=None, risk_per_trade_pct=2, max_risk_per_trade_pct=1))
    result = guarded.approve(signal(), equity=10_000, balance_peak=10_000, daily_pnl=0, open_positions=0, symbol_positions=0, spread_points=1, spec=spec(), trading_enabled=True, news_clear=True)
    assert not result.approved and "risk per trade exceeds configured guard" in result.reasons


def test_free_margin_below_required_margin_blocks():
    sized = SymbolSpec(.01, 100, .01, 1, .00001, .00001, 0, 0, 100_000.0)  # 0.05 lots needs 55 of margin at 1:100
    assert "insufficient margin for risk-sized volume" in approve(free_margin=10.0, leverage=100.0, spec=sized).reasons
    assert approve(free_margin=1_000.0, leverage=100.0, spec=sized).approved
    # A spec that cannot express margin at all is unverifiable, so it must not be approved.
    assert "insufficient margin for risk-sized volume" in approve(free_margin=1_000.0, leverage=100.0).reasons


def test_daily_loss_breach_is_read_from_the_store_and_survives_a_restart(tmp_path):
    settings = Settings(_env_file=None)
    engine_one, session_one = risk_database(tmp_path)
    with session_one() as db:
        store = RiskStateStore(db)
        store.observe_equity(10_000.0)
        store.add_realized_pnl(-250.0)
        first = RiskEngine(settings).approve(signal(), equity=10_000, balance_peak=10_000, daily_pnl=0, open_positions=0, symbol_positions=0, spread_points=1, spec=spec(), trading_enabled=True, news_clear=True, state=store)
        assert not first.approved and "daily loss limit reached" in first.reasons
    engine_one.dispose()

    # Simulated restart: new engine objects, new session, zero in-memory counters, same database.
    engine_two, session_two = risk_database(tmp_path)
    with session_two() as db:
        second = RiskEngine(settings).approve(signal(), equity=10_000, balance_peak=10_000, daily_pnl=0, open_positions=0, symbol_positions=0, spread_points=1, spec=spec(), trading_enabled=True, news_clear=True, state=RiskStateStore(db))
        assert not second.approved and "daily loss limit reached" in second.reasons
    engine_two.dispose()


def test_drawdown_peak_and_emergency_lock_survive_a_restart(tmp_path):
    settings = Settings(_env_file=None)
    engine_one, session_one = risk_database(tmp_path)
    with session_one() as db:
        store = RiskStateStore(db)
        store.observe_equity(12_000.0)
        # balance_peak is passed as 9 000 to prove the peak comes from the persistent store.
        first = RiskEngine(settings).approve(signal(), equity=9_000, balance_peak=9_000, daily_pnl=0, open_positions=0, symbol_positions=0, spread_points=1, spec=spec(), trading_enabled=True, news_clear=True, state=store)
        assert not first.approved and "maximum drawdown emergency stop" in first.reasons
        assert store.load().emergency_locked is True
    engine_one.dispose()

    engine_two, session_two = risk_database(tmp_path)
    with session_two() as db:
        store = RiskStateStore(db)
        second = RiskEngine(settings).approve(signal(), equity=9_000, balance_peak=9_000, daily_pnl=0, open_positions=0, symbol_positions=0, spread_points=1, spec=spec(), trading_enabled=True, news_clear=True, state=store)
        assert not second.approved
        assert {"maximum drawdown emergency stop", "bot is stopped or emergency locked"}.issubset(second.reasons)
        # The lock is not cleared by a new trading day either.
        assert store.load(date(2999, 1, 1)).emergency_locked is True
    engine_two.dispose()


def risk_database(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'risk.db').as_posix()}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)
