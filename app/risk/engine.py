"""Pre-trade gate: an ordered, named filter chain.

Every check has a name and reports its own reason; any failure means NO TRADE, and the first
failure is the rejection the caller reports. The limits enforced here are hard and server-side:

- ``max_loss_per_trade_usd`` is the only risk budget a volume may be derived from,
- ``max_lots_per_position`` caps the volume itself, whatever the budget would allow,
- ``max_bot_capital_usd`` is the allocation ceiling (not equity) the required margin must fit in,
- ``max_session_loss_usd`` and ``max_daily_trades`` are read from the persisted risk state, so a
  restart cannot hand the loop a fresh allowance of orders,
- ``max_open_positions`` is 1: no second position, no averaging, no grid,
- ``one_trade_authorization`` is the owner's single live trade: live ordering is permitted only
  while that persisted authorization is unspent, so a spent one refuses every later entry even
  though ``TRADING_MODE`` stays ``live`` (reconciliation, analytics and monitoring keep running).

Nothing here ever widens a limit or a stop. When the broker's minimum volume would risk more than
the per-trade budget at the strategy's technical stop, the trade is rejected instead of the stop
being moved closer, and the per-trade budget is never raised to make a minimum lot fit. Routine
rejections are returned as reasons, never raised.

Fail closed is the default for every input: an unreadable account, a terminal connected to a
different login/server than the operator configured, an unreadable risk state or a missing symbol
specification all stop the trade instead of being assumed harmless. A session that has reached its
loss or trade-count limit is *persisted* as a kill switch, so the verdict survives a restart.
"""
from dataclasses import dataclass, field
from datetime import date
import logging

from app.core.config import Settings
from app.core.schemas import Signal
from app.mt5.account import AccountProfile, account_matches
from app.risk.authorization import CHECK_NAME as AUTHORIZATION_CHECK, authorization_state
from app.risk.sizing import (
    SymbolSpec,
    min_stop_distance,
    minimum_volume_risk,
    position_risk,
    required_margin,
    risk_per_lot,
    size_for_loss_budget,
    value_per_point_per_lot,
)

logger = logging.getLogger(__name__)
__all__ = ["RiskEngine", "SymbolSpec", "EntryFacts", "RiskAssessment", "CheckResult", "CHECK_ORDER"]

# The filter chain, in the order it is evaluated. A name is stable so logs and the dry run can be
# read against it; check 3 pins the account the terminal is logged in to, check 12 rejects a
# minimum lot that would exceed the per-trade loss budget or a volume above the hard lot cap, check
# 16 refuses to trade without a readable risk state, and check 17 refuses a live order once the
# owner's single live-trade authorization has been spent.
CHECK_ORDER: tuple[str, ...] = (
    "mt5_connected",
    "account_authorized",
    "account_matches_expected",
    "algo_trading_enabled",
    "fresh_market_data",
    "trend_signal",
    "momentum_confirmed",
    "spread_within_limit",
    "valid_stop_loss",
    "valid_take_profit",
    "risk_reward",
    "position_sizing",
    "margin_within_capital",
    "margin_level",
    "total_exposure",
    "risk_state_available",
    "one_trade_authorization",
    "loss_limits",
    "single_position",
    "symbol_position_limit",
    "no_duplicate",
    "emergency_stop_clear",
)

# Float tolerance for prices and money; one tenth of a point is below any broker's tick.
_PRICE_EPSILON = 1e-12
_MONEY_EPSILON = 1e-9
_LOT_EPSILON = 1e-9


def exposure_pct(notional: float, equity: float) -> float | None:
    if equity is None or equity <= 0 or notional is None or notional < 0: return None
    return notional / equity * 100


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "reason": self.reason}


