class NewsFilter:
    """Provider interface; no scraping dependency. Unknown availability obeys fail-closed policy."""
    def __init__(self, fail_closed: bool = True): self.fail_closed, self.available = fail_closed, False
    def can_trade(self, symbol: str) -> bool: return self.available or not self.fail_closed
