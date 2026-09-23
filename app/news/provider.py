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
- ``TradingEconomicsCalendarProvider`` reads the official Trading Economics calendar **API**
  (``api.tradingeconomics.com``): no web page is ever fetched and no calendar row is ever invented.
  Events are normalised into ``NewsEvent`` rows, cached so the loop does not call the provider once
  per symbol per cycle, and every failure mode — a missing or rejected credential, a transport
  error, a non-200 answer, a payload that is not a list, a row with an invalid timestamp or an
  unrecognised importance, a calendar older than the allowed age — is reported as *not usable*, so
  the gate fails closed and no new position is opened. The provider never closes an existing
  position: it only answers whether a *new* entry is allowed.

The provider's credential is read from the environment only. It is never logged, and it is only
ever sent to the configured Trading Economics host.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import logging
import re

from app.core.clock import as_utc, isoformat, utcnow

logger = logging.getLogger(__name__)

HIGH_IMPACT = "high"
MEDIUM_IMPACT = "medium"
LOW_IMPACT = "low"
IMPACT_LEVELS = (LOW_IMPACT, MEDIUM_IMPACT, HIGH_IMPACT)

TRADING_ECONOMICS = "trading_economics"
TRADING_ECONOMICS_BASE_URL = "https://api.tradingeconomics.com"
DEFAULT_TIMEOUT_SECONDS = 20.0

# The gate distinguishes a fresh calendar from a stale one and from a provider that cannot be
# reached at all; the state is reported verbatim so an operator never has to read a log to tell.
STATE_FRESH = "FRESH"
STATE_STALE = "STALE"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_NO_CREDENTIAL = "NO_CREDENTIAL"
STATE_NOT_ATTEMPTED = "NOT_ATTEMPTED"

# Trading Economics rates importance 1/2/3 and occasionally spells it out. The app's own vocabulary
# is low/medium/high, so every row is normalised before it is allowed to influence the gate.
_IMPACT_BY_IMPORTANCE = {
    "1": LOW_IMPACT, "2": MEDIUM_IMPACT, "3": HIGH_IMPACT,
    "low": LOW_IMPACT, "medium": MEDIUM_IMPACT, "high": HIGH_IMPACT,
}

# ISO-4217 codes the pairs this bot scans can be built from. A symbol whose halves are not both
# recognised has no currency mapping, and a symbol whose currencies cannot be mapped is not traded:
# guessing is how an unrelated currency's events would block - or fail to block - a pair.
KNOWN_CURRENCIES = frozenset(
    "USD EUR JPY GBP CHF CAD AUD NZD CNY HKD SGD SEK NOK DKK PLN CZK HUF RON BGN HRK ISK "
    "TRY ZAR MXN BRL ARS CLP COP PEN UYU RUB UAH KZT ILS SAR AED QAR KWD BHD OMR JOD EGP "
    "NGN KES GHS MAD TND ZMW INR IDR MYR THB PHP VND KRW TWD PKR BDT LKR NPR".split()
)

# Trading Economics reports the *country* as well as the currency. A row that carries no currency
# of its own is attributed through its country; a high-impact row that still cannot be attributed
# makes the whole calendar unusable, because it is exactly the row that could have been protection.
_COUNTRY_CURRENCIES = {
    "united states": "USD", "euro area": "EUR", "euroarea": "EUR", "germany": "EUR", "france": "EUR",
    "italy": "EUR", "spain": "EUR", "netherlands": "EUR", "belgium": "EUR", "portugal": "EUR",
    "ireland": "EUR", "greece": "EUR", "austria": "EUR", "finland": "EUR", "slovakia": "EUR",
    "united kingdom": "GBP", "japan": "JPY", "switzerland": "CHF", "canada": "CAD",
    "australia": "AUD", "new zealand": "NZD", "china": "CNY", "hong kong": "HKD",
    "singapore": "SGD", "sweden": "SEK", "norway": "NOK", "denmark": "DKK", "poland": "PLN",
    "czech republic": "CZK", "hungary": "HUF", "romania": "RON", "bulgaria": "BGN",
    "turkey": "TRY", "south africa": "ZAR", "mexico": "MXN", "brazil": "BRL",
    "argentina": "ARS", "chile": "CLP", "colombia": "COP", "peru": "PEN", "russia": "RUB",
    "ukraine": "UAH", "kazakhstan": "KZT", "israel": "ILS", "saudi arabia": "SAR",
    "united arab emirates": "AED", "qatar": "QAR", "kuwait": "KWD", "india": "INR",
    "indonesia": "IDR", "malaysia": "MYR", "thailand": "THB", "philippines": "PHP",
    "vietnam": "VND", "south korea": "KRW", "taiwan": "TWD", "pakistan": "PKR",
    "bangladesh": "BDT", "sri lanka": "LKR", "nepal": "NPR", "egypt": "EGP", "nigeria": "NGN",
    "kenya": "KES", "ghana": "GHS", "morocco": "MAD", "tunisia": "TND",
}


