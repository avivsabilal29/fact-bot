"""Hermes Brain API client for FactBot pipeline.

Uses Hermes api_server (POST /v1/runs + GET /v1/runs/{run_id} polling) to run
the agent with MCP SearXNG tools and get a structured JSON verdict.

Falls back to DeepSeek direct if Hermes is unavailable.
"""

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

VALID_VERDICTS = {"fact", "hoax", "partly_true", "unverified"}

# Poll interval for /v1/runs/{run_id} status checks
_POLL_INTERVAL = 3.0


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


def _extract_json_from_reply(reply: str, default_claim: str = "") -> Optional[dict]:
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

    verdict_raw = str(data.get("verdict") or "").lower()
    rating_raw = str(data.get("rating") or "").lower()

    # Normalize common LLM verdict / rating variants
    verdict_map = {
        "true": "fact",
        "fact": "fact",
        "benar": "fact",
        "false": "hoax",
        "hoax": "hoax",
        "fake": "hoax",
        "salah": "hoax",
        "partly_true": "partly_true",
        "mostly_true": "partly_true",
        "partially_true": "partly_true",
        "unverified": "unverified",
        "unknown": "unverified",
    }
    verdict = verdict_map.get(verdict_raw) or verdict_map.get(rating_raw)

    # If raw verdict starts with 'benar' or 'true' or 'fact' (e.g. "BENAR (dengan catatan angka)")
    if not verdict:
        for prefix, norm_val in [("benar", "fact"), ("true", "fact"), ("fact", "fact"), ("salah", "hoax"), ("false", "hoax"), ("hoax", "hoax")]:
            if verdict_raw.startswith(prefix) or rating_raw.startswith(prefix):
                verdict = norm_val
                break
    if not verdict:
        logger.warning(
            "Hermes returned unmapped verdict '%s' (raw data: %s)", verdict_raw, data
        )
        return None

    data["verdict"] = verdict

    # Map explanation -> summary if summary is missing
    if not data.get("summary") and data.get("explanation"):
        data["summary"] = data["explanation"]
    elif not data.get("summary"):
        data["summary"] = "Hasil analisa klaim oleh FactBot."

    # Ensure claim field exists
    if not data.get("claim"):
        data["claim"] = default_claim

    # Normalize category to match factbot.tech API validation schema
    valid_categories = {
        "health", "government", "politics", "disaster", 
        "finance", "technology", "religion", "education", "other"
    }
    cat_raw = str(data.get("category") or "").lower().strip()
    if cat_raw not in valid_categories:
        data["category"] = "other"
    else:
        data["category"] = cat_raw

    if "confidence" not in data or data["confidence"] is None:
        data["confidence"] = 0.85
    if "language" not in data or not data["language"]:
        data["language"] = "en"
    return data


async def call_hermes(caption: str, claim: str, timeout: float = 120.0) -> Optional[dict]:
    """Call Hermes brain via api_server /v1/runs to analyze claim.

    Flow:
      1. POST /v1/runs with the analysis prompt
      2. Poll GET /v1/runs/{run_id} until status is 'completed' or timeout
      3. Extract assistant reply from run result
      4. Parse JSON verdict from reply

    Returns parsed verdict dict, or None if timeout/unavailable (caller falls back to DeepSeek).
    """
    base_url = (settings.hermes_webhook_url or "").rstrip("/")
    if not base_url:
        logger.info("HERMES_WEBHOOK_URL not set -- skipping Hermes brain")
        return None

    api_key = settings.hermes_webhook_secret or ""
    if not api_key:
        logger.warning("HERMES_WEBHOOK_SECRET not set -- cannot authenticate with Hermes api_server")
        return None

    # api_server runs on port 8642, use /p/factbot/ prefix to load factbot profile (SOUL.md + MCP)
    api_url = base_url.replace(":8644", ":8642") + "/p/factbot"

    prompt = _format_hermes_prompt(caption, claim)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    run_payload = {
        "input": prompt,
        "stream": False,
    }

    deadline = time.monotonic() + timeout

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Start a run
            logger.info("Starting Hermes run at %s/v1/runs...", api_url)
            resp = await client.post(
                f"{api_url}/v1/runs",
                json=run_payload,
                headers=headers,
            )
            resp.raise_for_status()
            run_data = resp.json()
            run_id = run_data.get("id") or run_data.get("run_id")
            if not run_id:
                logger.warning("Hermes /v1/runs returned no run_id: %s", run_data)
                return None

            logger.info("Hermes run started: %s, polling for result...", run_id)

            # Step 2: Poll until completed or timeout
            while time.monotonic() < deadline:
                await __import__("asyncio").sleep(_POLL_INTERVAL)

                poll_resp = await client.get(
                    f"{api_url}/v1/runs/{run_id}",
                    headers=headers,
                )
                poll_resp.raise_for_status()
                run_status = poll_resp.json()

                status = run_status.get("status", "")
                logger.debug("Hermes run %s status: %s", run_id, status)

                if status == "completed":
                    # Extract assistant reply from output
                    output = run_status.get("output") or run_status.get("result") or ""
                    if isinstance(output, list):
                        # OpenAI Responses API format: list of output items
                        for item in output:
                            if isinstance(item, dict):
                                content = item.get("content") or ""
                                if isinstance(content, list):
                                    for c in content:
                                        if isinstance(c, dict) and c.get("type") == "output_text":
                                            output = c.get("text", "")
                                            break
                                elif isinstance(content, str):
                                    output = content
                                    break
                    elif isinstance(output, dict):
                        output = str(output)

                    reply_text = str(output).strip()
                    if not reply_text:
                        logger.warning("Hermes run %s completed but output is empty", run_id)
                        return None

                    verdict_dict = _extract_json_from_reply(reply_text, claim)
                    if verdict_dict:
                        logger.info("Hermes run %s verdict: %s", run_id, verdict_dict.get("verdict"))
                        return verdict_dict

                    logger.warning(
                        "Hermes run %s: failed to parse verdict from: %s", run_id, reply_text[:200]
                    )
                    return None

                if status in ("failed", "cancelled", "error"):
                    logger.warning("Hermes run %s ended with status: %s", run_id, status)
                    return None

            logger.warning("Hermes run %s timed out after %.0fs", run_id, timeout)
            return None

    except httpx.TimeoutException:
        logger.warning("Hermes api_server request timed out")
        return None
    except Exception as e:
        logger.warning("Hermes api_server call failed: %s", e)
        return None
