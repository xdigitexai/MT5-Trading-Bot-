"""CLI: python -m app.backtest --symbol EURUSD --timeframe M15"""
from __future__ import annotations

import argparse
from app.backtesting.runner import run_backtest
from app.core.config import get_settings


def main() -> None:
    p = argparse.ArgumentParser(description="MT5 Forex Bot backtest (same strategy path as live/demo)")
    p.add_argument("--symbol", type=str, default=None, help="Single symbol, e.g. EURUSD")
    p.add_argument("--all", action="store_true", help="Backtest all configured symbols")
    p.add_argument("--timeframe", type=str, default="M15")
    p.add_argument("--start", type=str, default=None)
    p.add_argument("--end", type=str, default=None)
    p.add_argument("--initial-balance", type=float, default=100_000.0)
    p.add_argument("--reports-dir", type=str, default="reports")
    args = p.parse_args()
    settings = get_settings()
    if args.all or not args.symbol:
        symbols = list(settings.symbols)
    else:
        symbols = [args.symbol.upper()]
    print(f"Backtest symbols={symbols} timeframe={args.timeframe} balance={args.initial_balance}")
    print("NOTE: Using synthetic OHLC when MT5 history is not injected. Interpret as framework validation, not live edge proof.")
    result = run_backtest(symbols, initial_balance=args.initial_balance, settings=settings, reports_dir=args.reports_dir)
    print(result["summary"])
    print(f"\nReports written to {args.reports_dir}/")


if __name__ == "__main__":
    main()
