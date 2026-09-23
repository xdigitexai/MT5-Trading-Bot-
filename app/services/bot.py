"""Bot lifecycle state machine.

    STOPPED -> STARTING -> RUNNING <-> DEGRADED
                          |             |
                          +-> EMERGENCY_LOCKED / ERROR

``start()`` validates MT5 (it refuses to run without an initialized, logged-in terminal) before it
enters RUNNING and starts the market loop. ``stop()`` stops the loop first. ``DEGRADED`` is reported
whenever the bot is running but MT5 health has gone bad: the loop keeps failing closed, and the API
shows why.

``emergency_stop()`` halts signal generation immediately (the scheduler stops and no new cycle
starts), blocks new orders (the risk engine reads the lock on every cycle) and *persists* the lock
in the risk state, so it survives a restart and a fresh process cannot trade until an operator calls
``emergency_reset()`` explicitly. Closing positions is optional and is restricted to positions
carrying this bot's magic number: positions belonging to other systems are never touched.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import logging

from app.core.clock import isoformat, utcnow
from app.core.config import Settings
from app.database.base import AuditRecord
from app.mt5.account import AccountProfile, account_matches, account_profile
from app.mt5.constants import mt5_constants
from app.mt5.gateway import MT5Gateway
from app.risk.state import RiskStateStore
from app.services.scheduler import MarketScheduler

logger = logging.getLogger(__name__)


class BotState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    EMERGENCY_LOCKED = "EMERGENCY_LOCKED"
    ERROR = "ERROR"


@dataclass
class BotStatus:
    state: BotState = BotState.STOPPED
    detail: str = ""
    started_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self.state in (BotState.RUNNING, BotState.DEGRADED)

    @property
    def emergency_locked(self) -> bool:
        return self.state is BotState.EMERGENCY_LOCKED


class BotService:
    def __init__(
        self,
        settings: Settings,
        gateway: MT5Gateway,
        *,
        session_factory=None,
        scheduler: MarketScheduler | None = None,
    ):
        self.settings, self.gateway, self.session_factory = settings, gateway, session_factory
        self.scheduler = scheduler
        self.state = BotStatus()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> tuple[bool, str]:
        if self.state.running:
            return False, f"the bot is already {self.state.state.value}"
        if self.session_factory is None:
            return self._fail("a database session factory is required to persist the risk state and run the market loop")
        locked = self._persisted_lock()
        if locked is None:
            return self._fail("the persisted risk state could not be read; refusing to start (fail closed)")
        if locked:
            self.state.state = BotState.EMERGENCY_LOCKED
            self.state.detail = "the persisted emergency lock requires an explicit manual reset"
            logger.warning("bot_start_refused reason=emergency_locked")
            return False, self.state.detail
        self.state.state, self.state.detail = BotState.STARTING, "validating MT5"
        health = self.gateway.initialize()
        if not health.connected:
            return self._fail(f"MT5 initialization failed: {health.detail}")
        login = self.gateway.login()
        if not login.connected:
            self.gateway.shutdown()
            return self._fail(f"MT5 login failed: {login.detail}")
        profile = self.account_profile()
        if profile is None:
            self.gateway.shutdown()
            return self._fail("the broker account could not be read after login; refusing to start (fail closed)")
        matched, mismatch = account_matches(profile, self.settings.mt5_login, self.settings.mt5_server)
        if not matched:
            self.gateway.shutdown()
            self._set_persisted_lock(True)
            logger.critical("bot_start_account_mismatch login=%s server=%s expected_login=%s expected_server=%s", profile.login, profile.server, self.settings.mt5_login, self.settings.mt5_server)
            return self._fail(f"{mismatch}; the emergency lock was persisted")
        if profile.is_real:
            # A real account is reported as its own state, never downgraded by TRADING_MODE.
            logger.critical(
                "bot_start_account_is_real login=%s server=%s company=%s trade_mode=%s balance=%.2f live_orders_permitted=%s",
                profile.login, profile.server, profile.company, profile.trade_mode, profile.balance, self.settings.live_orders_permitted,
            )
        if self.scheduler is None:
            self.gateway.shutdown()
            return self._fail("no market loop is configured for this bot")
        started, detail = self.scheduler.start()
        if not started:
            self.gateway.shutdown()
            return self._fail(f"the market loop did not start: {detail}")
        self.state.state = BotState.RUNNING
        self.state.started_at = utcnow()
        self.state.detail = "running: MT5 validated and the market loop is active"
        logger.info("bot_started mode=%s live_orders_permitted=%s", self.settings.trading_mode.value, self.settings.live_orders_permitted)
        return True, self.state.detail

    def stop(self) -> tuple[bool, str]:
        if self.scheduler is not None and not self.scheduler.stop():
            logger.error("bot_stop_timeout the market loop thread did not stop within the timeout")
        locked = self._persisted_lock()
        if locked is None:
            self.state.state = BotState.DEGRADED
            self.state.detail = "stopped, but the persisted risk state could not be read"
            return True, self.state.detail
        if locked:
            self.state.state = BotState.EMERGENCY_LOCKED
            self.state.detail = "stopped, the persisted emergency lock is still in force"
            logger.warning("bot_stopped_emergency_lock_persisted")
            return True, self.state.detail
        self.state.state, self.state.detail = BotState.STOPPED, "stopped by request"
        logger.info("bot_stopped")
        return True, self.state.detail

    def emergency_stop(self, close_positions: bool = False, cancel_pending_orders: bool = True) -> dict:
        self.state.state = BotState.EMERGENCY_LOCKED
        self.state.detail = "emergency stop: signal generation halted and new orders blocked"
        report = {"emergency_locked": True, "scheduler_stopped": False, "lock_persisted": False, "close_positions_requested": bool(close_positions), "cancel_pending_orders_requested": bool(cancel_pending_orders)}
        if self.scheduler is not None:
            report["scheduler_stopped"] = bool(self.scheduler.stop())
        report["lock_persisted"] = self._set_persisted_lock(True)
        if close_positions:
            report["positions"] = self._close_managed_positions()
        if cancel_pending_orders:
            report["orders"] = self._cancel_managed_orders()
        logger.critical("emergency_stop lock_persisted=%s close_positions=%s cancel_pending=%s", report["lock_persisted"], close_positions, cancel_pending_orders)
        return report

    def emergency_reset(self) -> tuple[bool, str]:
        if self.state.running:
            return False, "stop the bot before resetting the emergency lock"
        if not self._set_persisted_lock(False):
            return False, "the emergency lock could not be cleared in the risk state; it remains in force"
        self.state.state, self.state.detail = BotState.STOPPED, "manual emergency reset complete"
        self.state.started_at = None
        logger.warning("emergency_reset")
        return True, self.state.detail

    def refresh_state(self) -> BotState:
        """DEGRADED reflects live MT5 health and blocked market data; a persisted lock always wins."""
        if self.state.state not in (BotState.STOPPED, BotState.STARTING, BotState.ERROR):
            locked = self._persisted_lock()
            if locked:
                self.state.state, self.state.detail = BotState.EMERGENCY_LOCKED, "the persisted emergency lock is in force"
            elif locked is None:
                self.state.state, self.state.detail = BotState.DEGRADED, "the persisted risk state could not be read"
            else:
                health = self.gateway.health()
                if not health.connected:
                    self.state.state, self.state.detail = BotState.DEGRADED, f"degraded: MT5 is unhealthy ({health.detail})"
                else:
                    blocked = self._real_account_reason() or self._market_block_reason()
                    self.state.state = BotState.DEGRADED if blocked else BotState.RUNNING
                    self.state.detail = f"degraded: {blocked}" if blocked else "running: MT5 healthy and every configured symbol scanned"
        return self.state.state

    def account_profile(self) -> AccountProfile | None:
        """The broker account as MT5 reports it; None when it cannot be read."""
        return account_profile(self.gateway.account_info())

    def _real_account_reason(self) -> str | None:
        """A REAL broker account is its own state: TRADING_MODE never downgrades it to demo."""
        profile = self.account_profile()
        if profile is None:
            return None
        if profile.is_real and not self.settings.live_orders_permitted:
            return (f"the connected broker account is {profile.trade_mode_label} (trade_mode={profile.trade_mode}, "
                    f"server {profile.server}) while live trading is not enabled: no order may be sent")
        if not profile.classified:
            return f"the broker account trade mode is unrecognised (trade_mode={profile.trade_mode}): the account is not verified as demo"
        return None

    def _market_block_reason(self) -> str | None:
        """First fail-closed reason when the last cycle could not scan a single symbol."""
        cycle = (self.scheduler.state.last_cycle or {}) if self.scheduler is not None else {}
        blocked = list(cycle.get("blocked") or ())
        symbols = dict(cycle.get("symbols") or {})
        if blocked and symbols and all(result.get("status") == "BLOCKED" for result in symbols.values()):
            return f"every configured symbol is blocked from trading ({blocked[0]})"
        return None

    def status(self) -> dict:
        self.refresh_state()
        health = self.gateway.health()
        profile = self.account_profile()
        real_account_block = self._real_account_reason()
        return {
            "state": self.state.state.value,
            "detail": self.state.detail,
            "running": self.state.running,
            "emergency_locked": self.state.state is BotState.EMERGENCY_LOCKED,
            "started_at": isoformat(self.state.started_at),
            "mt5": {"connected": health.connected, "detail": health.detail},
            "scheduler": self.scheduler.status() if self.scheduler is not None else None,
            "mode": self.settings.trading_mode.value,
            "live_orders_permitted": self.settings.live_orders_permitted,
            # The account's own trade mode, reported next to (never replaced by) the bot's mode.
            "account": profile.as_dict() if profile is not None else None,
            "account_trade_mode": profile.trade_mode_label if profile is not None else "UNKNOWN",
            "account_state": "BLOCKED:" + real_account_block if real_account_block else "AUTHORIZED",
            "hard_limits": {
                "max_bot_capital_usd": self.settings.max_bot_capital_usd,
                "max_loss_per_trade_usd": self.settings.max_loss_per_trade_usd,
                "max_session_loss_usd": self.settings.max_session_loss_usd,
                "max_daily_trades": self.settings.max_daily_trades,
                "max_open_positions": self.settings.max_open_positions,
                "max_lots_per_position": self.settings.max_lots_per_position,
            },
        }

    # ------------------------------------------------------------------ internals

    def _fail(self, detail: str) -> tuple[bool, str]:
        self.state.state, self.state.detail = BotState.ERROR, detail
        logger.error("bot_start_failed detail=%s", detail)
        return False, detail

    @contextmanager
    def _session(self):
        db = self.session_factory()
        try:
            yield db
        finally:
            db.close()

    def _persisted_lock(self) -> bool | None:
        """True/False from the risk state, or None when it could not be read (fail closed)."""
        if self.session_factory is None:
            return False
        try:
            with self._session() as db:
                return bool(RiskStateStore(db).load().emergency_locked)
        except Exception as error:  # unreadable state must never be read as "unlocked"
            logger.error("risk_state_unreadable error=%s", type(error).__name__)
            return None

    def _set_persisted_lock(self, locked: bool) -> bool:
        if self.session_factory is None:
            logger.error("emergency_lock_not_persisted reason=no_session_factory locked=%s", locked)
            return False
        try:
            with self._session() as db:
                RiskStateStore(db).set_emergency_locked(locked)
                db.add(AuditRecord(
                    event_type="EMERGENCY_LOCK" if locked else "EMERGENCY_RESET",
                    severity="CRITICAL" if locked else "WARNING",
                    message="emergency lock persisted" if locked else "emergency lock cleared by an operator",
                ))
                db.commit()
            return True
        except Exception as error:
            logger.error("emergency_lock_persist_failed locked=%s error=%s", locked, type(error).__name__)
            return False

    def _close_managed_positions(self) -> dict:
        positions = self.gateway.positions()
        if positions is None:
            logger.error("emergency_close_skipped reason=positions_unreadable")
            return {"closed": 0, "failed": 0, "skipped": 0, "detail": "open positions could not be read from MT5"}
        managed = [position for position in positions if getattr(position, "magic", self.settings.magic_number) == self.settings.magic_number]
        accepted = (mt5_constants().TRADE_RETCODE_DONE, mt5_constants().TRADE_RETCODE_DONE_PARTIAL)
        closed = failed = 0
        for position in managed:
            try:
                result = self.gateway.close_position(position)
            except Exception as error:
                failed += 1
                logger.error("emergency_close_failed ticket=%s symbol=%s error=%s", getattr(position, "ticket", None), getattr(position, "symbol", None), type(error).__name__)
                continue
            retcode = getattr(result, "retcode", None) if result is not None else None
            if retcode in accepted:
                closed += 1
                logger.warning("emergency_position_closed ticket=%s symbol=%s retcode=%s", getattr(position, "ticket", None), getattr(position, "symbol", None), retcode)
            else:
                failed += 1
                logger.error("emergency_close_rejected ticket=%s symbol=%s retcode=%s", getattr(position, "ticket", None), getattr(position, "symbol", None), retcode)
        return {"closed": closed, "failed": failed, "skipped": len(positions) - len(managed), "detail": "closing requested for bot-managed positions only"}

    def _cancel_managed_orders(self) -> dict:
        orders = self.gateway.orders()
        if orders is None:
            logger.error("emergency_cancel_skipped reason=orders_unreadable")
            return {"cancelled": 0, "failed": 0, "skipped": 0, "detail": "pending orders could not be read from MT5"}
        managed = [order for order in orders if getattr(order, "magic", self.settings.magic_number) == self.settings.magic_number]
        accepted = (mt5_constants().TRADE_RETCODE_DONE, mt5_constants().TRADE_RETCODE_DONE_PARTIAL)
        cancelled = failed = 0
        for order in managed:
            try:
                result = self.gateway.cancel_order(order)
            except Exception as error:
                failed += 1
                logger.error("emergency_cancel_failed ticket=%s symbol=%s error=%s", getattr(order, "ticket", None), getattr(order, "symbol", None), type(error).__name__)
                continue
            retcode = getattr(result, "retcode", None) if result is not None else None
            if retcode in accepted:
                cancelled += 1
            else:
                failed += 1
                logger.error("emergency_cancel_rejected ticket=%s symbol=%s retcode=%s", getattr(order, "ticket", None), getattr(order, "symbol", None), retcode)
        return {"cancelled": cancelled, "failed": failed, "skipped": len(orders) - len(managed), "detail": "pending orders are cancelled for bot-managed tickets only"}
