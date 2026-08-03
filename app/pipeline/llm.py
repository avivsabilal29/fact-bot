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
    "Balas HANYA dengan SATU objek JSON (tanpa markdown fence, tanpa teks lain) "
    "menggunakan skema persis berikut:\n"
    '{"verdict": "fact|hoax|partly_true|unverified", '
    '"category": "health|government|politics|disaster|finance|technology|religion|education|other", '
    '"summary": "<ringkasan 1-2 kalimat dalam Bahasa Indonesia>", '
    '"claim": "<teks klaim yang dianalisa>", '
    '"evidence": ["<fakta terverifikasi dengan sumber>"], '
    '"sources": ["<nama/URL sumber rujukan>"], '
    '"confidence": 0.0-1.0, '
    '"notes": ["<catatan/peringatan>"]}'
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


async def analyze_claim(caption: str, claim: str) -> dict:
    """Analisa klaim terhadap caption/title dan kembalikan dict verdict tervalidasi.

    Selalu mengembalikan dict dengan 'verdict' ∈ VALID_VERDICTS. Output LLM yang
    invalid/tidak terparse memicu 1x retry dengan feedback skema; bila masih
    invalid, fallback ke verdict='unverified' + notes penjelas.
    Melempar LLMConfigError bila proxy tidak dikonfigurasi (permanent error).
    """
    system = (
        "Kamu adalah analis fact-check untuk FactBot (KlarifAI). Tugasmu memverifikasi "
        "klaim yang muncul di caption/title reel media sosial.\n"
        "ATURAN TEGAS:\n"
        "1. EVIDENCE-ONLY: hanya gunakan fakta yang bisa diverifikasi dari sumber terpercaya "
        "(pemberitaan resmi, pernyataan pemerintah/lembaga, data publik). JANGAN PERNAH mengarang "
        "fakta, angka, atau sumber.\n"
        "2. Kalau bukti tidak cukup untuk memutuskan, verdict WAJIB 'unverified'.\n"
        "3. 'verdict' dan 'category' WAJIB memakai nilai enum yang diberikan.\n"
        "4. 'summary' dalam Bahasa Indonesia, 1-2 kalimat, netral.\n"
        "5. 'confidence' mencerminkan seberapa yakin kamu terhadap verdict berdasarkan bukti "
        "yang benar-benar ada (0.0-1.0)."
    )

    user = (
        f"Caption/title reel:\n{caption or '(kosong)'}\n\n"
        f"Klaim yang harus diverifikasi:\n{claim}\n\n"
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
        summary, evidence, sources, confidence = "", [], [], 0.0
    else:
        notes = _clean_list(data.get("notes")) if isinstance(data, dict) else []
        summary = _clean_str(data.get("summary")) if isinstance(data, dict) else ""
        evidence = _clean_list(data.get("evidence")) if isinstance(data, dict) else []
        sources = _clean_list(data.get("sources")) if isinstance(data, dict) else []
        confidence = _clamp_confidence(data.get("confidence")) if isinstance(data, dict) else 0.0

    return {
        "verdict": verdict,
        "category": category,
        "summary": summary,
        "claim": claim,
        "evidence": evidence,
        "sources": sources,
        "confidence": confidence,
        "notes": notes,
    }
