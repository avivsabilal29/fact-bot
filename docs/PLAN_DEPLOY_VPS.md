# PLAN DEPLOY VPS — Cutover Funnel → Direct nginx

> **Tujuan:** Pindahkan bot dari lokal (Tailscale Funnel `parkee.tail67f453.ts.net`)
> ke VPS `169.58.111.12` — webhook langsung lewat `https://factbot.tech/webhooks/meta`
> (nginx path routing, TANPA funnel). Detail teknis arsitektur: `docs/DEPLOY_VPS.md`.

## 🗺️ Arsitektur Setelah Deploy

```
SEKARANG (lokal)                    SETELAH (VPS)
───────────────                     ───────────────────────────────
Meta webhook                        Meta webhook
   │                                    │
   ▼                                    ▼
parkee.tail67f453.ts.net           factbot.tech (Cloudflare)
   │  Tailscale Funnel                  │
   ▼                                    ▼
laptop :8001 (systemd)             nginx (stack teman, existing)
   │                                    │  /webhooks/ → bot:8001
   ▼                                    ▼
uvicorn bot                        bot (Docker) + bot-worker (Docker)
                                        │
                                        ▼
                                   factbot_db (Postgres 17, existing)
```

| | Lokal (sekarang) | VPS (target) |
|---|---|---|
| **Webhook URL** | `https://parkee.tail67f453.ts.net/webhooks/meta` | `https://factbot.tech/webhooks/meta` |
| **Proses** | uvicorn + worker (systemd user) | `bot` + `bot-worker` (Docker Compose) |
| **DB** | SQLite (`data/klarifai.db`) | Postgres 17 (stack teman) ATAU SQLite volume |
| **Tunnel** | Tailscale Funnel + watchdog | ❌ **TIDAK ADA** — langsung nginx |
| **Watchdog** | `klarifai-funnel-watchdog` | ❌ Tidak perlu (Docker `restart: unless-stopped`) |

## ✅ Status Persiapan (SUDAH SIAP)

| Komponen | Status |
|---|---|
| `Dockerfile` (bot) | ✅ Build sukses, image `factbot/bot:test` 2.04 GB (18/18 step) |
| `docker-compose.yml` (bot + bot-worker, external network) | ✅ Lengkap: healthcheck, restart, volume, logging |
| nginx `location /webhooks/` → `bot:8001` | ✅ **SUDAH TERPASANG** di VPS (variable + resolver 127.0.0.11) |
| `.env` (FACTBOT_*, DEEPSEEK_*, META_*, IG_*) | ✅ Terisi di lokal — tinggal salin + tambah field VPS |
| Pipeline analisa (caption-only) | ✅ Terbukti E2E (DM → URL faktabot) |
| ProgressNotifier (anti-senyap DM) | ✅ Terbukti live |
| URL publik fix (lookaside → instagram.com) | ✅ Terbukti live |
| SSH key `factbot-avivsabilalm` (RSA 4096) | ✅ Ada, **HostName masih 0.0.0.0** → perlu diisi IP |

## 📋 Plan Eksekusi (5 Langkah)

### LANGKAH 1 — Persiapan di Laptop (30 menit)
```bash
cd ~/fact_bot_server
# 1. Fix SSH config (HostName masih 0.0.0.0!)
#    edit ~/.ssh/config:  Host factbot → HostName 169.58.111.12
ssh factbot "echo SSH-OK && hostname"          # tes koneksi

# 2. Push commit yang SUDAH dibuat (0a01d5d — 42 files: pipeline, progress,
#    URL fix, hermes brain, docs). Cek: git log --oneline -1

# 3. Siapkan .env VPS (dari lokal + tambahan)
cp .env .env.vps
#    tambahkan: EXTERNAL_NETWORK=<nama network stack teman>
#    tambahkan: DATABASE_URL=postgresql+asyncpg://<user>:<pass>@factbot_db:5432/<db>  (atau biarkan SQLite)
#    PASTIKAN tidak ada parkee_* tersisa — hanya FACTBOT_*/DEEPSEEK_*
```

