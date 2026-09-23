"""Order execution.

Everything here fails closed. An order is only sent when the stop loss is validated, the symbol
specification is known, the account margin can be verified and the idempotency key is reserved
in the database; the reservation uses a unique constraint so the same signal can never produce
a second order. A gateway error or an ambiguous response is recorded and never retried blindly.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, TradingMode
from app.core.schemas import TradeIntent
from app.database.base import AuditRecord, ExecutionGuardRecord, SignalRecord, TradeRecord
from app.execution.validation import validate_stops
from app.mt5.constants import MT5Constants, mt5_constants
from app.mt5.account import account_matches, account_profile
from app.mt5.gateway import MT5Gateway
from app.risk.sizing import margin_within_free_margin, normalize_volume, required_margin, spec_from_symbol_info

logger = logging.getLogger(__name__)

EXECUTED = "EXECUTED"
SUBMITTED = "SUBMITTED"
REJECTED = "REJECTED"
FAILED = "FAILED"
DUPLICATE = "DUPLICATE"
ALLOWED_STATUSES = (EXECUTED, SUBMITTED)

# MT5 position types, mirrored like the request constants above: 0 = BUY, 1 = SELL.
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1


def stored_stop_loss(stop_loss: float | None) -> float:
    """trades.stop_loss is NOT NULL, so a rejected order without a stop records 0.0."""
    return float(stop_loss) if stop_loss else 0.0


def position_facts(position) -> dict:
    """The live position as MT5 reports it, for the post-execution verification and its logs."""
    side = {POSITION_TYPE_BUY: "BUY", POSITION_TYPE_SELL: "SELL"}.get(getattr(position, "type", None), "UNKNOWN")
    return {
        "ticket": getattr(position, "ticket", None),
        "symbol": str(getattr(position, "symbol", "")),
        "side": side,
        "volume": float(getattr(position, "volume", 0.0) or 0.0),
        "entry": getattr(position, "price_open", None),
        "stop_loss": float(getattr(position, "sl", 0.0) or 0.0),
        "take_profit": float(getattr(position, "tp", 0.0) or 0.0),
    }


@dataclass
class ExecutionResult:
    """Structured outcome of one submit() call."""
    status: str
    reason: str = ""
    reasons: list[str] = field(default_factory=list)
    record: TradeRecord | None = None
    order_ticket: str | None = None
    request: dict | None = None
    retcode: int | None = None
    protection: dict | None = None

    @property
    def accepted(self) -> bool:
        return self.status in ALLOWED_STATUSES


class ExecutionService:
    def __init__(self, settings: Settings, gateway: MT5Gateway, constants: MT5Constants | None = None):
        self.settings, self.gateway = settings, gateway
        self.constants = constants or mt5_constants()
        self.accepted_retcodes = (self.constants.TRADE_RETCODE_DONE, self.constants.TRADE_RETCODE_DONE_PARTIAL)

    def idempotency_key(self, intent: TradeIntent) -> str:
        """Explicit caller key, otherwise a deterministic identity for this signal."""
        raw = (intent.idempotency_key or "").strip()
        if not raw:
            signal = intent.signal
            payload = "|".join(str(x) for x in (
                intent.signal_id, signal.symbol, signal.strategy, signal.action,
                signal.timestamp.isoformat(), intent.requested_price, intent.volume,
                signal.stop_loss, signal.take_profit,
            ))
            raw = sha256(payload.encode()).hexdigest()
        return raw[:128]

    def submit(self, db: Session, intent: TradeIntent) -> ExecutionResult:
        key = self.idempotency_key(intent)
        signal, settings = intent.signal, self.settings
        existing = db.scalar(select(TradeRecord).where(TradeRecord.trade_id == intent.trade_id))
        if existing is not None:
            logger.info("order_duplicate trade_id=%s status=%s", intent.trade_id, existing.status)
            return ExecutionResult(DUPLICATE, f"trade_id {intent.trade_id} already submitted with status {existing.status}", ["duplicate trade_id"], existing, existing.mt5_order_ticket)
        reserved = db.scalar(select(ExecutionGuardRecord).where(ExecutionGuardRecord.idempotency_key == key))
        if reserved is not None:
            logger.info("order_duplicate idempotency_key=%s status=%s", key, reserved.status)
            return ExecutionResult(DUPLICATE, f"idempotency key already used with status {reserved.status}", ["duplicate idempotency key"], None, reserved.order_ticket)

        blocked = self._preflight(intent)
        if blocked is not None:
            record = self._persist_rejection(db, intent, blocked.status, blocked.reason, blocked.reasons)
            blocked.record = record
            logger.warning("order_blocked trade_id=%s symbol=%s status=%s reasons=%s", intent.trade_id, signal.symbol, blocked.status, blocked.reasons)
            return blocked

        info = self.gateway.symbol_info(signal.symbol)
        spec = spec_from_symbol_info(info)
        if spec is None:
            return self._reject(db, intent, "symbol specification is unusable for sizing", ["symbol specification is unusable for sizing"])
        volume = normalize_volume(intent.volume, spec)
        if volume is None:
            return self._reject(db, intent, "volume is invalid or below the broker minimum", ["volume is invalid or below the broker minimum"])
        stops = validate_stops(signal.action, intent.requested_price, signal.stop_loss, signal.take_profit, spec, min_risk_reward=settings.min_risk_reward_ratio)
        if not stops.ok:
            return self._reject(db, intent, "order rejected before send: invalid stop configuration", stops.reasons)
        account = self.gateway.account_info()
        margin = required_margin(volume, spec, intent.requested_price, float(getattr(account, "leverage", 0) or 0))
        if not margin_within_free_margin(margin, getattr(account, "margin_free", None)):
            return self._reject(db, intent, "insufficient free margin for the risk-sized volume", ["insufficient free margin"])

        # Reserved only now: a pre-send rejection must not consume the idempotency key, but no
        # order can be sent without the reservation, so a duplicate can never reach the broker.
        guard = self._reserve(db, key, intent, volume)
        if guard is None:
            logger.warning("order_duplicate idempotency_key=%s race_lost=true", key)
            return ExecutionResult(DUPLICATE, "idempotency key was reserved concurrently", ["duplicate idempotency key"], None, None)

        request = self.build_request(intent, spec, volume)
        logger.info("order_send trade_id=%s symbol=%s volume=%s side=%s sl=%s tp=%s key=%s", intent.trade_id, signal.symbol, volume, signal.action, request["sl"], request["tp"], key)
        try:
            result = self.gateway.order_send(request)
        except Exception as error:  # a broken gateway must not look like a filled order
            logger.error("order_send_failed trade_id=%s error=%s", intent.trade_id, type(error).__name__)
            return self._finish(db, guard, intent, request, None, FAILED, f"gateway raised {type(error).__name__}: order state unknown, failed closed", [str(error)])

        if result is None:
            return self._finish(db, guard, intent, request, None, FAILED, "gateway returned no result: order state unknown, failed closed", ["gateway returned no result"])
        retcode = getattr(result, "retcode", None)
        if retcode is None:
            return self._finish(db, guard, intent, request, result, FAILED, "gateway response has no retcode: order state unknown, failed closed", ["ambiguous gateway response"])
        if retcode in self.accepted_retcodes:
            outcome = self._finish(db, guard, intent, request, result, EXECUTED, "order executed", [])
            outcome.protection = self._verify_protection(db, intent, outcome, spec)
            return outcome
        if retcode == self.constants.TRADE_RETCODE_PLACED:
            return self._finish(db, guard, intent, request, result, SUBMITTED, "order placed as pending", [])
        comment = getattr(result, "comment", "") or ""
        return self._finish(db, guard, intent, request, result, REJECTED, f"broker rejected the order (retcode {retcode})", [f"retcode {retcode}", str(comment)])

    def _reject(self, db: Session, intent: TradeIntent, reason: str, reasons: list[str]) -> ExecutionResult:
        record = self._persist_rejection(db, intent, REJECTED, reason, reasons)
        logger.warning("order_blocked trade_id=%s symbol=%s reasons=%s", intent.trade_id, intent.signal.symbol, reasons)
        return ExecutionResult(REJECTED, reason, reasons, record)

    def build_request(self, intent: TradeIntent, spec, volume: float) -> dict:
        signal, constants = intent.signal, self.constants
        side = constants.ORDER_TYPE_BUY if str(signal.action).upper() == "BUY" else constants.ORDER_TYPE_SELL
        if spec.filling_mode & 1:
            filling = constants.ORDER_FILLING_FOK
        elif spec.filling_mode & 2:
            filling = constants.ORDER_FILLING_IOC
        else:
            filling = constants.ORDER_FILLING_RETURN
        # MT5 brokers may reject punctuation such as ':' in order comments.
        safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in intent.trade_id)[:24]
        return {
            "action": constants.TRADE_ACTION_DEAL,
            "symbol": signal.symbol,
            "volume": volume,
            "type": side,
            "price": round(float(intent.requested_price), spec.digits),
            "sl": round(float(signal.stop_loss), spec.digits),
            "tp": round(float(signal.take_profit), spec.digits) if signal.take_profit else 0.0,
            "deviation": self.settings.order_deviation_points,
            "magic": self.settings.magic_number,
            "comment": f"bot-{safe_id}",
            "type_time": constants.ORDER_TIME_GTC,
            "type_filling": filling,
        }

    def _preflight(self, intent: TradeIntent) -> ExecutionResult | None:
        """Mode gates, symbol universe and MT5 availability; None means the order may proceed."""
        signal, settings = intent.signal, self.settings
        if settings.trading_mode in (TradingMode.BACKTEST, TradingMode.PAPER):
            return ExecutionResult(REJECTED, "non-MT5 mode blocked broker execution", ["non-MT5 mode blocked broker execution"])
        if settings.trading_mode is TradingMode.LIVE and not settings.live_orders_permitted:
            return ExecutionResult(REJECTED, "LIVE dual safety guard blocked execution", ["LIVE dual safety guard blocked execution"])
        if settings.symbols and signal.symbol not in settings.symbols:
            return ExecutionResult(REJECTED, "symbol is not in the configured trading universe", ["symbol is not in the configured trading universe"])
        if intent.volume is None or intent.volume <= 0:
            return ExecutionResult(REJECTED, "volume must be greater than zero", ["volume must be greater than zero"])
        health = self.gateway.health()
        if not health.connected:
            return ExecutionResult(REJECTED, f"MT5 unavailable, no order sent: {health.detail}", ["MT5 unavailable, no order sent"])
        if self.gateway.symbol_info(signal.symbol) is None:
            return ExecutionResult(REJECTED, "symbol specification unavailable", ["symbol specification unavailable"])
        account = self.gateway.account_info()
        if account is None:
            return ExecutionResult(REJECTED, "account information unavailable for margin verification", ["account information unavailable for margin verification"])
        profile = account_profile(account)
        if profile is None or not profile.classified:
            return ExecutionResult(REJECTED, "the broker account trade mode could not be verified: refusing to send", ["unverified broker account trade mode"])
        # The terminal must still be logged in to the account the operator pinned: a silent
        # reconnect to another account is a hard stop, checked here as well as in the risk engine.
        matched, mismatch = account_matches(profile, settings.mt5_login, settings.mt5_server)
        if not matched:
            logger.critical("order_blocked_account_mismatch symbol=%s login=%s server=%s expected_login=%s expected_server=%s", signal.symbol, profile.login, profile.server, settings.mt5_login, settings.mt5_server)
            return ExecutionResult(REJECTED, mismatch, ["broker account does not match the configured account"])
        if profile.is_real and not settings.live_orders_permitted:
            logger.critical("order_blocked_account_is_real symbol=%s trade_mode=%s live_orders_permitted=%s", signal.symbol, profile.trade_mode, settings.live_orders_permitted)
            return ExecutionResult(REJECTED, f"the connected broker account is {profile.trade_mode_label} (trade_mode={profile.trade_mode}) and live trading is not enabled", ["real account without the live gate"])
        return None

    def _reserve(self, db: Session, key: str, intent: TradeIntent, volume: float) -> ExecutionGuardRecord | None:
        guard = ExecutionGuardRecord(idempotency_key=key, signal_id=intent.signal_id or intent.trade_id, symbol=intent.signal.symbol, side=str(intent.signal.action), volume=volume, status="PENDING")
        db.add(guard)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
        logger.info("order_reserved key=%s symbol=%s volume=%s", key, guard.symbol, volume)
        return guard

    def _finish(self, db: Session, guard: ExecutionGuardRecord, intent: TradeIntent, request: dict | None, result, status: str, reason: str, reasons: list[str]) -> ExecutionResult:
        signal = intent.signal
        retcode = getattr(result, "retcode", None) if result is not None else None
        ticket = str(getattr(result, "order", "") or "") or None if result is not None else None
        deal = str(getattr(result, "deal", "") or "") or None if result is not None else None
        guard.status, guard.order_ticket = status, ticket
        record = TradeRecord(
            trade_id=intent.trade_id, symbol=signal.symbol, side=str(signal.action), volume=guard.volume,
            status=status, requested_price=intent.requested_price, stop_loss=stored_stop_loss(signal.stop_loss),
            take_profit=signal.take_profit, signal_score=signal.score, strategy=signal.strategy,
            mt5_order_ticket=ticket, mt5_position_ticket=deal,
            executed_price=getattr(result, "price", None) if result is not None else None,
            open_time=datetime.now(timezone.utc) if status in ALLOWED_STATUSES else None,
        )
        db.add(record)
        self._sync_signal(db, intent, status, ticket)
        db.add(AuditRecord(
            event_type="TRADE_SUBMITTED" if status in ALLOWED_STATUSES else f"TRADE_{status}",
            severity="INFO" if status in ALLOWED_STATUSES else "WARNING" if status == REJECTED else "ERROR",
            message=f"{signal.symbol} {status}: {reason}",
            metadata_json=json.dumps({"trade_id": intent.trade_id, "symbol": signal.symbol, "status": status, "retcode": retcode, "order": ticket, "reasons": reasons, "request": request}),
        ))
        db.commit()
        db.refresh(record)
        logger.info("order_finished trade_id=%s symbol=%s status=%s retcode=%s ticket=%s", intent.trade_id, signal.symbol, status, retcode, ticket)
        return ExecutionResult(status, reason, reasons, record, ticket, request, retcode)

    # ------------------------------------------------------------------ post-execution protection

    def _verify_protection(self, db: Session, intent: TradeIntent, outcome: ExecutionResult, spec) -> dict:
        """Read the filled position back and make sure the broker really holds its protection.

        An accepted order is not yet a protected position: nothing guarantees the fill carried the
        stop loss and take profit the request asked for. The live position is therefore read back
        from MT5 and compared with the levels of the approved signal. A difference is repaired once
        with a SLTP modification, and a position that is still unprotected afterwards is closed -
        an unprotected bot position is the one outcome this layer must never leave standing.
        """
        signal = intent.signal
        expected_sl, expected_tp = float(signal.stop_loss or 0.0), float(signal.take_profit or 0.0)
        tolerance = spec.point / 2 if (spec is not None and spec.point > 0) else 1e-9
        report = {"verified": False, "corrected": False, "closed": False, "position": None, "detail": ""}
        positions = self.gateway.positions()
        position = None if positions is None else self._find_position(positions, signal.symbol, outcome.order_ticket)
        if position is None:
            report["detail"] = "the live position could not be read back after the fill, so its protection is unverified"
            logger.critical(
                "position_protection_unverified symbol=%s order_ticket=%s reason=%s",
                signal.symbol, outcome.order_ticket, "positions_unreadable" if positions is None else "position_not_found",
            )
            return report
        report["position"] = position_facts(position)
        if self._protection_intact(position, expected_sl, expected_tp, tolerance):
            report.update(verified=True, detail="the broker holds the position with the stop loss and take profit of the approved signal")
            logger.info(
                "position_protected ticket=%s symbol=%s side=%s volume=%s entry=%s sl=%s tp=%s",
                report["position"]["ticket"], report["position"]["symbol"], report["position"]["side"], report["position"]["volume"],
                report["position"]["entry"], report["position"]["stop_loss"], report["position"]["take_profit"],
            )
            return report

        logger.critical(
            "position_protection_missing ticket=%s symbol=%s sl=%s tp=%s expected_sl=%s expected_tp=%s",
            getattr(position, "ticket", None), signal.symbol,
            getattr(position, "sl", None), getattr(position, "tp", None), expected_sl, expected_tp,
        )
        try:
            self.gateway.modify_position(int(getattr(position, "ticket", 0)), signal.symbol, expected_sl, expected_tp)
        except Exception as error:  # a failed repair is reported, never assumed to have worked
            logger.error("position_protection_repair_failed ticket=%s error=%s", getattr(position, "ticket", None), type(error).__name__)
        refreshed = self.gateway.positions()
        repaired = None if refreshed is None else self._find_position(refreshed, signal.symbol, getattr(position, "ticket", None))
        if repaired is not None and self._protection_intact(repaired, expected_sl, expected_tp, tolerance):
            report.update(verified=True, corrected=True, position=position_facts(repaired), detail="the position carried no usable protection, so the stop loss and take profit were re-applied and verified")
            self._protection_event(db, intent, "TRADE_PROTECTION_CORRECTED", "WARNING", f"{signal.symbol} position {getattr(repaired, 'ticket', None)} was unprotected after the fill; SL {expected_sl} / TP {expected_tp} were re-applied")
            logger.warning("position_protection_corrected ticket=%s symbol=%s sl=%s tp=%s", getattr(repaired, "ticket", None), signal.symbol, expected_sl, expected_tp)
            return report

        target = repaired if repaired is not None else position
        closed = False
        try:
            closed = self.gateway.close_position(target) is not None
        except Exception as error:
            logger.error("position_protection_close_failed ticket=%s error=%s", getattr(target, "ticket", None), type(error).__name__)
        report.update(closed=closed, position=position_facts(target), detail="protection could not be established, so the bot-managed position was closed" if closed else "protection could not be established and closing the position did not return a result")
        self._protection_event(
            db, intent, "TRADE_PROTECTION_FAILED", "CRITICAL",
            f"{signal.symbol} position {getattr(target, 'ticket', None)} could not be protected (SL {expected_sl} / TP {expected_tp}); close_sent={closed}",
        )
        logger.critical("position_protection_failed ticket=%s symbol=%s close_sent=%s", getattr(target, "ticket", None), signal.symbol, closed)
        return report

    def _find_position(self, positions, symbol: str, ticket) -> object | None:
        """The bot's own position for an accepted order: by ticket first, then the single symbol match.

        ``MAX_OPEN_POSITIONS=1`` makes a lone bot-managed position on the symbol unambiguous, so the
        fallback cannot pick a position belonging to another order.
        """
        magic = self.settings.magic_number
        managed = [position for position in positions if getattr(position, "magic", magic) == magic]
        wanted = "" if ticket is None else str(ticket)
        for position in managed:
            if wanted and str(getattr(position, "ticket", "")) == wanted:
                return position
        same_symbol = [position for position in managed if str(getattr(position, "symbol", "")).upper() == str(symbol).upper()]
        return same_symbol[0] if len(same_symbol) == 1 else None

    def _protection_intact(self, position, expected_sl: float, expected_tp: float, tolerance: float) -> bool:
        """True only when the stop loss and the take profit are present *and* the approved levels."""
        stop_loss = float(getattr(position, "sl", 0.0) or 0.0)
        take_profit = float(getattr(position, "tp", 0.0) or 0.0)
        if stop_loss <= 0 or (expected_tp > 0 and take_profit <= 0):
            return False
        if abs(stop_loss - expected_sl) > tolerance:
            return False
        return expected_tp <= 0 or abs(take_profit - expected_tp) <= tolerance

    def _protection_event(self, db: Session, intent: TradeIntent, event_type: str, severity: str, message: str) -> None:
        db.add(AuditRecord(
            event_type=event_type, severity=severity, message=message,
            metadata_json=json.dumps({"symbol": intent.signal.symbol, "trade_id": intent.trade_id}),
        ))
        db.commit()

    def _persist_rejection(self, db: Session, intent: TradeIntent, status: str, reason: str, reasons: list[str]) -> TradeRecord:
        signal = intent.signal
        record = TradeRecord(
            trade_id=intent.trade_id, symbol=signal.symbol, side=str(signal.action), volume=float(intent.volume or 0.0),
            status=status, requested_price=intent.requested_price, stop_loss=stored_stop_loss(signal.stop_loss),
            take_profit=signal.take_profit, signal_score=signal.score, strategy=signal.strategy,
        )
        db.add(record)
        self._sync_signal(db, intent, status, None)
        db.add(AuditRecord(event_type=f"TRADE_{status}", severity="WARNING", message=f"{signal.symbol} {status}: {reason}", metadata_json=json.dumps({"trade_id": intent.trade_id, "symbol": signal.symbol, "reasons": reasons})))
        db.commit()
        db.refresh(record)
        return record

    def _sync_signal(self, db: Session, intent: TradeIntent, status: str, ticket: str | None) -> SignalRecord:
        """The persisted signal always reflects the execution outcome."""
        signal = intent.signal
        signal_id = intent.signal_id or intent.trade_id
        row = db.scalar(select(SignalRecord).where(SignalRecord.signal_id == signal_id))
        executed = status in ALLOWED_STATUSES
        if row is None:
            row = SignalRecord(
                signal_id=signal_id, symbol=signal.symbol, strategy=signal.strategy, timeframe=signal.timeframe,
                direction=str(signal.action), confidence=signal.confidence, score=signal.score,
                entry_price=signal.entry, stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                reason="; ".join(signal.reasons) or None, created_at=signal.timestamp,
            )
            db.add(row)
        row.executed = row.executed or executed
        row.status = status
        row.order_ticket = ticket or row.order_ticket
        return row

    def emergency_stop(self, db: Session, close_positions: bool, cancel_pending: bool):
        db.add(AuditRecord(event_type="EMERGENCY_STOP", severity="CRITICAL", message=f"close={close_positions}, cancel_pending={cancel_pending}"))
        db.commit()
