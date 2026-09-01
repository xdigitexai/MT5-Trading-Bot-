from types import SimpleNamespace
from app.core.config import Settings, TradingMode
from app.risk.position_audit import audit_position


def test_volume_cap_violation_detected():
    settings = Settings(trading_mode=TradingMode.DEMO, live_trading_enabled=False, max_demo_volume=0.10)
    pos = SimpleNamespace(
        ticket=1, type=0, price_open=1.1, volume=12.46, sl=1.09, tp=1.12,
        profit=-100.0, price_current=1.095, magic=260825, comment="bot", symbol="EURGBP",
    )
    snap = audit_position(pos, equity=100_000, settings=settings)
    assert snap.violation is not None
    assert "demo_volume_cap" in snap.violation
