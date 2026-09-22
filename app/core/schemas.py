from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, Field


class Side(StrEnum): BUY = "BUY"; SELL = "SELL"
class SignalAction(StrEnum): BUY = "BUY"; SELL = "SELL"; HOLD = "HOLD"
class SignalStatus(StrEnum):
    """Lifecycle of a persisted signal, from the strategy candidate to its execution outcome.

    The EXECUTED/SUBMITTED/REJECTED/FAILED/DUPLICATE values are the execution layer's statuses;
    NEW, BLOCKED, RISK_REJECTED and ABANDONED are set by the trading loop and by reconciliation.
    """
    NEW = "NEW"
    BLOCKED = "BLOCKED"
    RISK_REJECTED = "RISK_REJECTED"
    ABANDONED = "ABANDONED"
    EXECUTED = "EXECUTED"
    SUBMITTED = "SUBMITTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"

class Signal(BaseModel):
    action: SignalAction = SignalAction.HOLD
    confidence: float = Field(ge=0, le=1)
    score: int = Field(ge=0, le=100)
    symbol: str
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy: str
    timeframe: str
    reasons: list[str] = []
    timestamp: datetime

class TradeIntent(BaseModel):
    trade_id: str
    signal: Signal
    volume: float
    requested_price: float
    idempotency_key: str
    # Optional link back to the persisted SignalRecord; falls back to trade_id when absent.
    signal_id: str | None = None

class RiskDecision(BaseModel):
    approved: bool
    reasons: list[str] = []
    volume: float | None = None

class EmergencyRequest(BaseModel):
    close_positions: bool = False
    cancel_pending_orders: bool = True
