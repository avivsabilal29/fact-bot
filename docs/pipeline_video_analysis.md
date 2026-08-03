# Pipeline Analisa Video/Reel — FactBot

> **Dokumen desain** (konsep). Deadline hackathon: **16 Agt 2026**. Backend: FastAPI (`~/fact_bot_server`).
> LLM: `PARKEE_PROXY_URL` (OpenAI-compatible, bisa Ollama lokal) — model `deepseek-v3`.
> Target VPS: `169.58.111.12` (4 vCPU / 7.8 GB RAM).
> Platform publikasi: `POST https://factbot.tech/api/v1/reports` → `https://factbot.tech/r/{id}`.

---

## 0. Prinsip desain

| Prinsip | Arti di pipeline ini |
|---|---|
| **Event-driven** | Tidak ada cron/polling untuk job. Webhook DM → enqueue event → worker asyncio konsumsi. |
| **Tanpa magic number** | Semua ambang batas (retry, timeout, limit durasi, concurrency) adalah **konfigurasi** (`.env`/`Settings`), bukan konstanta di kode. Keputusan routing ditentukan oleh **keberadaan data** (caption kosong? transkrip kosong?), bukan panjang karakter. |
| **Robust / retryable** | Setiap job punya state machine eksplisit + persistensi SQLite → crash-safe (recover saat startup). Retry berbasis **kelas error**, bukan timer tetap. |
| **Realtime** | Webhook balas `202/200` cepat, kerja berat pindah ke worker. User dapat 2 pesan: ack instan → hasil. |
| **Idempotent** | `report_id = {platform}_{media_id}` deterministik → retry aman, 409 = reuse URL lama. |
| **Graceful degradation** | Setiap tahap punya fallback; bot **selalu** membalas user — paling buruk dengan verdict `unverified` + penjelasan, bukan diam. |

---

## 1. Alur end-to-end (reel diterima → URL dikirim ke DM)

```
┌─────────────────────────── IG DM / Messenger ───────────────────────────┐
│ 1. User kirim reel/video          webhook POST /webhooks/meta           │
│    payload: attachments[].type=ig_reel, .payload.url, .payload.title    │
│    ─────────────────────────────────────────────────────────────────    │
│    handle_message() → media terdeteksi → pending_claims[sender] =       │
│    {url, title, media_type}  +  balas ACCEPT_REPLY ("kirim klaimnya")   │
│                                                                         │
│ 2. User kirim TEXT klaim         webhook POST /webhooks/meta            │
│    ─────────────────────────────────────────────────────────────────    │
│    handle_message() → pending ada → consume-once (pop) →                │
│    buat AnalysisJob(id=ig_{media_id}, url, title, claim_text, sender)   │
│    → balas CLAIM_RECEIVED_REPLY ("sedang diverifikasi...")  ⚡ instan   │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │ job_id → asyncio.Queue (in-process)
                                   ▼
┌────────────────────────── WORKER (event-driven) ────────────────────────┐
│  PENDING → FETCHING → TRANSCRIBING → VERIFYING → RENDERING → UPLOADING  │
│                                                                         │
│  FETCHING     : ambil caption via Graph API (media_id) +                │
│                 yt-dlp download video (kalau perlu, lihat §2)           │
│  TRANSCRIBING : ffmpeg → audio 16kHz mono → faster-whisper (base/small) │
│                 + OCR frame sampling (pytesseract) — paralel            │
│  VERIFYING    : LLM (deepseek-v3 via PARKEE_PROXY_URL) → JSON verdict   │
│                 {verdict, category, summary, claim, evidence, sources}  │
│  RENDERING    : template Markdown (§4) → simpan file .md                │
│  UPLOADING    : POST https://factbot.tech/api/v1/reports                │
│                 (Bearer key, idempotent id, timeout ≥30s)               │
│  DONE         : dapat public_url → reply DM ke sender                  │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
                 IG DM: "Hasil analisa: https://factbot.tech/r/{id} 🎯"
```

