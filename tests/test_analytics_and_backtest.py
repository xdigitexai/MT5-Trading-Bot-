from app.analytics.metrics import compute_performance
from app.analytics.sessions import session_name
from app.ai.analyst import analyze
from app.backtesting.runner import synthesize_ohlc, simulate_symbol, run_backtest
from app.core.config import Settings, TradingMode
from datetime import datetime, timezone


def test_session_overlap():
    ts = datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc)
    assert session_name(ts) == "London_NY_Overlap"


def test_performance_metrics_basic():
    r = compute_performance([10, -5, 20, -10])
    assert r.trades == 4
    assert r.net_profit == 15
    assert r.wins == 2


def test_ai_disabled_by_default():
    s = Settings(trading_mode=TradingMode.DEMO, live_trading_enabled=False, openai_analyst_enabled=False)
    out = analyze({"trades": 0}, s)
    assert "disabled" in out.lower()


def test_synthetic_backtest_runs():
    m15, h1, h4 = synthesize_ohlc(800, seed=1)
    trades = simulate_symbol("EURUSD", m15, h1, h4)
    assert isinstance(trades, list)


def test_run_backtest_writes_reports(tmp_path):
    result = run_backtest(["EURUSD"], initial_balance=100_000, reports_dir=tmp_path)
    assert "overall" in result
    assert (tmp_path / "EURUSD_backtest.txt").exists() or (tmp_path / "all_pairs_report.txt").exists()
