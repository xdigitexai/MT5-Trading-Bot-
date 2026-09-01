"""Risk engine: position sizing and pre-trade approval.

Sizing formula (per 1.0 lot):
  risk_amount   = equity * risk_per_trade_pct / 100
  ticks_to_sl   = abs(entry - stop) / tick_size
  loss_per_lot  = ticks_to_sl * tick_value
  raw_volume    = risk_amount / loss_per_lot
  volume        = floor_to_step(raw_volume) within [volume_min, volume_max]

Hard gates (any failure → reject, never clamp-and-send):
  - estimated_loss_at_SL <= risk_amount * (1 + tolerance)
  - volume <= max_demo_volume (DEMO only)
  - volume >= volume_min only if that min does not exceed risk
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from app.core.config import Settings
from app.core.schemas import RiskDecision, Signal


@dataclass(frozen=True)
class SymbolSpec:
    volume_min: float
    volume_max: float
    volume_step: float
    tick_value: float
    tick_size: float
    point: float
    trade_stops_level: int


@dataclass(frozen=True)
class SizeAudit:
    equity: float
    risk_pct: float
    risk_amount: float
    entry: float
    stop: float
    sl_distance: float
    tick_size: float
    tick_value: float
    loss_per_lot: float
    raw_volume: float
    normalized_volume: float
    estimated_loss_at_sl: float
    risk_check: str  # PASS | FAIL
    reject_reason: str | None = None


class RiskEngine:
    # Allow 2% tolerance for float/broker rounding on estimated loss vs target risk
    LOSS_TOLERANCE = 1.02

    def __init__(self, settings: Settings):
        self.settings = settings

    def lot_size(self, equity: float, entry: float, stop: float, spec: SymbolSpec) -> float:
        audit = self.size_audit(equity, entry, stop, spec)
        if audit.risk_check != "PASS":
            return 0.0
        return audit.normalized_volume

    def size_audit(self, equity: float, entry: float, stop: float, spec: SymbolSpec) -> SizeAudit:
        risk_pct = float(self.settings.risk_per_trade_pct)
        risk_amount = equity * risk_pct / 100.0
        distance = abs(entry - stop)

        base = dict(
            equity=equity,
            risk_pct=risk_pct,
            risk_amount=risk_amount,
            entry=entry,
            stop=stop,
            sl_distance=distance,
            tick_size=spec.tick_size,
            tick_value=spec.tick_value,
            loss_per_lot=0.0,
            raw_volume=0.0,
            normalized_volume=0.0,
            estimated_loss_at_sl=0.0,
        )

        if equity <= 0:
            return SizeAudit(**base, risk_check="FAIL", reject_reason="invalid_equity")
        if distance <= 0:
            return SizeAudit(**base, risk_check="FAIL", reject_reason="invalid_sl_distance")
        if spec.tick_size <= 0 or spec.tick_value <= 0:
            return SizeAudit(**base, risk_check="FAIL", reject_reason="invalid_tick_spec")
        if spec.volume_step <= 0 or spec.volume_min <= 0:
            return SizeAudit(**base, risk_check="FAIL", reject_reason="invalid_volume_spec")

        loss_per_lot = (distance / spec.tick_size) * spec.tick_value
        if loss_per_lot <= 0:
            return SizeAudit(**base, loss_per_lot=loss_per_lot, risk_check="FAIL", reject_reason="invalid_loss_per_lot")

        raw = risk_amount / loss_per_lot

        # Floor to volume_step (never round up risk)
        quotient = raw / spec.volume_step
        nearest = round(quotient)
        if abs(quotient - nearest) <= 1e-9:
            steps = Decimal(nearest)
        else:
            steps = Decimal(str(quotient)).to_integral_value(rounding=ROUND_FLOOR)
        normalized = float(steps * Decimal(str(spec.volume_step)))
        normalized = round(normalized, 8)

        # Min lot exceeds risk budget → reject (do not force min lot)
        min_loss = (distance / spec.tick_size) * spec.tick_value * spec.volume_min
        if normalized < spec.volume_min:
            if min_loss > risk_amount * self.LOSS_TOLERANCE:
                return SizeAudit(
                    **{**base, "loss_per_lot": loss_per_lot, "raw_volume": raw,
                       "normalized_volume": 0.0, "estimated_loss_at_sl": min_loss},
                    risk_check="FAIL",
                    reject_reason="min_volume_exceeds_risk",
                )
            return SizeAudit(
                **{**base, "loss_per_lot": loss_per_lot, "raw_volume": raw,
                   "normalized_volume": 0.0, "estimated_loss_at_sl": 0.0},
                risk_check="FAIL",
                reject_reason="risk_based_lot_sizing_below_broker_minimum",
            )

        # Never raise to volume_max if that exceeds risk — reject instead of clamp-and-send
        if normalized > spec.volume_max:
            return SizeAudit(
                **{**base, "loss_per_lot": loss_per_lot, "raw_volume": raw,
                   "normalized_volume": normalized, "estimated_loss_at_sl": loss_per_lot * normalized},
                risk_check="FAIL",
                reject_reason="volume_exceeds_broker_max",
            )

        estimated_loss = loss_per_lot * normalized
        if estimated_loss > risk_amount * self.LOSS_TOLERANCE:
            return SizeAudit(
                **{**base, "loss_per_lot": loss_per_lot, "raw_volume": raw,
                   "normalized_volume": normalized, "estimated_loss_at_sl": estimated_loss},
                risk_check="FAIL",
                reject_reason="position_size_exceeds_risk",
            )

        # DEMO hard cap
        max_demo = float(getattr(self.settings, "max_demo_volume", 1.0) or 1.0)
        if normalized > max_demo:
            return SizeAudit(
                **{**base, "loss_per_lot": loss_per_lot, "raw_volume": raw,
                   "normalized_volume": normalized, "estimated_loss_at_sl": estimated_loss},
                risk_check="FAIL",
                reject_reason="demo_volume_cap",
            )

        return SizeAudit(
            **{**base, "loss_per_lot": loss_per_lot, "raw_volume": raw,
               "normalized_volume": normalized, "estimated_loss_at_sl": estimated_loss},
            risk_check="PASS",
            reject_reason=None,
        )

    def approve(
        self,
        signal: Signal,
        *,
        equity: float,
        balance_peak: float,
        daily_pnl: float,
        open_positions: int,
        symbol_positions: int,
        spread_points: float,
        spec: SymbolSpec,
        trading_enabled: bool,
        news_clear: bool,
        account_available: bool = True,
        symbol_valid: bool = True,
        market_open: bool = True,
        tick_valid: bool = True,
        margin_sufficient: bool = True,
        duplicate_exists: bool = False,
        correlation_allowed: bool = True,
    ) -> RiskDecision:
        reasons: list[str] = []
        if not trading_enabled:
            reasons.append("bot is stopped or emergency locked")
        if not account_available:
            reasons.append("account unavailable")
        if not symbol_valid:
            reasons.append("symbol invalid")
        if not market_open:
            reasons.append("market closed")
        if not tick_valid:
            reasons.append("invalid tick")
        if signal.action == "HOLD" or signal.score < self.settings.min_signal_score:
            reasons.append("signal quality below threshold")
        if not signal.entry or not signal.stop_loss or not signal.take_profit:
            reasons.append("missing mandatory entry/SL/TP")
        if (
            signal.entry
            and signal.stop_loss
            and abs(signal.entry - signal.stop_loss) < spec.trade_stops_level * spec.point
        ):
            reasons.append("stop loss violates broker stop distance")
        if spread_points > self.settings.max_spread_points_default:
            reasons.append("spread protection triggered")
        if daily_pnl <= -(equity * self.settings.max_daily_loss_pct / 100):
            reasons.append("daily loss limit reached")
        if balance_peak > 0 and (balance_peak - equity) / balance_peak * 100 >= self.settings.max_drawdown_pct:
            reasons.append("maximum drawdown emergency stop")
        if open_positions >= self.settings.max_open_positions:
            reasons.append("maximum simultaneous positions reached")
        if symbol_positions >= self.settings.max_positions_per_symbol:
            reasons.append("maximum positions per symbol reached")
        if not correlation_allowed:
            reasons.append("correlated exposure limit reached")
        if not margin_sufficient:
            reasons.append("insufficient margin")
        if duplicate_exists:
            reasons.append("duplicate position or order exists")
        if not news_clear:
            reasons.append("news filter blocks trading")

        audit = self.size_audit(equity, signal.entry or 0, signal.stop_loss or 0, spec)
        volume = audit.normalized_volume if audit.risk_check == "PASS" else 0.0
        if audit.risk_check != "PASS":
            reasons.append(audit.reject_reason or "position_size_exceeds_risk")

        return RiskDecision(approved=not reasons, reasons=reasons, volume=volume or None)
