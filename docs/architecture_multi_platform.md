# Arsitektur Multi-Platform FactBot — "Engine Sama, Bridge Beda"

> **Konsep:** hexagonal / port-adapter. Satu *engine* fact-check (platform-agnostic),
> tiap platform hanyalah *bridge* (adapter) masuk/keluar.
> **Target:** MVP Instagram (16 Agt 2026, hackathon) — arsitektur siap tambah FB / X / WA tanpa ubah core.
> **Status:** dokumen desain. Implementasi saat ini: `app/webhooks/meta.py` (monolith IG, 478 baris).

---

## 1. Prinsip & Gambar Besar

```
                    ┌────────────────────────────────────────────────┐
                    │              CORE  (engine)                    │
                    │  app/core/  — TIDAK BOLEH import platforms     │
                    │                                                │
                    │  models.py   (IncomingEvent, OutgoingReply,    │
                    │               AttachmentKind, MediaRef)        │
                    │  ports.py    (PlatformBridge = PORT / kontrak) │
                    │  engine.py   (dedup → echo-skip → route → ack)│
                    │  router.py   (state machine pending_claims)    │
                    │  verifier.py (pipeline fact-check → result)    │
                    │  replies.py  (template balasan + formatter)    │
                    │  queue.py    (asyncio worker queue)            │
                    └──────▲───────────────────────────┬────────────┘
                           │ implements (adapter)      │ memanggil port
              ┌────────────┼───────────┬───────────────┼──────────────┐
   ADAPTERS   │ IG bridge  │ FB bridge │ X bridge      │ WA bridge    │
 (app/plat-  │ parse/send │ parse/send│ parse/send    │ parse/send   │
   forms/)    └─────┬──────┴─────┬────┴──────┬─────────┴──────┬───────┘
                    │            │           │               │
   INBOUND         │ Meta       │ Meta      │ POLLING       │ Baileys
   (transport      │ webhook    │ webhook   │ (X DM webhook │ localhost
    = detail       │ demux      │ demux     │ = tier       │ :3000 →
    adapter)       │            │           │  berbayar)   │ forward
                    │            │           │               │
   OUTBOUND        │ graph.insta │ graph.fb  │ X API v2 DM   │ POST
                    │ -gram.com   │ .com      │ (media upload│ :3000/send
                    │ /messages   │ Messenger │ + DM)        │
                    └────────────┴───────────┴───────────────┴────────┘
```

**Aturan emas (arah dependensi):**
- `app/main.py` → `app/platforms/` → `app/core/`  (satu arah, tidak pernah sebaliknya)
- `app/core/` hanya tahu `ports.py` + `models.py`. Core bisa diuji dengan `FakeBridge` tanpa jaringan.
- Transport masuk (webhook vs polling vs localhost bridge) adalah **detail adapter** — port tidak peduli.
- Menambah platform = tambah 1 folder di `app/platforms/` + 1 baris registry. Core **tidak disentuh**.

---

## 2. Struktur Direktori

`app/platforms/` (bukan `app/bridges/`) — alasan: (a) "bridge" kita pakai sebagai nama kelas
`PlatformBridge` dan (b) Baileys *bridge* adalah proses Node terpisah; nama folder "bridges" bakal
ganda arti. Folder = platform, kelas = bridge.