**Alur DM dalam satu diagram state (per sender):**

```
       ┌──────────────┐   ig_reel    ┌────────────────────┐   text klaim   ┌──────────────────┐
       │   IDLE       │─────────────▶│ PENDING (ada media)│───────────────▶│ ANALYZING (job)  │
       └──────────────┘              └────────────────────┘                └──────────────────┘
            ▲                media baru menimpa (overwrite)                       │
            └─────────────────────────────────────────────────────────────────────┘
              job DONE → kirim URL → kembali IDLE (slot kosong lagi)
```

> Konsisten dengan `docs/webhook_state_machine.md`: satu slot pending per sender, consume-once,
> tanpa TTL/magic time. Perubahan: alih-alih hanya membalas "verifying", slot kini **membuat AnalysisJob**.

---

## 2. Tahap Extraction

### 2.1 Empat sumber teks (urut prioritas biaya)

| # | Sumber | Cara ambil | Biaya | Kapan dipakai |
|---|---|---|---|---|
| 1 | **Claim text (DM)** | Sudah di payload webhook (text user) | gratis | **Selalu** — ini objek yang diverifikasi |
| 2 | **Caption/title media** | Sudah di payload (`payload.title`) + fetch Graph API `/{media_id}?fields=caption` | 1 HTTP call | **Selalu** — konteks media |
| 3 | **Transkrip video** | yt-dlp → ffmpeg → faster-whisper | CPU berat (menit) | **Hanya jika** caption tidak cukup (§2.3) |
| 4 | **OCR overlay video** | ffmpeg extract frame → pytesseract | CPU sedang | Saat transkrip kosong / video teks-overlay |

### 2.2 Detail tiap tahap

**Caption** — sudah tersedia dari payload `ig_reel` (url+title). Tetap fetch Graph API `/{media_id}` untuk `caption` lengkap + `permalink` (berguna sebagai referensi di report). Kalau fetch gagal → pakai `title` dari payload. Zero-risk stage.

**Transkrip (jalur penuh):**
```
yt-dlp -f "best[height<=720]/best" --no-playlist -o "{job_dir}/video.mp4" <url>
        │
        ▼
ffmpeg -i video.mp4 -vn -ac 1 -ar 16000 -c:a pcm_s16le audio.wav
        │
        ▼
faster-whisper (model="small" atau "base", language="id" hint, vad_filter=True)
        │
        ▼
transcript.txt  (+ segments ber-timestamp utk kutipan akurat)
```
- `vad_filter=True` (Silero VAD) memotong bagian tanpa suara → hemat ~30–50% waktu transkripsi.
- Model `base` ≈ 2× lebih cepat dari `small`, akurasi ID lumayan; `small` untuk kualitas. Keduanya muat di 7.8 GB RAM (base ~1 GB peak).
- Jalankan sebagai **subprocess** (`asyncio.create_subprocess_exec`) supaya worker event loop tidak terblokir; timeout konfigurasi.
- **Cache audio/transkrip** per media_id → reel yang sama dua kali tidak ditranskrip ulang.

**OCR overlay:** sampling frame tiap `OCR_FRAME_INTERVAL` detik (config, default 5s) → pytesseract (`--psm 6`). Digabung jadi `ocr_text.txt`. Sering jadi pembawa klaim di video hoax (teks besar di layar tanpa narasi).

**Sumber klaim (evidence) untuk LLM:**
- Claim text (wajib), caption/title, transkrip, OCR text.
- Post-hackathon: tambah hasil pencarian web (news API / search) sebagai evidence eksternal (lihat §8).

### 2.3 Keputusan: kapan download video vs cukup caption?

Prinsip: **download hanya jika data yang sudah ada tidak cukup** — keputusan berbasis *keberadaan konten*, bukan panjang string. MVP pakai rule deterministik; post-hackathon ganti gerbang LLM murah.

