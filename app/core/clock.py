"""UTC time helpers and MT5 time coercion.

Storage round-trips through SQLite (and some drivers) hand back naive datetimes even for
timezone-aware columns, so every timestamp that leaves the database is normalised through
``as_utc`` before it is compared. Naive values are read as UTC because everything the bot writes
is UTC. MT5 reports seconds since the epoch, which ``epoch_to_utc`` converts.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def epoch_to_utc(value: object) -> datetime | None:
    """MT5 timestamps (int/float epoch seconds or datetime) as an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    moment = as_utc(value)
    return moment.isoformat() if moment is not None else None