@dataclass(frozen=True)
class EntryFacts:
    """Live state the caller must supply; every default is the fail-closed value.

    An unverifiable input is passed as ``None`` and rejected: the gate never assumes a market is
    connected, an account is demo, or an allowance is available.
    """

    spec: SymbolSpec | None = None
    connected: bool = False
    account: AccountProfile | None = None
    algo_trading_enabled: bool | None = None
    data_age_seconds: float | None = None
    momentum_action: str | None = None
    spread_points: float | None = None
    open_positions: int | None = None
    symbol_positions: int | None = None
    exposure: float | None = None
    margin_level: float | None = None
    duplicate_exists: bool = True
    emergency_locked: bool = False
    trading_enabled: bool = False
    free_margin: float | None = None
    equity: float | None = None
    leverage: float = 0.0
    pending_orders: int | None = 0


@dataclass(frozen=True)
class RiskAssessment:
    """The full chain plus the numbers the decision was made from."""

    approved: bool
    checks: list[CheckResult] = field(default_factory=list)
    volume: float | None = None
    risk_usd: float | None = None
    min_lot_risk_usd: float | None = None
    margin_usd: float | None = None
    capital_usd: float = 0.0
    stop_distance_points: float | None = None
    value_per_point_per_lot: float | None = None
    risk_reward: float | None = None
    session_loss_usd: float = 0.0
    trades_opened: int = 0
    max_lots_per_position: float | None = None
    session_started_at: str | None = None
    kill_switch_reason: str | None = None

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.ok]

    @property
    def reason(self) -> str:
        failed = self.failures
        return failed[0].reason if failed else ""

    @property
    def failed_checks(self) -> list[str]:
        return [check.name for check in self.failures]

    @property
    def kill_switch_active(self) -> bool:
        return bool((self.kill_switch_reason or "").strip())

    def as_dict(self) -> dict:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "failed_checks": self.failed_checks,
            "volume": self.volume,
            "risk_usd": self.risk_usd,
            "min_lot_risk_usd": self.min_lot_risk_usd,
            "margin_usd": self.margin_usd,
            "capital_usd": self.capital_usd,
            "stop_distance_points": self.stop_distance_points,
            "value_per_point_per_lot": self.value_per_point_per_lot,
            "risk_reward": self.risk_reward,
            "session_loss_usd": self.session_loss_usd,
            "trades_opened": self.trades_opened,
            "max_lots_per_position": self.max_lots_per_position,
            "session_started_at": self.session_started_at,
            "kill_switch_reason": self.kill_switch_reason,
            "kill_switch_active": self.kill_switch_active,
            "checks": [check.as_dict() for check in self.checks],
        }