| Kondisi input | Jalur | Alasan |
|---|---|---|
| Caption non-kosong + claim text jelas & self-contained | **Caption-only** (skip download) | Claim sudah bisa diverifikasi; hemat CPU/bandwidth, latency detik-an |
| Caption kosong / hanya title pendek | **Full pipeline** (download → transkrip → OCR) | Video satu-satunya pembawa konten |
| Claim text kosong (user kirim reel tanpa text) | Tetap buat job dengan verdict "claim tidak jelas" → minta klarifikasi via DM; **jangan** buang-buang transkrip | Tidak ada objek verifikasi |
| Download gagal (private/deleted/region-block) | **Caption-only + OCR frame poster** (kalau thumbnail bisa diambil) | Graceful degradation |
| Transkrip kosong (video musik/no speech) | **OCR frames + caption** | Teks overlay = sumber utama |
| Video > `MAX_VIDEO_SECONDS` (config, mis. 180s) | Transkrip segmen pertama saja + catatan di report | Jangan gantung worker berjam-jam |
| Video tanpa suara & tanpa teks & tanpa caption | Langsung `unverified` + penjelasan | Tidak ada konten yang bisa diverifikasi |

> Tidak ada magic number: `MAX_VIDEO_SECONDS`, `OCR_FRAME_INTERVAL`, model whisper, dst. semua di `Settings` (env), bukan konstanta inline.

---

## 3. Analysis Job & State Machine

### 3.1 Diagram state

```
                    ┌──────────── retry (attempt < max_retries) ────────────┐
                    ▼                                                       │
 PENDING ─▶ QUEUED ─▶ FETCHING ─▶ TRANSCRIBING ─▶ VERIFYING ─▶ RENDERING ─▶ UPLOADING ─▶ DONE
             │           │            │              │             │            │
             │           └──▶ FAILED ◀┴──────────────┴─────────────┴────────────┘
             │                    │
             └──▶ FALLBACK_ANALYSIS ─▶ DONE   (degraded: caption-only / unverified)
```

Transisi **selalu** dipicu event (fungsi sukses/gagal), bukan timer. Satu-satunya "waktu" adalah:
- `timeout` per subprocess/HTTP (config, kelas error `TimeoutError`),
- backoff antar-retry (config: exponential + jitter; bukan magic — policy).

### 3.2 Tabel transisi

| Dari | Event | Ke | Syarat |
|---|---|---|---|
| `PENDING` | worker pickup | `QUEUED` | job diambil dari queue |
| `QUEUED` | mulai eksekusi | `FETCHING` | slot concurrency tersedia |
| `FETCHING` | caption+media siap | `TRANSCRIBING` | perlu transkrip (§2.3) |
| `FETCHING` | caption cukup | `VERIFYING` | jalur caption-only |
| `FETCHING` | error download | `FALLBACK_ANALYSIS` | error class = unreachable/private |
| `FETCHING` | error transient | `FAILED` | retry habis → `FALLBACK_ANALYSIS` |
| `TRANSCRIBING` | transkrip/OCR siap | `VERIFYING` | hasil non-kosong (gabung caption) |
| `TRANSCRIBING` | semua kosong | `FALLBACK_ANALYSIS` | verdict `unverified`, catatan "konten tidak bisa diekstrak" |
| `VERIFYING` | JSON valid ter-parse | `RENDERING` | skema lolos validasi |
| `VERIFYING` | JSON invalid / timeout | `FAILED`→retry | retry max → fallback `unverified` |
| `RENDERING` | markdown tersimpan | `UPLOADING` | file .md ada di disk |
| `UPLOADING` | 201 Created | `DONE` | simpan `public_url` |
| `UPLOADING` | 409 Conflict | `DONE` | idempotent: ambil URL report lama |
| `UPLOADING` | 5xx/timeout | `FAILED`→retry | retry max → simpan lokal + kirim status |

