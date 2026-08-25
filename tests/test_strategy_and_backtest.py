import numpy as np
import pandas as pd
from app.backtesting.engine import metrics, monte_carlo
from app.core.schemas import SignalAction
from app.strategies.trend_following import evaluate

def candles(start: float, direction: float):
    close = start + np.arange(220) * direction
    return pd.DataFrame({"open":close-.0001, "high":close+.0003, "low":close-.0003, "close":close})

def test_invalid_strategy_data_fails_to_hold():
    frame = pd.DataFrame({"close":[1.0]*220})
    assert evaluate("EURUSD", frame, frame, frame).action is SignalAction.HOLD

def test_insufficient_candles_fails_to_hold():
    frame = candles(1.0,.001).head(20)
    assert evaluate("EURUSD", frame, frame, frame).action is SignalAction.HOLD

def test_backtest_metrics_and_monte_carlo_are_deterministic():
    result = metrics([10,-5,20,-10])
    assert result.net_profit == 15 and result.trades == 4 and result.max_drawdown == 10
    assert monte_carlo([10,-5,20,-10], 10, seed=1) == monte_carlo([10,-5,20,-10], 10, seed=1)
