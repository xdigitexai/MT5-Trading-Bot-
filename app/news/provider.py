"""News protection policy.

The provider abstraction exists so the trading loop can ask one question — "may this symbol be
traded right now?" — and get an honest answer about *availability* as well as about events:

- ``NewsProvider.available`` is False whenever no real event data is configured. Nothing here
  fabricates a calendar.
- When the configured policy requires news protection (``settings.news_fail_closed``) and the
  provider is unavailable, ``decision()`` reports ``allowed=False``. The bot then refuses new
  orders instead of pretending a protection window is being enforced.
- ``StaticNewsProvider`` blocks a symbol while a configured high-impact event for either of its
  currencies falls inside the configured window around it. An empty calendar counts as
  unconfigured, so it stays unavailable rather than silently allowing every trade.
- ``StaticNewsProvider.from_file`` reads an operator-supplied JSON calendar; a missing or corrupt
  file yields an unavailable provider with the reason recorded, never a crash.
"""
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
import json
import logging

from app.core.clock import as_utc, utcnow

logger = logging.getLogger(__name__)

HIGH_IMPACT = "high"


@dataclass(frozen=True)
class NewsEvent:
    when: datetime
    currency: str
    impact: str = HIGH_IMPACT
    title: str = ""


@dataclass(frozen=True)
class NewsDecision:
    allowed: bool
    available: bool
    reason: str
    events: list[str] = field(default_factory=list)


def currencies_of(symbol: str) -> tuple[str, ...]:
    text = str(symbol or "").strip().upper()
    if len(text) < 6:
        return (text,) if text else ()
    return (text[:3], text[3:6])


class NewsProvider:
    """Interface implemented by every news source; unavailable unless real data is configured."""

    available = False

    def decision(self, symbol: str, now: datetime | None = None) -> NewsDecision:
        raise NotImplementedError

    def can_trade(self, symbol: str, now: datetime | None = None) -> bool:
        return self.decision(symbol, now).allowed


class UnavailableNewsProvider(NewsProvider):
    """Nothing configured: reports unavailable and obeys the configured policy."""

    def __init__(self, fail_closed: bool = True, detail: str = "no news source is configured"):
        self.fail_closed, self.detail = fail_closed, detail

    @property
    def available(self) -> bool:
        return False

    def decision(self, symbol: str, now: datetime | None = None) -> NewsDecision:
        if self.fail_closed:
            return NewsDecision(False, False, f"news data is unavailable ({self.detail}) and the policy requires news protection: trading is blocked")
        return NewsDecision(True, False, f"news data is unavailable ({self.detail}); the policy does not require news protection, so no window is applied")


class StaticNewsProvider(NewsProvider):
    """Operator-supplied calendar; every block is traceable to a configured event."""

    def __init__(self, events: list[NewsEvent] | None = None, *, fail_closed: bool = True, window_minutes: int = 30, source: str | None = None):
        self.events = list(events or ())
        self.fail_closed, self.window_minutes, self.source = fail_closed, window_minutes, source

    @property
    def available(self) -> bool:
        return bool(self.events)

    def decision(self, symbol: str, now: datetime | None = None) -> NewsDecision:
        if not self.available:
            return UnavailableNewsProvider(self.fail_closed, f"the news calendar{'' if self.source is None else f' {self.source}'} contains no events").decision(symbol, now)
        moment = as_utc(now) or utcnow()
        window = timedelta(minutes=self.window_minutes)
        currencies = currencies_of(symbol)
        blocking = [
            event for event in self.events
            if event.impact.lower() == HIGH_IMPACT
            and event.currency.upper() in currencies
            and abs(as_utc(event.when) - moment) <= window
        ]
        if blocking:
            labels = [f"{event.currency} {event.title or 'high impact'} at {as_utc(event.when).isoformat()}" for event in blocking]
            return NewsDecision(False, True, f"high impact news inside the +/-{self.window_minutes} minute window: {'; '.join(labels)}", labels)
        return NewsDecision(True, True, f"no high impact news for {currencies} inside the +/-{self.window_minutes} minute window")

    @classmethod
    def from_file(cls, path: str, *, fail_closed: bool = True, window_minutes: int = 30) -> NewsProvider:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.error("news_calendar_unreadable path=%s error=%s", path, type(error).__name__)
            return UnavailableNewsProvider(fail_closed, f"news calendar {path} could not be read ({type(error).__name__})")
        items = payload if isinstance(payload, list) else payload.get("events", [])
        events, rejected = [], 0
        for item in items:
            try:
                events.append(NewsEvent(
                    when=as_utc(datetime.fromisoformat(str(item["time"]))),
                    currency=str(item["currency"]).upper(),
                    impact=str(item.get("impact", HIGH_IMPACT)).lower(),
                    title=str(item.get("title", "")),
                ))
            except (KeyError, TypeError, ValueError):
                rejected += 1
        if rejected:
            logger.warning("news_calendar_entries_rejected path=%s rejected=%s accepted=%s", path, rejected, len(events))
        return cls(events, fail_closed=fail_closed, window_minutes=window_minutes, source=path)


def build_news_provider(settings) -> NewsProvider:
    """Configured provider, or an honest 'unavailable' one when nothing is set up."""
    if settings.news_events_file:
        return StaticNewsProvider.from_file(settings.news_events_file, fail_closed=settings.news_fail_closed, window_minutes=settings.news_window_minutes)
    return UnavailableNewsProvider(settings.news_fail_closed)
