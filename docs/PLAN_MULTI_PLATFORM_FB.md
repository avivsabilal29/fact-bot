# PLAN MULTI-PLATFORM: Facebook Messenger (object=page)

> **Tujuan:** Dukungan **Facebook Messenger (DM Page)** — SAMA PERSIS dengan Instagram:
> routing caption-based, state machine pending_claims, replies, progress DM, job pipeline,
> URL faktabot. Arsitektur **deterministik**: 1 engine, N bridge (platform-aware), tanpa
> kondisi platform di dalam logika analisa.
>
> **Prinsip:** IG sudah terbukti E2E. FB diimplementasikan sebagai **bridge kedua** yang
> memetakan payload Meta → format internal yang SAMA — engine/pipeline TIDAK berubah.

---

## 1. Konteks: Pattern Webhook FB (dari log produksi 2026-08-06)

### 1.1 DM biasa (text-only)
```json
{"object": "page", "entry": [{"time": ..., "id": "1211134082089748", "messaging": [{
  "sender": {"id": "28230267056577003"},
  "recipient": {"id": "1211134082089748"},
  "message": {"mid": "m_jRAXeMgW4p3LyyvY_...", "text": "Saya aviv sabilal"}
}]}]}
```

### 1.2 DM reel + message (2 webhooks terpisah — SAMA seperti IG!)
```json
{"object": "page", "entry": [{"id": "1211134082089748", "messaging": [{
  "sender": {"id": "28230267056577003"},
  "message": {"mid": "m_balN1ty8ejquE81gaWrrmC...",
    "attachments": [{"type": "reel", "payload": {
      "url": "https://www.facebook.com/reel/4536917899877426?fs=e&s=m",
      "title": "Her Baby Arrived at 35,000 Feet! ...",
      "reel_video_id": 4536917899877426
    }}]}
}]}]}
```
Lalu webhook kedua (text):
```json
{"message": {"mid": "m_IE0KmBj59QRr3YEn10FtMyc...", "text": "ini video apa?"}}
```

### 1.3 DM feed/post + message
```json
{"message": {"mid": "m_XHbt1cOi1M8lS3tAI_kPqS...",
  "attachments": [{"type": "post", "payload": {
    "url": "https://www.facebook.com/mutiyatyiara.tyiarrap/posts/pfbid02t9ddfZJTn2vF5qFwBMZabrH...",
    "title": "Awwwwwwhhhh 😂😂😂",
    "id": 2129139887642734
  }}]}}
```

