"""The MT5 native economic-calendar provider: no paid API, no credential, one local file.

MetaTrader 5 already downloads the MetaQuotes economic calendar, and the MQL5 ``Calendar*`` family
exposes it to programs. This deployment therefore does not buy a calendar: an MQL5 program
(``mql5/XdigitexCalendarBridge.mq5``, installed in the terminal's ``MQL5\\Experts`` folder and
started by the terminal itself) reads that calendar for the eight currencies this bot trades and
writes one JSON file — the *bridge* — into the MT5 Common Files folder, which is shared between the
terminal and this engine:

    <APPDATA>\\MetaQuotes\\Terminal\\Common\\Files\\xdigitex_calendar.json

This module is the read-only Python half. It never writes the bridge: the MQL5 side owns the file
and replaces it atomically (temp file -> flush -> close -> rename), so a partially written file can
never be observed here.

Everything that could make the calendar *unusable* is reported as a failure of the provider, and
``NEWS_FAIL_CLOSED`` then refuses every new entry:

- the file is missing, unreadable, or not valid JSON;
- the heartbeat is missing or unparseable, older than ``max_age_seconds`` (default 300 s) or in the
  future;
- the heartbeat disagrees with this machine's clock by more than ``clock_tolerance_seconds``, which
  is what a wrong terminal timezone looks like from here;
- ``bridge_status`` is anything other than ``OK``;
- the bridge's own ``event_count`` does not match the number of exported rows (a truncated write);
- the bridge exported no events at all;
- the server-vs-UTC offset is missing, absurd, or not a whole number of minutes;
- a row's server timestamp and its derived UTC instant disagree;
- a row carries an importance the app cannot map to low/medium/high.

Time normalisation is the subtle part and is done explicitly. MQL5 documents that every calendar
timestamp is in **trade-server time**, not UTC. The bridge therefore exports each row's raw server
time (``scheduled_time_server``), the offset it measured itself in the terminal
(``server_utc_offset_seconds`` = ``TimeTradeServer() - TimeGMT()``, rounded to the minute) and the
UTC instant derived from those two (``scheduled_time_utc``). This module recomputes
``server - offset`` and refuses the row (and the calendar) if it does not agree with the exported
UTC instant, so a bridge exporting times without a correct offset fails closed instead of blocking
— or failing to block — the wrong window. Nothing here assumes the server is UTC, and nothing
assumes a fixed +2/+3.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import logging
import os

from app.core.clock import as_utc, isoformat, utcnow
from app.news.provider import (
    HIGH_IMPACT,
    LOW_IMPACT,
    MEDIUM_IMPACT,
    CalendarMalformed,
    CalendarUnavailable,
    NewsCache,
    NewsDecision,
    NewsError,
    NewsEvent,
    NewsProvider,
    NullNewsCache,
    STATE_FRESH,
    STATE_NOT_ATTEMPTED,
    STATE_STALE,
    STATE_UNAVAILABLE,
    pair_currencies,
)

logger = logging.getLogger(__name__)

MT5_CALENDAR = "mt5_calendar"
BRIDGE_FILENAME = "xdigitex_calendar.json"
BRIDGE_STATUS_OK = "OK"

# The eight currencies this deployment trades (every symbol in SYMBOLS is built from two of them).
# A row in any other currency may be logged but can never block a pair.
TRADED_CURRENCIES = frozenset("USD EUR GBP JPY CHF AUD CAD NZD".split())

# The bridge normalises MQL5's ENUM_CALENDAR_EVENT_IMPORTANCE into these words; the raw enum name
# and its numeric value travel alongside, and both are accepted here, so a build whose vocabulary
# changes is still read correctly rather than being silently guessed at.
IMPACT_BY_NAME = {
    "high": HIGH_IMPACT,
    "medium": MEDIUM_IMPACT,
    "moderate": MEDIUM_IMPACT,
    "low": LOW_IMPACT,
    "none": LOW_IMPACT,
}
IMPACT_BY_MQL5_NAME = {
    "CALENDAR_IMPORTANCE_HIGH": HIGH_IMPACT,
    "CALENDAR_IMPORTANCE_MODERATE": MEDIUM_IMPACT,
    "CALENDAR_IMPORTANCE_LOW": LOW_IMPACT,
    "CALENDAR_IMPORTANCE_NONE": LOW_IMPACT,
}
IMPACT_BY_CODE = {3: HIGH_IMPACT, 2: MEDIUM_IMPACT, 1: LOW_IMPACT, 0: LOW_IMPACT}

# No real broker is more than 14 hours from UTC, and no real trade server has a sub-minute offset.
MAX_OFFSET_SECONDS = 14 * 3600

DEFAULT_MAX_AGE_SECONDS = 300
DEFAULT_READ_SECONDS = 30
DEFAULT_FAILURE_RETRY_SECONDS = 15
DEFAULT_CLOCK_TOLERANCE_SECONDS = 300


class BridgeStale(CalendarUnavailable):
    """The heartbeat exists but is too old (or dated in the future): the calendar must not be used."""


def default_bridge_path() -> Path:
    """The MT5 Common Files path the MQL5 bridge writes to, as this machine reports it."""
    root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(root) / "MetaQuotes" / "Terminal" / "Common" / "Files" / BRIDGE_FILENAME


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _field(item: dict, *names: str) -> object:
    for name in names:
        if name in item and item[name] not in (None, ""):
            return item[name]
    return None


def _number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalendarMalformed(f"the bridge field {name} is {type(value).__name__} instead of a number")
    return float(value)


def importance_of(row: dict) -> str | None:
    """The app's low/medium/high for one bridge row, or None when the row uses an unknown level."""
    name = _text(_field(row, "importance", "impact")).lower()
    if name in IMPACT_BY_NAME:
        return IMPACT_BY_NAME[name]
    raw = _text(_field(row, "importance_mql5")).upper()
    if raw in IMPACT_BY_MQL5_NAME:
        return IMPACT_BY_MQL5_NAME[raw]
    code = _field(row, "importance_code")
    if isinstance(code, int) and not isinstance(code, bool):
        return IMPACT_BY_CODE.get(code)
    # The numeric impact_type MQL5 reports on the value itself, as a last resort.
    impact = _field(row, "impact_type_code")
    if isinstance(impact, int) and not isinstance(impact, bool):
        return IMPACT_BY_CODE.get(impact)
    return None


