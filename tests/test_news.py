"""The calendar provider, its normalisation, its cache and the fail-closed gate.

Nothing here contacts a real provider: the official endpoint is replaced by a transport stand-in,
so the suite proves the parsing and the policy without a credential and without a network.
"""
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select

import pytest

from app.database.base import NewsEventRecord, NewsProviderStateRecord
from app.news.provider import (
    HIGH_IMPACT,
    LOW_IMPACT,
    MEDIUM_IMPACT,
    CalendarMalformed,
    SqlNewsCache,
    StaticNewsProvider,
    STATE_NOT_ATTEMPTED,
    TradingEconomicsCalendarClient,
    TradingEconomicsCalendarProvider,
    NewsEvent,
    STATE_FRESH,
    STATE_NO_CREDENTIAL,
    STATE_STALE,
    STATE_UNAVAILABLE,
    build_news_provider,
    currencies_of,
    normalise_impact,
    pair_currencies,
    parse_calendar_payload,
    parse_timestamp,
)
from app.core.config import Settings
from app.services.scheduler import MarketScheduler
from conftest import FakePosition, market_gateway

from tests.test_scheduler import process_cycle, trading_settings

PROVIDER = "trading_economics"
KEY = "unit-test-credential-value"


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: object = None, body_is_json: bool = True):
        self.status_code, self._payload, self._json = status_code, payload, body_is_json

    def json(self):
        if not self._json:
            raise ValueError("not json")
        return self._payload


class FakeTransport:
    """httpx.Client stand-in: records every call and replays queued answers."""

    def __init__(self, answers: list):
        self.answers, self.calls = list(answers), []
        self.last = FakeResponse(200, [])

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        answer = self.answers.pop(0) if self.answers else self.last
        if isinstance(answer, Exception):
            raise answer
        self.last = answer
        return answer


def row(event_id="1", event="Non-Farm Payrolls", country="United States", currency="USD", date=None, importance=3, actual="250K", forecast="200K", previous="180K"):
    return {
        "CalendarId": event_id, "Event": event, "Country": country, "Currency": currency,
        "Date": date, "Importance": importance, "Actual": actual, "Forecast": forecast,
        "Previous": previous, "Source": "Trading Economics", "URL": "/united-states/non-farm-payrolls",
    }


def clock_of(instant: datetime):
    """A clock the test controls, so calendar age is measured rather than waited for."""
    state = {"now": instant}

    def clock():
        return state["now"]

    return clock, state


def provider_with(answers: list, *, now: datetime, **kwargs) -> tuple[TradingEconomicsCalendarProvider, FakeTransport]:
    transport = FakeTransport(answers)
    client = TradingEconomicsCalendarClient(KEY, transport=transport)
    kwargs.setdefault("refresh_seconds", 300)
    kwargs.setdefault("max_age_seconds", 900)
    return TradingEconomicsCalendarProvider(client, clock=clock_of(now)[0], **kwargs), transport


# --------------------------------------------------------------------------- parsing


def test_the_calendar_payload_is_normalised_into_the_apps_own_representation():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    events = parse_calendar_payload([row(date="2026-05-01T12:30:00")], retrieved_at=now)

    assert len(events) == 1
    event = events[0]
    assert (event.event_id, event.title, event.country, event.currency) == ("1", "Non-Farm Payrolls", "United States", "USD")
    assert event.when == datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    assert (event.impact, event.actual, event.forecast, event.previous) == (HIGH_IMPACT, "250K", "200K", "180K")
    assert event.retrieved_at == now and event.provider == PROVIDER
    assert event.as_dict()["timestamp"] == "2026-05-01T12:30:00+00:00"


def test_importance_is_normalised_from_numbers_and_words():
    for value, expected in ((1, LOW_IMPACT), ("2", MEDIUM_IMPACT), (3, HIGH_IMPACT), ("low", LOW_IMPACT),
                            ("Medium", MEDIUM_IMPACT), ("HIGH", HIGH_IMPACT)):
        assert normalise_impact(value) == expected
    assert normalise_impact(None) is None and normalise_impact("urgent") is None


