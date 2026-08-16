"""Hermes callback receiver for FactBot pipeline.

Manages pending Hermes analysis requests using asyncio.Event per delivery_id.
After Hermes fires the agent async, it POSTs the result back to POST /hermes-callback.
The waiting call_hermes() is unblocked and gets the verdict text.
"""

import asyncio
import logging
from typing import Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Registry: delivery_id -> asyncio.Event (signalled when result arrives)
_pending: Dict[str, asyncio.Event] = {}
# Registry: delivery_id -> reply text from Hermes agent
_results: Dict[str, str] = {}


def register_pending(delivery_id: str) -> asyncio.Event:
    """Register a delivery_id we are waiting on. Returns the Event to await."""
    event = asyncio.Event()
    _pending[delivery_id] = event
    logger.debug("Registered pending Hermes callback for delivery_id=%s", delivery_id)
    return event


def resolve_callback(delivery_id: str, reply_text: str) -> None:
    """Store reply_text and unblock the waiter for delivery_id."""
    _results[delivery_id] = reply_text
    event = _pending.get(delivery_id)
    if event is None:
        logger.warning(
            "Hermes callback delivery_id=%s not found in pending registry -- ignored",
            delivery_id,
        )
        return
    event.set()
    logger.info("Resolved Hermes callback delivery_id=%s (%d chars)", delivery_id, len(reply_text))


async def wait_for_result(delivery_id: str, timeout: float = 90.0) -> Optional[str]:
    """Wait for the Hermes callback result. Returns reply text or None on timeout."""
    event = _pending.get(delivery_id)
    if event is None:
        logger.warning("wait_for_result called for unregistered delivery_id=%s", delivery_id)
        return None
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        reply = _results.get(delivery_id)
        return reply
    except asyncio.TimeoutError:
        logger.warning(
            "Hermes callback timed out after %.1fs for delivery_id=%s", timeout, delivery_id
        )
        return None
    finally:
        # Always clean up regardless of outcome
        _pending.pop(delivery_id, None)
        _results.pop(delivery_id, None)


@router.post("/hermes-callback")
async def hermes_callback(request: Request) -> JSONResponse:
    """Receive async result from Hermes brain after agent finishes.

    Hermes delivers agent output via HTTP POST here (deliver: http).
    Always returns 200 -- Hermes retries on non-2xx.
    """
    # Extract delivery_id: try header first, then body
    delivery_id = request.headers.get("X-Delivery-Id", "")

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    if not delivery_id:
        delivery_id = str(body.get("delivery_id", ""))

    if not delivery_id:
        logger.warning("Hermes callback received with no delivery_id -- ignored")
        return JSONResponse({"status": "ignored", "reason": "no delivery_id"})

    # Extract reply text -- try common field names Hermes may use
    reply_text = ""
    for field in ("message", "reply", "content", "text"):
        val = body.get(field)
        if val and isinstance(val, str):
            reply_text = val
            break

    if not reply_text:
        # Last resort: serialize the whole body
        reply_text = str(body)

    resolve_callback(delivery_id, reply_text)
    return JSONResponse({"status": "ok", "delivery_id": delivery_id})
