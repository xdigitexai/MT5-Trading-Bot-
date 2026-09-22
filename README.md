# MT5 Forex Trading System

This is a **fail-closed, demo-first** algorithmic trading system. It does not promise or imply profitability. MT5 execution is intentionally Windows-host only because the `MetaTrader5` Python package communicates with a local MT5 terminal.

## Safety defaults

- `TRADING_MODE=demo` by default.
- Live orders require both `TRADING_MODE=live` and `LIVE_TRADING_ENABLED=true`.
- Every order requires a stop loss, risk approval, valid symbol/tick/spread/margin checks, and an idempotency key.
- Unavailable MT5, database, market data, or news policy fails closed: no new orders.
- `NEWS_FAIL_CLOSED=true` (the default) blocks new trades while no news calendar is configured: with no calendar there is no protection window to enforce, so the bot fails closed instead of pretending. Point `NEWS_EVENTS_FILE` at a JSON calendar, or set `NEWS_FAIL_CLOSED=false` to accept trading without news protection.

## Quick start (demo)

1. Install Python 3.11+ and a 64-bit MT5 terminal on Windows. Log in to a **demo** account in MT5 and enable algorithmic trading.
2. Copy `.env.example` to `.env`; set MT5 credentials, terminal path, database/Redis URLs, and a unique `API_TOKEN`.
3. Start PostgreSQL and Redis: `docker compose up -d postgres redis`
4. Create a venv and install: `python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -e ".[dev]"`
5. Run migrations: `alembic upgrade head`
6. Start the API: `uvicorn app.main:app --reload`
7. Start only after reviewing `/api/risk`: `POST /api/bot/start` with `Authorization: Bearer <API_TOKEN>`. The market loop then scans the configured symbols on `SCHEDULER_INTERVAL_SECONDS` and reconciles MT5 history on `RECONCILIATION_INTERVAL_SECONDS`; `GET /api/bot/status` reports the loop state, `GET /api/signals` the persisted signals, and `GET /api/performance` plus `GET /api/statistics` the analytics of the trades MT5 has confirmed as closed.

Run unit tests with `pytest`. Demo integration tests require `RUN_MT5_INTEGRATION=1` and a configured demo account.

## Known deployment boundary

Docker is supplied for PostgreSQL/Redis/API development. The live MT5 worker must run on the Windows host that owns the MT5 terminal; never assume a Linux container can execute MT5 orders.