def test_a_raw_iso_timestamp_is_read_as_utc_and_a_broken_one_is_rejected():
    assert parse_timestamp("2026-05-01T12:30:00") == datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    assert parse_timestamp("2026-05-01T12:30:00Z") == datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    assert parse_timestamp("2026-05-01T14:30:00+02:00") == datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    for broken in (None, "", "yesterday", "01/05/2026 12:30"):
        assert parse_timestamp(broken) is None


def test_a_payload_that_is_not_a_list_or_a_row_that_is_not_an_object_is_malformed():
    now = datetime.now(timezone.utc)
    with pytest.raises(CalendarMalformed):
        parse_calendar_payload({"events": []}, retrieved_at=now)
    with pytest.raises(CalendarMalformed):
        parse_calendar_payload(["not a row"], retrieved_at=now)


def test_an_invalid_timestamp_makes_the_whole_calendar_unusable():
    with pytest.raises(CalendarMalformed):
        parse_calendar_payload([row(date="not-a-date")], retrieved_at=datetime.now(timezone.utc))


def test_an_unrecognised_importance_makes_the_calendar_unusable():
    with pytest.raises(CalendarMalformed):
        parse_calendar_payload([row(date="2026-05-01T12:30:00", importance="critical")], retrieved_at=datetime.now(timezone.utc))


def test_a_currency_is_taken_from_the_row_or_derived_from_its_country():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    mapped = parse_calendar_payload([row(date="2026-05-01T12:30:00", currency="", country="Germany", importance=2)], retrieved_at=now)
    assert mapped[0].currency == "EUR"


def test_a_high_impact_row_that_cannot_be_attributed_makes_the_calendar_unusable():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    # A high-impact row with no currency is exactly the row that could have been protection.
    with pytest.raises(CalendarMalformed):
        parse_calendar_payload([row(date="2026-05-01T12:30:00", currency="", country="Atlantis", importance=3)], retrieved_at=now)
    # A medium-impact row of the same shape is dropped instead: it can never block anything.
    kept = parse_calendar_payload([row(date="2026-05-01T12:30:00", currency="", country="Atlantis", importance=2)], retrieved_at=now)
    assert kept == []


# --------------------------------------------------------------------------- currency mapping


def test_events_are_mapped_to_the_pairs_that_hold_their_currency():
    for symbol, expected in (("EURUSDm", ("EUR", "USD")), ("GBPUSDm", ("GBP", "USD")), ("USDJPYm", ("USD", "JPY")),
                             ("EURGBPm", ("EUR", "GBP")), ("EURUSD", ("EUR", "USD"))):
        assert pair_currencies(symbol) == expected, symbol
    assert pair_currencies("XAUUSDm") is None  # a metal, not a currency pair
    assert pair_currencies("USTECm") is None
    assert pair_currencies("") is None


def test_an_unrelated_currency_never_blocks_a_pair():
    now = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    provider, _ = provider_with([FakeResponse(200, [row(date="2026-05-01T12:30:00", currency="JPY", country="Japan")])], now=now)

    # A BoJ release moves JPY pairs only: the dollar and sterling crosses are untouched by it.
    assert provider.decision("EURUSDm", now).allowed is True
    assert provider.decision("EURGBPm", now).allowed is True
    assert provider.decision("GBPJPYm", now).allowed is False  # JPY is one half of this pair
    assert provider.decision("USDJPYm", now).allowed is False


# --------------------------------------------------------------------------- window


def test_only_high_impact_events_inside_the_window_block():
    now = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    payload = [
        row("a", "CPI", "United States", "USD", "2026-05-01T12:26:00", 3),
        row("b", "Retail sales", "United States", "USD", "2026-05-01T12:34:00", 3),
        row("c", "PMI", "United States", "USD", "2026-05-01T12:30:00", 2),
        row("d", "Holiday", "United States", "USD", "2026-05-01T12:30:00", 1),
        row("e", "Old data", "United States", "USD", "2026-05-01T11:00:00", 3),
    ]
    provider, transport = provider_with([FakeResponse(200, payload)], now=now, window_minutes=5, refresh_seconds=7200)

    blocked = provider.decision("EURUSDm", now)
    assert blocked.allowed is False and "CPI" in blocked.reason and "Retail sales" in blocked.reason
    assert "PMI" not in blocked.reason and "Holiday" not in blocked.reason
    assert "Old data" not in blocked.reason  # outside the +/-5 minute window
    assert len(transport.calls) == 1

    # Once every event has left the window the pair is tradable again, without a re-fetch.
    later = now + timedelta(minutes=12)
    assert provider.decision("EURUSDm", later).allowed is True
    assert len(transport.calls) == 1


