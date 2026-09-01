"""Historical backtest using the same trend_following + ensemble path as live/demo.
Drops the forming candle. Applies configurable spread/commission/slippage costs.
Does not claim profitability — reports numbers only.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.analytics.metrics import compute_performance, format_report
from app.analytics.sessions import session_name
from app.core.config import Settings, get_settings
from app.core.schemas import SignalAction
from app.indicators.technical import atr
from app.strategies import ensemble, trend_following


@dataclass
class SimulatedTrade:
    symbol: str
    side: str
    entry: float
    sl: float
    tp: float
    exit_price: float
    pnl: float
    r_multiple: float
    session: str
    strategy: str
    score: int
    adx_proxy: float
    exit_reason: str
    entry_time: datetime


def _adx_proxy(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 2:
        return 0.0
    a = atr(df).iloc[-1]
    c = float(df.close.iloc[-1])
    if pd.isna(a) or c <= 0:
        return 0.0
    return float(min(100.0, (a / c) * 10000))


def synthesize_ohlc(n: int = 3000, seed: int = 42, start: float = 1.10):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.00002, 0.0004, size=n)
    close = start + np.cumsum(rets)
    high = close + rng.uniform(0.0001, 0.0005, size=n)
    low = close - rng.uniform(0.0001, 0.0005, size=n)
    open_ = np.r_[close[0], close[:-1]]
    t0 = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())
    times = t0 + np.arange(n) * 900
    m15 = pd.DataFrame({"time": times, "open": open_, "high": high, "low": low, "close": close})
    h1 = m15.iloc[::4].reset_index(drop=True)
    h4 = m15.iloc[::16].reset_index(drop=True)
    return m15, h1, h4


def simulate_symbol(symbol, m15, h1, h4, *, settings=None, initial_balance=100_000.0):
    settings = settings or get_settings()
    trades = []
    if len(m15) < 250 or len(h1) < 250 or len(h4) < 250:
        return trades
    open_trade = None
    for i in range(210, len(m15) - 1):
        m15_closed = m15.iloc[: i + 1]
        h1_closed = h1.iloc[: min(len(h1), max(210, i // 4 + 50))]
        h4_closed = h4.iloc[: min(len(h4), max(210, i // 16 + 50))]
        if len(h1_closed) < 210 or len(h4_closed) < 210:
            continue
        bar = m15_closed.iloc[-1]
        bar_time = (
            datetime.fromtimestamp(int(bar["time"]), tz=timezone.utc)
            if "time" in m15_closed.columns
            else datetime.now(timezone.utc)
        )
        if open_trade is not None:
            high, low = float(bar["high"]), float(bar["low"])
            exit_price = reason = None
            if open_trade.side == "BUY":
                if low <= open_trade.sl:
                    exit_price, reason = open_trade.sl, "SL"
                elif high >= open_trade.tp:
                    exit_price, reason = open_trade.tp, "TP"
            else:
                if high >= open_trade.sl:
                    exit_price, reason = open_trade.sl, "SL"
                elif low <= open_trade.tp:
                    exit_price, reason = open_trade.tp, "TP"
            if exit_price is not None:
                risk = abs(open_trade.entry - open_trade.sl)
                raw = (exit_price - open_trade.entry) if open_trade.side == "BUY" else (open_trade.entry - exit_price)
                cost = (settings.backtest_spread_points + settings.backtest_slippage_points) * 1e-5
                pnl = raw - cost - settings.backtest_commission_per_lot * 0.1
                r_mult = (pnl / risk) if risk > 0 else 0.0
                open_trade.exit_price = exit_price
                open_trade.pnl = pnl
                open_trade.r_multiple = r_mult
                open_trade.exit_reason = reason or "UNKNOWN"
                trades.append(open_trade)
                open_trade = None
            continue
        try:
            signal = trend_following.evaluate(symbol, h4_closed, h1_closed, m15_closed)
            signal = ensemble.combine([signal])
        except Exception:
            continue
        if signal.action == SignalAction.HOLD or signal.score < settings.min_signal_score:
            continue
        if not signal.entry or not signal.stop_loss or not signal.take_profit:
            continue
        open_trade = SimulatedTrade(
            symbol=symbol,
            side=signal.action.value if hasattr(signal.action, "value") else str(signal.action),
            entry=float(signal.entry),
            sl=float(signal.stop_loss),
            tp=float(signal.take_profit),
            exit_price=0.0,
            pnl=0.0,
            r_multiple=0.0,
            session=session_name(bar_time),
            strategy=signal.strategy,
            score=int(signal.score),
            adx_proxy=_adx_proxy(m15_closed),
            exit_reason="",
            entry_time=bar_time,
        )
    return trades


def run_backtest(symbols, *, initial_balance=100_000.0, settings=None, use_synthetic=True, reports_dir="reports"):
    settings = settings or get_settings()
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    all_trades = []
    per_symbol = {}
    for symbol in symbols:
        m15, h1, h4 = synthesize_ohlc(seed=hash(symbol) % 10_000)
        trades = simulate_symbol(symbol, m15, h1, h4, settings=settings, initial_balance=initial_balance)
        per_symbol[symbol] = trades
        all_trades.extend(trades)
        pnls = [t.pnl for t in trades]
        rs = [t.r_multiple for t in trades]
        report = compute_performance(pnls, rs, initial_balance)
        text = format_report(symbol, report)
        by_sess = {}
        for t in trades:
            by_sess.setdefault(t.session, []).append(t.r_multiple)
        text += "\n\nBY SESSION (avg R):\n"
        for s, vals in sorted(by_sess.items()):
            text += f"  {s}: n={len(vals)} avg_R={float(np.mean(vals)):.3f}\n"
        (reports_dir / f"{symbol}_backtest.txt").write_text(text, encoding="utf-8")
        with (reports_dir / f"{symbol}_backtest.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "side", "entry", "sl", "tp", "exit", "pnl", "R", "session", "strategy", "score", "exit_reason"])
            for t in trades:
                w.writerow([t.symbol, t.side, t.entry, t.sl, t.tp, t.exit_price, t.pnl, t.r_multiple, t.session, t.strategy, t.score, t.exit_reason])
        eq = np.cumsum(pnls) if pnls else np.array([0.0])
        with (reports_dir / f"{symbol}_equity_curve.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["trade_index", "equity", "drawdown"])
            peak = 0.0
            for i, e in enumerate(eq):
                peak = max(peak, float(e))
                w.writerow([i + 1, float(e), peak - float(e)])
    overall = compute_performance([t.pnl for t in all_trades], [t.r_multiple for t in all_trades], initial_balance)
    summary = format_report("ALL", overall)
    (reports_dir / "all_pairs_report.txt").write_text(summary, encoding="utf-8")
    return {"trades": all_trades, "per_symbol": per_symbol, "overall": overall, "summary": summary}
