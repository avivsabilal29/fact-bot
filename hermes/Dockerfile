# =============================================================================
# Hermes Brain — Dockerfile (profile factbot untuk FactBot)
# -----------------------------------------------------------------------------
# Membuat image Hermes headless yang menjalankan profile 'factbot' sebagai
# "brain layer" bot: persona (SOUL.md), memory knowledge, dan nanti orchestrator
# sub-agents. Instalasi via installer resmi Hermes.
#
# Mode: gateway (webhook listener) — bot FastAPI bisa memanggil via HTTP.
# Profile di-mount dari ./hermes-profiles/factbot (lihat docker-compose.yml).
# =============================================================================
FROM python:3.12-slim

# --- System deps minimal (curl utk installer + healthcheck; xz utk extract Node tarball) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git xz-utils \
    && rm -rf /var/lib/apt/lists/*

# --- Install Hermes via installer resmi (di-commit lokal — domain host
#     hermes-agent.nousresearch.com di-DNS-salah oleh WiFi laptop/ISP, jadi
#     installer di-COPY dari repo bukan di-curl. Semua download aktual
#     (uv, github repo, nodejs, pypi) pakai domain normal.) ---
# Pre-seed uv via pip (PyPI reachable; astral.sh SSL gagal dari laptop ini):
# installer cek $HERMES_HOME/bin/uv → skip download astral.sh (baris 562 install.sh).
COPY install.sh /tmp/hermes-install.sh
RUN pip install --no-cache-dir uv \
    && mkdir -p /hermes-data/bin \
    && cp "$(command -v uv)" /hermes-data/bin/uv \
    && bash /tmp/hermes-install.sh && rm /tmp/hermes-install.sh

# Hermes binary: root install pakai FHS layout → /usr/local/bin/hermes
# (log installer: "Root install on Linux — using FHS layout")
ENV PATH="/root/.local/bin:/usr/local/bin:${PATH}"

# --- Layout data Hermes ---
# HERMES_HOME = root tempat profile/memory/skills/sessions disimpan.
# Profile 'factbot' di-mount (read-only) ke <HERMES_HOME>/profiles/factbot.
ENV HERMES_HOME=/hermes-data
ENV HERMES_PROFILE=factbot
RUN mkdir -p /hermes-data && chmod 755 /hermes-data

# --- Healthcheck (hermes chat one-shot? gunakan proses check sederhana) ---
# Hermes tidak punya /health — healthcheck di compose memakai `hermes --version`.

WORKDIR /hermes-data
EXPOSE 8644

# Mode default: gateway (webhook listener port 8644).
# Alternatif: hermes chat -q "..." (one-shot).
CMD ["hermes", "gateway", "run"]
