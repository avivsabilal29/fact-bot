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

# DM reply templates (FactBot branding)
MENTION_REPLY = "Sorry, this feature is not supported yet — we're still in development phase. 🙏"
ACCEPT_REPLY = "Thanks for sharing! 🙏 I've received your reel/post. Now please reply with the claim you want me to verify. ✍️"
DENY_REPLY = "Sorry, currently I can only analyze reels or posts. Please send me a reel or post to verify. 🎬"
CLAIM_RECEIVED_REPLY = "Got it! ✅ I'm verifying your claim now — this may take a moment. 🔍"

# Media attachment types that trigger the accept flow
ACCEPTED_MEDIA_TYPES = {"ig_reel", "reel", "ig_post", "post", "image", "video"}

# One pending claim slot per sender (event-driven, NO time window). New media overwrites old.
# _platform disimpan agar claim job dibuat via bridge platform yang benar (FB/IG).
pending_claims = {}  # sender_id -> {"url": str, "title": str, "media_type": str, "_platform": str}


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
            f"🤖 Hai @{from_user}! FactBot menerima klaim: "
            f"\"{claim[:150]}\"\n\n"
            f"⏳ Verifikasi sedang diproses... "
            f"Mohon tunggu sebentar.\n\n"
            f"📌 Tag: {text}"
        )
    else:
        reply_msg = (
            f"🤖 Hai @{from_user}! Terima kasih sudah men-tag FactBot.\n\n"
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

    # Platform detection (broker): object=page → Facebook Messenger, else Instagram.
    # Ditandai sekali di sini (msg["_platform"]) → dipakai handle_message / _create_claim_job.
    platform = "facebook" if payload.get("object") == "page" else "instagram"

    for entry in entries:
        # Format 1: changes[] — comments, mentions, feed (Instagram Graph webhook)
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
                elif field == "mention":
                    # FB mention (komentar di postingan) — balas MENTION_REPLY via DM
                    await handle_fb_mention(value, platform)
                elif field == "messages":
                    await handle_message(value)
                else:
                    logger.info(f"  ⏭️ Field '{field}' not handled (no-op)")
            except Exception as e:
                import traceback
                logger.error(f"Error handling {field}: {e}\n{traceback.format_exc()}")

        # Format 2: messaging[] — DM (Messaging webhook) — tandai platform tiap msg
        messaging = entry.get("messaging", [])
        for msg in messaging:
            msg["_platform"] = platform
            try:
                await handle_message(msg)
            except Exception as e:
                import traceback
                logger.error(f"Error handling DM: {e}\n{traceback.format_exc()}")

    return {"status": "received"}


async def _fetch_fb_commenter(comment_id: str) -> str | None:
    """Fetch commenter PSID dari komentar FB via Graph API — best-effort.

    GET graph.facebook.com/v26.0/{comment_id}?fields=from{id,name},message + Page token.
    Return from.id (PSID) atau None kalau gagal (jangan crash).
    """
    token = settings.meta_page_access_token
    if not comment_id or not token:
        logger.info("No comment_id or Page token — skipping FB commenter fetch")
        return None
    try:
        url = f"https://graph.facebook.com/v26.0/{comment_id}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params={
                "fields": "from{id,name},message",
                "access_token": token,
            })
        if resp.is_error:
            logger.warning(f"Failed to fetch FB commenter {comment_id}: {resp.text[:200]}")
            return None
        data = resp.json()
        from_raw = data.get("from") or {}
        return (from_raw.get("id") or "") or None
    except Exception as e:
        logger.warning(f"FB commenter fetch exception: {e}")
        return None


async def handle_fb_mention(value: dict, platform: str = "facebook") -> None:
    """Handle Facebook mention (field=mention) — komentar di postingan.

    Payload TIDAK berisi sender id, cuma comment_id. Alur (behavior disamain dgn IG):
    GET /{comment_id}?fields=from → PSID commenter → _reply_dm(from_id, MENTION_REPLY,
    'facebook'). Best-effort: semua kegagalan = log warning, jangan crash.
    """
    comment_id = value.get("comment_id", "") or ""
    logger.info(f"🔔 FB mention event (comment_id={comment_id})")

    if not comment_id or platform != "facebook":
        logger.info("  ⏭️ Mention tanpa comment_id / bukan FB — no-op")
        return

    # Dedup by comment_id agar webhook duplikat tidak double-reply
    if _mark_processed(comment_id):
        logger.info(f"Already processed FB mention {comment_id}, skipping")
        return

    from_id = await _fetch_fb_commenter(comment_id)
    if not from_id:
        logger.warning(f"  ⏭️ Tidak dapat PSID commenter utk {comment_id} — skip reply")
        return
    await _reply_dm(from_id, MENTION_REPLY, "facebook")


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


