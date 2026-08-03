"""Job store layer — abstraksi DUAL-BACKEND untuk antrian job FactBot.

Lokal (dev):  SqliteJobStore   — sqlite3 + threading.Lock, file dari
               settings.database_url (sqlite:///...). Single-process, tanpa infra.
VPS (prod):   PostgresJobStore — asyncpg, REUSE pola SQL yang sudah ada di
               app/worker.py baris 40-87 (SCHEMA_SQL, CLAIM_SQL SKIP LOCKED,
               REQUEUE) supaya job yang dibuat webhook langsung dikonsumsi oleh
               service worker existing (kind='verify_media', payload JSONB).

Pemakaian dari webhook (async):
    store = get_job_store(settings)
    job_id = await store.create_job({...})   # raise JobExistsError kalau report_id duplikat

Semua method async — webhook handler dan worker sama-sama async. Asyncpg di-import
lazy supaya modul ini tetap bisa di-import tanpa asyncpg (backend sqlite / tes).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.db import _parse_sqlite_dsn

logger = logging.getLogger("factbot.jobs")

# --- Konstanta (TANPA magic number: semua threshold lewat Settings/konstanta bernama) ---
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
JOB_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED})

DEFAULT_MAX_ATTEMPTS = 3          # sama dengan default skema worker.py
KIND_VERIFY_MEDIA = "verify_media"  # satu-satunya kind yang dipahami worker.py

# Field yang boleh di-update via update() — whitelist anti SQL injection via nama kolom
_ALLOWED_UPDATE_FIELDS = frozenset(
    {"status", "attempts", "max_attempts", "last_error", "public_url"}
)
_JOB_COLUMNS = (
    "id", "report_id", "platform", "media_url", "media_title", "media_id",
    "claim_text", "sender_id", "status", "attempts", "max_attempts",
    "last_error", "public_url", "created_at", "updated_at",
)
_SELECT_JOB = "SELECT " + ", ".join(_JOB_COLUMNS) + " FROM jobs"

# Skema lokal (sqlite). Index partial: hanya job 'queued' yang dipertimbangkan claim.
SQLITE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    report_id    TEXT NOT NULL UNIQUE,
    platform     TEXT NOT NULL DEFAULT '',
    media_url    TEXT NOT NULL DEFAULT '',
    media_title  TEXT NOT NULL DEFAULT '',
    media_id     TEXT NOT NULL DEFAULT '',
    claim_text   TEXT NOT NULL DEFAULT '',
    sender_id    TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error   TEXT,
    public_url   TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_pick ON jobs (status, created_at) WHERE status = 'queued';
"""