### 1.4 Mention (komentar di postingan) — BUKAN DM, tapi TETAP dibalas MENTION_REPLY
```json
{"object": "page", "entry": [{"changes": [{"field": "mention", "value": {
  "message": "FactCheck-FB video apa",
  "post_id": "669976192871967_122197857164920798",
  "comment_id": "122197857164920798_2719805541795582",
  "item": "comment", "verb": "add"
}}]}]}
```
> ⚠️ **Mention = komentar, BUKAN DM** — payload TIDAK berisi sender id, cuma comment_id.
> **Behavior (disamain dgn IG):** balas `MENTION_REPLY` ("Sorry, this feature is not
> supported yet — we're still in development phase. 🙏") **via DM ke commenter**.
> Alur: `field==mention` → `GET /{comment_id}?fields=from{id,name},message` (ambil PSID
> commenter) → `_reply_dm(from_id, MENTION_REPLY, "facebook")` → best-effort
> (fetch/reply gagal = log warning, jangan crash). Kalau reply DM gagal → fallback
> `POST /{comment_id}/replies` (butuh `pages_manage_comments` — mungkin 400, graceful).

---

## 2. Mapping Payload: Instagram vs Facebook

| Aspek | Instagram (terbukti) | Facebook (target) | Normalisasi |
|---|---|---|---|
| `object` | `"instagram"` | `"page"` | `platform = "facebook" if object=="page" else "instagram"` |
| sender id | IG-scoped ID (`1338129438379972`) | PSID (`28230267056577003`) | **tanpa perubahan — cuma string** |
| mid | `aWdfZAG...` (base64) | `m_...` | untuk dedup `_mark_processed` (gak beda) |
| attachment type | `ig_reel` / `ig_post` | `reel` / `post` | **sudah include di `ACCEPTED_MEDIA_TYPES`** ✅ |
| media id | `reel_video_id` / `ig_post_media_id` | `reel_video_id` (angka) / `id` | `media_id = reel_video_id or ig_post_media_id or payload.id` |
| media url | lookaside CDN → normalize `/reel/{id}` `/p/{id}` | `facebook.com/reel/{id}` / `.../posts/pfbid...` | **sudah publik, TIDAK perlu normalize** ✅ |
| reply endpoint | `graph.instagram.com/v26.0/{ig_bid}/messages` + IG token | `graph.facebook.com/v26.0/{page_id}/messages` + **Page token** | **PILIHAN endpoint per platform** |
| DM detail fetch | `graph.instagram.com/v26.0/{mid}` | `graph.facebook.com/v26.0/{mid}?fields=...` | **PILIHAN endpoint** (atau skip — logging only) |
| report_id | `instagram_{media_id}` | `facebook_{media_id}` | **sudah platform-aware di `build_report_id`** ✅ |

---

## 3. Arsitektur Deterministik: 1 Engine, N Bridge

```
                    ┌─────────────────────────────────────────────┐
                    │          app/webhooks/meta.py               │
                    │        (BROKER — platform-aware)            │
                    │  object=instagram → normalize IG            │
                    │  object=page      → normalize FB            │
                    └──────────────┬──────────────────────────────┘
                                   │ format internal (sudah ada):
                                   │   {sender_id, msg_text, attachments,
                                   │    media:{url,title,media_id,...}, platform}
                                   ▼
                    ┌─────────────────────────────────────────────┐
                    │          handle_message() (state machine)   │
                    │  template→MENTION · media→ACCEPT+pending    │
                    │  text+pending→CLAIM · text→DENY · echo→skip │
                    └──────────────┬──────────────────────────────┘
                                   │ platform diteruskan ke _reply_dm
                                   ▼
        ┌──────────────────────────┴──────────────────────────┐
        │  _reply_dm(recipient, text, platform)               │
        │  instagram → graph.instagram.com/... + IG token     │
        │  facebook  → graph.facebook.com/... + PAGE token    │
        └──────────────────────────┬──────────────────────────┘
                                   ▼
        ┌──────────────────────────┴──────────────────────────┐
        │  _create_claim_job(media, claim, sender, platform)  │
        │  report_id = build_report_id(platform, media_id)    │
        │  job["platform"] = platform  → worker → faktabot    │
        └─────────────────────────────────────────────────────┘
```

**Aturan deterministik (kunci):**
1. `platform` ditentukan **sekali** di broker (dari `payload.object`) → disimpan di `msg["_platform"]`
2. `_reply_dm` / `_create_claim_job` **menerima platform sebagai param** — TIDAK menebak
3. Worker & pipeline **TIDAK tahu platform** untuk logika analisa — platform cuma dipakai
   di (a) endpoint reply, (b) report_id prefix
4. Reply selalu via **bridge yang sesuai** — kalau salah, gagal 400 (yang sekarang terjadi)

---

## 4. Perubahan Kode (spesifik per file)

### 4.1 `app/webhooks/meta.py` — BROKER + platform routing

**a. handle_webhook (baris ~219-227):** tandai platform di tiap msg
```python
messaging = entry.get("messaging", [])
platform = "facebook" if payload.get("object") == "page" else "instagram"
for msg in messaging:
    msg["_platform"] = platform
    await handle_message(msg)
```

**b. `_reply_dm(recipient_id, text, platform="instagram")`** — pilih endpoint
```python
if platform == "facebook":
    page_id, token = settings.meta_page_id, settings.meta_page_access_token
    url = f"https://graph.facebook.com/v26.0/{page_id}/messages"
else:
    ig_id, token = settings.ig_business_id, settings.ig_user_token or settings.ig_basic_token
    url = f"https://graph.instagram.com/v26.0/{ig_id}/messages"
payload = {"recipient": {"id": recipient_id}, "message": {"text": text}}
# POST + error handling sama seperti sekarang
```

**c. `handle_message`:** baca platform dari value, teruskan ke semua `_reply_dm`
```python
platform = value.get("_platform", "instagram")
# ... semua call: await _reply_dm(from_id, REPLY, platform)
```

**d. `_create_claim_job(media, claim_text, sender_id)`** — terima platform
```python
# media dict sekarang juga bawa "_platform" (dari pending_claims)
platform = media.get("_platform", "instagram")
...
job = {"report_id": build_report_id(platform, media_id, media_url),
       "platform": platform, ...}
```

**e. pending_claims simpan `_platform`**
```python
pending_claims[from_id] = {..., "_platform": platform}
```

**g. Handler `field == "mention"` (FB)** — balas MENTION_REPLY via DM ke commenter:
```python
elif field == "mention":
    # FB mention = komentar. Ambil commenter via GET /{comment_id}?fields=from,
    # lalu balas MENTION_REPLY via DM (facebook). Best-effort: gagal = log warning.
    comment_id = value.get("comment_id", "")
    if comment_id and platform == "facebook":
        from_id = await _fetch_fb_commenter(comment_id)   # GET /{comment_id}?fields=from
        if from_id:
            await _reply_dm(from_id, MENTION_REPLY, "facebook")
        # fallback: reply komentar langsung (butuh pages_manage_comments — best-effort)
    else:
        logger.info("  ⏭️ Mention tanpa comment_id — no-op")
```
`_fetch_fb_commenter` (helper baru): GET `graph.facebook.com/v26.0/{comment_id}?fields=from{id,name}` + Page token → return `from.id` atau None.

### 4.2 `app/api/reply.py` — send_result_dm / send_progress_dm platform-aware
```python
async def send_result_dm(sender_id, public_url, platform="instagram"):
    if platform == "facebook":
        url = f"https://graph.facebook.com/v26.0/{settings.meta_page_id}/messages"
        token = settings.meta_page_access_token
    else:
        url = f"https://graph.instagram.com/v26.0/{settings.ig_business_id}/messages"
        token = settings.ig_user_token or settings.ig_basic_token
    # body sama: {"recipient": {...}, "message": {"text": ...}}
```
> Perlu juga: `send_progress_dm(sender_id, text, platform="instagram")` — worker panggil
> dengan platform dari job.

### 4.3 `app/worker.py` — teruskan platform ke DM
```python
# _notify_user / _notify_result: ambil platform dari job
platform = job.get("platform", "instagram")
await send_result_dm(sender_id, url, platform=platform)
# progress: await send_progress_dm(sender_id, text, platform=platform)
```

### 4.4 `app/jobs.py` — sudah platform-aware ✅ (build_report_id). TIDAK perlu diubah.

### 4.5 `app/config.py` — tidak perlu field baru (META_PAGE_ID / META_PAGE_ACCESS_TOKEN
sudah ada). ✅

