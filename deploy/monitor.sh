#!/usr/bin/env bash
# =============================================================================
# monitor.sh — health + log check FactBot.
# Jalankan DI VPS, atau dari laptop via Hermes cron:
#   ssh root@169.58.111.12 'bash /opt/factbot/deploy/monitor.sh'
# Exit code != 0 → ada masalah (Hermes bisa alert / auto-heal).
# =============================================================================
set -uo pipefail

cd /opt/factbot || exit 1

echo "== $(date -Is) =="
docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}' || exit 1

# Health publik (nginx → bot) — ini yang dilihat Meta & UptimeRobot
if ! curl -fsS -m 5 https://factbot.tech/health; then
    echo "FAIL: public /health tidak merespons"
    exit 1
fi
echo ""

# Status healthcheck Docker
for c in factbot-bot-1 factbot-bot-worker-1; do
    st="$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)"
    echo "$c: $st"
    [ "$st" = "healthy" ] || exit 1
done

# Ekor log (error/job terbaru)
docker compose logs --tail=25 bot bot-worker 2>&1 | tail -30 || true

# Statistik singkat
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' bot bot-worker 2>/dev/null || true
