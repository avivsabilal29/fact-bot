# PLAN PHASE 1 — "Hitam-Putih" MVP (Sekarang → 8 Agt 2026)

> **Tujuan:** Alur lengkap jalan di Instagram: user DM reel + klaim → bot analisa → upload ke faktabot → URL hasil dikirim balik ke DM.
> **Strategi:** Test **LOKAL** dulu (laptop, systemd `klarifai-bot`, Tailscale funnel). Deploy **VPS** di akhir Phase 1 (exception handling di §7).
> **Deadline:** 8 Agt 2026 (5 hari kerja). Final hackathon: 16 Agt.

---

## 0. Prinsip (dari docs/pipeline_video_analysis.md)

| Prinsip | Penerapan |
|---|---|
| Event-driven | Webhook → enqueue job → worker asyncio konsumsi (gak ada polling) |
| Tanpa magic number | Ambang batas = konfigurasi di `.env` (Settings), bukan konstanta |
| Robust/retryable | State machine eksplisit + persistensi → crash-safe |
| Realtime | Webhook balas cepat, kerja berat di worker, user dapat 2 pesan (ack → hasil) |
| Idempotent | `report_id = {platform}_{media_id}` → 409 = reuse URL lama |
| Graceful degradation | Bot SELALU balas user, paling buruk verdict `unverified` |

---

## 1. Ringkasan Task

| # | Task | File utama | Prioritas |
|---|---|---|---|
| 1.1 | Hubungkan routing → job queue | `app/webhooks/meta.py`, `app/jobs.py` | 🔴 Wajib pertama |
| 1.2 | Worker pipeline (teks dulu) | `app/pipeline/analyzer.py`, `app/worker.py` | 🔴 Wajib |
| 1.3 | LLM integration | `app/pipeline/llm.py` | 🔴 Wajib |
| 1.4 | Upload ke faktabot | `app/api/factbot.py` | 🔴 Wajib |
| 1.5 | Reply URL ke DM | `app/webhooks/meta.py` | 🔴 Wajib |
| 1.6 | Database migration (SQLite → Postgres) | `app/db/` | 🟡 Setelah pipeline jalan |
| 1.7 | Deploy ke VPS | `docker-compose.yml`, `deploy/` | 🟡 Final |

**Urutan eksekusi: 1.1 → 1.2 → 1.3 → 1.4 → 1.5 (E2E lokal) → 1.6 → 1.7 (VPS).**

---

## 2. Task 1.1 — Routing → Job Queue

### Tujuan
Saat user kirim text klaim (pending ada), alih-alih cuma log + CLAIM_RECEIVED_REPLY, bot **membuat AnalysisJob** di database dan enqueue ke worker.

### Langkah
1. Buat `app/jobs.py` — model & fungsi job:
   - `AnalysisJob` dataclass/pydantic: `job_id`, `platform`, `media_url`, `media_title`, `media_id`, `claim_text`, `sender_id`, `status`, `report_id`, `public_url`, `created_at`, `updated_at`, `error`.
   - `create_job(db, job) -> str` (INSERT, status=PENDING).
   - `get_job(db, job_id)`, `update_job(db, job_id, **fields)`.
   - `claim_next_job(db, worker_id) -> job | None` (SELECT ... FOR UPDATE SKIP LOCKED — untuk Postgres; fallback SQLite pakai `BEGIN IMMEDIATE`).
2. Di `app/webhooks/meta.py` `handle_message()` — branch text-klaim (pending ada):
   - Extract `media_id` dari URL reel (`/reel/{shortcode}/` atau angka di akhir).
   - `report_id = f"ig_{media_id}"` (idempotent key).
   - `create_job(...)` → log `📦 Job {job_id} created`.
   - Balas CLAIM_RECEIVED_REPLY (tetap).
3. Enqueue: job disimpan di DB; worker yang polling (lihat 1.2). Untuk MVP lokal, bisa langsung `asyncio.create_task(worker.process_job(job))` ATAU worker loop terpisah — keputusan di §2.4.

### Dependensi
- Koneksi DB minimal (SQLite dulu — `app/db/` sudah ada stub).

### Test
- Unit: kirim webhook reel (B) → text klaim (C) → assert job muncul di DB dengan status PENDING, field lengkap.
- Unit: text tanpa pending (A) → tidak ada job dibuat.

---

## 3. Task 1.2 — Worker Pipeline (Teks Dulu)

### Tujuan
Worker memproses job: caption/teks → LLM verdict → Markdown → upload → simpan URL.

