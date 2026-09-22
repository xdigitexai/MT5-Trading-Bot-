"""Strategy registry.

Every entry in STRATEGIES exposes the shared contract:

    evaluate(symbol, h4, h1, m15, rr=2.0) -> Signal

`ensemble` is deliberately NOT part of STRATEGIES: it is not an evaluate() strategy but a combiner
that consumes the other strategies' signals, so it is exposed as ENSEMBLE and through
`ensemble.combine(signals, settings)`. HOLD signals below are fail-closed abstentions.
"""
from collections.abc import Callable
import pandas as pd
from app.core.schemas import Signal
from app.strategies import breakout, mean_reversion, momentum, trend_following, volatility
from app.strategies.ensemble import Ensemble, combine

ENSEMBLE = "ensemble"
DEFAULT_TIMEFRAME = "M15"
Strategy = Callable[[str, pd.DataFrame, pd.DataFrame, pd.DataFrame, float], Signal]

STRATEGIES: dict[str, Strategy] = {
    "trend_following": trend_following.evaluate,
    "breakout": breakout.evaluate,
    "momentum": momentum.evaluate,
    "mean_reversion": mean_reversion.evaluate,
    "volatility": volatility.evaluate,
}
STRATEGY_NAMES: tuple[str, ...] = (*STRATEGIES, ENSEMBLE)

def enabled_strategies(settings) -> list[str]:
    """Enabled standalone strategies in canonical order; the ensemble combiner is excluded."""
    return [name for name in STRATEGIES if name in settings.enabled_strategies]

def ensemble_enabled(settings) -> bool:
    """True when the combiner should run over the enabled strategies' signals."""
    return ENSEMBLE in settings.enabled_strategies

def strategy_timeframe(settings, name: str) -> str:
    """Configured timeframe for a strategy, falling back to its own default."""
    return settings.strategy_timeframes.get(name, DEFAULT_TIMEFRAME)

__all__ = ["ENSEMBLE", "STRATEGIES", "STRATEGY_NAMES", "Ensemble", "combine", "enabled_strategies", "ensemble_enabled", "strategy_timeframe"]
