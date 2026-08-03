#!/bin/sh
# FactBot entrypoint — jalan sebagai appuser (non-root)
set -e

# yt-dlp berubah hampir tiap minggu (situs media ganti struktur).
# Self-update best-effort: gagal (offline/lock) tidak menggagalkan start.
yt-dlp -U >/dev/null 2>&1 || true

exec "$@"