### Langkah
1. Refactor `app/worker.py` (sudah ada skeleton dari sub-agent) jadi pipeline konkret:
   - Loop utama: `while True: job = claim_next_job(); if job: await process_job(job)` dengan sleep configurable (bukan magic number — pakai `worker_poll_interval` dari Settings).
   - `process_job(job)`:
     - status → FETCHING: ambil caption via Graph API (`_get_media_caption`) atau dari payload (title reel sudah ada). **MVP: pakai title + claim_text dulu, download video = stretch** (fallback di §2.2 Phase 2).
     - status → VERIFYING: `llm.analyze(caption, claim_text)` → JSON verdict.
     - status → RENDERING: `render_markdown(verdict)` → string .md.
     - status → UPLOADING: `factbot.create_report(...)` → public_url.
     - status → DONE: simpan public_url, reply DM.
   - Setiap transisi `update_job(status=...)` — crash-safe (restart → lanjut dari status terakhir).
2. State machine retry: error kelas → `TRANSIENT` (network, 5xx) = retry dgn backoff configurable; `PERMANENT` (400, invalid) = status FAILED + balas user pesan gagal.

### Keputusan MVP (penting)
| Aspek | MVP (Phase 1) | Nanti |
|---|---|---|
| Download video | ❌ Skip (pakai title + claim) | yt-dlp + whisper |
| Transkrip audio | ❌ Skip | faster-whisper |
| OCR | ❌ Skip | pytesseract |
| Sumber analisa | title reel + claim text | transkrip + OCR |

> Alasan: biar alur E2E (DM → URL) kebukti CEPAT; video processing ditambah di Phase 2 (sudah ada di requirements + Dockerfile, tinggal aktivasi).

### Test
- Hermetic: mock LLM + mock faktabot → job PENDING → DONE, URL terisi.
- Hermetic: LLM error transient → retry; error permanent → FAILED + pesan user.
- Crash test: kill worker tengah jalan → restart → job lanjut dari status terakhir.

---

## 4. Task 1.3 — LLM Integration

### Tujuan
`app/pipeline/llm.py`: panggil `PARKEE_PROXY_URL` (OpenAI-compatible `/v1/chat/completions`) → verdict JSON terstruktur.

### Langkah
1. `Settings`: `parkee_proxy_url`, `parkee_api_key`, `parkee_model` (default `deepseek-v3`), `llm_timeout_seconds` (≥30s).
2. `analyze(caption, claim) -> Verdict`:
   - System prompt: fact-checker, output JSON **hanya**.
   - User prompt: caption + claim.
   - `response_format: {"type": "json_object"}` (kalau didukung; fallback parse manual).
3. Schema output (`Verdict` pydantic):
   ```json
   {
     "verdict": "hoax|fact|partly_true|unverified",
     "category": "health|government|politics|disaster|finance|technology|religion|education|other",
     "summary": "1-2 kalimat (20-300 char)",
     "claim": "klaim asli",
     "title": "judul editorial",
     "evidence": ["poin fakta 1", "..."],
     "sources": ["url sumber", "..."]
   }
   ```
4. Fallback: LLM timeout/error → verdict `unverified` + summary default (bukan crash).

### Test
- Hermetic: mock httpx response → parse JSON benar.
- Live (opsional kalau ada key): panggil API beneran sekali.

---

## 5. Task 1.4 — Upload ke faktabot

### Tujuan
`app/api/factbot.py`: POST `https://factbot.tech/api/v1/reports` → `url`.

### Langkah
1. `Settings`: `factbot_api_url` (default `https://factbot.tech`), `factbot_api_key`.
2. `create_report(report_id, title, name, platform, verdict, category, summary, claim, source_url, content_md) -> url`:
   - Header `Authorization: Bearer {key}`, `Content-Type: application/json`.
   - Body: semua field wajib + `id=report_id` (idempotent).
   - Timeout ≥30s (dari Settings).
   - **409 CONFLICT** → `GET /api/v1/reports/{id}` → reuse `url` (bukan error!).
   - 413/429 → jangan parse body (bisa non-JSON dari proxy) — cek status code dulu.
3. `render_markdown(verdict)` di `app/pipeline/renderer.py`: template dari contoh `docs/test_prabowo_nuklir.md` (verdict box, tabel klaim-vs-fakta, sumber, mermaid opsional).

### Test
- Hermetic: mock responses 201/409/400/429.
- Live (sudah terbukti): POST report test → 201 → url.

---

## 6. Task 1.5 — Reply URL ke DM

### Tujuan
Worker selesai → kirim `https://factbot.tech/r/{id}` ke sender DM.

### Langkah
1. Fungsi `send_result_dm(sender_id, public_url)` di `app/webhooks/meta.py` (atau `app/api/reply.py`):
   - Pesan: `"✅ Verification complete! Here's your result: {public_url} 🔍"` (+ verdict singkat).
