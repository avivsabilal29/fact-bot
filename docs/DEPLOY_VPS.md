# Deploy FactBot ke VPS — Docker Compose (169.58.111.12)

> **Target final**: FactBot (FastAPI, `~/fact_bot_server`) jalan di VPS Ubuntu 24.04
> (4 vCPU / 7.8 GB RAM / Docker 29.7.1) sebagai container Docker Compose, di belakang
> nginx `factbot.tech` (SSL, Cloudflare) milik stack teman. Deadline hackathon: **16 Agt 2026**.
> Dokumen ini = panduan arsitektur + checklist migrasi dari lokal (systemd + Tailscale Funnel).

---

## 1. Arsitektur ringkas

```
Internet / Cloudflare
        │ https://factbot.tech (443, SSL)
        ▼
┌─────────────────────────── stack teman (project sendiri) ───────────────────────────┐
│  nginx (80/443) ──proxy──▶ factbot_web (Next.js :3000) ──▶ factbot_db (PostgreSQL 17)│
│      │  location /webhooks/, /auth/, /health → variabel + resolver Docker DNS        │
│      └───────────────────────────────────────────────────────────────────────────────┤
└──────────────┬───────────────────────────────────────────────────────────────────────┘
               │  network Docker bersama (external, contoh: factbot_net)
               ▼
┌──────────────────────────── project `factbot` (file ini) ───────────────────────────┐
│  bot         :8001  uvicorn app.main:app (1 worker)   ← TANPA port publik            │
│  bot-worker  :      python -m app.worker (queue PG SKIP LOCKED)                      │
│  volume      :      factbot_data (data/), whisper_cache (model faster-whisper)       │
│  .env        :      secrets — chmod 600, tidak pernah di-commit                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

- **Bot tidak expose port apa pun ke host**. Nginx menjangkau `bot:8001` via Docker DNS
  (`resolver 127.0.0.11` + `proxy_pass` pakai variabel) — bot boleh restart/ganti IP tanpa
  reload nginx. (Nginx teman **sudah** punya `location /webhooks/` → `bot:8001`; saat ini 502
  karena service `bot` belum ada — setelah `docker compose up -d`, 502 hilang dengan sendirinya.)
- Nama service `bot` di compose = network alias `bot` di network bersama → itulah yang
  di-resolve nginx. **Jangan ganti nama service.**
- Satu uvicorn worker saja — `app/webhooks/meta.py` menyimpan state in-memory
  (`pending_claims`, dedup set). `--workers > 1` memecah state antar proses.

## 2. File yang disediakan

| File | Fungsi |
|---|---|
| `Dockerfile` | Image produksi: python:3.12-slim + ffmpeg/libgomp1/tesseract/curl, non-root, healthcheck |
| `docker-entrypoint.sh` | Self-update yt-dlp (best-effort) lalu exec CMD |
| `docker-compose.yml` | **Utama** — service `bot` + `bot-worker`, external network stack teman |
| `docker-compose.full.yml` | Opsional/greenfield — contoh stack lengkap nginx+web+db+bot+worker |
| `.env.example` | Template semua env (copy → `.env` di VPS) |
| `.dockerignore` | Pastikan `.env`, `data`, `logs`, `venv` tidak masuk image |
| `app/worker.py` | Worker job queue (PG, SKIP LOCKED, heartbeat, crash recovery) |
| `deploy/deploy.sh` | Build+up+health gate+verify webhook+auto-rollback (dipanggil Hermes) |
| `deploy/monitor.sh` | Health + log check (exit ≠ 0 = masalah) |
| `deploy/nginx-webhooks.conf` | Snippet nginx referensi (jika lokasi lain perlu ditambah) |
| `deploy/sql/jobs.sql` | Skema tabel `jobs` + `workers` (juga dibuat otomatis oleh worker) |

## 3. Dockerfile — keputusan penting

- **`python:3.12-slim-bookworm`** + `ffmpeg` (ekstrak audio), `libgomp1` (**wajib** untuk
  faster-whisper/ctranslate2 — tanpa ini image crash saat load model), `tesseract-ocr`
  (+`tesseract-ocr-ind`) untuk OCR overlay, `curl` untuk healthcheck, `git` untuk
  huggingface_hub/yt-dlp.
- **yt-dlp di-install `--user` (ke `~/.local` appuser)** + `PATH` di-env: entrypoint bisa
  `yt-dlp -U` self-update **tanpa root** — yt-dlp berubah hampir tiap minggu karena situs
  media berganti struktur. Best-effort, gagal tidak menggagalkan start.
- **Non-root** (`appuser` UID 1001); `COPY --chown=appuser:appuser . .`; volume whisper
  cache di `/home/appuser/.cache/huggingface` (`HF_HOME`) supaya model `small` (~460 MB)
  di-download sekali saja.
- `HEALTHCHECK` curl `:8001/health` (interval 30s, start_period 45s — cukup untuk install
  deps pertama kali).
- `--proxy-headers --forwarded-allow-ips *` di CMD uvicorn: percaya `X-Forwarded-*` dari
  nginx (client IP asli di access log).

## 4. Worker terpisah vs background task — trade-off & rekomendasi

| Aspek | Background task in-process | Service `bot-worker` terpisah (dipilih) |
|---|---|---|
| Infra | 0 tambahan | 1 service (image sama), queue di PG yang **sudah ada** |
| Webhook latency | Risiko blocking kalau salah asyncio (whisper adalah CPU-bound; yt-dlp/ffmpeg subprocess) | Nol risiko — event loop bot hanya ack+enqueue |
| Restart bot | Job in-flight hilang (recover via state di DB) | Sama recover-nya, tapi webhook tetap responsif selama worker restart |
| Isolasi | OOM/CPU whisper bisa mengganggu API | Container terpisah; `--scale bot-worker=2` untuk nambah kapasitas |
| Retry | Harus ditulis manual + state | State machine di PG + backoff class-based (sudah di `worker.py`) |
| Kompleksitas | Paling kecil | Sedang (1 tabel, 1 modul) |

**Rekomendasi: `bot-worker` terpisah + queue PostgreSQL `SKIP LOCKED`.** Alasan:
(a) Meta butuh webhook dibalas 200 secepat mungkin; analisa video butuh menit dan CPU.
(b) PG 17 sudah jalan di stack (factbot_db) — **nol infrastruktur baru**, beda dengan Redis.
(c) `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED)` memberi claim atomik —
aman untuk banyak worker; `attempts` + `run_after` = retry backoff eksponensial + jitter;
crash recovery requeue job `running` yang ditinggal worker mati (heartbeat table `workers`).
(d) Konsisten dengan state machine di `docs/pipeline_video_analysis.md` §3.

> **Jalur MVP** (kalau mau paling cepat sampai demo): pakai worker in-process sesuai
> `docs/pipeline_video_analysis.md` §3.3 (asyncio.Queue + Semaphore di lifespan, SQLite jobs)
> dan **jangan** start service `bot-worker`. Kode sudah dipisah di `app/pipeline/` sehingga
> pindah ke service terpisah tinggal memindah loop. Catatan: SQLite yang dipakai 2 proses
> (bot+worker) berisiko lock — itu alasan utama PG untuk arsitektur final.

## 5. Realtime & reliability

- **Webhook** → handler hanya ack + enqueue (`INSERT INTO jobs`) + balas DM ack instan;
  kerja berat di worker. Meta **retry otomatis** delivery yang gagal (exponential backoff,
  jam-an) — bot wajib 200 secepatnya; dedup `mid`/`comment_id` sudah ada di `meta.py`
  (pindahkan ke tabel PG nanti supaya idempoten lintas restart).
- **Idempotensi upload**: `report_id = {platform}_{media_id}` (fallback sha1 URL) — retry
  aman; 409 dari `FACTBOT_API_URL` → reuse `public_url` lama (spec pipeline §6).
- **Healthcheck + restart**: `restart: unless-stopped` + healthcheck kedua service;
  `stop_grace_period: 30s` (SIGTERM → worker selesai/requeue job).
- **Logging**: uvicorn + worker → stdout → `docker logs`; driver `json-file` rotasi
  `max-size 10m / max-file 3` (tidak makan disk). Opsional JSON formatter
  (`python-json-logger`) untuk parsing.

## 6. Migrasi lokal (systemd) → VPS (compose) — checklist

### A. Persiapan di laptop
1. Commit semua perubahan, push: `git add -A && git commit -m "deploy: docker compose + worker" && git push origin main`
   (repo: `git@github.com:avivsabilal29/fact-bot.git`).
2. (Opsional) Tes build lokal: `docker build -t factbot/bot:test .`

### B. Setup sekali di VPS (`ssh root@169.58.111.12`)
```bash
# 1. SSH key utk akses GitHub (pull tanpa password) — tambahkan ke GitHub → Settings → Deploy keys
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" && cat ~/.ssh/id_ed25519.pub