async def _reply_dm(recipient_id: str, text: str, platform: str = "instagram") -> bool:
    """Send a DM reply via the platform's Messaging API.

    instagram → POST graph.instagram.com/v26.0/{ig_business_id}/messages (IG token)
    facebook  → POST graph.facebook.com/v26.0/{meta_page_id}/messages (Page token)
    Body sama untuk kedua platform: {"recipient": {"id": ...}, "message": {"text": ...}}
    """
    if platform == "facebook":
        page_id = settings.meta_page_id
        token = settings.meta_page_access_token
        if not page_id or not token:
            logger.warning("No meta_page_id or Page token configured for FB DM reply")
            return False
        url = f"https://graph.facebook.com/v26.0/{page_id}/messages"
    else:
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
        logger.info(f"✅ DM reply sent to {recipient_id} via {platform}")
        return True
    except Exception as e:
        logger.error(f"DM reply exception: {e}")
        return False


async def _log_dm_detail(mid: str, from_id: str, platform: str = "instagram") -> None:
    """Fetch full DM detail via platform API for LOGGING only — never sends replies.

    instagram → graph.instagram.com/v26.0/{mid} (IG token) — existing behavior.
    facebook  → graph.facebook.com/v26.0/{mid}?fields=... (Page token).
                JANGAN fetch mid FB (m_...) ke graph.instagram.com (selalu 400);
                kalau fetch FB gagal → skip (log info), jangan crash.
    """
    if platform == "facebook":
        token = settings.meta_page_access_token
        if not mid or not token:
            logger.info("No mid or Page token — skipping FB DM scrape")
            return
        try:
            url = f"https://graph.facebook.com/v26.0/{mid}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params={
                    "fields": "id,created_time,message,from,to,attachments",
                    "access_token": token,
                })
            if resp.is_error:
                logger.info(f"⏭️ FB DM scrape skipped ({resp.status_code}): {resp.text[:200]}")
                return
            detail = resp.json()
            dm_from = (detail.get("from") or {}).get("id", from_id)
            dm_text = detail.get("message", "") or ""
            logger.info(f"📄 FB DM detail: from={dm_from} | text=\"{dm_text[:200]}\"")
        except Exception as e:
            logger.info(f"⏭️ FB DM scrape skipped (exception: {e})")
        return

    # --- Instagram path (existing behavior) ---
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
    except Exception as e:
        logger.error(f"DM scrape error: {e}")


async def _create_claim_job(media: dict, claim_text: str, sender_id: str) -> str | None:
    """Buat job queue utk klaim via job store (SQLite lokal / Postgres VPS).

    Phase 1 MVP: caption-only, TANPA download video — media_url/title dari
    pending DM attachment, media_id belum tersedia di payload DM (kosong).
    Graceful degradation (prinsip SOUL): kalau job store gagal (DB error dsb),
    log error + return None — webhook TETAP membalas CLAIM_RECEIVED_REPLY.
    """
    try:
        from app.jobs import build_report_id, get_job_store  # lazy: hindari circular import

        store = get_job_store(settings)
        # Platform asal DM (dari broker handle_webhook): media bawa "_platform"
        platform = media.get("_platform", "instagram")
        media_url = media.get("url", "") or ""
        media_id = media.get("media_id", "") or ""
        reel_video_id = media.get("reel_video_id", "") or ""
        ig_post_media_id = media.get("ig_post_media_id", "") or ""

        # Normalisasi source URL → URL PUBLIK, bukan CDN internal Meta
        # (lookaside.fbsbx.com signed URL expired & butuh auth → gak bisa dibuka user lain).
        # Prioritas: reel_video_id → ig_post_media_id → URL mentah (kalau sudah publik).
        # HANYA utk Instagram — URL FB (facebook.com/reel|posts/pfbid...) sudah publik.
        if not media_id:
            media_id = reel_video_id or ig_post_media_id or ""
        if platform == "instagram" and ("lookaside.fbsbx.com" in media_url or "ig_messaging_cdn" in media_url):
            if reel_video_id:
                media_url = f"https://www.instagram.com/reel/{reel_video_id}/"
            elif ig_post_media_id:
                media_url = f"https://www.instagram.com/p/{ig_post_media_id}/"
            elif media_id:
                # fallback: media_id dari payload — coba sebagai reel (paling umum untuk konten viral)
                media_url = f"https://www.instagram.com/reel/{media_id}/"

        job = {
            "report_id": build_report_id(platform, media_id, media_url),
            "platform": platform,
            "media_url": media_url,
            "media_title": media.get("title", "") or "",
            "media_id": media_id,
            "claim_text": claim_text,
            "sender_id": sender_id,
        }
        job_id = await store.create_job(job)
        logger.info(f'📦 Job {job["report_id"]} created (status=queued, media_url={media_url})')
        return job_id
    except Exception:  # noqa: BLE001 — graceful degradation, jangan crash webhook
        logger.exception(f"⚠️ Gagal membuat job utk {sender_id} — reply tetap dikirim")
        return None