2. Panggil dari worker setelah DONE (worker punya akses `_reply_dm` via import — pastikan tidak circular import; atau pindahkan `_reply_dm` ke `app/api/reply.py`).
3. Anti-loop: reply dari bot → echo → skip (sudah ada guard).

### Test
- Hermetic: worker DONE → mock `_reply_dm` → assert pesan berisi URL.
- Live: DM reel+klaim → dapat URL di DM (E2E).

---

## 7. Task 1.6 + 1.7 — Database & Deploy VPS (Exception)

### 1.6 Database migration (SQLite → Postgres)
- **Kenapa nanti:** MVP lokal bisa SQLite (file `data/factbot.db`). Postgres butuh koneksi ke `factbot_db` VPS.
- Langkah: `DATABASE_URL` dari Settings → `asyncpg` (sudah di requirements) → skema `jobs` table dibuat otomatis saat start (worker.py sudah handle).
- Migrasi data: gak perlu (job sementara).

### 1.7 Deploy VPS (setelah E2E lokal hijau)
1. `docker build -t factbot/bot .` (Dockerfile sudah terbukti build ✅).
2. Di VPS: `scp` atau `git clone` repo → `docker-compose.yml` (full) → isi `.env` → `docker compose up -d --build`.
3. Nginx sudah route `/webhooks/` → `bot:8001` ✅ (variable-based, tinggal bot container muncul).
4. Update callback Meta dashboard → `https://factbot.tech/webhooks/meta` (verify token `fact_bot_aviv_2026`).
5. Test verify challenge → 200. Test DM E2E via production URL.
6. **Rollback plan:** kalau VPS gagal → balik ke lokal (funnel masih jalan), callback kembali ke `parkee.tail67f453.ts.net`.

### Checklist deploy
```
[ ] docker compose config -q  (valid)
[ ] docker compose up -d --build
[ ] curl -f https://factbot.tech/health  → 200
[ ] verify webhook challenge → 200
[ ] DM reel+klaim → URL (E2E prod)
[ ] tail -f logs (gak ada error)
```

---

## 8. Timeline (5 hari kerja)

| Hari | Task | Keluar |
|---|---|---|
| H1 (3 Agt) | 1.1 + 1.2 skeleton | Job queue + worker loop jalan |
| H2 (4 Agt) | 1.3 LLM + 1.4 faktabot client | Verdict JSON + upload sukses |
| H3 (5 Agt) | 1.5 reply + E2E lokal | **DM → URL penuh di lokal** 🎯 |
| H4 (6 Agt) | Test hardening + 1.6 Postgres | Robust, crash-safe |
| H5 (7 Agt) | 1.7 deploy VPS + callback | **Live di factbot.tech** 🚀 |
| 8 Agt | Buffer/penyangga | Cadangan 1 hari |

---

## 9. Risiko & Mitigasi

| Risiko | Dampak | Mitigasi |
|---|---|---|
| `PARKEE_PROXY_URL` masih placeholder | LLM gak jalan | Fallback: DeepSeek langsung / Ollama lokal `http://localhost:11434/v1` |
| Meta app masih Development mode | User lain gak bisa tes | Submit Live ASAP; testing pakai akun app-role |
| yt-dlp login wall | Download reel gagal | MVP skip download (caption dulu) |
| Circular import worker↔meta | Error saat start | `_reply_dm` pindah ke `app/api/reply.py` |

---

## 10. Definisi "Selesai" Phase 1

- [ ] User DM reel ke @factacheckfact → balas ACCEPT (minta klaim)
- [ ] User DM text klaim → balas CLAIM_RECEIVED
- [ ] Worker proses → upload faktabot → URL `https://factbot.tech/r/{id}` valid
- [ ] Bot kirim URL ke DM user ✅
- [ ] Semua di **lokal** dulu (systemd + funnel) → lalu **VPS** (compose + factbot.tech)
- [ ] E2E test 20+ skenario (happy path, retry, 409, gagal download, LLM timeout)

---

## 11. Konsep yang Belum Masuk Phase 1 (Referensi)

| Konsep | Status | Rujukan |
|---|---|---|
| Hermes profile `factbot` sebagai brain layer (persona/soul) | Post-hackathon | `docs/architecture_multi_platform.md` §12 |
| Router decision: direct LLM vs sub-agent (orchestrator) | Phase 2+ | §12.5 + `docs/pipeline_video_analysis.md` §4.0 |
| RAG knowledge base | Post-hackathon | §12.2 |
| Hermes di Docker Compose (profile di-mount sbg `$HERMES_HOME`) | Post-hackathon | §12.4 |

> **Aturan Phase 1:** direct LLM (PARKEE_PROXY_URL) saja — jalur terpendek ke demo hidup.
> Hermes brain TIDAK di jalur kritis demo.
