# =============================================================================
# deploy.sh — Deploy FactBot di VPS (169.58.111.12)
# -----------------------------------------------------------------------------
# Dipanggil dari mana saja; biasanya di-orchestrate Hermes agent:
#   ssh root@169.58.111.12 'cd /opt/factbot && bash deploy.sh main'
# -----------------------------------------------------------------------------
# Alur: pull kode → tag image lama (utk rollback) → build+up (image per-commit)
#       → health gate (Docker healthcheck) → verify publik (nginx→bot) →
#       verify webhook Meta (GET subscribe) → (gagal?) rollback otomatis.
# =============================================================================
set -euo pipefail

APP_DIR="/opt/factbot"
BRANCH="${1:-main}"
PUBLIC_BASE="https://factbot.tech"
COMPOSE="docker compose"

cd "$APP_DIR"

log()  { echo -e "\033[1;34m[deploy]\033[0m $*"; }
fail() { echo -e "\033[1;31m[deploy][FAIL]\033[0m $*"; }

rollback() {
    fail "ROLLBACK ke image factbot/bot:prev ..."
    IMAGE_TAG=prev $COMPOSE up -d --no-build bot bot-worker || true
    exit 1
}

# 1. Ambil kode terbaru
log "Fetch & checkout $BRANCH"
git fetch --prune origin
git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" origin/"$BRANCH"
git pull --ff-only origin "$BRANCH"
COMMIT="$(git rev-parse --short HEAD)"
log "Commit: $COMMIT"

# 2. Simpan image lama utk rollback (jika ada)
docker tag factbot/bot:latest factbot/bot:prev 2>/dev/null || true

# 3. Build + up (image di-tag per commit; :latest baru diupdate setelah sukses)
log "Build image (tag $COMMIT) ..."
IMAGE_TAG="$COMMIT" $COMPOSE build bot
log "Up services ..."
IMAGE_TAG="$COMMIT" $COMPOSE up -d bot bot-worker
docker tag "factbot/bot:$COMMIT" factbot/bot:latest

# 4. Health gate via Docker healthcheck (30 x 2s = 60s)
log "Health gate ..."
status="starting"
for i in $(seq 1 30); do
    status="$(docker inspect -f '{{.State.Health.Status}}' factbot-bot-1 2>/dev/null || echo starting)"
    [ "$status" = "healthy" ] && break
    sleep 2
done
if [ "$status" != "healthy" ]; then
    fail "bot tidak sehat (status=$status). Log:"
    $COMPOSE logs --tail=40 bot || true
    rollback
fi
log "Bot healthy ✓"

# 5. Verify lewat publik (nginx → bot)
if ! curl -fsS -m 10 "$PUBLIC_BASE/health" >/dev/null 2>&1; then
    fail "public health gagal — cek nginx location /health di deploy/nginx-webhooks.conf"
    rollback
fi
log "Public /health OK ✓"

# 6. Verify webhook Meta (GET subscribe → harus balas challenge 'pong')
VERIFY_TOKEN="$(grep -E '^META_VERIFY_TOKEN=' .env | head -n1 | cut -d= -f2- | tr -d '"' || true)"
if [ -n "$VERIFY_TOKEN" ]; then
    challenge="$(curl -fsS -m 10 "$PUBLIC_BASE/webhooks/meta?hub.mode=subscribe&hub.verify_token=$VERIFY_TOKEN&hub.challenge=pong" || true)"
    if [ "$challenge" = "pong" ]; then
        log "Webhook verify OK ✓"
    else
        fail "webhook verify gagal (challenge=$challenge)"
        rollback
    fi
else
    log "⚠️ META_VERIFY_TOKEN kosong di .env — skip webhook verify"
fi

log "✅ Deploy $COMMIT selesai."
log "   Rollback manual: IMAGE_TAG=prev $COMPOSE up -d --no-build bot bot-worker"
