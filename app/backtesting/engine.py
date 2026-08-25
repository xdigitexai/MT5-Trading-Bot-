from dataclasses import dataclass
import numpy as np

@dataclass(frozen=True)
class BacktestMetrics:
    net_profit: float; gross_profit: float; gross_loss: float; win_rate: float; profit_factor: float; expectancy: float; max_drawdown: float; trades: int

def metrics(pnls: list[float]) -> BacktestMetrics:
    values = np.asarray(pnls, dtype=float)
    if not len(values): return BacktestMetrics(0,0,0,0,0,0,0,0)
    gross_profit=float(values[values>0].sum()); gross_loss=float(-values[values<0].sum()); equity=np.cumsum(values); peaks=np.maximum.accumulate(np.r_[0,equity])[1:]
    return BacktestMetrics(float(values.sum()),gross_profit,gross_loss,float((values>0).mean()),gross_profit/gross_loss if gross_loss else float("inf"),float(values.mean()),float((peaks-equity).max()),len(values))

def monte_carlo(pnls: list[float], iterations: int = 1000, seed: int = 7) -> dict:
    if not pnls or iterations < 1: return {"iterations":0,"max_drawdown_range":None}
    rng=np.random.default_rng(seed); drawdowns=[]
    for _ in range(iterations): drawdowns.append(metrics(rng.permutation(pnls).tolist()).max_drawdown)
    return {"iterations":iterations,"max_drawdown_range":[float(np.percentile(drawdowns,5)),float(np.percentile(drawdowns,95))]}
