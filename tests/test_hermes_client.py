"""Hermetic unit tests for app/pipeline/hermes_client.py."""
import pytest
from app.pipeline.hermes_client import (
    _extract_json_from_reply,
    _format_hermes_prompt,
    VALID_VERDICTS,
)


def test_format_hermes_prompt_contains_full_caption_and_claim():
    caption = "This is a full caption with many words about a claim"
    claim = "Is this information are true?"
    prompt = _format_hermes_prompt(caption, claim)

    assert caption in prompt
    assert claim in prompt
    assert "searxng_web_search" in prompt


def test_extract_json_raw_valid_json():
    reply = '{"verdict": "fact", "category": "health", "title": "Test Title", "summary": "Test Summary"}'
    res = _extract_json_from_reply(reply)

    assert res is not None
    assert res["verdict"] == "fact"
    assert res["category"] == "health"


def test_extract_json_embedded_in_markdown():
    reply = """Here is my analysis:
```json
{
  "verdict": "hoax",
  "category": "politics",
  "title": "Hoax Title",
  "summary": "This is false."
}
```
"""
    res = _extract_json_from_reply(reply)

    assert res is not None
    assert res["verdict"] == "hoax"


def test_extract_json_invalid_verdict_returns_none():
    reply = '{"verdict": "completely_true", "category": "health"}'
    res = _extract_json_from_reply(reply)

    assert res is None


def test_extract_json_non_json_returns_none():
    reply = "I could not find any evidence for this claim."
    res = _extract_json_from_reply(reply)

    assert res is None


def test_extract_json_empty_reply_returns_none():
    assert _extract_json_from_reply("") is None
    assert _extract_json_from_reply(None) is None
