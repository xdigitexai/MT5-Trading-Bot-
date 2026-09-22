"""Market data acquisition for the trading loop.

Candle handling is deliberately conservative:

- MT5 returns the newest bar first-class, and that bar may still be forming, so a bar is only
  usable once its own period has ended by the feed's newest timestamp. In practice that drops the
  newest bar and keeps one bar back, which is also safe for a feed that already omits it.
- A frame must carry at least ``MINIMUM_CANDLES`` completed bars of OHLC data with strictly
  increasing timestamps, no missing values and positive closes: the strategies need EMA200 and ATR
  warm-up, and anything less must fail closed rather than be traded on.
- Freshness is measured against the local UTC clock. MT5 reports *server* time, so a broker whose
  server clock is behind UTC by more than ``max_age_seconds`` needs that setting raised; the check
  errs towards blocking, never towards trading on stale data.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import pandas as pd
from app.core.clock import as_utc, epoch_to_utc
from app.mt5.timeframes import mt5_timeframe, timeframe_seconds

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("open", "high", "low", "close")
OPTIONAL_COLUMNS = ("time", "tick_volume")
MINIMUM_CANDLES = 210


@dataclass(frozen=True)
class TickSnapshot:
    bid: float
    ask: float
    time: datetime | None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    def spread_points(self, point: float) -> float:
        return (self.ask - self.bid) / point if point > 0 else 0.0


def _number(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _times(values: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(values):
        return pd.to_datetime(values, unit="s", utc=True)
    return pd.to_datetime(values, utc=True, errors="coerce")


def bars_to_frame(bars: object) -> pd.DataFrame | None:
    """MT5 candles as a typed frame, or None when the feed cannot be interpreted safely."""
    if bars is None:
        return None
    try:
        frame = bars.copy() if isinstance(bars, pd.DataFrame) else pd.DataFrame(bars)
    except (TypeError, ValueError) as error:
        logger.error("candle_feed_unusable error=%s", type(error).__name__)
        return None
    if frame.empty or "time" not in frame.columns or not set(REQUIRED_COLUMNS).issubset(frame.columns):
        return None
    columns = [name for name in (*OPTIONAL_COLUMNS, *REQUIRED_COLUMNS) if name in frame.columns]
    frame = frame.loc[:, columns].copy()
    frame["time"] = _times(frame["time"])
    if frame["time"].isna().any():
        return None
    for column in (*REQUIRED_COLUMNS, "tick_volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.reset_index(drop=True)


def completed_candles(
    gateway: object,
    symbol: str,
    timeframe: str,
    count: int,
    *,
    now: datetime | None = None,
    max_age_seconds: int = 7200,
) -> tuple[pd.DataFrame | None, str]:
    """Completed candles for one symbol/timeframe, or (None, reason) when anything is unverifiable."""
    interval = timeframe_seconds(timeframe)
    code = mt5_timeframe(timeframe)
    if interval is None or code is None:
        return None, f"unsupported timeframe {timeframe!r}"
    frame = bars_to_frame(gateway.rates(symbol, code, count))  # type: ignore[attr-defined]
    if frame is None:
        return None, "candle feed is unavailable or malformed"
    newest = frame["time"].iloc[-1]
    completed = frame[frame["time"] + timedelta(seconds=interval) <= newest]
    if completed.empty:
        return None, "the feed returned no completed candles"
    if len(completed) < MINIMUM_CANDLES:
        return None, f"only {len(completed)} completed candles were returned, {MINIMUM_CANDLES} required"
    if completed["time"].duplicated().any() or not completed["time"].is_monotonic_increasing:
        return None, "candle timestamps are not strictly increasing"
    tail = completed.tail(MINIMUM_CANDLES)
    if tail[list(REQUIRED_COLUMNS)].isna().any().any():
        return None, "completed candles contain missing OHLC values"
    if (tail["close"] <= 0).any():
        return None, "completed candles contain non-positive closes"
    if now is not None:
        last_close = completed["time"].iloc[-1] + timedelta(seconds=interval)
        age = (as_utc(now) - last_close).total_seconds()
        limit = interval + max_age_seconds
        if age > limit:
            return None, f"candles are {age:.0f}s old (limit {limit}s): market closed or feed stale"
    return completed.reset_index(drop=True), ""


def candle_close_time(frame: pd.DataFrame, timeframe: str) -> datetime | None:
    """The moment the newest completed bar's period ended; the identity stamp of its signal."""
    if frame is None or frame.empty:
        return None
    interval = timeframe_seconds(timeframe)
    if interval is None:
        return None
    return as_utc(frame["time"].iloc[-1].to_pydatetime()) + timedelta(seconds=interval)


def tick_snapshot(
    tick: object,
    spec,
    now: datetime | None = None,
    *,
    max_age_seconds: int = 7200,
) -> tuple[TickSnapshot | None, str]:
    """Validated bid/ask snapshot, or (None, reason) when the market cannot be priced."""
    if tick is None:
        return None, "no tick is available (market closed or feed unavailable)"
    bid, ask = _number(getattr(tick, "bid", None)), _number(getattr(tick, "ask", None))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None, "the tick carries no usable bid/ask"
    if ask < bid:
        return None, "the tick ask is below its bid"
    moment = epoch_to_utc(getattr(tick, "time", None))
    if moment is None:
        return None, "the tick carries no timestamp"
    if now is not None:
        age = (as_utc(now) - moment).total_seconds()
        if age > max_age_seconds:
            return None, f"the last tick is {age:.0f}s old (limit {max_age_seconds}s): market closed or feed stale"
    return TickSnapshot(bid, ask, moment), ""