@dataclass(frozen=True)
class NewsEvent:
    """One calendar row in the app's own vocabulary.

    ``when`` is always UTC. The identity and provenance fields (``event_id`` … ``provider``) are
    what the persisted cache stores and what a blocked decision quotes back to an operator.
    """

    when: datetime
    currency: str
    impact: str = HIGH_IMPACT
    title: str = ""
    event_id: str = ""
    country: str = ""
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    retrieved_at: datetime | None = None
    provider: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "when", as_utc(self.when))
        object.__setattr__(self, "currency", str(self.currency or "").upper())
        object.__setattr__(self, "impact", str(self.impact or "").lower())
        object.__setattr__(self, "retrieved_at", as_utc(self.retrieved_at))

    @property
    def high_impact(self) -> bool:
        return self.impact == HIGH_IMPACT

    def label(self) -> str:
        """A log-safe one-line summary; never contains a credential."""
        title = self.title or "high impact event"
        return f"{self.currency} {title} at {as_utc(self.when).isoformat()}"

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "country": self.country,
            "currency": self.currency,
            "timestamp": isoformat(self.when),
            "impact": self.impact,
            "actual": self.actual,
            "forecast": self.forecast,
            "previous": self.previous,
            "retrieved_at": isoformat(self.retrieved_at),
            "provider": self.provider,
        }


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


def pair_currencies(symbol: str) -> tuple[str, str] | None:
    """The two ISO codes an FX symbol trades, or None when the pair cannot be mapped.

    Broker-native names carry a suffix (``EURUSDm`` on this broker), so the symbol is reduced to
    its letters and the first two three-letter groups are validated against the known ISO codes.
    """
    letters = re.sub(r"[^A-Za-z]", "", str(symbol or "")).upper()
    if len(letters) < 6:
        return None
    base, quote = letters[:3], letters[3:6]
    if base in KNOWN_CURRENCIES and quote in KNOWN_CURRENCIES and base != quote:
        return (base, quote)
    return None


def normalise_impact(value: object) -> str | None:
    """Trading Economics importance as the app's low/medium/high, or None when unrecognised."""
    text = str(value if value is not None else "").strip().lower()
    return _IMPACT_BY_IMPORTANCE.get(text)