```
fact_bot_server/
├── app/
│   ├── main.py                  # FastAPI app; include router dari bridge yang enabled
│   ├── config.py                # AppSettings aggregator + Settings per platform (env prefix)
│   ├── core/                    # ★ ENGINE — platform-agnostic, tanpa network, tanpa import platforms
│   │   ├── __init__.py
│   │   ├── models.py            # Platform, AttachmentKind, MediaRef, IncomingEvent,
│   │   │                        #   OutgoingReply, SendResult, Claim
│   │   ├── ports.py             # class PlatformBridge(ABC) — THE PORT (3 method)
│   │   ├── engine.py            # handle_event(): dedup → echo-skip → route → ack → enqueue
│   │   ├── router.py            # state machine pending_claims per sender (murni, tanpa IO)
│   │   ├── verifier.py          # pipeline fact-check (stub sekarang; inti bisnis masa depan)
│   │   ├── replies.py           # template balasan + formatter per platform (truncate, markdown)
│   │   └── queue.py             # asyncio.Queue + worker loop (retry, timeout, backpressure)
│   ├── platforms/               # ★ BRIDGES — satu folder per platform
│   │   ├── __init__.py          # registry: build_bridges(settings) -> dict[Platform, PlatformBridge]
│   │   ├── meta/                # transport BERSAMA IG+FB (1 app Meta, 1 callback URL)
│   │   │   ├── __init__.py
│   │   │   ├── transport.py     # verifikasi X-Hub-Signature-256 + handshake hub.challenge
│   │   │   └── router.py        # GET/POST /webhooks/meta → DEMUX → bridge IG / bridge FB
│   │   ├── instagram/
│   │   │   ├── __init__.py
│   │   │   ├── bridge.py        # InstagramBridge: parse changes[]+messaging[] → IncomingEvent
│   │   │   │                    #   send: POST graph.instagram.com/{ig_id}/messages
│   │   │   └── schemas.py       # (opsional) model pydantic payload mentah IG
│   │   ├── facebook/
│   │   │   ├── __init__.py
│   │   │   └── bridge.py        # FacebookBridge: parse feed/messaging page → send Graph API
│   │   ├── x/
│   │   │   ├── __init__.py
│   │   │   ├── bridge.py        # XBridge: inbound via POLLING (webhook DM = tier berbayar),
│   │   │   │                    #   send: X API v2 POST /2/dm_conversations/{id}/messages
│   │   │   └── poller.py        # asyncio task: poll DM conversations tiap N detik
│   │   └── whatsapp/
│   │       ├── __init__.py
│   │       ├── bridge.py        # WhatsAppBridge: inbound via forward dari Baileys bridge,
│   │       │                    #   send: POST http://localhost:3000/send {chatId, message}
│   │       └── webhook.py       # POST /webhooks/whatsapp (diterima dari bridge Node)
│   ├── api/                     # endpoint admin/test (existing /test/reply, /health)
│   └── webhooks/                # LEGACY → dimigrasi ke platforms/, lalu dihapus
├── docs/
│   ├── architecture_multi_platform.md   # ← dokumen ini
│   └── webhook_state_machine.md         # spesifikasi perilaku state machine (jadi acuan core/router.py)
├── tests/
│   ├── test_engine.py           # core murni + FakeBridge — replay skenario E2E A–F (tanpa jaringan)
│   ├── test_ig_bridge.py        # payload Meta mentah → IncomingEvent (golden)
│   ├── test_fb_bridge.py
│   └── test_wa_bridge.py
├── scripts/                     # (existing) funnel_watchdog.py
├── requirements.txt
└── .env
```

> `app/webhooks/meta.py` sekarang: 478 baris yang mencampur verifikasi + parsing 3 format payload +
> state machine + template balasan + HTTP send. Semua itu terpisah menjadi: `meta/transport.py` +
> `platforms/instagram/bridge.py` + `core/router.py` + `core/replies.py` + `core/engine.py`.

---

## 3. Port: `PlatformBridge` (satu kontrak, 3 method)

```python
# app/core/ports.py
"""PORT — kontrak yang diimplementasikan semua bridge. Core hanya bergantung pada ini."""
from abc import ABC, abstractmethod
from .models import Platform, IncomingEvent, OutgoingReply, SendResult, MediaRef


class PlatformBridge(ABC):
    """Satu bridge per platform: INBOUND (parse) + OUTBOUND (send).

    Sengaja SEMPIT: 3 method. Tidak ada port untuk repository, event bus,
    media resolver terpisah, dst. (lihat §8 untuk alasan anti-over-engineering).
    """

    platform: Platform  # di-set oleh subclass

    # ── INBOUND ──────────────────────────────────────────────────────
    @abstractmethod
    def parse(self, raw: dict) -> list[IncomingEvent]:
        """Normalisasi payload mentah platform → event kanonik.

        PURE + SYNC: tidak boleh network. Payload yang butuh enrich (mis. IG
        tidak menyertakan URL media) memakai resolve_media() di bawah — dipanggil
        core HANYA saat URL benar-benar dibutuhkan (klaim masuk).
        """

    # ── OUTBOUND ─────────────────────────────────────────────────────
    @abstractmethod
    async def send(self, reply: OutgoingReply) -> SendResult:
        """Kirim balasan kanonik ke platform. Adapter yang tahu cara map
        OutgoingReply → format API platform (DM vs komentar publik, dll)."""

    # ── ENRICH (opsional, default no-op) ─────────────────────────────
    async def resolve_media(self, event: IncomingEvent) -> MediaRef | None:
        """Isi URL/download info utk media yang cuma punya ID di payload.

        IG: GET graph.instagram.com/{media_id}?fields=permalink (seperti
        _get_media_caption sekarang). X/WA biasanya sudah bawa URL → biarkan default.
        """
        return event.media
```

