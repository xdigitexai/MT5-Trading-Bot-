"""One authorization, one live trade - and the persistent reservation that stops the second one.

Every broker call in this file goes to the fake gateway: no test here can send a real order. The
single live trade is authorized once, reserved at send time by a conditional UPDATE, and from then
on no cycle, process, restart or new calendar day may authorize another one.
"""
from datetime import date, datetime, timezone
from threading import Barrier, Thread

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main
from app.core.config import Settings
from app.database.base import Base, OneTradeAuthorizationRecord, TradeRecord
from app.database.session import get_db
from app.execution.service import ExecutionService
from app.mt5.account import AccountProfile
from app.news.provider import UnavailableNewsProvider
from app.risk.authorization import CHECK_NAME, authorization_state
from app.risk.engine import EntryFacts, RiskEngine
from app.risk.sizing import SymbolSpec
from app.risk.state import RiskStateStore
from app.services.bot import BotService
from app.services.reconciliation import Reconciler
from app.services.scheduler import MarketScheduler
from conftest import FakeAccount, FakeDeal, FakeGateway, FakePosition, make_intent, make_signal

TOKEN = "one-trade-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
# One point (0.00001) is worth 1.00 USD per lot, so a 70-point stop on the 0.01 minimum lot is
# exactly the default 0.70 USD per-trade budget: a setup that fits the hard limits on the nose.
MICRO = SymbolSpec(volume_min=0.01, volume_max=100.0, volume_step=0.01, tick_value=1.0, tick_size=0.00001, point=0.00001, contract_size=100_000.0)


def live_settings(**overrides) -> Settings:
    """The deployment's shape: TRADING_MODE=live with the live gate on and one daily trade."""
    values = {"trading_mode": "live", "live_trading_enabled": True, "symbols": ["EURUSD"], "max_daily_trades": 1, "api_token": TOKEN, "news_fail_closed": False}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def real_account(trade_mode: int = 2) -> AccountProfile:
    return AccountProfile(login=134693538, server="ExnessKE-MT5Real9", company="Exness (KE) Limited", currency="USD", balance=11.56, equity=11.56, free_margin=11.56, leverage=400.0, trade_mode=trade_mode)


def live_signal(**overrides):
    values = dict(symbol="EURUSDm", entry=1.10000, stop_loss=1.09930, take_profit=1.10140)
    values.update(overrides)
    return make_signal(**values)


def live_facts(**overrides) -> EntryFacts:
    values = dict(
        spec=MICRO, connected=True, account=real_account(), algo_trading_enabled=True, data_age_seconds=5.0,
        momentum_action="HOLD", spread_points=1.0, open_positions=0, symbol_positions=0, exposure=0.0,
        margin_level=None, duplicate_exists=False, emergency_locked=False, trading_enabled=True,
        free_margin=11.56, equity=11.56, leverage=400.0,
    )
    values.update(overrides)
    return EntryFacts(**values)


