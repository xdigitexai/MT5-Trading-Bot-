"""News filter. Unconfigured feed does not block trading."""
class NewsFilter:
    def __init__(self, fail_closed: bool = True):
        self.fail_closed = fail_closed
        self.available = False
        self._configured = False
    def attach_feed(self) -> None:
        self._configured = True
        self.available = True
    def can_trade(self, symbol: str) -> bool:
        if not self._configured:
            return True
        if self.available:
            return True
        return not self.fail_closed
