"""Ensemble combiner. Single active strategy passes through."""
from app.core.schemas import Signal, SignalAction

def combine(signals: list[Signal]) -> Signal:
    if not signals:
        raise ValueError("ensemble requires at least one signal")
    active = [x for x in signals if x.action != SignalAction.HOLD]
    if not active:
        return signals[0].model_copy(update={"action": SignalAction.HOLD, "score": 0})
    if len(active) == 1:
        return active[0]
    direction = active[0].action
    aligned = [x for x in active if x.action == direction]
    if len(aligned) >= 2:
        return max(aligned, key=lambda x: x.score)
    return active[0].model_copy(
        update={"action": SignalAction.HOLD, "score": 0,
                "reasons": list(active[0].reasons) + ["ensemble disagreement"]}
    )
