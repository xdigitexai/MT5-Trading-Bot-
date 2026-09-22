"""Position sizing must always be derived from risk, clamped down, and fail closed."""
import pytest

from app.risk.sizing import (
    SymbolSpec,
    margin_within_free_margin,
    min_stop_distance,
    normalize_volume,
    position_risk,
    position_size,
    required_margin,
    risk_per_lot,
    spec_from_symbol_info,
)
from conftest import FakeSymbolInfo


def spec(**overrides) -> SymbolSpec:
    base = dict(volume_min=0.01, volume_max=100.0, volume_step=0.01, tick_value=1.0, tick_size=0.00001, point=0.00001, trade_stops_level=0, freeze_level=0, contract_size=100_000.0, margin_per_lot=0.0, digits=5, filling_mode=1)
    base.update(overrides)
    return SymbolSpec(**base)


def test_lots_are_derived_from_the_risk_budget():
    # 10 000 equity at 0.5% risks 50; a 0.01 stop is 1 000 ticks worth 1.0 each = 1 000 per lot.
    assert risk_per_lot(1.1, 1.09, spec()) == pytest.approx(1000.0)
    assert position_size(10_000, 0.5, 1.1, 1.09, spec()) == 0.05
    assert position_size(50_000, 0.5, 1.1, 1.09, spec()) == 0.25


def test_size_rounds_down_to_volume_step_and_never_up():
    lots = position_size(10_000, 0.5, 1.1, 1.0969, spec())
    assert lots == 0.16  # 50 / 310 = 0.1612 -> floored to the step
    assert lots * risk_per_lot(1.1, 1.0969, spec()) <= 50


def test_size_respects_volume_min_and_max():
    assert normalize_volume(0.009, spec()) is None
    assert position_size(100, 0.5, 1.1, 1.09, spec()) is None  # 0.0005 lots is below volume_min
    assert position_size(10_000_000, 2.0, 1.1, 1.09, spec()) == 100.0
    assert normalize_volume(1_000.0, spec()) == 100.0


def test_size_fails_closed_on_bad_stop_distance():
    assert position_size(10_000, 0.5, 1.1, 1.1, spec()) is None
    assert position_size(10_000, 0.5, 1.1, 0.0, spec()) is None
    assert position_size(10_000, 0.5, 1.1, None, spec()) is None
    assert position_size(10_000, 0.5, None, 1.09, spec()) is None
    assert position_size(0, 0.5, 1.1, 1.09, spec()) is None
    assert position_size(10_000, 0, 1.1, 1.09, spec()) is None


def test_size_fails_closed_without_usable_symbol_properties():
    assert risk_per_lot(1.1, 1.09, spec(tick_value=0.0, tick_size=0.0)) == pytest.approx(1000.0)  # contract-size fallback
    assert risk_per_lot(1.1, 1.09, spec(tick_value=0.0, tick_size=0.0, contract_size=0.0)) is None
    assert risk_per_lot(1.1, 1.09, spec(tick_value=0.0)) == pytest.approx(1000.0)  # contract-size fallback
    assert risk_per_lot(1.1, 1.09, spec(tick_value=0.0, contract_size=0.0)) is None
    assert position_size(10_000, 0.5, 1.1, 1.09, spec(tick_value=0.0, tick_size=0.0, contract_size=0.0)) is None
    assert position_size(10_000, 0.5, 1.1, 1.09, spec(volume_step=0.0)) is None
    assert position_size(10_000, 0.5, 1.1, 1.09, spec(volume_min=0.0)) is None


def test_size_fails_closed_when_margin_exceeds_free_margin():
    # 0.05 lots of EURUSD at 1.1 with 1:100 leverage needs 55 of margin.
    assert required_margin(0.05, spec(), 1.1, 100.0) == 55.0
    assert position_size(10_000, 0.5, 1.1, 1.09, spec(), free_margin=10.0, leverage=100.0) is None
    assert position_size(10_000, 0.5, 1.1, 1.09, spec(), free_margin=100.0, leverage=100.0) == 0.05
    assert position_size(10_000, 0.5, 1.1, 1.09, spec(), free_margin=100.0, leverage=0.0) is None


def test_margin_helpers_fail_closed():
    assert required_margin(0.1, spec(margin_per_lot=10.0), 1.1, 100.0) == 1.0
    assert required_margin(0.1, spec(contract_size=0.0), 1.1, 0.0) is None
    assert required_margin(0.0, spec(), 1.1, 100.0) is None
    assert margin_within_free_margin(None, 1_000.0) is False
    assert margin_within_free_margin(100.0, None) is False
    assert margin_within_free_margin(100.0, 0.0) is False
    assert margin_within_free_margin(100.0, 1_000.0) is True
    assert margin_within_free_margin(950.0, 1_000.0, buffer_pct=10) is False


def test_risk_budget_is_never_exceeded_and_the_next_step_would_exceed_it():
    for equity, distance in ((10_000, 0.0113), (25_000, 0.0042), (5_000, 0.02)):
        s = spec()
        budget = equity * 0.5 / 100
        lots = position_size(equity, 0.5, 1.1, 1.1 - distance, s)
        assert lots is not None and lots >= s.volume_min
        assert position_risk(lots, 1.1, 1.1 - distance, s) <= budget + 1e-9
        assert position_risk(lots + s.volume_step, 1.1, 1.1 - distance, s) > budget


def test_min_stop_distance_uses_the_broker_levels():
    assert min_stop_distance(spec(trade_stops_level=20)) == 20 * 0.00001
    assert min_stop_distance(spec(trade_stops_level=20, freeze_level=50)) == 50 * 0.00001
    assert min_stop_distance(spec(point=0.0)) == 0.0


def test_spec_from_symbol_info_reads_broker_fields_and_rejects_unusable_specs():
    s = spec_from_symbol_info(FakeSymbolInfo())
    assert (s.volume_min, s.volume_step, s.tick_value, s.tick_size, s.digits, s.filling_mode) == (0.01, 0.01, 1.0, 0.00001, 5, 1)
    assert spec_from_symbol_info(None) is None
    assert spec_from_symbol_info(FakeSymbolInfo(point=0.0)) is None
    assert spec_from_symbol_info(FakeSymbolInfo(volume_step=0.0)) is None
    assert spec_from_symbol_info(FakeSymbolInfo(volume_min=1.0, volume_max=0.5)) is None