def test_the_window_is_measured_from_the_configured_minutes():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    payload = [row(date="2026-05-01T12:35:00", currency="EUR", country="Euro Area")]
    for window, expected in ((30, True), (45, False)):
        provider, _ = provider_with([FakeResponse(200, payload)], now=now, window_minutes=window)
        assert provider.decision("EURUSDm", now).allowed is expected


# --------------------------------------------------------------------------- provider failures


def test_a_missing_credential_fails_closed_and_never_calls_the_provider():
    now = datetime.now(timezone.utc)
    transport = FakeTransport([FakeResponse(200, [])])
    provider = TradingEconomicsCalendarProvider(TradingEconomicsCalendarClient("", transport=transport), clock=clock_of(now)[0])

    decision = provider.decision("EURUSDm", now)
    assert decision.allowed is False and "news_gate=FAIL" in decision.reason
    assert transport.calls == []
    status = provider.status(now)
    assert status["healthy"] is False and status["state"] == STATE_NO_CREDENTIAL
    assert status["credential_env_var"] == "TRADING_ECONOMICS_API_KEY"


def test_a_rejected_credential_fails_closed():
    now = datetime.now(timezone.utc)
    provider, _ = provider_with([FakeResponse(401, {"message": "invalid credentials"})], now=now)

    assert provider.decision("EURUSDm", now).allowed is False
    assert provider.status(now)["state"] == STATE_NO_CREDENTIAL
    assert "rejected" in provider.status(now)["detail"]


def test_a_provider_outage_fails_closed():
    now = datetime.now(timezone.utc)
    provider, _ = provider_with([ConnectionError("no route to host")], now=now)

    decision = provider.decision("EURUSDm", now)
    assert decision.allowed is False and "usable" in decision.reason
    status = provider.status(now)
    assert status["healthy"] is False and status["state"] == STATE_UNAVAILABLE
    assert status["last_error"].startswith("CalendarUnavailable")


def test_a_non_success_status_fails_closed():
    now = datetime.now(timezone.utc)
    for code in (500, 503, 429):
        provider, _ = provider_with([FakeResponse(code, None)], now=now)
        assert provider.decision("EURUSDm", now).allowed is False
        assert provider.status(now)["state"] == STATE_UNAVAILABLE


def test_a_body_that_is_not_json_fails_closed():
    now = datetime.now(timezone.utc)
    provider, _ = provider_with([FakeResponse(200, None, body_is_json=False)], now=now)
    assert provider.decision("EURUSDm", now).allowed is False
    assert provider.status(now)["last_error"].startswith("CalendarMalformed")


def test_a_calendar_older_than_the_allowed_age_is_rejected():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    clock, state = clock_of(now)
    transport = FakeTransport([FakeResponse(200, [row(date="2026-05-01T18:00:00")])])
    provider = TradingEconomicsCalendarProvider(
        TradingEconomicsCalendarClient(KEY, transport=transport), clock=clock,
        refresh_seconds=86_400, max_age_seconds=600, window_minutes=30,
    )

    assert provider.decision("EURUSDm", now).allowed is True
    assert provider.status(now)["state"] == STATE_FRESH

    state["now"] = now + timedelta(minutes=30)  # inside the refresh interval, past the age limit
    status = provider.status(state["now"])
    assert status["state"] == STATE_STALE and status["data_age_seconds"] == pytest.approx(1800.0)
    decision = provider.decision("EURUSDm", state["now"])
    assert decision.allowed is False and "stale" in decision.reason

    # The very next fetch, once the refresh interval allows it, makes it fresh again.
    state["now"] = now + timedelta(hours=25)
    assert provider.decision("EURUSDm", state["now"]).allowed is True
    assert len(transport.calls) == 2


