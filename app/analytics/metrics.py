"""Expanded performance metrics for backtests and journal analysis."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math
import numpy as np


@dataclass
class PerformanceReport:
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    net_profit: float
    profit_factor: float
    expectancy: float
    average_win: float
    average_loss: float
    max_drawdown: float
    max_drawdown_pct: float
    average_r: float
    total_r: float
    sharpe_like: float
    longest_losing_streak: int
    longest_winning_streak: int

    def to_dict(self) -> dict:
        return asdict(self)


def _streaks(pnls: list[float]) -> tuple[int, int]:
    max_lose = max_win = cur_l = cur_w = 0
    for p in pnls:
        if p > 0:
            cur_w += 1
            cur_l = 0
            max_win = max(max_win, cur_w)
        elif p < 0:
            cur_l += 1
            cur_w = 0
            max_lose = max(max_lose, cur_l)
        else:
            cur_l = cur_w = 0
    return max_lose, max_win


def compute_performance(pnls: list[float], r_multiples: list[float] | None = None, initial_balance: float = 100_000.0) -> PerformanceReport:
    values = np.asarray(pnls, dtype=float)
    if len(values) == 0:
        return PerformanceReport(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    wins = values[values > 0]
    losses = values[values < 0]
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    equity = np.cumsum(values)
    peaks = np.maximum.accumulate(np.r_[0.0, equity])[1:]
    dd = peaks - equity
    max_dd = float(dd.max()) if len(dd) else 0.0
    peak_for_pct = float(peaks[np.argmax(dd)]) if len(dd) else initial_balance
    max_dd_pct = (max_dd / peak_for_pct * 100.0) if peak_for_pct > 0 else 0.0
    r = np.asarray(r_multiples if r_multiples is not None else pnls, dtype=float)
    avg_r = float(r.mean()) if len(r) else 0.0
    total_r = float(r.sum()) if len(r) else 0.0
    std = float(values.std(ddof=1)) if len(values) > 1 else 0.0
    sharpe = float(values.mean() / std * math.sqrt(252)) if std > 1e-12 else 0.0
    lose_s, win_s = _streaks(values.tolist())
    return PerformanceReport(
        trades=len(values),
        wins=int((values > 0).sum()),
        losses=int((values < 0).sum()),
        win_rate=float((values > 0).mean()),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_profit=float(values.sum()),
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        expectancy=float(values.mean()),
        average_win=float(wins.mean()) if len(wins) else 0.0,
        average_loss=float(losses.mean()) if len(losses) else 0.0,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        average_r=avg_r,
        total_r=total_r,
        sharpe_like=sharpe,
        longest_losing_streak=lose_s,
        longest_winning_streak=win_s,
    )


def format_report(symbol: str, report: PerformanceReport) -> str:
    pf = f"{report.profit_factor:.2f}" if math.isfinite(report.profit_factor) else "inf"
    lines = [
        f"{symbol}",
        "--------------------------------",
        f"Trades: {report.trades}",
        f"Wins: {report.wins}  Losses: {report.losses}",
        f"Win Rate: {report.win_rate * 100:.1f}%",
        f"Gross Profit: {report.gross_profit:.2f}",
        f"Gross Loss: {report.gross_loss:.2f}",
        f"Net Profit: {report.net_profit:.2f}",
        f"Profit Factor: {pf}",
        f"Expectancy: {report.expectancy:.4f}",
        f"Average Win: {report.average_win:.2f}",
        f"Average Loss: {report.average_loss:.2f}",
        f"Max Drawdown: {report.max_drawdown:.2f} ({report.max_drawdown_pct:.1f}%)",
        f"Average R: {report.average_r:.3f}",
        f"Total R: {report.total_r:.2f}",
        f"Sharpe-like: {report.sharpe_like:.3f}",
        f"Longest Losing Streak: {report.longest_losing_streak}",
        f"Longest Winning Streak: {report.longest_winning_streak}",
    ]
    return "\n".join(lines)
