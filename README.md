# MT5 Forex Trading System

This is a **fail-closed, demo-first** algorithmic trading system. It does not promise or imply profitability. MT5 execution is intentionally Windows-host only because the `MetaTrader5` Python package communicates with a local MT5 terminal.

## Safety defaults

- `TRADING_MODE=demo` by default.
- Live orders require both `TRADING_MODE=live` and `LIVE_TRADING_ENABLED=true`.
- Every order requires a stop loss, risk approval, valid symbol/tick/spread/margin checks, and an idempotency key.
- Unavailable MT5, database, market data, or news policy fails closed: no new orders.
- `NEWS_FAIL_CLOSED=true` (the default) blocks new trades while no news calendar is configured: with no calendar there is no protection window to enforce, so the bot fails closed instead of pretending. Point `NEWS_EVENTS_FILE` at a JSON calendar, or set `NEWS_FAIL_CLOSED=false` to accept trading without news protection.
- `NEWS_PROVIDER=trading_economics` reads the official Trading Economics calendar **API** (`api.tradingeconomics.com`, never their website and never a scrape). Rows are normalised into the app's news-event representation (`event id`, title, country, currency, UTC timestamp, impact, actual, forecast, previous, `retrieved_at`, provider), cached in `news_events`/`news_provider_state` so the provider is not called once per symbol per cycle, and matched to pairs by currency — a EUR event blocks EURUSD and EURGBP, never GBPJPY. Only **high-impact** events inside `NEWS_WINDOW_MINUTES` around the release block, and only *new* entries: the provider can never close an open position. A missing or rejected credential, a transport error, a non-200 answer, an unparsable payload, an invalid timestamp and a calendar older than `NEWS_MAX_AGE_SECONDS` are all reported as unusable, and `NEWS_FAIL_CLOSED=true` then refuses every new entry. The credential is read from `TRADING_ECONOMICS_API_KEY` in the environment only and is never logged.

## Hard server-side risk limits

The pre-trade gate (`app/risk/engine.py`) is an ordered chain of named checks; any failure means NO
TRADE. The limits are absolute and persisted, not a percentage of equity, and they are **ceilings,
never targets**: the bot risks less whenever the technical setup allows it.

