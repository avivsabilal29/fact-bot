#!/usr/bin/env python3
"""
funnel_watchdog.py — Hybrid watchdog untuk auto-recovery Tailscale Funnel (laptop 'parkee').

Masalah: funnel sering mati diam-diam saat jaringan drop (log tailscaled:
'magicsock: send error', 'Rebinding', 'connection terminated',
'no more connections') — URL publik jadi 000, webhook Meta berhenti masuk.
Config funnel biasanya MASIH ADA tapi serving-nya mati.

Solusi recovery yang terbukti:
    tailscale funnel reset
    tailscale funnel --bg 8001
(tunggu ~5 detik, URL hidup lagi).

Dua mekanisme deteksi (HYBRID):
  1. EVENT LISTENER (utama, real-time, no polling): stream
     `journalctl -f -u tailscaled -o cat`, picu VERIFY kalau baris log
     mengandung pattern: 'send error', 'rebinding', 'connection terminated',
     'no more connections', 'funnel', 'serve config'.
  2. HEARTBEAT safety net (cadangan): tiap 60 detik curl health check
     https://parkee.tail67f453.ts.net/health — kalau bukan '200' → picu VERIFY.

VERIFY + RECOVERY: saat terpicu → curl health check 1x; kalau 200 = false
alarm (log saja); kalau gagal → `tailscale funnel reset` (toleransi gagal),
`tailscale funnel --bg 8001`, tunggu 5 detik, verify ulang 1x, log hasil.

Guard anti-loop: setelah recovery, cooldown 30 detik sebelum recovery berikutnya.

Hanya stdlib (subprocess, threading, logging, time, re) — tanpa pip install.
Log ke <BASE>/logs/funnel_watchdog.log. Graceful shutdown via SIGTERM/SIGINT.
"""

import logging
import os
import re
import signal
import subprocess
import threading
import time

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "funnel_watchdog.log")

HEALTH_URL = "https://parkee.tail67f453.ts.net/health"
CURL_TIMEOUT = 10                 # detik per curl
HEARTBEAT_INTERVAL = 60           # detik antar heartbeat check
COOLDOWN_AFTER_RECOVERY = 30      # detik tunggu sebelum recovery berikutnya
POST_RECOVERY_WAIT = 5            # detik tunggu setelah funnel --bg sebelum verify ulang
RECOVERY_RETRY_INTERVAL = 10      # detik antar retry verify setelah recovery
RECOVERY_VERIFY_TIMEOUT = 75      # detik maksimum menunggu funnel hidup setelah recovery
JOURNALCTL_RESTART_DELAY = 3      # detik sebelum restart journalctl kalau keluar

JOURNALCTL_CMD = ["journalctl", "-f", "-u", "tailscaled", "-o", "cat"]
FUNNEL_RESET_CMD = ["tailscale", "funnel", "reset"]
FUNNEL_START_CMD = ["tailscale", "funnel", "--bg", "8001"]

# Pattern pemicu VERIFY (case-insensitive)
TRIGGER_PATTERNS = [
    r"send error",
    r"rebinding",
    r"connection terminated",
    r"no more connections",
    r"funnel",
    r"serve config",
]

logger = logging.getLogger("funnel_watchdog")


