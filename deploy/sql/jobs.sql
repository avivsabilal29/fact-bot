-- ============================================================================
-- FactBot job queue — PostgreSQL (dieksekusi otomatis oleh app/worker.py saat
-- start; file ini untuk setup manual / referensi DBA).
-- Queue memakai SKIP LOCKED → aman dipakai banyak worker sekaligus.
-- ============================================================================

CREATE TABLE IF NOT EXISTS jobs (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL,                  -- 'verify_media' | ...
    report_id    TEXT NOT NULL UNIQUE,           -- idempotent: {platform}_{media_id}
    payload      JSONB NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'queued', -- queued|running|done|failed
    attempts     INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    run_after    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- retry backoff
    worker       TEXT,                           -- worker yang sedang mengerjakan
    last_error   TEXT,
    public_url   TEXT,                           -- https://factbot.tech/r/{id}
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jobs_pick
    ON jobs (status, run_after) WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Query claim job (dipakai worker; atomik karena FOR UPDATE SKIP LOCKED):
--   UPDATE jobs SET status='running', worker=$1, updated_at=now()
--   WHERE id = (SELECT id FROM jobs
--               WHERE status='queued' AND run_after <= now()
--               ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
--   RETURNING id, kind, report_id, payload, attempts, max_attempts;

-- Crash recovery (dijalankan worker saat start):
--   UPDATE jobs SET status='queued', run_after=now(), worker=NULL
--   WHERE status='running' AND updated_at < now() - interval '10 minutes';
