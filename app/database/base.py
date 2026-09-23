from datetime import date, datetime
from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase): pass

class AuditRecord(Base):
    __tablename__ = "bot_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class TradeRecord(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    volume: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), index=True)
    mt5_order_ticket: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mt5_position_ticket: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    executed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_score: Mapped[int] = mapped_column(Integer)
    strategy: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Reconciliation columns: every value below is only known once MT5 history is read back, so
    # each one is nullable and no existing column is renamed or dropped.
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float | None] = mapped_column(Float, nullable=True)
    swap: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mt5_deal_ticket: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    volume_closed: Mapped[float | None] = mapped_column(Float, nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SignalRecord(Base):
    """Persisted strategy signal; the API filters on symbol, strategy, status and created_at."""
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    direction: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Float)
    score: Mapped[int] = mapped_column(Integer, default=0)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    executed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    order_ticket: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="NEW", index=True)


class RiskStateRecord(Base):
    """Durable daily risk state.

    Daily-loss and peak-equity tracking must not live in process memory: a restart would silently
    reset the limits. One row per day, and a new day inherits the previous emergency lock.
    """
    __tablename__ = "risk_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, unique=True, index=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    starting_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    emergency_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Trades opened by this bot today/session. Persisted for the same reason as realized_pnl: a
    # restart must not hand the loop a fresh allowance of orders.
    trades_opened: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ExecutionGuardRecord(Base):
    """Idempotency fence: the unique key makes a second order for the same intent impossible."""
    __tablename__ = "execution_guards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    volume: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), index=True)
    order_ticket: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