**Kenapa `parse` sync & `send` async?** Parsing = transform dict→model, murni, mudah di-test.
Send = IO jaringan. Memisahkan keduanya membuat `core/engine.py` bisa diuji penuh tanpa network.

**Kenapa demux (IG vs FB) di luar bridge?** Karena IG & FB berbagi 1 callback URL Meta;
siapa yang dapat event apa ditentukan *sebelum* parse (oleh field / recipient.id). Itu urusan
transport bersama (`meta/router.py`), bukan urusan bridge.

---

## 4. Model Data Terpusat (canonical, platform-agnostic)

```python
# app/core/models.py
from enum import Enum
from pydantic import BaseModel, Field


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    FACEBOOK  = "facebook"
    X         = "x"
    WHATSAPP  = "whatsapp"


class AttachmentKind(str, Enum):
    """SATU enum internal utk semua jenis lampiran semua platform."""
    REEL     = "reel"      # IG ig_reel / FB video / X video / WA video
    POST     = "post"      # IG ig_post / FB post / X post link
    IMAGE    = "image"     # semua platform
    VIDEO    = "video"
    AUDIO    = "audio"     # WA voice note
    DOCUMENT = "document"  # WA file, FB file
    LINK     = "link"      # share/url biasa (WA link preview, X link)
    TEMPLATE = "template"  # IG template (pola mention/button) — BUKAN media klaim
    TEXT     = "text"      # teks polos tanpa media
    UNKNOWN  = "unknown"


class MediaRef(BaseModel):
    kind: AttachmentKind
    url: str | None = None
    title: str | None = None
    platform_id: str | None = None   # IG media_id / WA message id / dst.


class IncomingEvent(BaseModel):
    """SATU representasi 'pesan masuk' utk semua platform."""
    event_id: str                    # kunci DEDUP: mid / comment_id / WA message id
    platform: Platform
    sender_id: str                   # ID scoped platform (bukan global)
    sender_username: str | None = None
    text: str = ""
    media: MediaRef | None = None
    thread_id: str | None = None     # utk WA group / FB thread; None = 1-on-1
    raw: dict = Field(default_factory=dict, exclude=True)  # utk debug, tak dipakai core


class OutgoingReply(BaseModel):
    """SATU representasi 'balasan keluar'. Core menyusun; bridge yang mengirim."""
    platform: Platform
    recipient_id: str
    text: str | None = None
    media_url: str | None = None      # masa depan: kirim gambar/video hasil verifikasi
    reply_to_event_id: str | None = None  # opsional: balas dalam thread/komentar
    metadata: dict = Field(default_factory=dict)


class SendResult(BaseModel):
    ok: bool
    platform_message_id: str | None = None
    error: str | None = None


class Claim(BaseModel):
    """Klaim yang sedang diverifikasi (hasil normalisasi state machine)."""
    claim_id: str
    platform: Platform
    sender_id: str
    media: MediaRef | None
    text: str
```

**Peta field lama → baru (dari `meta.py`):**

| Sekarang (meta.py) | Baru (core/models.py) |
|---|---|
| `from_id` / `sender.id` | `IncomingEvent.sender_id` |
| `msg_text` / `text` | `IncomingEvent.text` |
| `mid` / `comment_id` | `IncomingEvent.event_id` (dedup) |
| `attachments[].type` ("ig_reel") | `MediaRef.kind` (`AttachmentKind.REEL`) |
| `pending_claims[sender] = {url,title,media_type}` | `Claim` + state di `core/router.py` |
| `_processed_ids` set | dedup di `core/engine.py` (bisa pindah ke Redis nanti) |

---

## 5. Normalisasi Attachment (tabel mapping per platform)

**Aturan:** tabel mapping hidup di ADAPTER (bukan core). Core hanya tahu `AttachmentKind`.
Tiap bridge punya fungsi `_normalize(raw_attachments) -> MediaRef | None`.

