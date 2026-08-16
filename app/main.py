"""FactBot — Fact-Check Bot Backend (FastAPI entry point)."""

import logging
import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.config import settings
from app.webhooks.meta import router as meta_webhook_router
from app.pipeline.hermes_callback import router as hermes_callback_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("klarifai")

_STARTED_TS = time.time()  # utk /health/detail (uptime)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("🚀 FactBot backend starting...")
    logger.info(f"📱 Meta App ID: {settings.meta_app_id}")
    logger.info(f"📱 IG Business ID: {settings.ig_business_id}")
    # Webhook-based (poller disabled — real-time via Meta webhooks)
    logger.info("🔔 Webhook mode: listening for Meta events")
    yield
    # Shutdown
    logger.info("👋 FactBot backend shutting down...")


app = FastAPI(
    title="FactBot Fact-Check Bot",
    description="Multi-platform fact-checking bot API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(meta_webhook_router)
app.include_router(hermes_callback_router)


@app.get("/")
async def root():
    return {
        "name": "FactBot",
        "version": "0.1.0",
        "status": "running",
        "platforms": ["facebook", "instagram"],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/detail")
async def health_detail():
    """Deep health: uptime, state in-memory, queue PG (kalau ada), worker heartbeat."""
    from app.webhooks.meta import pending_claims

    info = {
        "service": "FactBot",
        "version": "0.1.0",
        "uptime_s": round(time.time() - _STARTED_TS, 1),
        "pending_claims": len(pending_claims),
        "queue_depth": None,
        "worker_alive": None,
    }
    # Queue PG opsional — kalau DATABASE_URL masih sqlite, biarkan None
    if settings.database_url.startswith("postgresql"):
        try:
            import asyncpg

            dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
            conn = await asyncpg.connect(dsn, timeout=3)
            try:
                info["queue_depth"] = await conn.fetchval(
                    "SELECT count(*) FROM jobs WHERE status = 'queued'"
                )
                info["worker_alive"] = bool(
                    await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM workers "
                        "WHERE last_seen > now() - interval '90 seconds')"
                    )
                )
            finally:
                await conn.close()
        except Exception:
            pass
    return info


@app.get("/auth/instagram/callback")
async def instagram_oauth_callback(request: Request):
    """OAuth callback target for Instagram Business Login (redirect URL)."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    logger.info(f"🔐 IG OAuth callback: code={code[:20] if code else 'none'}..., state={state}")
    return HTMLResponse(
        "<h2>FactBot</h2><p>Login berhasil! Silakan kembali ke aplikasi.</p>"
    )


@app.get("/privacy")
async def privacy_policy():
    """Privacy policy page (required for app publish / App Review)."""
    return HTMLResponse(
        """
        <html><body style="font-family:sans-serif;max-width:720px;margin:40px auto;line-height:1.6">
        <h1>Kebijakan Privasi FactBot</h1>
        <p><em>Terakhir diperbarui: Agustus 2026</em></p>
        <h2>1. Data yang Kami Proses</h2>
        <p>FactBot adalah bot verifikasi fakta yang membalas komentar publik berisi @mention. Kami memproses data yang diperlukan untuk menjalankan layanan:</p>
        <ul>
          <li>Konten komentar publik yang men-tag akun kami (teks, username, media ID)</li>
          <li>Identitas publik akun yang berinteraksi (username, ID publik)</li>
          <li>Meta webhook payload yang dikirim oleh platform</li>
        </ul>
        <h2>2. Penggunaan Data</h2>
        <p>Data digunakan semata-mata untuk: (a) mendeteksi permintaan verifikasi fakta, (b) menghasilkan balasan otomatis di thread publik, dan (c) meningkatkan akurasi basis data verifikasi. Kami tidak menjual data pengguna.</p>
        <h2>3. Penyimpanan & Keamanan</h2>
        <p>Data disimpan di server kami dengan akses terbatas. Webhook payload diverifikasi tanda tangannya (HMAC-SHA256) untuk memastikan integritas.</p>
        <h2>4. Hak Pengguna</h2>
        <p>Anda dapat meminta penghapusan data dengan menghubungi avivsabilal29@gmail.com.</p>
        <h2>5. Kontak</h2>
        <p>Pertanyaan privasi: avivsabilal29@gmail.com</p>
        </body></html>
        """
    )


@app.get("/terms")
async def terms_of_service():
    """Terms of service page (required for app publish)."""
    return HTMLResponse(
        """
        <html><body style="font-family:sans-serif;max-width:720px;margin:40px auto;line-height:1.6">
        <h1>Syarat Layanan FactBot</h1>
        <p><em>Terakhir diperbarui: Agustus 2026</em></p>
        <h2>1. Layanan</h2>
        <p>FactBot menyediakan verifikasi fakta otomatis melalui balasan komentar publik di platform media sosial.</p>
        <h2>2. Keterbatasan</h2>
        <p>Hasil verifikasi bersifat informatif dan tidak menggantikan keputusan profesional (medis, hukum, finansial). Verdict "Belum Dapat Diverifikasi" berarti sumber belum cukup.</p>
        <h2>3. Penggunaan yang Dilarang</h2>
        <p>Dilarang menyalahgunakan bot untuk spam, pelecehan, atau penyebaran informasi palsu yang disengaja.</p>
        <h2>4. Perubahan</h2>
        <p>Kami dapat memperbarui syarat ini sewaktu-waktu dan akan mengumumkan perubahan di halaman ini.</p>
        <h2>5. Kontak</h2>
        <p>avivsabilal29@gmail.com</p>
        </body></html>
        """
    )


@app.post("/test/reply")
async def test_reply(data: dict):
    """
    Test endpoint to manually trigger a reply.
    Usage: POST /test/reply with {"platform": "facebook|instagram", "comment_id": "...", "message": "..."}
    """
    from app.api.reply import reply_to_fb_comment, reply_to_ig_comment
    
    platform = data.get("platform", "facebook")
    comment_id = data.get("comment_id")
    message = data.get("message", "✅ Test reply from FactBot 🤖")
    
    token = settings.meta_page_access_token
    if not token:
        return {"error": "META_PAGE_ACCESS_TOKEN not configured"}
    
    if platform == "facebook":
        result = await reply_to_fb_comment(comment_id, message, token)
    elif platform == "instagram":
        result = await reply_to_ig_comment(comment_id, message, token)
    else:
        return {"error": f"Unknown platform: {platform}"}
    
    return {"platform": platform, "reply_id": result, "success": result is not None}
