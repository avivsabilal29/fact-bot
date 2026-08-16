"""Phase 1 pipeline orchestrator: job -> verdict LLM -> markdown rendered.

MVP: caption diambil dari media_title (caption reel); media_url disimpan sebagai
konteks tambahan pada verdict sebelum dirender.
"""

import logging

from app.pipeline.llm import analyze_claim
from app.pipeline.renderer import render_markdown

logger = logging.getLogger(__name__)


async def run_analysis(job: dict) -> dict:
    """Jalankan analisa lengkap untuk satu job dict.

    job diharapkan memiliki kunci: media_title (caption untuk MVP), media_url,
    claim_text. Mengembalikan {"verdict": dict, "markdown": str}.
    Melempar LLMConfigError bila proxy LLM tidak dikonfigurasi (permanent error).
    """
    media_title = (job.get("media_title") or "").strip()
    media_url = (job.get("media_url") or "").strip()
    claim_text = (job.get("claim_text") or "").strip()

    if not claim_text:
        raise ValueError("job.claim_text kosong — tidak ada klaim untuk dianalisa.")

    # MVP: caption = media_title (caption/title reel).
    caption = media_title or "(tidak ada caption)"

    verdict = await analyze_claim(caption, claim_text)
    # Konteks tambahan untuk renderer (kunci non-skema, aman).
    verdict["context"] = caption
    if media_url:
        verdict["context"] = f"{caption}\n({media_url})"

    # Derive title from summary when LLM did not supply one.
    if not verdict.get("title"):
        summary = verdict.get("summary") or ""
        if len(summary) > 75:
            summary = summary[:75].rsplit(" ", 1)[0]
        verdict["title"] = f"Analysis: {summary}" if summary else ""

    markdown = await render_markdown(verdict)

    logger.info(
        "Analysis selesai: verdict=%s category=%s confidence=%s",
        verdict["verdict"], verdict["category"], verdict["confidence"],
    )
    return {"verdict": verdict, "markdown": markdown}
