#!/usr/bin/env bash
# =============================================================================
# start_local.sh — Startup lokal bot FastAPI (FactBot) + cloudflared tunnel
# -----------------------------------------------------------------------------
# Fungsi:
#   1. Cek & aktivasi venv (~/fact_bot_server/venv)
#   2. Cek port 8001 — kalau dipakai uvicorn lama, kill aman; kalau proses
#      lain, report error dan berhenti
#   3. Start uvicorn app.main:app di background, log ke logs/uvicorn.log
#   4. Health check berulang (curl localhost:8001/health, timeout 30 detik)
#   5. Start cloudflared tunnel --url http://localhost:8001 di background,
#      log ke logs/tunnel.log
#   6. Extract URL trycloudflare dari log & tampilkan besar-besar
#   7. Trap EXIT: cleanup bersih (kill uvicorn + cloudflared)
#   8. Retry otomatis 3x (jeda 5 detik) kalau health check gagal
#
# Penggunaan:
#   ./start_local.sh          # start server + tunnel
#   Ctrl+C                    # stop + cleanup otomatis
# =============================================================================

set -u          # error kalau ada variabel belum di-set
set -o pipefail # pipeline dianggap gagal kalau salah satu command gagal

BASE_DIR="$HOME/fact_bot_server"
VENV_DIR="$BASE_DIR/venv"
LOG_DIR="$BASE_DIR/logs"
UVICORN_LOG="$LOG_DIR/uvicorn.log"
TUNNEL_LOG="$LOG_DIR/tunnel.log"
PORT=8001
HEALTH_URL="http://127.0.0.1:${PORT}/health"
HEALTH_TIMEOUT=30   # detik, batas tunggu health check
MAX_RETRY=3         # jumlah percobaan start uvicorn
RETRY_DELAY=5       # jeda antar percobaan (detik)
TUNNEL_URL_WAIT=45  # batas tunggu URL trycloudflare muncul di log (detik)

UVICORN_PID=""
TUNNEL_PID=""
TUNNEL_URL=""

# Warna ANSI (fallback kosong kalau bukan TTY biar tidak berantakan)
if [ -t 1 ]; then
    C_GREEN=$'\033[1;32m'
    C_CYAN=$'\033[1;36m'
    C_YELLOW=$'\033[1;33m'
    C_RED=$'\033[1;31m'
    C_BOLD=$'\033[1m'
    C_RESET=$'\033[0m'
else
    C_GREEN=""; C_CYAN=""; C_YELLOW=""; C_RED=""; C_BOLD=""; C_RESET=""
fi

log_info()  { echo "${C_CYAN}[INFO]${C_RESET} $*"; }
log_ok()    { echo "${C_GREEN}[OK]${C_RESET} $*"; }
log_warn()  { echo "${C_YELLOW}[WARN]${C_RESET} $*"; }
log_err()   { echo "${C_RED}[ERROR]${C_RESET} $*"; }

# -----------------------------------------------------------------------------
# 7. Cleanup: matikan uvicorn + cloudflared (TERM dulu, KILL kalau bandel)
#    Dipanggil otomatis via trap EXIT — termasuk saat Ctrl+C / error.
# -----------------------------------------------------------------------------
cleanup() {
    local exit_code=$?
    echo ""
    log_warn "Cleanup: menghentikan proses..."
    for pid in "$UVICORN_PID" "$TUNNEL_PID"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    sleep 2
    for pid in "$UVICORN_PID" "$TUNNEL_PID"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            log_warn "PID $pid masih hidup — paksa kill (-9)."
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    log_ok "Semua proses dihentikan. Sampai jumpa!"
    exit "$exit_code"
}
trap cleanup EXIT

# -----------------------------------------------------------------------------
# 1. Cek & aktivasi venv
# -----------------------------------------------------------------------------
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    log_err "venv tidak ditemukan di $VENV_DIR"
    log_err "Buat dulu:  python3 -m venv $VENV_DIR"
    log_err "Lalu:       $VENV_DIR/bin/pip install -r requirements.txt"
    exit 1
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log_ok "venv diaktifkan: $(python --version 2>&1)"