| Platform | Payload mentah (type) | → `AttachmentKind` | Catatan |
|---|---|---|---|
| IG DM | `attachments[].type == "ig_reel"` | `REEL` | payload.url = URL reel |
| IG DM | `"ig_post"` | `POST` | |
| IG DM | `"share"` (punya `payload.url/link`) | `LINK` | share posting orang |
| IG DM | `"template"` | `TEMPLATE` | = pola mention/button → bukan media klaim |
| IG komentar/mention | `media.media_product_type == REELS` | `REEL` | URL TIDAK ada → `resolve_media()` |
| IG komentar/mention | `FEED` | `POST` | ditto |
| FB Messenger | `message.attachments[].type` image/video/file | `IMAGE` / `VIDEO` / `DOCUMENT` | |
| FB Page feed | post/photo/video | `POST` / `IMAGE` / `VIDEO` | |
| X DM | `media[].type` image/video/gif | `IMAGE` / `VIDEO` | URL ikut di payload |
| X DM | teks ber-URL | `LINK` | |
| WA | `image` / `video` / `audio` / `document` | `IMAGE` / `VIDEO` / `AUDIO` / `DOCUMENT` | dari Baileys event |
| WA | teks polos | `TEXT` | |

Contoh (di `platforms/instagram/bridge.py`):

```python
# Mapping IG-specific → kanonik. HANYA ada di adapter.
_IG_ATTACHMENT_MAP = {
    "ig_reel":  AttachmentKind.REEL,
    "ig_post":  AttachmentKind.POST,
    "share":    AttachmentKind.LINK,
    "template": AttachmentKind.TEMPLATE,
}

def _normalize(self, attachments: list[dict]) -> MediaRef | None:
    for att in attachments or []:
        kind = _IG_ATTACHMENT_MAP.get(att.get("type", ""))
        if kind is None:
            continue
        payload = att.get("payload") or {}
        return MediaRef(
            kind=kind,
            url=payload.get("url") or payload.get("link"),
            title=payload.get("title"),
            platform_id=payload.get("media_id") or payload.get("id"),
        )
    return None
```

**Aturan routing yang sudah ditetapkan state machine (`docs/webhook_state_machine.md`)
diterjemahkan ke enum:**

| Kondisi | Aksi |
|---|---|
| media.kind ∈ {REEL, POST, IMAGE, VIDEO, LINK} | simpan pending Claim → balas ACCEPT |
| media.kind == TEMPLATE | balas MENTION (bukan klaim) |
| text + ada pending | klaim dikonsumsi → ack "verifying..." → enqueue |
| text tanpa pending | DENY |
| non-message / echo / event_id duplikat | skip |

---

## 6. Routing Webhook di FastAPI

**Satu endpoint per *keluarga transport*, bukan per platform:**

| Endpoint | Platform | Handshake | Kenapa |
|---|---|---|---|
| `GET/POST /webhooks/meta` | IG **+** FB | `hub.challenge` + `X-Hub-Signature-256` | 1 app Meta, 1 callback URL di dashboard. Payload didemux per event |
| `POST /webhooks/x` | X | CRC (crc_token) | **Catatan:** webhook DM X = tier enterprise. MVP X pakai **polling** (`poller.py`), endpoint ini opsional |
| `POST /webhooks/whatsapp` | WA | token sederhana | Baileys bridge (Node, `localhost:3000`) forward event masuk ke sini; atau polling `:3000` |

**Demux Meta** (`platforms/meta/router.py`):

```python
router = APIRouter(prefix="/webhooks/meta", tags=["meta"])

@router.get("")
async def verify(request: Request):            # hub.challenge (existing logic)
    ...

@router.post("")
async def meta_webhook(request: Request):
    body = await request.body()
    if not transport.verify_signature(body, request.headers):   # shared IG+FB
        raise HTTPException(403, "Invalid signature")
    payload = json.loads(body)

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field, value = change.get("field"), change.get("value", {})
            if field in {"comments", "mentions"}:        # → IG
                bridge = bridges[Platform.INSTAGRAM]
            elif field == "feed":                        # → FB page
                bridge = bridges[Platform.FACEBOOK]
            else:
                continue
            for ev in bridge.parse({"entry": entry, "change": change}):
                await engine.handle_event(ev)            # cepat: ack + enqueue, return

        for msg in entry.get("messaging", []):           # DM: IG atau Messenger
            recipient_id = (msg.get("recipient") or {}).get("id")
            if recipient_id == settings.instagram.business_id:
                bridge = bridges[Platform.INSTAGRAM]
            else:                                        # page id → Messenger
                bridge = bridges[Platform.FACEBOOK]
            for ev in bridge.parse({"entry": entry, "messaging": msg}):
                await engine.handle_event(ev)

    return {"status": "received"}                        # SELALU 200 cepat
```

