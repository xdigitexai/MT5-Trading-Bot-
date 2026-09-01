"""Trading session classification (UTC/GMT hours)."""
from __future__ import annotations

from datetime import datetime, timezone


def session_name(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    h = ts.astimezone(timezone.utc).hour
    if 13 <= h < 16:
        return "London_NY_Overlap"
    if 8 <= h < 16:
        return "London"
    if 13 <= h < 21:
        return "New_York"
    if 7 <= h < 15:
        return "Frankfurt"
    if 23 <= h or h < 7:
        return "Tokyo"
    if 21 <= h or h < 5:
        return "Sydney"
    return "Off_Hours"