### 3.3 Skema job (SQLite `jobs`)

```json
{
  "id": "job_<uuid>",                 // internal, acak
  "report_id": "instagram_17841439294248081",  // = {platform}_{media_id}, deterministik
  "platform": "instagram",
  "media_id": "17841439294248081",
  "media_url": "https://...",         // dari payload
  "title": "",                        // dari payload
  "claim_text": "vaksin bikin magnet",
  "sender_id": "1338129438379972",
  "state": "FETCHING",                // state machine
  "attempts": 1,
  "last_error": null,                 // kelas error utk klasifikasi retry
  "artifacts": {                      // hasil tiap stage
    "caption": "...",
    "transcript": "...",
    "ocr_text": "...",
    "transcript_segments": []
  },
  "llm_result": null,                 // verdict JSON ter-validasi
  "markdown_path": "data/reports/ig_xxx.md",
  "public_url": null,                 // https://factbot.tech/r/{id}
  "created_at": "...", "updated_at": "..."
}
```

- **Persistence:** tabel `jobs` di SQLite (`DATABASE_URL` sudah ada). Update state **setiap transisi** (satu UPDATE, murah).
- **Crash recovery:** saat startup, `UPDATE jobs SET state='PENDING' WHERE state IN ('FETCHING','TRANSCRIBING','VERIFYING','RENDERING','UPLOADING')` lalu requeue. Tidak ada job menggantung selamanya.
- **Queue:** MVP = `asyncio.Queue` in-process + `Semaphore` (concurrency config, mis. 2 media job paralel — muat 4 vCPU). Post-hackathon: Redis (`REDIS_URL` sudah di config) + RQ/Dramatiq untuk multi-worker & retry terkelola.
- **Worker:** task asyncio `worker_loop()` di-start di `lifespan` (setelah webhook router) — event-driven murni, tanpa polling.

### 3.4 Struktur file yang diusulkan

```
app/pipeline/
  __init__.py
  jobs.py          # dataclass Job + repo SQLite (create/get/update_state/requeue_stale)
  queue.py         # asyncio.Queue + Semaphore + worker_loop (start/stop di lifespan)
  extract.py       # caption fetch (Graph API) + keputusan jalur (caption-only vs full)
  media.py         # yt-dlp download + ffmpeg audio + frame sampling (subprocess)
  transcribe.py    # faster-whisper (base/small, vad_filter) + OCR pytesseract
  verify.py        # LLM call → JSON verdict + validasi Pydantic
  render.py        # template markdown → file .md
  upload.py        # POST factbot.tech/api/v1/reports + 409/retry handling
  reply.py         # kirim hasil/fallback ke DM (reuse _reply_dm di webhooks/meta.py)
```

---

## 4. Tahap LLM (VERIFYING)

### 4.0 Router decision: direct LLM vs sub-agent (konsep orchestrator)

> Detail penuh: `docs/architecture_multi_platform.md` §12.5. Ringkasan untuk pipeline:

```
VERIFYING dimulai → ROUTER (decision, berbasis KEBERADAAN DATA, bukan magic number)
  ├─ SIMPLE  (caption/title + claim jelas, TANPA video)  → direct LLM (~3s)  ✅ realtime
  ├─ MEDIUM  (perlu cari sumber eksternal)               → search + LLM paralel (~8s)
  └─ COMPLEX (video + transkrip + OCR + multi-source)    → SPAWN SUB-AGENTS (30-60s)
```

- **Phase 1 (MVP):** hanya jalur SIMPLE — direct LLM via `PARKEE_PROXY_URL`. Real-time.
- **Phase 2+:** tambah jalur MEDIUM/COMPLEX bertahap. Threshold = konfigurasi (Settings),
  bukan konstanta — prinsip "tanpa magic number".