def _parse_instant(value: object, *, name: str) -> datetime:
    text = _text(value)
    if not text:
        raise CalendarMalformed(f"the bridge field {name} is missing")
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise CalendarMalformed(f"the bridge field {name} is not an ISO-8601 timestamp") from error
    return as_utc(moment)


@dataclass(frozen=True)
class BridgeReading:
    """One successfully validated bridge file, with the evidence the gate reports back."""

    events: list[NewsEvent]
    generated_at: datetime
    server_time: datetime | None
    offset_seconds: int
    bridge_status: str
    event_count: int
    high_impact_events: int
    age_seconds: float
    clock_skew_seconds: float | None
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "events": len(self.events),
            "high_impact_events": self.high_impact_events,
            "generated_at": isoformat(self.generated_at),
            "terminal_server_time": isoformat(self.server_time),
            "server_utc_offset_seconds": self.offset_seconds,
            "bridge_status": self.bridge_status,
            "event_count": self.event_count,
            "age_seconds": self.age_seconds,
            "clock_skew_seconds": self.clock_skew_seconds,
        }


def parse_bridge(
    payload: object,
    *,
    now: datetime,
    max_age_seconds: int,
    clock_tolerance_seconds: int = DEFAULT_CLOCK_TOLERANCE_SECONDS,
    file_mtime: float | None = None,
    currencies: frozenset[str] = TRADED_CURRENCIES,
    provider: str = MT5_CALENDAR,
    source: str = "",
) -> BridgeReading:
    """Validate a bridge payload and normalise it into the app's own news events.

    Structural damage raises ``CalendarMalformed``; a heartbeat that exists but may not be used
    raises ``BridgeStale``; anything else that makes the bridge unusable raises
    ``CalendarUnavailable``. In every case the caller treats the calendar as unusable.
    """
    moment = as_utc(now) or utcnow()
    if not isinstance(payload, dict):
        raise CalendarMalformed(f"the bridge payload is {type(payload).__name__} instead of an object")

    status = _text(_field(payload, "bridge_status"))
    if status.upper() != BRIDGE_STATUS_OK:
        detail = _text(_field(payload, "last_error"))
        raise CalendarUnavailable(
            f"the bridge reports bridge_status={status or '(missing)'}"
            + (f": {detail}" if detail else "")
        )

    generated_at = _parse_instant(_field(payload, "generated_at"), name="generated_at")
    age = (moment - generated_at).total_seconds()
    if age > max_age_seconds:
        raise BridgeStale(f"the bridge heartbeat is {age:.0f}s old, older than the {max_age_seconds}s limit")
    if age < -clock_tolerance_seconds:
        raise BridgeStale(
            f"the bridge heartbeat is {-age:.0f}s in the future, so the bridge clock (and therefore "
            f"its server offset) disagrees with this machine's clock"
        )

    # The heartbeat is the terminal's own UTC estimate (TimeGMT()). Comparing it with the file's
    # modification time — written by the same machine, with the same clock as this process — is
    # what catches a terminal whose timezone, and therefore whose server offset, is wrong.
    skew = None
    if file_mtime is not None:
        skew = abs((moment.timestamp() - file_mtime) - age)
        if skew > clock_tolerance_seconds:
            raise CalendarUnavailable(
                f"the bridge clock disagrees with this machine's clock by {skew:.0f}s "
                f"(tolerance {clock_tolerance_seconds}s), so its server offset cannot be trusted"
            )

    offset = _field(payload, "server_utc_offset_seconds")
    offset_seconds = int(_number(offset, name="server_utc_offset_seconds"))
    if abs(offset_seconds) > MAX_OFFSET_SECONDS or offset_seconds % 60 != 0:
        raise CalendarMalformed(f"the bridge server_utc_offset_seconds={offset_seconds} is not a sane whole-minute offset")

    server_time = None
    if _field(payload, "terminal_server_time") is not None:
        server_time = _parse_instant(_field(payload, "terminal_server_time"), name="terminal_server_time")

    rows = _field(payload, "events")
    if rows is None:
        raise CalendarMalformed("the bridge payload carries no events list")
    if not isinstance(rows, list):
        raise CalendarMalformed(f"the bridge events field is {type(rows).__name__} instead of a list")

    declared = _field(payload, "event_count")
    if declared is None:
        raise CalendarMalformed("the bridge payload carries no event_count")
    declared_count = int(_number(declared, name="event_count"))
    if declared_count != len(rows):
        raise CalendarMalformed(
            f"the bridge declares {declared_count} event(s) but exports {len(rows)}: the file is not self-consistent"
        )
    if declared_count == 0:
        raise CalendarUnavailable(
            "the bridge exported no calendar event(s) at all, which is not a calendar that can prove a window is clear"
        )

    events: list[NewsEvent] = []
    skipped = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CalendarMalformed(f"bridge row {index} is {type(row).__name__} instead of an object")

        currency = _text(_field(row, "currency")).upper()
        if currency not in currencies:
            # Defence in depth: the bridge already filters, but a row outside the traded currencies
            # must never be able to block a pair.
            skipped += 1
            continue

        impact = importance_of(row)
        if impact is None:
            raise CalendarMalformed(
                f"bridge row {index} carries an importance this app cannot map to low/medium/high"
            )

        server_text = _field(row, "scheduled_time_server")
        server_when = _parse_instant(server_text, name=f"row {index} scheduled_time_server")
        # MQL5 reports calendar times in trade-server time, so UTC is the server instant minus the
        # offset the bridge measured. The exported UTC instant is only accepted when it agrees.
        derived = server_when - timedelta(seconds=offset_seconds)
        exported = _field(row, "scheduled_time_utc")
        if exported is None:
            when = derived
        else:
            when = _parse_instant(exported, name=f"row {index} scheduled_time_utc")
            if abs((when - derived).total_seconds()) > 1:
                raise CalendarMalformed(
                    f"bridge row {index} says {isoformat(when)} in UTC but {isoformat(derived)} is what "
                    f"its server time {isoformat(server_when)} minus the {offset_seconds}s offset gives"
                )

        value_id = _field(row, "value_id")
        event_id = _field(row, "event_id")
        if value_id is None and event_id is None:
            raise CalendarMalformed(f"bridge row {index} carries neither value_id nor event_id")
        identifier = f"mt5:{value_id if value_id is not None else f'{event_id}@{isoformat(server_when)}'}"

        events.append(NewsEvent(
            when=when,
            currency=currency,
            impact=impact,
            title=_text(_field(row, "title", "name")) or "",
            event_id=identifier,
            country=_text(_field(row, "country")),
            actual=_text(_field(row, "actual")) or None,
            forecast=_text(_field(row, "forecast")) or None,
            previous=_text(_field(row, "previous")) or None,
            retrieved_at=moment,
            provider=provider,
        ))

    if not events:
        raise CalendarUnavailable(
            "the bridge exported no event in the eight traded currencies, which is not a calendar that can prove a window is clear"
        )
    if skipped:
        logger.debug("mt5_calendar_rows_outside_the_traded_currencies skipped=%s kept=%s", skipped, len(events))

    return BridgeReading(
        events=events,
        generated_at=generated_at,
        server_time=server_time,
        offset_seconds=offset_seconds,
        bridge_status=status,
        event_count=declared_count,
        high_impact_events=sum(1 for event in events if event.high_impact),
        age_seconds=age,
        clock_skew_seconds=skew,
        source=source,
    )