**Registrasi di `main.py`** — via registry, supaya menambah platform tidak menyentuh main:

```python
# app/platforms/__init__.py
def build_bridges(settings) -> dict[Platform, PlatformBridge]:
    bridges = {}
    if settings.instagram.enabled:
        bridges[Platform.INSTAGRAM] = InstagramBridge(settings.instagram)
    if settings.facebook.enabled:
        bridges[Platform.FACEBOOK] = FacebookBridge(settings.facebook)
    if settings.x.enabled:
        bridges[Platform.X] = XBridge(settings.x)          # + start poller di lifespan
    if settings.whatsapp.enabled:
        bridges[Platform.WHATSAPP] = WhatsAppBridge(settings.whatsapp)
    return bridges

# app/main.py
bridges = build_bridges(settings)
app.include_router(meta_router(bridges))          # demux IG+FB
app.include_router(whatsapp_webhook_router(bridges))
# lifespan: engine = Engine(bridges); queue worker = asyncio.create_task(...)
```

**Batasan waktu penting:** Meta mengharapkan 200 dalam ~20 detik. Semua kerja berat
(verifikasi, fetch caption, LLM) TIDAK boleh di request path — lihat §7.

---

## 7. Realtime & Worker Queue

**Pola:** webhook = cepat (parse → ack → enqueue → 200). Worker = lambat (verifikasi → kirim hasil).

```
webhook POST
  → meta router (verify sig, demux)
  → IG bridge.parse()                    (sync, murni)
  → engine.handle_event(event)
      ├─ dedup? echo?  → skip
      ├─ media baru    → simpan pending → bridge.send(ACCEPT)   ← langsung
      ├─ template      → bridge.send(MENTION)
      ├─ text+claim    → bridge.send("⏳ verifying...") → enqueue(Job)   ← langsung
      └─ text polos    → bridge.send(DENY)
  → 200 OK  (total < 200ms)
        │
        ▼  (asyncio.Queue, worker terpisah)
  worker loop
      → verifier.verify(claim)        # httpx ke LLM/search — 10–60 detik
      → bridge.send(OutgoingReply(text=hasil + URL))   ← hasil datang belakangan
```

```python
# app/core/queue.py — sengaja SIMPLE: asyncio.Queue, tanpa Redis/Celery (lihat §8)
import asyncio, logging
from dataclasses import dataclass
from .models import Claim

logger = logging.getLogger(__name__)

@dataclass
class Job:
    kind: str          # "verify_claim"
    claim: Claim

_queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=1000)

def enqueue(job: Job) -> None:
    _queue.put_nowait(job)                     # maxsize = backpressure alami

async def worker(engine) -> None:
    """Satu worker cukup utk MVP. Naikkan jadi N task kalau antrean menumpuk."""
    while True:
        job = await _queue.get()
        try:
            await engine.process_job(job)      # verifier + send hasil
        except Exception:
            logger.exception("job %s failed", job.kind)
        finally:
            _queue.task_done()
```

- Worker di-start di `lifespan` (`asyncio.create_task(worker(engine))`).
- **Retry:** cukup 1 retry dengan backoff 5s di `process_job`; kegagalan permanen di-log (MVP).
- **Dedup:** pindahkan `_processed_ids` ke `core/engine.py` (berlaku lintas platform).
  Opsi naik kelas nanti: Redis SETEX dengan TTL — tanpa mengubah core.
- **Kenapa bukan FastAPI BackgroundTasks?** BackgroundTasks jalan di proses request yang sama,
  tidak ada backpressure/retry, dan bisa ikut mati kalau koneksi klien putus. Queue task
  eksplisit lebih aman utk kerja 10–60 detik.

---

## 8. Config Per Platform (pydantic-settings)

Pola: **satu kelas Settings per platform dengan `env_prefix`**, di-agregat `AppSettings`.