- **Kenapa bertahap:** sub-agent (Hermes brain / delegate) menambah latency 30-60s dan
  biaya multi-call; tidak cocok untuk ~80% kasus yang cuma butuh satu panggilan LLM.
  Realtime = router cerdas, bukan selalu sub-agent.

### 4.1 Arsitektur panggilan

```
app/pipeline/verify.py
  └─ llm_complete(messages, json_mode=True, timeout=LLM_TIMEOUT)  # httpx POST {PARKEE_PROXY_URL}/chat/completions
       headers: Authorization: Bearer {PARKEE_API_KEY}   # kalau proxy butuh key
       body   : {model: deepseek-v3, messages: [...], temperature: 0.2,
                 response_format: {type: "json_object"}}
  └─ parse & validate dgn Pydantic (VerifierResult) → kalau invalid → retry
```

- Pakai **httpx langsung** (OpenAI-compatible endpoint = satu POST sederhana) → tidak perlu dependency `openai` SDK. Cukup tambah ke `requirements.txt`: `yt-dlp`, `faster-whisper`, `pytesseract`, `opencv-python-headless`, `Pillow`.
- **Penting:** verifikasi yang diverifikasi adalah **claim text user**, dengan transkrip/caption/OCR sebagai *evidence*. LLM tidak boleh mengarang fakta — kalau evidence tidak cukup → `unverified`.

### 4.2 Prompt skeleton

```
SYSTEM:
Kamu adalah FactBot, asisten fact-checker berbahasa Indonesia. Tugasmu: memverifikasi
sebuah KLAIM berdasarkan BUKTI yang diberikan. Jangan menambahkan fakta dari luar bukti.
Jika bukti tidak cukup untuk memutuskan, keluarkan verdict "unverified".

Verdict hanya salah satu dari:
- "fact"        → klaim benar berdasarkan bukti
- "hoax"        → klaim salah / menyesatkan berdasarkan bukti
- "partly_true" → klaim sebagian benar, sebagian salah
- "unverified"  → bukti tidak cukup untuk memutuskan

Balas HANYA JSON, tanpa teks lain, dengan skema:
{
  "verdict": "<fact|hoax|partly_true|unverified>",
  "category": "<politik|kesehatan|ekonomi|teknologi|sosial|lainnya>",
  "summary": "<1-2 kalimat kesimpulan ramah pengguna>",
  "claim": "<klaim utama yang diekstrak dari video/caption, 1 kalimat>",
  "evidence": ["<kutipan langsung dari transkrip/caption yang mendukung penilaian>"],
  "sources": ["<nama/jenis sumber, mis. 'Pernyataan resmi Kemenkes' — KOSONGKAN jika tidak ada>"],
  "confidence": 0.0-1.0,
  "notes": ["<kekurangan bukti / hal yang perlu diverifikasi manual>"]
}

USER:
Berikut data media yang perlu dianalisa.

KL AIM (dari pengirim):
{claim_text}

CAPTION MEDIA:
{caption}

TRANSCRIPT VIDEO:
{transcript}

TEKS OVERLAY (OCR):
{ocr_text}

Analisa klaim di atas. Jika transkrip/OCR kosong, gunakan caption saja.
```

### 4.3 Validasi output

- Pydantic `VerifierResult` — `verdict` harus enum 4 nilai; `confidence` di [0,1]; JSON invalid / skema gagal → **retry (maks 2, config)** dengan pesan error di-inject: *"Output kamu bukan JSON valid. Balas hanya JSON."*
- Normalisasi `verdict` → label Indonesia untuk reply & markdown:
  `fact→✅ FAKTA`, `hoax→❌ HOAX`, `partly_true→⚠️ SEBAGIAN BENAR`, `unverified→❔ BELUM DAPAT DIVERIFIKASI` (konsisten dgn `format_verdict` di `app/api/reply.py`).

### 4.4 Template Markdown (RENDERING)