def parse_timestamp(value: object) -> datetime | None:
    """An ISO-8601 calendar timestamp as aware UTC; None when it is missing or unparseable."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _field(item: dict, *names: str) -> object:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class NewsError(Exception):
    """Base class for every way the calendar provider can fail."""


class CalendarUnavailable(NewsError):
    """Transport failure, timeout or a non-success HTTP status: the provider cannot be reached."""


class CalendarAuthError(NewsError):
    """The credential is missing or was refused: the provider is configured but not authorized."""


class CalendarMalformed(NewsError):
    """The provider answered, but the payload cannot be interpreted as a calendar."""


def parse_calendar_payload(payload: object, *, retrieved_at: datetime, provider: str = TRADING_ECONOMICS) -> list[NewsEvent]:
    """Normalise the official calendar payload into ``NewsEvent`` rows.

    The official endpoint answers with a JSON array of objects carrying ``CalendarId``, ``Event``,
    ``Country``, ``Currency``, ``Date``, ``Importance``, ``Actual``, ``Forecast`` and ``Previous``.
    A payload that is not a list, a row that is not an object, a row without a usable timestamp or
    importance, or a *high-impact* row whose currency cannot be attributed, all raise
    ``CalendarMalformed`` — a calendar that cannot be read in full is not a calendar that may be
    used to prove a window is clear.
    """
    moment = as_utc(retrieved_at) or utcnow()
    if not isinstance(payload, list):
        raise CalendarMalformed(f"the calendar payload is {type(payload).__name__} instead of a list of events")
    events: list[NewsEvent] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise CalendarMalformed(f"calendar row {index} is {type(item).__name__} instead of an object")
        when = parse_timestamp(_field(item, "Date", "date", "Timestamp"))
        if when is None:
            raise CalendarMalformed(f"calendar row {index} has a missing or invalid timestamp")
        impact = normalise_impact(_field(item, "Importance", "importance", "Impact"))
        if impact is None:
            raise CalendarMalformed(f"calendar row {index} has an unrecognised importance")
        country = _text(_field(item, "Country", "country")) or ""
        currency = (_text(_field(item, "Currency", "currency")) or "").upper()
        if not currency:
            currency = _COUNTRY_CURRENCIES.get(country.strip().lower(), "")
        if not currency:
            if impact == HIGH_IMPACT:
                raise CalendarMalformed(f"high impact calendar row {index} carries no currency and no country that maps to one")
            logger.debug("news_event_skipped_unattributable index=%s country=%s impact=%s", index, country, impact)
            continue
        events.append(NewsEvent(
            when=when,
            currency=currency,
            impact=impact,
            title=_text(_field(item, "Event", "event", "Category")) or "",
            event_id=_text(_field(item, "CalendarId", "calendar_id", "EventId")) or f"{provider}:{index}:{when.isoformat()}",
            country=country,
            actual=_text(_field(item, "Actual", "actual")),
            forecast=_text(_field(item, "Forecast", "forecast", "TEForecast")),
            previous=_text(_field(item, "Previous", "previous")),
            retrieved_at=moment,
            provider=provider,
        ))
    return events


class TradingEconomicsCalendarClient:
    """The official Trading Economics calendar API, and nothing else.

    The credential is read from the environment by the caller and is only ever placed in the ``c``
    query parameter of the ``api.tradingeconomics.com`` URL. No web page is fetched and the
    credential is never logged.
    """

    provider = TRADING_ECONOMICS

    def __init__(self, api_key: object, *, base_url: str = TRADING_ECONOMICS_BASE_URL, timeout: float = DEFAULT_TIMEOUT_SECONDS, transport=None):
        self._api_key = str(api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else (api_key or "")).strip()
        self.base_url = str(base_url or TRADING_ECONOMICS_BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def transport(self):
        if self._transport is None:
            import httpx  # imported lazily so the module stays importable without the client stack
            self._transport = httpx.Client()
        return self._transport

    def close(self) -> None:
        if self._transport is not None and hasattr(self._transport, "close"):
            try:
                self._transport.close()
            except Exception as error:  # closing a transport is never worth a crash
                logger.debug("news_transport_close_failed error=%s", type(error).__name__)

    def calendar(self, start: datetime, end: datetime) -> object:
        """Raw calendar rows between two UTC instants; raises a ``NewsError`` subclass on failure."""
        if not self.configured:
            raise CalendarAuthError("no Trading Economics API credential is configured")
        url = f"{self.base_url}/calendar/from/{as_utc(start):%Y-%m-%d}/to/{as_utc(end):%Y-%m-%d}"
        params = {"c": self._api_key, "f": "json"}
        try:
            response = self.transport().get(url, params=params, timeout=self.timeout)
        except NewsError:
            raise
        except Exception as error:
            raise CalendarUnavailable(f"the calendar request failed ({type(error).__name__})") from error
        status = getattr(response, "status_code", None)
        if status in (401, 403):
            raise CalendarAuthError(f"the Trading Economics credential was rejected (HTTP {status})")
        if status != 200:
            raise CalendarUnavailable(f"the calendar endpoint answered HTTP {status if status is not None else 'nothing'}")
        try:
            return response.json()
        except Exception as error:
            raise CalendarMalformed(f"the calendar response was not valid JSON ({type(error).__name__})") from error


@dataclass
class CalendarSnapshot:
    """Everything the gate knows about the calendar right now, including why it is unusable."""

    events: list[NewsEvent] = field(default_factory=list)
    retrieved_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    healthy: bool = False
    state: str = STATE_NOT_ATTEMPTED
    detail: str = "the calendar has not been fetched yet"
    last_error: str | None = None


@dataclass(frozen=True)
class CachedCalendar:
    """The persisted half of the cache: the last good calendar and when it was retrieved."""

    events: list[NewsEvent]
    retrieved_at: datetime | None
    last_success_at: datetime | None = None
    detail: str = ""


class NewsCache:
    """Interface for the calendar cache; the default keeps nothing."""

    def load(self) -> CachedCalendar | None:
        return None

    def save(self, events: list[NewsEvent], *, retrieved_at: datetime | None, last_success_at: datetime | None, healthy: bool, detail: str) -> None:
        return None


class NullNewsCache(NewsCache):
    """No persistence: the cache lives only in the provider's memory."""


