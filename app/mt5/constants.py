"""MT5 request constants.

MetaTrader5 stays the source of truth for execution, but its module-level enums only import on a
Windows host with the package installed, so the documented numeric values are mirrored here for
building request dicts and for fake-gateway tests. Sending an order still requires a connected
gateway; these constants never authorize execution on their own.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MT5Constants:
    ORDER_TYPE_BUY: int = 0
    ORDER_TYPE_SELL: int = 1
    TRADE_ACTION_DEAL: int = 1
    TRADE_ACTION_SLTP: int = 2
    ORDER_TIME_GTC: int = 0
    ORDER_FILLING_FOK: int = 0
    ORDER_FILLING_IOC: int = 1
    ORDER_FILLING_RETURN: int = 2
    TRADE_RETCODE_PLACED: int = 10008
    TRADE_RETCODE_DONE: int = 10009
    TRADE_RETCODE_DONE_PARTIAL: int = 10010


# Retcodes that mean the broker accepted the order; anything else is a rejection.
ACCEPTED_RETCODES = (MT5Constants.TRADE_RETCODE_DONE, MT5Constants.TRADE_RETCODE_DONE_PARTIAL)


def mt5_constants(api: Any = None) -> MT5Constants:
    """Real MT5 enum values when the module is importable, mirrored values otherwise."""
    if api is None:
        try:
            import MetaTrader5 as api  # type: ignore[no-redef]
        except ImportError:
            return MT5Constants()
    defaults = MT5Constants()
    return MT5Constants(*[int(getattr(api, field, getattr(defaults, field))) for field in MT5Constants.__dataclass_fields__])
