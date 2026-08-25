from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import Settings, TradingMode
from app.core.schemas import TradeIntent
from app.database.base import AuditRecord, TradeRecord
from app.mt5.gateway import MT5Gateway, mt5

class ExecutionService:
    def __init__(self, settings: Settings, gateway: MT5Gateway): self.settings, self.gateway = settings, gateway
    def submit(self, db: Session, intent: TradeIntent) -> TradeRecord:
        existing = db.scalar(select(TradeRecord).where(TradeRecord.trade_id == intent.trade_id))
        if existing: return existing  # database idempotency fence
        signal = intent.signal
        record = TradeRecord(trade_id=intent.trade_id, symbol=signal.symbol, side=signal.action, volume=intent.volume, status="REJECTED", requested_price=intent.requested_price, stop_loss=signal.stop_loss, take_profit=signal.take_profit, signal_score=signal.score, strategy=signal.strategy)
        if self.settings.trading_mode in (TradingMode.BACKTEST, TradingMode.PAPER):
            db.add(record); db.add(AuditRecord(event_type="TRADE_REJECTED", severity="WARNING", message="non-MT5 mode blocked broker execution")); db.commit(); return record
        if self.settings.trading_mode is TradingMode.LIVE and not self.settings.live_orders_permitted:
            db.add(record); db.add(AuditRecord(event_type="TRADE_REJECTED", severity="WARNING", message="LIVE dual safety guard blocked execution")); db.commit(); return record
        if mt5 is None or not self.gateway.health().connected:
            db.add(record); db.add(AuditRecord(event_type="TRADE_REJECTED", severity="WARNING", message="MT5 unavailable: no order sent")); db.commit(); return record
        side = mt5.ORDER_TYPE_BUY if signal.action == "BUY" else mt5.ORDER_TYPE_SELL
        symbol_info = mt5.symbol_info(signal.symbol)
        if symbol_info is None:
            record.status = "REJECTED"
            db.add(record); db.add(AuditRecord(event_type="TRADE_REJECTED", severity="WARNING", message="symbol specification unavailable")); db.commit(); return record
        # ``symbol_info.filling_mode`` is a bitmask (FOK=1, IOC=2), while
        # ORDER_FILLING_* are request enum values (FOK=0, IOC=1, RETURN=2).
        if symbol_info.filling_mode & 1:
            filling = mt5.ORDER_FILLING_FOK
        elif symbol_info.filling_mode & 2:
            filling = mt5.ORDER_FILLING_IOC
        else:
            filling = mt5.ORDER_FILLING_RETURN
        # MT5 brokers may reject punctuation such as ':' in order comments.
        safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in intent.trade_id)[:24]
        request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": signal.symbol, "volume": intent.volume, "type": side, "price": intent.requested_price, "sl": signal.stop_loss, "tp": signal.take_profit, "deviation": self.settings.order_deviation_points, "magic": self.settings.magic_number, "comment": f"bot-{safe_id}", "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling}
        result = self.gateway.order_send(request)
        record.status = "EXECUTED" if result and result.retcode == mt5.TRADE_RETCODE_DONE else "SUBMITTED" if result and result.retcode == mt5.TRADE_RETCODE_PLACED else "REJECTED"
        if result:
            record.mt5_order_ticket, record.mt5_position_ticket = str(result.order), str(result.deal)
            record.executed_price = result.price
        db.add(record); db.add(AuditRecord(event_type="TRADE_SUBMITTED", message=f"{signal.symbol} {record.status}", metadata_json=str(result)))
        db.commit(); db.refresh(record); return record
    def emergency_stop(self, db: Session, close_positions: bool, cancel_pending: bool):
        db.add(AuditRecord(event_type="EMERGENCY_STOP", severity="CRITICAL", message=f"close={close_positions}, cancel_pending={cancel_pending}")); db.commit()
