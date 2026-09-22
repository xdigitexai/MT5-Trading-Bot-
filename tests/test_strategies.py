"""Strategy layer tests.

Fixtures are deterministic OHLCV frames built with numpy/pandas only: sine ranges, patterned
trends and explicitly forced final candles. Confidence values are treated as quality scores
throughout, never as probabilities of profit.
"""
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from app.core.config import Settings, TradingMode
from app.core.schemas import Signal, SignalAction
from app.indicators.technical import atr_percentile, bollinger, donchian, roc, sma, stochastic, volume_series
from app.strategies import ENSEMBLE, STRATEGIES, STRATEGY_NAMES, Ensemble, combine, enabled_strategies, ensemble_enabled, strategy_timeframe
from app.strategies import breakout, mean_reversion, momentum, volatility
from app.strategies.ensemble import levels_of

NEW_STRATEGIES = (breakout, momentum, mean_reversion, volatility)
STRATEGY_IDS = [module.STRATEGY for module in NEW_STRATEGIES]

def candle(close, spread=0.0002):
    """OHLC frame where each candle opens at the previous close."""
    close = np.asarray(close, dtype=float)
    spread = np.asarray(spread, dtype=float)
    open_ = np.r_[close[0], close[:-1]]
    return paint(open_, np.maximum(open_, close) + spread, np.minimum(open_, close) - spread, close)

def paint(open_, high, low, close):
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close})

def trending(direction, n=260, step=0.0002, pullback=False):
    close = 1.10 + direction * step * np.arange(n)
    if pullback:
        # One strictly negative close-to-close step keeps RSI(14) defined; a monotone series divides by zero.
        close[-5] -= direction * step * 1.5
    return candle(close)

def breakout_frames(direction, n=260, size=0.0030, repeat=False, wicked=False):
    """Sine range plus a forced close beyond the prior 20-candle extreme.

    repeat=True forces buffered breaks on the preceding bars (already extended, never fresh) and
    wicked=True leaves the last close inside the range behind a long wick.
    """
    base = candle(1.10 + 0.0008 * np.sin(np.arange(n) / 3.0))
    close, open_, high, low = (base[column].to_numpy().copy() for column in ("close", "open", "high", "low"))
    open_[-1] = close[-2]
    move = size * 0.05 if wicked else size
    close[-1] = close[-2] + direction * move
    if direction > 0:
        high[-1] = close[-2] + size
        low[-1] = open_[-1] - move * 0.6
    else:
        low[-1] = close[-2] - size
        high[-1] = open_[-1] + move * 0.6
    for offset in range(2, 6) if repeat else ():
        open_[offset * -1] = close[-offset - 1]
        close[offset * -1] = close[-offset - 1] + direction * size
        if direction > 0:
            high[offset * -1] = close[offset * -1] + size * 0.1
            low[offset * -1] = open_[offset * -1] - size * 0.2
        else:
            low[offset * -1] = close[offset * -1] - size * 0.1
            high[offset * -1] = open_[offset * -1] + size * 0.2
    return paint(open_, high, low, close)

def momentum_frames(direction, n=260, unit=0.0004, boost=0.0):
    """Trend built from a 4-bar rhythm of three up bars and one deeper down bar.

    The rhythm keeps RSI(14) inside the momentum band instead of pinned at an extreme. boost adds
    an extra final thrust to build the overextended case.
    """
    cycle = np.array([1.0, 1.0, 1.0, -2.0]) * unit * direction
    close = np.r_[1.10, 1.10 + np.cumsum(np.resize(cycle, n - 1))]
    close[-1] += boost * direction
    return candle(close)

def mean_reversion_frames(direction, n=260, amplitude=0.002, period=32, slide=0.008, bounce=0.0004):
    """Wide sine range with a forced three-bar slide into a band plus a reversal close."""
    base = 1.10 + amplitude * np.sin(2 * np.pi * np.arange(n) / period)
    close = base.copy()
    close[-3:] = base[-3:] - np.linspace(slide / 4, slide, 3) * direction
    close[-1] = close[-2] + bounce * direction
    return candle(close, spread=0.0004)

