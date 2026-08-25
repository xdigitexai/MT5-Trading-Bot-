from app.core.schemas import Signal, SignalAction

def combine(signals: list[Signal]) -> Signal:
    active = [x for x in signals if x.action != SignalAction.HOLD]
    if not active: return signals[0].model_copy(update={"action": SignalAction.HOLD, "score": 0})
    direction = active[0].action
    aligned = [x for x in active if x.action == direction]
    return max(aligned, key=lambda x: x.score) if len(aligned) >= 2 else active[0].model_copy(update={"action": SignalAction.HOLD, "reasons": ["ensemble disagreement"]})