async def handle_message(value: dict):
    """Handle Instagram DM / Messenger messages — route by content:
    template (mention pattern) → MENTION_REPLY, media reel/post → ACCEPT_REPLY
    + pending claim state, text with pending claim → CLAIM_RECEIVED_REPLY,
    text without pending → DENY_REPLY, payload without text/media → silent skip.
    Platform (dari broker handle_webhook) diteruskan ke semua reply DM."""
    msg_raw = value.get("message", "")
    # Platform asal: ditandai broker di handle_webhook (msg["_platform"]).
    platform = value.get("_platform", "instagram")

    # Skip non-message events (read receipts, deliveries, seen, etc.) — no "message" key
    if not msg_raw:
        logger.info("⏭️ Non-message event (read/delivery/seen) — skipping")
        return

    mid = None
    msg_text = ""
    attachments = []
    if isinstance(msg_raw, dict):
        mid = msg_raw.get("mid")
        msg_text = msg_raw.get("text", "") or ""
        attachments = msg_raw.get("attachments", []) or []
    else:
        mid = value.get("mid")
        msg_text = str(msg_raw) if msg_raw else ""
    from_id = value.get("sender", {}).get("id", "unknown")
    logger.info(f"💬 DM received from {from_id} (mid={mid}, platform={platform}): \"{msg_text[:80]}\"")

    # Skip if sender is the bot itself (echo loop prevention) — platform-aware:
    # FB echo = sender == Page ID, IG echo = sender == IG business ID
    bot_self = settings.meta_page_id if platform == "facebook" else settings.ig_business_id
    if from_id == bot_self:
        logger.info("⏭️ DM from bot itself (echo) — skipping")
        return

    # Dedup by mid so a duplicated webhook doesn't double-reply
    if mid and _mark_processed(mid):
        logger.info(f"⏭️ Already replied to DM {mid} — skipping")
        return

    # Log full DM detail via API (logging only — never replies)
    await _log_dm_detail(mid, from_id, platform)

    # --- Routing ---
    media_types = {a.get("type") for a in attachments if isinstance(a, dict) and a.get("type")}

    # Template = mention pattern (all template attachments treated as mention)
    if "template" in media_types:
        logger.info("🎯 Template DM (mention pattern)")
        await _reply_dm(from_id, MENTION_REPLY, platform)
        return

    # Media reel/post → save as pending claim, ask for clarification
    if media_types & ACCEPTED_MEDIA_TYPES:
        url = ""
        title = ""
        media_type = ""
        media_id = ""
        reel_video_id = ""
        ig_post_media_id = ""
        for a in attachments:
            if isinstance(a, dict) and a.get("type") in ACCEPTED_MEDIA_TYPES:
                payload = a.get("payload", {})
                if not isinstance(payload, dict):
                    payload = {}
                url = payload.get("url", "") or ""
                title = payload.get("title", "") or ""
                media_type = a.get("type", "")
                # ID yang bisa dibangun jadi URL publik (Phase 1: caption-only,
                # tapi source link user harus bisa dibuka publik — bukan CDN Meta)
                media_id = payload.get("media_id", "") or payload.get("id", "") or ""
                reel_video_id = payload.get("reel_video_id", "") or ""
                ig_post_media_id = payload.get("ig_post_media_id", "") or ""
                break
        pending_claims[from_id] = {
            "url": url,
            "title": title,
            "media_type": media_type,
            "media_id": media_id,
            "reel_video_id": reel_video_id,
            "ig_post_media_id": ig_post_media_id,
            "_platform": platform,
        }
        logger.info(f"🎬 Media DM accepted — awaiting claim (pending for {from_id}, platform={platform})")
        await _reply_dm(from_id, ACCEPT_REPLY, platform)
        return

    # Text-only
    if msg_text:
        if from_id in pending_claims:
            media = pending_claims.pop(from_id)  # consume-once, clear
            logger.info(f'📝 Claim diterima dari {from_id} untuk media {media["url"]}: "{msg_text[:100]}" (pending cleared)')
            # Enqueue ke job store (SQLite lokal / Postgres VPS) — graceful kalau gagal
            await _create_claim_job(media, msg_text, from_id)
            await _reply_dm(from_id, CLAIM_RECEIVED_REPLY, platform)
            return
        logger.info("🚫 Text-only DM tanpa pending — denied")
        await _reply_dm(from_id, DENY_REPLY, platform)
        return

    # No text AND no media — weird payload, skip silently
    logger.info("⏭️ DM without text or media — skipping")
