"""Hermetic tests for multilingual i18n support in renderer.py and llm.py."""
import asyncio
import pytest
from app.pipeline.renderer import _get_i18n, render_markdown, _I18N, _I18N_FALLBACK


# ── _get_i18n() tests ─────────────────────────────────────────────────────────

def test_get_i18n_indonesian():
    i18n = _get_i18n("id")
    assert i18n["h1_prefix"] == "Hasil Analisa"
    assert i18n["conclusion_label"] == "Kesimpulan"
    assert i18n["verdicts"]["fact"] == "✅ FAKTA"


def test_get_i18n_english():
    i18n = _get_i18n("en")
    assert i18n["h1_prefix"] == "Analysis Result"
    assert i18n["conclusion_label"] == "Conclusion"
    assert i18n["verdicts"]["fact"] == "✅ FACT"


def test_get_i18n_japanese():
    i18n = _get_i18n("ja")
    assert "分析結果" in i18n["h1_prefix"]
    assert "結論" in i18n["conclusion_label"]


def test_get_i18n_unknown_falls_back_to_english():
    i18n = _get_i18n("xx")
    assert i18n["h1_prefix"] == _I18N[_I18N_FALLBACK]["h1_prefix"]
    assert i18n["h1_prefix"] == "Analysis Result"


def test_get_i18n_none_returns_indonesian():
    i18n = _get_i18n(None)
    assert i18n["h1_prefix"] == "Hasil Analisa"


# ── render_markdown() language routing tests ──────────────────────────────────

def _make_verdict(lang: str, verdict: str = "fact") -> dict:
    return {
        "language": lang,
        "verdict": verdict,
        "category": "other",
        "title": "Test Title",
        "summary": "Test summary.",
        "claim": "Test claim",
        "evidence": ["Evidence item"],
        "sources": ["https://example.com"],
        "confidence": 0.9,
        "notes": [],
    }


def test_render_indonesian_headers():
    md = asyncio.run(render_markdown(_make_verdict("id")))
    assert "Hasil Analisa" in md
    assert "Kesimpulan" in md
    assert "Klaim yang Dianalisa" in md
    assert "Analysis Result" not in md


def test_render_english_headers():
    md = asyncio.run(render_markdown(_make_verdict("en")))
    assert "Analysis Result" in md
    assert "Conclusion" in md
    assert "Claim Under Review" in md
    assert "Hasil Analisa" not in md


def test_render_japanese_headers():
    md = asyncio.run(render_markdown(_make_verdict("ja")))
    assert "分析結果" in md
    assert "結論" in md
    assert "Hasil Analisa" not in md
    assert "Analysis Result" not in md


def test_render_unknown_lang_falls_back_to_english():
    md = asyncio.run(render_markdown(_make_verdict("xx")))
    assert "Analysis Result" in md
    assert "Conclusion" in md


def test_render_indonesian_verdict_labels():
    md = asyncio.run(render_markdown(_make_verdict("id", "fact")))
    assert "FAKTA" in md

    md_hoax = asyncio.run(render_markdown(_make_verdict("id", "hoax")))
    assert "HOAX" in md_hoax


def test_render_english_verdict_labels():
    md = asyncio.run(render_markdown(_make_verdict("en", "fact")))
    assert "FACT" in md
    assert "FAKTA" not in md


def test_render_disclaimer_in_correct_language():
    md_id = asyncio.run(render_markdown(_make_verdict("id")))
    assert "Selalu cek sumber resmi" in md_id

    md_en = asyncio.run(render_markdown(_make_verdict("en")))
    assert "Always check official sources" in md_en


def test_render_no_language_field_defaults_to_indonesian():
    verdict = _make_verdict("id")
    del verdict["language"]
    md = asyncio.run(render_markdown(verdict))
    assert "Hasil Analisa" in md


# ── llm.py language extraction test ─────────────────────────────────────────

def test_language_field_extracted_from_llm_response():
    """Simulate what analyze_claim() does with the language field."""
    from app.pipeline.llm import _clean_str
    data = {"language": "en", "verdict": "fact", "summary": "test"}
    language = (_clean_str(data.get("language")) or "id")[:2].lower()
    assert language == "en"


def test_language_field_missing_falls_back_to_id():
    from app.pipeline.llm import _clean_str
    data = {"verdict": "fact", "summary": "test"}
    language = (_clean_str(data.get("language")) or "id")[:2].lower()
    assert language == "id"


def test_language_field_normalization():
    """'id-ID' should normalize to 'id'."""
    from app.pipeline.llm import _clean_str
    data = {"language": "id-ID"}
    language = (_clean_str(data.get("language")) or "id")[:2].lower()
    assert language == "id"
