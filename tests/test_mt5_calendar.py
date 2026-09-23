"""The MT5 native-calendar bridge: its JSON contract, its heartbeat and the gate it feeds.

Nothing here needs a terminal or a credential: the MQL5 bridge is modelled by the very JSON it
writes (``xdigitex_calendar.json``), so these tests prove the Python half — parsing, currency and
importance normalisation, the server-to-UTC conversion, heartbeat freshness and every fail-closed
path — without a broker connection and without a single order.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.database.base import NewsEventRecord, NewsProviderStateRecord
from app.news.mt5_calendar import (
    BRIDGE_FILENAME,
    BridgeStale,
    MT5_CALENDAR,
    Mt5CalendarProvider,
    TRADED_CURRENCIES,
    default_bridge_path,
    importance_of,
    parse_bridge,
)
from app.news.provider import (
    HIGH_IMPACT,
    LOW_IMPACT,
    MEDIUM_IMPACT,
    CalendarMalformed,
    CalendarUnavailable,
    SqlNewsCache,
    STATE_FRESH,
    STATE_NOT_ATTEMPTED,
    STATE_STALE,
    STATE_UNAVAILABLE,
    build_news_provider,
)
from app.services.scheduler import MarketScheduler
from conftest import market_gateway

from tests.test_scheduler import process_cycle, trading_settings

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
# This broker's trade server runs three hours ahead of UTC, which is what the bridge measures.
OFFSET = 10800
BRIDGE = BRIDGE_FILENAME


def iso(moment: datetime) -> str:
    return moment.isoformat()


def event(
    *,
    value_id: int = 900001,
    event_id: int = 840030001,
    name: str = "Non-Farm Payrolls",
    currency: str = "USD",
    country: str = "United States",
    server_time: datetime | None = None,
    importance: str = "high",
    importance_mql5: str = "CALENDAR_IMPORTANCE_HIGH",
    importance_code: int = 3,
    actual: str | None = None,
    forecast: str | None = "200K",
    previous: str | None = "180K",
    offset: int = OFFSET,
) -> dict:
    """One bridge row exactly as the MQL5 program writes it (server time + measured offset)."""
    moment = server_time or datetime(2026, 5, 11, 15, 0)
    utc_moment = (moment - timedelta(seconds=offset)).replace(tzinfo=timezone.utc)
    return {
        "event_id": event_id,
        "value_id": value_id,
        "event_code": "nonfarm-payrolls",
        "name": name,
        "title": name,
        "currency": currency,
        "country": country,
        "country_code": "US",
        "scheduled_time": utc_moment.isoformat(),
        "scheduled_time_server": moment.isoformat(),
        "scheduled_time_utc": utc_moment.isoformat(),
        "importance": importance,
        "importance_mql5": importance_mql5,
        "importance_code": importance_code,
        "impact_type_code": importance_code,
        "actual": actual,
        "actual_raw": None,
        "forecast": forecast,
        "forecast_raw": 20000000,
        "previous": previous,
        "previous_raw": 18000000,
        "revision": 0,
        "unit": "CALENDAR_UNIT_NONE",
        "multiplier": "CALENDAR_MULTIPLIER_NONE",
        "source": "MetaTrader 5 native economic calendar (MQL5 CalendarValueHistory)",
        "source_url": "",
        "last_updated": iso(NOW),
    }


def payload(
    events: list[dict] | None = None,
    *,
    generated_at: datetime | None = None,
    status: str = "OK",
    offset: int = OFFSET,
    **overrides,
) -> dict:
    """A whole bridge file as the MQL5 program writes it, heartbeat included."""
    moment = generated_at or NOW
    rows = [event(offset=offset)] if events is None else events
    document = {
        "bridge": "XdigitexCalendarBridge",
        "bridge_version": "1.00",
        "bridge_status": status,
        "generated_at": iso(moment),
        "generated_at_source": "TimeGMT()",
        "terminal_server_time": (moment + timedelta(seconds=offset)).isoformat(),
        "terminal_server_time_utc": iso(moment),
        "server_utc_offset_seconds": offset,
        "terminal_build": 6182,
        "terminal_company": "Exness (KE) Limited",
        "account_server": "ExnessKE-MT5Real9",
        "connected": True,
        "currencies": sorted(TRADED_CURRENCIES),
        "refresh_seconds": 60,
        "cycles": 12,
        "failures": 0,
        "change_id": 0,
        "event_count": len(rows),
        "high_impact_event_count": sum(1 for row in rows if row["importance"] == "high"),
        "last_error": None,
        "events": rows,
    }
    document.update(overrides)
    return document


def write_bridge(tmp_path, document: dict | str, *, mtime: datetime | None = None):
    """Write the bridge file, stamping the modification time the heartbeat is compared with."""
    path = tmp_path / BRIDGE
    path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
    stamp = (mtime or NOW).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def provider_for(tmp_path, document, *, now: datetime = NOW, **kwargs) -> tuple[Mt5CalendarProvider, dict]:
    path = write_bridge(tmp_path, document, mtime=now)
    kwargs.setdefault("max_age_seconds", 300)
    kwargs.setdefault("clock_tolerance_seconds", 300)
    state = {"now": now}
    return Mt5CalendarProvider(path, clock=lambda: state["now"], **kwargs), state


# --------------------------------------------------------------------------- the file contract


def test_a_bridge_row_is_normalised_into_the_apps_own_representation(tmp_path):
    path = write_bridge(tmp_path, payload())
    reading = parse_bridge(
        json.loads(path.read_text(encoding="utf-8")), now=NOW, max_age_seconds=300, file_mtime=NOW.timestamp()
    )

    assert reading.bridge_status == "OK" and reading.event_count == 1 and reading.high_impact_events == 1
    row = reading.events[0]
    assert (row.currency, row.impact, row.title, row.country) == ("USD", HIGH_IMPACT, "Non-Farm Payrolls", "United States")
    assert row.event_id == "mt5:900001" and row.provider == MT5_CALENDAR
    assert (row.actual, row.forecast, row.previous) == (None, "200K", "180K")
    assert row.retrieved_at == NOW


def test_the_bridge_path_defaults_to_the_mt5_common_files_folder():
    assert default_bridge_path().name == BRIDGE_FILENAME
    assert "MetaQuotes" in str(default_bridge_path()) and "Common" in str(default_bridge_path())


def test_a_truncated_bridge_is_rejected(tmp_path):
    # A half-written file is exactly what an atomic write prevents; if one ever appears it must not
    # be read as a calendar.
    path = write_bridge(tmp_path, json.dumps(payload())[:200])
    provider = Mt5CalendarProvider(path, clock=lambda: NOW)

    assert provider.decision("EURUSDm", NOW).allowed is False
    assert provider.status(NOW)["state"] == STATE_UNAVAILABLE
    assert "not valid JSON" in provider.status(NOW)["detail"]


def test_a_payload_that_is_not_an_object_is_malformed():
    with pytest.raises(CalendarMalformed):
        parse_bridge([1, 2, 3], now=NOW, max_age_seconds=300)


def test_the_provider_never_writes_the_bridge_file(tmp_path):
    provider, _ = provider_for(tmp_path, payload())
    path = tmp_path / BRIDGE
    before = (path.read_text(encoding="utf-8"), path.stat().st_mtime)

    provider.decision("EURUSDm", NOW + timedelta(seconds=1))
    provider.status(NOW + timedelta(seconds=1))

    assert (path.read_text(encoding="utf-8"), path.stat().st_mtime) == before


# --------------------------------------------------------------------------- heartbeat


def test_a_fresh_heartbeat_is_accepted_and_an_old_one_is_refused(tmp_path):
    document = payload()
    path = write_bridge(tmp_path, document)

    fresh = parse_bridge(json.loads(path.read_text(encoding="utf-8")), now=NOW, max_age_seconds=300, file_mtime=NOW.timestamp())
    assert fresh.age_seconds == 0

    stale = NOW + timedelta(seconds=301)
    with pytest.raises(BridgeStale):
        parse_bridge(json.loads(path.read_text(encoding="utf-8")), now=stale, max_age_seconds=300, file_mtime=NOW.timestamp())


def test_a_bridge_that_stops_beating_fails_the_gate_even_inside_the_read_interval(tmp_path):
    provider, state = provider_for(tmp_path, payload(), read_seconds=3600, failure_retry_seconds=3600)

    # The first read is healthy: the bridge is one second old.
    assert provider.decision("EURUSDm", NOW + timedelta(seconds=1)).allowed is True

    # The bridge stopped writing an hour ago and the provider is not due to re-read it, so the age
    # of the snapshot itself is what refuses the trade. Stale data is never silently used.
    state["now"] = NOW + timedelta(hours=1)
    status = provider.status(state["now"])
    assert status["state"] == STATE_STALE and status["data_age_seconds"] == pytest.approx(3600.0)
    decision = provider.decision("EURUSDm", state["now"])
    assert decision.allowed is False and "stale" in decision.reason and "news_gate=FAIL" in decision.reason


def test_a_heartbeat_dated_in_the_future_is_refused(tmp_path):
    future = NOW + timedelta(hours=2)
    path = write_bridge(tmp_path, payload(generated_at=future))
    with pytest.raises(BridgeStale):
        parse_bridge(json.loads(path.read_text(encoding="utf-8")), now=NOW, max_age_seconds=300, file_mtime=future.timestamp())


def test_a_bridge_clock_that_disagrees_with_this_machine_is_refused(tmp_path):
    # The file was written now, but its heartbeat claims another instant: that is what a terminal
    # running in the wrong timezone looks like, and its server offset cannot be trusted.
    path = write_bridge(tmp_path, payload(generated_at=NOW + timedelta(hours=3)))
    with pytest.raises(CalendarUnavailable) as failure:
        parse_bridge(json.loads(path.read_text(encoding="utf-8")), now=NOW, max_age_seconds=300, file_mtime=NOW.timestamp())
    assert "clock" in str(failure.value)


@pytest.mark.parametrize("bad", [None, "", "yesterday"])
def test_a_missing_or_invalid_heartbeat_is_refused(bad):
    document = payload()
    document["generated_at"] = bad
    with pytest.raises(CalendarMalformed):
        parse_bridge(document, now=NOW, max_age_seconds=300)


# --------------------------------------------------------------------------- failures


@pytest.mark.parametrize("status", ["DEGRADED", "ERROR", "STALE", "", "ok-ish"])
def test_a_bridge_status_other_than_ok_fails_closed(status):
    with pytest.raises(CalendarUnavailable):
        parse_bridge(payload(status=status), now=NOW, max_age_seconds=300)


def test_the_bridge_error_is_reported_verbatim():
    document = payload(status="DEGRADED", last_error="CalendarValueHistory(USD) failed with error 5401")
    with pytest.raises(CalendarUnavailable) as failure:
        parse_bridge(document, now=NOW, max_age_seconds=300)
    assert "DEGRADED" in str(failure.value) and "5401" in str(failure.value)


def test_a_missing_bridge_file_fails_closed(tmp_path):
    provider = Mt5CalendarProvider(tmp_path / "not-there.json", clock=lambda: NOW)
    decision = provider.decision("EURUSDm", NOW)

    assert decision.allowed is False and decision.available is False
    assert "does not exist" in decision.reason and "news_gate=FAIL" in decision.reason
    assert provider.status(NOW)["state"] == STATE_UNAVAILABLE
    assert provider.status(NOW)["bridge_file_exists"] is False


def test_an_unreadable_bridge_file_fails_closed(tmp_path):
    path = tmp_path / BRIDGE
    path.mkdir()  # a directory where the file should be: the read raises OSError, not a crash
    provider = Mt5CalendarProvider(path, clock=lambda: NOW)

    assert provider.decision("EURUSDm", NOW).allowed is False
    assert provider.status(NOW)["state"] == STATE_UNAVAILABLE


def test_the_provider_recovers_as_soon_as_the_bridge_is_written(tmp_path):
    path = tmp_path / BRIDGE
    provider = Mt5CalendarProvider(path, clock=lambda: NOW, failure_retry_seconds=10, read_seconds=30)

    assert provider.decision("EURUSDm", NOW).allowed is False
    write_bridge(tmp_path, payload())

    # Inside the failure retry interval nothing is re-read, and the gate stays closed.
    assert provider.decision("EURUSDm", NOW + timedelta(seconds=5)).allowed is False
    assert provider.decision("EURUSDm", NOW + timedelta(seconds=11)).allowed is True


def test_the_bridge_is_read_once_per_cycle_not_once_per_symbol(tmp_path):
    provider, _ = provider_for(tmp_path, payload(), read_seconds=300)
    reads = {"count": 0}
    original = provider.read

    def counted(now=None):
        reads["count"] += 1
        return original(now)

    provider.read = counted
    symbols = ["EURUSDm", "GBPUSDm", "USDJPYm", "USDCHFm", "AUDUSDm", "USDCADm", "NZDUSDm", "EURJPYm", "GBPJPYm", "EURGBPm"]

    assert all(provider.decision(symbol, NOW + timedelta(seconds=1)).allowed for symbol in symbols)
    assert reads["count"] == 1


# --------------------------------------------------------------------------- the fields


def test_importance_is_mapped_from_every_representation_mql5_offers():
    assert importance_of({"importance": "high"}) == HIGH_IMPACT
    assert importance_of({"importance": "medium"}) == MEDIUM_IMPACT
    assert importance_of({"importance": "low"}) == LOW_IMPACT
    assert importance_of({"importance": "none"}) == LOW_IMPACT
    assert importance_of({"importance_mql5": "CALENDAR_IMPORTANCE_HIGH"}) == HIGH_IMPACT
    assert importance_of({"importance_mql5": "CALENDAR_IMPORTANCE_MODERATE"}) == MEDIUM_IMPACT
    assert importance_of({"importance_code": 3}) == HIGH_IMPACT
    assert importance_of({"impact_type_code": 1}) == LOW_IMPACT
    # Nothing is invented: an unknown level is None, and the caller then refuses the calendar.
    assert importance_of({"importance": "critical"}) is None
    assert importance_of({}) is None
    assert importance_of({"importance_code": "high"}) is None


def test_an_unmappable_importance_makes_the_whole_calendar_unusable():
    # A terminal reporting a level this app has no meaning for is a calendar that cannot be read
    # in full: nothing is guessed, and the gate refuses every new entry instead.
    row = {
        **event(),
        "importance": "unknown",
        "importance_mql5": "CALENDAR_IMPORTANCE_UNKNOWN",
        "importance_code": 9,
        "impact_type_code": 9,
    }
    with pytest.raises(CalendarMalformed):
        parse_bridge(payload([row]), now=NOW, max_age_seconds=300)


def test_only_the_eight_traded_currencies_can_reach_the_gate(tmp_path):
    far = datetime(2026, 5, 11, 15, 0)
    document = payload([
        event(value_id=1, currency="USD", server_time=far),
        event(value_id=2, currency="EUR", server_time=far),
        # A row the bridge filtered out anyway; even if it appeared it could never block a pair.
        event(value_id=3, currency="TRY", country="Turkey", name="CBRT Rate Decision", server_time=far),
    ])
    provider, _ = provider_for(tmp_path, document)
    provider.refresh(NOW)  # status reports the last read, so read once first
    status = provider.status(NOW)

    assert status["currencies"] == sorted(TRADED_CURRENCIES)
    assert status["events"] == 2
    assert provider.decision("GBPUSDm", NOW).allowed is True


def test_a_calendar_with_no_traded_event_is_refused():
    document = payload([event(value_id=3, currency="TRY", country="Turkey")])
    with pytest.raises(CalendarUnavailable):
        parse_bridge(document, now=NOW, max_age_seconds=300)


def test_a_bridge_that_declares_a_different_event_count_is_not_self_consistent():
    with pytest.raises(CalendarMalformed) as failure:
        parse_bridge(payload(event_count=99), now=NOW, max_age_seconds=300)
    assert "self-consistent" in str(failure.value)


@pytest.mark.parametrize("missing", ["identity", "currency"])
def test_a_row_that_cannot_be_identified_or_attributed_is_refused(missing):
    row = event()
    if missing == "identity":
        del row["value_id"]
        del row["event_id"]
        with pytest.raises(CalendarMalformed):
            parse_bridge(payload([row]), now=NOW, max_age_seconds=300)
    else:
        # A row with no currency cannot be attributed, so it can never prove a window is clear.
        del row["currency"]
        with pytest.raises(CalendarUnavailable):
            parse_bridge(payload([row]), now=NOW, max_age_seconds=300)


# --------------------------------------------------------------------------- time normalisation


def test_the_server_offset_converts_a_known_release_to_its_real_utc_time():
    """The US Non-Farm Payrolls report is always released at 8:30 am New York time.

    In the northern summer that is 12:30 UTC, and a broker whose trade server runs at UTC+3 sees the
    release at 15:30 server time. The bridge exports exactly that pair, and the conversion lands on
    the known UTC instant: no fixed +2/+3 is assumed anywhere, the measured offset is used.
    """
    row = event(server_time=datetime(2026, 5, 1, 15, 30), offset=10800)
    reading = parse_bridge(payload([row]), now=NOW, max_age_seconds=300)

    assert reading.offset_seconds == 10800
    assert reading.events[0].when == datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    # And 12:30 UTC is 8:30 in New York on that date (EDT, UTC-4).
    assert reading.events[0].when.astimezone(timezone(timedelta(hours=-4))).strftime("%H:%M") == "08:30"


def test_the_same_server_stamp_in_winter_lands_an_hour_later_in_utc():
    """The offset is measured, not guessed: 15:30 server time means 13:30 UTC at UTC+2.

    Both rows are "the same release at 8:30 New York", and both are at 15:30 server time; only the
    offset the terminal measured differs, so only the offset may change the UTC instant.
    """
    row = event(server_time=datetime(2026, 1, 2, 15, 30), offset=7200)
    reading = parse_bridge(payload([row], offset=7200), now=NOW, max_age_seconds=300)

    assert reading.offset_seconds == 7200
    assert reading.events[0].when == datetime(2026, 1, 2, 13, 30, tzinfo=timezone.utc)
    # 13:30 UTC is still 8:30 in New York (EST, UTC-5): the same local release time.
    assert reading.events[0].when.astimezone(timezone(timedelta(hours=-5))).strftime("%H:%M") == "08:30"


def test_the_blocking_window_is_measured_on_the_utc_conversion(tmp_path):
    release = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    document = payload([event(server_time=datetime(2026, 5, 1, 15, 30))], generated_at=release)
    provider, _ = provider_for(tmp_path, document, now=release, window_minutes=30)

    # 12:30 UTC — the real release instant — is inside the window…
    blocked = provider.decision("EURUSDm", release)
    assert blocked.allowed is False and "Non-Farm Payrolls" in blocked.reason

    # …while the raw server stamp (15:30) is three hours away, and by then the bridge is stale, so
    # the gate refuses for the honest reason instead of pretending the window is clear.
    provider2, _ = provider_for(tmp_path, document, now=release, window_minutes=30, read_seconds=86_400)
    later = datetime(2026, 5, 1, 15, 30, tzinfo=timezone.utc)
    decision = provider2.decision("EURUSDm", later)
    assert decision.allowed is False and "news_gate=FAIL" in decision.reason
    assert "older than the 300s limit" in decision.reason and "Non-Farm Payrolls" not in decision.reason
    assert provider2.status(later)["state"] == STATE_STALE


def test_a_row_whose_server_time_and_utc_instant_disagree_is_rejected():
    row = event(server_time=datetime(2026, 5, 1, 15, 30), offset=10800)
    row["scheduled_time_utc"] = "2026-05-01T15:30:00+00:00"  # the server stamp passed off as UTC
    with pytest.raises(CalendarMalformed) as failure:
        parse_bridge(payload([row]), now=NOW, max_age_seconds=300)
    assert "offset" in str(failure.value)


@pytest.mark.parametrize("offset", [None, "three hours", 100000, 10830])
def test_a_missing_or_absurd_server_offset_is_rejected(offset):
    document = payload()
    document["server_utc_offset_seconds"] = offset
    with pytest.raises(CalendarMalformed):
        parse_bridge(document, now=NOW, max_age_seconds=300)


def test_a_row_without_a_server_timestamp_is_rejected():
    row = event()
    del row["scheduled_time_server"]
    with pytest.raises(CalendarMalformed):
        parse_bridge(payload([row]), now=NOW, max_age_seconds=300)


# --------------------------------------------------------------------------- the gate


def test_only_high_impact_events_inside_the_window_block(tmp_path):
    now = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    rows = [
        event(value_id=1, name="CPI", server_time=datetime(2026, 5, 1, 15, 26)),
        event(value_id=2, name="Retail sales", server_time=datetime(2026, 5, 1, 15, 34)),
        event(value_id=3, name="PMI", importance="medium", importance_code=2, server_time=datetime(2026, 5, 1, 15, 30)),
        event(value_id=4, name="Holiday", importance="low", importance_code=1, server_time=datetime(2026, 5, 1, 15, 30)),
        event(value_id=5, name="Old data", server_time=datetime(2026, 5, 1, 14, 0)),
    ]
    provider, _ = provider_for(tmp_path, payload(rows, generated_at=now), now=now, window_minutes=5)

    blocked = provider.decision("EURUSDm", now)
    assert blocked.allowed is False and "CPI" in blocked.reason and "Retail sales" in blocked.reason
    assert "PMI" not in blocked.reason and "Holiday" not in blocked.reason
    assert "Old data" not in blocked.reason  # 90 minutes before the window

    # The bridge keeps beating, and once every event has left the window the pair is tradable again
    # without a fresh snapshot: the same rows are re-evaluated, nothing is re-fetched from a broker.
    later = now + timedelta(minutes=12)
    write_bridge(tmp_path, payload(rows, generated_at=later), mtime=later)
    assert provider.decision("EURUSDm", later).allowed is True


def test_a_currency_that_is_not_in_the_pair_never_blocks_it(tmp_path):
    now = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    document = payload(
        [event(currency="JPY", country="Japan", name="BoJ Rate Decision", server_time=datetime(2026, 5, 1, 15, 30))],
        generated_at=now,
    )
    provider, _ = provider_for(tmp_path, document, now=now)

    assert provider.decision("EURUSDm", now).allowed is True
    assert provider.decision("EURGBPm", now).allowed is True
    assert provider.decision("USDJPYm", now).allowed is False
    assert provider.decision("GBPJPYm", now).allowed is False


def test_the_status_reports_what_an_operator_needs_and_no_credential(tmp_path):
    provider, _ = provider_for(tmp_path, payload())
    provider.refresh(NOW)  # status reports the last read; the default event is ten days away
    status = provider.status(NOW)

    assert status["provider"] == MT5_CALENDAR and status["state"] == STATE_FRESH and status["healthy"] is True
    assert status["events"] == 1 and status["high_impact_events"] == 1
    assert status["server_utc_offset_seconds"] == OFFSET and status["max_age_seconds"] == 300
    assert status["bridge_file_exists"] is True and status["clock_skew_seconds"] == pytest.approx(0.0, abs=5)
    assert status["requires_credential"] is False and status["paid_api"] is False
    assert "credential_env_var" not in status  # there is no key to point an operator at


@pytest.mark.parametrize("broken", ["missing", "malformed", "status", "stale", "clock", "no events"])
def test_every_provider_failure_refuses_a_new_entry(tmp_path, broken):
    path = tmp_path / BRIDGE
    if broken == "malformed":
        path.write_text("{not json", encoding="utf-8")
    elif broken == "status":
        write_bridge(tmp_path, payload(status="DEGRADED"))
    elif broken == "stale":
        write_bridge(tmp_path, payload(generated_at=NOW - timedelta(hours=1)), mtime=NOW - timedelta(hours=1))
    elif broken == "clock":
        write_bridge(tmp_path, payload(generated_at=NOW + timedelta(hours=3)))
    elif broken == "no events":
        write_bridge(tmp_path, payload([]))

    provider = Mt5CalendarProvider(path, clock=lambda: NOW, failure_retry_seconds=1)
    decision = provider.decision("EURUSDm", NOW)

    assert decision.allowed is False and "news_gate=FAIL" in decision.reason
    assert provider.status(NOW)["healthy"] is False


# --------------------------------------------------------------------------- cache


def test_the_calendar_is_persisted_under_its_own_provider_and_restored_untrusted(tmp_path, session_factory, db):
    cache = SqlNewsCache(session_factory, provider=MT5_CALENDAR)
    first, _ = provider_for(tmp_path, payload(), cache=cache, max_age_seconds=300)

    assert first.decision("EURUSDm", NOW + timedelta(seconds=1)).allowed is True
    assert db.scalar(select(NewsProviderStateRecord)).healthy is True
    stored = db.scalar(select(NewsEventRecord))
    assert (stored.provider, stored.event_id, stored.currency, stored.impact) == (MT5_CALENDAR, "mt5:900001", "USD", HIGH_IMPACT)

    # A restarted engine sees the same rows and their retrieval time, but it has verified nothing
    # itself yet, so the gate stays closed until the bridge has actually been read.
    restarted = Mt5CalendarProvider(tmp_path / BRIDGE, clock=lambda: NOW + timedelta(minutes=1), cache=cache)
    status = restarted.status(NOW + timedelta(minutes=1))
    assert status["events"] == 1 and status["healthy"] is False and status["state"] == STATE_NOT_ATTEMPTED
    assert "restored from the persisted cache" in status["detail"]

    # The bridge is still being written, so the very next read makes it fresh again.
    assert restarted.decision("EURUSDm", NOW + timedelta(minutes=2)).allowed is True

    # A restored calendar whose bridge has disappeared keeps the gate closed: a cache is not proof.
    gone = Mt5CalendarProvider(tmp_path / "gone.json", clock=lambda: NOW, cache=cache)
    assert gone.status(NOW)["events"] == 1
    assert gone.decision("EURUSDm", NOW).allowed is False


def test_the_cache_can_be_rebound_to_another_provider(session_factory):
    trading_economics = SqlNewsCache(session_factory)
    rebound = trading_economics.for_provider(MT5_CALENDAR)

    assert rebound.provider == MT5_CALENDAR and trading_economics.provider == "trading_economics"
    assert rebound.for_provider(MT5_CALENDAR) is rebound


# --------------------------------------------------------------------------- wiring


def test_mt5_calendar_is_selected_from_the_configuration_without_a_credential(tmp_path):
    settings = Settings(_env_file=None, news_provider="mt5_calendar", mt5_calendar_bridge_file=str(tmp_path / BRIDGE))

    provider = build_news_provider(settings)
    assert isinstance(provider, Mt5CalendarProvider)
    assert settings.trading_economics_api_key is None  # nothing to pay for, nothing to configure

    # The provider works with no key at all: the bridge is its only input.
    now = datetime.now(timezone.utc)
    write_bridge(tmp_path, payload([event(server_time=now + timedelta(days=1) + timedelta(seconds=OFFSET))], generated_at=now), mtime=now)
    assert provider.decision("EURUSDm", now).allowed is True
    assert provider.status(now)["requires_credential"] is False


def test_the_trading_economics_provider_is_still_available_but_not_required():
    economics = build_news_provider(Settings(_env_file=None, news_provider="trading_economics"))

    assert economics.provider == "trading_economics"
    # With no key it reports an honest failure — and nothing in this deployment reads that key.
    assert economics.decision("EURUSDm").allowed is False


def test_an_unknown_provider_name_still_fails_closed():
    assert build_news_provider(Settings(_env_file=None, news_provider="something_else")).decision("EURUSDm").allowed is False


# --------------------------------------------------------------------------- the loop


def test_a_fresh_bridge_lets_the_cycle_trade(tmp_path, session_factory):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now)
    # The only event is a day away, so nothing is inside the +/-30 minute window.
    document = payload([event(server_time=now + timedelta(days=1) + timedelta(seconds=OFFSET))], generated_at=now)
    provider, _ = provider_for(tmp_path, document, now=now)
    scheduler = MarketScheduler(trading_settings(symbols=["EURUSD"], news_fail_closed=True), gateway, session_factory, news=provider)

    summary = process_cycle(scheduler, now)

    assert summary["news"]["provider"] == MT5_CALENDAR and summary["news"]["state"] == STATE_FRESH
    assert summary["executions"] == 1 and len(gateway.requests) == 1


def test_a_stale_bridge_blocks_the_cycle(tmp_path, session_factory):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now)
    provider, _ = provider_for(
        tmp_path, payload(generated_at=now - timedelta(hours=1)), now=now, read_seconds=86_400, failure_retry_seconds=86_400
    )
    scheduler = MarketScheduler(trading_settings(symbols=["EURUSD"], news_fail_closed=True), gateway, session_factory, news=provider)

    summary = process_cycle(scheduler, now)

    assert gateway.requests == [] and summary["executions"] == 0
    assert "news_gate=FAIL" in summary["symbols"]["EURUSD"]["blocked"][0]
    assert summary["news"]["healthy"] is False


def test_a_missing_bridge_blocks_the_cycle(tmp_path, session_factory):
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now)
    provider = Mt5CalendarProvider(tmp_path / "gone.json", clock=lambda: now)
    scheduler = MarketScheduler(trading_settings(symbols=["EURUSD"], news_fail_closed=True), gateway, session_factory, news=provider)

    summary = process_cycle(scheduler, now)

    assert gateway.requests == [] and summary["executions"] == 0
    assert "news_gate=FAIL" in summary["symbols"]["EURUSD"]["blocked"][0]