### LANGKAH 2 — Setup VPS Sekali (30 menit)
```bash
ssh factbot
# 1. SSH key GitHub (deploy key) supaya bisa pull repo
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" && cat ~/.ssh/id_ed25519.pub
#    → tambahkan ke github.com/avivsabilal29/fact-bot → Settings → Deploy keys

# 2. Clone repo
mkdir -p /opt/factbot && cd /opt/factbot
git clone git@github.com:avivsabilal29/fact-bot.git .

# 3. .env — salin dari laptop (scp) + isi EXTERNAL_NETWORK
#    scp .env.vps factbot:/opt/factbot/.env   (dari laptop)
cd /opt/factbot && chmod 600 .env

# 4. Cek network stack teman (WAJIB — bot harus join network yang sama dgn nginx)
docker network ls
docker inspect factbot-nginx-1 -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
#    → isi EXTERNAL_NETWORK di .env (mis. factbot_default)

# 5. Verifikasi nginx sudah punya /webhooks/ (seharusnya sudah, dari setup 2026-08-02)
docker exec factbot-nginx-1 nginx -t
docker exec factbot-nginx-1 grep -A6 "location /webhooks/" /etc/nginx/conf.d/default.conf
```

### LANGKAH 3 — Build & Start di VPS (20-40 menit, image 2GB)
```bash
ssh factbot 'cd /opt/factbot && docker compose config --quiet && docker compose up -d --build'
ssh factbot 'cd /opt/factbot && docker compose ps'      # bot + bot-worker harus healthy
ssh factbot 'docker compose logs -f bot'                # cek startup (Whisper model load bisa lama)
```

**Health gate:**
```bash
# Dari laptop:
curl -fsS https://factbot.tech/health                       # → bot health via nginx ({"status":"ok",...})
curl -fsS "https://factbot.tech/webhooks/meta?hub.mode=subscribe&hub.verify_token=<TOKEN>&hub.challenge=pong"
# → harus membalas "pong"
```

### LANGKAH 4 — Cutover Webhook Meta (5 menit, PALING KRITIS)
```bash
# Meta App Dashboard → WhatsApp/IG → Webhooks → Edit callback URL:
#   DARI: https://parkee.tail67f453.ts.net/webhooks/meta
#   KE:   https://factbot.tech/webhooks/meta
#   Verify token: <META_VERIFY_TOKEN> → Verify & Save
```

**Verifikasi cutover:**
1. Kirim DM test (reel + klaim) dari HP → harus dapat URL `https://factbot.tech/r/{id}`
2. Amati: `ssh factbot 'docker compose logs -f --tail=50 bot bot-worker'`
3. Cek timeline: ACCEPT → "🔄 Analyzing..." → URL (progress notifier jalan)

### LANGKAH 5 — Matikan Lokal (5 menit, setelah E2E VPS terbukti)
```bash
# Dari laptop — hentikan sistem lokal
systemctl --user stop klarifai-bot && systemctl --user disable klarifai-bot
systemctl --user stop klarifai-funnel-watchdog && systemctl --user disable klarifai-funnel-watchdog
tailscale funnel reset          # matikan Funnel
```

## 🛟 Rollback Plan (kalau VPS bermasalah)

| Skenario | Tindakan | Waktu |
|---|---|---|
| VPS health gagal | Balik callback Meta → URL Tailscale + `systemctl --user start klarifai-bot` | 5 menit |
| Webhook 502 di nginx | `docker compose logs bot` → cek port/network; rollback image `:prev` | 10 menit |
| Bot jalan tapi job gagal | Cek `DEEPSEEK_API_KEY` / `FACTBOT_API_KEY` di .env VPS | 5 menit |
| Image build gagal di VPS | `docker build -t factbot/bot:test .` di laptop → `docker save/load` + `docker compose up -d` | 15 menit |

**Prinsip:** lokal TETAP HIDUP sampai VPS terbukti E2E. Cutover = 1 langkah kecil (callback URL), rollback = 1 langkah kecil (balikin callback).

## ⚠️ 3 Hal yang Sering Jadi Masalah (dari pengalaman session ini)

1. **`EXTERNAL_NETWORK` salah nama** → bot gak bisa di-reach nginx (502). Cek `docker network ls` di VPS, bukan nebak.
2. **SSL cert** — di VPS TIDAK perlu `ssl/ca-bundle.pem` (Ubuntu fresh bundle lengkap). Kalau muncul `CERTIFICATE_VERIFY_FAILED`: `apt install ca-certificates` lalu restart container.
3. **`PARKEE_*` sisa di .env** → config error. Pastikan cuma `FACTBOT_PROXY_URL`/`FACTBOT_PROXY_KEY`/`FACTBOT_MODEL` (proxy diatur belakangan, sekarang DeepSeek langsung).

## 🎯 Keputusan yang Perlu Lo Konfirmasi Sebelum Eksekusi

| # | Pertanyaan | Opsi |
|---|---|---|
| 1 | **DB di VPS** | A) Postgres stack teman (butuh kredensial dari pemilik) — rekomendasi utk produksi · B) SQLite volume (paling cepat, MVP) |
| 2 | **Kapan eksekusi** | Sekarang (semua komponen siap) atau nanti |
| 3 | **Commit dulu?** | ✅ **SUDAH** — commit `0a01d5d` (42 files) tinggal `git push origin main` |

