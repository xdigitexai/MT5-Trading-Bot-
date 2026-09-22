"""Descriptive analytics over reconciled closed trades.

Only trades that MT5 has confirmed as realized are counted: a trade needs a close time and a
reconciliation stamp, which means the broker reported the outcome. An order the bot sent is not a
result until then, so nothing here is extrapolated from open positions.

Net realized P/L per trade is ``profit + commission + swap``, because MT5 reports the broker's
profit and the trading costs separately. Win/loss/break-even classification and every aggregate
below use that net value, so a gross-profitable trade whose costs exceed its profit counts as a
loss. ``gross_profit`` and ``gross_loss`` are reported as magnitudes (the inputs of the profit
factor), while the per-trade averages and extremes keep the sign of the P/L they describe:
``average_loss`` and ``largest_loss`` are negative. Drawdown figures are measured in account
currency on the closed-trade P/L curve ordered by close time, starting from zero.

Expectancy and profit factor are descriptive statistics of the closed sample, NOT a forecast:
they describe what the recorded sample did, they do not predict future profit, and a sample
without losers produces no profit factor at all rather than a division by zero.
"""
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.clock import as_utc, utcnow
from app.database.base import TradeRecord

REALIZED_STATUSES = ("CLOSED", "PARTIALLY_CLOSED")
EMPTY_STATE = "no reconciled closed trades yet"
NO_FORECAST_NOTE = "expectancy and profit factor describe the recorded closed sample; they are not a profit forecast"


def net_pnl(trade: TradeRecord) -> float:
    """Realized result of one closed trade in account currency, costs included."""
    return float(trade.profit or 0.0) + float(trade.commission or 0.0) + float(trade.swap or 0.0)


def realized_trades(db: Session) -> list[TradeRecord]:
    """Closed trades MT5 has confirmed, oldest close first so the equity curve is ordered."""
    statement = (
        select(TradeRecord)
        .where(TradeRecord.status.in_(REALIZED_STATUSES), TradeRecord.close_time.is_not(None), TradeRecord.reconciled_at.is_not(None))
        .order_by(TradeRecord.close_time.asc(), TradeRecord.id.asc())
    )
    return list(db.scalars(statement))


def drawdown(values: list[float]) -> tuple[float, float]:
    """(current, maximum) closed-trade drawdown in account currency, peaks measured from zero."""
    equity = peak = maximum = current = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        current = peak - equity
        maximum = max(maximum, current)
    return current, maximum


def profit_factor(gross_profit: float, gross_loss: float) -> tuple[float | None, str | None]:
    """Profit factor, or (None, reason) when it is undefined; never a division by zero."""
    if gross_loss > 0:
        return gross_profit / gross_loss, None
    if gross_profit > 0:
        return None, "no losing trades: profit factor is undefined (gross loss is zero)"
    return None, "no realized profit or loss: profit factor is undefined"


def average_risk_reward(trades: list[TradeRecord]) -> float | None:
    """Mean planned reward/risk of the closed trades whose bracket is known."""
    ratios = []
    for trade in trades:
        entry = trade.executed_price if trade.executed_price is not None else trade.requested_price
        if entry is None or not trade.stop_loss or trade.take_profit is None:
            continue
        risk = abs(float(entry) - float(trade.stop_loss))
        if risk <= 0:
            continue
        ratios.append(abs(float(trade.take_profit) - float(entry)) / risk)
    return sum(ratios) / len(ratios) if ratios else None


def _summary(trades: list[TradeRecord]) -> dict:
    results = [net_pnl(trade) for trade in trades]
    wins = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    factor, factor_reason = profit_factor(sum(wins), -sum(losses))
    return {
        "trades": len(results),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": len(results) - len(wins) - len(losses),
        "gross_profit": sum(wins),
        "gross_loss": -sum(losses),
        "net_pnl": sum(results),
        "win_rate": len(wins) / len(results) if results else None,
        "profit_factor": factor,
        "profit_factor_state": factor_reason,
    }


def _group(trades: list[TradeRecord], attribute: str) -> dict[str, dict]:
    buckets: dict[str, list[TradeRecord]] = {}
    for trade in trades:
        buckets.setdefault(str(getattr(trade, attribute) or "unknown"), []).append(trade)
    return {name: _summary(rows) for name, rows in sorted(buckets.items())}


def performance(db: Session) -> dict:
    """Outcome metrics of the reconciled closed trades, with an explicit empty state."""
    trades = realized_trades(db)
    results = [net_pnl(trade) for trade in trades]
    wins = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    factor, factor_reason = profit_factor(gross_profit, gross_loss)
    if not trades:
        factor_reason = "no reconciled closed trades: profit factor is undefined"
    current, maximum = drawdown(results)
    return {
        "state": EMPTY_STATE if not trades else "reconciled closed trades",
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": len(results) - len(wins) - len(losses),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_realized_pnl": sum(results),
        "average_win": gross_profit / len(wins) if wins else None,
        "average_loss": -gross_loss / len(losses) if losses else None,
        "largest_win": max(wins) if wins else None,
        "largest_loss": min(losses) if losses else None,
        "profit_factor": factor,
        "profit_factor_state": factor_reason,
        "expectancy": sum(results) / len(results) if results else None,
        "current_drawdown": current,
        "maximum_drawdown": maximum,
        "notes": [NO_FORECAST_NOTE],
    }


def statistics(db: Session) -> dict:
    """Win/loss statistics of the reconciled closed trades, grouped by strategy and symbol."""
    trades = realized_trades(db)
    results = [net_pnl(trade) for trade in trades]
    wins = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    factor, factor_reason = profit_factor(sum(wins), -sum(losses))
    if not trades:
        factor_reason = "no reconciled closed trades: profit factor is undefined"
    today = utcnow().date()
    today_trades = [trade for trade in trades if _close_day(trade) == today]
    return {
        "state": EMPTY_STATE if not trades else "reconciled closed trades",
        "total_closed_trades": len(trades),
        "win_rate": len(wins) / len(results) if results else None,
        "loss_rate": len(losses) / len(results) if results else None,
        "profit_factor": factor,
        "profit_factor_state": factor_reason,
        "maximum_drawdown": drawdown(results)[1],
        "average_risk_reward": average_risk_reward(trades),
        "today_trades": len(today_trades),
        "today_realized_pnl": sum(net_pnl(trade) for trade in today_trades),
        "by_strategy": _group(trades, "strategy"),
        "by_symbol": _group(trades, "symbol"),
        "notes": [NO_FORECAST_NOTE],
    }


def _close_day(trade: TradeRecord):
    moment: datetime | None = as_utc(trade.close_time)
    return None if moment is None else moment.astimezone(timezone.utc).date()