| Limit | Default | Meaning |
| --- | --- | --- |
| `MAX_BOT_CAPITAL_USD` | 3.00 | Allocation ceiling for this bot (not the account's equity); the required margin must fit inside it |
| `MAX_LOSS_PER_TRADE_USD` | 0.70 | The only risk budget a volume may be derived from |
| `MAX_SESSION_LOSS_USD` | 1.40 | Realized session loss at which new entries stop |
| `MAX_DAILY_TRADES` | 3 | Orders this bot may open per session |
| `MAX_OPEN_POSITIONS` | 1 | One position at a time: no averaging, grid, hedging or recovery |
| `MAX_LOTS_PER_POSITION` | 0.01 | Hard volume cap for a single position, whatever the budget would allow |

- Volume is always derived: `MAX_LOSS_PER_TRADE_USD / (stop distance in points x value per point
  per lot)`, floored to the broker's `volume_step` and clamped by `volume_min`/`volume_max`.
- If the broker's minimum volume would lose more than `MAX_LOSS_PER_TRADE_USD` at the strategy's
  technical (ATR-based) stop, the trade is **rejected** - the stop is never tightened and the limit
  is never raised to make a minimum lot fit.
- If the risk-derived volume would exceed `MAX_LOTS_PER_POSITION`, the trade is **rejected** rather
  than resized, so a tight stop can never be turned into a bigger-than-intended position.
- If the required margin exceeds `MAX_BOT_CAPITAL_USD`, the trade is rejected.
- The session loss, the trade counter, the session start and the kill-switch latch live in the
  `risk_state` table, so a restart cannot reset them. Reaching the session-loss limit or the trade
  limit persists *why* the session closed to new entries.
- Every order carries a broker-side stop loss (from the strategy's own ATR level, never an
  arbitrary dollar distance) and a take profit of at least 1:2.
- Immediately after a fill the position is read back from MT5 and compared with the approved
  levels. A position that was filled without its stop loss or take profit (or with a different one)
  is repaired once with a SLTP modification and verified again; a position that is still
  unprotected is **closed**, because an unprotected bot position is never left standing.
- The account's own `trade_mode` is read from MT5 (0=DEMO, 1=CONTEST, 2=REAL) and reported. A REAL
  account is its own state: `TRADING_MODE=demo` never downgrades it, and no order is sent on a real
  account unless both live gates are set.
- The live **login and server** are compared against `MT5_LOGIN`/`MT5_SERVER` before any order. A
  terminal that has reconnected to another account stops trading and persists an emergency lock,
  so neither the next cycle nor a restarted process can send an order until an operator resets it.
- Every unverifiable input fails closed: MT5 disconnected, terminal unavailable, stale market data,
  unreadable database or risk state, account mismatch, algorithmic trading disabled, spread too
  wide, or unusable symbol data all mean no new order.

## Dry run (read-only, submits nothing)

```
python -m app.dry_run          # human-readable report; exit 0 on PASS, 1 on REJECT
python -m app.dry_run --json   # the same report as JSON
```

It connects to the configured terminal, discovers the broker's real symbol names (for example
`EURUSDm`), reads the account, prices and candles, then runs the strategy, the market loop's
bracket re-anchoring, the sizing functions and the risk gate itself, and prints the account, the
expected account and whether it matches, the database and migration status, the trade mode, the
symbol, bid/ask, spread, the signal, entry/SL/TP, risk/reward, the broker minimum lot, the derived
lot, the hard lot cap, the expected loss at the stop, the required margin, the session P/L, the
session start, the kill switch, the live gates and the verdict. The gateway is wrapped so every MT5
write call raises: the report always ends with `orders submitted: 0`. `REJECT` (including "the
strategy returned HOLD") is a normal, expected outcome.

The risk state it reports comes from the configured database and nowhere else: an unreachable
database is reported as `DOWN` and judged as a REJECT, never replaced by a local SQLite file that
would report a different store's allowance.

## Quick start (demo)

1. Install Python 3.11+ and a 64-bit MT5 terminal on Windows. Log in to a **demo** account in MT5 and enable algorithmic trading.
2. Copy `.env.example` to `.env`; set MT5 credentials, terminal path, database/Redis URLs, and a unique `API_TOKEN`.
3. Start PostgreSQL and Redis. Either `docker compose up -d postgres redis`, or point `DATABASE_URL`/`REDIS_URL` at native services (`postgresql+psycopg://user:pw@127.0.0.1:5432/forexbot`, `redis://127.0.0.1:6379/0`) — the hostnames `postgres`/`redis` only resolve inside the compose network.
4. Create a venv and install: `python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -e ".[dev]"`
5. Run migrations: `alembic upgrade head`
6. Start the API: `uvicorn app.main:app --reload`
7. Start only after reviewing `/api/risk`: `POST /api/bot/start` with `Authorization: Bearer <API_TOKEN>`. The market loop then scans the configured symbols on `SCHEDULER_INTERVAL_SECONDS` and reconciles MT5 history on `RECONCILIATION_INTERVAL_SECONDS`; `GET /api/bot/status` reports the loop state, `GET /api/signals` the persisted signals, and `GET /api/performance` plus `GET /api/statistics` the analytics of the trades MT5 has confirmed as closed.

Run unit tests with `pytest`. Demo integration tests require `RUN_MT5_INTEGRATION=1` and a configured demo account.

## Unattended startup on Windows

`runtime/engine_service.py` is what the Windows Task Scheduler starts; `runtime/engine_task.xml` is
the task it registers (`MT5TradingBotEngine`).

```
schtasks /create /tn "MT5TradingBotEngine" /xml "runtime\engine_task.xml" /f   # elevated shell
# or, without elevation, for the current user:
Register-ScheduledTask -TaskName "MT5TradingBotEngine" -Xml (Get-Content -Raw runtime\engine_task.xml) -Force
Start-ScheduledTask -TaskName "MT5TradingBotEngine"
```

- **Trigger.** Logon, in the interactive session, because the `MetaTrader5` package talks to
  `terminal64.exe`, which only exists there. `MultipleInstancesPolicy=IgnoreNew` discards a second
  launch, and the market loop's database lease means a second *scheduler* still cannot order.
- **Restart.** The runner restarts the API after an unexpected exit, and the task carries a
  restart-on-failure policy for a harness-level failure. `runtime/stop_engine.flag` is the
  deliberate stop: create it and end the task.
- **Logs.** `runtime/logs/engine.log`, rotating at 5 MB with five kept files. Nothing here writes a
  credential: the application logs symbols, statuses and reasons only.
- **Resume.** With `AUTO_START_TRADING=true` the runner asks the API for `POST /api/bot/start` once
  the API answers — the same validated path an operator would use. Trading therefore resumes only
  after the persistent risk state, PostgreSQL, MT5, the pinned account, the emergency lock, the
  session loss, the trade count and the news provider have all been verified. Anything that does not
  verify leaves the engine running, serving status, and refusing to trade.

## Operational status

`GET /api/status` (bearer token) answers in one call: engine state, MT5 connectivity, the account
and whether it is the pinned one, the account's real/demo trade mode, database and risk-store
health, the calendar provider's health and **data age**, the scheduler's heartbeat and whether this
process is the single leader, `live_orders_permitted`, the open-position count, the session realized
P/L, trades used / `MAX_DAILY_TRADES` and the kill-switch state. No credential appears in it or in
any log line.

Exactly one process may turn a candle into an order: the market loop holds a lease row in
`scheduler_locks` for its whole lifetime, so a second engine is a standby that never scans. Verify
it on a running host with `python runtime/second_worker_check.py` — it runs one scheduler cycle as a
separate process against the live database with a gateway that cannot reach the broker, and reports
`skipped: true` with the lease still owned by the running engine.

## Known deployment boundary

Docker is supplied for PostgreSQL/Redis/API development. The live MT5 worker must run on the Windows host that owns the MT5 terminal; never assume a Linux container can execute MT5 orders.