def volatility_frames(direction, spike=False, quiet=100, n=161):
    """Quiet base, then a 61-bar volatility expansion with a forced range break.

    spike=True turns the final 16 bars into a volatility spike far above the recent norm.
    """
    index = np.arange(n)
    wobble = np.r_[0.00002 * np.sin(np.arange(quiet) / 1.7), 0.0006 * np.sin(np.arange(n - quiet) / 2.5)]
    close = 1.10 + wobble + np.where(index < quiet, 0.0, 0.0001 * (index - quiet)) * direction
    spread = np.where(index < quiet, 0.0001, 0.0005)
    if spike:
        spread[-16:] = 0.0080
    base = candle(close, spread)
    open_, high, low, closes = (base[column].to_numpy().copy() for column in ("open", "high", "low", "close"))
    open_[-1] = closes[-2]
    closes[-1] = closes[-2] + direction * 0.004
    if direction > 0:
        high[-1] = closes[-1] + 0.0002
        low[-1] = open_[-1] - 0.0020
    else:
        low[-1] = closes[-1] - 0.0002
        high[-1] = open_[-1] + 0.0020
    return paint(open_, high, low, closes)

def trade_frames(module, direction):
    if module is breakout:
        return trending(direction), breakout_frames(direction)
    if module is momentum:
        return trending(direction), momentum_frames(direction)
    if module is mean_reversion:
        return trending(direction), mean_reversion_frames(direction)
    return volatility_frames(direction), volatility_frames(direction)

def vote(strategy, action, confidence, entry=1.1000, risk=0.0020, rr=2.0):
    if action is SignalAction.HOLD:
        return Signal(symbol="EURUSD", confidence=0, score=0, strategy=strategy, timeframe="M15", reasons=["no setup"], timestamp=datetime.now(timezone.utc))
    stop_loss = entry - risk if action is SignalAction.BUY else entry + risk
    take_profit = entry + risk * rr if action is SignalAction.BUY else entry - risk * rr
    return Signal(action=action, confidence=confidence, score=round(confidence * 100), symbol="EURUSD", entry=entry, stop_loss=stop_loss, take_profit=take_profit, strategy=strategy, timeframe="M15", reasons=["synthetic vote"], timestamp=datetime.now(timezone.utc))

def assert_valid_bracket(signal, rr):
    assert signal.stop_loss is not None and signal.take_profit is not None
    if signal.action is SignalAction.BUY:
        assert signal.stop_loss < signal.entry < signal.take_profit
        assert signal.take_profit == pytest.approx(signal.entry + (signal.entry - signal.stop_loss) * rr)
    else:
        assert signal.take_profit < signal.entry < signal.stop_loss
        assert signal.take_profit == pytest.approx(signal.entry - (signal.stop_loss - signal.entry) * rr)

def test_sma_bollinger_and_roc_match_their_definitions():
    close = pd.Series(1.10 + 0.001 * np.sin(np.arange(80) / 4.0))
    assert sma(close, 20).iloc[-1] == pytest.approx(close.tail(20).mean())
    lower, mid, upper = bollinger(close, 20, 2.0)
    assert mid.iloc[-1] == pytest.approx(close.tail(20).mean())
    assert mid.iloc[-1] - lower.iloc[-1] == pytest.approx(upper.iloc[-1] - mid.iloc[-1])
    assert roc(close, 10).iloc[-1] == pytest.approx((close.iloc[-1] / close.iloc[-11] - 1) * 100)

def test_donchian_stochastic_and_percentile_stay_inside_their_bounds():
    rising = candle(1.10 + 0.0005 * np.arange(240))
    high, low = donchian(rising, 20)
    assert high.iloc[-1] == pytest.approx(rising.high.tail(20).max())
    assert low.iloc[-1] == pytest.approx(rising.low.tail(20).min())
    percent_k, percent_d = stochastic(rising, 14, 3)
    assert 0 <= percent_k.iloc[-1] <= 100 and percent_k.iloc[-1] > 50
    assert 0 <= percent_d.iloc[-1] <= 100
    percentile = atr_percentile(rising, 14, 100)
    assert 0 <= percentile.iloc[-1] <= 1

