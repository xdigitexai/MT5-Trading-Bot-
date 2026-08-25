from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, Field


class Side(StrEnum): BUY = "BUY"; SELL = "SELL"
class SignalAction(StrEnum): BUY = "BUY"; SELL = "SELL"; HOLD = "HOLD"

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

class RiskDecision(BaseModel):
    approved: bool
    reasons: list[str] = []
    volume: float | None = None

class EmergencyRequest(BaseModel):
    close_positions: bool = False
    cancel_pending_orders: bool = True
