"""Hermetic tests for SearXNG integration in app/pipeline/llm.py."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# searxng_search — returns [] on connection error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_searxng_search_returns_empty_on_connection_error():
    from app.pipeline.llm import searxng_search

    with patch("app.pipeline.llm.httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get.side_effect = Exception("connection refused")

        result = await searxng_search("test query")

    assert result == []


@pytest.mark.asyncio
async def test_searxng_search_returns_parsed_results():
    from app.pipeline.llm import searxng_search

    fake_data = {
        "results": [
            {"title": "Title 1", "url": "http://example.com/1", "content": "Excerpt one"},
            {"title": "Title 2", "url": "http://example.com/2"},  # no content key
        ]
    }

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = fake_data

    with patch("app.pipeline.llm.httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=mock_resp)

        result = await searxng_search("test query", max_results=5)

    assert len(result) == 2
    assert result[0] == {"title": "Title 1", "url": "http://example.com/1", "content": "Excerpt one"}
    assert result[1]["content"] == ""


@pytest.mark.asyncio
async def test_searxng_search_content_truncated_to_300():
    from app.pipeline.llm import searxng_search

    long_content = "x" * 500
    fake_data = {"results": [{"title": "T", "url": "http://u.com", "content": long_content}]}

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = fake_data

    with patch("app.pipeline.llm.httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=mock_resp)

        result = await searxng_search("q")

    assert len(result[0]["content"]) == 300


# ---------------------------------------------------------------------------
# _build_search_query — strips hashtags, URLs, @mentions
# ---------------------------------------------------------------------------

def test_build_search_query_strips_hashtags():
    from app.pipeline.llm import _build_search_query

    caption = "Breaking news #viral #hoax @user check this"
    claim = "Vaccine causes harm"
    result = _build_search_query(caption, claim)

    assert "#viral" not in result
    assert "#hoax" not in result
    assert "@user" not in result
    assert "Vaccine causes harm" in result


def test_build_search_query_strips_urls():
    from app.pipeline.llm import _build_search_query

    caption = "See https://t.co/abcdef and http://example.com/path?q=1 for details"
    claim = "some claim"
    result = _build_search_query(caption, claim)

    assert "https://" not in result
    assert "http://" not in result


def test_build_search_query_max_150_chars():
    from app.pipeline.llm import _build_search_query

    caption = "word " * 50   # very long caption
    claim = "claim " * 20
    result = _build_search_query(caption, claim)

    assert len(result) <= 150


def test_build_search_query_empty_caption():
    from app.pipeline.llm import _build_search_query

    result = _build_search_query("", "my claim here")
    assert result == "my claim here"


# ---------------------------------------------------------------------------
# analyze_claim — web block injected when search_results non-empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_claim_injects_web_block():
    """When searxng returns results, the user prompt must include the WEB SEARCH RESULTS block."""
    from app.pipeline import llm as llm_module

    fake_search_results = [
        {"title": "BBC Report", "url": "https://bbc.com/news/1", "content": "Some fact here"},
    ]

    captured_messages = []

    async def fake_llm_complete(messages, **kwargs):
        captured_messages.extend(messages)
        return (
            '{"verdict": "fact", "category": "other", "summary": "OK", '
            '"claim": "test", "evidence": [], "sources": [], "confidence": 0.9, "notes": []}'
        )

    with patch.object(llm_module, "searxng_search", return_value=fake_search_results), \
         patch.object(llm_module, "llm_complete", side_effect=fake_llm_complete):

        await llm_module.analyze_claim("caption text", "test claim")

    assert captured_messages, "llm_complete was never called"
    user_msg = next(m["content"] for m in captured_messages if m["role"] == "user")
    assert "WEB SEARCH RESULTS" in user_msg
    assert "https://bbc.com/news/1" in user_msg
    assert "BBC Report" in user_msg


@pytest.mark.asyncio
async def test_analyze_claim_no_web_block_when_search_empty():
    """When searxng returns [], the WEB SEARCH RESULTS block must NOT appear."""
    from app.pipeline import llm as llm_module

    captured_messages = []

    async def fake_llm_complete(messages, **kwargs):
        captured_messages.extend(messages)
        return (
            '{"verdict": "unverified", "category": "other", "summary": "No data", '
            '"claim": "test", "evidence": [], "sources": [], "confidence": 0.1, "notes": []}'
        )

    with patch.object(llm_module, "searxng_search", return_value=[]), \
         patch.object(llm_module, "llm_complete", side_effect=fake_llm_complete):

        await llm_module.analyze_claim("caption", "some claim")

    user_msg = next(m["content"] for m in captured_messages if m["role"] == "user")
    assert "WEB SEARCH RESULTS" not in user_msg
