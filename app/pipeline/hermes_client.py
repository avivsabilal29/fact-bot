"""Hermes Brain webhook client for FactBot pipeline.

Sends claim analysis requests to Hermes brain (running in Docker with MCP SearXNG).
Hermes acts as the AI engine: reads SOUL.md rules, calls searxng_web_search tool,
returns a structured JSON verdict.

Falls back to DeepSeek direct if Hermes is unavailable.
"""

import hashlib
import hmac
import json
import logging
import re
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

VALID_VERDICTS = {"fact", "hoax", "partly_true", "unverified"}


def _sign_body(body_bytes: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 hex digest over body bytes (X-Webhook-Signature V1)."""
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def _format_hermes_prompt(caption: str, claim: str) -> str:
    """Format prompt for Hermes brain with full caption and claim."""
    return (
        "You are FactBot. Verify the following claim from a social media post using evidence.\n\n"
        f"CAPTION FROM POST:\n{caption}\n\n"
        f"CLAIM TO VERIFY:\n{claim}\n\n"
        "INSTRUCTIONS:\n"
        "1. Search the web for evidence using the searxng_web_search tool.\n"
        "2. Run 2-3 specific search queries derived from keywords in the CAPTION "
        "(do not use generic 'is this true?' questions).\n"
        "3. Analyze the evidence found from web search.\n"
        "4. Output ONLY the raw JSON verdict object as specified in your SOUL.md contract. No other text.\n"
    )


def _extract_json_from_reply(reply: str) -> Optional[dict]:
    """Parse JSON verdict from Hermes reply text.

    Handles raw JSON, JSON in markdown fences, or JSON embedded in text.
    Validates required fields and a valid verdict value.
    """
    if not reply or not reply.strip():
        return None

    cleaned = reply.strip()

    # Strip markdown code fence if present
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n?```$", "", cleaned)
        cleaned = cleaned.strip()

    data = None

    # Try direct parse first
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: find the outermost JSON object
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

    if not isinstance(data, dict):
        logger.warning("Hermes reply is not a valid JSON object: %s", reply[:150])
        return None

    verdict = str(data.get("verdict") or "").lower()
    if verdict not in VALID_VERDICTS:
        logger.warning(
            "Hermes returned invalid verdict '%s' (must be one of %s)", verdict, VALID_VERDICTS
        )
        return None

    data["verdict"] = verdict
    return data


async def call_hermes(caption: str, claim: str, timeout: float = 120.0) -> Optional[dict]:
    """Call Hermes brain webhook to analyze claim.

    Returns parsed verdict dict if successful, or None on any error/timeout.
    Never raises an exception -- caller handles None by falling back to DeepSeek.

    Signs the request body with HMAC-SHA256 (X-Webhook-Signature V1 header)
    using HERMES_WEBHOOK_SECRET (the factbot subscription secret).
    """
    base_url = (settings.hermes_webhook_url or "").rstrip("/")
    if not base_url:
        logger.info("HERMES_WEBHOOK_URL not set -- skipping Hermes brain")
        return None

    secret = settings.hermes_webhook_secret or ""
    if not secret:
        logger.warning("HERMES_WEBHOOK_SECRET not set -- cannot sign Hermes webhook requests")
        return None

    prompt = _format_hermes_prompt(caption, claim)
    payload = {
        "platform": "webhook",
        "user_id": "factbot-pipeline",
        "message": prompt,
    }
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signature = _sign_body(body_bytes, secret)

    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,  # Hermes generic V1 HMAC-SHA256
    }

    url = f"{base_url}/webhooks/factbot"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            logger.info("Calling Hermes brain at %s (timeout=%ss)...", url, timeout)
            resp = await client.post(url, content=body_bytes, headers=headers)
            resp.raise_for_status()

            res_data = resp.json()
            reply_text = ""
            if isinstance(res_data, dict):
                reply_text = str(res_data.get("reply") or res_data.get("message") or "")
            elif isinstance(res_data, str):
                reply_text = res_data
            if not reply_text:
                reply_text = resp.text

            verdict_dict = _extract_json_from_reply(reply_text)
            if verdict_dict:
                logger.info("Hermes brain verdict: %s", verdict_dict.get("verdict"))
                return verdict_dict

            logger.warning("Failed to parse JSON verdict from Hermes reply: %s", reply_text[:200])
            return None

    except httpx.TimeoutException:
        logger.warning("Hermes brain timed out after %ss", timeout)
        return None
    except Exception as e:
        logger.warning("Hermes brain request failed: %s", e)
        return None