def live_database(tmp_path, name: str = "one-trade.db"):
    """A file-backed database, so a "restart" can reopen the very same persisted authorization.

    ``IMMEDIATE`` transactions serialize the writers on this file, which is what makes the
    concurrency test below deterministic: the loser's reservation begins after the winner committed,
    so it reads the spent row instead of a stale one.
    """
    engine = create_engine(
        f"sqlite:///{(tmp_path / name).as_posix()}",
        connect_args={"isolation_level": "IMMEDIATE", "timeout": 30.0, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def execution_for(settings, gateway) -> ExecutionService:
    return ExecutionService(settings, gateway)


def live_gateway() -> FakeGateway:
    return FakeGateway(account=FakeAccount(trade_mode=2), position_list=(FakePosition(),))


# --------------------------------------------------------------------------- 1. available

def test_before_the_first_trade_the_authorization_is_available(tmp_path):
    engine, sessions = live_database(tmp_path)
    with sessions() as db:
        store = RiskStateStore(db)
        state = authorization_state(live_settings(), store)

        assert store.one_trade_authorization_consumed() is False
        assert state.available is True and state.ordering_enabled is True
        assert state.consumed is False and state.blocked_reason == ""
        # The whole chain, including the authorization check, approves the first live entry.
        assessment = RiskEngine(live_settings()).assess(live_signal(), live_facts(), state=store)
        assert assessment.approved and assessment.failures == []
        assert [check for check in assessment.checks if check.name == CHECK_NAME][0].ok is True
    engine.dispose()


# --------------------------------------------------------------------------- 2. the first order

def test_the_first_valid_live_order_executes(db):
    gateway = live_gateway()
    result = execution_for(live_settings(), gateway).submit(db, make_intent())

    assert result.status == "EXECUTED" and result.accepted and result.order_ticket == "55501"
    assert len(gateway.requests) == 1 and gateway.requests[0]["symbol"] == "EURUSD"
    assert gateway.requests[0]["sl"] == 1.09 and gateway.requests[0]["tp"] == 1.12


# --------------------------------------------------------------------------- 3. consumed

def test_the_send_spends_the_single_live_authorization(db):
    gateway = live_gateway()
    execution_for(live_settings(), gateway).submit(db, make_intent(trade_id="trade-1", key="key-1"))

    store = RiskStateStore(db)
    row = db.scalar(select(OneTradeAuthorizationRecord))
    assert row.consumed is True and row.consumed_trade_id == "trade-1"
    assert row.consumed_at is not None and "EURUSD BUY" in row.consumed_reason
    assert store.one_trade_authorization_consumed() is True
    # The mode is untouched: ordering is what stopped, not the deployment.
    assert live_settings().trading_mode.value == "live" and live_settings().live_orders_permitted is True
    state = authorization_state(live_settings(), store)
    assert state.consumed is True and state.available is False and state.ordering_enabled is False
    assert "new manual authorization" in state.blocked_reason


# --------------------------------------------------------------------------- 4. the second order

def test_a_second_live_order_is_rejected_once_the_authorization_is_spent(db):
    gateway = live_gateway()
    execution = execution_for(live_settings(), gateway)
    first = execution.submit(db, make_intent(trade_id="trade-1", key="key-1"))

    second = execution.submit(db, make_intent(trade_id="trade-2", key="key-2"))

    assert first.status == "EXECUTED"
    assert second.status == "REJECTED" and not second.accepted
    assert "already reserved" in second.reason and "new manual authorization" in second.reason
    assert "the single authorized live trade is already consumed" in second.reasons
    assert len(gateway.requests) == 1  # the second order never reached the gateway
    assert sorted(row.status for row in db.scalars(select(TradeRecord))) == ["EXECUTED", "REJECTED"]

    # And the gate refuses it by name, on every later cycle, even though the account is authorized.
    assessment = RiskEngine(live_settings()).assess(live_signal(), live_facts(), state=RiskStateStore(db))
    assert not assessment.approved and CHECK_NAME in assessment.failed_checks
    assert "already reserved" in assessment.reason


def test_the_authorization_check_is_only_about_live_ordering(db):
    """A spent authorization never blocks a non-live deployment: nothing real could be sent there."""
    gateway = live_gateway()
    execution_for(live_settings(), gateway).submit(db, make_intent())
    demo = Settings(_env_file=None, symbols=["EURUSD"])

    assessment = RiskEngine(demo).assess(live_signal(), live_facts(account=real_account(trade_mode=0)), state=RiskStateStore(db))

    assert [check for check in assessment.checks if check.name == CHECK_NAME][0].ok is True
    assert authorization_state(demo, RiskStateStore(db)).ordering_enabled is False


# --------------------------------------------------------------------------- 5. a restart

def test_a_restart_does_not_reset_the_authorization(tmp_path):
    engine_one, sessions_one = live_database(tmp_path)
    first_gateway = live_gateway()
    with sessions_one() as db:
        assert execution_for(live_settings(), first_gateway).submit(db, make_intent(trade_id="trade-1", key="key-1")).status == "EXECUTED"
    engine_one.dispose()

    # Simulated restart: new process, new engine, new session, zero in-memory state, same database.
    engine_two, sessions_two = live_database(tmp_path)
    restarted_gateway = live_gateway()
    with sessions_two() as db:
        store = RiskStateStore(db)
        assert store.one_trade_authorization_consumed() is True
        second = execution_for(live_settings(), restarted_gateway).submit(db, make_intent(trade_id="trade-2", key="key-2"))
        assert second.status == "REJECTED" and "already reserved" in second.reason
        assert restarted_gateway.requests == []
        assert not RiskEngine(live_settings()).assess(live_signal(), live_facts(), state=store).approved
    engine_two.dispose()


# --------------------------------------------------------------------------- 6. a new day

def test_a_new_calendar_day_does_not_reset_the_authorization(tmp_path):
    engine, sessions = live_database(tmp_path)
    with sessions() as db:
        execution_for(live_settings(), live_gateway()).submit(db, make_intent(trade_id="trade-1", key="key-1"))
        store = RiskStateStore(db)
        tomorrow = date(2999, 1, 1)

        # The daily trade counter is a *daily* cap and does reset tomorrow; the authorization is not
        # that, which is exactly why this refusal cannot be delegated to MAX_DAILY_TRADES.
        assert store.load(tomorrow).trades_opened == 0
        assert store.one_trade_authorization_consumed() is True
        assert authorization_state(live_settings(), store).ordering_enabled is False

        tomorrow_assessment = RiskEngine(live_settings()).assess(live_signal(), live_facts(), state=store, day=tomorrow)
        assert not tomorrow_assessment.approved and CHECK_NAME in tomorrow_assessment.failed_checks
    engine.dispose()


# --------------------------------------------------------------------------- manual re-arm

def test_only_an_explicit_manual_authorization_re_arms_one_trade(tmp_path):
    engine, sessions = live_database(tmp_path)
    gateway = live_gateway()
    with sessions() as db:
        execution = execution_for(live_settings(), gateway)
        assert execution.submit(db, make_intent(trade_id="trade-1", key="key-1")).status == "EXECUTED"
        assert execution.submit(db, make_intent(trade_id="trade-2", key="key-2")).status == "REJECTED"

        RiskStateStore(db).authorize_one_trade("operator re-armed exactly one more live trade")

        assert authorization_state(live_settings(), RiskStateStore(db)).available is True
        assert execution.submit(db, make_intent(trade_id="trade-3", key="key-3")).status == "EXECUTED"
        assert len(gateway.requests) == 2
        assert execution.submit(db, make_intent(trade_id="trade-4", key="key-4")).status == "REJECTED"
        assert len(gateway.requests) == 2
    engine.dispose()


# --------------------------------------------------------------------------- fail closed

class UnreadableAuthorization:
    """A store whose authorization read fails: the gate must refuse rather than assume one is left."""

    def load(self, day=None): raise RuntimeError("the risk-state database is unavailable")
    def observe_equity(self, equity, day=None): raise RuntimeError("the risk-state database is unavailable")
    def one_trade_authorization(self): raise RuntimeError("the risk-state database is unavailable")


def test_an_unreadable_authorization_refuses_a_live_order():
    assessment = RiskEngine(live_settings()).assess(live_signal(), live_facts(), state=UnreadableAuthorization())

    assert not assessment.approved and CHECK_NAME in assessment.failed_checks
    reason = [check for check in assessment.failures if check.name == CHECK_NAME][0].reason
    assert "could not be read" in reason and "no live order may be authorized" in reason


def test_an_unreadable_authorization_refuses_to_send(db, monkeypatch):
    def broken(self, day=None):
        raise RuntimeError("the authorization table is gone")

    monkeypatch.setattr(RiskStateStore, "one_trade_authorization", broken)
    gateway = live_gateway()

    result = execution_for(live_settings(), gateway).submit(db, make_intent())

    assert result.status == "REJECTED" and "could not be reserved" in result.reason
    assert gateway.requests == []


# --------------------------------------------------------------------------- the race

def test_two_concurrent_reservations_let_exactly_one_win(tmp_path):
    """Two processes reserving the same authorization at the same moment: one winner, never two."""
    engine, sessions = live_database(tmp_path)
    with sessions() as db:
        RiskStateStore(db).one_trade_authorization()  # the row exists before the race, as it does in production
    outcomes, barrier = {}, Barrier(2, timeout=30)

    def reserve(name: str) -> None:
        with sessions() as db:
            barrier.wait()
            outcomes[name] = RiskStateStore(db).consume_one_trade_authorization(f"trade-{name}")

    threads = [Thread(target=reserve, args=(name,), daemon=True) for name in ("a", "b")]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=60)

    assert len(outcomes) == 2
    assert sorted(outcomes.values()) == [False, True]
    with sessions() as db:
        row = db.scalar(select(OneTradeAuthorizationRecord))
        assert row.consumed is True and row.consumed_trade_id in ("trade-a", "trade-b")
        assert RiskStateStore(db).consume_one_trade_authorization("trade-late") is False
    engine.dispose()


def test_two_concurrent_processes_cannot_both_send_an_order(tmp_path):
    """The same race one level up: two engine processes, one live authorization, one order."""
    engine, sessions = live_database(tmp_path)
    settings = live_settings()
    gateway = FakeGateway(account=FakeAccount(trade_mode=2), position_list=(FakePosition(),))
    execution = ExecutionService(settings, gateway)
    outcomes, barrier = {}, Barrier(2, timeout=30)

    def send(name: str) -> None:
        with sessions() as db:
            barrier.wait()
            outcomes[name] = execution.submit(db, make_intent(trade_id=f"trade-{name}", key=f"key-{name}"))

    threads = [Thread(target=send, args=(name,), daemon=True) for name in ("a", "b")]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=60)

    assert len(outcomes) == 2
    assert sorted(outcome.status for outcome in outcomes.values()) == ["EXECUTED", "REJECTED"]
    assert len(gateway.requests) == 1  # exactly one order reached the broker stand-in
    with sessions() as db:
        assert RiskStateStore(db).one_trade_authorization_consumed() is True
    engine.dispose()


