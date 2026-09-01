from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings

_engine = None
SessionLocal = None


def _ensure_engine():
    global _engine, SessionLocal
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
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
