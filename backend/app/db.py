"""SQLite: one file holds the catalog, the FTS index and the warehouse tables."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None

FTS_DDL = [
    # External-content FTS5 index over chunks.text, kept in sync by triggers.
    """CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        text, content='chunks', content_rowid='pk', tokenize='porter unicode61')""",
    """CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid, text) VALUES (new.pk, new.text); END""",
    """CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.pk, old.text); END""",
    """CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF text ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.pk, old.text);
        INSERT INTO chunks_fts(rowid, text) VALUES (new.pk, new.text); END""",
]


def _on_connect(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        path = get_settings().sqlite_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})
        event.listen(_engine, "connect", _on_connect)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    from app.catalog import models  # noqa: F401  (register tables)

    engine = get_engine()
    models.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for ddl in FTS_DDL:
            conn.exec_driver_sql(ddl)


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def readonly_connection() -> sqlite3.Connection:
    """A connection that physically cannot write: used for user-supplied SQL."""
    path = get_settings().sqlite_path
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA query_only=ON")
    return conn


def writer_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings().sqlite_path, timeout=30, check_same_thread=False)
    _on_connect(conn, None)
    return conn
