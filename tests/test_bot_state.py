"""Lifecycle: start validates MT5, the emergency lock is durable, and other systems are untouched."""
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.clock import utcnow
from app.database.base import AuditRecord
from app.news.provider import UnavailableNewsProvider
from app.risk.state import RiskStateStore
from app.services.bot import BotService, BotState
from app.services.scheduler import MarketScheduler
from conftest import FakeGateway, FakeOrder, FakePosition, market_gateway


def bot_settings(**overrides) -> Settings:
    values = {"symbols": [], "news_fail_closed": False}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def build(settings, gateway, session_factory) -> tuple[BotService, MarketScheduler]:
    scheduler = MarketScheduler(settings, gateway, session_factory, news=UnavailableNewsProvider(False))
    return BotService(settings, gateway, session_factory=session_factory, scheduler=scheduler), scheduler


@pytest.fixture
def bot_factory(session_factory):
    """Builds bot services and guarantees their market loop thread is stopped before teardown."""
    services: list[BotService] = []

    def make(settings, gateway) -> tuple[BotService, MarketScheduler]:
        service, scheduler = build(settings, gateway, session_factory)
        services.append(service)
        return service, scheduler

    yield make
    for service in services:
        if service.scheduler is not None:
            service.scheduler.stop()


def live_gateway():
    """A gateway that would trade if nothing blocked it."""
    now = utcnow()
    return market_gateway(now), now


def test_start_validates_mt5_and_fails_closed(bot_factory):
    service, scheduler = bot_factory(bot_settings(), FakeGateway(connected=False))

    ok, detail = service.start()

    assert ok is False and "initialization failed" in detail
    assert service.state.state is BotState.ERROR and service.state.running is False
    assert scheduler.running is False


def test_the_starting_state_is_visible_while_mt5_is_validated(bot_factory):
    gateway = FakeGateway()
    service, _ = bot_factory(bot_settings(), gateway)
    observed = []

    def observe():
        observed.append(service.state.state)
        return gateway.health()

    gateway.initialize = observe
    service.start()

    assert observed == [BotState.STARTING]
    assert service.state.state is BotState.RUNNING


def test_start_and_stop_drive_the_market_loop(bot_factory):
    service, scheduler = bot_factory(bot_settings(), FakeGateway())

    ok, detail = service.start()

    assert ok is True and "running" in detail
    assert service.state.state is BotState.RUNNING and service.state.running is True
    assert scheduler.running is True and scheduler.thread_alive is True

    service.stop()
    assert service.state.state is BotState.STOPPED and scheduler.running is False and scheduler.thread_alive is False


def test_running_with_unhealthy_mt5_is_degraded(bot_factory):
    gateway = FakeGateway()
    service, _ = bot_factory(bot_settings(), gateway)
    service.start()

    gateway.connected = False
    status = service.status()
    assert status["state"] == BotState.DEGRADED.value and "unhealthy" in status["detail"]
    assert status["mt5"]["connected"] is False

    gateway.connected = True
    assert service.status()["state"] == BotState.RUNNING.value


def test_running_with_blocked_market_data_is_degraded(bot_factory):
    """MT5 is healthy, but the last cycle could not scan a single symbol."""
    service, scheduler = bot_factory(bot_settings(symbols=["EURUSD"]), FakeGateway())
    service.start()
    scheduler.stop()  # let the first cycle finish, then inspect the reported state

    status = service.status()

    assert status["state"] == BotState.DEGRADED.value
    assert "every configured symbol is blocked" in status["detail"]
    assert "no tick is available" in status["detail"]


