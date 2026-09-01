"""Reconcile open MT5 positions against current risk rules (read-only)."""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.mt5.gateway import mt5


@dataclass
class PositionRiskSnapshot:
    ticket: int
    symbol: str
    side: str
    volume: float
    entry: float
    current: float
    sl: float
    tp: float
    floating_profit: float
    estimated_loss_at_sl: float | None
    estimated_profit_at_tp: float | None
    risk_r: float | None
    magic: int
    comment: str
    violation: str | None


def audit_position(pos, equity: float, settings: Settings) -> PositionRiskSnapshot:
    side = "BUY" if pos.type == 0 else "SELL"
    entry = float(pos.price_open)
    volume = float(pos.volume)
    sl = float(pos.sl or 0)
    tp = float(pos.tp or 0)
    profit = float(getattr(pos, "profit", 0) or 0)
    current = float(getattr(pos, "price_current", 0) or entry)
    magic = int(getattr(pos, "magic", 0) or 0)
    comment = str(getattr(pos, "comment", "") or "")
    est_loss = est_tp = risk_r = None
    violation = None

    max_demo = float(getattr(settings, "max_demo_volume", 0.10) or 0.10)
    if volume > max_demo + 1e-9:
        violation = f"volume {volume} exceeds demo_volume_cap {max_demo}"

    risk_amount = equity * settings.risk_per_trade_pct / 100.0
    if mt5 is not None and sl > 0:
        try:
            order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
            calc = mt5.order_calc_profit(order_type, pos.symbol, volume, entry, sl)
            if calc is not None:
                est_loss = abs(float(calc))
                if est_loss > risk_amount * 1.05:
                    msg = f"estimated_loss_at_SL {est_loss:.2f} > risk_budget {risk_amount:.2f}"
                    violation = f"{violation}; {msg}" if violation else msg
            if tp > 0:
                calc_tp = mt5.order_calc_profit(order_type, pos.symbol, volume, entry, tp)
                if calc_tp is not None:
                    est_tp = abs(float(calc_tp))
            if est_loss and est_loss > 0 and profit is not None:
                risk_r = float(profit) / est_loss if est_loss else None
        except Exception:
            pass

    return PositionRiskSnapshot(
        ticket=int(pos.ticket),
        symbol=pos.symbol,
        side=side,
        volume=volume,
        entry=entry,
        current=current,
        sl=sl,
        tp=tp,
        floating_profit=profit,
        estimated_loss_at_sl=est_loss,
        estimated_profit_at_tp=est_tp,
        risk_r=risk_r,
        magic=magic,
        comment=comment,
        violation=violation,
    )
