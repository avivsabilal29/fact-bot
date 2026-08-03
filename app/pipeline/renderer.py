"""Markdown rendering: verdict dict -> laporan analisa FactBot yang mudah dibaca.

Template mengikuti docs/test_prabowo_nuklir.md: verdict box, klaim yang dianalisa,
bukti, tabel Klaim vs Fakta, sumber rujukan, catatan, dan disclaimer.
"""

import logging

logger = logging.getLogger(__name__)

VERDICT_LABELS = {
    "fact": "✅ FAKTA",
    "hoax": "❌ HOAX",
    "partly_true": "⚠️ SEBAGIAN BENAR",
    "unverified": "❔ BELUM DAPAT DIVERIFIKASI",
}

CATEGORY_LABELS = {
    "health": "Kesehatan",
    "government": "Pemerintahan",
    "politics": "Politik",
    "disaster": "Bencana",
    "finance": "Keuangan",
    "technology": "Teknologi",
    "religion": "Agama",
    "education": "Pendidikan",
    "other": "Lainnya",
}

DISCLAIMER = (
    "*Dokumen ini dihasilkan otomatis oleh FactBot. "
    "Selalu cek sumber resmi sebelum menyebarkan informasi.*"
)


def _table_cell(text: str) -> str:
    """Sanitasi teks untuk sel tabel markdown (pipa & newline)."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _quote(text: str) -> str:
    """Bungkus teks sebagai blockquote markdown, pertahankan baris baru."""
    if not text:
        return "> —"
    return "\n".join(f"> {line}" for line in str(text).splitlines())


async def render_markdown(verdict: dict) -> str:
    """Render dict verdict tervalidasi menjadi dokumen markdown lengkap."""
    verdict_value = verdict.get("verdict", "unverified")
    label = VERDICT_LABELS.get(verdict_value, VERDICT_LABELS["unverified"])
    category = CATEGORY_LABELS.get(verdict.get("category", "other"), "Lainnya")
    claim = (verdict.get("claim") or "").strip() or "(klaim tidak tersedia)"
    context = (verdict.get("context") or "").strip()
    summary = (verdict.get("summary") or "").strip() or "Belum ada ringkasan."

    try:
        confidence_pct = f"{max(0.0, min(1.0, float(verdict.get('confidence', 0.0)))) * 100:.0f}%"
    except (TypeError, ValueError):
        confidence_pct = "—"

    evidence = [str(e).strip() for e in (verdict.get("evidence") or []) if str(e).strip()]
    sources = [str(s).strip() for s in (verdict.get("sources") or []) if str(s).strip()]
    notes = [str(n).strip() for n in (verdict.get("notes") or []) if str(n).strip()]

    title = claim if len(claim) <= 80 else claim[:77] + "..."

    lines = [
        f"# Hasil Analisa: {title}",
        "",
        f"> **Kesimpulan: {label}** — {summary}",
        "",
        "---",
        "",
        "## Klaim yang Dianalisa",
        "",
        _quote(claim),
        "",
    ]
    if context:
        lines.append(f"- **Konteks (caption/title):** {_table_cell(context)}")
    lines += [
        f"- **Kategori:** {category}",
        f"- **Tingkat keyakinan:** {confidence_pct}",
        "",
        "## Bukti",
        "",
    ]
    if evidence:
        lines += [f"- {item}" for item in evidence]
    else:
        lines.append("- Belum ada bukti yang cukup untuk memverifikasi klaim ini.")
    lines += [
        "",
        "## Klaim vs Fakta",
        "",
        "| Klaim | Fakta | Status |",
        "|---|---|---|",
    ]
    if evidence:
        lines += [f"| {_table_cell(claim)} | {_table_cell(item)} | {label} |" for item in evidence]
    else:
        lines.append(f"| {_table_cell(claim)} | Belum dapat diverifikasi | {label} |")
    lines += [
        "",
        "## Sumber Rujukan",
        "",
    ]
    if sources:
        lines += [f"{i}. {src}" for i, src in enumerate(sources, 1)]
    else:
        lines.append("- Belum ada sumber rujukan.")
    lines += [
        "",
        "## Catatan",
        "",
    ]
    if notes:
        lines += [f"- {note}" for note in notes]
    else:
        lines.append("- Tidak ada catatan tambahan.")
    lines += [
        "",
        "---",
        "",
        DISCLAIMER,
        "",
    ]

    return "\n".join(lines)
