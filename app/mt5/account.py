"""Account identity, read from MT5 instead of the environment.

``TRADING_MODE`` is a *bot* setting: it says how this process is allowed to behave, never what the
broker account is. The truth about the account is ``account_info().trade_mode`` (0 = DEMO,
1 = CONTEST, 2 = REAL), so it is read there and reported explicitly. An unreadable trade mode is
kept as ``None`` and must be treated as "unknown", never as demo: the caller fails closed.

``account_matches`` is the second half of that identity: the login and the server the terminal is
actually connected to must still be the account the operator configured (``MT5_LOGIN`` /
``MT5_SERVER``). A terminal that silently reconnects to another account is a hard stop, so the
comparison is available to every layer that is about to send an order.
"""
from dataclasses import asdict, dataclass
from typing import Any

TRADE_MODE_DEMO = 0
TRADE_MODE_CONTEST = 1
TRADE_MODE_REAL = 2
TRADE_MODE_NAMES: dict[int, str] = {TRADE_MODE_DEMO: "DEMO", TRADE_MODE_CONTEST: "CONTEST", TRADE_MODE_REAL: "REAL"}
UNKNOWN_TRADE_MODE = "UNKNOWN"


def trade_mode_name(trade_mode: int | None) -> str:
    return TRADE_MODE_NAMES.get(trade_mode, UNKNOWN_TRADE_MODE) if trade_mode is not None else UNKNOWN_TRADE_MODE


@dataclass(frozen=True)
class AccountProfile:
    """The broker account as MT5 reports it, including its real/demo classification."""

    login: int | None
    server: str
    company: str
    currency: str
    balance: float
    equity: float
    free_margin: float | None
    leverage: float
    trade_mode: int | None

    @property
    def trade_mode_label(self) -> str:
        return trade_mode_name(self.trade_mode)

    @property
    def is_real(self) -> bool:
        return self.trade_mode == TRADE_MODE_REAL

    @property
    def is_demo(self) -> bool:
        return self.trade_mode == TRADE_MODE_DEMO

    @property
    def classified(self) -> bool:
        """True only when the broker reported a trade mode this module understands."""
        return self.trade_mode in TRADE_MODE_NAMES

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["trade_mode_label"] = self.trade_mode_label
        payload["is_real"] = self.is_real
        payload["classified"] = self.classified
        return payload


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def account_profile(account: Any) -> AccountProfile | None:
    """MT5 account_info as a profile; None when the account cannot be read at all."""
    if account is None:
        return None
    raw_mode = getattr(account, "trade_mode", None)
    try:
        trade_mode = int(raw_mode) if raw_mode is not None else None
    except (TypeError, ValueError):
        trade_mode = None
    login = getattr(account, "login", None)
    try:
        login = int(login) if login is not None else None
    except (TypeError, ValueError):
        login = None
    free_margin = getattr(account, "margin_free", None)
    return AccountProfile(
        login=login,
        server=_text(getattr(account, "server", None)),
        company=_text(getattr(account, "company", None)),
        currency=_text(getattr(account, "currency", None)),
        balance=_number(getattr(account, "balance", None)),
        equity=_number(getattr(account, "equity", None)),
        free_margin=None if free_margin is None else _number(free_margin),
        leverage=_number(getattr(account, "leverage", None)),
        trade_mode=trade_mode,
    )


def account_matches(profile: AccountProfile | None, expected_login: int | None, expected_server: str | None) -> tuple[bool, str]:
    """Whether the live account is still the one the operator pinned, with the reason when it is not.

    A configured expectation is mandatory: when ``expected_login`` or ``expected_server`` is set, a
    missing, unreadable or different value fails. An account identity that was never configured
    cannot have "changed unexpectedly", and a deployment that logs in at all always configures both
    (``MT5Gateway.login`` refuses to log in without them), so the unconfigured case is not a hole in
    the trading path - it is only reachable by a caller that never contacted the broker.
    """
    login_expected = expected_login is not None
    server_expected = bool(expected_server and str(expected_server).strip())
    if not (login_expected or server_expected):
        return True, ""
    if profile is None:
        return False, "the broker account could not be read, so the expected login/server cannot be verified"
    expected = f"{expected_login if login_expected else '-'} @ {expected_server if server_expected else '-'}"
    if login_expected and profile.login != int(expected_login):
        return False, f"the terminal is logged in to MT5 account {profile.login} ({profile.server}), not the expected {expected}: refusing to trade"
    if server_expected and str(profile.server).strip().upper() != str(expected_server).strip().upper():
        return False, f"the terminal is connected to server {profile.server}, not the expected {expected_server}: refusing to trade"
    return True, ""