class RiskEngine:
    """The single pre-trade gate; its limits come from the settings and the persisted risk state."""

    def __init__(self, settings: Settings): self.settings = settings

    def lot_size(self, equity: float, entry: float, stop: float, spec: SymbolSpec) -> float:
        """Legacy helper: risk-derived lots from the percentage budget, 0.0 when unusable."""
        from app.risk.sizing import position_size
        lots = position_size(equity, self.settings.risk_per_trade_pct, entry, stop, spec)
        return lots if lots is not None else 0.0

    def size(self, equity: float, entry: float, stop: float, spec: SymbolSpec, **kwargs) -> float | None:
        from app.risk.sizing import position_size
        return position_size(equity, self.settings.risk_per_trade_pct, entry, stop, spec, **kwargs)

    # ------------------------------------------------------------------ the chain

    def assess(self, signal: Signal, facts: EntryFacts, *, state=None, day: date | None = None) -> RiskAssessment:
        settings = self.settings
        spec = facts.spec
        entry = signal.entry if signal.entry else None
        stop_loss = signal.stop_loss
        take_profit = signal.take_profit
        action = str(signal.action).upper()
        side = action if action in ("BUY", "SELL") else None
        stop_points = None
        risk_distance = None
        per_lot = None
        value_point = None
        if spec is not None and entry and stop_loss and spec.point > 0:
            risk_distance = abs(float(entry) - float(stop_loss))
            stop_points = risk_distance / spec.point
            per_lot = risk_per_lot(entry, stop_loss, spec)
            value_point = value_per_point_per_lot(spec)

        # --- derived numbers (pure sizing code, no side effects) -------------------------------
        volume = size_for_loss_budget(settings.max_loss_per_trade_usd, entry or 0.0, stop_loss or 0.0, spec) if spec else None
        risk_usd = position_risk(volume, entry or 0.0, stop_loss or 0.0, spec) if (volume and entry and stop_loss and spec) else None
        min_lot_risk = minimum_volume_risk(entry or 0.0, stop_loss or 0.0, spec) if (entry and stop_loss and spec) else None
        margin = required_margin(volume, spec, entry or 0.0, facts.leverage) if (volume and entry and spec) else None
        risk_reward = (abs(float(take_profit) - float(entry)) / risk_distance) if (take_profit and entry and risk_distance) else None
        capital = float(settings.max_bot_capital_usd)

        # --- persisted counters ---------------------------------------------------------------
        session_loss = 0.0
        trades_opened = 0
        peak_equity = None
        session_started_at = None
        kill_switch_reason = None
        emergency_locked = bool(facts.emergency_locked)
        # A gate with no risk state has no session history, so it cannot verify the loss or trade
        # allowance: that is an unavailable input, never a zero-loss session.
        risk_state_ok = state is not None
        risk_state_reason = "" if risk_state_ok else "the persisted risk state is unavailable, so the session loss, trade count and kill switch cannot be verified"
        if state is not None:
            try:
                row = state.load(day)
                session_loss = max(0.0, -float(row.realized_pnl or 0.0))
                trades_opened = int(row.trades_opened or 0)
                peak_equity = row.peak_equity
                session_started_at = row.session_started_at
                kill_switch_reason = row.kill_switch_reason
                emergency_locked = emergency_locked or bool(row.emergency_locked)
                if facts.equity:
                    state.observe_equity(facts.equity, day)
            except Exception as error:  # an unreadable store must never read as "no loss yet"
                risk_state_ok = False
                risk_state_reason = f"the persisted risk state could not be read ({type(error).__name__}), so the session loss, trade count and kill switch cannot be verified"
                logger.error("risk_state_unreadable error=%s fail_closed=true", type(error).__name__)

        checks: list[CheckResult] = []
        add = checks.append

        # 1 MT5 connected
        add(CheckResult("mt5_connected", bool(facts.connected), "MT5 is not connected"))

        # 2 account authorized: the broker's trade_mode decides, never TRADING_MODE
        account = facts.account
        if account is None or not account.classified:
            add(CheckResult("account_authorized", False, "the broker account trade mode could not be read, so the account is not authorized"))
        elif account.is_real and not settings.live_orders_permitted:
            add(CheckResult("account_authorized", False, f"the connected broker account is {account.trade_mode_label} (trade_mode={account.trade_mode}) and live trading is not enabled: real-account orders are refused"))
        else:
            add(CheckResult("account_authorized", True))

        # 3 the live login/server is still the account the operator pinned. A terminal that
        # reconnected to another account is a hard stop, so the mismatch is *persisted* as an
        # emergency lock: every later cycle and every restarted process sees it, not just this one.
        # Only an account that was really read can have "changed": an unreadable one is already
        # refused by check 2, and latching a manual-reset lock on a transient read error would
        # punish the operator for a hiccup that the next cycle may not even see.
        matched, mismatch_reason = account_matches(account, settings.mt5_login, settings.mt5_server)
        if matched:
            add(CheckResult("account_matches_expected", True))
        else:
            add(CheckResult("account_matches_expected", False, mismatch_reason))
            account_was_read = account is not None and account.classified
            if not account_was_read:
                logger.error("account_unverified reason=%s", mismatch_reason)
            else:
                if not emergency_locked:
                    emergency_locked = True
                if state is not None:
                    try:
                        state.set_emergency_locked(True, day)
                        state.set_kill_switch(mismatch_reason, day)
                    except Exception as error:
                        logger.error("account_mismatch_lock_not_persisted error=%s", type(error).__name__)
                logger.critical("account_mismatch login=%s server=%s expected_login=%s expected_server=%s reason=%s", getattr(account, "login", None), getattr(account, "server", None), settings.mt5_login, settings.mt5_server, mismatch_reason)

        # 4 algo trading enabled in the terminal
        add(CheckResult("algo_trading_enabled", facts.algo_trading_enabled is True, "algorithmic trading is not enabled in the MT5 terminal (or its state is unknown)"))

        # 5 fresh market data
        age = facts.data_age_seconds
        add(CheckResult("fresh_market_data", age is not None and age <= settings.market_data_max_age_seconds, f"market data is not fresh ({'unknown age' if age is None else f'{age:.0f}s old, limit {settings.market_data_max_age_seconds}s'})"))

        # 6 valid trend signal
        if side is None:
            add(CheckResult("trend_signal", False, "the strategy did not produce a BUY or SELL signal (HOLD / no trade)"))
        elif signal.score < settings.min_signal_score:
            add(CheckResult("trend_signal", False, f"signal quality {signal.score} is below the required {settings.min_signal_score}"))
        elif settings.risk_per_trade_pct > settings.max_risk_per_trade_pct:
            add(CheckResult("trend_signal", False, "risk per trade exceeds configured guard"))
        else:
            add(CheckResult("trend_signal", True))

        # 7 momentum must not contradict the trend (HOLD is neutral; no trend means nothing to contradict)
        momentum = str(facts.momentum_action).upper() if facts.momentum_action is not None else None
        if momentum is None:
            add(CheckResult("momentum_confirmed", False, "momentum confirmation could not be computed"))
        elif side is None or momentum in ("HOLD", side):
            add(CheckResult("momentum_confirmed", True))
        else:
            add(CheckResult("momentum_confirmed", False, f"momentum contradicts the trend: momentum says {momentum}, the trend signal says {side}"))

        # 8 spread
        spread_ok = facts.spread_points is not None and facts.spread_points <= settings.max_spread_points_default
        add(CheckResult("spread_within_limit", spread_ok, f"spread protection triggered (spread {facts.spread_points if facts.spread_points is not None else 'unknown'} points, limit {settings.max_spread_points_default})"))

        # 9 technical stop loss, strictly on the risk side and not closer than the broker allows
        minimum = min_stop_distance(spec) if spec is not None else 0.0
        stop_reason = ""
        if spec is None:
            stop_reason = "symbol specification is unavailable, so the stop loss cannot be validated"
        elif side is None:
            stop_reason = "the trade direction is not tradable, so it carries no valid stop loss"
        elif stop_loss is None or float(stop_loss or 0) <= 0:
            stop_reason = "the order has no stop loss: a naked position is never sent"
        elif side == "BUY" and float(stop_loss) >= float(entry) - _PRICE_EPSILON:
            stop_reason = "BUY stop loss must be below the entry price"
        elif side == "SELL" and float(stop_loss) <= float(entry) + _PRICE_EPSILON:
            stop_reason = "SELL stop loss must be above the entry price"
        elif risk_distance is not None and minimum > 0 and risk_distance < minimum:
            stop_reason = "stop loss violates broker stop distance"
        add(CheckResult("valid_stop_loss", not stop_reason, stop_reason))

        # 10 take profit present and on the right side
        tp_reason = ""
        if take_profit is None or float(take_profit or 0) <= 0:
            tp_reason = "take profit is required and must be a positive price"
        elif side == "BUY" and float(take_profit) <= float(entry) + _PRICE_EPSILON:
            tp_reason = "BUY take profit must be above the entry price"
        elif side == "SELL" and float(take_profit) >= float(entry) - _PRICE_EPSILON:
            tp_reason = "SELL take profit must be below the entry price"
        add(CheckResult("valid_take_profit", not tp_reason, tp_reason))

        # 11 risk/reward, a target relationship and not a profit guarantee
        ratio = settings.min_risk_reward_ratio
        add(CheckResult("risk_reward", risk_reward is not None and risk_reward >= ratio - 1e-9, f"take profit violates the {ratio:g}:1 risk/reward requirement (computed {risk_reward if risk_reward is None else round(risk_reward, 4)})"))

        # 12 position sizing: derived from the loss budget and the technical stop, then bounded by
        # the hard lot cap. The cap is a rejection, never a clamp: a setup whose risk-derived volume
        # exceeds it is not silently resized, and the stop is never tightened to shrink the volume.
        cap = float(settings.max_lots_per_position)
        sizing_reason = ""
        if spec is None or entry is None or not stop_loss or risk_distance is None:
            sizing_reason = "the stop distance is unknown, so no volume can be derived"
        elif per_lot is None:
            sizing_reason = "the broker symbol properties cannot price the stop distance"
        elif volume is None:
            if min_lot_risk is not None and min_lot_risk > settings.max_loss_per_trade_usd + _MONEY_EPSILON:
                sizing_reason = f"the broker minimum volume {spec.volume_min:g} would risk {min_lot_risk:.2f} USD at the technical stop ({stop_points:.1f} points), above the {settings.max_loss_per_trade_usd:.2f} USD per-trade limit"
            else:
                sizing_reason = "risk-based lot sizing below broker minimum"
        elif volume > cap + _LOT_EPSILON:
            sizing_reason = f"the risk-derived volume {volume:g} exceeds the {cap:g} lot hard cap for a single position"
        elif risk_usd is not None and risk_usd > settings.max_loss_per_trade_usd + _MONEY_EPSILON:
            sizing_reason = f"the derived volume {volume:g} risks {risk_usd:.4f} USD, above the {settings.max_loss_per_trade_usd:.2f} USD per-trade limit"
        add(CheckResult("position_sizing", not sizing_reason, sizing_reason))

        # 13 margin must fit inside the bot's capital allocation and the free margin
        margin_reason = ""
        if volume is None:
            margin_reason = "no volume was derived, so the required margin cannot be verified"
        elif margin is None:
            margin_reason = "the required margin cannot be verified from the broker's symbol properties"
        elif margin > capital + _MONEY_EPSILON:
            margin_reason = f"required margin {margin:.2f} USD exceeds the {capital:.2f} USD bot capital allocation"
        elif facts.free_margin is None or margin > float(facts.free_margin) + _MONEY_EPSILON:
            margin_reason = f"insufficient free margin for the risk-sized volume (required {margin:.2f} USD, free {facts.free_margin})"
        add(CheckResult("margin_within_capital", not margin_reason, margin_reason))

        # the pre-existing margin-level and exposure guards, retained unchanged
        level = facts.margin_level
        add(CheckResult("margin_level", level is None or level >= settings.min_margin_level_pct, f"margin level below minimum ({level} < {settings.min_margin_level_pct})"))
        current_exposure = exposure_pct(facts.exposure, facts.equity) if facts.exposure is not None else None
        if facts.exposure is None:
            add(CheckResult("total_exposure", True))
        elif current_exposure is None:
            add(CheckResult("total_exposure", False, "total exposure cannot be verified"))
        else:
            add(CheckResult("total_exposure", current_exposure <= settings.max_total_exposure_pct, f"total exposure limit reached ({current_exposure:.1f}% > {settings.max_total_exposure_pct}%)"))

        # 16 the persisted risk state has to be readable: without it the session loss, the trade
        # count and the kill switch are unknown, and an unknown allowance is never an allowance.
        add(CheckResult("risk_state_available", risk_state_ok, risk_state_reason))

        # 17 the owner's single live-trade authorization. ONE live entry may ever be authorized, and
        # the reservation is persisted, so the refusal holds on the next cycle, after a restart, a
        # reboot or a new calendar day - and, unlike MAX_DAILY_TRADES, it does not reset tomorrow.
        # Only *ordering* is refused here: TRADING_MODE stays `live`, so the loop keeps scanning and
        # reporting, and reconciliation, analytics, the MT5 monitor and the dashboard keep working.
        authorization = authorization_state(settings, state)
        add(CheckResult(AUTHORIZATION_CHECK, not authorization.blocked, authorization.blocked_reason))

        # 18 persisted session loss, trade-count and equity limits, plus the session kill switch.
        # Reaching either dollar limit also *persists* why the session closed, so a restarted
        # process reads the verdict instead of recomputing a fresh allowance from zeroed counters.
        limit_reasons = []
        if risk_state_ok:
            if session_loss >= settings.max_session_loss_usd - _MONEY_EPSILON and session_loss > 0:
                limit_reasons.append(f"session loss limit reached ({session_loss:.2f} USD of {settings.max_session_loss_usd:.2f} USD)")
            if trades_opened >= settings.max_daily_trades:
                limit_reasons.append(f"daily trade limit reached ({trades_opened} of {settings.max_daily_trades} trades)")
            if facts.equity and session_loss >= float(facts.equity) * settings.max_daily_loss_pct / 100:
                limit_reasons.append("daily loss limit reached")
            if peak_equity and facts.equity and float(peak_equity) > 0 and (float(peak_equity) - float(facts.equity)) / float(peak_equity) * 100 >= settings.max_drawdown_pct:
                limit_reasons.append("maximum drawdown emergency stop")
                if state is not None:
                    state.set_emergency_locked(True, day)
                    emergency_locked = True
                    kill_switch_reason = kill_switch_reason or "maximum drawdown emergency stop"
                    state.set_kill_switch("maximum drawdown emergency stop", day)
                logger.error("risk_emergency_lock equity=%.2f peak=%s limit_pct=%s persisted=%s", float(facts.equity), peak_equity, settings.max_drawdown_pct, state is not None)
        if limit_reasons:
            kill_switch_reason = kill_switch_reason or "; ".join(limit_reasons)
            if state is not None and risk_state_ok:
                try:
                    state.set_kill_switch(kill_switch_reason, day)
                except Exception as error:
                    logger.error("kill_switch_not_persisted error=%s", type(error).__name__)
            logger.warning("session_kill_switch reasons=%s persisted=%s", limit_reasons, state is not None)
        add(CheckResult("loss_limits", not limit_reasons, "; ".join(limit_reasons)))

        # 19 at most one position, ever
        open_positions = facts.open_positions
        if open_positions is None:
            add(CheckResult("single_position", False, "the number of open positions could not be verified"))
        elif open_positions >= settings.max_open_positions:
            add(CheckResult("single_position", False, f"maximum simultaneous positions reached ({open_positions} open, limit {settings.max_open_positions})"))
        else:
            add(CheckResult("single_position", True))

        symbol_positions = facts.symbol_positions
        if symbol_positions is None:
            add(CheckResult("symbol_position_limit", False, "the number of positions on this symbol could not be verified"))
        else:
            add(CheckResult("symbol_position_limit", symbol_positions < settings.max_positions_per_symbol, f"maximum positions per symbol reached ({symbol_positions}, limit {settings.max_positions_per_symbol})"))

        # 20 no duplicate signal or order
        add(CheckResult("no_duplicate", not facts.duplicate_exists, "duplicate position or order exists"))

        # 21 emergency stop inactive, no session kill switch latched, and the loop allowed to trade
        kill_switch_active = bool((kill_switch_reason or "").strip())
        if emergency_locked or kill_switch_active or not facts.trading_enabled:
            reason = "bot is stopped or emergency locked"
            if kill_switch_active and not emergency_locked:
                reason = f"session kill switch is latched: {kill_switch_reason}"
            add(CheckResult("emergency_stop_clear", False, reason))
        else:
            add(CheckResult("emergency_stop_clear", True))

        assessment = RiskAssessment(
            approved=all(check.ok for check in checks),
            checks=checks,
            volume=volume,
            risk_usd=risk_usd,
            min_lot_risk_usd=min_lot_risk,
            margin_usd=margin,
            capital_usd=capital,
            stop_distance_points=stop_points,
            value_per_point_per_lot=value_point,
            risk_reward=risk_reward,
            session_loss_usd=session_loss,
            trades_opened=trades_opened,
            max_lots_per_position=cap,
            session_started_at=session_started_at.isoformat() if session_started_at is not None else None,
            kill_switch_reason=kill_switch_reason,
        )
        if assessment.approved:
            logger.info("risk_approved symbol=%s strategy=%s volume=%s risk_usd=%.4f margin_usd=%s", signal.symbol, signal.strategy, volume, risk_usd or 0.0, margin)
        else:
            logger.warning("risk_rejected symbol=%s strategy=%s volume=%s failed=%s reason=%s", signal.symbol, signal.strategy, volume, assessment.failed_checks, assessment.reason)
        return assessment
