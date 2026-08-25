from datetime import datetime, timezone
from app.core.config import Settings
from app.core.schemas import Signal, SignalAction
from app.risk.engine import RiskEngine, SymbolSpec

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