---

## 5. Behavior Replies (IDENTIK dengan IG — tidak berubah)

| Event | Reply (EN, sama persis) | Trigger |
|---|---|---|
| Mention (template) | `MENTION_REPLY` | attachments contains `template` |
| Reel/Post diterima | `ACCEPT_REPLY` → pending | attachment type ∈ {reel, post, ig_reel, ig_post, image, video} |
| Text + pending ada | `CLAIM_RECEIVED_REPLY` → job dibuat | text & pending[from_id] |
| Text tanpa pending | `DENY_REPLY` | text & no pending |
| Echo bot sendiri | skip (anti-loop) | sender == bot |
| Read/delivery | skip | no "message" key |

**State machine `pending_claims` — SATU dict untuk semua platform** (key = sender id;
PSID & IG ID tidak pernah bertabrakan karena beda namespace). ✅

---

## 6. Env & Konfigurasi (sudah ada di .env)

```
META_PAGE_ID=1211134082089748
META_PAGE_ACCESS_TOKEN=<EAAV... pages_messaging>   # sudah ada
IG_USER_TOKEN / IG_BASIC_TOKEN                     # sudah ada
```

**Catatan:** token FB sekarang `pages_messaging` — cukup untuk **DM** (reply + typing).
Mention/komentar butuh `pages_manage_comments` → **out of scope** (lihat §7).

---

## 7. Out of Scope (dokumentasi saja)

| Fitur | Alasan | Butuh |
|---|---|---|
| Mention/komentar reply | Token kurang permission | `pages_manage_comments` + endpoint `/comment_id/replies` |
| FB typing indicator | Belum di-verifikasi via Page token (baru IG) | Test `sender_action` via `/page_id/messages` |
| Multi-user concurrency FB | Sama dengan IG (dict per sender) | — |

---

## 8. Test Plan (kasus nyata dari log lo)

| # | Kasus | Payload | Expected |
|---|---|---|---|
| T1 | DM text-only | "Saya aviv sabilal" | DENY_REPLY via FB endpoint (HTTP 200) |
| T2 | DM reel | reel 4536917899877426 | ACCEPT_REPLY + pending (200) |
| T3 | DM text setelah reel | "ini video apa?" | CLAIM_RECEIVED + job `facebook_4536917899877426` |
| T4 | DM feed | post pfbid02t9... id 2129139887642734 | ACCEPT_REPLY + pending |
| T5 | DM text setelah feed | "ini feed apa" | CLAIM_RECEIVED + job `facebook_2129139887642734` |
| T6 | Echo / read | sender == page / no message | skip |
| T7 | **E2E**: klaim FB → analisa → URL faktabot | full flow | Result DM via FB (200), report live |
| T8 | Regression IG | payload IG lama | semua reply tetap via IG (200) |

**Gate:** semua reply HTTP 200 via endpoint platform yang benar; report_id prefix benar;
no cross-platform reply (FB gak pernah kirim via IG).

---

## 9. Deployment & Rollback

```bash
# Deploy: rsync + rebuild bot di VPS
rsync -az app/ root@169.58.111.12:/opt/factbot/app/
ssh root@169.58.111.12 "cd /opt/factbot && docker compose up -d --build bot"

# Test: kirim DM FB (lo) → liat log bot → verifikasi reply 200

# Rollback: git checkout meta.py reply.py worker.py → rsync → rebuild
```

---

*Dokumen ini = plan eksekusi. Implementasi: sub-agent paralel (broker/meta.py, reply.py+worker.py,
verifikasi read-only), lalu test E2E dari log nyata §8.*
