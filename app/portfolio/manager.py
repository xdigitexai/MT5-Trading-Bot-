from dataclasses import dataclass
from app.mt5.gateway import MT5Gateway, mt5

@dataclass(frozen=True)
class TrailingPolicy:
    break_even_r: float = 1.0
    activate_r: float = 1.5
    atr_multiple: float = 1.0
    minimum_change_points: int = 5

class PositionManager:
    """Makes at most one material, protective SL change per observation."""
    def __init__(self, gateway: MT5Gateway, policy: TrailingPolicy = TrailingPolicy()): self.gateway, self.policy = gateway, policy
    def manage(self, position, current_price: float, atr_value: float, point: float):
        if not position.sl or atr_value <= 0 or point <= 0: return None
        risk = abs(position.price_open - position.sl)
        if risk <= 0: return None
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        progress = (current_price-position.price_open)/risk if is_buy else (position.price_open-current_price)/risk
        if progress < self.policy.break_even_r: return None
        proposed = position.price_open if progress < self.policy.activate_r else (current_price-atr_value*self.policy.atr_multiple if is_buy else current_price+atr_value*self.policy.atr_multiple)
        improves = proposed > position.sl if is_buy else proposed < position.sl
        if not improves or abs(proposed-position.sl)/point < self.policy.minimum_change_points: return None
        return self.gateway.modify_position(position.ticket, position.symbol, proposed, position.tp)
