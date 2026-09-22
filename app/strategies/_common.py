"""Shared building blocks for the strategy modules.

Every strategy follows the same fail-closed contract: unusable input or an unconfirmed setup
returns a HOLD signal carrying a human-readable reason, and a traded signal always carries a
stop loss strictly on the risk side of entry plus a target that honours the requested R:R.
Confidence is a quality score, never a probability of profit.
"""
from datetime import datetime, timezone
import pandas as pd
from app.core.schemas import Signal, SignalAction

REQUIRED_COLUMNS = ("open", "high", "low", "close")

def hold(symbol: str, strategy: str, reason: str, timeframe: str = "M15") -> Signal:
    return Signal(symbol=symbol, confidence=0, score=0, strategy=strategy, timeframe=timeframe, reasons=[reason], timestamp=datetime.now(timezone.utc))

def trade(symbol: str, strategy: str, action: SignalAction, score: int, levels: tuple[float, float, float], reasons: list[str], timeframe: str = "M15") -> Signal:
    entry, stop_loss, take_profit = levels
    bounded = min(max(int(score), 0), 100)
    return Signal(action=action, confidence=bounded / 100, score=bounded, symbol=symbol, entry=float(entry), stop_loss=float(stop_loss), take_profit=float(take_profit), strategy=strategy, timeframe=timeframe, reasons=reasons, timestamp=datetime.now(timezone.utc))

def invalid_frames(frames: tuple[pd.DataFrame, ...], minimum: int) -> bool:
    """True when any supplied frame lacks completed candles, required columns, or valid positive closes."""
    for frame in frames:
        if not set(REQUIRED_COLUMNS).issubset(frame.columns) or len(frame) < minimum: return True
        if frame[list(REQUIRED_COLUMNS)].tail(minimum).isna().any().any(): return True
        if (frame.close.tail(minimum) <= 0).any(): return True
    return False

def last(series: pd.Series) -> float | None:
    """Latest value of a vectorised indicator, or None while it is still NaN/empty."""
    if not len(series): return None
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)

def bracket(action: SignalAction, entry: float, stop_loss: float, rr: float) -> tuple[float, float, float] | None:
    """Entry/stop/target triple, or None when the stop is not strictly on the risk side of entry."""
    if action is SignalAction.BUY and not stop_loss < entry: return None
    if action is SignalAction.SELL and not stop_loss > entry: return None
    risk = abs(entry - stop_loss)
    take_profit = entry + risk * rr if action is SignalAction.BUY else entry - risk * rr
    return entry, stop_loss, take_profit
