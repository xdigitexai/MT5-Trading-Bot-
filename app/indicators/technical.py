import numpy as np
import pandas as pd

def ema(close: pd.Series, period: int) -> pd.Series: return close.ewm(span=period, adjust=False).mean()
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    diff = close.diff(); up = diff.clip(lower=0); down = -diff.clip(upper=0)
    rs = up.ewm(alpha=1 / period, adjust=False).mean() / down.ewm(alpha=1 / period, adjust=False).mean().replace(0, np.nan)
    return 100 - (100 / (1 + rs))
def macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, 12) - ema(close, 26); signal = ema(line, 9)
    return line, signal, line - signal
def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous = frame.close.shift(1)
    tr = pd.concat([frame.high - frame.low, (frame.high - previous).abs(), (frame.low - previous).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()
def adx(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    # A lightweight ADX sufficient for regime filtering; warmup values remain NaN.
    up, down = frame.high.diff(), -frame.low.diff()
    plus = up.where((up > down) & (up > 0), 0.0); minus = down.where((down > up) & (down > 0), 0.0)
    value_atr = atr(frame, period).replace(0, np.nan)
    pdi = 100 * plus.ewm(alpha=1/period, adjust=False).mean() / value_atr
    mdi = 100 * minus.ewm(alpha=1/period, adjust=False).mean() / value_atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/period, adjust=False).mean()