@dataclass
class BridgeSnapshot:
    """What the provider knows about the bridge right now, including why it is unusable."""

    events: list[NewsEvent] = field(default_factory=list)
    generated_at: datetime | None = None
    server_time: datetime | None = None
    offset_seconds: int | None = None
    event_count: int = 0
    high_impact_events: int = 0
    clock_skew_seconds: float | None = None
    healthy: bool = False
    state: str = STATE_NOT_ATTEMPTED
    detail: str = "the MT5 calendar bridge has not been read yet"
    last_error: str | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None


class Mt5CalendarProvider(NewsProvider):
    """Reads the MQL5 bridge file, validates its heartbeat and feeds the existing gate.

    The provider is read-only towards the bridge: the MQL5 side owns it. One read serves every
    symbol of a cycle (``read_seconds`` throttles it), the rows are normalised into the same
    ``NewsEvent`` representation the Trading Economics provider produces, and ``decision()`` answers
    only from a bridge that is fresh, self-consistent and honest about its clock.
    """

    provider = MT5_CALENDAR

    def __init__(
        self,
        path: object = None,
        *,
        fail_closed: bool = True,
        window_minutes: int = 30,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
        read_seconds: int = DEFAULT_READ_SECONDS,
        failure_retry_seconds: int = DEFAULT_FAILURE_RETRY_SECONDS,
        clock_tolerance_seconds: int = DEFAULT_CLOCK_TOLERANCE_SECONDS,
        currencies: frozenset[str] = TRADED_CURRENCIES,
        cache: NewsCache | None = None,
        clock=utcnow,
    ):
        self.path = str(path) if path else str(default_bridge_path())
        self.fail_closed, self.window_minutes = bool(fail_closed), int(window_minutes)
        self.max_age_seconds = max(1, int(max_age_seconds))
        self.read_seconds = max(1, int(read_seconds))
        self.failure_retry_seconds = max(1, min(int(failure_retry_seconds), self.read_seconds))
        self.clock_tolerance_seconds = max(0, int(clock_tolerance_seconds))
        self.currencies = frozenset(currencies)
        self.cache, self.clock = cache or NullNewsCache(), clock
        self.snapshot = BridgeSnapshot()
        self._restore()

    # ------------------------------------------------------------------ cache

    def _restore(self) -> None:
        """A restored calendar is evidence of an earlier read, never of a healthy provider now."""
        cached = self.cache.load()
        if cached is None or not cached.events:
            return
        self.snapshot.events = list(cached.events)
        self.snapshot.event_count = len(cached.events)
        self.snapshot.high_impact_events = sum(1 for event in cached.events if event.high_impact)
        self.snapshot.detail = (
            f"{len(cached.events)} calendar event(s) were restored from the persisted cache; this process has not read the bridge yet"
        )
        logger.info("mt5_calendar_cache_restored events=%s retrieved_at=%s", len(cached.events), isoformat(cached.retrieved_at))

    # ------------------------------------------------------------------ read

    def _due(self, moment: datetime) -> bool:
        last = self.snapshot.last_attempt_at
        if last is None:
            return True
        interval = self.read_seconds if self.snapshot.healthy else self.failure_retry_seconds
        return (moment - as_utc(last)).total_seconds() >= interval

    def _mtime(self) -> float | None:
        try:
            return os.stat(self.path).st_mtime
        except OSError:
            return None

    def read(self, now: datetime | None = None) -> BridgeReading:
        """Read and validate the bridge once; raises a ``NewsError`` subclass on any failure."""
        moment = as_utc(now) or self.clock()
        try:
            text = Path(self.path).read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise CalendarUnavailable(f"the bridge file {self.path} does not exist") from error
        except OSError as error:
            raise CalendarUnavailable(f"the bridge file {self.path} could not be read ({type(error).__name__})") from error
        try:
            payload = json.loads(text)
        except ValueError as error:
            raise CalendarMalformed(f"the bridge file {self.path} is not valid JSON ({type(error).__name__})") from error
        return parse_bridge(
            payload,
            now=moment,
            max_age_seconds=self.max_age_seconds,
            clock_tolerance_seconds=self.clock_tolerance_seconds,
            file_mtime=self._mtime(),
            currencies=self.currencies,
            provider=self.provider,
            source=self.path,
        )

    def refresh(self, now: datetime | None = None) -> BridgeSnapshot:
        """Re-read the bridge when it is due; never raises."""
        moment = as_utc(now) or self.clock()
        if not self._due(moment):
            return self.snapshot
        self.snapshot.last_attempt_at = moment
        try:
            reading = self.read(moment)
        except NewsError as error:
            self._fail(type(error).__name__, str(error), moment, stale=isinstance(error, BridgeStale))
            return self.snapshot
        except Exception as error:  # an unexpected failure is still a failure of the provider
            logger.exception("mt5_calendar_read_failed error=%s", type(error).__name__)
            self._fail(type(error).__name__, f"the bridge could not be read ({type(error).__name__})", moment)
            return self.snapshot

        self.snapshot.events = reading.events
        self.snapshot.generated_at = reading.generated_at
        self.snapshot.server_time = reading.server_time
        self.snapshot.offset_seconds = reading.offset_seconds
        self.snapshot.event_count = len(reading.events)
        self.snapshot.high_impact_events = reading.high_impact_events
        self.snapshot.clock_skew_seconds = reading.clock_skew_seconds
        self.snapshot.healthy, self.snapshot.state, self.snapshot.last_error = True, STATE_FRESH, None
        self.snapshot.last_success_at = moment
        self.snapshot.detail = (
            f"{len(reading.events)} event(s), {reading.high_impact_events} high impact, read from the MT5 calendar bridge "
            f"(server offset {reading.offset_seconds}s, heartbeat {reading.age_seconds:.0f}s old)"
        )
        self.cache.save(reading.events, retrieved_at=moment, last_success_at=moment, healthy=True, detail=self.snapshot.detail)
        logger.info(
            "mt5_calendar_bridge_read events=%s high_impact_events=%s heartbeat_age_seconds=%.0f server_offset_seconds=%s",
            len(reading.events), reading.high_impact_events, reading.age_seconds, reading.offset_seconds,
        )
        return self.snapshot

    def _fail(self, kind: str, detail: str, moment: datetime, *, stale: bool = False) -> None:
        self.snapshot.healthy = False
        self.snapshot.state = STATE_STALE if stale else STATE_UNAVAILABLE
        self.snapshot.last_error = f"{kind}: {detail}"
        self.snapshot.detail = f"the MT5 calendar bridge is unusable: {detail}"
        self.cache.save(
            self.snapshot.events,
            retrieved_at=self.snapshot.generated_at,
            last_success_at=self.snapshot.last_success_at,
            healthy=False,
            detail=self.snapshot.detail,
        )
        logger.error("mt5_calendar_bridge_unusable kind=%s detail=%s", kind, detail)

    # ------------------------------------------------------------------ gate

    @property
    def available(self) -> bool:
        return self.snapshot.healthy

    def data_age_seconds(self, now: datetime | None = None) -> float | None:
        moment = as_utc(now) or self.clock()
        reference = as_utc(self.snapshot.generated_at)
        return None if reference is None else (moment - reference).total_seconds()

    def state(self, now: datetime | None = None) -> str:
        if not self.snapshot.healthy:
            return self.snapshot.state
        age = self.data_age_seconds(now)
        return STATE_STALE if (age is None or age > self.max_age_seconds) else STATE_FRESH

    def status(self, now: datetime | None = None) -> dict:
        moment = as_utc(now) or self.clock()
        age = self.data_age_seconds(moment)
        return {
            "provider": self.provider,
            "available": self.available,
            "healthy": self.snapshot.healthy,
            "state": self.state(moment),
            "detail": self.snapshot.detail,
            "last_error": self.snapshot.last_error,
            "last_attempt_at": isoformat(self.snapshot.last_attempt_at),
            "last_success_at": isoformat(self.snapshot.last_success_at),
            "generated_at": isoformat(self.snapshot.generated_at),
            "terminal_server_time": isoformat(self.snapshot.server_time),
            "server_utc_offset_seconds": self.snapshot.offset_seconds,
            "clock_skew_seconds": None if self.snapshot.clock_skew_seconds is None else round(self.snapshot.clock_skew_seconds, 1),
            "data_age_seconds": None if age is None else round(age, 1),
            "max_age_seconds": self.max_age_seconds,
            "read_seconds": self.read_seconds,
            "window_minutes": self.window_minutes,
            "bridge_file": self.path,
            "bridge_file_exists": os.path.exists(self.path),
            "events": len(self.snapshot.events),
            "high_impact_events": self.snapshot.high_impact_events,
            "currencies": sorted(self.currencies),
            # This provider needs no credential and no paid API; the Trading Economics key is not
            # required for it to work.
            "requires_credential": False,
            "paid_api": False,
        }

    def decision(self, symbol: str, now: datetime | None = None) -> NewsDecision:
        moment = as_utc(now) or self.clock()
        self.refresh(moment)
        if not self.snapshot.healthy:
            reason = f"{self.snapshot.detail} (news_gate=FAIL)"
            if self.fail_closed:
                return NewsDecision(False, False, f"news protection is required and cannot be enforced: {reason}")
            return NewsDecision(True, False, f"the MT5 calendar bridge is unusable ({reason}); the policy does not require news protection, so no window is applied")
        age = self.data_age_seconds(moment)
        if age is None or age > self.max_age_seconds:
            reason = f"the MT5 calendar bridge is stale ({'age unknown' if age is None else f'{age:.0f}s old, limit {self.max_age_seconds}s'}) (news_gate=FAIL)"
            if self.fail_closed:
                return NewsDecision(False, True, f"news protection is required and cannot be enforced: {reason}")
            return NewsDecision(True, True, f"the MT5 calendar bridge is stale ({reason}); the policy does not require news protection, so no window is applied")
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
        return NewsDecision(
            True, True,
            f"MT5 calendar bridge {self.state(moment)} ({age:.0f}s old, server offset {self.snapshot.offset_seconds}s), "
            f"no high impact news for {currencies} inside the +/-{self.window_minutes} minute window",
        )