# 2. Clone repo
mkdir -p /opt/factbot && cd /opt/factbot
git clone git@github.com:avivsabilal29/fact-bot.git .

# 3. Env: salin + isi secrets
cp .env.example .env && chmod 600 .env && nano .env
#    META_APP_SECRET / META_PAGE_ACCESS_TOKEN / IG tokens / PARKEE_PROXY_URL+KEY /
#    FACTBOT_API_KEY / META_VERIFY_TOKEN (nilai BARU, catat) /
#    DATABASE_URL=postgresql+asyncpg://<user>:<pass>@factbot_db:5432/<db>
#    (minta kredensial PG dari pemilik stack; tes: docker exec factbot_db pg_isready -U <user>)

# 4. Temukan network Docker stack teman (wajib!)
docker network ls
docker inspect <container-nginx> -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
#    isi EXTERNAL_NETWORK di .env dengan nama itu (mis. factbot_net)

# 5. Pastikan nginx punya location /webhooks/ → bot:8001 (variabel+resolver).
#    Kalau belum / mau tambah /health /auth: mount deploy/nginx-webhooks.conf,
#    lalu: docker exec <nginx> nginx -t && docker exec <nginx> nginx -s reload

# 6. Validasi & start
docker compose config          # cek interpolasi
docker compose up -d           # build bot + start bot & bot-worker
docker compose ps              # keduanya harus healthy

