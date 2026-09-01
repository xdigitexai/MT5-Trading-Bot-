"""Legacy metrics + re-export expanded analytics."""
from dataclasses import dataclass
import numpy as np
from app.analytics.metrics import PerformanceReport, compute_performance

@dataclass(frozen=True)
class BacktestMetrics:
    net_profit: float
    gross_profit: float
    gross_loss: float
    win_rate: float
    profit_factor: float
    expectancy: float
    max_drawdown: float
    trades: int

def metrics(pnls: list[float]) -> BacktestMetrics:
    r = compute_performance(pnls)
    return BacktestMetrics(
        r.net_profit, r.gross_profit, r.gross_loss, r.win_rate,
        r.profit_factor if r.profit_factor != float("inf") else float("inf"),
        r.expectancy, r.max_drawdown, r.trades,
    )

def monte_carlo(pnls: list[float], iterations: int = 1000, seed: int = 7) -> dict:
    if not pnls or iterations < 1:
        return {"iterations": 0, "max_drawdown_range": None}
    rng = np.random.default_rng(seed)
    drawdowns = []
    for _ in range(iterations):
        drawdowns.append(metrics(rng.permutation(pnls).tolist()).max_drawdown)
    return {
        "iterations": iterations,
        "max_drawdown_range": [float(np.percentile(drawdowns, 5)), float(np.percentile(drawdowns, 95))],
    }
