"""Analytics must describe exactly the reconciled closed trades, and admit when there are none."""
from datetime import datetime, timedelta, timezone

import pytest

from app.database.base import TradeRecord
from app.services.analytics import EMPTY_STATE, net_pnl, performance, statistics


def closed_trade(db, trade_id: str, *, profit: float, close_time: datetime, symbol: str = "EURUSD", strategy: str = "trend_following", commission: float = 0.0, swap: float = 0.0, entry: float | None = 1.1, stop_loss: float = 1.09, take_profit: float | None = 1.12, status: str = "CLOSED", reconciled: bool = True) -> TradeRecord:
    row = TradeRecord(
        trade_id=trade_id, symbol=symbol, side="BUY", volume=0.1, status=status, stop_loss=stop_loss,
        take_profit=take_profit, signal_score=80, strategy=strategy, requested_price=entry,
        executed_price=entry, profit=profit, commission=commission, swap=swap,
        open_time=(close_time - timedelta(hours=1)) if close_time else None, close_time=close_time,
        mt5_deal_ticket=f"deal-{trade_id}", mt5_position_ticket=f"pos-{trade_id}",
        reconciled_at=close_time if reconciled else None, close_reason="take_profit",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def book(db):
    """Six closed trades: three winners, two losers and one break-even, in close-time order."""
    now = datetime.now(timezone.utc)
    closed_trade(db, "A", profit=-50.0, close_time=now - timedelta(days=40))
    closed_trade(db, "B", profit=-20.0, close_time=now - timedelta(days=39))
    closed_trade(db, "C", profit=100.0, close_time=now, strategy="breakout")
    closed_trade(db, "D", profit=50.0, commission=-10.0, close_time=now, symbol="GBPUSD", strategy="breakout")
    closed_trade(db, "E", profit=25.0, swap=5.0, close_time=now - timedelta(days=38), symbol="GBPUSD")
    closed_trade(db, "F", profit=0.0, close_time=now - timedelta(days=37), symbol="GBPUSD", take_profit=None)
    return db


def test_performance_matches_the_hand_built_closed_trade_set(book):
    report = performance(book)

    assert report["state"] == "reconciled closed trades"
    assert (report["total_trades"], report["winning_trades"], report["losing_trades"], report["breakeven_trades"]) == (6, 3, 2, 1)
    assert report["gross_profit"] == pytest.approx(170.0)
    assert report["gross_loss"] == pytest.approx(70.0)
    assert report["net_realized_pnl"] == pytest.approx(100.0)
    assert report["average_win"] == pytest.approx(170.0 / 3)
    assert report["average_loss"] == pytest.approx(-35.0)
    assert report["largest_win"] == pytest.approx(100.0)
    assert report["largest_loss"] == pytest.approx(-50.0)
    assert report["profit_factor"] == pytest.approx(170.0 / 70.0)
    assert report["profit_factor_state"] is None
    assert report["expectancy"] == pytest.approx(100.0 / 6)
    assert report["current_drawdown"] == pytest.approx(0.0)
    assert report["maximum_drawdown"] == pytest.approx(70.0)
    assert any("not a profit forecast" in note for note in report["notes"])


def test_statistics_report_win_rates_today_and_groupings(book):
    report = statistics(book)

    assert report["total_closed_trades"] == 6
    assert report["win_rate"] == pytest.approx(0.5)
    assert report["loss_rate"] == pytest.approx(1 / 3)
    assert report["profit_factor"] == pytest.approx(170.0 / 70.0)
    assert report["maximum_drawdown"] == pytest.approx(70.0)
    assert report["average_risk_reward"] == pytest.approx(2.0)  # five trades carry a 2:1 bracket
    assert report["today_trades"] == 2
    assert report["today_realized_pnl"] == pytest.approx(140.0)

    strategy = report["by_strategy"]
    assert set(strategy) == {"breakout", "trend_following"}
    assert strategy["breakout"] == {
        "trades": 2, "winning_trades": 2, "losing_trades": 0, "breakeven_trades": 0,
        "gross_profit": pytest.approx(140.0), "gross_loss": pytest.approx(0.0), "net_pnl": pytest.approx(140.0),
        "win_rate": pytest.approx(1.0), "profit_factor": None, "profit_factor_state": "no losing trades: profit factor is undefined (gross loss is zero)",
    }
    assert strategy["trend_following"]["trades"] == 4
    assert strategy["trend_following"]["net_pnl"] == pytest.approx(-40.0)
    assert strategy["trend_following"]["win_rate"] == pytest.approx(0.25)
    assert strategy["trend_following"]["profit_factor"] == pytest.approx(30.0 / 70.0)
    assert strategy["trend_following"]["breakeven_trades"] == 1

    symbol = report["by_symbol"]
    assert set(symbol) == {"EURUSD", "GBPUSD"}
    assert symbol["EURUSD"]["trades"] == 3 and symbol["EURUSD"]["net_pnl"] == pytest.approx(30.0)
    assert symbol["EURUSD"]["profit_factor"] == pytest.approx(100.0 / 70.0)
    assert symbol["GBPUSD"]["trades"] == 3 and symbol["GBPUSD"]["net_pnl"] == pytest.approx(70.0)
    assert symbol["GBPUSD"]["profit_factor"] is None


def test_the_empty_state_is_explicit_instead_of_fabricated(db):
    report = performance(db)
    stats = statistics(db)

    assert report["state"] == EMPTY_STATE
    assert report["total_trades"] == 0 and report["net_realized_pnl"] == 0.0
    assert report["gross_profit"] == 0.0 and report["gross_loss"] == 0.0
    assert report["average_win"] is None and report["average_loss"] is None
    assert report["largest_win"] is None and report["largest_loss"] is None
    assert report["profit_factor"] is None and "no reconciled closed trades" in report["profit_factor_state"]
    assert report["expectancy"] is None
    assert report["current_drawdown"] == 0.0 and report["maximum_drawdown"] == 0.0

    assert stats["state"] == EMPTY_STATE
    assert stats["win_rate"] is None and stats["loss_rate"] is None
    assert stats["profit_factor"] is None and stats["maximum_drawdown"] == 0.0
    assert stats["average_risk_reward"] is None
    assert stats["today_trades"] == 0 and stats["today_realized_pnl"] == 0.0
    assert stats["by_strategy"] == {} and stats["by_symbol"] == {}


def test_profit_factor_without_a_single_loss_is_undefined_not_infinite(db):
    now = datetime.now(timezone.utc)
    closed_trade(db, "win-1", profit=10.0, close_time=now - timedelta(days=2))
    closed_trade(db, "win-2", profit=20.0, close_time=now - timedelta(days=1))

    report = performance(db)
    stats = statistics(db)

    assert report["profit_factor"] is None
    assert report["profit_factor_state"] == "no losing trades: profit factor is undefined (gross loss is zero)"
    assert stats["profit_factor"] is None and stats["profit_factor_state"] == report["profit_factor_state"]
    assert report["gross_profit"] == pytest.approx(30.0) and report["gross_loss"] == 0.0
    assert report["expectancy"] == pytest.approx(15.0)
    assert report["maximum_drawdown"] == 0.0


def test_only_reconciled_closed_trades_are_counted(db):
    now = datetime.now(timezone.utc)
    closed_trade(db, "closed", profit=12.0, close_time=now)
    closed_trade(db, "partial", profit=5.0, close_time=now, status="PARTIALLY_CLOSED")
    closed_trade(db, "unreconciled", profit=99.0, close_time=now, reconciled=False)
    closed_trade(db, "still-open", profit=99.0, close_time=None, reconciled=False, status="EXECUTED")

    report = performance(db)

    assert report["total_trades"] == 2
    assert report["net_realized_pnl"] == pytest.approx(17.0)
    assert statistics(db)["total_closed_trades"] == 2


def test_a_gross_profitable_trade_whose_costs_exceed_it_counts_as_a_loss(db):
    now = datetime.now(timezone.utc)
    row = closed_trade(db, "costly", profit=2.0, commission=-5.0, close_time=now)

    assert net_pnl(row) == pytest.approx(-3.0)
    report = performance(db)

    assert report["total_trades"] == 1 and report["losing_trades"] == 1 and report["winning_trades"] == 0
    assert report["gross_loss"] == pytest.approx(3.0)
    assert report["net_realized_pnl"] == pytest.approx(-3.0)
