"""Unit tests for app/pipeline/hermes_callback.py."""
import asyncio
import pytest
import pytest_asyncio
import httpx
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
async def test_register_and_resolve():
    """Register a delivery_id, resolve it, assert wait_for_result returns the text."""
    from app.pipeline import hermes_callback as hc
    # Clean state
    hc._pending.clear()
    hc._results.clear()

    hc.register_pending("test-001")
    hc.resolve_callback("test-001", "hello verdict")
    result = await hc.wait_for_result("test-001", timeout=2.0)
    assert result == "hello verdict"


@pytest.mark.asyncio
async def test_wait_timeout():
    """Register but never resolve -- wait_for_result returns None on timeout."""
    from app.pipeline import hermes_callback as hc
    hc._pending.clear()
    hc._results.clear()

    hc.register_pending("test-002")
    result = await hc.wait_for_result("test-002", timeout=0.1)
    assert result is None


@pytest.mark.asyncio
async def test_callback_endpoint_ok():
    """POST /hermes-callback with delivery_id in body resolves the waiter."""
    from app.pipeline import hermes_callback as hc
    from app.main import app

    hc._pending.clear()
    hc._results.clear()

    # Register a pending delivery
    hc.register_pending("test-003")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/hermes-callback",
            json={"delivery_id": "test-003", "message": "reply from hermes"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # The waiter should now unblock immediately
    result = await hc.wait_for_result("test-003", timeout=1.0)
    assert result == "reply from hermes"


@pytest.mark.asyncio
async def test_callback_endpoint_no_delivery_id():
    """POST /hermes-callback with no delivery_id returns ignored."""
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/hermes-callback",
            json={"message": "reply with no id"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_callback_endpoint_header_delivery_id():
    """POST /hermes-callback with X-Delivery-Id header resolves the waiter."""
    from app.pipeline import hermes_callback as hc
    from app.main import app

    hc._pending.clear()
    hc._results.clear()

    hc.register_pending("test-004")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/hermes-callback",
            json={"message": "via header"},
            headers={"X-Delivery-Id": "test-004"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    result = await hc.wait_for_result("test-004", timeout=1.0)
    assert result == "via header"
