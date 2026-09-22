"""MT5 history reconciliation.

MT5 is the source of truth. The reconciler pulls broker deals for the configured lookback window,
groups them into positions, matches them to local ``TradeRecord`` rows by MT5 ticket/deal id first
and by symbol + open time + volume second, and then overwrites the local row with the broker's
entry price, exit price, closed volume, commission, swap, profit, tickets, timestamps, status and
close reason.

Realized P/L is booked into ``RiskStateStore`` as the *difference* against the value already stored
on the trade, so running reconciliation twice never double-counts a trade and the daily-loss and
drawdown limits keep reflecting reality. Only closed trades are booked: a floating result is not a
result.

Deals without our magic number are ignored (they belong to another strategy or to manual trading),
and a trade that has no counterpart on the broker side is only logged and counted — nothing is
invented on either side. A stale PENDING execution guard is retired only after the broker has been
queried and no order was found for it; if MT5 cannot be read, the guards are left alone.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import as_utc, epoch_to_utc, utcnow
from app.database.base import AuditRecord, ExecutionGuardRecord, SignalRecord, TradeRecord
from app.risk.state import RiskStateStore
from app.services.analytics import net_pnl

logger = logging.getLogger(__name__)

ENTRY_IN, ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY = 0, 1, 2, 3
DEAL_REASONS = {0: "client", 1: "mobile", 2: "web", 3: "expert", 4: "stop_loss", 5: "take_profit", 6: "stop_out", 7: "rollover", 8: "variation_margin", 9: "split"}
MATCHABLE_STATUSES = ("EXECUTED", "SUBMITTED", "CLOSED", "PARTIALLY_CLOSED", "FAILED")
GUARD_CONFIRMED_STATUSES = ("EXECUTED", "SUBMITTED")
RETIRED_GUARD_STATUS = "RETIRED"
PRICE_TOLERANCE = 1e-9


@dataclass(frozen=True)
class BrokerPosition:
    """One MT5 position rebuilt from its deals."""

    position_id: str
    symbol: str
    volume: float
    volume_closed: float
    entry_price: float | None
    exit_price: float | None
    open_time: datetime | None
    close_time: datetime | None
    profit: float
    commission: float
    swap: float
    tickets: frozenset[str]
    order_ticket: str | None
    entry_deal: str | None
    exit_deal: str | None
    close_reason: str | None
    comment: str
    closed: bool

    @property
    def net(self) -> float:
        return self.profit + self.commission + self.swap


@dataclass
class ReconciliationSummary:
    history_start: datetime | None = None
    history_end: datetime | None = None
    deals: int = 0
    positions: int = 0
    ignored_foreign: int = 0
    considered: int = 0
    matched: int = 0
    matched_by_ticket: int = 0
    matched_by_fallback: int = 0
    updated: int = 0
    unchanged: int = 0
    unmatched_broker: int = 0
    unmatched_local: int = 0
    pnl_applied: float = 0.0
    guards_retired: int = 0
    guards_kept: int = 0
    details: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "history_start": self.history_start.isoformat() if self.history_start else None,
            "history_end": self.history_end.isoformat() if self.history_end else None,
            "deals": self.deals,
            "positions": self.positions,
            "ignored_foreign": self.ignored_foreign,
            "considered": self.considered,
            "matched": self.matched,
            "matched_by_ticket": self.matched_by_ticket,
            "matched_by_fallback": self.matched_by_fallback,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "unmatched_broker": self.unmatched_broker,
            "unmatched_local": self.unmatched_local,
            "pnl_applied": self.pnl_applied,
            "guards_retired": self.guards_retired,
            "guards_kept": self.guards_kept,
            "details": list(self.details),
            "errors": list(self.errors),
        }


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _int(value: object, default: int = -1) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _weighted_price(deals: list[object]) -> float | None:
    volume = sum(_float(getattr(deal, "volume", 0.0)) for deal in deals)
    if volume <= 0:
        return None
    return sum(_float(getattr(deal, "price", 0.0)) * _float(getattr(deal, "volume", 0.0)) for deal in deals) / volume


def group_deals(deals, magic: int) -> tuple[list[BrokerPosition], int]:
    """Broker deals grouped into positions; returns the positions and the foreign-deal count."""
    buckets: dict[str, list[object]] = {}
    ignored = 0
    for deal in deals or ():
        if _int(getattr(deal, "magic", None)) != int(magic):
            ignored += 1
            continue
        position_id = _text(getattr(deal, "position_id", None)) or _text(getattr(deal, "order", None))
        if position_id is None:
            ignored += 1
            continue
        buckets.setdefault(position_id, []).append(deal)
    return [_build_position(position_id, rows) for position_id, rows in sorted(buckets.items())], ignored


def _build_position(position_id: str, rows: list[object]) -> BrokerPosition:
    def moment(deal) -> datetime | None:
        return epoch_to_utc(getattr(deal, "time", None))

    def kind(deal) -> int:
        return _int(getattr(deal, "entry", ENTRY_IN), ENTRY_IN)

    entries = [deal for deal in rows if kind(deal) in (ENTRY_IN, ENTRY_INOUT)]
    exits = [deal for deal in rows if kind(deal) in (ENTRY_OUT, ENTRY_OUT_BY)]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(rows, key=lambda deal: moment(deal) or epoch)
    entry_times = [moment(deal) for deal in entries or ordered]
    exit_times = [moment(deal) for deal in exits]
    volume = sum(_float(getattr(deal, "volume", 0.0)) for deal in entries)
    volume_closed = sum(_float(getattr(deal, "volume", 0.0)) for deal in exits)
    last_exit = exits[-1] if exits else None
    tickets = {_text(getattr(deal, "ticket", None)) for deal in rows} | {_text(getattr(deal, "order", None)) for deal in rows}
    entry_deal = next((_text(getattr(deal, "ticket", None)) for deal in ordered if kind(deal) in (ENTRY_IN, ENTRY_INOUT)), None)
    return BrokerPosition(
        position_id=position_id,
        symbol=_text(getattr(ordered[0], "symbol", None)) or "UNKNOWN",
        volume=volume if volume > 0 else volume_closed,
        volume_closed=volume_closed,
        entry_price=_weighted_price(entries) if entries else _weighted_price(ordered),
        exit_price=_weighted_price(exits) if exits else None,
        open_time=min((value for value in entry_times if value), default=None),
        close_time=max((value for value in exit_times if value), default=None),
        profit=sum(_float(getattr(deal, "profit", 0.0)) for deal in rows),
        commission=sum(_float(getattr(deal, "commission", 0.0)) for deal in rows),
        swap=sum(_float(getattr(deal, "swap", 0.0)) for deal in rows),
        tickets=frozenset(ticket for ticket in tickets if ticket),
        order_ticket=_text(getattr(ordered[0], "order", None)),
        entry_deal=entry_deal,
        exit_deal=_text(getattr(last_exit, "ticket", None)) if last_exit is not None else None,
        close_reason=DEAL_REASONS.get(_int(getattr(last_exit, "reason", -1)), None) if last_exit is not None else None,
        comment=_text(getattr(ordered[0], "comment", None)) or "",
        closed=volume_closed > 0 and volume_closed >= (volume if volume > 0 else volume_closed) - PRICE_TOLERANCE,
    )


def order_comment(trade_id: str) -> str:
    """The MT5 order comment the execution layer attaches to a trade id."""
    safe = "".join(character if character.isalnum() or character in "-_" else "-" for character in str(trade_id))[:24]
    return f"bot-{safe}"


class Reconciler:
    def __init__(self, settings, gateway):
        self.settings, self.gateway = settings, gateway

    def run(self, db: Session, *, now: datetime | None = None, lookback_hours: int | None = None) -> ReconciliationSummary:
        moment = as_utc(now) or utcnow()
        hours = self.settings.reconciliation_lookback_hours if lookback_hours is None else lookback_hours
        start = moment - timedelta(hours=hours)
        summary = ReconciliationSummary(history_start=start, history_end=moment)
        deals = self.gateway.history(start, moment)
        if deals is None:
            summary.errors.append("MT5 history is unavailable: reconciliation skipped and guards left untouched")
            logger.error("reconciliation_history_unavailable start=%s end=%s", start.isoformat(), moment.isoformat())
            return summary
        in_window = [deal for deal in deals if self._in_window(epoch_to_utc(getattr(deal, "time", None)), start, moment)]
        positions, summary.ignored_foreign = group_deals(in_window, self.settings.magic_number)
        summary.deals, summary.positions = len(in_window), len(positions)

        trades = list(db.scalars(select(TradeRecord).where(TradeRecord.status.in_(MATCHABLE_STATUSES))))
        summary.considered = len(trades)
        matched, unmatched_broker = self._match(trades, positions)
        for trade in trades:
            found = matched.get(trade.id)
            if found is None:
                summary.unmatched_local += 1
                summary.details.append(f"local trade {trade.trade_id} ({trade.symbol} {trade.status}) has no broker position in this window")
                logger.warning("reconciliation_unmatched_local trade_id=%s symbol=%s status=%s", trade.trade_id, trade.symbol, trade.status)
                continue
            position, kind = found
            summary.matched += 1
            summary.matched_by_ticket += 1 if kind == "ticket" else 0
            summary.matched_by_fallback += 1 if kind == "fallback" else 0
            if self._apply(db, trade, position, summary, moment):
                summary.updated += 1
            else:
                summary.unchanged += 1
        summary.unmatched_broker = len(unmatched_broker)
        for position in unmatched_broker:
            summary.details.append(f"broker position {position.position_id} ({position.symbol} {position.volume} lots) has no local trade")
            logger.warning("reconciliation_unmatched_broker position=%s symbol=%s volume=%s closed=%s", position.position_id, position.symbol, position.volume, position.closed)
        db.commit()

        orders, live_positions = self.gateway.orders(), self.gateway.positions()
        if orders is None or live_positions is None:
            summary.errors.append("open orders/positions could not be read: stale execution guards were left untouched")
            logger.error("reconciliation_order_state_unavailable")
        else:
            self._retire_stale_guards(db, moment, in_window, orders, live_positions, summary)
        db.add(AuditRecord(
            event_type="RECONCILIATION",
            severity="INFO" if not summary.errors else "WARNING",
            message=f"reconciliation matched={summary.matched} updated={summary.updated} pnl={summary.pnl_applied:.2f} guards_retired={summary.guards_retired}",
            metadata_json=None,
        ))
        db.commit()
        logger.info(
            "reconciliation_finished deals=%s positions=%s considered=%s matched=%s updated=%s unchanged=%s unmatched_broker=%s unmatched_local=%s pnl_applied=%.2f guards_retired=%s errors=%s",
            summary.deals, summary.positions, summary.considered, summary.matched, summary.updated, summary.unchanged,
            summary.unmatched_broker, summary.unmatched_local, summary.pnl_applied, summary.guards_retired, summary.errors,
        )
        return summary

    def _in_window(self, moment: datetime | None, start: datetime, end: datetime) -> bool:
        if moment is None:
            return True
        return start - timedelta(minutes=5) <= moment <= end + timedelta(minutes=5)

    def _match(self, trades: list[TradeRecord], positions: list[BrokerPosition]) -> tuple[dict[int, tuple[BrokerPosition, str]], list[BrokerPosition]]:
        """Ticket-first matching, then symbol + open time + volume; each side matches at most once."""
        by_ticket: dict[str, BrokerPosition] = {}
        for position in positions:
            for ticket in position.tickets:
                by_ticket.setdefault(ticket, position)
        matched: dict[int, tuple[BrokerPosition, str]] = {}
        taken: set[str] = set()
        for trade in trades:
            for ticket in (trade.mt5_position_ticket, trade.mt5_deal_ticket, trade.mt5_order_ticket):
                position = by_ticket.get(str(ticket)) if ticket else None
                if position is not None and position.position_id not in taken:
                    matched[trade.id] = (position, "ticket")
                    taken.add(position.position_id)
                    break
        window = self.settings.reconciliation_match_window_seconds
        for trade in [row for row in trades if row.id not in matched]:
            reference = as_utc(trade.open_time) or as_utc(trade.created_at)
            for position in positions:
                if position.position_id in taken or position.symbol.upper() != str(trade.symbol).upper():
                    continue
                if abs(position.volume - float(trade.volume or 0.0)) > 1e-6:
                    continue
                if reference is not None and position.open_time is not None and abs((reference - position.open_time).total_seconds()) > window:
                    continue
                matched[trade.id] = (position, "fallback")
                taken.add(position.position_id)
                break
        return matched, [position for position in positions if position.position_id not in taken]

    def _apply(self, db: Session, trade: TradeRecord, position: BrokerPosition, summary: ReconciliationSummary, moment: datetime) -> bool:
        """Write the broker's truth onto the local row; True when a business field changed."""
        status = "CLOSED" if position.closed else "PARTIALLY_CLOSED" if position.volume_closed > 0 else trade.status
        values = {
            "executed_price": position.entry_price if position.entry_price is not None else trade.executed_price,
            "exit_price": position.exit_price,
            "volume_closed": position.volume_closed if position.volume_closed > 0 else trade.volume_closed,
            "commission": position.commission,
            "swap": position.swap,
            "profit": position.profit,
            "open_time": position.open_time or trade.open_time,
            "close_time": position.close_time,
            "mt5_deal_ticket": position.exit_deal or position.entry_deal or trade.mt5_deal_ticket,
            "mt5_position_ticket": position.position_id,
            "mt5_order_ticket": position.order_ticket or trade.mt5_order_ticket,
            "close_reason": position.close_reason or trade.close_reason,
            "status": status,
        }
        changed = any(self._differs(getattr(trade, name), value) for name, value in values.items())
        if not changed:
            return False
        before = net_pnl(trade)
        for name, value in values.items():
            setattr(trade, name, value)
        trade.reconciled_at = moment
        if position.close_time is not None:
            delta = net_pnl(trade) - before
            if abs(delta) > PRICE_TOLERANCE:
                RiskStateStore(db).add_realized_pnl(delta, position.close_time.date())
                summary.pnl_applied += delta
        logger.info("reconciliation_updated trade_id=%s symbol=%s status=%s volume_closed=%s profit=%.2f exit=%s", trade.trade_id, trade.symbol, trade.status, trade.volume_closed, position.profit, trade.exit_price)
        return True

    def _differs(self, current: object, proposed: object) -> bool:
        if isinstance(current, datetime) or isinstance(proposed, datetime):
            if not isinstance(current, datetime) or not isinstance(proposed, datetime):
                return True
            return as_utc(current) != as_utc(proposed)
        if isinstance(current, (int, float)) and isinstance(proposed, (int, float)) and not isinstance(current, bool):
            return abs(float(current) - float(proposed)) > PRICE_TOLERANCE
        return current != proposed

    def _retire_stale_guards(self, db: Session, moment: datetime, deals: list, orders, positions, summary: ReconciliationSummary) -> None:
        """Retire PENDING reservations whose process died before the broker answered."""
        stale_before = moment - timedelta(seconds=self.settings.stale_guard_seconds)
        guards = list(db.scalars(select(ExecutionGuardRecord).where(ExecutionGuardRecord.status == "PENDING")))
        if not guards:
            return
        evidence = self._order_evidence(deals, orders, positions)
        for guard in guards:
            created = as_utc(guard.created_at) or moment
            if created > stale_before:
                continue
            key = guard.signal_id or guard.idempotency_key
            local = db.scalar(select(TradeRecord).where(TradeRecord.trade_id == key))
            if local is not None and local.status in GUARD_CONFIRMED_STATUSES:
                guard.status, guard.order_ticket = local.status, local.mt5_order_ticket or guard.order_ticket
                summary.guards_kept += 1
                logger.info("guard_confirmed_by_trade key=%s trade_id=%s status=%s", guard.idempotency_key, key, local.status)
                continue
            found = evidence.get(order_comment(key))
            if found is not None:
                guard.order_ticket = guard.order_ticket or found
                summary.guards_kept += 1
                logger.warning("guard_pending_order_found key=%s ticket=%s: an order exists at the broker, the guard is kept", guard.idempotency_key, guard.order_ticket)
                continue
            guard.status = RETIRED_GUARD_STATUS
            summary.guards_retired += 1
            self._abandon_signal(db, guard.signal_id, "execution guard retired: no broker order exists for it")
            db.add(AuditRecord(event_type="GUARD_RETIRED", severity="WARNING", message=f"stale PENDING execution guard {guard.idempotency_key} retired: no order found at the broker"))
            logger.warning("guard_retired key=%s signal_id=%s created_at=%s", guard.idempotency_key, guard.signal_id, created.isoformat())
        db.commit()

    def _abandon_signal(self, db: Session, signal_id: str | None, reason: str) -> None:
        if not signal_id:
            return
        row = db.scalar(select(SignalRecord).where(SignalRecord.signal_id == signal_id))
        if row is not None and row.status == "NEW":
            row.status = "ABANDONED"
            row.reason = f"{row.reason or ''} | {reason}".strip(" |")

    def _order_evidence(self, deals: list, orders, positions) -> dict[str, str]:
        """Comment -> ticket for every broker record that could belong to a reserved order."""
        evidence: dict[str, str] = {}
        for record in list(deals) + list(orders or ()) + list(positions or ()):
            comment = _text(getattr(record, "comment", None))
            ticket = _text(getattr(record, "ticket", None)) or _text(getattr(record, "order", None)) or _text(getattr(record, "position_id", None))
            if comment and ticket:
                evidence.setdefault(comment, ticket)
        return evidence