def _now_iso() -> str:
    """Timestamp UTC ISO-8601, fixed-width → urutan leksikografis == kronologis."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_report_id(platform: str, media_id: str = "", media_url: str = "") -> str:
    """Report ID idempotent (pola worker.py: {platform}_{media_id}).

    Fallback deterministic: hash URL kalau media_id belum tersedia (DM caption-only),
    terakhir uuid acak. Report_id UNIQUE → create_job duplikat = JobExistsError.
    """
    if media_id:
        return f"{platform}_{media_id}"
    if media_url:
        digest = hashlib.sha1(media_url.encode("utf-8")).hexdigest()
        return f"{platform}_{digest}"
    return f"{platform}_{uuid.uuid4().hex}"


class JobExistsError(Exception):
    """create_job() dipanggil dengan report_id yang sudah ada (UNIQUE → guard idempotent)."""


class JobStore(ABC):
    """Kontrak job store — implementasi: SqliteJobStore (lokal) & PostgresJobStore (VPS)."""

    @abstractmethod
    async def init(self) -> None:
        """Pastikan skema (tabel jobs) ada — idempotent, aman dipanggil berkali-kali."""

    @abstractmethod
    async def create_job(self, job: dict) -> str:
        """Simpan job baru → return job id (str). Raise JobExistsError kalau report_id duplikat."""

    @abstractmethod
    async def claim_next(self) -> dict | None:
        """Ambil 1 job 'queued' secara ATOMIK → status 'running'. None kalau kosong."""

    @abstractmethod
    async def update(self, job_id: str, **fields: Any) -> None:
        """Update field whitelisted: status|attempts|max_attempts|last_error|public_url."""

    @abstractmethod
    async def get(self, job_id: str) -> dict | None:
        """Ambil job by id — None kalau tidak ada."""

    @abstractmethod
    async def requeue_stale(self, stale_seconds: float) -> int:
        """Requeue job 'running' yang updated_at-nya lebih lama dari stale_seconds.
        Return jumlah job yang di-requeue (crash recovery)."""


class SqliteJobStore(JobStore):
    """Backend lokal: sqlite3 + threading.Lock (single-process). DSN: sqlite:///<path>.

    Catatan: operasi sqlite pendek & sinkron — aman untuk beban dev/MVP. Untuk
    produksi multi-process pakai PostgresJobStore (SKIP LOCKED).
    """

    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or settings.database_url
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError(
                f"SqliteJobStore butuh dsn 'sqlite:///...' — dapat: {self.database_url!r}"
            )
        self._path = _parse_sqlite_dsn(self.database_url)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _ensure_conn(self) -> sqlite3.Connection:
        """Buka koneksi sekali (lazy) + pastikan skema. Dipanggil di dalam self._lock."""
        if self._conn is None:
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            if self._path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SQLITE_SCHEMA_SQL)
            conn.commit()
            self._conn = conn
        return self._conn

    async def init(self) -> None:
        with self._lock:
            self._ensure_conn()

    async def create_job(self, job: dict) -> str:
        job_id = job.get("id") or uuid.uuid4().hex
        report_id = job["report_id"]
        now = _now_iso()
        with self._lock:
            conn = self._ensure_conn()
            try:
                conn.execute(
                    "INSERT INTO jobs (id, report_id, platform, media_url, media_title, "
                    "media_id, claim_text, sender_id, status, attempts, max_attempts, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (
                        job_id,
                        report_id,
                        job.get("platform", ""),
                        job.get("media_url", ""),
                        job.get("media_title", ""),
                        job.get("media_id", ""),
                        job.get("claim_text", ""),
                        job.get("sender_id", ""),
                        STATUS_QUEUED,
                        int(job.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
                        now,
                        now,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise JobExistsError(
                    f"report_id {report_id!r} sudah ada (UNIQUE) — job idempotent"
                ) from exc
        return job_id

    async def claim_next(self) -> dict | None:
        """Claim atomik: kunci thread + BEGIN IMMEDIATE (padanan SKIP LOCKED utk sqlite)."""
        with self._lock:
            conn = self._ensure_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    _SELECT_JOB + " WHERE status = ? ORDER BY created_at, id LIMIT 1",
                    (STATUS_QUEUED,),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                job_id = row["id"]
                conn.execute(
                    "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                    (STATUS_RUNNING, _now_iso(), job_id),
                )
                conn.commit()
                # Baca ulang dalam txn yg sama → kembalikan state POST-update (seperti RETURNING)
                claimed = conn.execute(_SELECT_JOB + " WHERE id = ?", (job_id,)).fetchone()
                return dict(claimed)
            except Exception:
                conn.rollback()
                raise

    async def update(self, job_id: str, **fields: Any) -> None:
        unknown = set(fields) - _ALLOWED_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"field update tak dikenal: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in JOB_STATUSES:
            raise ValueError(f"status invalid: {fields['status']!r} (pilih {sorted(JOB_STATUSES)})")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        params = [fields[name] for name in fields] + [_now_iso(), job_id]
        with self._lock:
            conn = self._ensure_conn()
            conn.execute(f"UPDATE jobs SET {assignments}, updated_at = ? WHERE id = ?", params)
            conn.commit()

    async def get(self, job_id: str) -> dict | None:
        with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(_SELECT_JOB + " WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row is not None else None

    async def requeue_stale(self, stale_seconds: float) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat(
            timespec="milliseconds"
        )
        with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? "
                "WHERE status = ? AND updated_at < ?",
                (STATUS_QUEUED, _now_iso(), STATUS_RUNNING, cutoff),
            )
            conn.commit()
            return cur.rowcount or 0


class PostgresJobStore(JobStore):
    """Backend VPS: asyncpg — REUSE pola SQL app/worker.py (SCHEMA_SQL, CLAIM_SQL
    SKIP LOCKED, REQUEUE_STALE_SQL) supaya job dari webhook langsung dikonsumsi
    service worker existing. Pool dibuat lazy + retry bounded (DB bisa belum siap
    saat container start). Asyncpg di-import lazy — hanya wajib utk backend postgres.
    """

    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or settings.database_url
        if not (
            self.database_url.startswith("postgresql")
            or self.database_url.startswith("postgres://")
        ):
            raise ValueError(
                f"PostgresJobStore butuh dsn postgres — dapat: {self.database_url!r}"
            )
        self._pool: Any = None
        self._claim_sql: str | None = None
        self._requeue_stale_sql: str | None = None

    def _dsn(self) -> str:
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            import asyncpg  # lazy: hanya dibutuhkan utk backend postgres
            from app.worker import CLAIM_SQL, REQUEUE_STALE_SQL, SCHEMA_SQL  # pola SQL existing

            self._claim_sql = CLAIM_SQL
            self._requeue_stale_sql = REQUEUE_STALE_SQL.rstrip().rstrip(";") + " RETURNING id"

            delay = 1.0
            last_exc: Exception | None = None
            for _ in range(3):  # retry bounded — pola _connect_with_retry di worker.py
                try:
                    pool = await asyncpg.create_pool(self._dsn(), min_size=1, max_size=4)
                    await pool.execute(SCHEMA_SQL)
                    self._pool = pool
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5.0)
            if self._pool is None:
                raise RuntimeError(f"Postgres unreachable: {last_exc}") from last_exc
        return self._pool

    async def init(self) -> None:
        await self._ensure_pool()

    async def create_job(self, job: dict) -> str:
        import asyncpg

        pool = await self._ensure_pool()
        payload = {
            "platform": job.get("platform", ""),
            "media_url": job.get("media_url", ""),
            "media_title": job.get("media_title", ""),
            "media_id": job.get("media_id", ""),
            "claim_text": job.get("claim_text", ""),
            "sender_id": job.get("sender_id", ""),
        }
        try:
            row = await pool.fetchrow(
                "INSERT INTO jobs (kind, report_id, payload, max_attempts) "
                "VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
                job.get("kind", KIND_VERIFY_MEDIA),
                job["report_id"],
                json.dumps(payload, ensure_ascii=False),
                int(job.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise JobExistsError(
                f"report_id {job['report_id']!r} sudah ada (UNIQUE) — job idempotent"
            ) from exc
        return str(row["id"])

    async def claim_next(self) -> dict | None:
        pool = await self._ensure_pool()
        claimer = f"jobstore-{os.getpid()}"  # identitas claimer (kolom worker di skema)
        row = await pool.fetchrow(self._claim_sql, claimer)
        if row is None:
            return None
        data = dict(row)
        payload = data.pop("payload", None)
        if isinstance(payload, dict):
            data.update(payload)
        return data

    async def update(self, job_id: str, **fields: Any) -> None:
        pool = await self._ensure_pool()
        unknown = set(fields) - _ALLOWED_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"field update tak dikenal: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in JOB_STATUSES:
            raise ValueError(f"status invalid: {fields['status']!r}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ${i}" for i, name in enumerate(fields, start=1))
        params = [fields[name] for name in fields] + [job_id]
        await pool.execute(
            f"UPDATE jobs SET {assignments}, updated_at = now() "
            f"WHERE id = ${len(params)}::bigint",
            *params,
        )

    async def get(self, job_id: str) -> dict | None:
        pool = await self._ensure_pool()
        row = await pool.fetchrow("SELECT * FROM jobs WHERE id = $1::bigint", job_id)
        if row is None:
            return None
        data = dict(row)
        payload = data.pop("payload", None)
        if isinstance(payload, dict):
            data.update(payload)
        return data

    async def requeue_stale(self, stale_seconds: float) -> int:
        pool = await self._ensure_pool()
        rows = await pool.fetch(self._requeue_stale_sql, timedelta(seconds=stale_seconds))
        return len(rows)


# --- Factory: pilih backend dari settings.database_url ---
_STORE_CACHE: dict[str, JobStore] = {}


def get_job_store(cfg: Any = None) -> JobStore:
    """Factory job store — sqlite:// → SqliteJobStore, postgresql:// → PostgresJobStore.

    Instance di-cache per database_url (singleton per proses) supaya webhook tidak
    membuka koneksi baru tiap pesan. `cfg` boleh Settings atau objek ber-attribut
    database_url (memudahkan tes hermetic).
    """
    cfg = cfg if cfg is not None else settings
    url = cfg.database_url
    cached = _STORE_CACHE.get(url)
    if cached is not None:
        return cached
    if url.startswith("sqlite:///"):
        store: JobStore = SqliteJobStore(url)
    elif url.startswith("postgresql") or url.startswith("postgres://"):
        store = PostgresJobStore(url)
    else:
        raise ValueError(
            f"database_url tak didukung: {url!r} — pakai 'sqlite:///...' atau 'postgresql://...'"
        )
    _STORE_CACHE[url] = store
    return store
