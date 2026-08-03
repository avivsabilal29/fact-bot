# =============================================================================
# FactBot — production image (VPS 169.58.111.12)
# -----------------------------------------------------------------------------
# - python:3.12-slim + system deps utk pipeline video:
#     ffmpeg          : ekstrak audio dari video (yt-dlp output → wav 16kHz)
#     libgomp1        : faster-whisper / ctranslate2 butuh OpenMP runtime
#     tesseract-ocr   : OCR overlay teks video (stretch goal pipeline)
#     curl            : healthcheck
#     git             : huggingface_hub / yt-dlp fallback
# - yt-dlp di-install ke ~/.local (user appuser) supaya bisa self-update
#   (yt-dlp -U) di entrypoint tanpa hak root — situs media sering berubah.
# - Aplikasi jalan NON-root (user appuser, UID 1001).
# =============================================================================
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/home/appuser/.cache/huggingface \
    PATH="/home/appuser/.local/bin:$PATH"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgomp1 \
        tesseract-ocr \
        tesseract-ocr-ind \
        curl \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# User dibuat DULU (sebelum COPY --chown) supaya --chown valid.
RUN useradd -m -u 1001 appuser

# Dependencies dulu (layer cache — rebuild cepat saat kode berubah).
# --chown PENTING: file repo ada yg mode 600 → tanpanya appuser tak bisa baca.
COPY --chown=appuser:appuser requirements.txt ./

USER appuser
RUN pip install --user --no-cache-dir -r requirements.txt

# Entrypoint: self-update yt-dlp (best-effort) lalu exec CMD
COPY --chown=appuser:appuser docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Kode aplikasi
COPY --chown=appuser:appuser . .

# Direktori runtime (data job, reports, log) + cache model whisper.
# Butuh root: /app milik root. Volume bernama mewarisi ownership image saat
# mount pertama → HF cache harus milik appuser supaya model bisa di-download.
USER root
RUN mkdir -p /app/data /app/logs /app/reports \
    && mkdir -p /home/appuser/.cache/huggingface \
    && chown -R appuser:appuser /app /home/appuser

USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8001/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
# workers=1 PENTING: state in-memory (pending_claims, dedup set) di meta.py
# — lebih dari 1 worker memecah state antar proses.
# --proxy-headers: percaya X-Forwarded-* dari nginx (client IP asli di log).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