```python
# app/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class MetaTransportSettings(BaseSettings):          # dipakai IG + FB
    model_config = SettingsConfigDict(env_prefix="META_", env_file=".env")
    app_id: str = ""
    app_secret: str = ""
    verify_token: str = ""
    page_id: str = ""
    page_access_token: str = ""
    skip_signature_check: bool = False

class InstagramSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IG_", env_file=".env")
    enabled: bool = False
    business_id: str = ""
    user_token: str = ""
    basic_token: str = ""
    bot_username: str = "factacheckfact"
    bot_username_alt: str = "factcheckfact"

class FacebookSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FB_", env_file=".env")
    enabled: bool = False
    bot_username: str = ""

class XSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="X_", env_file=".env")
    enabled: bool = False
    api_key: str = ""
    api_secret: str = ""
    bearer_token: str = ""
    poll_interval_sec: int = 20

class WhatsAppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WA_", env_file=".env")
    enabled: bool = False
    bridge_url: str = "http://localhost:3000"
    allowed_users: str = "*"

@dataclass
class AppSettings:
    meta: MetaTransportSettings = field(default_factory=MetaTransportSettings)
    instagram: InstagramSettings = field(default_factory=InstagramSettings)
    facebook: FacebookSettings = field(default_factory=FacebookSettings)
    x: XSettings = field(default_factory=XSettings)
    whatsapp: WhatsAppSettings = field(default_factory=WhatsAppSettings)

settings = AppSettings()
```

- Env var: `IG_ENABLED=true`, `IG_BUSINESS_ID=...`, `X_BEARER_TOKEN=...`, `WA_BRIDGE_URL=...`
  (existing `META_*`, `IG_BUSINESS_ID` tetap kompatibel).
- `enabled: bool` = saklar deploy: platform yang belum dipakai cukup dimatikan, kode tetap ada.
- `meta_app_secret_2` (multi-app) bisa jadi `list[str]` di MetaTransportSettings.

---

## 9. Level Abstraksi yang PAS (trade-off, deadline 16 Agt)

**Lakukan (nilai tinggi, biaya rendah):** port `PlatformBridge` 3 method, model kanonik,
folder per platform, engine murni, queue `asyncio`, settings per platform.
Itu semua sudah cukup untuk IG hari ini DAN siap FB/X/WA.

**JANGAN (YAGNI — over-engineering utk hackathon):**

| Ide abstraksi | Keputusan | Alasan |
|---|---|---|
| Port `Repository`/`UnitOfWork`/`MediaResolver` terpisah | ❌ skip | DB belum dipakai (state in-memory); tambah saat butuh |
| DI container (injector/`fastapi` Depends utk semua) | ❌ registry dict cukup | 1 argumen `bridges` di router/lifespan jauh lebih jelas |
| Event bus / outbox / CQRS | ❌ skip | 1 alur: webhook → queue → send |
| Validasi ketat payload per platform (pydantic penuh) | ⚠️ minimal | Payload Meta berubah-ubah; pola `.get()` defensif (seperti sekarang) lebih tahan banting. `schemas.py` opsional utk golden-test saja |
| Queue terabstraksi (port utk Redis/Celery/arq) | ❌ skip | `asyncio.Queue` 1 file; swap ke arq nanti = ganti 1 modul, core tak berubah |
| Multi-worker, retry/backoff canggih, rate-limit per platform | ⚠️ nanti | MVP: 1 worker + 1 retry |
| Parsing webhook X/WA sekarang | ❌ skip | Tulis folder saat platform itu dikerjakan — struktur sudah menyediakan slotnya |

**Jawaban utk pertanyaan "satu file besar vs banyak module":** untuk deadline, target
keseimbangan — *jangan pecah sampai 30 file kecil*, tapi *wajib pisahkan 3 hal*:
(1) core (murni), (2) adapter per platform, (3) transport webhook. Monolith 478 baris `meta.py`
tetap boleh ada **sebagai `platforms/instagram/bridge.py` yang dipindah, bukan ditulis ulang** —
logika `handle_message`/`handle_instagram_comment` pindah hampir apa adanya, hanya ditambah
`parse()` yang mengembalikan `IncomingEvent`.

**Roadmap migrasi 3 langkah (tanpa mengubah perilaku):**
1. **Langkah 1 (~½ hari):** buat `core/models.py` + `core/ports.py` + `core/router.py`
   (state machine dipindah dari `meta.py` — spesifikasinya sudah ada di `docs/webhook_state_machine.md`,
   skenario E2E A–F jadi unit test).