def test_volume_helper_reports_an_absent_feed_and_prefers_volume():
    frame = candle(1.10 + 0.0005 * np.arange(40))
    assert volume_series(frame).isna().all()
    tick = frame.assign(tick_volume=np.arange(40, dtype=float))
    assert volume_series(tick).iloc[-1] == 39
    both = tick.assign(volume=np.arange(40, 80, dtype=float))
    assert volume_series(both).iloc[-1] == 79

@pytest.mark.parametrize("module", NEW_STRATEGIES, ids=STRATEGY_IDS)
def test_strategies_fail_closed_on_bad_input(module):
    good = trending(1)
    truncated = candle(1.10 + 0.0005 * np.arange(20))
    missing_columns = good.drop(columns=["high"])
    nan_close = good.copy()
    nan_close.loc[nan_close.index[-1], "close"] = np.nan
    negative_close = good.copy()
    negative_close.loc[negative_close.index[-5:], "close"] = -1.0
    for frame in (good.iloc[0:0], truncated, missing_columns, nan_close, negative_close):
        signal = module.evaluate("EURUSD", frame, frame, frame)
        assert signal.action is SignalAction.HOLD
        assert signal.entry is None and signal.stop_loss is None and signal.take_profit is None
        assert signal.confidence == 0 and signal.score == 0 and signal.reasons
    assert module.evaluate("", good, good, good).action is SignalAction.HOLD
    assert module.evaluate("EURUSD", good, good, good, rr=0.5).action is SignalAction.HOLD

@pytest.mark.parametrize("module", NEW_STRATEGIES, ids=STRATEGY_IDS)
def test_strategies_reject_unconfirmed_markets(module):
    # A flat market carries no breakout, no momentum, no band stretch and no volatility expansion.
    flat = candle(1.10 + 0.00002 * np.sin(np.arange(260) / 2.0))
    signal = module.evaluate("EURUSD", flat, flat, flat)
    assert signal.action is SignalAction.HOLD
    assert signal.score == 0 and signal.stop_loss is None and signal.take_profit is None

@pytest.mark.parametrize("module", NEW_STRATEGIES, ids=STRATEGY_IDS)
@pytest.mark.parametrize("rr", [2.0, 3.0])
def test_strategies_place_valid_stops_and_targets(module, rr):
    for direction, action in ((1, SignalAction.BUY), (-1, SignalAction.SELL)):
        h1, m15 = trade_frames(module, direction)
        signal = module.evaluate("EURUSD", h1, h1, m15, rr=rr)
        assert signal.action is action
        assert signal.strategy == module.STRATEGY and signal.timeframe == "M15"
        assert signal.confidence == pytest.approx(signal.score / 100)
        assert signal.score > 0 and signal.reasons
        assert_valid_bracket(signal, rr)

def test_breakout_scores_volume_expansion_and_reports_the_absence_of_a_volume_feed():
    h1, m15 = trade_frames(breakout, 1)
    without_volume = breakout.evaluate("EURUSD", h1, h1, m15)
    with_volume = breakout.evaluate("EURUSD", h1, h1, m15.assign(volume=np.r_[np.full(259, 1000.0), 5000.0]))
    assert without_volume.action is SignalAction.BUY and with_volume.action is SignalAction.BUY
    assert with_volume.score > without_volume.score
    assert any("volume feed unavailable" in reason for reason in without_volume.reasons)
    assert any("volume expansion confirmed" in reason for reason in with_volume.reasons)

def test_breakout_ignores_wick_only_extremes_and_already_extended_ranges():
    h1 = trending(1)
    wicked = breakout.evaluate("EURUSD", h1, h1, breakout_frames(1, wicked=True))
    repeated = breakout.evaluate("EURUSD", h1, h1, breakout_frames(1, repeat=True))
    assert wicked.action is SignalAction.HOLD and repeated.action is SignalAction.HOLD
    assert "no fresh close beyond the prior 20-candle range" in wicked.reasons[0]
    assert "no fresh close beyond the prior 20-candle range" in repeated.reasons[0]

def test_momentum_refuses_overextended_entries():
    h1 = trending(1)
    hot = momentum.evaluate("EURUSD", h1, h1, momentum_frames(1, boost=0.004))
    overbought = momentum.evaluate("EURUSD", h1, h1, momentum_frames(1, boost=0.010))
    assert hot.action is SignalAction.HOLD and overbought.action is SignalAction.HOLD
    assert "overextended upside" in hot.reasons[0]

