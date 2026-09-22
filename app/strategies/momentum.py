import pandas as pd
from app.core.schemas import Signal, SignalAction
from app.indicators.technical import atr, ema, macd, roc, rsi, sma
from app.strategies._common import bracket, hold, invalid_frames, last, trade

STRATEGY = "momentum"
MIN_CANDLES = 120
FAST_MA, SLOW_MA = 20, 50
RSI_PERIOD, ROC_PERIOD = 14, 10
BUY_RSI = (52.0, 70.0)     # momentum band, deliberately below the overbought extreme
SELL_RSI = (30.0, 48.0)
OVERBOUGHT, OVERSOLD = 72.0, 28.0
MAX_STRETCH_ATR = 3.0      # price this far from its moving average is not entered
MIN_ROC_PCT = 0.05         # percent of change over ROC_PERIOD
STOP_ATR = 1.5

def evaluate(symbol: str, h4: pd.DataFrame, h1: pd.DataFrame, m15: pd.DataFrame, rr: float = 2.0) -> Signal:
    """Momentum continuation on M15 from RSI, MACD, rate of change and moving averages.

    Idea: only join momentum that is aligned and still has room to run. Confirmation rules: price is
    above (below) a rising (falling) SMA(50) and above (below) SMA(20), MACD sits on the right side of
    its signal and the histogram is expanding, RSI is inside a momentum band that stops short of the
    extremes, ROC(10) points the right way, and H1 EMA(50) agrees. Overextension is refused outright:
    an RSI at the 72/28 extreme or a price more than 3 ATR from its mean returns HOLD. Uses only
    completed rows.
    """
    if not symbol or rr < 1:
        return hold(symbol, STRATEGY, "invalid strategy input")
    if invalid_frames((h4, h1, m15), MIN_CANDLES):
        return hold(symbol, STRATEGY, "insufficient or invalid candles")
    close = m15.close
    fast_series, slow_series = sma(close, FAST_MA), sma(close, SLOW_MA)
    line, signal_line, histogram = macd(close)
    fast, slow = last(fast_series), last(slow_series)
    slow_previous = last(slow_series.iloc[:-5]) if len(slow_series) > 5 else None
    line_value, signal_value = last(line), last(signal_line)
    histogram_value, histogram_previous = last(histogram), last(histogram.iloc[:-1])
    rsi_value, roc_value, volatility = last(rsi(close, RSI_PERIOD)), last(roc(close, ROC_PERIOD)), last(atr(m15))
    entry, h1_close = float(close.iloc[-1]), float(h1.close.iloc[-1])
    h1_ema = last(ema(h1.close, SLOW_MA))
    values = (fast, slow, slow_previous, line_value, signal_value, histogram_value, histogram_previous, rsi_value, roc_value, volatility, h1_ema)
    if any(value is None for value in values) or volatility <= 0:
        return hold(symbol, STRATEGY, "indicator unavailable")
    stretch = (entry - fast) / volatility
    if rsi_value >= OVERBOUGHT or stretch >= MAX_STRETCH_ATR:
        return hold(symbol, STRATEGY, f"overextended upside (rsi {rsi_value:.1f}, {stretch:.1f} ATR from sma{FAST_MA}), entry avoided")
    if rsi_value <= OVERSOLD or stretch <= -MAX_STRETCH_ATR:
        return hold(symbol, STRATEGY, f"overextended downside (rsi {rsi_value:.1f}, {stretch:.1f} ATR from sma{FAST_MA}), entry avoided")
    checks = {
        SignalAction.BUY: (
            (f"price above a rising sma({SLOW_MA})", slow_previous < slow < entry, 25),
            (f"price above sma({FAST_MA})", entry > fast, 10),
            ("macd above its signal and rising", line_value > signal_value and histogram_value > histogram_previous, 25),
            (f"rsi inside the {BUY_RSI[0]:g}-{BUY_RSI[1]:g} momentum band", BUY_RSI[0] <= rsi_value <= BUY_RSI[1], 20),
            (f"roc({ROC_PERIOD}) above {MIN_ROC_PCT:g}%", roc_value >= MIN_ROC_PCT, 10),
            (f"h1 ema({SLOW_MA}) agrees", h1_close > h1_ema, 10),
        ),
        SignalAction.SELL: (
            (f"price below a falling sma({SLOW_MA})", entry < slow < slow_previous, 25),
            (f"price below sma({FAST_MA})", entry < fast, 10),
            ("macd below its signal and falling", line_value < signal_value and histogram_value < histogram_previous, 25),
            (f"rsi inside the {SELL_RSI[0]:g}-{SELL_RSI[1]:g} momentum band", SELL_RSI[0] <= rsi_value <= SELL_RSI[1], 20),
            (f"roc({ROC_PERIOD}) below -{MIN_ROC_PCT:g}%", roc_value <= -MIN_ROC_PCT, 10),
            (f"h1 ema({SLOW_MA}) agrees", h1_close < h1_ema, 10),
        ),
    }
    action = min(checks, key=lambda candidate: sum(not ok for _, ok, _ in checks[candidate]))
    failed = [name for name, ok, _ in checks[action] if not ok]
    if failed:
        return hold(symbol, STRATEGY, f"momentum confirmation incomplete for {action.value}: {', '.join(failed)}")
    score = sum(weight for _, _, weight in checks[action])
    stop = entry - STOP_ATR * volatility if action is SignalAction.BUY else entry + STOP_ATR * volatility
    levels = bracket(action, entry, stop, rr)
    if levels is None:
        return hold(symbol, STRATEGY, "invalid stop placement")
    reasons = [f"{action.value} momentum confirmed on M15"] + [name for name, _, _ in checks[action]]
    reasons.append(f"stop {STOP_ATR:g} ATR from entry at {stop:.5f}, target honours {rr:g}R")
    return trade(symbol, STRATEGY, action, score, levels, reasons)
