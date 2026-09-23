"""The dry run must report the whole decision without any path to the broker."""
import pytest

from app import dry_run
from app import main as api_main  # noqa: F401  (imported to prove the CLI does not shadow the API)
from app.core.config import Settings
from conftest import FakeAccount, market_gateway


def hard_settings(**overrides) -> Settings:
    values = {"symbols": ["EURUSD"], "news_fail_closed": False}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_the_dry_run_gateway_refuses_every_write_call():
    gateway = dry_run.ReadOnlyGateway(market_gateway())

    for name, args in (("order_send", ({},)), ("modify_position", (1, "EURUSD", 1, 2)), ("close_position", (object(),)), ("cancel_order", (object(),))):
        with pytest.raises(RuntimeError):
            getattr(gateway, name)(*args)

    assert gateway.attempted_writes == ["order_send", "modify_position", "close_position", "cancel_order"]


def test_the_dry_run_has_no_sqlite_fallback_for_the_risk_state(monkeypatch):
    """A local file would report another store's allowance; the dry run must not be able to."""
    assert not hasattr(dry_run, "FALLBACK_STATE_FILE")

    def refused(*args, **kwargs):
        raise RuntimeError("the configured database is unreachable")

    monkeypatch.setattr(dry_run, "create_engine", refused)

    with pytest.raises(RuntimeError):
        dry_run.state_session_factory(hard_settings())


def test_the_dry_run_reports_the_hard_limits_and_a_real_account_without_ordering(monkeypatch, session_factory):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    gateway = market_gateway(now, account=FakeAccount(trade_mode=2, balance=11.56, equity=11.56, margin_free=11.56, leverage=400.0))
    wrapped = dry_run.ReadOnlyGateway(gateway)
    monkeypatch.setattr(dry_run, "state_session_factory", lambda settings: (session_factory, "test database"))
    monkeypatch.setattr(dry_run, "database_report", lambda settings: {"status": "UP", "dialect": "sqlite", "revision": dry_run.EXPECTED_ALEMBIC_REVISION, "expected_revision": dry_run.EXPECTED_ALEMBIC_REVISION, "migrated": True, "error": None})

    report = dry_run.build_report(hard_settings(), wrapped, now)

    assert report["orders_submitted"] == 0 and report["write_calls_attempted"] == []
    assert gateway.requests == []
    assert report["account"]["trade_mode_label"] == "REAL" and report["account"]["is_real"] is True
    assert report["hard_limits"] == {"max_bot_capital_usd": 3.0, "max_loss_per_trade_usd": 0.70, "max_session_loss_usd": 1.40, "max_daily_trades": 3, "max_open_positions": 1, "max_lots_per_position": 0.01}
    assert report["verdict"] == "REJECT" and "REAL" in report["reason"]
    assert report["selected"] == "EURUSD" and report["selection"]["risk"]["min_lot_risk_usd"] > 0.70
    assert report["database"]["status"] == "UP" and report["database"]["migrated"] is True
    assert report["session"]["kill_switch_reason"] is None and report["session"]["available"] is True
    assert report["live_gates"] == {
        "trading_mode": "demo", "live_trading_enabled": False, "live_orders_permitted": False,
        "orders_authorized": False, "changes_applied": [], "note": report["live_gates"]["note"],
    }

    text = dry_run.render(report)
    for expected in (
        "MT5 HARD-LIMIT DRY RUN", "detected trade mode     : REAL", "alembic revision", "VERDICT: REJECT",
        "orders submitted: 0", "broker minimum lot", "expected loss at SL", "risk/reward",
        "max lots per position : 0.01", "LIVE GATES", "TRADING_MODE=demo  LIVE_TRADING_ENABLED=false",
        "REQUIRES EXPLICIT AUTHORIZATION", "account_matches_expected",
    ):
        assert expected in text


def test_the_dry_run_refuses_to_judge_when_the_risk_state_is_unreachable(monkeypatch):
    """The gate fails closed without a risk state, and the report says why instead of guessing."""
    from datetime import datetime, timezone

    def unavailable(settings):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(dry_run, "state_session_factory", unavailable)
    monkeypatch.setattr(dry_run, "database_report", lambda settings: {"status": "DOWN", "dialect": None, "revision": None, "expected_revision": dry_run.EXPECTED_ALEMBIC_REVISION, "migrated": False, "error": "OperationalError: connection refused"})
    now = datetime.now(timezone.utc)
    gateway = market_gateway(now, account=FakeAccount(trade_mode=0))
    wrapped = dry_run.ReadOnlyGateway(gateway)

    report = dry_run.build_report(hard_settings(), wrapped, now)

    assert report["verdict"] == "REJECT" and "risk-state database is unreachable" in report["reason"]
    assert report["session"]["available"] is False and report["symbols_evaluated"] == []
    assert report["orders_submitted"] == 0 and wrapped.attempted_writes == []
    text = dry_run.render(report)
    assert "status                : DOWN" in text and "orders submitted: 0" in text


def test_the_dry_run_reports_a_stale_schema_as_a_rejection(monkeypatch, session_factory):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    gateway = dry_run.ReadOnlyGateway(market_gateway(now, account=FakeAccount(trade_mode=0)))
    monkeypatch.setattr(dry_run, "state_session_factory", lambda settings: (session_factory, "test database"))
    monkeypatch.setattr(dry_run, "database_report", lambda settings: {"status": "UP", "dialect": "sqlite", "revision": "0004_hard_risk_limits", "expected_revision": dry_run.EXPECTED_ALEMBIC_REVISION, "migrated": False, "error": "the database is at migration 0004_hard_risk_limits"})

    report = dry_run.build_report(hard_settings(), gateway, now)

    assert report["verdict"] == "REJECT" and "not at 0005_session_kill_switch" in report["reason"]