Struktur mengikuti contoh `docs/test_prabowo_nuklir.md`:

```
# Hasil Analisa: {claim pendek}
> **Kesimpulan: {emoji} {LABEL}** — {summary}
---
## Klaim yang Dianalisa
{claim}
## Bukti dari Media
- **Caption:** {caption}
- **Transkrip (kutipan):** {evidence bullet}
- **Teks overlay:** {ocr_text}
## Klaim vs Fakta
| Klaim | Status | Catatan |
## Sumber Rujukan
{numbered sources — kosong + catatan jika unverified}
## Catatan Penting
{notes + konteks}
---
*Dokumen ini dihasilkan otomatis oleh FactBot. Selalu cek sumber resmi sebelum menyebarkan informasi.*
```

---

## 5. Fallback & Graceful Degradation

**Aturan emas: setiap kegagalan punya jalur keluar, dan bot selalu membalas DM.**

| Tahap gagal | Kelas error | Aksi | Balasan ke user |
|---|---|---|---|
| Fetch caption (Graph API) | transient (5xx/timeout) | retry 2× (config) → pakai `title` payload | — (tetap lanjut) |
| Download video (yt-dlp) | unreachable/private/deleted | **caption-only** + flag `video_unavailable` di report | verdict dari caption + catatan "video tidak dapat diakses" |
| Download video | transient (rate-limit) | retry w/ backoff | — |
| Transkrip kosong | — (data-driven) | **OCR frames + caption** | — |
| Transkrip + OCR kosong | — | verdict `unverified`, notes "konten tidak bisa diekstrak" | URL report tetap terkirim |
| LLM timeout/5xx | transient | retry 2× (config, backoff+jitter) | — |
| LLM JSON invalid terus | non-transient | verdict `unverified` + summary "analisa gagal" | URL report (unverified) |
| Upload 409 Conflict | idempotent | **GET report lama** → reuse `public_url` | URL lama terkirim (tanpa duplikat) |
| Upload 5xx/timeout | transient | retry w/ backoff → simpan markdown lokal | DM: "report sedang dibuat, coba tag lagi nanti" + log |
| Upload 401/403 | config error | `FAILED` permanen + alert log | DM maaf, hubungi admin |
| Semua gagal total | — | `FAILED` | DM: "Maaf, gagal menganalisa media ini. Coba kirim ulang." |

**Prinsip retry:** klasifikasi error → `transient` (timeout, 5xx, rate-limit, network) retry dengan backoff eksponensial + jitter (max `MAX_RETRIES`, config); `non-transient` (4xx, JSON invalid, unreachable) langsung fallback — tidak buang waktu.

---

## 6. Idempotensi & Upload

### 6.1 ID deterministik

```
report_id = f"{platform}_{media_id}"        # contoh: "instagram_17841439294248081"
```

- Dibuat **sekali di job creation** (sebelum analisa) → dipakai ulang di setiap retry/attempt → upload hanya boleh terjadi sekali.
- Media yang sama dikirim lagi (claim sama) → `report_id` sama → server platform balas **409** → bot **GET** report yang sudah ada → kirim `public_url` yang sama ke user. Tidak ada duplikat.
- **Edge case:** payload DM `ig_reel` kadang **tidak punya `media_id`** (hanya url+title). Fallback deterministik: `report_id = f"{platform}_url_{sha1(media_url)[:16]}"`.
- **Caveat (opsional, post-hackathon):** dua claim berbeda pada reel yang sama akan memakai report yang sama. Kalau mau report per-claim: `report_id = f"{platform}_{media_id}_{sha1(claim_text)[:8]}"`. MVP ikut spec `{platform}_{media_id}`.

### 6.2 Alur upload