def test_emergency_stop_stops_the_loop_persists_the_lock_and_blocks_trading(bot_factory, db):
    gateway, now = live_gateway()
    service, scheduler = bot_factory(bot_settings(symbols=["EURUSD"]), gateway)
    assert service.start()[0] is True

    report = service.emergency_stop()
    orders_before = len(gateway.requests)

    assert service.state.state is BotState.EMERGENCY_LOCKED and service.state.emergency_locked is True
    assert report["scheduler_stopped"] is True and report["lock_persisted"] is True
    assert scheduler.running is False
    assert RiskStateStore(db).load().emergency_locked is True
    assert "EMERGENCY_LOCK" in [event.event_type for event in db.scalars(select(AuditRecord))]
    assert service.stop()[1].startswith("stopped, the persisted emergency lock")

    # A cycle started by the scheduler (or by hand) now refuses to scan and cannot order.
    summary = scheduler.run_once(now=now)
    assert len(gateway.requests) == orders_before
    assert summary["blocked"] and "emergency lock" in summary["blocked"][0]


def test_the_emergency_lock_survives_a_restart_and_needs_an_explicit_reset(bot_factory, db):
    gateway, _ = live_gateway()
    service, _ = bot_factory(bot_settings(), gateway)
    service.emergency_stop()

    restarted_gateway, now = live_gateway()
    restarted, restarted_scheduler = bot_factory(bot_settings(symbols=["EURUSD"]), restarted_gateway)
    ok, detail = restarted.start()

    assert ok is False and "reset" in detail
    assert restarted.state.state is BotState.EMERGENCY_LOCKED
    assert restarted_scheduler.run_once(now=now)["blocked"]
    assert restarted_gateway.requests == []

    reset_ok, reset_detail = restarted.emergency_reset()
    assert reset_ok is True and "reset complete" in reset_detail
    assert RiskStateStore(db).load().emergency_locked is False
    assert restarted.state.state is BotState.STOPPED
    assert restarted.start()[0] is True


def test_emergency_reset_is_refused_while_the_bot_is_running(bot_factory):
    service, scheduler = bot_factory(bot_settings(), FakeGateway())
    service.start()

    ok, detail = service.emergency_reset()

    assert ok is False and "stop the bot" in detail
    assert scheduler.running is True


def test_emergency_close_touches_only_bot_managed_tickets(bot_factory):
    ours_position = FakePosition(ticket=1, magic=260825)
    other_position = FakePosition(ticket=2, symbol="GBPUSD", magic=999999)
    ours_order = FakeOrder(ticket=11, magic=260825)
    other_order = FakeOrder(ticket=12, symbol="GBPUSD", magic=999999)
    gateway = FakeGateway(position_list=(ours_position, other_position), order_list=(ours_order, other_order))
    service, _ = bot_factory(bot_settings(), gateway)

    report = service.emergency_stop(close_positions=True, cancel_pending_orders=True)

    assert gateway.closed == [ours_position] and gateway.cancelled == [ours_order]
    assert report["positions"] == {"closed": 1, "failed": 0, "skipped": 1, "detail": "closing requested for bot-managed positions only"}
    assert report["orders"]["cancelled"] == 1 and report["orders"]["skipped"] == 1


def test_start_needs_a_database_session_factory(settings):
    service = BotService(settings, FakeGateway())

    ok, detail = service.start()

    assert ok is False and "session factory" in detail
    assert service.state.state is BotState.ERROR


def test_live_orders_require_both_gates_and_are_never_enabled_here(bot_factory):
    service, _ = bot_factory(bot_settings(), FakeGateway())
    status = service.status()

    assert status["mode"] == "demo" and status["live_orders_permitted"] is False
    assert Settings(_env_file=None, trading_mode="demo", live_trading_enabled=True).live_orders_permitted is False
    assert Settings(_env_file=None, trading_mode="live", live_trading_enabled=False).live_orders_permitted is False


def test_an_unreadable_risk_state_blocks_start_and_reports_degraded(bot_factory, monkeypatch):
    service, _ = bot_factory(bot_settings(), FakeGateway())

    def broken(self, day=None):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(RiskStateStore, "load", broken)
    ok, detail = service.start()

    assert ok is False and "risk state could not be read" in detail
    assert service.state.state is BotState.ERROR
    assert service.status()["state"] == BotState.ERROR
