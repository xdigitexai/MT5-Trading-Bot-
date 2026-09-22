"""Pre-send stop validation.

No order may leave the bot without a protective stop loss. Every rule here fails closed: a
missing, zero-distance, wrong-sided or too-close stop, and a take-profit that breaks the
requested risk/reward, are all returned as explicit reasons for the caller to reject the order.
"""
from dataclasses import dataclass, field

from app.risk.sizing import SymbolSpec, min_stop_distance

# Float comparison tolerance for prices; one tenth of a point is below any broker's tick.
_PRICE_EPSILON = 1e-12


@dataclass(frozen=True)
class StopValidation:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _side(action: str) -> str | None:
    text = str(action).upper()
    return text if text in ("BUY", "SELL") else None


def validate_stops(
    action: str,
    entry: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    spec: SymbolSpec,
    *,
    min_risk_reward: float | None = None,
) -> StopValidation:
    reasons: list[str] = []
    side = _side(action)
    if side is None: reasons.append("order side must be BUY or SELL")
    if entry is None or entry <= 0: reasons.append("entry price is missing or invalid")
    if stop_loss is None or stop_loss <= 0: reasons.append("stop loss is required and must be a positive price")
    if side is None or entry is None or stop_loss is None or entry <= 0 or stop_loss <= 0:
        return StopValidation(False, reasons)
    risk = abs(entry - stop_loss)
    if risk <= 0: reasons.append("stop loss distance must be greater than zero")
    else:
        minimum = min_stop_distance(spec)
        if minimum > 0 and risk < minimum: reasons.append("stop loss is closer than the broker minimum stop distance")
        if side == "BUY":
            if stop_loss >= entry - _PRICE_EPSILON: reasons.append("BUY stop loss must be below the entry price")
        elif stop_loss <= entry + _PRICE_EPSILON: reasons.append("SELL stop loss must be above the entry price")
    if take_profit is not None:
        if take_profit <= 0: reasons.append("take profit must be a positive price")
        else:
            if side == "BUY":
                if take_profit <= entry + _PRICE_EPSILON: reasons.append("BUY take profit must be above the entry price")
            elif take_profit >= entry - _PRICE_EPSILON: reasons.append("SELL take profit must be below the entry price")
            reward = abs(take_profit - entry)
            if min_risk_reward is not None and risk > 0 and reward / risk < min_risk_reward - 1e-9:
                reasons.append(f"take profit violates the {min_risk_reward:g}:1 risk/reward requirement")
    return StopValidation(not reasons, reasons)
