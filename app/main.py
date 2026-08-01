"""KlarifAI — Fact-Check Bot Backend (FastAPI entry point)."""

import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.webhooks.meta import router as meta_webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("klarifai")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("🚀 KlarifAI backend starting...")
    logger.info(f"📱 Meta App ID: {settings.meta_app_id}")
    logger.info(f"📱 IG Business ID: {settings.ig_business_id}")
    # Webhook-based (poller disabled — real-time via Meta webhooks)
    logger.info("🔔 Webhook mode: listening for Meta events")
    yield
    # Shutdown
    logger.info("👋 KlarifAI backend shutting down...")


app = FastAPI(
    title="KlarifAI Fact-Check Bot",
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


@app.get("/")
async def root():
    return {
        "name": "KlarifAI",
        "version": "0.1.0",
        "status": "running",
        "platforms": ["facebook", "instagram"],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/test/reply")
async def test_reply(data: dict):
    """
    Test endpoint to manually trigger a reply.
    Usage: POST /test/reply with {"platform": "facebook|instagram", "comment_id": "...", "message": "..."}
    """
    from app.api.reply import reply_to_fb_comment, reply_to_ig_comment
    
    platform = data.get("platform", "facebook")
    comment_id = data.get("comment_id")
    message = data.get("message", "✅ Test reply from KlarifAI bot 🤖")
    
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
