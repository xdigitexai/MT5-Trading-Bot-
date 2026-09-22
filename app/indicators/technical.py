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
def sma(close: pd.Series, period: int) -> pd.Series: return close.rolling(period).mean()
def bollinger(close: pd.Series, period: int = 20, mult: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, period); width = close.rolling(period).std(ddof=0) * mult
    return mid - width, mid, mid + width
def roc(close: pd.Series, period: int) -> pd.Series: return (close / close.shift(period) - 1) * 100
def donchian(frame: pd.DataFrame, period: int) -> tuple[pd.Series, pd.Series]:
    return frame.high.rolling(period).max(), frame.low.rolling(period).min()
def stochastic(frame: pd.DataFrame, k: int = 14, d: int = 3) -> tuple[pd.Series, pd.Series]:
    lowest, highest = frame.low.rolling(k).min(), frame.high.rolling(k).max()
    percent_k = 100 * (frame.close - lowest) / (highest - lowest).replace(0, np.nan)
    return percent_k, percent_k.rolling(d).mean()
def atr_percentile(frame: pd.DataFrame, period: int = 14, lookback: int = 100) -> pd.Series:
    # Percentile rank of the current ATR inside its own lookback window; 1.0 is the window's most volatile point.
    return atr(frame, period).rolling(lookback).rank(pct=True)
def volume_series(frame: pd.DataFrame) -> pd.Series:
    # NaN series when the feed carries neither volume column, so callers can detect an absent feed.
    for column in ("volume", "tick_volume"):
        if column in frame.columns: return frame[column].astype(float)
    return pd.Series(np.nan, index=frame.index, dtype=float)
