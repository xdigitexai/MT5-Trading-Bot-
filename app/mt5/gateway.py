"""Only boundary allowed to call MetaTrader5; every failure returns a closed state."""
from dataclasses import dataclass
from typing import Any
from app.core.config import Settings

try:
    import MetaTrader5 as mt5
except ImportError:  # permits strategy/backtest tests without a Windows terminal
    mt5 = None

@dataclass(frozen=True)
class MT5Health:
    connected: bool
    detail: str

class MT5Gateway:
    def __init__(self, settings: Settings): self.settings, self._connected = settings, False
    def initialize(self) -> MT5Health:
        if mt5 is None: return MT5Health(False, "MetaTrader5 package unavailable")
        kwargs = {"path": self.settings.mt5_terminal_path} if self.settings.mt5_terminal_path else {}
        if not mt5.initialize(**kwargs): return MT5Health(False, str(mt5.last_error()))
        self._connected = True
        return MT5Health(True, "initialized")
    def login(self) -> MT5Health:
        if not self._connected: return MT5Health(False, "terminal not initialized")
        if not all((self.settings.mt5_login, self.settings.mt5_password, self.settings.mt5_server)):
            return MT5Health(False, "MT5 credentials are not configured")
        ok = mt5.login(self.settings.mt5_login, password=self.settings.mt5_password.get_secret_value(), server=self.settings.mt5_server)
        return MT5Health(bool(ok), "logged in" if ok else str(mt5.last_error()))
    def shutdown(self):
        if mt5: mt5.shutdown()
        self._connected = False
    def health(self) -> MT5Health:
        if not self._connected or mt5 is None: return MT5Health(False, "not connected")
        info = mt5.terminal_info()
        return MT5Health(info is not None, "healthy" if info else str(mt5.last_error()))
    def account_info(self) -> Any: return mt5.account_info() if self.health().connected else None
    def terminal_info(self) -> Any: return mt5.terminal_info() if self.health().connected else None
    def discover_symbol(self, canonical: str) -> str | None:
        if not self.health().connected: return None
        candidates = mt5.symbols_get(group=f"*{canonical}*") or []
        for item in candidates:
            if item.name.upper().startswith(canonical):
                mt5.symbol_select(item.name, True)
                return item.name
        return None
    def tick(self, symbol: str): return mt5.symbol_info_tick(symbol) if self.health().connected else None
    def symbol_info(self, symbol: str): return mt5.symbol_info(symbol) if self.health().connected else None
    def rates(self, symbol: str, timeframe: int, count: int):
        return mt5.copy_rates_from_pos(symbol, timeframe, 0, count) if self.health().connected else None
    def positions(self): return mt5.positions_get() if self.health().connected else ()
    def orders(self): return mt5.orders_get() if self.health().connected else ()
    def history(self, start, end): return mt5.history_deals_get(start, end) if self.health().connected else ()
    def order_send(self, request: dict): return mt5.order_send(request) if self.health().connected else None
    def modify_position(self, ticket: int, symbol: str, stop_loss: float, take_profit: float):
        if not self.health().connected: return None
        return mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": symbol, "sl": stop_loss, "tp": take_profit})
    def cancel_order(self, order):
        """Remove one pending order; the caller decides which tickets are its own."""
        if not self.health().connected: return None
        return mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": order.ticket})
    def close_position(self, position):
        if not self.health().connected: return None
        tick = self.tick(position.symbol)
        if tick is None: return None
        side = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if side == mt5.ORDER_TYPE_SELL else tick.ask
        return mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "position": position.ticket, "symbol": position.symbol, "volume": position.volume, "type": side, "price": price, "deviation": self.settings.order_deviation_points, "magic": self.settings.magic_number})