# --------------------------------------------------------------------------- 7. everything else keeps working

def test_reconciliation_and_reporting_still_work_after_ordering_is_disabled(session_factory, client_for):
    """A spent authorization stops ordering; it must not stop reconciliation, analytics or status."""
    settings = live_settings()
    gateway = live_gateway()
    client = client_for(settings, session_factory, gateway)
    now = datetime.now(timezone.utc)
    with session_factory() as db:
        outcome = execution_for(settings, gateway).submit(db, make_intent(trade_id="trade-1", key="key-1"))
        assert outcome.status == "EXECUTED"
        trade = db.scalar(select(TradeRecord))
        trade.mt5_position_ticket = "9001"
        db.commit()

        # MT5 is the source of truth: the closed deal still reconciles and realizes the P/L.
        deals = (
            FakeDeal(ticket=9001, order=7001, position_id=9001, entry=0, volume=0.05, price=1.1, commission=-1.0, time=(now.timestamp() - 3600)),
            FakeDeal(ticket=9002, order=7002, position_id=9001, entry=1, type=1, volume=0.05, price=1.105, profit=25.0, commission=-1.0, swap=-0.5, reason=5, time=(now.timestamp() - 600)),
        )
        summary = Reconciler(settings, FakeGateway(deals=deals)).run(db, now=now)
        db.refresh(trade)
        assert summary.matched == 1 and summary.updated == 1 and summary.pnl_applied == 22.5
        assert trade.status == "CLOSED" and trade.close_reason == "take_profit"

    status = client.get("/api/bot/status", headers=AUTH).json()
    assert status["mode"] == "live" and status["live_orders_permitted"] is True
    assert status["one_trade_authorization"]["consumed"] is True
    assert status["one_trade_authorization"]["ordering_enabled"] is False

    risk = client.get("/api/risk", headers=AUTH).json()
    assert risk["mode"] == "live" and risk["live_orders_permitted"] is True
    assert risk["one_trade_authorization"]["consumed"] is True
    assert risk["one_trade_authorization"]["ordering_enabled"] is False
    assert risk["one_trade_authorization"]["consumed_trade_id"] == "trade-1"
    assert risk["one_trade_authorization"]["consumed_at"]  # when the one trade was reserved

    performance = client.get("/api/performance", headers=AUTH).json()
    assert performance["total_trades"] == 1 and performance["net_realized_pnl"] == pytest.approx(22.5)

    operational = client.get("/api/status", headers=AUTH).json()
    assert "connected" in operational["mt5"]  # the MT5 monitor still answers
    assert operational["trading_mode"] == "live" and operational["live_orders_permitted"] is True
    assert operational["one_trade_authorization"]["ordering_enabled"] is False
    assert operational["risk"]["day"] is not None  # the risk state is still reported
    assert operational["database"]["healthy"] is True


@pytest.fixture
def client_for(session_factory, monkeypatch):
    """A live-configured API client over the test database; the news provider is not the live one."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "news", UnavailableNewsProvider(False))
    clients = []

    def build(settings, factory, gateway) -> TestClient:
        scheduler = MarketScheduler(settings, gateway, factory, news=UnavailableNewsProvider(False))
        bot = BotService(settings, gateway, session_factory=factory, scheduler=scheduler)
        main.app.dependency_overrides[main.current_settings] = lambda: settings
        main.app.dependency_overrides[main.current_bot] = lambda: bot
        main.app.dependency_overrides[main.current_scheduler] = lambda: scheduler
        monkeypatch.setattr(main, "settings", settings)

        def override_db():
            session = factory()
            try:
                yield session
            finally:
                session.close()

        main.app.dependency_overrides[get_db] = override_db
        clients.append((TestClient(main.app), scheduler))
        return clients[-1][0]

    yield build
    for _, scheduler in clients:
        scheduler.stop()
    main.app.dependency_overrides.clear()
