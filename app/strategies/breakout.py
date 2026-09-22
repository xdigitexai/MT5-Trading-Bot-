import pandas as pd
from app.core.schemas import Signal, SignalAction
from app.indicators.technical import atr, donchian, ema, sma, volume_series
from app.strategies._common import bracket, hold, invalid_frames, last, trade

STRATEGY = "breakout"
RANGE_PERIOD = 20        # completed candles that define support/resistance
COOLDOWN_BARS = 5        # a range broken inside this window is already extended
BUFFER_ATR = 0.25        # the close must clear the level by this multiple of ATR
MIN_BODY_ATR = 0.30      # the candle body must be a real break, not a wick
CLOSE_POSITION = 0.60    # the close must finish in the outer 40% of the bar range
MAX_ATR_STRETCH = 2.5    # refuse breakouts taken while ATR is far above its own norm
STOP_ATR = 0.80          # floor for the distance between entry and stop
MIN_CANDLES = 60

def evaluate(symbol: str, h4: pd.DataFrame, h1: pd.DataFrame, m15: pd.DataFrame, rr: float = 2.0) -> Signal:
    """Donchian range breakout on M15 with explicit false-breakout protection.

    Idea: a new extreme is not a trade. Only a FRESH close beyond the prior 20-candle range counts,
    and only when a level was not already broken during the cooldown window, so sustained trends do
    not re-fire on every bar. Confirmation rules: the close clears the level by an ATR buffer, the
    candle body reaches a third of ATR, the close finishes in the outer 40% of the bar (a wick alone
    is rejected), volume or tick volume expands when the feed supplies it, H1 EMA(50) agrees with the
    direction, and ATR is not stretched far beyond its recent norm. Uses only completed rows.
    """
    if not symbol or rr < 1:
        return hold(symbol, STRATEGY, "invalid strategy input")
    if invalid_frames((h4, h1, m15), MIN_CANDLES):
        return hold(symbol, STRATEGY, "insufficient or invalid candles")
    atr_series = atr(m15)
    average_atr, volatility = last(sma(atr_series, 50)), last(atr_series)
    range_high, range_low = (level.shift(1) for level in donchian(m15, RANGE_PERIOD))
    resistance, support = last(range_high), last(range_low)
    if None in (volatility, resistance, support) or volatility <= 0:
        return hold(symbol, STRATEGY, "indicator unavailable")
    buffer = volatility * BUFFER_ATR
    close_above, close_below = m15.close > range_high + buffer, m15.close < range_low - buffer
    fresh_up = bool(close_above.iloc[-1]) and not close_above.iloc[-1 - COOLDOWN_BARS:-1].any()
    fresh_down = bool(close_below.iloc[-1]) and not close_below.iloc[-1 - COOLDOWN_BARS:-1].any()
    if not (fresh_up or fresh_down):
        return hold(symbol, STRATEGY, f"no fresh close beyond the prior {RANGE_PERIOD}-candle range")
    last_candle, bar_range = m15.iloc[-1], float(m15.high.iloc[-1] - m15.low.iloc[-1])
    body = abs(float(last_candle.close - last_candle.open))
    close_top = (float(last_candle.close - last_candle.low) / bar_range) if bar_range > 0 else 0.0
    close_bottom = (float(last_candle.high - last_candle.close) / bar_range) if bar_range > 0 else 0.0
    bullish = fresh_up and last_candle.close > last_candle.open and body >= MIN_BODY_ATR * volatility and close_top >= CLOSE_POSITION
    bearish = fresh_down and last_candle.close < last_candle.open and body >= MIN_BODY_ATR * volatility and close_bottom >= CLOSE_POSITION
    if not (bullish or bearish):
        return hold(symbol, STRATEGY, "breakout candle not confirmed: body or close position fails the wick filter")
    volume = volume_series(m15)
    recent_volume = volume.tail(RANGE_PERIOD)
    feed_available = bool(recent_volume.notna().any())
    average_volume, latest_volume = float(recent_volume.mean()) if feed_available else None, last(volume)
    volume_confirmed = (not feed_available) or (average_volume > 0 and latest_volume is not None and latest_volume >= average_volume)
    if not volume_confirmed:
        return hold(symbol, STRATEGY, "volume below its recent average, breakout participation missing")
    stretched = average_atr is not None and average_atr > 0 and volatility > MAX_ATR_STRETCH * average_atr
    if stretched:
        return hold(symbol, STRATEGY, "atr far above its recent norm, breakout not traded")
    h1_close, h1_ema = float(h1.close.iloc[-1]), last(ema(h1.close, 50))
    if h1_ema is None:
        return hold(symbol, STRATEGY, "indicator unavailable")
    action = SignalAction.BUY if bullish else SignalAction.SELL
    agrees = h1_close > h1_ema if bullish else h1_close < h1_ema
    if not agrees:
        return hold(symbol, STRATEGY, "h1 ema(50) disagrees with the breakout direction")
    entry = float(last_candle.close)
    stop = min(resistance - buffer, entry - STOP_ATR * volatility) if bullish else max(support + buffer, entry + STOP_ATR * volatility)
    levels = bracket(action, entry, stop, rr)
    if levels is None:
        return hold(symbol, STRATEGY, "invalid stop placement")
    score = 35  # fresh, ATR-buffered break of the prior range
    score += 15  # candle body and close position pass the wick filter
    score += 10  # volatility not stretched beyond its recent norm
    score += 15 if (feed_available and volume_confirmed) else 5  # volume expansion, or a feed without volume
    score += 15  # h1 ema(50) agreement
    reasons = [
        f"close broke the prior {RANGE_PERIOD}-candle {'resistance' if bullish else 'support'} by more than {BUFFER_ATR:g} ATR",
        f"candle body and close position confirm the {action.value.lower()} break, no {COOLDOWN_BARS}-bar repeat",
        "volume expansion confirmed" if feed_available else "volume feed unavailable, participation not measurable",
        "h1 ema(50) agrees with the breakout direction",
        f"stop sits behind the broken level at {stop:.5f}, target honours {rr:g}R",
    ]
    return trade(symbol, STRATEGY, action, score, levels, reasons)
