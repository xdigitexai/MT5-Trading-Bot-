from datetime import datetime, timezone
import pandas as pd
from app.core.schemas import Signal, SignalAction
from app.indicators.technical import atr, ema, macd, rsi

def evaluate(symbol: str, h4: pd.DataFrame, h1: pd.DataFrame, m15: pd.DataFrame, rr: float = 2.0) -> Signal:
    """Uses only completed rows. Caller must omit the still-forming candle."""
    hold = lambda reason: Signal(symbol=symbol, confidence=0, score=0, strategy="trend_following", timeframe="M15", reasons=[reason], timestamp=datetime.now(timezone.utc))
    if not symbol or rr < 1:
        return hold("invalid strategy input")
    required = {"open", "high", "low", "close"}
    for data in (h4, h1, m15):
        if not required.issubset(data.columns) or len(data) < 210:
            return hold("insufficient or invalid candles")
        if data[list(required)].tail(210).isna().any().any() or (data.close.tail(210) <= 0).any():
            return hold("invalid candle values")
    h4_fast, h4_slow = ema(h4.close, 50).iloc[-1], ema(h4.close, 200).iloc[-1]
    h1_fast, h1_slow = ema(h1.close, 50).iloc[-1], ema(h1.close, 200).iloc[-1]
    momentum, macd_signal, _ = macd(m15.close); last, prev = m15.iloc[-1], m15.iloc[-2]
    if any(pd.isna(x) for x in (h4_fast, h4_slow, h1_fast, h1_slow, momentum.iloc[-1], macd_signal.iloc[-1])):
        return hold("indicator unavailable")
    bullish = h4_fast > h4_slow and h1_fast > h1_slow and last.close > last.open and last.high > prev.high and rsi(m15.close).iloc[-1] >= 55 and momentum.iloc[-1] > macd_signal.iloc[-1]
    bearish = h4_fast < h4_slow and h1_fast < h1_slow and last.close < last.open and last.low < prev.low and rsi(m15.close).iloc[-1] <= 45 and momentum.iloc[-1] < macd_signal.iloc[-1]
    score = 0
    if (h4_fast > h4_slow) == (h1_fast > h1_slow): score += 35
    if bullish or bearish: score += 40
    volatility = atr(m15).iloc[-1]
    if pd.notna(volatility) and volatility > 0: score += 15
    action = SignalAction.BUY if bullish else SignalAction.SELL if bearish else SignalAction.HOLD
    entry = float(last.close) if action != SignalAction.HOLD else None
    sl = entry - float(volatility * 1.8) if action == SignalAction.BUY else entry + float(volatility * 1.8) if action == SignalAction.SELL else None
    tp = entry + (entry - sl) * rr if action == SignalAction.BUY else entry - (sl - entry) * rr if action == SignalAction.SELL else None
    # Score deliberately remains a quality score, not a probability forecast.
    return Signal(action=action, confidence=score/100, score=score, symbol=symbol, entry=entry, stop_loss=sl, take_profit=tp, strategy="trend_following", timeframe="M15", reasons=["H4/H1 EMA alignment", "M15 momentum and breakout confirmation"], timestamp=datetime.now(timezone.utc))