# 7. Smoke test lokal & publik
curl -fsS http://127.0.0.1:8001/health          # hanya jika debug port diaktifkan
curl -fsS https://factbot.tech/health           # lewat nginx → bot
docker compose logs -f bot                      # cek startup log
```

### C. Update callback di Meta App Dashboard
1. **Webhooks → edit callback URL**:
   `https://parkee.tail67f453.ts.net/webhooks/meta` → **`https://factbot.tech/webhooks/meta`**
   Verify token = `META_VERIFY_TOKEN` (nilai baru) → **Verify & Save**.
   Path endpoint **tidak berubah** (`/webhooks/meta`) — hanya hostname.
2. IG OAuth (kalau dipakai): tambah `https://factbot.tech/auth/instagram/callback`
   ke **Valid OAuth Redirect URIs**.
3. Verifikasi manual:
   `curl "https://factbot.tech/webhooks/meta?hub.mode=subscribe&hub.verify_token=<TOKEN>&hub.challenge=pong"`
   → harus membalas `pong`.
4. Kirim event test dari dashboard Meta (atau komentar/DM sungguhan); amati
   `docker compose logs -f bot`.

### D. Cutover (setelah VPS terbukti E2E)
```bash
systemctl --user stop klarifai-bot && systemctl --user disable klarifai-bot
tailscale funnel reset          # opsional: matikan Funnel Tailscale
```
- **Rollback plan**: kalau VPS bermasalah → set callback URL Meta kembali ke URL Tailscale →
  `systemctl --user start klarifai-bot` → perbaiki VPS.