def test_mean_reversion_refuses_to_fade_an_aligned_trend():
    falling = trending(-1)
    signal = mean_reversion.evaluate("EURUSD", falling, falling, mean_reversion_frames(1))
    assert signal.action is SignalAction.HOLD
    assert "blocked by the aligned h4/h1 downtrend structure" in signal.reasons[0]

def test_mean_reversion_refuses_a_trending_regime():
    # A steady directional market drives ADX above the range limit, so the fade is skipped.
    trend = trending(1, pullback=True)
    signal = mean_reversion.evaluate("EURUSD", trend, trend, trend)
    assert signal.action is SignalAction.HOLD
    assert "trending regime" in signal.reasons[0]

def test_volatility_refuses_abnormal_spikes():
    spiked = volatility_frames(1, spike=True)
    signal = volatility.evaluate("EURUSD", spiked, spiked, spiked)
    assert signal.action is SignalAction.HOLD
    assert "abnormal volatility" in signal.reasons[0] and "trading refused" in signal.reasons[0]

def test_ensemble_combines_agreeing_votes_and_keeps_the_strongest_bracket():
    settings = Settings(_env_file=None)
    signals = [vote("breakout", SignalAction.BUY, 0.85, entry=1.1000), vote("momentum", SignalAction.BUY, 0.75, entry=1.1004)]
    combined = combine(signals, settings)
    assert combined.action is SignalAction.BUY
    assert combined.strategy == ENSEMBLE and combined.symbol == "EURUSD"
    assert combined.entry == pytest.approx(1.1000)
    assert combined.confidence == pytest.approx(0.80) and combined.score == 80
    assert_valid_bracket(combined, 2.0)
    assert any("2 agreeing vote" in reason for reason in combined.reasons)
    assert any("not a probability of profit" in reason for reason in combined.reasons)

def test_ensemble_reports_disagreement_and_threshold_failures():
    settings = Settings(_env_file=None)
    empty = combine([], settings)
    assert empty.action is SignalAction.HOLD and "no eligible strategy votes" in empty.reasons[0]
    abstained = combine([vote("breakout", SignalAction.HOLD, 0.9), vote("momentum", SignalAction.HOLD, 0.9)], settings)
    assert abstained.action is SignalAction.HOLD and "no eligible strategy votes" in abstained.reasons[0]
    balanced = combine([vote("breakout", SignalAction.BUY, 0.9), vote("momentum", SignalAction.SELL, 0.9)], settings)
    assert balanced.action is SignalAction.HOLD and any("ensemble disagreement" in reason for reason in balanced.reasons)
    assert balanced.entry is None and balanced.stop_loss is None and balanced.take_profit is None
    single = combine([vote("breakout", SignalAction.BUY, 0.9)], settings)
    assert single.action is SignalAction.HOLD and "insufficient votes" in single.reasons[0]
    weak = combine([vote("breakout", SignalAction.BUY, 0.3), vote("momentum", SignalAction.BUY, 0.3)], settings)
    assert weak.action is SignalAction.HOLD and "insufficient confidence" in weak.reasons[0]

def test_ensemble_lets_opposing_votes_suppress_the_aggregate():
    settings = Settings(_env_file=None)
    suppressed = combine([vote("breakout", SignalAction.BUY, 0.9), vote("momentum", SignalAction.BUY, 0.9), vote("volatility", SignalAction.SELL, 0.5)], settings)
    assert suppressed.action is SignalAction.BUY
    assert suppressed.confidence == pytest.approx(0.65)
    drowned = combine([vote("breakout", SignalAction.BUY, 0.9), vote("momentum", SignalAction.BUY, 0.9), vote("volatility", SignalAction.SELL, 1.0)], settings)
    assert drowned.action is SignalAction.HOLD and "insufficient confidence" in drowned.reasons[0]

