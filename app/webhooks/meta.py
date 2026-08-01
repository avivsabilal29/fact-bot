"""Meta webhook handlers for Instagram & Facebook mentions - with auto-reply."""

import hashlib
import hmac
import json
import logging

import httpx
from fastapi import APIRouter, Request, HTTPException
from starlette.responses import PlainTextResponse

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/meta", tags=["meta"])

# Dedup: recently processed comment IDs (in-memory)
_processed_ids = set()
_MAX_PROCESSED = 500


def _mark_processed(comment_id: str) -> bool:
    """Return True if already processed, False otherwise. Marks on first sight."""
    if comment_id in _processed_ids:
        return True
    _processed_ids.add(comment_id)
    if len(_processed_ids) > _MAX_PROCESSED:
        _processed_ids.clear()
    return False

# Bot's own username
BOT_USERNAME = "factacheckfact"
BOT_USERNAME_ALT = "factcheckfact"


def _verify_signature(payload: bytes, signature_header: str, app_secret: str) -> bool:
    """Verify X-Hub-Signature-256 header matches payload signed with app secret."""
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _verify_signature_multi(payload: bytes, signature_header: str, *secrets: str) -> bool:
    """Try multiple app secrets (multi-app webhook endpoint)."""
    for secret in secrets:
        if secret and _verify_signature(payload, signature_header, secret):
            return True
    return False


def _is_mention(text: str) -> bool:
    """Check if comment contains @mention of our bot."""
    text_lower = text.lower()
    return (
        f"@{BOT_USERNAME}" in text_lower
        or f"@{BOT_USERNAME_ALT}" in text_lower
    )


def _strip_mention(text: str) -> str:
    """Remove @mention prefix from text, return the actual claim."""
    text_lower = text.lower()
    for mention in [f"@{BOT_USERNAME}", f"@{BOT_USERNAME_ALT}"]:
        if mention in text_lower:
            # Remove the mention and clean up
            text = text.replace(mention, "", 1)
    return text.strip().strip(" ,:;")


async def _get_media_caption(media_id: str, token: str) -> str | None:
    """Get caption/content of an Instagram post. Returns None if media inaccessible."""
    url = f"https://graph.facebook.com/v25.0/{media_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={
            "fields": "caption,media_type,permalink",
            "access_token": token,
        })
        if resp.is_error:
            logger.warning(f"Failed to get media {media_id}: {resp.text[:200]}")
            return None
        data = resp.json()
        return data.get("caption", "") or ""


async def _reply_to_comment(comment_id: str, message: str, token: str) -> bool:
    """Reply to an Instagram comment using Facebook Graph API."""
    url = f"https://graph.facebook.com/v25.0/{comment_id}/replies"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data={
            "message": message,
            "access_token": token,
        })
        if resp.is_error:
            logger.error(f"Reply failed for {comment_id}: {resp.text[:200]}")
            return False
        reply_id = resp.json().get("id", "?")
        logger.info(f"✅ Reply sent! Reply ID: {reply_id}")
        return True


async def _process_mention(
    comment_id: str,
    media_id: str,
    text: str,
    from_user: str,
    token: str,
):
    """Process a mention: read media, send reply."""
    logger.info(f"🔔 Mention from @{from_user}: \"{text[:100]}\"")

    # Get post caption/content — if fetch fails, skip reply (invalid media id)
    caption = await _get_media_caption(media_id, token)
    if caption is None:
        logger.warning(f"Skipping reply: media {media_id} not accessible")
        return False
    claim = _strip_mention(text)

    # Build reply
    if claim:
        reply_msg = (
            f"🤖 Hai @{from_user}! KlarifAI menerima klaim: "
            f"\"{claim[:150]}\"\n\n"
            f"⏳ Verifikasi sedang diproses... "
            f"Mohon tunggu sebentar.\n\n"
            f"📌 Tag: {text}"
        )
    else:
        reply_msg = (
            f"🤖 Hai @{from_user}! Terima kasih sudah men-tag KlarifAI.\n\n"
            f"Silakan sertakan klaim yang ingin dicek, contoh:\n"
            f"\"@factacheckfact beneran vaksin gratis ini hoax?\"\n\n"
            f"💡 Tag. Verifikasi. Percaya Lagi."
        )

    # Send reply
    success = await _reply_to_comment(comment_id, reply_msg, token)
    return bool(success)