# cek cloudflared tersedia di PATH
if ! command -v cloudflared >/dev/null 2>&1; then
    log_err "cloudflared tidak ditemukan di PATH."
    log_err "Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    exit 1
fi
log_ok "cloudflared: $(cloudflared --version 2>&1 | head -n1)"

mkdir -p "$LOG_DIR"

# -----------------------------------------------------------------------------
# 2. Cek port 8001 — return 0 kalau DIPAKAI, 1 kalau BEBAS
# -----------------------------------------------------------------------------
port_in_use() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | grep -q "[:.]${PORT} "
    elif command -v lsof >/dev/null 2>&1; then
        lsof -i ":$PORT" >/dev/null 2>&1
    else
        # fallback: coba bind port pakai Python (exit 0 = dipakai, 1 = bebas)
        python - "$PORT" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
    sys.exit(1)  # bind sukses => port bebas
except OSError:
    sys.exit(0)  # bind gagal => port dipakai
finally:
    s.close()
PY
    fi
}

if port_in_use; then
    log_warn "Port $PORT sudah dipakai."
    # cari proses uvicorn lama milik bot ini
    # catatan: pola "[u]vicorn" (bracket trick) mencegah pgrep match dirinya sendiri;
    # grep -vw $$/$PPID mencegah script membunuh shell sendiri / parent-nya
    OLD_PIDS=$(pgrep -f "[u]vicorn app\.main:app" | grep -vw -e "$$" -e "$PPID" || true)
    if [ -n "$OLD_PIDS" ]; then
        log_warn "Menemukan uvicorn lama (PID: $OLD_PIDS) — menghentikan secara aman..."
        kill $OLD_PIDS 2>/dev/null || true
        sleep 3
        for pid in $OLD_PIDS; do
            if kill -0 "$pid" 2>/dev/null; then
                log_warn "PID $pid masih hidup — paksa kill (-9)."
                kill -9 "$pid" 2>/dev/null || true
            fi
        done
        sleep 1
        if port_in_use; then
            log_err "Port $PORT masih dipakai setelah kill. Cek manual: ss -ltnp | grep :$PORT"
            exit 1
        fi
        log_ok "Port $PORT sudah bebas."
    else
        log_err "Port $PORT dipakai proses lain (bukan uvicorn bot ini)."
        log_err "Cek manual:  ss -ltnp | grep :$PORT"
        log_err "Hentikan proses itu dulu, lalu jalankan ulang script."
        exit 1
    fi
else
    log_ok "Port $PORT bebas."
fi

