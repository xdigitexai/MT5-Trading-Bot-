import pandas as pd
from app.core.schemas import Signal, SignalAction
from app.indicators.technical import atr, atr_percentile, donchian, sma
from app.strategies._common import bracket, hold, invalid_frames, last, trade

STRATEGY = "volatility"
MIN_CANDLES = 120
ATR_PERIOD, PERCENTILE_LOOKBACK = 14, 100
EXPANSION_PERCENTILE = 0.60   # atr must sit in the upper part of its own recent distribution
NORM_WINDOW = 50              # window that defines the recent volatility norm
MAX_ATR_MULTIPLE = 3.0        # beyond this the regime is abnormal and is refused
RANGE_PERIOD = 20
BUFFER_ATR = 0.20
MIN_BODY_ATR = 0.25
CLOSE_POSITION = 0.60
STOP_ATR = 1.5

def evaluate(symbol: str, h4: pd.DataFrame, h1: pd.DataFrame, m15: pd.DataFrame, rr: float = 2.0) -> Signal:
    """Volatility expansion breakout on M15, refusing abnormal volatility regimes.

    Idea: a volatility squeeze releasing into a directional close is tradeable; a volatility spike is
    not. Confirmation rules: M15 ATR(14) sits in the upper 40% of its last 100 bars and the same is
    true on H1, the close clears the prior 20-candle donchian level by an ATR buffer, the candle closes
    in the outer 40% of its own range with a body of at least a quarter ATR, and ATR is at or above its
    50-bar norm. Refusal rules: an ATR above 3x its own 50-bar norm is treated as an abnormal
    condition and returns HOLD, as does an expansion percentile that has not risen yet. Uses only
    completed rows.
    """
    if not symbol or rr < 1:
        return hold(symbol, STRATEGY, "invalid strategy input")
    if invalid_frames((h4, h1, m15), MIN_CANDLES):
        return hold(symbol, STRATEGY, "insufficient or invalid candles")
    atr_series = atr(m15, ATR_PERIOD)
    volatility, average_atr = last(atr_series), last(sma(atr_series, NORM_WINDOW))
    m15_percentile = last(atr_percentile(m15, ATR_PERIOD, PERCENTILE_LOOKBACK))
    h1_percentile = last(atr_percentile(h1, ATR_PERIOD, PERCENTILE_LOOKBACK))
    range_high, range_low = (level.shift(1) for level in donchian(m15, RANGE_PERIOD))
    resistance, support = last(range_high), last(range_low)
    values = (volatility, average_atr, m15_percentile, h1_percentile, resistance, support)
    if any(value is None for value in values) or volatility <= 0 or average_atr <= 0:
        return hold(symbol, STRATEGY, "indicator unavailable")
    multiple = volatility / average_atr
    if multiple > MAX_ATR_MULTIPLE:
        return hold(symbol, STRATEGY, f"abnormal volatility: atr {multiple:.1f}x its {NORM_WINDOW}-bar norm, trading refused")
    if m15_percentile < EXPANSION_PERCENTILE:
        return hold(symbol, STRATEGY, f"volatility not expanding: atr in the {m15_percentile:.0%} percentile of its last {PERCENTILE_LOOKBACK} bars")
    buffer = volatility * BUFFER_ATR
    last_candle, bar_range = m15.iloc[-1], float(m15.high.iloc[-1] - m15.low.iloc[-1])
    body = abs(float(last_candle.close - last_candle.open))
    close_top = (float(last_candle.close - last_candle.low) / bar_range) if bar_range > 0 else 0.0
    close_bottom = (float(last_candle.high - last_candle.close) / bar_range) if bar_range > 0 else 0.0
    checks = {
        SignalAction.BUY: (
            (f"m15 atr in the upper {1 - EXPANSION_PERCENTILE:.0%} of its last {PERCENTILE_LOOKBACK} bars", m15_percentile >= EXPANSION_PERCENTILE, 30),
            (f"h1 volatility expanding above the {EXPANSION_PERCENTILE:.0%} percentile", h1_percentile >= EXPANSION_PERCENTILE, 15),
            (f"close above the prior {RANGE_PERIOD}-candle range by {BUFFER_ATR:g} ATR", float(last_candle.close) > resistance + buffer, 25),
            ("bullish close in the outer 40% of the bar with a real body", last_candle.close > last_candle.open and close_top >= CLOSE_POSITION and body >= MIN_BODY_ATR * volatility, 15),
            (f"atr at or above its {NORM_WINDOW}-bar norm", multiple >= 1.0, 15),
        ),
        SignalAction.SELL: (
            (f"m15 atr in the upper {1 - EXPANSION_PERCENTILE:.0%} of its last {PERCENTILE_LOOKBACK} bars", m15_percentile >= EXPANSION_PERCENTILE, 30),
            (f"h1 volatility expanding above the {EXPANSION_PERCENTILE:.0%} percentile", h1_percentile >= EXPANSION_PERCENTILE, 15),
            (f"close below the prior {RANGE_PERIOD}-candle range by {BUFFER_ATR:g} ATR", float(last_candle.close) < support - buffer, 25),
            ("bearish close in the outer 40% of the bar with a real body", last_candle.close < last_candle.open and close_bottom >= CLOSE_POSITION and body >= MIN_BODY_ATR * volatility, 15),
            (f"atr at or above its {NORM_WINDOW}-bar norm", multiple >= 1.0, 15),
        ),
    }
    failures = {action: [name for name, ok, _ in checks[action] if not ok] for action in checks}
    action = min(checks, key=lambda candidate: len(failures[candidate]))
    if failures[action]:
        return hold(symbol, STRATEGY, f"volatility expansion unconfirmed for {action.value}: {', '.join(failures[action])}")
    score = sum(weight for _, _, weight in checks[action])
    entry = float(last_candle.close)
    stop = entry - STOP_ATR * volatility if action is SignalAction.BUY else entry + STOP_ATR * volatility
    levels = bracket(action, entry, stop, rr)
    if levels is None:
        return hold(symbol, STRATEGY, "invalid stop placement")
    reasons = [f"{action.value} volatility expansion confirmed on M15"] + [name for name, _, _ in checks[action]]
    reasons.append(f"atr {volatility:.5f} is {multiple:.2f}x its {NORM_WINDOW}-bar norm, no abnormal spike")
    reasons.append(f"stop {STOP_ATR:g} ATR from entry at {stop:.5f}, target honours {rr:g}R")
    return trade(symbol, STRATEGY, action, score, levels, reasons)