def test_ensemble_honours_configurable_thresholds_and_fails_closed_on_bad_brackets():
    strict = Settings(_env_file=None, ensemble_min_votes=3, ensemble_min_confidence=0.9)
    pair = [vote("breakout", SignalAction.BUY, 0.9), vote("momentum", SignalAction.BUY, 0.9)]
    assert combine(pair, strict).action is SignalAction.HOLD
    assert combine(pair, strict).reasons[0].startswith("insufficient votes")
    relaxed = Settings(_env_file=None, ensemble_min_votes=1, ensemble_min_confidence=0.6)
    assert combine([vote("breakout", SignalAction.BUY, 0.9)], relaxed).action is SignalAction.BUY
    broken = vote("breakout", SignalAction.BUY, 0.9).model_copy(update={"stop_loss": None})
    incomplete = combine([broken, vote("momentum", SignalAction.BUY, 0.85)], relaxed)
    assert incomplete.action is SignalAction.HOLD and "incomplete bracket" in incomplete.reasons[0]
    inverted = vote("breakout", SignalAction.BUY, 0.9).model_copy(update={"stop_loss": 1.1010})
    assert levels_of(SignalAction.BUY, inverted) is None
    assert combine([inverted, vote("momentum", SignalAction.BUY, 0.85)], relaxed).action is SignalAction.HOLD

def test_ensemble_wrapper_reuses_one_settings_instance():
    settings = Settings(_env_file=None, ensemble_min_votes=1)
    engine = Ensemble(settings)
    assert engine.combine([vote("breakout", SignalAction.SELL, 0.7), vote("momentum", SignalAction.SELL, 0.7)]).action is SignalAction.SELL
    assert engine.combine([vote("breakout", SignalAction.SELL, 0.7)]).action is SignalAction.SELL

def test_settings_keep_safe_defaults_and_reject_unknown_strategies():
    settings = Settings(_env_file=None)
    assert settings.enabled_strategies == ["trend_following"]
    assert settings.ensemble_min_votes == 2 and settings.ensemble_min_confidence == pytest.approx(0.6)
    assert settings.trading_mode is TradingMode.DEMO and settings.live_trading_enabled is False
    assert settings.risk_per_trade_pct == pytest.approx(0.5) and settings.max_daily_loss_pct == pytest.approx(2)
    assert settings.min_signal_score == 75
    parsed = Settings(_env_file=None, enabled_strategies="trend_following,breakout,ensemble")
    assert parsed.enabled_strategies == ["trend_following", "breakout", "ensemble"]
    with pytest.raises(ValidationError, match="unknown strategy names"):
        Settings(_env_file=None, enabled_strategies=["trend_following", "unicorn"])
    with pytest.raises(ValidationError, match="unknown strategy names"):
        Settings(_env_file=None, strategy_timeframes={"unicorn": "H1"})
    assert Settings(_env_file=None, strategy_timeframes="breakout:H1").strategy_timeframes == {"breakout": "H1"}

def test_registry_votes_end_to_end_into_a_single_signal():
    h4 = h1 = trending(1)
    m15 = breakout_frames(1)
    votes = [evaluate("EURUSD", h4, h1, m15) for evaluate in STRATEGIES.values()]
    assert sum(vote.action is SignalAction.BUY for vote in votes) >= 2
    combined = combine(votes, Settings(_env_file=None))
    assert combined.action is SignalAction.BUY and combined.strategy == ENSEMBLE
    assert_valid_bracket(combined, 2.0)
    assert any("levels taken from the strongest agreeing vote" in reason for reason in combined.reasons)

def test_registry_exposes_the_canonical_names_and_keeps_the_ensemble_separate():
    assert set(STRATEGIES) == {"trend_following", "breakout", "momentum", "mean_reversion", "volatility"}
    assert ENSEMBLE == "ensemble" and ENSEMBLE in STRATEGY_NAMES and ENSEMBLE not in STRATEGIES
    assert all(callable(evaluate) for evaluate in STRATEGIES.values())
    settings = Settings(_env_file=None, enabled_strategies=["ensemble", "breakout", "trend_following"], strategy_timeframes={"breakout": "H1"})
    assert enabled_strategies(settings) == ["trend_following", "breakout"]
    assert ensemble_enabled(settings) is True
    assert strategy_timeframe(settings, "breakout") == "H1"
    assert strategy_timeframe(settings, "momentum") == "M15"
    assert ensemble_enabled(Settings(_env_file=None)) is False
