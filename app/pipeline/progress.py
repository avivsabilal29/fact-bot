"""ProgressNotifier — kirim update progress DM bertahap (anti-senyap, anti-spam).

Analisa klaim DM bisa makan waktu 30-60s; user tidak boleh dibiarkan tanpa
info (stuck). Notifier ini mengirim update bertahap dengan batasan ketat:

  - maksimal 2 pesan progress per job  (anti-spam, ``_max_sends``),
  - 1 pesan per phase                  (dedup set),
  - minimal interval antar kiriman     (cegah ngejar-ngejar),
  - kegagalan kirim TIDAK mematikan job (graceful degradation): semua
    exception di-swallow, return False.
"""

import logging
import time

logger = logging.getLogger(__name__)


class ProgressNotifier:
    """Kirim update progress DM ke user, anti-spam dan anti-crash.

    ``send_fn(sender_id, text)`` adalah callable async yang dipasang saat
    wiring (default None). Kalau None, kiriman di-skip dengan warning —
    tidak pernah crash. ``enabled`` / ``min_interval`` / ``slow_after``
    fallback ke settings ``progress_*`` kalau tidak di-override.
    """

    _max_sends = 2  # batas keras anti-spam: maksimal 2 progress per job

    def __init__(
        self,
        sender_id: str,
        job_id: str,
        send_fn=None,
        *,
        enabled: bool | None = None,
        min_interval: float | None = None,
        slow_after: float | None = None,
    ):
        # Lazy import supaya modul ini tetap ringan / hermetic-test friendly.
        from app.config import settings

        self.sender_id = sender_id
        self.job_id = job_id
        self.send_fn = send_fn
        self._enabled = settings.progress_enabled if enabled is None else enabled
        self._min_interval = (
            settings.progress_min_interval_seconds
            if min_interval is None
            else min_interval
        )
        self._slow_after = (
            settings.progress_slow_after_seconds if slow_after is None else slow_after
        )
        self._sent_phases: set[str] = set()
        self._last_sent_ts: float | None = None
        self._sent_count = 0
        self._started_ts = time.monotonic()

    @property
    def sent_count(self) -> int:
        """Jumlah pesan progress yang sudah berhasil terkirim."""
        return self._sent_count

    async def send(self, phase: str, text: str, *, force: bool = False) -> bool:
        """Kirim progress untuk ``phase`` tertentu.

        Syarat: (a) enabled, (b) phase belum pernah dikirim (dedup),
        (c) belum lewat batas anti-spam, (d) jarak dari kiriman terakhir
        >= min_interval ATAU belum pernah kirim sama sekali, KECUALI
        ``force=True`` (pesan penting seperti retry/error — selalu lolos
        gate interval, tapi tetap respect anti-spam max 2 + dedup).
        Return True kalau terkirim, False kalau di-skip / gagal.
        """
        if not self._enabled:
            return False
        if self._sent_count >= self._max_sends:
            return False
        if phase in self._sent_phases:
            return False
        now = time.monotonic()
        if not force:
            if self._last_sent_ts is not None and (now - self._last_sent_ts) < self._min_interval:
                return False
        return await self._deliver(phase, text)

    async def send_if_slow(self, phase: str, text: str, slow_after: float | None = None) -> bool:
        """Kirim progress kalau job sudah berjalan > ``slow_after`` detik.

        Path terpisah dari :meth:`send`: tidak kena gate min_interval —
        cukup enabled + slow_after + batas anti-spam (sent_count < 2).
        Cocok dipanggil periodik dari pipeline yang lama.
        """
        if not self._enabled:
            return False
        if self._sent_count >= self._max_sends:
            return False
        threshold = self._slow_after if slow_after is None else slow_after
        if (time.monotonic() - self._started_ts) <= threshold:
            return False
        return await self._deliver(phase, text)

    async def _deliver(self, phase: str, text: str) -> bool:
        if self.send_fn is None:
            logger.warning(
                "ProgressNotifier(job=%s): send_fn None, progress '%s' di-skip",
                self.job_id,
                phase,
            )
            return False
        try:
            await self.send_fn(self.sender_id, text)
        except Exception:
            # Progress GAGAL tidak boleh mematikan job (graceful degradation).
            logger.exception(
                "ProgressNotifier(job=%s): kirim progress '%s' gagal", self.job_id, phase
            )
            return False
        self._sent_phases.add(phase)
        self._last_sent_ts = time.monotonic()
        self._sent_count += 1
        return True
