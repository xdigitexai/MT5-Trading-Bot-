"""Timeframe names to MT5 timeframe constants and durations.

MetaTrader5 exposes its timeframe enums as module attributes that only exist on a Windows host
with the package installed, so the documented numeric values are mirrored here for building
``copy_rates_from_pos`` calls and for fake-gateway tests (the same approach as app.mt5.constants).
An unknown name returns ``None`` and the caller must treat that as "candles cannot be fetched".
"""
TIMEFRAMES: dict[str, int] = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M6": 6, "M10": 10, "M12": 12, "M15": 15, "M20": 20, "M30": 30,
    "H1": 16385, "H2": 16386, "H3": 16387, "H4": 16388, "H6": 16390, "H8": 16392, "H12": 16396,
    "D1": 16408, "W1": 32769, "MN1": 49153,
}
TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M6": 6, "M10": 10, "M12": 12, "M15": 15, "M20": 20, "M30": 30,
    "H1": 60, "H2": 120, "H3": 180, "H4": 240, "H6": 360, "H8": 480, "H12": 720,
    "D1": 1440, "W1": 10080, "MN1": 43200,
}


def normalise(name: str | None) -> str:
    return str(name or "").strip().upper()


def mt5_timeframe(name: str | None) -> int | None:
    return TIMEFRAMES.get(normalise(name))


def timeframe_name(value: int | None) -> str | None:
    for name, code in TIMEFRAMES.items():
        if code == value:
            return name
    return None


def timeframe_minutes(name: str | None) -> int | None:
    return TIMEFRAME_MINUTES.get(normalise(name))


def timeframe_seconds(name: str | None) -> int | None:
    minutes = timeframe_minutes(name)
    return None if minutes is None else minutes * 60