# -----------------------------------------------------------------------------
# 3. Start uvicorn di background (log ke logs/uvicorn.log)
# -----------------------------------------------------------------------------
start_uvicorn() {
    : > "$UVICORN_LOG"   # bersihkan log lama biar mudah dibaca
    (cd "$BASE_DIR" && exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" >> "$UVICORN_LOG" 2>&1) &
    UVICORN_PID=$!
    log_info "uvicorn started (PID $UVICORN_PID), log: $UVICORN_LOG"
}

# -----------------------------------------------------------------------------
# 4. Health check berulang sampai siap (timeout 30 detik)
# -----------------------------------------------------------------------------
wait_for_health() {
    local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if curl -fsS -m 5 "$HEALTH_URL" >/dev/null 2>&1; then
            return 0
        fi
        if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
            log_warn "Proses uvicorn (PID $UVICORN_PID) mati sebelum health check sukses."
            return 1
        fi
        sleep 2
    done
    return 1
}

# -----------------------------------------------------------------------------
# 8. Start uvicorn + retry otomatis 3x (jeda 5 detik) kalau health check gagal
# -----------------------------------------------------------------------------
start_uvicorn_with_retry() {
    local attempt
    for attempt in $(seq 1 "$MAX_RETRY"); do
        log_info "Percobaan $attempt/$MAX_RETRY: start uvicorn..."
        start_uvicorn
        if wait_for_health; then
            log_ok "Health check sukses! Bot FastAPI siap di http://localhost:$PORT"
            return 0
        fi
        log_err "Health check gagal (percobaan $attempt/$MAX_RETRY)."
        tail -n 20 "$UVICORN_LOG" 2>/dev/null | sed 's/^/    | /'
        if [ "$attempt" -lt "$MAX_RETRY" ]; then
            log_warn "Restart dalam $RETRY_DELAY detik..."
            sleep "$RETRY_DELAY"
        fi
    done
    log_err "Gagal setelah $MAX_RETRY percobaan. Cek log: $UVICORN_LOG"
    return 1
}

# -----------------------------------------------------------------------------
# 5. Start cloudflared tunnel di background (log ke logs/tunnel.log)
# -----------------------------------------------------------------------------
start_tunnel() {
    : > "$TUNNEL_LOG"   # penting: bersihkan log lama biar tidak ketemu URL basi
    cloudflared tunnel --url "http://localhost:$PORT" >> "$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!
    log_info "cloudflared started (PID $TUNNEL_PID), log: $TUNNEL_LOG"
}

# -----------------------------------------------------------------------------
# 6. Tunggu & extract URL trycloudflare dari log tunnel
# -----------------------------------------------------------------------------
wait_for_tunnel_url() {
    local i url=""
    for i in $(seq 1 "$TUNNEL_URL_WAIT"); do
        url=$(grep -oE 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -n1)
        if [ -n "$url" ]; then
            TUNNEL_URL="$url"
            return 0
        fi
        if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
            log_err "cloudflared mati sebelum dapat URL tunnel."
            tail -n 20 "$TUNNEL_LOG" 2>/dev/null | sed 's/^/    | /'
            return 1
        fi
        sleep 1
    done
    return 1
}

# Tampilkan URL tunnel besar-besar
print_big_url() {
    local url="$1"
    echo ""
    echo "${C_GREEN}══════════════════════════════════════════════════════════════════════${C_RESET}"
    echo "${C_GREEN}${C_BOLD}                                                                  ${C_RESET}"
    echo "${C_GREEN}${C_BOLD}   🌐  TUNNEL PUBLIC URL (untuk webhook Meta/WhatsApp API):        ${C_RESET}"
    echo "${C_GREEN}${C_BOLD}                                                                  ${C_RESET}"
    echo "${C_YELLOW}${C_BOLD}   ${url}${C_RESET}"
    echo "${C_GREEN}${C_BOLD}                                                                  ${C_RESET}"
    echo "${C_GREEN}${C_BOLD}   📌  Set WEBHOOK di Meta App Dashboard pakai URL di atas,        ${C_RESET}"
    echo "${C_GREEN}${C_BOLD}       lalu tambahkan path endpoint webhook (mis. ${url}/webhook)  ${C_RESET}"
    echo "${C_GREEN}${C_BOLD}   ⚠️   URL trycloudflare BERUBAH setiap restart script ini!        ${C_RESET}"
    echo "${C_GREEN}${C_BOLD}                                                                  ${C_RESET}"
    echo "${C_GREEN}══════════════════════════════════════════════════════════════════════${C_RESET}"
    echo ""
}

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
echo ""
log_info "======================================================"
log_info " Start lokal bot FastAPI (FactBot) + cloudflared"
log_info " Base dir : $BASE_DIR"
log_info " Port     : $PORT"
log_info "======================================================"
echo ""

start_uvicorn_with_retry || exit 1

start_tunnel

if wait_for_tunnel_url; then
    print_big_url "$TUNNEL_URL"
    log_ok "Semua berjalan! Tekan Ctrl+C untuk stop (cleanup otomatis)."
    echo ""
    # Pantau kedua proses; kalau salah satu mati, exit (trap cleanup yang bersihkan)
    while kill -0 "$UVICORN_PID" 2>/dev/null && kill -0 "$TUNNEL_PID" 2>/dev/null; do
        sleep 5
    done
    if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
        log_err "Proses uvicorn berhenti sendiri. Cek log: $UVICORN_LOG"
    fi
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
        log_err "Proses cloudflared berhenti sendiri. Cek log: $TUNNEL_LOG"
    fi
    exit 1
else
    log_err "Gagal mendapatkan URL tunnel dalam $TUNNEL_URL_WAIT detik. Cek log: $TUNNEL_LOG"
    exit 1
fi
