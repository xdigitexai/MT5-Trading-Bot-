"""Database session factory.

Supports PostgreSQL and SQLite. For local DEMO without Docker Postgres, set:
  DATABASE_URL=sqlite:///./forexbot.db
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings

_engine = None
SessionLocal = None


def _ensure_engine():
    global _engine, SessionLocal
    if _engine is None:
        url = get_settings().database_url
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs = {"connect_args": {"check_same_thread": False}}
        _engine = create_engine(url, **kwargs)
        # Ensure tables exist for SQLite local demo
        if url.startswith("sqlite"):
            from app.database.base import Base
            Base.metadata.create_all(_engine)
        SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return SessionLocal


def get_db():
    Session = _ensure_engine()
    db = Session()
    try:
        yield db
    finally:
        db.close()


def __getattr__(name: str):
    if name == "SessionLocal":
        return _ensure_engine()
    raise AttributeError(name)
