"""LLM client (OpenAI-compatible) + claim analysis for the FactBot pipeline.

Phase 1 MVP: caption/title reel + claim text -> structured JSON verdict.

Graceful degradation: a missing/placeholder proxy config raises LLMConfigError
(permanent job failure) instead of crashing, so the worker can mark the job
as failed and surface a "BELUM DAPAT DIVERIFIKASI" verdict with notes.
"""

import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

VALID_VERDICTS = {"fact", "hoax", "partly_true", "unverified"}
VALID_CATEGORIES = {
    "health", "government", "politics", "disaster", "finance",
    "technology", "religion", "education", "other",
}

SCHEMA_HINT = (
    "Reply ONLY with ONE JSON object (no markdown fence, no other text) "
    "using exactly this schema:\n"
    '{"verdict": "fact|hoax|partly_true|unverified", '
    '"category": "health|government|politics|disaster|finance|technology|religion|education|other", '
    '"language": "ISO 639-1 code of the claim language e.g. id en ja ar es fr de", '
    '"title": "fact-check report title max 80 chars IN THE SAME LANGUAGE as the claim NOT a copy of the claim question", '
    '"summary": "1-2 sentence summary IN THE SAME LANGUAGE as the claim", '
    '"claim": "claim text as stated by the user", '
    '"evidence": ["verified fact with source IN THE SAME LANGUAGE as the claim"], '
    '"sources": ["source name or URL"], '
    '"confidence": 0.0-1.0, '
    '"notes": ["note or warning IN THE SAME LANGUAGE as the claim"]}'
)


class LLMConfigError(RuntimeError):
    """LLM proxy tidak dikonfigurasi (kosong/placeholder) — error permanen untuk job."""


class LLMError(RuntimeError):
    """Permintaan LLM gagal di level transport/HTTP — retryable oleh worker."""


def _check_config() -> None:
    """Raise LLMConfigError when NO LLM backend is configured.

    Priority: FACTBOT_PROXY_URL (company proxy) → DEEPSEEK_API_KEY (direct).
    Both missing/placeholder → permanent config error for the job.
    """
    proxy = (settings.factbot_proxy_url or "").strip()
    lowered = proxy.lower()
    proxy_valid = bool(proxy) and "your-parkee" not in lowered and "placeholder" not in lowered
    deepseek_ok = bool((settings.deepseek_api_key or "").strip())
    if proxy_valid or deepseek_ok:
        return
    raise LLMConfigError(
        "LLM belum dikonfigurasi: set FACTBOT_PROXY_URL (proxy) atau "
        "DEEPSEEK_API_KEY (direct API) di .env / Settings sebelum pipeline analisa."
    )


def _endpoint() -> str:
    """Build the chat/completions endpoint for the ACTIVE backend."""
    proxy = (settings.factbot_proxy_url or "").strip()
    lowered = proxy.lower()
    if proxy and "your-parkee" not in lowered and "placeholder" not in lowered:
        url = proxy.rstrip("/")
        if url.endswith("/v1"):
            return f"{url}/chat/completions"
        return f"{url}/v1/chat/completions"
    # Fallback: DeepSeek direct
    return f"{settings.deepseek_base_url.rstrip('/')}/chat/completions"


def _model() -> str:
    """Active model: proxy model if proxy configured, else DeepSeek direct model."""
    proxy = (settings.factbot_proxy_url or "").strip()
    lowered = proxy.lower()
    if proxy and "your-parkee" not in lowered and "placeholder" not in lowered:
        return settings.factbot_model or "deepseek-v4-flash"
    return settings.deepseek_model or "deepseek-v4-flash"


def _auth_header() -> dict:
    """Bearer token for the ACTIVE backend."""
    proxy = (settings.factbot_proxy_url or "").strip()
    lowered = proxy.lower()
    if proxy and "your-parkee" not in lowered and "placeholder" not in lowered:
        key = settings.factbot_proxy_key
    else:
        key = settings.deepseek_api_key
    return {"Authorization": f"Bearer {key}"} if key else {}