```
RENDERING selesai → markdown_path siap
   │
   ▼
POST https://factbot.tech/api/v1/reports
   headers: Authorization: Bearer {REPORTS_API_KEY}
   json:    { id: report_id, title, content_markdown, source: {platform, media_id, media_url}, ... }
   timeout: max(30s, config)            # spec platform ≥30s
   │
   ├─ 201 → {url: "https://factbot.tech/r/{id}"} → simpan public_url → DONE → reply DM
   ├─ 409 → GET /api/v1/reports/{report_id} → ambil url lama → DONE → reply DM (reuse)
   ├─ 5xx/timeout → retry (backoff) → habis → fallback §5
   └─ 4xx lain → FAILED permanen + alert
```

---

## 7. Prioritas MVP (16 Agt) vs Post-hackathon

| # | MVP — wajib sebelum 16 Agt | Post-hackathon |
|---|---|---|
| 1 | **Caption-only path dulu** (claim + caption/title → LLM → markdown → upload → DM). Zero dep baru; demo jalan di hari pertama. | — |
| 2 | **State machine + SQLite jobs + asyncio worker** (crash-safe, retry, recover startup) | Redis + RQ/Dramatiq, multi-worker, dashboard job |
| 3 | **Full media path**: yt-dlp + ffmpeg + faster-whisper `base`/`small` + VAD (di VPS 4 vCPU) | Model `medium`/distil, bahasa auto-detect, diarisasi per segmen |
| 4 | **OCR pytesseract** frame sampling (stretch goal — aktifkan kalau waktu cukup) | Ganti VLM (vision LLM) utk overlay + analisa visual frame |
| 5 | **Idempotensi + 409 reuse** + retry class-based | — |
| 6 | Graceful degradation penuh (§5) | Queue DLQ + alerting (Prometheus/Grafana) |
| 7 | Verdict 4-level + kategori + sources dari konten media saja | **Source retrieval eksternal** (search API/news) → evidence lebih kuat, RAG + knowledge base |
| 8 | Logging structured ke `logs/` | Tracing (OpenTelemetry), sentry |

**Estimasi latency jalur penuh (4 vCPU, angka indikatif — config, bukan janji):** reel 60 detik ≈ download 10–30s + transkripsi base 60–120s + LLM 10–30s → **total ~2–3 menit**. Jalur caption-only: **< 30 detik**. Ini masih "realtime" untuk UX DM (ack instan dikirim duluan).

**Urutan implementasi MVP (minggu):**
1. Minggu 1: jobs + worker + caption-only path + upload + reply DM (E2E demo).
2. Minggu 2: full media path (yt-dlp/ffmpeg/whisper) + fallback + idempotensi.
3. Buffer: OCR + pengujian E2E + hardening (crash recovery, error classes).

---

## 8. Rekomendasi konkret (ringkas)

1. **Jangan tunda jalur caption-only** — 80% demo value, 0 dependency berat. Full pipeline adalah upgrade di atasnya, bukan prasyarat.
2. **State machine + SQLite sejak hari pertama** — biayanya kecil (1 tabel, 1 worker loop) dan menyelamatkan dari job menggantung saat demo.
3. **faster-whisper `base` + VAD + batas durasi video** — cukup utk akurasi ID di MVP; `small` kalau kualitas kurang.
4. **LLM satu panggilan terstruktur** (JSON mode, Pydantic validation, retry 2×) — jangan pecah jadi multi-call dulu.
5. **Idempotensi non-negotiable** — `report_id` deterministik + handle 409, karena webhook Meta bisa deliver duplikat & user bisa resend.
6. **Selalu balas DM** — fallback paling buruk pun berupa pesan yang jelas, bukan silence (webhook retry Meta akan spam kalau endpoint error).
7. **Semua angka = config** (`Settings`/env): `MAX_RETRIES`, `LLM_TIMEOUT`, `MAX_VIDEO_SECONDS`, `OCR_FRAME_INTERVAL`, `WORKER_CONCURRENCY`, `UPLOAD_TIMEOUT` — nol magic number di kode.