@router.get("")
async def verify_webhook(request: Request):
    """Meta sends a GET request to verify the webhook endpoint."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    logger.info(f"Webhook verify: mode={mode}, token={token}")

    if mode == "subscribe" and token == settings.meta_verify_token:
        logger.info("✅ Webhook verified!")
        return PlainTextResponse(challenge)
    else:
        logger.warning(f"Webhook verify failed: token mismatch")
        raise HTTPException(status_code=403, detail="Verify token mismatch")


@router.post("")
async def handle_webhook(request: Request):
    """Receive real-time webhook events from Meta."""
    # Verify signature — try all configured app secrets (multi-app setup)
    sig_header = request.headers.get("X-Hub-Signature-256", "")
    if sig_header:
        body = await request.body()
        secrets = [
            settings.meta_app_secret,
            getattr(settings, "meta_app_secret_2", ""),
        ]
        if getattr(settings, "meta_skip_signature", False):
            logger.info("⚠️ Signature check SKIPPED (META_SKIP_SIGNATURE_CHECK=true) — accepting payload")
        elif not _verify_signature_multi(body, sig_header, *secrets):
            logger.warning("Invalid webhook signature (tried %d secret(s))", len([s for s in secrets if s]))
            raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    logger.info(f"📦 RAW PAYLOAD: {json.dumps(payload, ensure_ascii=False)[:2000]}")
    logger.debug(f"Webhook payload keys: {list(payload.keys())}")

    entries = payload.get("entry", [])

    for entry in entries:
        changes = entry.get("changes", [])
        for change in changes:
            field = change.get("field", "")
            value = change.get("value", {})
            logger.info(f"⚡ Webhook event: field={field}")
            logger.info(f"  📄 value: {json.dumps(value, ensure_ascii=False)[:800]}")

            try:
                if field == "comments":
                    await handle_instagram_comment(value)
                elif field == "mentions":
                    await handle_mention(value)
                elif field == "feed":
                    await handle_feed_event(value)
                elif field == "messages":
                    await handle_message(value)
                else:
                    logger.info(f"  ⏭️ Field '{field}' not handled (no-op)")
            except Exception as e:
                import traceback
                logger.error(f"Error handling {field}: {e}\n{traceback.format_exc()}")

    return {"status": "received"}


async def handle_instagram_comment(value: dict):
    """
    Handle new Instagram comment (on bot's own posts).
    If the comment @mentions the bot, auto-reply.
    """
    comment_id = value.get("id")
    media_id = value.get("media_id")
    text = value.get("text", "")
    from_user = (value.get("from") or {}).get("username", "unknown")

    logger.info(f"💬 IG comment from @{from_user}: \"{text[:80]}\"")

    # Skip if comment from bot itself (infinite loop prevention)
    if from_user == BOT_USERNAME:
        logger.info("Skipping bot's own comment")
        return

    # Only auto-reply if the bot is @mentioned
    if not _is_mention(text):
        logger.info("Not a mention of this bot, skipping")
        return

    # Dedup
    if comment_id and _mark_processed(comment_id):
        logger.info(f"Already processed comment {comment_id}, skipping")
        return

    # Get the token
    token = settings.meta_page_access_token
    if not token:
        logger.warning("No access token configured, can't reply")
        return

    logger.info(f"🎯 Detected @mention by @{from_user}, processing...")
    await _process_mention(comment_id, media_id, text, from_user, token)


async def handle_mention(value: dict):
    """
    Handle @mention events from Instagram (when tagged on OTHER people's posts).
    Robust extraction: Meta's mentions payload structure varies.
    """
    import json as _json
    # Log the FULL raw value so we can see the actual structure
    logger.info(f"📦 Raw mention value: {_json.dumps(value, ensure_ascii=False)[:500]}")

    # Extract with multiple fallback key names
    comment_id = (
        value.get("comment_id")
        or value.get("commentId")
        or value.get("id")
    )
    media_id = (
        value.get("media_id")
        or value.get("mediaId")
        or (value.get("media") or {}).get("id")
        or (value.get("target") or {}).get("media_id")
    )
    text = (
        value.get("text")
        or value.get("comment_text")
        or (value.get("comment") or {}).get("text")
        or ""
    )
    from_raw = value.get("from") or value.get("user") or {}
    from_user = (
        from_raw.get("username") if isinstance(from_raw, dict) else str(from_raw)
    ) or "unknown"

    logger.info(f"🔔 Mention from @{from_user}: \"{text[:100]}\" (comment={comment_id}, media={media_id})")

    if not comment_id or not media_id:
        logger.warning(f"Incomplete mention data: comment_id={comment_id}, media_id={media_id}")
        return

    # Dedup
    if _mark_processed(comment_id):
        logger.info(f"Already processed mention {comment_id}, skipping")
        return

    token = settings.meta_page_access_token
    if not token:
        logger.warning("No access token configured")
        return

    await _process_mention(comment_id, media_id, text, from_user, token)


async def handle_feed_event(value: dict):
    """Handle Facebook feed events."""
    item = value.get("item", "")
    verb = value.get("verb", "")
    post_id = value.get("post_id")
    comment_id = value.get("comment_id")
    message = value.get("message", "")

    logger.info(f"FB event: {item}/{verb}")

    # If it's a comment on a post and mentions the bot, reply
    if comment_id and message and _is_mention(message):
        logger.info(f"🎯 FB mention detected, auto-reply not yet implemented for FB")
        # TODO: Implement Facebook reply


async def _reply_dm(recipient_id: str, text: str) -> bool:
    """Send a DM reply via Instagram Messaging API (POST /{ig-id}/messages)."""
    ig_id = settings.ig_business_id
    token = settings.ig_user_token or settings.ig_basic_token
    if not ig_id or not token:
        logger.warning("No ig_business_id or IG token configured for DM reply")
        return False
    url = f"https://graph.instagram.com/v26.0/{ig_id}/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, params={"access_token": token}, json=payload)
        if resp.is_error:
            logger.error(f"DM reply failed ({resp.status_code}): {resp.text[:200]}")
            return False
        logger.info(f"✅ DM reply sent to {recipient_id}")
        return True
    except Exception as e:
        logger.error(f"DM reply exception: {e}")
        return False


async def handle_message(value: dict):
    """Handle Instagram DM / Messenger messages — auto-reply intro to every DM."""
    msg_raw = value.get("message", "")
    mid = None
    if isinstance(msg_raw, dict):
        mid = msg_raw.get("mid")
        msg_text = msg_raw.get("text", "")
    else:
        mid = value.get("mid")
        msg_text = str(msg_raw) if msg_raw else ""
    from_id = value.get("sender", {}).get("id", "unknown")
    logger.info(f"💬 DM received from {from_id} (mid={mid}): \"{msg_text[:80]}\"")

    # Skip if sender is the bot itself (echo loop prevention)
    if from_id == settings.ig_business_id:
        logger.info("⏭️ DM from bot itself (echo) — skipping")
        return

    # Dedup by mid so a duplicated webhook doesn't double-reply
    if mid and _mark_processed(mid):
        logger.info(f"⏭️ Already replied to DM {mid} — skipping")
        return

    # Auto-reply intro (English) to every incoming DM
    intro = (
        "Hello! 👋 I'm FactBot, KlarifAI's fact-checking assistant.\n\n"
        "I'm here to help you analyze facts and news! 🔍\n"
        "Currently in development phase — more features coming soon. 🚀"
    )
    await _reply_dm(from_id, intro)

    # Scrape the full DM content via API (IG user token) for logging
    ig_token = settings.ig_user_token or settings.ig_basic_token
    if not mid or not ig_token:
        logger.info("No mid or IG token — skipping DM scrape")
        return

    try:
        url = f"https://graph.instagram.com/v26.0/{mid}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params={
                "fields": "id,created_time,message,from,to,attachments",
                "access_token": ig_token,
            })
        if resp.is_error:
            logger.warning(f"Failed to fetch DM {mid}: {resp.text[:200]}")
            return
        detail = resp.json()

        dm_from = (detail.get("from") or {}).get("username", from_id)
        dm_text = detail.get("message", "") or ""
        attachments = (detail.get("attachments") or {}).get("data", []) or []
        logger.info(f"📄 DM detail: from=@{dm_from} | text=\"{dm_text[:200]}\" | attachments={len(attachments)}")

        # Inspect attachments (template/share)
        for att in attachments:
            if "generic_template" in att:
                title = att["generic_template"].get("title", "")
                logger.info(f"🖼️ Attachment template: \"{title}\"")
                if title and "aplikasi terbaru" in title.lower():
                    logger.info("⏭️ System template (update app) — skipping")
            elif "share" in att:
                share = att.get("share", {})
                logger.info(f"🔗 Shared content: {share.get('link') or share.get('url', '')}")
            else:
                logger.info(f"📎 Attachment: {json.dumps(att, ensure_ascii=False)[:300]}")

        # If DM mentions the bot, treat as verification request
        if dm_text and _is_mention(dm_text):
            logger.info(f"🎯 DM @mention from @{dm_from}: \"{dm_text[:100]}\"")
            # TODO: reply in DM thread via POST /{ig-id}/messages
    except Exception as e:
        logger.error(f"DM scrape error: {e}")
