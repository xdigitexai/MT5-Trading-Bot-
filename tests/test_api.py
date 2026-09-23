"""The API must serve what the runtime persisted, and never trade without a token."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.core.config import Settings
from app.database.base import SignalRecord, TradeRecord
from app.database.session import get_db
from app.news.provider import UnavailableNewsProvider
from app.risk.state import RiskStateStore
from app.services.bot import BotService
from app.services.scheduler import MarketScheduler
from app.strategies import STRATEGIES, STRATEGY_NAMES
from conftest import FakeGateway

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PROTECTED_GETS = ("/api/signals", "/api/performance", "/api/statistics", "/api/strategies", "/api/bot/status", "/api/trades", "/api/account", "/api/positions", "/api/orders", "/api/risk")


def api_settings(**overrides) -> Settings:
    values = {"symbols": ["EURUSD"], "news_fail_closed": False, "api_token": TOKEN}
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def client(session_factory):
    """TestClient wired to the in-memory database and a fake gateway through dependency overrides."""
    config = api_settings()
    gateway = FakeGateway()
    scheduler = MarketScheduler(config, gateway, session_factory, news=UnavailableNewsProvider(False))
    bot = BotService(config, gateway, session_factory=session_factory, scheduler=scheduler)
    main.app.dependency_overrides[main.current_settings] = lambda: config
    main.app.dependency_overrides[main.current_bot] = lambda: bot
    main.app.dependency_overrides[main.current_scheduler] = lambda: scheduler

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    main.app.dependency_overrides[get_db] = override_db
    yield TestClient(main.app)
    scheduler.stop()
    main.app.dependency_overrides.clear()


def seed_signal(db, signal_id: str, *, symbol: str = "EURUSD", strategy: str = "trend_following", status: str = "NEW", executed: bool = False, created_at: datetime | None = None) -> SignalRecord:
    row = SignalRecord(
        signal_id=signal_id, symbol=symbol, strategy=strategy, timeframe="M15", direction="BUY",
        confidence=0.8, score=80, entry_price=1.1, stop_loss=1.09, take_profit=1.12, reason="seeded signal",
        created_at=created_at or datetime.now(timezone.utc), executed=executed,
        order_ticket="55501" if executed else None, status=status,
    )
    db.add(row)
    db.commit()
    return row


def seed_closed_trade(db, trade_id: str, profit: float, close_time: datetime) -> TradeRecord:
    row = TradeRecord(
        trade_id=trade_id, symbol="EURUSD", side="BUY", volume=0.1, status="CLOSED", stop_loss=1.09,
        take_profit=1.12, signal_score=80, strategy="trend_following", requested_price=1.1,
        executed_price=1.1, profit=profit, commission=0.0, swap=0.0, close_time=close_time,
        reconciled_at=close_time, close_reason="take_profit",
    )
    db.add(row)
    db.commit()
    return row


def test_health_is_unauthenticated_and_keeps_the_mode_gates(client, monkeypatch):
    # /api/health reports the process-wide configuration, so its premise is pinned here: the suite
    # must not depend on the gates the operator's own .env happens to hold.
    monkeypatch.setattr(main, "settings", api_settings(trading_mode="demo", live_trading_enabled=False))
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "demo" and body["live_orders_permitted"] is False
    assert "mt5" in body and "status" in body


def test_every_protected_route_requires_a_bearer_token(client):
    for path in PROTECTED_GETS:
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401, path
        assert client.get(path, headers=AUTH).status_code == 200, path
    assert client.post("/api/bot/start").status_code == 401
    assert client.post("/api/bot/stop").status_code == 401
    assert client.post("/api/trading/emergency-stop", json={}).status_code == 401
    assert client.post("/api/trading/emergency-reset").status_code == 401


def test_no_route_outside_health_is_anonymous(client):
    def depends_on_auth(node) -> bool:
        stack = [node]
        while stack:
            current = stack.pop()
            if current.call is main.auth:
                return True
            stack.extend(current.dependencies)
        return False

    for route in main.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or not hasattr(route, "dependant"):
            continue
        assert depends_on_auth(route.dependant) if path != "/api/health" else not depends_on_auth(route.dependant), path


def test_signals_returns_persisted_rows_with_filters_and_pagination(client, db):
    now = datetime.now(timezone.utc)
    seed_signal(db, "sig-1", created_at=now - timedelta(minutes=4), status="EXECUTED", executed=True)
    seed_signal(db, "sig-2", symbol="GBPUSD", created_at=now - timedelta(minutes=3))
    seed_signal(db, "sig-3", strategy="breakout", created_at=now - timedelta(minutes=2))
    seed_signal(db, "sig-4", symbol="GBPUSD", strategy="breakout", status="RISK_REJECTED", created_at=now - timedelta(minutes=1))
    seed_signal(db, "sig-5", created_at=now - timedelta(days=10))

    page = client.get("/api/signals", params={"limit": 2}, headers=AUTH).json()
    assert (page["count"], page["total"], page["limit"], page["offset"]) == (2, 5, 2, 0)
    assert [row["signal_id"] for row in page["signals"]] == ["sig-4", "sig-3"]

    second = client.get("/api/signals", params={"limit": 2, "offset": 2}, headers=AUTH).json()
    assert [row["signal_id"] for row in second["signals"]] == ["sig-2", "sig-1"]

    assert [row["signal_id"] for row in client.get("/api/signals", params={"symbol": "gbpusd"}, headers=AUTH).json()["signals"]] == ["sig-4", "sig-2"]
    assert [row["signal_id"] for row in client.get("/api/signals", params={"strategy": "breakout"}, headers=AUTH).json()["signals"]] == ["sig-4", "sig-3"]
    assert [row["signal_id"] for row in client.get("/api/signals", params={"status": "EXECUTED"}, headers=AUTH).json()["signals"]] == ["sig-1"]
    assert [row["signal_id"] for row in client.get("/api/signals", params={"executed": "true"}, headers=AUTH).json()["signals"]] == ["sig-1"]
    assert [row["signal_id"] for row in client.get("/api/signals", params={"executed": "false"}, headers=AUTH).json()["signals"]] == ["sig-4", "sig-3", "sig-2", "sig-5"]

    window = client.get("/api/signals", params={"date_from": (now - timedelta(minutes=3)).isoformat(), "date_to": now.isoformat()}, headers=AUTH).json()
    assert [row["signal_id"] for row in window["signals"]] == ["sig-4", "sig-3", "sig-2"]

    first = page["signals"][0]
    assert first["symbol"] == "GBPUSD" and first["strategy"] == "breakout" and first["status"] == "RISK_REJECTED"
    assert first["executed"] is False and first["stop_loss"] == pytest.approx(1.09) and first["created_at"]


def test_performance_and_statistics_serve_real_analytics(client, db):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    seed_closed_trade(db, "win", 100.0, today + timedelta(minutes=2))
    seed_closed_trade(db, "loss", -40.0, today + timedelta(minutes=1))

    performance = client.get("/api/performance", headers=AUTH).json()
    assert performance["total_trades"] == 2 and performance["winning_trades"] == 1 and performance["losing_trades"] == 1
    assert performance["net_realized_pnl"] == pytest.approx(60.0)
    assert performance["profit_factor"] == pytest.approx(2.5)
    assert performance["state"] == "reconciled closed trades"

    statistics = client.get("/api/statistics", headers=AUTH).json()
    assert statistics["total_closed_trades"] == 2 and statistics["win_rate"] == pytest.approx(0.5)
    assert statistics["today_trades"] == 2 and statistics["today_realized_pnl"] == pytest.approx(60.0)
    assert statistics["by_symbol"]["EURUSD"]["trades"] == 2
    assert statistics["by_strategy"]["trend_following"]["profit_factor"] == pytest.approx(2.5)


def test_analytics_endpoints_report_the_empty_state_instead_of_placeholder_notes(client):
    performance = client.get("/api/performance", headers=AUTH).json()
    statistics = client.get("/api/statistics", headers=AUTH).json()

    assert performance["state"] == "no reconciled closed trades yet" and performance["total_trades"] == 0
    assert performance["profit_factor"] is None and "no reconciled closed trades" in performance["profit_factor_state"]
    assert statistics["state"] == "no reconciled closed trades yet"
    assert statistics["win_rate"] is None and statistics["by_strategy"] == {} and statistics["by_symbol"] == {}


def test_strategies_are_derived_from_the_registry(client):
    body = client.get("/api/strategies", headers=AUTH).json()

    assert body["strategies"] == list(STRATEGY_NAMES)
    assert body["enabled"] == ["trend_following"]
    assert body["ensemble_enabled"] is False
    assert {detail["name"] for detail in body["details"]} == set(STRATEGY_NAMES)
    assert all(detail["implemented"] for detail in body["details"])
    assert body["details"][0]["timeframe"] == "M15"
    assert all(name in STRATEGIES or name == "ensemble" for name in body["strategies"])

    main.app.dependency_overrides[main.current_settings] = lambda: api_settings(enabled_strategies=["breakout", "ensemble"], strategy_timeframes={"breakout": "H1"})
    configured = client.get("/api/strategies", headers=AUTH).json()
    assert configured["enabled"] == ["breakout"] and configured["ensemble_enabled"] is True
    assert [detail["timeframe"] for detail in configured["details"] if detail["name"] == "breakout"] == ["H1"]


def test_bot_status_exposes_the_scheduler_state(client):
    body = client.get("/api/bot/status", headers=AUTH).json()

    assert body["state"] == "STOPPED" and body["mode"] == "demo" and body["live_orders_permitted"] is False
    scheduler = body["scheduler"]
    for field in ("running", "cycles", "last_scan_at", "last_signal_at", "last_execution_at", "last_reconciliation_at", "last_error"):
        assert field in scheduler
    assert scheduler["running"] is False and scheduler["cycles"] == 0


def test_emergency_stop_persists_the_lock_and_blocks_a_restart(client, db):
    assert client.post("/api/bot/start", headers=AUTH).status_code == 200

    response = client.post("/api/trading/emergency-stop", json={"close_positions": False, "cancel_pending_orders": True}, headers=AUTH)
    body = response.json()

    assert response.status_code == 200 and body["status"] == "emergency locked"
    assert body["lock_persisted"] is True and body["scheduler_stopped"] is True
    assert client.get("/api/bot/status", headers=AUTH).json()["state"] == "EMERGENCY_LOCKED"
    assert RiskStateStore(db).load().emergency_locked is True
    assert client.post("/api/bot/start", headers=AUTH).status_code == 409

    reset = client.post("/api/trading/emergency-reset", headers=AUTH)
    assert reset.status_code == 200 and reset.json()["state"] == "STOPPED"
    assert RiskStateStore(db).load().emergency_locked is False
    assert client.post("/api/bot/start", headers=AUTH).status_code == 200
    client.post("/api/bot/stop", headers=AUTH)


def test_bot_start_fails_closed_when_mt5_is_unavailable(client, session_factory):
    config = main.app.dependency_overrides[main.current_bot]()
    config.gateway.connected = False

    response = client.post("/api/bot/start", headers=AUTH)

    assert response.status_code == 409 and "initialization failed" in response.json()["detail"]
    assert client.get("/api/bot/status", headers=AUTH).json()["state"] == "ERROR"