- **Data**: MVP tidak ada data penting (state in-memory + jobs baru). Kalau mau pindah ke PG:
  dump SQLite lama atau mulai fresh (jobs table kosong = aman). Kalau tetap SQLite: copy
  `data/` ke volume `factbot_data`.

## 7. Workflow deploy pakai Hermes agent

Hermes (di laptop) sebagai **orchestrator** — semua langkah lewat satu perintah SSH:

```bash
# Deploy rilis baru (Hermes terminal):
ssh root@169.58.111.12 'cd /opt/factbot && bash deploy.sh main'

# Rollback ke image sebelumnya (kalau rilis bermasalah):
ssh root@169.58.111.12 'cd /opt/factbot && IMAGE_TAG=prev docker compose up -d --no-build bot bot-worker'

# Monitoring / alert (Hermes cron, tiap 15 menit):
ssh root@169.58.111.12 'bash /opt/factbot/deploy/monitor.sh'
```

`deploy.sh` melakukan: pull kode → tag image lama `:prev` → build + up (image di-tag
per-commit) → **health gate** (Docker healthcheck ≤60s) → verify publik `/health` → verify
webhook Meta (GET subscribe harus balas `pong`) → **auto-rollback** ke `:prev` jika gagal.
Hermes cukup memanggil skrip, membaca output, dan melaporkan; Hermes cron bisa men-trigger
`monitor.sh` dan alert bila exit ≠ 0.

- **Registry (GHCR)**: opsional. Hackathon: build langsung di VPS cukup (image per-commit
  + `:prev` untuk rollback). Registry baru berguna kalau build pindah ke CI.
- **Security**: SSH key di laptop (`~/.ssh/config` alias `vps`), `.env` chmod 600 di VPS,
  secret tidak pernah masuk git (`.gitignore` + `.dockerignore` sudah mengunci).
- Alternatif CI penuh (GitHub Actions build → GHCR → ssh deploy) bisa menyusul setelah demo.

## 8. Monitoring minimal

| Lapisan | Alat |
|---|---|
| Liveness bot | `GET /health` (sudah ada) + `GET /health/detail` (uptime, pending claims, queue depth, worker alive) |
| Healthcheck Docker | `docker compose ps` — restart otomatis kalau unhealthy + `restart: unless-stopped` |
| Log | `docker compose logs -f --tail=100 bot bot-worker`; rotasi json-file 10m×3 |
| Resource | `docker stats --no-stream` (whisper `small` peak ~1 GB, 4 vCPU cukup) |
| Uptime eksternal | UptimeRobot/pingdom → `https://factbot.tech/health` (60s) — verifikasi path sampai nginx+bot |
| Hermes cron | `deploy/monitor.sh` tiap 15 menit; alert kalau exit ≠ 0 |
| Meta side | App Dashboard → Webhooks → "Send test"; retry delivery terlihat di log bot |

**Indikator sehat**: kedua container `healthy`; `queue_depth` di `/health/detail` tidak
menumpuk (job selesai dalam menit); tidak ada lonjakan `ERROR`/`retry` di log.

---

*Deadline 16 Agt 2026 — jalur kritis: (1) setup .env + network, (2) `docker compose up -d`,
(3) update callback Meta, (4) E2E test. Pipeline analisa penuh (yt-dlp/ffmpeg/whisper/LLM)
mengisi `app/worker.py` + `app/pipeline/` sesuai `docs/pipeline_video_analysis.md` — worker
sudah jalan dan terverifikasi bahkan sebelum pipeline terpasang (job langsung `done`).*
