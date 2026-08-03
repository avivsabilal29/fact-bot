"""Reply helper functions for Meta API (Instagram + Facebook comments)."""

import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


BASE_FB = "https://graph.facebook.com/v25.0"
BASE_IG = "https://graph.instagram.com/v25.0"

# Pesan hasil verifikasi ke DM (dipakai worker — app/worker.py)
RESULT_DM_TEMPLATE = "✅ Verification complete! Here's your result: {public_url} 🔍"


async def send_result_dm(sender_id: str, public_url: str) -> bool:
    """Kirim hasil verifikasi (public_url) ke DM Instagram user.

    Sama pola _reply_dm di app/webhooks/meta.py: POST
    graph.instagram.com/v26.0/{ig_business_id}/messages.
    Return True bila terkirim; gagal/konfigurasi kosong → False (di-log).
    """
    ig_id = settings.ig_business_id
    token = settings.ig_user_token or settings.ig_basic_token
    if not ig_id or not token:
        logger.warning(
            "send_result_dm: ig_business_id atau IG token belum dikonfigurasi"
        )
        return False
    message = RESULT_DM_TEMPLATE.format(public_url=public_url)
    url = f"https://graph.instagram.com/v26.0/{ig_id}/messages"
    payload = {
        "recipient": {"id": sender_id},
        "message": {"text": message},
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, params={"access_token": token}, json=payload)
        if resp.is_error:
            logger.error(f"send_result_dm failed ({resp.status_code}): {resp.text[:200]}")
            return False
        logger.info(f"✅ Result DM sent to {sender_id}")
        return True
    except Exception as e:
        logger.error(f"send_result_dm exception: {e}")
        return False


async def send_progress_dm(sender_id: str, text: str) -> bool:
    """Kirim pesan progress (teks bebas) ke DM Instagram user — best effort.

    Endpoint & token sama dgn send_result_dm: POST
    graph.instagram.com/v26.0/{ig_business_id}/messages dengan
    IG_USER_TOKEN (fallback ig_basic_token). Dipakai worker
    (app/worker.py → ProgressNotifier) utk update bertahap ke user.

    Gagal → log warning + return False — progress GAGAL tidak boleh
    crash pipeline worker (graceful degradation).
    """
    ig_id = settings.ig_business_id
    token = settings.ig_user_token or settings.ig_basic_token
    if not ig_id or not token:
        logger.warning(
            "send_progress_dm: ig_business_id atau IG token belum dikonfigurasi"
        )
        return False
    url = f"https://graph.instagram.com/v26.0/{ig_id}/messages"
    payload = {
        "recipient": {"id": sender_id},
        "message": {"text": text},
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, params={"access_token": token}, json=payload)
        if resp.is_error:
            logger.warning(f"send_progress_dm failed ({resp.status_code}): {resp.text[:200]}")
            return False
        logger.info(f"Progress DM sent to {sender_id}")
        return True
    except Exception as e:
        logger.warning(f"send_progress_dm exception: {e}")
        return False


async def reply_to_fb_comment(
    comment_id: str, 
    message: str, 
    page_access_token: str
) -> Optional[str]:
    """
    Reply to a Facebook comment publicly in-thread.
    POST /{comment-id}/comments
    Returns the reply comment ID if successful.
    """
    url = f"{BASE_FB}/{comment_id}/comments"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data={
            "message": message,
            "access_token": page_access_token,
        })
        data = resp.json()
        if resp.is_error:
            logger.error(f"FB reply failed: {data}")
            return None
        return data.get("id")


async def reply_to_ig_comment(
    comment_id: str,
    message: str,
    page_access_token: str
) -> Optional[str]:
    """
    Reply to an Instagram comment publicly in-thread.
    POST /{ig-comment-id}/replies
    Uses Facebook Graph API (not Instagram API) since IG is linked to FB Page.
    """
    url = f"{BASE_FB}/{comment_id}/replies"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data={
            "message": message,
            "access_token": page_access_token,
        })
        data = resp.json()
        if resp.is_error:
            logger.error(f"IG reply failed: {data}")
            return None
        return data.get("id")


async def get_media_content(
    media_id: str,
    page_access_token: str
) -> Optional[dict]:
    """
    Get Instagram media content (caption, media_type, media_url).
    GET /{media-id}?fields=caption,media_type,media_url
    """
    url = f"{BASE_FB}/{media_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={
            "fields": "caption,media_type,media_url,permalink",
            "access_token": page_access_token,
        })
        if resp.is_error:
            logger.error(f"Failed to get media: {resp.json()}")
            return None
        return resp.json()


async def get_fb_post_content(
    post_id: str,
    page_access_token: str
) -> Optional[dict]:
    """
    Get Facebook post content.
    GET /{post-id}?fields=message,permalink_url
    """
    url = f"{BASE_FB}/{post_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={
            "fields": "message,permalink_url,created_time",
            "access_token": page_access_token,
        })
        if resp.is_error:
            logger.error(f"Failed to get post: {resp.json()}")
            return None
        return resp.json()


async def get_mentioned_comment(
    ig_user_id: str,
    page_access_token: str
) -> Optional[list]:
    """
    Get recent @mentions for an Instagram Business account.
    GET /{ig-user-id}?fields=mentioned_comment
    """
    url = f"{BASE_FB}/{ig_user_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={
            "fields": "mentioned_comment.comment_id,mentioned_comment.text,mentioned_comment.media",
            "access_token": page_access_token,
        })
        if resp.is_error:
            logger.error(f"Failed to get mentions: {resp.json()}")
            return None
        return resp.json()


async def format_verdict(
    verdict: str,
    label: str,
    confidence: float,
    explanation: str,
    sources: list[str]
) -> str:
    """
    Format a fact-check verdict into a reply message.
    """
    emoji_map = {
        "HOAX": "❌",
        "FAKTA": "✅",
        "MENYESATKAN": "⚠️",
        "BELUM_DAPAT_DIVERIFIKASI": "❔",
    }
    emoji = emoji_map.get(verdict, "❔")
    
    msg = f"{emoji} **{label}**\n\n{explanation}\n\n📚 **Sumber:**\n"
    for i, src in enumerate(sources, 1):
        msg += f"{i}. {src}\n"
    
    msg += "\n💡 *FactBot — Tag. Verifikasi. Percaya Lagi.*"
    
    return msg
