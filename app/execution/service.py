"""Order execution boundary. MT5 orders must not depend on database availability."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, TradingMode
from app.core.schemas import TradeIntent
from app.database.base import AuditRecord, TradeRecord
from app.mt5.gateway import MT5Gateway, mt5

log = logging.getLogger("execution")


@dataclass
class ExecutionResult:
    status: str
    mt5_order_ticket: str | None = None
    mt5_position_ticket: str | None = None
    executed_price: float | None = None
    retcode: int | None = None
    comment: str | None = None
    detail: str = ""


class ExecutionService:
    def __init__(self, settings: Settings, gateway: MT5Gateway):
        self.settings = settings
        self.gateway = gateway

    def submit(self, db: Session | None, intent: TradeIntent):
        signal = intent.signal

        if db is not None:
            try:
                existing = db.scalar(select(TradeRecord).where(TradeRecord.trade_id == intent.trade_id))
                if existing:
                    return existing
            except Exception:
                log.warning("DB idempotency check failed — continuing with MT5 submit")
                db = None

        record = TradeRecord(
            trade_id=intent.trade_id, symbol=signal.symbol, side=signal.action,
            volume=intent.volume, status="REJECTED", requested_price=intent.requested_price,
            stop_loss=signal.stop_loss, take_profit=signal.take_profit,
            signal_score=signal.score, strategy=signal.strategy,
        )

        if self.settings.trading_mode in (TradingMode.BACKTEST, TradingMode.PAPER):
            return self._reject(db, record, "non-MT5 mode blocked broker execution")
        if self.settings.trading_mode is TradingMode.LIVE and not self.settings.live_orders_permitted:
            return self._reject(db, record, "LIVE dual safety guard blocked execution")
        if self.settings.trading_mode is TradingMode.LIVE:
            return self._reject(db, record, "LIVE trading refused by hard safety guard")
        if mt5 is None or not self.gateway.health().connected:
            return self._reject(db, record, "MT5 unavailable: no order sent")

        result = self._send_mt5(intent)
        record.status = result.status
        record.mt5_order_ticket = result.mt5_order_ticket
        record.mt5_position_ticket = result.mt5_position_ticket
        record.executed_price = result.executed_price

        if db is not None:
            try:
                db.add(record)
                db.add(AuditRecord(event_type="TRADE_SUBMITTED",
                    message=f"{signal.symbol} {record.status} retcode={result.retcode}",
                    metadata_json=str(result.comment)))
                db.commit()
                db.refresh(record)
                return record
            except Exception:
                log.exception("DB journal failed after MT5 submit — order may still be live")
                try: db.rollback()
                except Exception: pass

        return result

    def _send_mt5(self, intent: TradeIntent) -> ExecutionResult:
        signal = intent.signal
        side = mt5.ORDER_TYPE_BUY if signal.action == "BUY" else mt5.ORDER_TYPE_SELL
        symbol_info = mt5.symbol_info(signal.symbol)
        if symbol_info is None:
            return ExecutionResult(status="REJECTED", detail="symbol specification unavailable")
        if symbol_info.filling_mode & 1:
            filling = mt5.ORDER_FILLING_FOK
        elif symbol_info.filling_mode & 2:
            filling = mt5.ORDER_FILLING_IOC
        else:
            filling = mt5.ORDER_FILLING_RETURN
        safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in intent.trade_id)[:24]
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": signal.symbol, "volume": intent.volume,
            "type": side, "price": intent.requested_price, "sl": signal.stop_loss, "tp": signal.take_profit,
            "deviation": self.settings.order_deviation_points, "magic": self.settings.magic_number,
            "comment": f"bot-{safe_id}", "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling,
        }
        raw = self.gateway.order_send(request)
        if raw is None:
            return ExecutionResult(status="REJECTED", detail="order_send returned None")
        retcode = int(getattr(raw, "retcode", -1))
        comment = str(getattr(raw, "comment", "") or "")
        if retcode == mt5.TRADE_RETCODE_DONE:
            status = "EXECUTED"
        elif retcode == mt5.TRADE_RETCODE_PLACED:
            status = "SUBMITTED"
        else:
            status = "REJECTED"
        return ExecutionResult(
            status=status,
            mt5_order_ticket=str(getattr(raw, "order", "") or ""),
            mt5_position_ticket=str(getattr(raw, "deal", "") or ""),
            executed_price=float(getattr(raw, "price", 0) or 0) or None,
            retcode=retcode, comment=comment, detail=comment,
        )

    def _reject(self, db, record, message: str):
        record.status = "REJECTED"
        if db is not None:
            try:
                db.add(record)
                db.add(AuditRecord(event_type="TRADE_REJECTED", severity="WARNING", message=message))
                db.commit()
                return record
            except Exception:
                log.warning("DB reject journal failed: %s", message)
        return ExecutionResult(status="REJECTED", detail=message)

    def emergency_stop(self, db, close_positions: bool, cancel_pending: bool):
        if db is None: return
        try:
            db.add(AuditRecord(event_type="EMERGENCY_STOP", severity="CRITICAL",
                message=f"close={close_positions}, cancel_pending={cancel_pending}"))
            db.commit()
        except Exception:
            log.exception("emergency_stop journal failed")