2. **Langkah 2 (~½ hari):** `platforms/meta/transport.py` (verifikasi + hub.challenge dipindah),
   `platforms/instagram/bridge.py` (handler dipindah, tambah `parse()`/`send()`).
3. **Langkah 3 (~½ hari):** `core/engine.py` + `core/queue.py` — webhook jadi parse→enqueue→200,
   worker yang kirim balasan. Hapus `app/webhooks/` setelah `meta.py` kosong.

**Paritas:** jalankan ulang payload E2E `docs/webhook_state_machine.md` (A–F) terhadap engine baru
sebelum lanjut ke platform lain.

---

## 10. Testing (tanpa jaringan)

```python
# tests/test_engine.py — replay skenario A–F dari docs/webhook_state_machine.md
class FakeBridge(PlatformBridge):
    platform = Platform.INSTAGRAM
    def __init__(self): self.sent: list[OutgoingReply] = []
    def parse(self, raw): ...              # ubah dict → IncomingEvent
    async def send(self, reply):
        self.sent.append(reply)
        return SendResult(ok=True)

async def test_media_then_claim_flow():
    engine = Engine({Platform.INSTAGRAM: FakeBridge()})
    await engine.handle_event(event_media())     # A: media → pending + ACCEPT
    await engine.handle_event(event_text_claim())  # C: text+pending → verifying + enqueue
    assert engine.bridges[Platform.INSTAGRAM].sent[-1].text.startswith("⏳")
```

- `test_ig_bridge.py`: payload Meta mentah (golden JSON) → `IncomingEvent` yang benar.
- Send API diuji dengan `httpx.MockTransport` — tidak pernah hit jaringan asli.

---

## 11. Checklist Menambah Platform Baru (contoh: X)

1. Buat `app/platforms/x/` → `bridge.py` (implement `parse` + `send`), `poller.py` (inbound).
2. Tambah `XSettings` di `app/config.py` (env `X_*`).
3. Daftarkan di `build_bridges()` + start poller di lifespan.
4. (Opsional) `test_x_bridge.py`.
5. **Core tidak berubah.** `AttachmentKind` ditambah hanya jika ada tipe baru (mis. `STICKER`).

```
Total perubahan utk platform baru: ±3 file baru + 3 baris config — 0 baris di core.
```

---

## 12. Hermes Brain Layer (Konsep — Post-Hackathon Enhancement)

> **Tujuan:** beri bot "soul" (persona), memory knowledge, dan kemampuan **orchestrator**
> yang bisa membagi kerja ke multiple sub-agent saat task berat. LAYER INI OPSIONAL —
> Phase 1 (MVP) pakai direct LLM (PARKEE_PROXY_URL) tanpa Hermes.

### 12.1 Posisi di arsitektur (Layer 3)

```
┌─ LAYER 1: BRIDGE (FastAPI bot) ──────────────┐
│  Webhook IG/FB/X/WA → routing → state → jobs │
└──────────────┬────────────────────────────────┘
               ▼
┌─ LAYER 2: WORKER (FastAPI worker) ───────────┐
│  claim job → ROUTER decision                 │
│  ├─ simple  → direct LLM (PARKEE_PROXY_URL)  │
│  └─ complex → panggil Hermes brain           │
└──────────────┬────────────────────────────────┘
               ▼
┌─ LAYER 3: BRAIN (Hermes profile "factbot") ──┐
│  Persona + memory + skills + RAG             │
│  Sub-agent delegation (research, transcribe) │
│  Output: verdict JSON + sources              │
└──────────────┬────────────────────────────────┘
               ▼
  Upload faktabot → URL → reply DM ✅
```

### 12.2 "Training" LLM — klarifikasi penting

LLM **tidak di-fine-tune** per project (butuh dataset + GPU + minggu — tidak feasible
untuk hackathon). Yang dilakukan = **prompt engineering + RAG**:

| Kebutuhan | Solusi | Biaya |
|---|---|---|
| Fine-tuning | ❌ Tidak feasible | GPU + dataset + minggu |
| System prompt / persona ("soul") | ✅ Persona FactBot di prompt | 1 hari |
| RAG (knowledge base) | ✅ Bot cek fakta dari database report + sumber | 2-3 hari |
| Few-shot examples | ✅ Contoh verdict yang benar | Jam |

### 12.3 Dua jenis memory — jangan dicampur