def test_the_provider_is_not_called_once_per_symbol_per_cycle():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    provider, transport = provider_with([FakeResponse(200, [row(date="2026-05-01T18:00:00")])], now=now)

    symbols = ["EURUSDm", "GBPUSDm", "USDJPYm", "USDCHFm", "AUDUSDm", "USDCADm", "NZDUSDm", "EURJPYm", "GBPJPYm", "EURGBPm"]
    assert all(provider.decision(symbol, now).allowed for symbol in symbols)
    assert len(transport.calls) == 1
    assert transport.calls[0]["url"].endswith("/calendar/from/2026-05-01/to/2026-05-08")
    assert transport.calls[0]["params"]["c"] == KEY


def test_a_failed_provider_is_retried_sooner_than_a_healthy_one():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    clock, state = clock_of(now)
    transport = FakeTransport([ConnectionError("down"), FakeResponse(200, [row(date="2026-05-01T18:00:00")])])
    provider = TradingEconomicsCalendarProvider(
        TradingEconomicsCalendarClient(KEY, transport=transport), clock=clock,
        refresh_seconds=3600, failure_retry_seconds=60, max_age_seconds=900,
    )

    assert provider.decision("EURUSDm", now).allowed is False
    state["now"] = now + timedelta(seconds=30)
    assert provider.decision("EURUSDm", state["now"]).allowed is False
    assert len(transport.calls) == 1  # inside the failure retry interval
    state["now"] = now + timedelta(seconds=61)
    assert provider.decision("EURUSDm", state["now"]).allowed is True
    assert len(transport.calls) == 2


def test_the_credential_is_never_written_to_a_log(caplog):
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    provider, _ = provider_with([FakeResponse(200, [row(date="2026-05-01T18:00:00")]), FakeResponse(401, None)], now=now)

    with caplog.at_level(logging.DEBUG):
        provider.decision("EURUSDm", now)
        provider.refresh(now + timedelta(seconds=400))
        provider.status(now)

    assert KEY not in caplog.text
    assert "c=" not in caplog.text


# --------------------------------------------------------------------------- cache


def test_the_calendar_is_persisted_and_restored_by_age_not_by_optimism(session_factory, db):
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    cache = SqlNewsCache(session_factory)
    first, _ = provider_with([FakeResponse(200, [row(date="2026-05-01T18:00:00", currency="EUR", country="Euro Area")])], now=now, cache=cache)
    assert first.decision("EURUSDm", now).allowed is True

    assert db.scalar(select(NewsProviderStateRecord)).healthy is True
    stored = db.scalar(select(NewsEventRecord))
    assert (stored.provider, stored.event_id, stored.currency, stored.impact) == (PROVIDER, "1", "EUR", HIGH_IMPACT)

    # A restarted process reads the same calendar and its retrieval time, but it has not verified
    # anything itself yet, so the gate is closed until it does.
    restarted, transport = provider_with([], now=now + timedelta(minutes=1), cache=cache)
    transport.last = ConnectionError("not reachable yet")
    status = restarted.status(now + timedelta(minutes=1))
    assert status["events"] == 1 and status["healthy"] is False and status["state"] == STATE_NOT_ATTEMPTED
    assert "restored from the persisted cache" in status["detail"]

    blocked = restarted.decision("EURUSDm", now + timedelta(minutes=1))
    assert blocked.allowed is False and "cannot be enforced" in blocked.reason

    transport.last = FakeResponse(200, [row(date="2026-05-01T18:00:00", currency="EUR", country="Euro Area")])
    assert restarted.decision("EURUSDm", now + timedelta(minutes=3)).allowed is True
    assert len(transport.calls) == 2


def test_the_cache_records_the_provider_health_timestamps(session_factory, db):
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    clock, state = clock_of(now)
    transport = FakeTransport([FakeResponse(200, [])])
    provider = TradingEconomicsCalendarProvider(
        TradingEconomicsCalendarClient(KEY, transport=transport), clock=clock,
        failure_retry_seconds=30, refresh_seconds=30, cache=SqlNewsCache(session_factory),
    )

    provider.refresh(now)
    record = db.scalar(select(NewsProviderStateRecord))
    assert record.healthy is True and record.last_success_at is not None and record.event_count == 0

    state["now"] = now + timedelta(seconds=60)
    transport.answers.append(ConnectionError("gone"))
    provider.refresh(state["now"])
    db.refresh(record)
    assert record.healthy is False and "unusable" in (record.detail or "")


