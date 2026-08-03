"""Database helpers ringan — backend-aware (sqlite lokal / postgres VPS).

Lokal (dev): get_conn() → sqlite3.Connection sync (query ad-hoc / tes).
VPS (prod) : pakai app.jobs.PostgresJobStore / asyncpg langsung (worker service
             mengelola pool-nya sendiri di app/worker.py).
init_db()  : memastikan skema job queue (tabel `jobs`) ada sesuai backend aktif.
"""

from __future__ import annotations

import sqlite3

from app.config import settings


def _parse_sqlite_dsn(dsn: str) -> str:
    """Parse DSN sqlite ke path file.

    sqlite:///./data/x.db   → './data/x.db'   (relatif)
    sqlite:////abs/x.db     → '/abs/x.db'     (absolut)
    sqlite:///:memory:      → ':memory:'
    """
    prefix = "sqlite:///"
    if not dsn.startswith(prefix):
        raise ValueError(f"dsn bukan sqlite: {dsn!r}")
    return dsn[len(prefix):]


def get_conn() -> sqlite3.Connection:
    """Koneksi sqlite sync utk backend lokal. Postgres → pakai PostgresJobStore."""
    if not settings.database_url.startswith("sqlite:///"):
        raise NotImplementedError(
            "get_conn() hanya utk sqlite lokal — postgres pakai app.jobs.PostgresJobStore"
        )
    conn = sqlite3.connect(_parse_sqlite_dsn(settings.database_url), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


async def init_db() -> None:
    """Pastikan skema `jobs` ada sesuai backend aktif (idempotent)."""
    from app.jobs import get_job_store  # lazy: hindari circular import (jobs.py import settings)

    await get_job_store(settings).init()
