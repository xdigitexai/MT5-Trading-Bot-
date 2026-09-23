"""Durable session risk state.

Session realized P/L, the trade counter, peak equity, the session start and the emergency lock are
stored in ``risk_state`` so that a process restart cannot silently reset the session-loss limit, the
trade allowance, the drawdown limit or the kill switch. A new trading day inherits the previous
day's emergency lock and peak equity, but not the session-scoped kill switch: that one closes the
session it was raised in, and the next session starts with a fresh allowance.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.base import RiskStateRecord


@dataclass(frozen=True)
class RiskSnapshot:
    day: date
    realized_pnl: float
    starting_equity: float | None
    peak_equity: float | None
    emergency_locked: bool
    session_started_at: datetime | None = None
    kill_switch_reason: str | None = None
    trades_opened: int = 0

    @property
    def session_loss(self) -> float:
        return max(0.0, -float(self.realized_pnl or 0.0))

    @property
    def kill_switch_active(self) -> bool:
        return bool((self.kill_switch_reason or "").strip())


class RiskStateStore:
    def __init__(self, db: Session): self.db = db

    def load(self, day: date | None = None) -> RiskStateRecord:
        target = day or datetime.now(timezone.utc).date()
        row = self.db.scalar(select(RiskStateRecord).where(RiskStateRecord.day == target))
        if row: return row
        previous = self.db.scalar(select(RiskStateRecord).order_by(RiskStateRecord.day.desc()).limit(1))
        row = RiskStateRecord(
            day=target,
            realized_pnl=0.0,
            starting_equity=previous.starting_equity if previous else None,
            peak_equity=previous.peak_equity if previous else None,
            emergency_locked=bool(previous.emergency_locked) if previous else False,
            # The session starts now: the moment this day's row is first materialised. The kill
            # switch is deliberately not inherited - it closes one session, not every future one.
            session_started_at=datetime.now(timezone.utc),
            kill_switch_reason=None,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def snapshot(self, day: date | None = None) -> RiskSnapshot:
        row = self.load(day)
        return RiskSnapshot(
            row.day, float(row.realized_pnl or 0.0), row.starting_equity, row.peak_equity,
            bool(row.emergency_locked), row.session_started_at, row.kill_switch_reason,
            int(row.trades_opened or 0),
        )

    def add_realized_pnl(self, amount: float, day: date | None = None) -> RiskStateRecord:
        row = self.load(day)
        row.realized_pnl = float(row.realized_pnl or 0.0) + float(amount)
        self.db.commit()
        self.db.refresh(row)
        return row

    def observe_equity(self, equity: float, day: date | None = None) -> RiskStateRecord:
        """Record the current equity: first value becomes starting equity, the high-water mark the peak."""
        row = self.load(day)
        if equity is None: return row
        equity = float(equity)
        if row.starting_equity is None: row.starting_equity = equity
        if row.peak_equity is None or equity > row.peak_equity: row.peak_equity = equity
        self.db.commit()
        self.db.refresh(row)
        return row

    def register_trade_opened(self, day: date | None = None) -> RiskStateRecord:
        """Count one opened position; the counter is read back from the database, never memory."""
        row = self.load(day)
        row.trades_opened = int(row.trades_opened or 0) + 1
        self.db.commit()
        self.db.refresh(row)
        return row

    def trades_opened(self, day: date | None = None) -> int:
        return int(self.load(day).trades_opened or 0)

    def set_emergency_locked(self, locked: bool, day: date | None = None) -> RiskStateRecord:
        row = self.load(day)
        row.emergency_locked = bool(locked)
        self.db.commit()
        self.db.refresh(row)
        return row

    def set_kill_switch(self, reason: str, day: date | None = None) -> RiskStateRecord:
        """Persist why this session stopped accepting entries; idempotent for the same reason.

        The latch is written once per session, so a loop that re-evaluates the same limit every
        cycle does not write on every cycle, and a restarted process reads the verdict instead of
        recomputing a fresh allowance from zeroed in-memory counters.
        """
        row = self.load(day)
        reason = str(reason)
        if (row.kill_switch_reason or "") == reason:
            return row
        row.kill_switch_reason = reason
        self.db.commit()
        self.db.refresh(row)
        return row

    def clear_kill_switch(self, day: date | None = None) -> RiskStateRecord:
        row = self.load(day)
        row.kill_switch_reason = None
        self.db.commit()
        self.db.refresh(row)
        return row

    def daily_pnl(self, day: date | None = None) -> float:
        return float(self.load(day).realized_pnl or 0.0)