| Jenis | Isi | Tempat |
|---|---|---|
| **Memory BOT** (knowledge & prosedur) | cara cek fakta, kategori, SOP DM | Hermes memory + skills |
| **Memory PER-USER** (konteks percakapan) | pending_claims, riwayat DM user | **Postgres bot sendiri** (bukan Hermes) |

> Hermes memory = satu profile = satu operator. Bot melayani BANYAK user → konteks
> per-user WAJIB di DB bot. Hermes dipakai untuk "otak", bukan "percakapan tiap user".

### 12.4 Hermes profile khusus "factbot"

```bash
hermes profile create factbot   # ~/.hermes/profiles/factbot/ (isolasi penuh)
```

Profile = folder portabel (config.yaml, .env, skills/, memories/, state.db).
**Bisa di-deploy ke Docker Compose** — tinggal mount folder sebagai `$HERMES_HOME`:

```yaml
hermes-brain:
  build: ./hermes
  volumes:
    - ./hermes-profiles/factbot:/hermes-data   # profile di-mount
  environment:
    HERMES_HOME: /hermes-data
    WEBHOOK_ENABLED: "true"
    WEBHOOK_PORT: "8644"
    WEBHOOK_SECRET: "${HERMES_WEBHOOK_SECRET}"
  restart: unless-stopped
```

Bot FastAPI memanggil brain via salah satu:
1. **Hermes webhook** (POST ke `hermes-brain:8644`) — async job berat
2. **`hermes chat -q`** (subprocess) — sync one-shot
3. **`hermes proxy`** (OpenAI-compatible endpoint) — dipanggil seperti LLM biasa

### 12.5 Orchestrator: kapan pakai sub-agent (job dispatcher)

```
Task masuk → ROUTER (decision)
  ├─ SIMPLE  (caption jelas, no video)  → direct LLM  (~3s)  ✅ realtime
  ├─ MEDIUM  (perlu cari sumber)        → search + LLM paralel (~8s)
  └─ COMPLEX (video + transkrip + OCR)  → SPAWN SUB-AGENTS paralel (30-60s)
```

| | Direct LLM | Sub-agents |
|---|---|---|
| Latency | 2-5s ✅ | 30-60s ❌ |
| Akurasi | Cukup | Lebih dalam |
| Biaya | Murah | Mahal |
| Kapan | ~80% kasus | ~20% kasus berat |

**Realtime ≠ selalu sub-agent.** Realtime = router cerdas: cepat untuk yang simple,
dispenser untuk yang berat. Threshold routing = konfigurasi (bukan magic number).

### 12.6 Rekomendasi prioritas

| Langkah | Waktu | Prioritas |
|---|---|---|
| Phase 1: direct LLM (tanpa Hermes brain) | 5 hari | 🔴 Demo inti |
| Hermes profile `factbot` (persona/RAG) | 2-3 hari | 🟡 Paralel |
| Hermes jadi brain di pipeline kompleks | Post-hackathon | 🟢 Enhancer |
| Hermes di Docker Compose prod | Post-hackathon | 🟢 |

> **Alasan:** jangan taruh Hermes di jalur kritis Phase 1 — kalau Hermes-in-Docker
> bermasalah saat demo, seluruh bot mati. Direct LLM = jalur terpendek ke demo hidup.

---

## Lampiran: Peta Migrasi File Lama → Baru

| File lama | Isi yang pindah | Ke |
|---|---|---|
| `app/webhooks/meta.py` | verifikasi sig + hub.challenge | `app/platforms/meta/transport.py` |
| `app/webhooks/meta.py` | router POST/GET | `app/platforms/meta/router.py` |
| `app/webhooks/meta.py` | `handle_message`, `handle_instagram_comment`, `_reply_dm`, `_get_media_caption` | `app/platforms/instagram/bridge.py` |
| `app/webhooks/meta.py` | `pending_claims`, `_mark_processed`, `_is_mention`, template balasan | `app/core/router.py`, `app/core/engine.py`, `app/core/replies.py` |
| `app/api/reply.py` | `reply_to_fb_comment`, `reply_to_ig_comment`, `get_media_content` | `app/platforms/instagram/bridge.py`, `app/platforms/facebook/bridge.py` |
| `app/api/reply.py` | `format_verdict` | `app/core/replies.py` (dipakai verifier) |
| `app/config.py` | flat `Settings` | per-platform `*Settings` (env prefix) |