class FunnelWatchdog:
    """Hybrid watchdog: event listener (journalctl stream) + heartbeat safety net."""

    def __init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_recovery_ts = 0.0
        self._journal_proc = None
        self._pattern = re.compile(
            "|".join("(?:%s)" % p for p in TRIGGER_PATTERNS),
            re.IGNORECASE,
        )

    # -- util ----------------------------------------------------------------

    @staticmethod
    def _run(cmd, timeout=30):
        """Jalankan subprocess, kembalikan (rc, stdout, stderr)."""
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except subprocess.TimeoutExpired:
            logger.warning("Command timeout: %s", " ".join(cmd))
            return -1, "", "timeout"
        except FileNotFoundError:
            logger.error("Command tidak ditemukan: %s", cmd[0])
            return -1, "", "command not found"

    def _health_check(self):
        """curl health endpoint; return True kalau HTTP code 200."""
        cmd = [
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "--max-time", str(CURL_TIMEOUT), HEALTH_URL,
        ]
        rc, out, _err = self._run(cmd, timeout=CURL_TIMEOUT + 5)
        if rc != 0:
            logger.warning("Health check curl gagal rc=%s", rc)
            return False
        return out.strip() == "200"

    # -- verify & recovery ---------------------------------------------------

    def _verify_once(self, reason):
        """VERIFY: health check 1x. Return True kalau sehat (false alarm)."""
        logger.info("VERIFY triggered (reason=%s) — health check 1x ...", reason)
        ok = self._health_check()
        if ok:
            logger.info("VERIFY result: healthy (200) — false alarm, tanpa aksi.")
        else:
            logger.warning("VERIFY result: DOWN — health check gagal.")
        return ok

    def _recover(self):
        """Recovery: funnel reset -> funnel --bg 8001 -> tunggu -> verify ulang."""
        logger.warning("RECOVERY mulai: %s", " ".join(FUNNEL_RESET_CMD))
        rc, out, err = self._run(FUNNEL_RESET_CMD)
        if rc != 0:
            # toleransi gagal — tetap lanjut start ulang
            logger.warning("funnel reset rc=%s (ditoleransi) out=%r err=%r", rc, out, err)
        else:
            logger.info("funnel reset OK.")

        logger.info("Start funnel: %s", " ".join(FUNNEL_START_CMD))
        rc, out, err = self._run(FUNNEL_START_CMD)
        if rc != 0:
            logger.error("funnel start rc=%s out=%r err=%r", rc, out, err)
        else:
            logger.info("funnel start OK.")

        logger.info("Menunggu %ds sebelum verify ulang ...", POST_RECOVERY_WAIT)
        time.sleep(POST_RECOVERY_WAIT)

        # Retry verify: Tailscale butuh waktu re-propagate (cert/DERP).
        # Cek tiap 10s sampai maksimum RECOVERY_VERIFY_TIMEOUT detik.
        deadline = time.time() + RECOVERY_VERIFY_TIMEOUT
        while time.time() < deadline:
            if self._health_check():
                logger.info("RECOVERY berhasil: funnel hidup lagi (200).")
                return True
            logger.warning(
                "RECOVERY verify ulang gagal — retry dalam %ds (sisa %.0fs) ...",
                RECOVERY_RETRY_INTERVAL, deadline - time.time(),
            )
            time.sleep(RECOVERY_RETRY_INTERVAL)
        logger.error("RECOVERY belum berhasil dalam %ds: health check tetap gagal.", RECOVERY_VERIFY_TIMEOUT)
        return False

    def _maybe_recover(self, reason):
        """VERIFY + recovery dengan guard anti-loop (cooldown 30s)."""
        with self._lock:
            now = time.time()
            if now - self._last_recovery_ts < COOLDOWN_AFTER_RECOVERY:
                logger.info(
                    "Trigger (%s) di-skip: cooldown %ds aktif "
                    "(recovery terakhir %.0fs lalu).",
                    reason, COOLDOWN_AFTER_RECOVERY, now - self._last_recovery_ts,
                )
                return
            if self._verify_once(reason):
                return  # false alarm — sehat, tidak perlu recovery
            self._last_recovery_ts = time.time()  # mulai cooldown sebelum recovery
            self._recover()

    # -- event listener ------------------------------------------------------

    def _on_log_line(self, line):
        if not line:
            return
        if self._pattern.search(line):
            logger.info("Log pattern cocok: %r", line.strip()[:200])
            self._maybe_recover("log:" + line.strip()[:80])

    def _event_listener(self):
        """Stream journalctl -f -u tailscaled; picu VERIFY pada pattern cocok."""
        logger.info("Event listener mulai: %s", " ".join(JOURNALCTL_CMD))
        while not self._stop.is_set():
            try:
                proc = subprocess.Popen(
                    JOURNALCTL_CMD,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # gabung error ke stream biar terlihat
                    text=True,
                    bufsize=1,
                )
                self._journal_proc = proc
                stream = proc.stdout or []
                for line in stream:
                    if self._stop.is_set():
                        break
                    self._on_log_line(line)
                if proc.stdout:
                    proc.stdout.close()
                rc = proc.wait()
                if self._stop.is_set():
                    break
                logger.warning(
                    "journalctl keluar rc=%s — restart dalam %ds ...",
                    rc, JOURNALCTL_RESTART_DELAY,
                )
                time.sleep(JOURNALCTL_RESTART_DELAY)
            except FileNotFoundError:
                logger.error("journalctl tidak ditemukan — event listener berhenti.")
                return
            except Exception as exc:  # noqa: BLE001 — tetap hidup, retry
                logger.exception("Event listener error: %s", exc)
                if self._stop.is_set():
                    break
                time.sleep(JOURNALCTL_RESTART_DELAY)

    # -- heartbeat safety net -------------------------------------------------

    def _heartbeat(self):
        """Tiap 60s health check; kalau bukan 200 → picu VERIFY."""
        logger.info("Heartbeat mulai (interval %ds).", HEARTBEAT_INTERVAL)
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            logger.info("Heartbeat: health check berkala ...")
            if self._health_check():
                logger.info("Heartbeat: sehat (200).")
            else:
                logger.warning("Heartbeat: health check GAGAL — picu VERIFY.")
                self._maybe_recover("heartbeat")

    # -- main / shutdown -----------------------------------------------------

    def stop(self, signum=None, frame=None):
        """Graceful shutdown: stop loop + kill subprocess journalctl."""
        if signum is not None:
            logger.info("Signal %s diterima — shutdown ...", signum)
        self._stop.set()
        proc = self._journal_proc
        if proc and proc.poll() is None:
            logger.info("Menghentikan subprocess journalctl (pid=%s) ...", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def run(self):
        listener = threading.Thread(
            target=self._event_listener, name="event-listener", daemon=True
        )
        heartbeat = threading.Thread(
            target=self._heartbeat, name="heartbeat", daemon=True
        )
        listener.start()
        heartbeat.start()
        logger.info("funnel_watchdog berjalan (hybrid: event listener + heartbeat).")
        try:
            while not self._stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()
        logger.info("Watchdog berhenti bersih.")


def setup_logging():
    """Log ke logs/funnel_watchdog.log (INFO) + stderr untuk visibility."""
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)


def main():
    setup_logging()
    wd = FunnelWatchdog()
    signal.signal(signal.SIGTERM, wd.stop)
    signal.signal(signal.SIGINT, wd.stop)
    wd.run()


if __name__ == "__main__":
    main()
