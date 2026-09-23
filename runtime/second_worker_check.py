"""Operator check: a second engine process cannot take the market loop from a running leader.

Runs one real scheduler cycle as a separate operating-system process, against the live database the
engine is using, with a gateway stub: nothing here can reach the broker, and the only question asked
is whether the distributed lease refuses the second process.

    .venv/Scripts/python.exe runtime/second_worker_check.py

Expect ``"skipped": true`` and the lease still owned by the running engine while it leads.
"""
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sqlalchemy import select  # noqa: E402

from app.core.clock import utcnow  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.database.runtime import SchedulerLockRecord  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.mt5.gateway import MT5Health  # noqa: E402
from app.services.scheduler import MarketScheduler  # noqa: E402


class StubGateway:
    """A gateway that can never place an order: every read fails closed."""

    def health(self): return MT5Health(False, "stub gateway: no terminal in this process")
    def symbol_info(self, symbol): return None
    def account_info(self): return None
    def terminal_info(self): return None
    def tick(self, symbol): return None
    def rates(self, symbol, timeframe, count): return None
    def positions(self): return ()
    def orders(self): return ()
    def history(self, start, end): return ()
    def order_send(self, request): raise AssertionError("this process must never reach the broker")


settings = get_settings()
worker = MarketScheduler(settings, StubGateway(), SessionLocal)
summary = worker.run_once(now=utcnow())

with SessionLocal() as db:
    row = db.scalar(select(SchedulerLockRecord).where(SchedulerLockRecord.lock_key == "market-loop"))
    lease = None if row is None else {"owner": row.owner, "heartbeat_at": str(row.heartbeat_at)}

print(json.dumps({
    "second_process_pid": os.getpid(),
    "second_worker_owner": worker.owner,
    "skipped": summary.get("skipped"),
    "reason": summary.get("reason"),
    "symbols_scanned": summary.get("symbols"),
    "executions": summary.get("executions"),
    "leader_in_this_process": summary.get("leader"),
    "lease_owner_after": None if lease is None else lease["owner"],
    "lease_still_held_by_the_live_engine": bool(lease) and lease["owner"] != worker.owner,
}, indent=2, default=str))
