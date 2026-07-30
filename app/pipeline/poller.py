"""Poll Instagram for new @mentions every 30 seconds.
No webhooks needed — just direct API calls."""

import logging
import asyncio
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

BOT_USERNAME = "factacheckfact"
CHECKED_COMMENTS_FILE = "data/checked_comments.txt"


async def get_recent_mentions(token: str, ig_business_id: str) -> list:
    """Poll Instagram Business Account for recent media with comments."""
    url = f"https://graph.facebook.com/v25.0/{ig_business_id}/media"
    params = {
        "fields": "id,caption,comments{id,text,from{username},timestamp}",
        "access_token": token,
        "limit": 10,
    }
    
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        if resp.is_error:
            logger.warning(f"Poll failed: {resp.text[:100]}")
            return []
        
        data = resp.json()
        mentions = []
        
        for media in data.get("data", []):
            comments = media.get("comments", {}).get("data", [])
            for comment in comments:
                text = comment.get("text", "")
                if f"@{BOT_USERNAME}" in text.lower():
                    mentions.append({
                        "comment_id": comment["id"],
                        "media_id": media["id"],
                        "text": text,
                        "username": comment.get("from", {}).get("username", "unknown"),
                        "timestamp": comment.get("timestamp", ""),
                    })
        
        return mentions


def load_checked() -> set:
    """Load already-processed comment IDs."""
    try:
        with open(CHECKED_COMMENTS_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def save_checked(comment_id: str):
    """Save processed comment ID."""
    with open(CHECKED_COMMENTS_FILE, "a") as f:
        f.write(f"{comment_id}\n")


async def check_and_reply(token: str, ig_business_id: str):
    """Check for new mentions and reply."""
    from app.webhooks.meta import _process_mention
    
    BOT_USERNAME = "factacheckfact"
    
    checked = load_checked()
    mentions = await get_recent_mentions(token, ig_business_id)
    new_count = 0
    
    for mention in mentions:
        cid = mention["comment_id"]
        if cid in checked:
            continue
        
        logger.info(f"🆕 New @mention from @{mention['username']}: \"{mention['text'][:60]}\"")
        
        # Skip if from bot itself
        if mention["username"].lower() == BOT_USERNAME:
            save_checked(cid)
            continue
        
        # Reply
        success = await _process_mention(
            comment_id=cid,
            media_id=mention["media_id"],
            text=mention["text"],
            from_user=mention["username"],
            token=token,
        )
        
        if success:
            save_checked(cid)
            new_count += 1
            logger.info(f"✅ Replied to @{mention['username']}")
    
    return new_count


async def poll_loop():
    """Main polling loop - runs every 30 seconds."""
    from app.config import settings
    
    token = settings.meta_page_access_token
    ig_id = settings.ig_business_id
    
    if not token or not ig_id:
        logger.error("Token or IG ID not configured")
        return
    
    logger.info(f"🤖 Starting mention poller for IG {ig_id} every 30s")
    
    while True:
        try:
            count = await check_and_reply(token, ig_id)
            if count:
                logger.info(f"📬 {count} new mention(s) replied")
        except Exception as e:
            logger.error(f"Poll error: {e}")
        
        await asyncio.sleep(30)