def test_an_unwritable_cache_never_fabricates_or_blocks_a_good_calendar():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    class BrokenCache(SqlNewsCache):
        def __init__(self):
            super().__init__(lambda: (_ for _ in ()).throw(RuntimeError("database is gone")))

    provider, _ = provider_with([FakeResponse(200, [row(date="2026-05-01T18:00:00")])], now=now, cache=BrokenCache())
    assert provider.decision("EURUSDm", now).allowed is True
    assert provider.status(now)["healthy"] is True


def test_a_cache_that_cannot_be_read_is_reported_and_leaves_the_gate_closed():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    def gone():
        raise RuntimeError("database is gone")

    cache = SqlNewsCache(gone)
    provider, _ = provider_with([FakeResponse(200, [row(date="2026-05-01T18:00:00")])], now=now, cache=cache)
    assert provider.status(now)["events"] == 0  # nothing was restored, and nothing was invented
    assert provider.decision("EURUSDm", now).allowed is True  # a live fetch still works


# --------------------------------------------------------------------------- wiring


def test_the_provider_is_selected_from_the_configuration():
    assert isinstance(build_news_provider(Settings(_env_file=None, news_provider="trading_economics")), TradingEconomicsCalendarProvider)
    # Nothing configured but the default policy: an honest "unavailable", not a fake calendar.
    static = build_news_provider(Settings(_env_file=None))
    assert static.available is False
    assert static.decision("EURUSDm").allowed is False
    assert build_news_provider(Settings(_env_file=None, news_fail_closed=False)).decision("EURUSDm").allowed is True


def test_currencies_of_still_reads_broker_native_names():
    assert currencies_of("EURUSDm") == ("EUR", "USD")
    assert currencies_of("USDJPYm") == ("USD", "JPY")


# --------------------------------------------------------------------------- the loop


def scheduler_with_news(settings, gateway, session_factory, provider) -> MarketScheduler:
    return MarketScheduler(settings, gateway, session_factory, news=provider)


def test_the_scheduler_publishes_calendar_health_and_fetches_once_per_cycle(settings, session_factory):
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    gateway = market_gateway(now)
    provider, transport = provider_with([FakeResponse(200, [row(date="2026-05-01T18:00:00")])], now=now)
    scheduler = scheduler_with_news(trading_settings(symbols=["EURUSD", "GBPUSD"]), gateway, session_factory, provider)

    summary = process_cycle(scheduler, now)

    assert summary["news"]["provider"] == PROVIDER and summary["news"]["healthy"] is True
    assert summary["news"]["state"] == STATE_FRESH
    assert summary["news"]["events"] == 1 and summary["news"]["high_impact_events"] == 1
    assert len(transport.calls) == 1  # one fetch served both symbols
    assert summary["executions"] == 2 and len(gateway.requests) == 2


def test_the_scheduler_fails_closed_when_the_provider_is_down(settings, session_factory, db):
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    gateway = market_gateway(now)
    provider, _ = provider_with([ConnectionError("network unavailable")], now=now)
    scheduler = scheduler_with_news(trading_settings(), gateway, session_factory, provider)

    summary = process_cycle(scheduler, now)

    assert gateway.requests == [] and summary["executions"] == 0
    blocked = summary["symbols"]["EURUSD"]["blocked"][0]
    assert "news" in blocked and "news_gate=FAIL" in blocked
    assert summary["news"]["healthy"] is False


def test_a_news_block_never_closes_an_open_position(settings, session_factory, db):
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    position = FakePosition(ticket=4242, symbol="EURUSD", volume=0.01)
    gateway = market_gateway(now, position_list=(position,))
    provider = StaticNewsProvider([NewsEvent(when=now, currency="EUR", impact=HIGH_IMPACT, title="ECB rate decision")], window_minutes=30)
    scheduler = scheduler_with_news(trading_settings(), gateway, session_factory, provider)

    summary = process_cycle(scheduler, now)

    assert gateway.requests == [] and gateway.closed == []
    assert summary["positions"] == {"managed": 1, "by_symbol": {"EURUSD": 1}}
    assert "ECB rate decision" in summary["symbols"]["EURUSD"]["blocked"][0]