class SqlNewsCache(NewsCache):
    """Calendar cache persisted in the application database.

    The loop must not call the provider once per symbol per cycle, and a restarted process must be
    able to see *when* the calendar it is holding was fetched — that age is what separates "fresh"
    from "stale". Failures here are logged and swallowed: an unwritable cache never fabricates a
    calendar, it only means the next process has to fetch one before it may trade.
    """

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def load(self) -> CachedCalendar | None:
        from sqlalchemy import select
        from app.database.base import NewsEventRecord, NewsProviderStateRecord

        try:
            with self._session() as db:
                state = db.scalar(select(NewsProviderStateRecord).where(NewsProviderStateRecord.provider == TRADING_ECONOMICS))
                rows = list(db.scalars(select(NewsEventRecord).where(NewsEventRecord.provider == TRADING_ECONOMICS).order_by(NewsEventRecord.event_time)))
        except Exception as error:
            logger.error("news_cache_unreadable error=%s", type(error).__name__)
            return None
        if state is None and not rows:
            return None
        events = [
            NewsEvent(
                when=row.event_time, currency=row.currency, impact=row.impact, title=row.title or "",
                event_id=row.event_id, country=row.country or "", actual=row.actual, forecast=row.forecast,
                previous=row.previous, retrieved_at=row.retrieved_at, provider=row.provider,
            )
            for row in rows
        ]
        return CachedCalendar(
            events=events,
            retrieved_at=as_utc(state.retrieved_at) if state is not None else None,
            last_success_at=as_utc(state.last_success_at) if state is not None else None,
            detail=(state.detail or "") if state is not None else "",
        )

    def save(self, events: list[NewsEvent], *, retrieved_at: datetime | None, last_success_at: datetime | None, healthy: bool, detail: str) -> None:
        from sqlalchemy import delete, select
        from app.database.base import NewsEventRecord, NewsProviderStateRecord

        try:
            with self._session() as db:
                if events:
                    db.execute(delete(NewsEventRecord).where(NewsEventRecord.provider == TRADING_ECONOMICS))
                    for event in events:
                        db.add(NewsEventRecord(
                            provider=event.provider or TRADING_ECONOMICS, event_id=event.event_id, title=event.title,
                            country=event.country, currency=event.currency, impact=event.impact,
                            event_time=as_utc(event.when), actual=event.actual, forecast=event.forecast,
                            previous=event.previous, retrieved_at=as_utc(event.retrieved_at) or utcnow(),
                        ))
                state = db.scalar(select(NewsProviderStateRecord).where(NewsProviderStateRecord.provider == TRADING_ECONOMICS))
                if state is None:
                    state = NewsProviderStateRecord(provider=TRADING_ECONOMICS)
                    db.add(state)
                state.healthy, state.detail, state.event_count = bool(healthy), str(detail or "")[:4000], len(events)
                state.retrieved_at = as_utc(retrieved_at)
                state.last_success_at = as_utc(last_success_at)
                state.last_attempt_at = utcnow()
                db.commit()
        except Exception as error:
            logger.error("news_cache_unwritable error=%s", type(error).__name__)

    def _session(self):
        return self.session_factory()