async def llm_complete(messages: list, json_mode: bool = True, timeout: float | None = None) -> str:
    """POST messages ke LLM backend (FACTBOT_PROXY_URL atau DeepSeek direct).

    Menggunakan helper _endpoint/_model/_auth_header sehingga aktif backend
    dipilih otomatis. Melempar LLMConfigError bila TIDAK ada backend yang
    dikonfigurasi; LLMError untuk kegagalan transport/HTTP/format.
    """
    _check_config()
    timeout = timeout or settings.llm_timeout_seconds
    endpoint = _endpoint()

    headers = {"Content-Type": "application/json"}
    headers.update(_auth_header())

    body = {
        "model": _model(),
        "messages": messages,
        "temperature": 0.2,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    logger.info("llm_complete -> %s (model=%s, json_mode=%s)", endpoint, _model(), json_mode)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(endpoint, headers=headers, json=body)

    if resp.is_error:
        raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"LLM proxy mengembalikan format non-OpenAI: {exc}") from exc

    if not isinstance(content, str) or not content.strip():
        raise LLMError("LLM proxy mengembalikan konten kosong.")

    return content.strip()


def _extract_json(text: str):
    """Ekstraksi JSON toleran: JSON mentah, fenced ```json```, atau teks dengan blok JSON."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass

    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        try:
            return json.loads(text)
        except ValueError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            pass
    return None


def _validate_verdict(data) -> tuple:
    """Return (verdict, category, problems). problems non-kosong berarti invalid."""
    if not isinstance(data, dict):
        return None, None, ["response bukan JSON object"]
    verdict = data.get("verdict")
    category = data.get("category")
    problems = []
    if verdict not in VALID_VERDICTS:
        problems.append(f"verdict {verdict!r} bukan salah satu dari {sorted(VALID_VERDICTS)}")
    if category not in VALID_CATEGORIES:
        problems.append(f"category {category!r} bukan salah satu dari {sorted(VALID_CATEGORIES)}")
    return verdict, category, problems


def _clean_str(value) -> str:
    return str(value).strip() if value is not None else ""


def _clean_list(value) -> list:
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def _clamp_confidence(value) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, conf))


def _build_search_query(caption: str, claim: str) -> str:
    """Build a concise search query from caption and claim for SearXNG.

    Priority: caption (contains the actual substance) over claim (often just
    'Is this true?' from the user). Falls back to claim when caption is empty.
    Strips URLs, hashtags, @mentions before extracting keywords.
    """
    import re

    def _clean(text: str) -> str:
        t = re.sub(r"https?://\S+", "", text)
        t = re.sub(r"#\S+", "", t)
        t = re.sub(r"@\S+", "", t)
        return " ".join(t.split()).strip()

    clean_caption = _clean(caption or "")
    clean_claim = _clean(claim or "")

    # Strip generic question prefixes from claim — they add no search value
    _generic_prefixes = (
        "is this true", "is this information true", "is this news true",
        "is this information are true", "is this news are true",
        "apakah benar", "apakah ini benar", "benarkah", "fact check:",
        "ces informations sont", "ist das wahr",
    )
    claim_lower = clean_claim.lower()
    for prefix in _generic_prefixes:
        if claim_lower.startswith(prefix):
            clean_claim = clean_claim[len(prefix):].strip(" ?:.,")
            break

    # Caption is primary signal — take first 100 chars of cleaned caption
    # Append remaining claim keywords only if they add substance
    if clean_caption:
        query = clean_caption[:100]
        # Append claim remainder only if it's not empty and not already covered
        if clean_claim and clean_claim.lower() not in query.lower():
            query = f"{query} {clean_claim}"
    else:
        query = clean_claim

    return query[:150].strip()


async def searxng_search(query: str, max_results: int = 5) -> list[dict]:
    """Query SearXNG and return list of {title, url, content} dicts.

    Returns empty list on any error (graceful fallback — pipeline continues).
    """
    base_url = (settings.searxng_url or "").rstrip("/")
    if not base_url:
        logger.warning("searxng_search: SEARXNG_URL tidak dikonfigurasi, skip.")
        return []

    params = {
        "q": query,
        "format": "json",
        "language": "auto",
        "categories": "general,news",
        "engines": "google,bing,duckduckgo",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base_url}/search", params=params)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        out = []
        for r in results[:max_results]:
            content = str(r.get("content") or "").strip()
            out.append({
                "title": str(r.get("title") or "").strip(),
                "url": str(r.get("url") or "").strip(),
                "content": content[:300],
            })
        return out
    except Exception as exc:
        logger.warning("searxng_search gagal (query=%r): %s", query[:80], exc)
        return []


async def analyze_claim(caption: str, claim: str) -> dict:
    """Analisa klaim terhadap caption/title dan kembalikan dict verdict tervalidasi.

    Primary: Hermes brain via webhook (has MCP SearXNG tool calling + SOUL.md rules).
    Fallback: DeepSeek direct API (with local SearXNG search injection).
    """
    # ── Primary: Hermes brain via webhook ──────────────────────────────────
    if settings.hermes_webhook_url and settings.hermes_webhook_url.strip():
        logger.info("Using Hermes brain as primary LLM engine")
        try:
            from app.pipeline.hermes_client import call_hermes
            hermes_res = await call_hermes(caption, claim, timeout=settings.llm_timeout_seconds)
            if hermes_res is not None:
                logger.info(f"Hermes brain verdict: {hermes_res.get('verdict')}")
                return hermes_res
            logger.warning("Hermes brain returned None — falling back to DeepSeek direct")
        except Exception as exc:
            logger.warning(f"Hermes brain call failed ({exc}) — falling back to DeepSeek direct")

    # ── Fallback: DeepSeek direct ──────────────────────────────────────────
    # Fetch web search results for grounding (graceful fallback if unavailable)
    search_query = _build_search_query(caption, claim)
    web_results = await searxng_search(search_query)

    web_context = ""
    if web_results:
        snippets = []
        for i, r in enumerate(web_results, 1):
            snippet = f"{i}. [{r['title']}]({r['url']})"
            if r["content"]:
                snippet += f"\n   {r['content'][:300]}"
            snippets.append(snippet)
        web_context = "\n\nWEB SEARCH RESULTS:\n" + "\n".join(snippets)
        logger.info("searxng_search: %d hasil untuk query=%r", len(web_results), search_query[:60])
    else:
        logger.warning("searxng_search: tidak ada hasil, pipeline lanjut tanpa web context.")

    system = (
        "You are a fact-check analyst for FactBot (KlarifAI). Your job is to verify "
        "claims from social media reel/post captions.\n"
        "STRICT RULES:\n"
        "1. EVIDENCE-ONLY: only use facts verifiable from trusted sources "
        "(official news, government/institution statements, public data). NEVER fabricate "
        "facts, numbers, or sources.\n"
        "2. If evidence is insufficient to decide, verdict MUST be 'unverified'.\n"
        "3. 'verdict' and 'category' MUST use the enum values provided.\n"
        "4. Detect the language of the claim text automatically and put its ISO 639-1 code "
        "in the 'language' field (e.g. 'id' for Indonesian, 'en' for English, 'ja' for Japanese, "
        "'ar' for Arabic, 'es' for Spanish).\n"
        "5. Write ALL text fields (title, summary, evidence, notes) in THE SAME LANGUAGE as "
        "the claim. Do NOT mix languages.\n"
        "6. 'confidence' reflects how certain you are based on verifiable evidence (0.0-1.0).\n"
        "7. If WEB SEARCH RESULTS are provided, use them as primary grounding evidence. "
        "Cite URLs from search results as sources."
    )

    user = (
        f"Caption/title reel:\n{caption or '(kosong)'}\n\n"
        f"Klaim yang harus diverifikasi:\n{claim}"
        f"{web_context}\n\n"
        f"{SCHEMA_HINT}"
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    raw = await llm_complete(messages, json_mode=True)
    data = _extract_json(raw)
    verdict, category, problems = _validate_verdict(data)

    if problems:
        # Satu retry dengan feedback skema eksplisit.
        feedback = (
            "Respons sebelumnya tidak valid: " + "; ".join(problems) +
            ". Kirim ulang HANYA JSON valid sesuai skema."
        )
        messages.append({"role": "user", "content": feedback})
        raw_retry = await llm_complete(messages, json_mode=True)
        data = _extract_json(raw_retry)
        verdict, category, problems = _validate_verdict(data)

    if problems:
        verdict, category = "unverified", "other"
        notes = [
            "Model tidak menghasilkan verdict valid; fallback ke 'unverified'.",
            "Detail: " + "; ".join(problems),
        ]
        title, summary, evidence, sources, confidence, language = "", "", [], [], 0.0, "id"
    else:
        notes = _clean_list(data.get("notes")) if isinstance(data, dict) else []
        title = _clean_str(data.get("title")) if isinstance(data, dict) else ""
        summary = _clean_str(data.get("summary")) if isinstance(data, dict) else ""
        evidence = _clean_list(data.get("evidence")) if isinstance(data, dict) else []
        sources = _clean_list(data.get("sources")) if isinstance(data, dict) else []
        confidence = _clamp_confidence(data.get("confidence")) if isinstance(data, dict) else 0.0
        language = (_clean_str(data.get("language")) or "id")[:2].lower() if isinstance(data, dict) else "id"

    return {
        "verdict": verdict,
        "category": category,
        "language": language,
        "title": title,
        "summary": summary,
        "claim": claim,
        "evidence": evidence,
        "sources": sources,
        "confidence": confidence,
        "notes": notes,
    }
