# Webhook State Machine — Instagram DM (FactBot)

> Dokumen konsep (bukan implementasi). Sumber kebenaran perilaku: `app/webhooks/meta.py`.
> Alur ini diasumsikan berlaku per-`sender.id` (satu state terpisah untuk tiap pengirim).

## Konsep inti: `pending_claims` per-sender

- State utama adalah **satu slot pending per sender**: ketika user mengirim **media** (mis. `ig_reel`),
  handler menyimpan info media tersebut sebagai *pending claim* untuk sender itu.
- Hanya **satu** pending yang boleh hidup per sender pada satu waktu.
- Pending **bukan** antrian (queue) — ia **satu nilai** yang bisa ditimpa (*overwrite*) oleh media baru.

## Alur media → pending → text → klaim → clear

```
user kirim ig_reel        → media terdeteksi → simpan pending_claims[sender] = info reel
user kirim text berikutnya → ada pending? → YA → text diperlakukan sebagai KLAIM
                            → proses klaim → pending DIHAPUS (consume-once) → state bersih
```

- Media baru apa pun (reel berikutnya) **menimpa** pending lama sender yang sama.
- Satu klaim mengonsumsi satu pending. Setelah dikonsumsi, slot kosong kembali.

## Alur text tanpa pending → deny

```
user kirim text            → ada pending? → TIDAK → text BUKAN klaim → DENY (ditolak/diabaikan)
```

- Text biasa tanpa media sebelumnya **tidak pernah** jadi klaim.
- Ini mencegah text acak ("halo", "hai") diperlakukan sebagai klaim.

## Alur template → mention

- Attachment bertipe `template` (mis. quick-reply/generic dari button yang diklik user)
  **bukan** media klaim → ditangani sebagai **mention** / interaksi button,
  tidak mengisi pending dan tidak membuat klaim.

## Alur echo/read → skip

- Event non-message (read, seen, typing) dan **echo** (pesan yang dikirim BOT sendiri,
  biasanya ditandai `message.is_echo` / berasal dari page sendiri) → **skip** tanpa proses.
- Hanya pesan masuk asli dari user yang diproses.

## Pembersihan state: TANPA time-window / magic number

- **Tidak ada** timeout, TTL, atau magic number (mis. "pending hangus setelah N detik").
- State dibersihkan hanya lewat dua mekanisme:
  1. **Consume-once** — pending terhapus segera setelah satu klaim diproses.
  2. **Overwrite oleh media baru** — pending lama digantikan media terbaru.
- Konsekuensi: pending bertahan selama tidak ada text yang mengklaimnya dan tidak ada media baru.
  State per-sender bersifat *stateless terhadap waktu*; deterministik terhadap urutan pesan.

## Skenario E2E yang diuji (lihat `/tmp/e2e_payloads.json`, dikirim berurutan)

| # | Payload | Sender | Isi | Ekspektasi |
|---|---------|--------|-----|------------|
| A | text `"Halo"` | 1338129438379972 | text tanpa pending | DENY (bukan klaim) |
| B | `ig_reel` | 1338129438379972 | media | pending dibuat |
| C | text `"klaim: vaksin bikin magnet"` | 1338129438379972 | text + pending ada | **KLAIM** (pending dikonsumsi) |
| D | text `"halo lagi"` | 1338129438379972 | text, pending sudah habis | DENY |
| E | `template` | 1338129438379972 | template | MENTION (bukan klaim) |
| F | `ig_reel` | **999999999999** | media, sender beda | pending dibuat **untuk sender lain** — state sender pertama tidak terpengaruh |

Catatan: payload dikirim berurutan (A→F) dalam satu sesi server agar state per-sender
terbentuk seperti tabel di atas. Karena state in-memory, jalankan E2E terhadap server
yang baru di-restart agar ekspektasi deterministik.
