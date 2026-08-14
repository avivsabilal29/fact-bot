"""FactBot public API client — upload laporan fakta & dapatkan public URL.

Kontrak API (https://factbot.tech/api/v1/reports):
  * 201          → report baru dibuat, body JSON: {"url": "https://..."}
  * 409          → report_id sudah ada (idempotent) — BUKAN error:
                   GET /api/v1/reports/{report_id} → reuse {url} lama.
  * 413 / 429    → body bisa non-JSON (proxy) — JANGAN di-parse,
                   raise UploadTransientError (retryable).
  * 401 / 403    → konfigurasi salah (FACTBOT_API_KEY) — UploadConfigError.
  * 5xx / timeout→ UploadTransientError (retryable).

Idempotency: report_id = "{platform}_{media_id}" — upload ulang job yang sama
selalu menghasilkan public_url yang sama (201 baru atau reuse 409).
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class UploadTransientError(Exception):
    """Error sementara (413/429/5xx/timeout) → retry dengan backoff."""


class UploadConfigError(Exception):
    """Error konfigurasi permanen (401/403/key kosong) → perbaiki, jangan retry."""


async def create_report(
    verdict: dict,
    markdown: str,
    report_id: str,
    platform: str,
    media_url: str,
    name: str,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Upload report ke FactBot API, return public_url (str).

    `client` opsional — dipakai tes hermetic (MockTransport); kalau None,
    dibuat client sendiri dengan timeout dari Settings.
    """
    api_key = settings.factbot_api_key
    if not api_key:
        raise UploadConfigError(
            "FACTBOT_API_KEY kosong — set di .env (lihat app/config.py)"
        )

    title = str(verdict.get("title") or "").strip()
    if not title:
        claim = str(verdict.get("claim") or "")
        title = f"Analisa: {claim[:60]}"

    body = {
        "id": report_id,
        "title": title,
        "name": name,
        "platform": platform,
        "verdict": verdict.get("verdict"),
        "category": verdict.get("category"),
        "summary": (lambda s: s[:297] + "..." if s and len(s) > 300 else s)(verdict.get("summary")),
        "claim": verdict.get("claim"),
        "source_url": media_url,
        "content": markdown,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    url = settings.factbot_api_url

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(settings.factbot_timeout))
    try:
        resp = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as e:
        raise UploadTransientError(f"upload timeout ke {url}: {e}") from e
    except httpx.HTTPError as e:
        raise UploadTransientError(f"network error ke {url}: {e}") from e

    # --- 201: report baru → {url} ---
    if resp.status_code == 201:
        try:
            data = resp.json()
        except ValueError as e:
            raise UploadTransientError(
                f"201 tapi body bukan JSON (len={len(resp.content)}): {e}"
            ) from e
        public_url = (data or {}).get("url")
        if not public_url:
            raise UploadTransientError(
                f"201 tapi field 'url' tidak ada di response: {data!r}"
            )
        logger.info("report %s dibuat → %s", report_id, public_url)
        return public_url

    # --- 409: idempotent — reuse URL report lama (bukan error) ---
    if resp.status_code == 409:
        get_url = _report_get_url(report_id)
        logger.info("409 untuk %s — reuse report lama via GET %s", report_id, get_url)
        try:
            old = await client.get(get_url, headers=headers)
        except httpx.TimeoutException as e:
            raise UploadTransientError(f"GET lama timeout ({get_url}): {e}") from e
        except httpx.HTTPError as e:
            raise UploadTransientError(f"GET lama gagal ({get_url}): {e}") from e
        if old.status_code == 401 or old.status_code == 403:
            raise UploadConfigError(
                f"FACTBOT_API_KEY ditolak saat GET report lama "
                f"(HTTP {old.status_code}) — cek FACTBOT_API_KEY di .env"
            )
        if old.status_code >= 500:
            raise UploadTransientError(
                f"GET report lama 5xx ({old.status_code}) — retryable"
            )
        try:
            old_data = old.json()
        except ValueError:
            old_data = {}
        public_url = (old_data or {}).get("url")
        if not public_url:
            raise UploadTransientError(
                f"409 + GET lama tanpa field 'url' (HTTP {old.status_code})"
            )
        logger.info("report %s reuse → %s", report_id, public_url)
        return public_url

    # --- 413 / 429: jangan parse body (proxy bisa kirim non-JSON) ---
    if resp.status_code in (413, 429):
        raise UploadTransientError(
            f"upload report {report_id} ditolak HTTP {resp.status_code} "
            f"(rate-limit/terlalu besar) — retryable"
        )

    # --- 401 / 403: konfigurasi salah → jelas, jangan retry ---
    if resp.status_code in (401, 403):
        raise UploadConfigError(
            f"FACTBOT_API_KEY ditolak (HTTP {resp.status_code}) — "
            f"cek FACTBOT_API_KEY di .env (url={url})"
        )

    # --- 5xx: retryable ---
    if resp.status_code >= 500:
        raise UploadTransientError(
            f"upload report {report_id} gagal HTTP {resp.status_code} — retryable"
        )

    # --- 4xx lainnya: kontrak tak dikenal → permanen ---
    raise UploadConfigError(
        f"upload report {report_id} ditolak HTTP {resp.status_code}: "
        f"{resp.text[:200]!r}"
    )


def _report_get_url(report_id: str) -> str:
    """URL GET satu report: {base}/reports/{report_id}.

    `base` diturunkan dari FACTBOT_API_URL dengan memotong '/reports' —
    tetap benar baik url berakhiran '/reports' maupun root API.
    """
    base = settings.factbot_api_url.rsplit("/reports", 1)[0]
    return f"{base}/reports/{report_id}"
