"""The effective live-ordering state the order gate decides from.

``Settings.live_orders_permitted`` - ``TRADING_MODE=live`` with ``LIVE_TRADING_ENABLED=true`` - is
the *configuration* answer and stays true. The deployment is deliberately not downgraded to another
mode to stop ordering: reconciliation, analytics, the MT5 monitor and the dashboard keep reading a
live deployment while it refuses new entries.

What changes once the owner's single authorized live trade has been spent is the *effective* answer
for order authorization::

    ordering_enabled = live_orders_permitted AND the one-trade authorization is unspent

so exactly one live entry may ever be authorized, and after it every further entry is refused - on
the next cycle, after a process restart, after a scheduler restart or a reboot, and on the next
calendar day - until an operator re-authorizes one trade explicitly. Every failure mode is closed:
an authorization that cannot be read is never an unspent one.
"""
from dataclasses import dataclass
from datetime import datetime
import logging

from app.core.clock import isoformat
from app.core.config import Settings
from app.risk.state import RiskStateStore

logger = logging.getLogger(__name__)

# The name of the check in the risk engine's chain, so a rejection reads the same everywhere.
CHECK_NAME = "one_trade_authorization"


@dataclass(frozen=True)
class AuthorizationState:
    """The one-trade authorization as the gate sees it: the configuration gate plus the flag."""

    live_orders_permitted: bool
    # None is "not verifiable" (no store, or a read that failed), never "unspent".
    consumed: bool | None = None
    consumed_at: datetime | None = None
    trade_id: str | None = None
    reason: str | None = None

    @property
    def available(self) -> bool:
        """True while one live entry may still be authorized."""
        return bool(self.live_orders_permitted and self.consumed is False)

    @property
    def ordering_enabled(self) -> bool:
        """The effective live-ordering state for order authorization."""
        return self.available

    @property
    def blocked_reason(self) -> str:
        """Why no live order may be authorized, or an empty string when one may."""
        if not self.live_orders_permitted:
            return ""
        if self.consumed is None:
            return "the single live-trade authorization could not be read, so no live order may be authorized"
        if not self.consumed:
            return ""
        spent = f" on trade {self.trade_id}" if self.trade_id else ""
        when = f" at {isoformat(self.consumed_at)}" if self.consumed_at is not None else ""
        return f"the single authorized live trade was already reserved{spent}{when}: a further live entry requires a new manual authorization"

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reason)

    def as_dict(self) -> dict:
        return {
            "live_orders_permitted": self.live_orders_permitted,
            "consumed": self.consumed,
            "consumed_at": isoformat(self.consumed_at),
            "consumed_trade_id": self.trade_id,
            "consumed_reason": self.reason,
            "ordering_enabled": self.ordering_enabled,
        }


def authorization_state(settings: Settings, store: RiskStateStore | None) -> AuthorizationState:
    """Read the authorization; a missing or unreadable store yields ``consumed=None`` (fail closed)."""
    live = bool(settings.live_orders_permitted)
    if store is None:
        return AuthorizationState(live_orders_permitted=live)
    try:
        row = store.one_trade_authorization()
    except Exception as error:  # an unreadable authorization must never read as "one still available"
        logger.error("one_trade_authorization_unreadable error=%s fail_closed=%s", type(error).__name__, live)
        return AuthorizationState(live_orders_permitted=live)
    return AuthorizationState(
        live_orders_permitted=live,
        consumed=bool(row.consumed),
        consumed_at=row.consumed_at,
        trade_id=row.consumed_trade_id,
        reason=row.consumed_reason,
    )
