"""Runtime coordination tables.

These rows carry operational bookkeeping rather than trading state, so they live here instead of
in ``app/database/base.py``: the trading models are mirrored one-to-one by the initial migrations
and changing them would invalidate the migration contract. ``SchedulerLockRecord`` is the
database-level half of the scheduler's single-runner guard: one row per lock key, taken at the
start of a cycle and released at the end, with takeover allowed only after the heartbeat goes
stale so a crashed process cannot deadlock the loop forever.
"""
from datetime import datetime
from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import Base


class SchedulerLockRecord(Base):
    __tablename__ = "scheduler_locks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lock_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    owner: Mapped[str] = mapped_column(String(128))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