class NewsProvider:
    """Interface implemented by every news source; unavailable unless real data is configured."""

    available = False
    provider = "none"

    def refresh(self, now: datetime | None = None) -> None:
        """Bring the calendar up to date if the source needs it; a no-op for a static calendar."""
        return None

    def status(self, now: datetime | None = None) -> dict:
        return {
            "provider": self.provider,
            "available": self.available,
            "healthy": self.available,
            "state": STATE_FRESH if self.available else STATE_UNAVAILABLE,
            "detail": "",
            "last_attempt_at": None,
            "last_success_at": None,
            "data_age_seconds": None,
            "events": 0,
            "high_impact_events": 0,
        }

    def decision(self, symbol: str, now: datetime | None = None) -> NewsDecision:
        raise NotImplementedError

    def can_trade(self, symbol: str, now: datetime | None = None) -> bool:
        return self.decision(symbol, now).allowed


class UnavailableNewsProvider(NewsProvider):
    """Nothing configured: reports unavailable and obeys the configured policy."""

    provider = "unavailable"

    def __init__(self, fail_closed: bool = True, detail: str = "no news source is configured"):
        self.fail_closed, self.detail = fail_closed, detail

    @property
    def available(self) -> bool:
        return False

    def status(self, now: datetime | None = None) -> dict:
        return {**super().status(now), "state": STATE_UNAVAILABLE, "detail": self.detail}

    def decision(self, symbol: str, now: datetime | None = None) -> NewsDecision:
        if self.fail_closed:
            return NewsDecision(False, False, f"news data is unavailable ({self.detail}) and the policy requires news protection: trading is blocked")
        return NewsDecision(True, False, f"news data is unavailable ({self.detail}); the policy does not require news protection, so no window is applied")


class StaticNewsProvider(NewsProvider):
    """Operator-supplied calendar; every block is traceable to a configured event."""

    provider = "static"

    def __init__(self, events: list[NewsEvent] | None = None, *, fail_closed: bool = True, window_minutes: int = 30, source: str | None = None):
        self.events = list(events or ())
        self.fail_closed, self.window_minutes, self.source = fail_closed, window_minutes, source

    @property
    def available(self) -> bool:
        return bool(self.events)

    def status(self, now: datetime | None = None) -> dict:
        return {
            **super().status(now),
            "state": STATE_FRESH if self.available else STATE_UNAVAILABLE,
            "detail": "" if self.available else f"the news calendar{'' if self.source is None else f' {self.source}'} contains no events",
            "events": len(self.events),
            "high_impact_events": sum(1 for event in self.events if event.high_impact),
            "window_minutes": self.window_minutes,
            "source": self.source,
        }

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
            labels = [event.label() for event in blocking]
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
                    provider="static",
                ))
            except (KeyError, TypeError, ValueError):
                rejected += 1
        if rejected:
            logger.warning("news_calendar_entries_rejected path=%s rejected=%s accepted=%s", path, rejected, len(events))
        return cls(events, fail_closed=fail_closed, window_minutes=window_minutes, source=path)


