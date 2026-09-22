import logging

from app.core.config import Settings
from app.core.schemas import RiskDecision, Signal
from app.risk.sizing import (
    SymbolSpec,
    margin_within_free_margin,
    min_stop_distance,
    position_size,
    required_margin,
)

logger = logging.getLogger(__name__)
__all__ = ["RiskEngine", "SymbolSpec"]


def exposure_pct(notional: float, equity: float) -> float | None:
    if equity is None or equity <= 0 or notional is None or notional < 0: return None
    return notional / equity * 100


class RiskEngine:
    """Pre-trade gate.

    Limits are read from the persistent risk state when a store is supplied, and the updated
    state is written back, so a restart cannot silently reset the daily-loss or drawdown limits
    or clear an emergency lock. Routine rejections are returned as reasons, never raised.
    """

    def __init__(self, settings: Settings): self.settings = settings

    def lot_size(self, equity: float, entry: float, stop: float, spec: SymbolSpec) -> float:
        """Legacy helper: risk-derived lots, 0.0 when the size is not usable."""
        lots = position_size(equity, self.settings.risk_per_trade_pct, entry, stop, spec)
        return lots if lots is not None else 0.0

    def size(
        self,
        equity: float,
        entry: float,
        stop: float,
        spec: SymbolSpec,
        *,
        free_margin: float | None = None,
        leverage: float = 0.0,
    ) -> float | None:
        return position_size(equity, self.settings.risk_per_trade_pct, entry, stop, spec, free_margin=free_margin, price=entry, leverage=leverage)

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
        state=None,
        day=None,
        exposure: float | None = None,
        margin_level: float | None = None,
        free_margin: float | None = None,
        leverage: float = 0.0,
    ) -> RiskDecision:
        if state is not None:
            row = state.load(day)
            daily_pnl = float(row.realized_pnl or 0.0)
            if row.peak_equity: balance_peak = float(row.peak_equity)
            if row.emergency_locked: trading_enabled = False
            state.observe_equity(equity, day)
        reasons = []
        if not trading_enabled: reasons.append("bot is stopped or emergency locked")
        if not account_available: reasons.append("account unavailable")
        if not symbol_valid: reasons.append("symbol invalid")
        if not market_open: reasons.append("market closed")
        if not tick_valid: reasons.append("invalid tick")
        if self.settings.risk_per_trade_pct > self.settings.max_risk_per_trade_pct: reasons.append("risk per trade exceeds configured guard")
        if signal.action == "HOLD" or signal.score < self.settings.min_signal_score: reasons.append("signal quality below threshold")
        if not signal.entry or not signal.stop_loss or not signal.take_profit: reasons.append("missing mandatory entry/SL/TP")
        if signal.entry and signal.stop_loss and abs(signal.entry - signal.stop_loss) < min_stop_distance(spec): reasons.append("stop loss violates broker stop distance")
        if spread_points > self.settings.max_spread_points_default: reasons.append("spread protection triggered")
        if daily_pnl <= -(equity * self.settings.max_daily_loss_pct / 100): reasons.append("daily loss limit reached")
        if balance_peak > 0 and (balance_peak - equity) / balance_peak * 100 >= self.settings.max_drawdown_pct:
            reasons.append("maximum drawdown emergency stop")
            if state is not None: state.set_emergency_locked(True, day)
            logger.error("risk_emergency_lock equity=%.2f peak=%.2f limit_pct=%s persisted=%s", equity, balance_peak, self.settings.max_drawdown_pct, state is not None)
        if open_positions >= self.settings.max_open_positions: reasons.append("maximum simultaneous positions reached")
        if symbol_positions >= self.settings.max_positions_per_symbol: reasons.append("maximum positions per symbol reached")
        current_exposure = exposure_pct(exposure, equity) if exposure is not None else None
        if exposure is not None and current_exposure is None: reasons.append("total exposure cannot be verified")
        elif current_exposure is not None and current_exposure > self.settings.max_total_exposure_pct: reasons.append("total exposure limit reached")
        if margin_level is not None and margin_level < self.settings.min_margin_level_pct: reasons.append("margin level below minimum")
        if not correlation_allowed: reasons.append("correlated exposure limit reached")
        if not margin_sufficient: reasons.append("insufficient margin")
        if duplicate_exists: reasons.append("duplicate position or order exists")
        if not news_clear: reasons.append("news filter blocks trading")
        volume = self.size(equity, signal.entry or 0, signal.stop_loss or 0, spec)
        if volume is None: reasons.append("risk-based lot sizing below broker minimum")
        elif free_margin is not None:
            margin = required_margin(volume, spec, signal.entry or 0, leverage)
            if not margin_within_free_margin(margin, free_margin): reasons.append("insufficient margin for risk-sized volume")
        if reasons:
            logger.warning("risk_rejected symbol=%s strategy=%s equity=%.2f reasons=%s", signal.symbol, signal.strategy, equity, reasons)
        else:
            logger.info("risk_approved symbol=%s strategy=%s equity=%.2f volume=%s", signal.symbol, signal.strategy, equity, volume)
        return RiskDecision(approved=not reasons, reasons=reasons, volume=volume)
