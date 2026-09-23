"""Derived, fail-closed position sizing.

Every size comes from the account risk budget and the stop distance; a fixed lot is never an
input. All functions are pure and return ``None`` whenever a required input is missing, zero,
negative or unverifiable, so a caller must read ``None`` as "do not trade" instead of falling
back to a default volume. No MT5 import at module scope.
"""
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any

# Only sub-nanostep binary representation noise is treated as an exact step boundary.
_STEP_EPSILON = 1e-9


@dataclass(frozen=True)
class SymbolSpec:
    volume_min: float
    volume_max: float
    volume_step: float
    tick_value: float
    tick_size: float
    point: float
    trade_stops_level: int = 0
    freeze_level: int = 0
    contract_size: float = 0.0
    margin_per_lot: float = 0.0
    digits: int = 5
    filling_mode: int = 0


def _positive(*values: Any) -> bool:
    for value in values:
        if not isinstance(value, (int, float)) or isinstance(value, bool): return False
        if value <= 0: return False
    return True


def _floor_steps(quotient: float) -> int:
    if quotient <= 0: return 0
    nearest = round(quotient)
    if abs(quotient - nearest) <= _STEP_EPSILON: return int(nearest)
    return int(Decimal(str(quotient)).to_integral_value(rounding=ROUND_FLOOR))


def risk_per_lot(entry: float, stop_loss: float, spec: SymbolSpec) -> float | None:
    """Loss in account currency for one lot over the stop distance; None when not derivable."""
    if not _positive(entry, stop_loss): return None
    distance = abs(entry - stop_loss)
    if distance <= 0: return None
    if _positive(spec.tick_size, spec.tick_value):
        return distance / spec.tick_size * spec.tick_value
    # Fallback for profit-currency-quoted symbols: value of a full 1.0 price unit for one lot.
    if _positive(spec.contract_size): return distance * spec.contract_size
    return None


def normalize_volume(volume: float | None, spec: SymbolSpec) -> float | None:
    """Floor a volume to the broker step and bounds; None when it cannot reach volume_min."""
    if volume is None or not _positive(volume): return None
    if not _positive(spec.volume_min, spec.volume_max, spec.volume_step): return None
    steps = _floor_steps(volume / spec.volume_step)
    max_steps = _floor_steps(spec.volume_max / spec.volume_step)
    if steps <= 0 or max_steps <= 0: return None
    lots = float(min(steps, max_steps) * Decimal(str(spec.volume_step)))
    lots = round(lots, 8)
    return lots if lots >= spec.volume_min else None


def required_margin(lots: float, spec: SymbolSpec, price: float, leverage: float = 0.0) -> float | None:
    """Margin needed for ``lots``; None when the broker properties cannot answer it."""
    if not _positive(lots, price): return None
    if _positive(spec.margin_per_lot): return lots * spec.margin_per_lot
    if _positive(leverage, spec.contract_size): return lots * spec.contract_size * price / leverage
    return None


def margin_within_free_margin(required: float | None, free_margin: float | None, *, buffer_pct: float = 0.0) -> bool:
    """False for any unknown or negative input: an unverifiable margin check must not pass."""
    if required is None or free_margin is None: return False
    if not _positive(required, free_margin): return False
    return required <= free_margin * (1 - max(buffer_pct, 0.0) / 100)


def position_risk(lots: float, entry: float, stop_loss: float, spec: SymbolSpec) -> float | None:
    """Account-currency risk of an already sized position, used to prove the budget is respected."""
    per_lot = risk_per_lot(entry, stop_loss, spec)
    if per_lot is None or not _positive(lots): return None
    return lots * per_lot


def position_size(
    equity: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    spec: SymbolSpec,
    *,
    free_margin: float | None = None,
    price: float | None = None,
    leverage: float = 0.0,
) -> float | None:
    """Lots whose loss at ``stop_loss`` costs at most ``risk_pct`` of ``equity``.

    Returns None (fail closed) for any unusable input, for a size below volume_min, and when the
    required margin exceeds the supplied free margin. The result never exceeds the risk budget.
    """
    if not _positive(equity, risk_pct, entry, stop_loss): return None
    per_lot = risk_per_lot(entry, stop_loss, spec)
    if per_lot is None: return None
    budget = equity * risk_pct / 100
    if not _positive(budget): return None
    lots = normalize_volume(budget / per_lot, spec)
    if lots is None: return None
    if free_margin is not None:
        margin = required_margin(lots, spec, price if price is not None else entry, leverage)
        if not margin_within_free_margin(margin, free_margin): return None
    return lots


def size_for_loss_budget(loss_budget: float, entry: float, stop_loss: float, spec: SymbolSpec) -> float | None:
    """Lots whose loss at ``stop_loss`` costs at most ``loss_budget`` in account currency.

    This is the hard-limit sizing path: the dollar budget and the *technical* stop distance are the
    only inputs, so the volume is always derived and never fixed. The result is floored to
    ``volume_step`` and clamped by ``volume_min``/``volume_max``; ``None`` means "do not trade",
    which includes the case where even the broker's minimum volume would risk more than the budget.
    """
    if not _positive(loss_budget, entry, stop_loss): return None
    per_lot = risk_per_lot(entry, stop_loss, spec)
    if per_lot is None: return None
    return normalize_volume(loss_budget / per_lot, spec)


def minimum_volume_risk(entry: float, stop_loss: float, spec: SymbolSpec) -> float | None:
    """Account-currency loss of the broker minimum volume over the stop distance, or None."""
    per_lot = risk_per_lot(entry, stop_loss, spec)
    if per_lot is None or not _positive(spec.volume_min): return None
    return spec.volume_min * per_lot


def value_per_point_per_lot(spec: SymbolSpec) -> float | None:
    """Account-currency value of one ``point`` for one lot; None when the broker cannot answer."""
    if not _positive(spec.point): return None
    if _positive(spec.tick_size, spec.tick_value): return spec.point / spec.tick_size * spec.tick_value
    if _positive(spec.contract_size): return spec.point * spec.contract_size
    return None


def min_stop_distance(spec: SymbolSpec) -> float:
    """Broker minimum stop distance in price units (stops level and freeze level)."""
    levels = max(spec.trade_stops_level or 0, spec.freeze_level or 0)
    return max(levels, 0) * spec.point if _positive(spec.point) else 0.0


def spec_from_symbol_info(info: Any) -> SymbolSpec | None:
    """Convert an MT5 symbol_info object into a SymbolSpec; None when required fields are absent."""
    if info is None: return None

    def number(name: str, default: float = 0.0) -> float:
        value = getattr(info, name, None)
        try: return float(value)
        except (TypeError, ValueError): return default

    def integer(name: str, default: int = 0) -> int:
        return int(number(name, float(default)))

    point = number("point")
    tick_size = number("trade_tick_size") or point
    digits = integer("digits", 5)
    spec = SymbolSpec(
        volume_min=number("volume_min"),
        volume_max=number("volume_max"),
        volume_step=number("volume_step"),
        # Some brokers publish the tick value only on the profit-denominated field.
        tick_value=number("trade_tick_value") or number("trade_tick_value_profit"),
        tick_size=tick_size,
        point=point,
        trade_stops_level=integer("trade_stops_level"),
        freeze_level=integer("freeze_level"),
        contract_size=number("trade_contract_size"),
        margin_per_lot=number("margin_initial"),
        digits=digits if 0 <= digits <= 8 else 5,
        filling_mode=integer("filling_mode"),
    )
    if not _positive(spec.volume_min, spec.volume_max, spec.volume_step, spec.point): return None
    if spec.volume_min > spec.volume_max: return None
    return spec
