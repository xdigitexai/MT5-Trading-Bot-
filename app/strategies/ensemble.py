"""Ensemble voting engine for the strategy layer.

Confidence here is a QUALITY SCORE produced by the individual strategies: it counts how many of
their confirmation rules a setup satisfied. It is NOT a probability of profit and NOT a win rate.

Voting rules, configurable through app.core.config.Settings:
- Only non-HOLD signals are eligible votes; HOLD is an abstention, not a vote against.
- Votes are weighted by confidence, so a stronger setup counts for more than a weak one.
- The direction with the greater total weight wins; equal weight is reported as disagreement.
- The winning side needs at least `ensemble_min_votes` agreeing strategies.
- A disagreeing strategy suppresses the result rather than silently cancelling it: the aggregate
  confidence is (winning weight - opposing weight) / agreeing votes, and it must reach
  `ensemble_min_confidence` for the trade to be emitted.
- Entry, stop and target come from the strongest agreeing signal; its bracket is revalidated here
  and an incomplete or inverted bracket fails closed to HOLD.
"""
from datetime import datetime, timezone
from app.core.config import Settings, get_settings
from app.core.schemas import Signal, SignalAction
from app.strategies._common import hold as hold_signal

STRATEGY = "ensemble"
TIMEFRAME = "M15"

def combine(signals: list[Signal], settings: Settings | None = None) -> Signal:
    """Combines one signal per strategy into a single voted signal, or a HOLD carrying the reason."""
    configuration = settings or get_settings()
    symbol = signals[0].symbol if signals else "UNKNOWN"
    eligible = [vote for vote in signals if vote.action is not SignalAction.HOLD]
    if not eligible:
        return _hold(symbol, "ensemble holds: no eligible strategy votes", signals)
    weight = {action: sum(vote.confidence for vote in eligible if vote.action is action) for action in (SignalAction.BUY, SignalAction.SELL)}
    if weight[SignalAction.BUY] == weight[SignalAction.SELL]:
        return _hold(symbol, "ensemble disagreement: opposing votes carry equal weight", eligible)
    action = SignalAction.BUY if weight[SignalAction.BUY] > weight[SignalAction.SELL] else SignalAction.SELL
    opposing = weight[SignalAction.BUY if action is SignalAction.SELL else SignalAction.SELL]
    agreeing = [vote for vote in eligible if vote.action is action]
    if len(agreeing) < configuration.ensemble_min_votes:
        return _hold(symbol, f"insufficient votes: {len(agreeing)} of {configuration.ensemble_min_votes} required", agreeing)
    aggregate = (weight[action] - opposing) / len(agreeing)
    if aggregate < configuration.ensemble_min_confidence:
        return _hold(symbol, f"insufficient confidence: {aggregate:.2f} below {configuration.ensemble_min_confidence:.2f}", agreeing)
    strongest = max(agreeing, key=lambda vote: vote.score)
    levels = levels_of(action, strongest)
    if levels is None:
        return _hold(symbol, f"strongest agreeing vote ({strongest.strategy}) has an incomplete bracket", agreeing)
    entry, stop_loss, take_profit = levels
    bounded = min(max(aggregate, 0.0), 1.0)
    reasons = [
        f"ensemble {action.value} from {len(agreeing)} agreeing vote(s) of {len(eligible)} eligible",
        f"confidence-weighted agreement {weight[action]:.2f} against {opposing:.2f} opposing weight",
        f"aggregate quality score {bounded:.2f} (a confirmation quality score, not a probability of profit)",
        f"levels taken from the strongest agreeing vote: {strongest.strategy} (score {strongest.score})",
    ]
    return Signal(action=action, confidence=bounded, score=round(bounded * 100), symbol=symbol, entry=entry, stop_loss=stop_loss, take_profit=take_profit, strategy=STRATEGY, timeframe=strongest.timeframe or TIMEFRAME, reasons=reasons, timestamp=datetime.now(timezone.utc))

def levels_of(action: SignalAction, signal: Signal) -> tuple[float, float, float] | None:
    """The signal's entry/stop/target, or None when the bracket is missing or on the wrong side."""
    entry, stop_loss, take_profit = signal.entry, signal.stop_loss, signal.take_profit
    if entry is None or stop_loss is None or take_profit is None:
        return None
    if action is SignalAction.BUY and not stop_loss < entry < take_profit:
        return None
    if action is SignalAction.SELL and not take_profit < entry < stop_loss:
        return None
    return float(entry), float(stop_loss), float(take_profit)

def _hold(symbol: str, reason: str, source: list[Signal]) -> Signal:
    return hold_signal(symbol, STRATEGY, reason, source[0].timeframe if source else TIMEFRAME)

class Ensemble:
    """Reusable settings-bound wrapper around combine()."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def combine(self, signals: list[Signal]) -> Signal:
        return combine(signals, self.settings)