class TradingEconomicsCalendarProvider(NewsProvider):
    """Official Trading Economics calendar API, cached, normalised and fail-closed.

    One refresh serves every symbol of the cycle (``refresh_seconds`` throttles the calls), the
    calendar is normalised into the app's ``NewsEvent`` rows and persisted through the cache, and
    ``decision()`` answers only from what it can prove: a healthy provider *and* a calendar whose
    age is inside ``max_age_seconds``. A month-old calendar, a rejected credential or a broken
    payload all mean the same thing to the gate — no new position.
    """

    provider = TRADING_ECONOMICS

    def __init__(
        self,
        client: TradingEconomicsCalendarClient,
        *,
        fail_closed: bool = True,
        window_minutes: int = 30,
        refresh_seconds: int = 300,
        max_age_seconds: int = 900,
        failure_retry_seconds: int = 60,
        lookback_hours: int = 12,
        horizon_hours: int = 168,
        cache: NewsCache | None = None,
        clock=utcnow,
    ):
        self.client, self.fail_closed, self.window_minutes = client, bool(fail_closed), int(window_minutes)
        self.refresh_seconds, self.max_age_seconds = max(1, int(refresh_seconds)), max(1, int(max_age_seconds))
        self.failure_retry_seconds = max(1, min(int(failure_retry_seconds), self.refresh_seconds))
        self.lookback_hours, self.horizon_hours = max(0, int(lookback_hours)), max(1, int(horizon_hours))
        self.cache, self.clock = cache or NullNewsCache(), clock
        self.snapshot = CalendarSnapshot()
        self._restore()

    # ------------------------------------------------------------------ cache

    def _restore(self) -> None:
        """Load the persisted calendar so its age survives a restart; never read as 'healthy'."""
        cached = self.cache.load()
        if cached is None or not cached.events:
            return
        self.snapshot.events = list(cached.events)
        self.snapshot.retrieved_at = cached.retrieved_at
        self.snapshot.last_success_at = cached.last_success_at
        self.snapshot.detail = (
            f"{len(cached.events)} calendar event(s) were restored from the persisted cache; this process has not fetched the calendar yet"
        )
        logger.info("news_cache_restored events=%s retrieved_at=%s", len(cached.events), isoformat(cached.retrieved_at))

    # ------------------------------------------------------------------ refresh

    def _due(self, moment: datetime) -> bool:
        last = self.snapshot.last_attempt_at
        if last is None:
            return True
        interval = self.refresh_seconds if self.snapshot.healthy else self.failure_retry_seconds
        return (moment - as_utc(last)).total_seconds() >= interval

    def refresh(self, now: datetime | None = None) -> CalendarSnapshot:
        """Fetch and normalise the calendar when it is due; never raises."""
        moment = as_utc(now) or self.clock()
        if not self._due(moment):
            return self.snapshot
        self.snapshot.last_attempt_at = moment
        try:
            payload = self.client.calendar(moment - timedelta(hours=self.lookback_hours), moment + timedelta(hours=self.horizon_hours))
            events = parse_calendar_payload(payload, retrieved_at=moment, provider=self.provider)
        except NewsError as error:
            self._fail(type(error).__name__, str(error), moment)
            return self.snapshot
        except Exception as error:  # an unexpected failure is still a failure of the provider
            logger.exception("news_calendar_refresh_failed error=%s", type(error).__name__)
            self._fail(type(error).__name__, f"the calendar could not be read ({type(error).__name__})", moment)
            return self.snapshot
        self.snapshot.events = events
        self.snapshot.retrieved_at = self.snapshot.last_success_at = moment
        self.snapshot.healthy, self.snapshot.state, self.snapshot.last_error = True, STATE_FRESH, None
        self.snapshot.detail = f"{len(events)} calendar event(s) fetched from the {self.provider} API"
        self.cache.save(events, retrieved_at=moment, last_success_at=moment, healthy=True, detail=self.snapshot.detail)
        logger.info("news_calendar_refreshed provider=%s events=%s high_impact_events=%s window_minutes=%s", self.provider, len(events), self._high_impact(), self.window_minutes)
        return self.snapshot

    def _fail(self, kind: str, detail: str, moment: datetime) -> None:
        auth = kind == CalendarAuthError.__name__
        self.snapshot.healthy = False
        self.snapshot.state = STATE_NO_CREDENTIAL if auth else STATE_UNAVAILABLE
        self.snapshot.last_error = f"{kind}: {detail}"
        self.snapshot.detail = f"the {self.provider} calendar is unusable: {detail}"
        self.cache.save(self.snapshot.events, retrieved_at=self.snapshot.retrieved_at, last_success_at=self.snapshot.last_success_at, healthy=False, detail=self.snapshot.detail)
        if auth:
            logger.error("news_calendar_unauthorized provider=%s kind=%s", self.provider, kind)
        else:
            logger.error("news_calendar_unavailable provider=%s kind=%s detail=%s", self.provider, kind, detail)

    def _high_impact(self) -> int:
        return sum(1 for event in self.snapshot.events if event.high_impact)

    # ------------------------------------------------------------------ gate

    @property
    def available(self) -> bool:
        return self.snapshot.healthy

    def data_age_seconds(self, now: datetime | None = None) -> float | None:
        moment = as_utc(now) or self.clock()
        reference = as_utc(self.snapshot.last_success_at) or as_utc(self.snapshot.retrieved_at)
        return None if reference is None else (moment - reference).total_seconds()

    def state(self, now: datetime | None = None) -> str:
        if not self.snapshot.healthy:
            return self.snapshot.state
        age = self.data_age_seconds(now)
        return STATE_STALE if (age is None or age > self.max_age_seconds) else STATE_FRESH

    def status(self, now: datetime | None = None) -> dict:
        moment = as_utc(now) or self.clock()
        return {
            "provider": self.provider,
            "configured": bool(self.client.configured),
            "available": self.available,
            "healthy": self.snapshot.healthy,
            "state": self.state(moment),
            "detail": self.snapshot.detail,
            "last_error": self.snapshot.last_error,
            "last_attempt_at": isoformat(self.snapshot.last_attempt_at),
            "last_success_at": isoformat(self.snapshot.last_success_at),
            "retrieved_at": isoformat(self.snapshot.retrieved_at),
            "data_age_seconds": None if self.data_age_seconds(moment) is None else round(self.data_age_seconds(moment), 1),
            "max_age_seconds": self.max_age_seconds,
            "refresh_seconds": self.refresh_seconds,
            "window_minutes": self.window_minutes,
            "events": len(self.snapshot.events),
            "high_impact_events": self._high_impact(),
            "credential_env_var": "TRADING_ECONOMICS_API_KEY" if not self.client.configured else None,
        }

    def decision(self, symbol: str, now: datetime | None = None) -> NewsDecision:
        moment = as_utc(now) or self.clock()
        self.refresh(moment)
        if not self.snapshot.healthy:
            reason = f"{self.snapshot.detail} (news_gate=FAIL)"
            if self.fail_closed:
                return NewsDecision(False, False, f"news protection is required and cannot be enforced: {reason}")
            return NewsDecision(True, False, f"the calendar provider is unusable ({reason}); the policy does not require news protection, so no window is applied")
        age = self.data_age_seconds(moment)
        if age is None or age > self.max_age_seconds:
            reason = f"the calendar is stale ({'age unknown' if age is None else f'{age:.0f}s old, limit {self.max_age_seconds}s'}) (news_gate=FAIL)"
            if self.fail_closed:
                return NewsDecision(False, True, f"news protection is required and cannot be enforced: {reason}")
            return NewsDecision(True, True, f"the calendar is stale ({reason}); the policy does not require news protection, so no window is applied")
        currencies = pair_currencies(symbol)
        if currencies is None:
            reason = f"the currencies of {symbol} cannot be mapped to an ISO pair, so its news window cannot be evaluated (news_gate=FAIL)"
            if self.fail_closed:
                return NewsDecision(False, True, f"news protection is required and cannot be enforced: {reason}")
            return NewsDecision(True, True, reason)
        window = timedelta(minutes=self.window_minutes)
        blocking = [
            event for event in self.snapshot.events
            if event.high_impact and event.currency in currencies and abs(event.when - moment) <= window
        ]
        if blocking:
            labels = [event.label() for event in blocking]
            return NewsDecision(False, True, f"high impact news inside the +/-{self.window_minutes} minute window: {'; '.join(labels)}", labels)
        return NewsDecision(True, True, f"calendar {self.state(moment)} ({age:.0f}s old), no high impact news for {currencies} inside the +/-{self.window_minutes} minute window")


