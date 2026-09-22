import pandas as pd
from app.core.schemas import Signal, SignalAction
from app.indicators.technical import adx, atr, bollinger, ema, rsi, stochastic
from app.strategies._common import bracket, hold, invalid_frames, last, trade

STRATEGY = "mean_reversion"
MIN_CANDLES = 210
BB_PERIOD, BB_MULT = 20, 2.0
RSI_PERIOD = 14
MID_MA = 20
BUY_RSI_MAX, SELL_RSI_MIN = 32.0, 68.0
MIN_STRETCH_ATR = 1.5      # distance from the mean, in ATR
MAX_ADX = 45.0             # this lightweight ADX reads ~30 in range-bound candles and 60+ in directional moves, so 45 separates the two regimes
STOCH_OS, STOCH_OB = 25.0, 75.0
STOP_ATR = 1.5

def evaluate(symbol: str, h4: pd.DataFrame, h1: pd.DataFrame, m15: pd.DataFrame, rr: float = 2.0) -> Signal:
    """Bollinger band mean reversion on M15, filtered against reversion into a strong trend.

    Idea: fade a stretched move back towards the mean, but only in a market that has no trend to
    fight. Confirmation rules: the close sits at or beyond a 2-sigma band, RSI is at its 32/68
    extreme, price is at least 1.5 ATR from the SAME PERIOD SMA(20), stochastic %K is at its 25/75
    extreme and the candle has turned in the trade direction. Protection rules: ADX(14) must stay
    below MAX_ADX (this lightweight ADX reads ~30 in range-bound candles and 60+ in directional
    moves, so a classic fixed 25 would never allow a trade), and an H4/H1 EMA(50)/EMA(200)
    structure aligned against the trade blocks it, so the strategy never fades a prevailing trend.
    Uses only completed rows.
    """
    if not symbol or rr < 1:
        return hold(symbol, STRATEGY, "invalid strategy input")
    if invalid_frames((h4, h1, m15), MIN_CANDLES):
        return hold(symbol, STRATEGY, "insufficient or invalid candles")
    close = m15.close
    lower, mid, upper = bollinger(close, BB_PERIOD, BB_MULT)
    lower_value, mid_value, upper_value = last(lower), last(mid), last(upper)
    rsi_value, adx_value, volatility = last(rsi(close, RSI_PERIOD)), last(adx(m15)), last(atr(m15))
    percent_k, _ = stochastic(m15, RSI_PERIOD, 3)
    stochastic_value = last(percent_k)
    entry, last_candle = float(close.iloc[-1]), m15.iloc[-1]
    h4_fast, h4_slow = last(ema(h4.close, 50)), last(ema(h4.close, 200))
    h1_fast, h1_slow = last(ema(h1.close, 50)), last(ema(h1.close, 200))
    values = (lower_value, mid_value, upper_value, rsi_value, adx_value, volatility, stochastic_value, h4_fast, h4_slow, h1_fast, h1_slow)
    if any(value is None for value in values) or volatility <= 0:
        return hold(symbol, STRATEGY, "indicator unavailable")
    stretch = (entry - mid_value) / volatility  # mid_value is the SMA(MID_MA) bollinger midline
    checks = {
        SignalAction.BUY: (
            (f"close at or below the lower bollinger band ({BB_MULT:g} sigma)", entry <= lower_value, 25),
            (f"rsi at or below {BUY_RSI_MAX:g}", rsi_value <= BUY_RSI_MAX, 20),
            (f"price stretched {MIN_STRETCH_ATR:g} ATR below sma({MID_MA})", stretch <= -MIN_STRETCH_ATR, 20),
            (f"adx below {MAX_ADX:g}, no trend to fight", adx_value < MAX_ADX, 15),
            (f"stochastic oversold below {STOCH_OS:g}", stochastic_value <= STOCH_OS, 10),
            ("bullish reversal candle", last_candle.close > last_candle.open, 10),
        ),
        SignalAction.SELL: (
            (f"close at or above the upper bollinger band ({BB_MULT:g} sigma)", entry >= upper_value, 25),
            (f"rsi at or above {SELL_RSI_MIN:g}", rsi_value >= SELL_RSI_MIN, 20),
            (f"price stretched {MIN_STRETCH_ATR:g} ATR above sma({MID_MA})", stretch >= MIN_STRETCH_ATR, 20),
            (f"adx below {MAX_ADX:g}, no trend to fight", adx_value < MAX_ADX, 15),
            (f"stochastic overbought above {STOCH_OB:g}", stochastic_value >= STOCH_OB, 10),
            ("bearish reversal candle", last_candle.close < last_candle.open, 10),
        ),
    }
    failures = {action: [name for name, ok, _ in checks[action] if not ok] for action in checks}
    down_structure = h4_fast < h4_slow and h1_fast < h1_slow
    up_structure = h4_fast > h4_slow and h1_fast > h1_slow
    if not failures[SignalAction.BUY] and down_structure:
        return hold(symbol, STRATEGY, "long reversion blocked by the aligned h4/h1 downtrend structure")
    if not failures[SignalAction.SELL] and up_structure:
        return hold(symbol, STRATEGY, "short reversion blocked by the aligned h4/h1 uptrend structure")
    if adx_value >= MAX_ADX:
        return hold(symbol, STRATEGY, f"adx {adx_value:.1f} at or above {MAX_ADX:g}: trending regime, mean reversion skipped")
    action = min(checks, key=lambda candidate: len(failures[candidate]))
    if failures[action]:
        return hold(symbol, STRATEGY, f"mean reversion setup incomplete for {action.value}: {', '.join(failures[action])}")
    score = sum(weight for _, _, weight in checks[action])
    stop = entry - STOP_ATR * volatility if action is SignalAction.BUY else entry + STOP_ATR * volatility
    levels = bracket(action, entry, stop, rr)
    if levels is None:
        return hold(symbol, STRATEGY, "invalid stop placement")
    reasons = [f"{action.value} mean reversion confirmed on M15"] + [name for name, _, _ in checks[action]]
    reasons.append(f"adx {adx_value:.1f} below {MAX_ADX:g} and no aligned h4/h1 trend against the trade")
    reasons.append(f"stop {STOP_ATR:g} ATR from entry at {stop:.5f}, target honours {rr:g}R")
    return trade(symbol, STRATEGY, action, score, levels, reasons)
