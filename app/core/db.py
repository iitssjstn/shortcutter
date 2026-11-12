"""Database-engine en sessies (SQLite via SQLAlchemy)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def init_db(db_path: str) -> Engine:
    """Maak (of hermaak) de engine en de tabellen."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # pragma: no cover - triviaal
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    from app.core import models  # noqa: F401  (registreert de tabellen)

    Base.metadata.create_all(engine)
    _migrate(engine)
    _engine = engine
    _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


# Kolommen die later zijn toegevoegd: (tabel, kolom, SQL-type met default)
_ADDED_COLUMNS = [
    ("jobs", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("user_settings", "framing_default", "VARCHAR(16) NOT NULL DEFAULT 'auto'"),
    ("projects", "framing", "VARCHAR(16) NOT NULL DEFAULT 'auto'"),
    ("clips", "framing_choice", "VARCHAR(16)"),
    ("clips", "crop_x", "FLOAT"),
    ("clips", "description", "TEXT NOT NULL DEFAULT ''"),
    ("user_settings", "post_language", "VARCHAR(8) NOT NULL DEFAULT 'en'"),
    ("clips", "hashtags", "VARCHAR(300) NOT NULL DEFAULT ''"),
]


def _migrate(engine: Engine) -> None:
    """Voeg ontbrekende kolommen toe aan een bestaande SQLite-database."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table, col, ddl in _ADDED_COLUMNS:
            if table in insp.get_table_names() and col not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


def get_engine() -> Engine:
    assert _engine is not None, "init_db() is nog niet aangeroepen"
    return _engine


def new_session() -> Session:
    assert _SessionLocal is not None, "init_db() is nog niet aangeroepen"
    return _SessionLocal()


def get_db() -> Iterator[Session]:
    """FastAPI-dependency."""
    db = new_session()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = new_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
