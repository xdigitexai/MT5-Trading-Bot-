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


def test_the_dry_run_reports_the_hard_limits_and_a_real_account_without_ordering(monkeypatch, session_factory):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    gateway = market_gateway(now, account=FakeAccount(trade_mode=2, balance=11.56, equity=11.56, margin_free=11.56, leverage=400.0))
    wrapped = dry_run.ReadOnlyGateway(gateway)
    monkeypatch.setattr(dry_run, "state_session_factory", lambda settings: (session_factory, "test database"))

    report = dry_run.build_report(hard_settings(), wrapped, now)

    assert report["orders_submitted"] == 0 and report["write_calls_attempted"] == []
    assert gateway.requests == []
    assert report["account"]["trade_mode_label"] == "REAL" and report["account"]["is_real"] is True
    assert report["hard_limits"] == {"max_bot_capital_usd": 3.0, "max_loss_per_trade_usd": 0.10, "max_session_loss_usd": 0.30, "max_daily_trades": 3, "max_open_positions": 1}
    assert report["verdict"] == "REJECT" and "REAL" in report["reason"]
    assert report["selected"] == "EURUSD" and report["selection"]["risk"]["min_lot_risk_usd"] > 0.10

    text = dry_run.render(report)
    for expected in ("MT5 HARD-LIMIT DRY RUN", "detected trade mode     : REAL", "VERDICT: REJECT", "orders submitted: 0", "broker minimum lot", "expected loss at SL", "risk/reward"):
        assert expected in text