---

## 🧠 Hermes Brain Layer — Instalasi Docker + Copy Profile `factbot`

> **Konteks:** Hermes profile `factbot` (SOUL.md 6 section, `~/.hermes/profiles/factbot/`) — copy bersih di repo cuma **44K** (config.yaml + SOUL.md + profile.yaml; state runtime di-skip).
> sudah dibuat & teruji di laptop. Ini "otak" bot: persona, memory knowledge, dan nanti
> orchestrator sub-agents. **Phase 1 pipeline TIDAK butuh ini** (pakai DeepSeek langsung) —
> service ini di-deploy SEKALIAN biar satu stack, tapi belum di-wire ke jalur kritis.

### Arsitektur setelah hermes-brain masuk

```yaml
services:
  bot:           # FastAPI webhook (existing, jalur kritis)
  bot-worker:    # pipeline analisa (existing, jalur kritis)
  hermes-brain:  # ⭐ BARU — Hermes profile factbot (post-hackathon brain)
```

### Langkah 1: Dockerfile Hermes (`hermes/Dockerfile` di repo) — ✅ SUDAH DIBUAT & TERUJI

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git xz-utils \
    && rm -rf /var/lib/apt/lists/*
# Installer di-commit (DNS laptop salah resolve hermes-agent.nousresearch.com)
COPY install.sh /tmp/hermes-install.sh
# Pre-seed uv via pip → installer skip download astral.sh (baris 562)
RUN pip install --no-cache-dir uv \
    && mkdir -p /hermes-data/bin \
    && cp "$(command -v uv)" /hermes-data/bin/uv \
    && bash /tmp/hermes-install.sh && rm /tmp/hermes-install.sh
ENV PATH="/root/.local/bin:/usr/local/bin:${PATH}"
ENV HERMES_HOME=/hermes-data
ENV HERMES_PROFILE=factbot
RUN mkdir -p /hermes-data && chmod 755 /hermes-data
WORKDIR /hermes-data
EXPOSE 8644
CMD ["hermes", "gateway", "run"]
```

> ✅ **Terbukti:** image `factbot/hermes-brain:test` (2.61GB) build sukses, hermes v0.19.1
> jalan, test persona 18s (honcho off).

> ⚠️ **Pitfall build yang sudah ditemukan:**
> 1. Installer butuh `xz-utils` (extract Node tarball) + `git` — tanpa itu build gagal `tar: xz: Cannot exec`.
> 2. **DNS laptop lo resolve `hermes-agent.nousresearch.com` ke IP salah** (147.45.69.3 = VPS temen, bukan Vercel 216.150.x.x) → curl installer gagal SSL (`CN=localhost`). Solusi: **installer di-commit ke repo** (`hermes/install.sh`), Dockerfile `COPY` + `bash` — semua download aktual installer (uv/github/nodejs/pypi) pakai domain normal. **Di VPS (DNS normal) masalah ini gak ada** — tapi tetap pakai COPY biar build deterministik.
> 3. **`astral.sh` (uv installer) SSL gagal dari laptop** → pre-seed uv via pip (PyPI reachable) ke `/hermes-data/bin/uv`; installer cek `$HERMES_HOME/bin/uv` dan skip download (baris 562 install.sh).

## 🧠 Hermes Brain — Konfigurasi (sudah di-setup)

- **Model:** `deepseek-v4-flash` (provider deepseek, base `https://api.deepseek.com/v1`) — di `config.yaml` profile
- **API key:** `DEEPSEEK_API_KEY` — dual jalur: env compose `${DEEPSEEK_API_KEY:-}` + `.env` profile (mount rw)
- **Honcho memory: DISABLED** (`memory_enabled: false` + `user_profile_enabled: false`) — user: "kalau oake honcho agak lama soalnya". Disable di **kedua** config: copy repo (`hermes-profiles/factbot/` — yang di-mount ke container VPS) DAN profile asli laptop (biar hasil test konsisten). Bot pure: SOUL.md + config, tanpa dependency memory eksternal. Konteks per-user tetap di DB bot (jobs/pending). **Terbukti 2.7× lebih cepat: 49s → 18s.**

### Langkah 2: Service di `docker-compose.yml`

```yaml
  # ------------------------------------------------------ HERMES BRAIN ------
  hermes-brain:
    build:
      context: ./hermes
    image: factbot/hermes-brain:${IMAGE_TAG:-latest}
    restart: unless-stopped
    environment:
      HERMES_HOME: /hermes-data
      HERMES_PROFILE: factbot
      WEBHOOK_ENABLED: "true"
      WEBHOOK_PORT: "8644"
      WEBHOOK_SECRET: "${HERMES_WEBHOOK_SECRET:-}"
      DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY:-}"       # model brain (deepseek-v4-flash)
    volumes:
      - hermes_profile:/hermes-data                 # state persist
      - ./hermes-profiles/factbot:/hermes-data/profiles/factbot   # SOUL/config/skills (rw — Hermes tulis state cron/sessions)
    networks:
      - web
    healthcheck:
      test: ["CMD", "hermes", "--version"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    stop_grace_period: 30s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

Volume tambahan di bagian `volumes:`:
```yaml
volumes:
  factbot_data:
  whisper_cache:
  hermes_profile:        # ⭐ BARU — state Hermes (memories, sessions) persist di VPS
```

### Langkah 3: Copy Profile `factbot` dari Laptop ke VPS

**⚠️ UPDATE: profile di repo (hermes-profiles/) SUDAH di-commit ke git** (commit 0a01d5d) —
jadi di VPS cukup `git clone` + `git pull`, TIDAK perlu scp manual:

```bash
# Di VPS — profile sudah ikut repo (hermes-profiles/factbot/, 44K bersih):
ls /opt/factbot/hermes-profiles/factbot/    # config.yaml + SOUL.md + profile.yaml

# Kalau mau profile dari laptop yg lebih fresh (mis. setelah edit SOUL):
scp -r ~/.hermes/profiles/factbot/ factbot:/opt/factbot/hermes-profiles/factbot/
```

**Yang ada di repo (44K, ke-commit):**

| Isi | Fungsi | Di-repo? |
|---|---|---|
| `SOUL.md` (9.7KB) | Persona FactBot (IDENTITY/MISSION/RULES/WORKFLOW/MEMORY MODEL/OUTPUT) | ✅ |
| `config.yaml` (2.9KB) | Model deepseek-v4-flash + provider + **honcho disabled** | ✅ |
| `profile.yaml` (235B) | Marker profile | ✅ |
| `.env` | DEEPSEEK_API_KEY — **TIDAK ke-commit** (gitignore) | ❌ → inject via compose env |
| `memories/`, `skills/` | Knowledge bot (kosong dulu — siap diisi) | ✅ (folder) |

**Catatan penting:**
- **Secrets (`DEEPSEEK_API_KEY`) TIDAK di-repo** → di VPS di-inject lewat `docker compose`
  environment (`${DEEPSEEK_API_KEY:-}`) dari root `.env` project.
- Mount profile **rw** (bukan ro) — Hermes butuh nulis state (cron/sessions) ke profile dir.
- Kalau mau bawa memory/session lama → `scp -r` folder `memories/` (opsional, MVP belum ada isinya).

### Langkah 4: Verifikasi Hermes Brain di VPS

```bash
ssh factbot 'cd /opt/factbot && docker compose up -d --build hermes-brain'
ssh factbot 'docker compose logs -f hermes-brain'     # tunggu "gateway ready"

# Test persona (dari VPS):
ssh factbot 'docker exec factbot-hermes-brain-1 hermes -p factbot chat -q "Halo, perkenalkan dirimu"'
# → harus jawab sebagai FactBot (persona dari SOUL.md)
```

### Kapan Hermes Brain Benar-Benar Dipakai

| Fase | Status | Pemakaian |
|---|---|---|
| Phase 1 (sekarang) | Deploy paralel, **belum di-wire** | Bot tetap pakai DeepSeek langsung — Hermes brain standby |
| Phase 2/3 (post-hackathon) | Wire ke worker | `router decision`: SIMPLE → direct LLM, COMPLEX → Hermes brain + sub-agents |

**Alasan:** jalur kritis (DM → URL) tidak boleh bergantung pada service baru yang belum teruji
E2E. Hermes brain di-deploy biar satu stack & profile tersimpan, tapi worker tetap langsung
ke DeepSeek sampai enhancement post-hackathon.

### Rollback Hermes Brain

```bash
ssh factbot 'cd /opt/factbot && docker compose stop hermes-brain'   # bot & worker TIDAK terpengaruh
# Atau hapus total:
ssh factbot 'cd /opt/factbot && docker compose rm -sf hermes-brain'
```

---

*Dokumen ini = runbook eksekusi. Teori & arsitektur detail: `docs/DEPLOY_VPS.md`.*
*Deadline 16 Agt 2026 — jalur kritis: Langkah 1-4 selesai dalam 1 sesi, Langkah 5 (matikan lokal) setelah E2E terbukti.*