def build_news_provider(settings, cache: NewsCache | None = None) -> NewsProvider:
    """Configured provider, or an honest 'unavailable' one when nothing is set up."""
    provider = str(getattr(settings, "news_provider", "") or "static").strip().lower()
    if provider == TRADING_ECONOMICS:
        client = TradingEconomicsCalendarClient(
            getattr(settings, "trading_economics_api_key", None),
            base_url=getattr(settings, "trading_economics_base_url", TRADING_ECONOMICS_BASE_URL),
            timeout=float(getattr(settings, "news_provider_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        )
        return TradingEconomicsCalendarProvider(
            client,
            fail_closed=settings.news_fail_closed,
            window_minutes=settings.news_window_minutes,
            refresh_seconds=int(getattr(settings, "news_refresh_seconds", 300)),
            max_age_seconds=int(getattr(settings, "news_max_age_seconds", 900)),
            lookback_hours=int(getattr(settings, "news_lookback_hours", 12)),
            horizon_hours=int(getattr(settings, "news_horizon_hours", 168)),
            cache=cache,
        )
    if settings.news_events_file:
        return StaticNewsProvider.from_file(settings.news_events_file, fail_closed=settings.news_fail_closed, window_minutes=settings.news_window_minutes)
    return UnavailableNewsProvider(settings.news_fail_closed)
