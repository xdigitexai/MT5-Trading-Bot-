from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from app.core.config import Settings
from app.core.schemas import RiskDecision, Signal

@dataclass(frozen=True)
class SymbolSpec:
    volume_min: float; volume_max: float; volume_step: float; tick_value: float; tick_size: float; point: float; trade_stops_level: int

class RiskEngine:
    def __init__(self, settings: Settings): self.settings = settings
    def lot_size(self, equity: float, entry: float, stop: float, spec: SymbolSpec) -> float:
        distance = abs(entry - stop)
        if equity <= 0 or distance <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0: return 0
        raw = (equity * self.settings.risk_per_trade_pct / 100) / (distance / spec.tick_size * spec.tick_value)
        # Decimal floor avoids binary-float artifacts (e.g. 0.05 becoming
        # 0.049999... and incorrectly rounding down to 0.04).
        quotient = raw / spec.volume_step
        nearest = round(quotient)
        # Treat only sub-nanostep representation noise as the boundary value.
        if abs(quotient - nearest) <= 1e-9:
            steps = Decimal(nearest)
        else:
            steps = Decimal(str(quotient)).to_integral_value(rounding=ROUND_FLOOR)
        rounded = float(steps * Decimal(str(spec.volume_step)))
        return round(min(spec.volume_max, rounded), 8) if rounded >= spec.volume_min else 0
    def approve(self, signal: Signal, *, equity: float, balance_peak: float, daily_pnl: float, open_positions: int, symbol_positions: int, spread_points: float, spec: SymbolSpec, trading_enabled: bool, news_clear: bool, account_available: bool = True, symbol_valid: bool = True, market_open: bool = True, tick_valid: bool = True, margin_sufficient: bool = True, duplicate_exists: bool = False, correlation_allowed: bool = True) -> RiskDecision:
        reasons=[]
        if not trading_enabled: reasons.append("bot is stopped or emergency locked")
        if not account_available: reasons.append("account unavailable")
        if not symbol_valid: reasons.append("symbol invalid")
        if not market_open: reasons.append("market closed")
        if not tick_valid: reasons.append("invalid tick")
        if signal.action == "HOLD" or signal.score < self.settings.min_signal_score: reasons.append("signal quality below threshold")
        if not signal.entry or not signal.stop_loss or not signal.take_profit: reasons.append("missing mandatory entry/SL/TP")
        if signal.entry and signal.stop_loss and abs(signal.entry - signal.stop_loss) < spec.trade_stops_level * spec.point: reasons.append("stop loss violates broker stop distance")
        if spread_points > self.settings.max_spread_points_default: reasons.append("spread protection triggered")
        if daily_pnl <= -(equity * self.settings.max_daily_loss_pct / 100): reasons.append("daily loss limit reached")
        if balance_peak > 0 and (balance_peak-equity)/balance_peak*100 >= self.settings.max_drawdown_pct: reasons.append("maximum drawdown emergency stop")
        if open_positions >= self.settings.max_open_positions: reasons.append("maximum simultaneous positions reached")
        if symbol_positions >= self.settings.max_positions_per_symbol: reasons.append("maximum positions per symbol reached")
        if not correlation_allowed: reasons.append("correlated exposure limit reached")
        if not margin_sufficient: reasons.append("insufficient margin")
        if duplicate_exists: reasons.append("duplicate position or order exists")
        if not news_clear: reasons.append("news filter blocks trading")
        volume = self.lot_size(equity, signal.entry or 0, signal.stop_loss or 0, spec)
        if volume == 0: reasons.append("risk-based lot sizing below broker minimum")
        return RiskDecision(approved=not reasons, reasons=reasons, volume=volume or None)
