"""FactBot worker — dual-backend job loop via app.jobs.JobStore.

Jalankan:
    python -m app.worker              # loop utama (service bot-worker di compose)
    python -m app.worker --health     # healthcheck: exit 0 bila store bisa init

Queue disediakan oleh app.jobs.get_job_store(settings) — implementasi
PostgreSQL (SKIP LOCKED) & sqlite (lihat app/jobs.py). Skema SQL Postgres
(SCHEMA_SQL, CLAIM_SQL, REQUEUE_*) DIPERTAHANKAN di sini — PostgresJobStore
di app/jobs.py meng-import pola SQL tersebut. Store sqlite punya skema flat
(tanpa kolom kind/payload) → worker membaca field payload ATAU field flat
dari baris job (dual-read, lihat _payload_of/handle_job).

Pipeline per job (PHASE 1 MVP — caption-only, TANPA download video):
    1. app.pipeline.analyzer.run_analysis(job)   → {"verdict": dict, "markdown": str}
    2. app.api.factbot.create_report(...)        → public_url (idempotent via report_id)
    3. store.update(job_id, status='done', public_url=..., attempts+1)
    4. app.api.reply.send_result_dm(sender_id, url) → kirim hasil ke DM user

Error handling kelas (docs/pipeline_video_analysis.md §5):
    * LLMConfigError / UploadConfigError → status 'failed' LANGSUNG + pesan maaf
    * UploadTransientError / LLMError / network → retry attempts+1;
      attempts >= max_attempts (Settings.worker_max_attempts) → 'failed' + pesan.

Backoff pacing worker-side: store.update() hanya menerima whitelist
(status|attempts|max_attempts|last_error|public_url — TANPA run_after), jadi
jadwal retry disimpan in-memory di `_backoff` (job_id → deadline monotonic).
Job yang di-claim sebelum deadline-nya dilepas kembali ke queue (status
'queued') tanpa diproses. Pada multi-worker, worker lain bisa saja memproses
lebih cepat — aman: attempts tetap bertambah & dibatasi max_attempts.

Semua timeout/ambang dari Settings (app/config.py) — TANPA magic number.
Import analyzer/factbot/reply/llm LAZY di dalam fungsi → hindari circular
import & memungkinkan modul dibangun bertahap.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import socket
import sys
import time as _time
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:  # pragma: no cover — hanya utk type-check; import runtime LAZY
    from app.pipeline.progress import ProgressNotifier

logger = logging.getLogger("factbot.worker")

POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", settings.worker_poll_seconds))
CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", settings.worker_concurrency))
HEARTBEAT_SECONDS = float(os.getenv("WORKER_HEARTBEAT_SECONDS", settings.worker_heartbeat_seconds))
HEARTBEAT_STALE = float(os.getenv("WORKER_HEARTBEAT_STALE_SECONDS", settings.worker_heartbeat_stale_seconds))
RUNNING_STALE_SECONDS = 600.0  # job 'running' lebih lama dari ini → di-requeue (crash recovery)

# --- Skema queue (kanonik; PostgresJobStore di app/jobs.py meng-import ini) ---
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,                  -- 'verify_media' | ...
    report_id   TEXT NOT NULL UNIQUE,           -- idempotent: {platform}_{media_id}
    payload     JSONB NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'queued', -- queued|running|done|failed
    attempts    INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    run_after   TIMESTAMPTZ NOT NULL DEFAULT now(),
    worker      TEXT,
    last_error  TEXT,
    public_url  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_pick
    ON jobs (status, run_after) WHERE status = 'queued';
CREATE TABLE IF NOT EXISTS workers (
    worker_id   TEXT PRIMARY KEY,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Claim job secara ATOMIK: UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED)
CLAIM_SQL = """
UPDATE jobs SET status='running', worker=$1, updated_at=now()
WHERE id = (
    SELECT id FROM jobs
    WHERE status='queued' AND run_after <= now()
    ORDER BY id LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id, kind, report_id, payload, attempts, max_attempts;
"""

# Crash recovery: job 'running' yang terlalu lama / worker-nya mati → requeue
REQUEUE_STALE_SQL = """
UPDATE jobs SET status='queued', run_after=now(), worker=NULL, updated_at=now()
WHERE status='running' AND updated_at < now() - $1::interval;
"""
REQUEUE_DEAD_WORKER_SQL = """
UPDATE jobs SET status='queued', run_after=now(), worker=NULL, updated_at=now()
WHERE status='running' AND worker IN (
    SELECT worker_id FROM workers
    WHERE last_seen < now() - ($1::text || ' seconds')::interval
);
"""

# --- Pesan DM ke user ---
RESULT_REPLY = "✅ Verification complete! Here's your result: {url} 🔍"
FAILED_REPLY = "😔 Sorry, verification failed for your request — please try again later. 🙏"
PROGRESS_START_REPLY = "🔄 Analyzing your content... This usually takes 30–60 seconds. I will send the result here."
PROGRESS_SLOW_REPLY = "⏳ Still working on it — the AI is processing your content. Hang tight!"
RETRY_REPLY = "⚠️ Verification hit a temporary issue — retrying now. No action needed from you."

# --- Backoff in-memory: job_id -> monotonic deadline (retry pacing worker-side) ---
_backoff: dict = {}


class JobTransientError(Exception):
    """Error sementara (timeout, 5xx, rate-limit) → retry dgn backoff."""


class JobPermanentError(Exception):
    """Error permanen (konfigurasi salah) → langsung failed."""


def _get_store():
    """Lazy import app.jobs — menghindari circular import.

    Kontrak JobStore (app/jobs.py): init / create_job / claim_next / update /
    get / requeue_stale. update() menerima whitelist: status|attempts|
    max_attempts|last_error|public_url (TANPA run_after).
    """
    from app.jobs import get_job_store

    return get_job_store(settings)


def _payload_of(job: dict) -> dict:
    """Field payload job → dict.

    PG: baris berisi kolom payload (jsonb — bisa str dari asyncpg) + field
    payload di-merge store. Sqlite: TANPA kolom payload — field flat
    (platform/media_url/.../sender_id) langsung di baris. Return {} bila
    tidak ada payload (caller fallback ke field flat job).
    """
    payload = job.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return payload if isinstance(payload, dict) else {}


async def _notify_user(job, message: str) -> None:
    """Kirim DM ke sender (payload.sender_id ATAU field flat) — best effort."""
    payload = _payload_of(job)
    sender_id = payload.get("sender_id") or job.get("sender_id")
    if not sender_id:
        logger.warning("job %s: tidak ada sender_id di job — DM tidak dikirim", job.get("id"))
        return
    try:
        from app.api.reply import send_result_dm  # lazy — hindari circular

        await send_result_dm(str(sender_id), message)
    except Exception as e:  # noqa: BLE001
        logger.error("job %s: gagal kirim DM ke %s: %s", job.get("id"), sender_id, e)


async def _mark_done(store, job, public_url: str | None = None) -> None:
    await store.update(
        job["id"],
        status="done",
        public_url=public_url,
        last_error=None,
        attempts=job.get("attempts", 0) + 1,
    )


# --- Progress DM (ProgressNotifier — app/pipeline/progress.py) ---
def _make_progress(job) -> ProgressNotifier | None:
    """Buat ProgressNotifier utk satu job — best effort.

    Lazy import app.pipeline.progress + app.api.reply.send_progress_dm
    (hindari circular import & modul dibangun bertahap). Return None bila
    job tanpa sender_id / import gagal / konstruksi gagal → progress DM
    non-aktif. Progress TIDAK boleh menghentikan pipeline utama.
    """
    payload = _payload_of(job)
    sender_id = payload.get("sender_id") or job.get("sender_id")
    if not sender_id:
        return None
    try:
        from app.pipeline.progress import ProgressNotifier  # lazy — modul paralel
        from app.api.reply import send_progress_dm  # lazy — hindari circular
    except Exception:  # noqa: BLE001
        logger.exception(
            "job %s: progress import gagal — progress DM non-aktif", job.get("id")
        )
        return None
    try:
        return ProgressNotifier(str(sender_id), str(job.get("id")), send_fn=send_progress_dm)
    except Exception:  # noqa: BLE001
        logger.exception(
            "job %s: progress konstruksi gagal — progress DM non-aktif", job.get("id")
        )
        return None


async def _progress_send(progress, phase: str, text: str, *, force: bool = False) -> None:
    """Kirim DM progress — best effort; error progress tidak boleh crash pipeline.

    ``force=True`` dipakai pesan penting (retry/error) supaya selalu lolos
    gate interval — user harus tau ada kendala, bukan dibiarkan sunyi.
    """
    if progress is None:
        return
    try:
        await progress.send(phase, text, force=force)
    except Exception:  # noqa: BLE001
        logger.exception("job progress %s gagal (diabaikan)", phase)


async def _progress_send_if_slow(progress, phase: str, text: str) -> None:
    """Kirim DM progress bila proses lambat — best effort (aman, sama dgn _progress_send)."""
    if progress is None:
        return
    try:
        await progress.send_if_slow(phase, text)
    except Exception:  # noqa: BLE001
        logger.exception("job progress %s gagal (diabaikan)", phase)


async def handle_job(store, job, progress=None) -> None:
    """Dispatcher job → pipeline verify_media (caption-only MVP).

    progress: ProgressNotifier | None dari process_job — DM update bertahap
    ke user (best effort; error progress tidak menghentikan pipeline).
    """
    kind = job.get("kind") or "verify_media"  # sqlite: tanpa kolom kind → default
    logger.info("job %s (%s) mulai: %s", job.get("id"), kind, job.get("report_id"))

    if kind != "verify_media":
        logger.warning("job %s: kind tak dikenal %r — tandai done", job.get("id"), kind)
        await _mark_done(store, job)
        return

    report_id = job.get("report_id")
    if not report_id:
        raise JobPermanentError(f"job {job.get('id')}: report_id kosong")

    # Dual-read: payload (PG) ATAU field flat (sqlite)
    payload = _payload_of(job)
    sender_id = payload.get("sender_id") or job.get("sender_id")
    platform = payload.get("platform") or job.get("platform") or "instagram"
    media_url = payload.get("media_url") or job.get("media_url") or ""
    name = payload.get("name") or job.get("name") or sender_id or "User"

    # 0. PROGRESS — kabari user analisa dimulai (best effort).
    #    HANYA di attempt pertama (attempts==0): retry JANGAN kirim "start" lagi
    #    (anti-duplikat "🔄 Analyzing..." — dedup ProgressNotifier per-attempt
    #    tidak berlaku lintas retry karena instance dibuat baru di process_job).
    if job.get("attempts", 0) < 1:
        await _progress_send(progress, "start", PROGRESS_START_REPLY)

    # 1. ANALYZE — LLM verdict + markdown (lazy import)
    from app.pipeline.analyzer import run_analysis  # lazy
    try:
        from app.pipeline.llm import LLMConfigError  # home asli kelas config error
    except ImportError:
        LLMConfigError = JobPermanentError  # fallback defensif
    try:
        result = await run_analysis(job)
        verdict = result["verdict"]  # run_analysis → {"verdict": dict, "markdown": str}
        markdown = result["markdown"]
    except LLMConfigError as e:
        raise JobPermanentError(f"LLM config error: {e}") from e

    # 1b. PROGRESS — analisa selesai; kalau lambat, kirim update "masih bekerja"
    # (send_if_slow handle keputusan kirim/tidak — tidak ada DM ekstra utk analisa cepat)
    await _progress_send_if_slow(progress, "slow", PROGRESS_SLOW_REPLY)

    # 2. UPLOAD — create_report → public_url (idempotent; 409 → reuse)
    from app.api.factbot import create_report, UploadConfigError, UploadTransientError  # lazy
    try:
        public_url = await create_report(
            verdict=verdict,
            markdown=markdown,
            report_id=report_id,
            platform=platform,
            media_url=media_url,
            name=name,
        )
    except UploadConfigError as e:
        raise JobPermanentError(str(e)) from e
    except UploadTransientError as e:
        raise JobTransientError(str(e)) from e  # retryable — ditangani process_job

    # 3. DONE — simpan public_url
    await _mark_done(store, job, public_url=public_url)

    # 4. DM — kirim hasil ke user (best effort)
    await _notify_user(job, RESULT_REPLY.format(url=public_url))


async def _fail(store, job, error: str, permanent: bool = False) -> bool:
    """Update job setelah error. Return True bila job masuk status 'failed'."""
    attempts = job.get("attempts", 0) + 1
    max_attempts = int(job.get("max_attempts") or settings.worker_max_attempts)
    if permanent or attempts >= max_attempts:
        await store.update(job["id"], status="failed", attempts=attempts, last_error=error)
        logger.error("job %s FAILED permanen: %s", job.get("id"), error)
        return True

    backoff = min(2 ** attempts * 10, 300) + random.randint(0, 5)
    # store.update tidak menerima run_after → pacing retry in-memory worker-side
    _backoff[job["id"]] = _time.monotonic() + backoff
    await store.update(job["id"], status="queued", attempts=attempts, last_error=error)
    logger.warning("job %s retry #%d dalam %ss: %s", job.get("id"), attempts, backoff, error)
    return False


async def _maybe_release(store, job) -> bool:
    """Job di-claim sebelum deadline backoff-nya → lepas kembali (tanpa proses).

    Return True bila job dilepas (tidak diproses pada tick ini).
    """
    job_id = job.get("id")
    deadline = _backoff.get(job_id)
    if deadline is None:
        return False
    if _time.monotonic() < deadline:
        logger.info(
            "job %s masih backoff (%.0fs) — dilepas kembali ke queue",
            job_id, deadline - _time.monotonic(),
        )
        try:
            await store.update(job_id, status="queued")  # whitelist: aman
        except Exception:  # noqa: BLE001
            logger.exception("job %s: gagal release backoff", job_id)
        return True
    _backoff.pop(job_id, None)
    return False


async def process_job(store, job) -> None:
    """Jalankan pipeline satu job + klasifikasi error kelas."""
    progress = _make_progress(job)  # DM progress bertahap (best effort; None = non-aktif)
    try:
        await handle_job(store, job, progress=progress)
    except JobPermanentError as e:
        await _fail(store, job, str(e), permanent=True)
        await _notify_user(job, FAILED_REPLY)
    except JobTransientError as e:
        if await _fail(store, job, str(e), permanent=False):
            await _notify_user(job, FAILED_REPLY)
        elif job.get("attempts") == 1:  # retry pertama → kabari user (dedup otomatis di notifier)
            await _progress_send(progress, "retry", RETRY_REPLY, force=True)
    except Exception as e:  # noqa: BLE001 — tak terduga (mis. LLMError) → retry
        logger.exception("job %s error tak terduga: %s", job.get("id"), e)
        if await _fail(store, job, str(e), permanent=False):
            await _notify_user(job, FAILED_REPLY)
        elif job.get("attempts") == 1:
            await _progress_send(progress, "retry", RETRY_REPLY, force=True)


async def worker_tick(store) -> bool:
    """Ambil & proses SATU job dari queue. Return True bila ada job DIPROSES.

    Dipakai loop utama dan tes hermetic (satu iterasi loop). Job yang masih
    dalam masa backoff dilepas kembali (False).
    """
    job = await store.claim_next()
    if job is None:
        return False
    if await _maybe_release(store, job):
        return False
    await process_job(store, job)
    return True


async def _claim_loop(sem: asyncio.Semaphore, worker_id: str) -> None:
    store = _get_store()
    while True:
        try:
            job = await store.claim_next()
            if job:
                if await _maybe_release(store, job):
                    await asyncio.sleep(min(0.5, POLL_SECONDS))
                    continue
                async with sem:
                    await process_job(store, job)
            else:
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("worker loop error")
            store = _get_store()  # re-init — recovery koneksi mati
            await asyncio.sleep(POLL_SECONDS)


async def _recover_stale(store) -> None:
    requeue = getattr(store, "requeue_stale", None)
    if requeue is None:
        return
    try:
        await requeue(RUNNING_STALE_SECONDS)
    except Exception:  # noqa: BLE001
        logger.exception("requeue_stale gagal")


async def _worker_main(store, worker_id: str) -> None:
    sem = asyncio.Semaphore(CONCURRENCY)
    await _recover_stale(store)
    await asyncio.gather(_claim_loop(sem, worker_id))


async def _ensure_init(store) -> None:
    """Pastikan skema ada — kontrak JobStore.init(); fallback ensure_schema."""
    init = getattr(store, "init", None)
    if init is not None:
        try:
            await init()
            return
        except TypeError:
            pass
    ensure = getattr(store, "ensure_schema", None)
    if ensure is not None:
        try:
            await ensure(SCHEMA_SQL)
        except Exception:  # noqa: BLE001
            logger.exception("ensure_schema gagal")


async def _pg_ping_fallback() -> bool:
    """Fallback healthcheck sementara: jobs.py belum ada → ping PG langsung."""
    try:
        import asyncpg  # lazy — tidak wajib di venv lokal
    except ImportError:
        return False
    try:
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn, timeout=3)
        try:
            return True
        finally:
            await conn.close()
    except Exception:  # noqa: BLE001
        return False


async def _health_check() -> int:
    """Exit 0 bila store bisa init (skema dibuat / koneksi OK)."""
    try:
        store = _get_store()
    except ImportError:
        logger.warning("healthcheck: app.jobs belum ada — fallback ping PG")
        return 0 if await _pg_ping_fallback() else 1
    except Exception as e:  # noqa: BLE001
        logger.error("healthcheck: store init gagal: %s", e)
        return 1
    try:
        await _ensure_init(store)
        ping = getattr(store, "ping", None)
        if ping is not None:
            return 0 if await ping() else 1
        return 0
    except Exception as e:  # noqa: BLE001
        logger.error("healthcheck: store init gagal: %s", e)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="FactBot worker")
    parser.add_argument("--health", action="store_true", help="healthcheck: exit 0 bila store bisa init")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async def run() -> int:
        if args.health:
            # Healthcheck: gagal harus clean (exit 1), bukan traceback
            return await _health_check()

        store = _get_store()
        await _ensure_init(store)
        worker_id = f"{socket.gethostname()}-{os.getpid()}"
        logger.info(
            "🚀 FactBot worker %s start (backend=%s, concurrency=%d, poll=%ss, "
            "max_attempts=%d)",
            worker_id, type(store).__name__, CONCURRENCY, POLL_SECONDS,
            settings.worker_max_attempts,
        )

        task = asyncio.create_task(_worker_main(store, worker_id))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.info("worker berhenti (graceful)")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
