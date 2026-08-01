# Pipeline Sinkronisasi TTD → Morbis

Empat tahap terpisah, tiap tahap resumable (bisa dihentikan & dilanjutkan).

```text
01_fetch_pegawai.py ──> output/pegawai.csv
02_build_mapping.py ──> output/mapping.csv   (nama → id_peg, status match/no_match/ambiguous/already)
03_upload_ttd.py   ──> output/upload_report.csv  (QR diambil dari TTD-Online, POST multipart)
04_verify_upload.py ─> output/verify_report.csv  (cek id_peg sudah punya TTD)
```

## Cara pakai

```bash
# 1. fetch daftar pegawai dari TTD-Online (725)
python3 scripts/ttd_sync/01_fetch_pegawai.py

# 2. build mapping nama -> id_peg (bisa dihentikan, lanjut resume)
python3 scripts/ttd_sync/02_build_mapping.py

# 3a. dry-run 5 pegawai dulu (ambil QR, siapkan payload, TANPA POST)
python3 scripts/ttd_sync/03_upload_ttd.py --dry-run 5

# 3b. upload bertahap: 1 → 5 → 20 → 100 → semua
python3 scripts/ttd_sync/03_upload_ttd.py --limit 1
python3 scripts/ttd_sync/03_upload_ttd.py --limit 5
python3 scripts/ttd_sync/03_upload_ttd.py --limit 20
python3 scripts/ttd_sync/03_upload_ttd.py

# 4. verifikasi: cek id_peg di Morbis sudah punya TTD
python3 scripts/ttd_sync/04_verify_upload.py
```

## Aturan keselamatan

1. **Jangan upload langsung 725** — selalu lewati dry-run → 1 → 5 → 20 → 100.
2. **Mapping dulu, upload kemudian** — kesalahan mapping = TTD masuk ke pegawai
   salah, tidak bisa diperbaiki otomatis. Pegawai `no_match` / `ambiguous`
   TIDAK di-upload — wajib ditinjau manual.
3. `--limit` membatasi jumlah upload per run; sisanya resume lain waktu.
4. Jeda antar-request default 0.5s (anti-spam). Naikkan via `--delay` bila perlu.
5. QR PNG disalin ke `output/qr/{pid}_*.png` sebelum upload — arsip lokal.

## File hasil

| File | Isi |
|------|-----|
| `output/pegawai.csv` | pid, nama, nama_display, gelar, method, status |
| `output/mapping.csv` | pid, nama_display, gelar, id_peg, id_peg_nama, match_type, status |
| `output/upload_report.csv` | pid, nama_display, id_peg, status(OK/FAIL/DRYRUN), msg, ts |
| `output/verify_report.csv` | pid, nama_display, id_peg, has_ttd, ttd_path, ok |
| `output/qr/` | salinan QR PNG tiap pegawai yang diproses |

## Catatan

- Kredensial & override mapping di `scripts/ttd_sync/lib_morbis.py`
  (JANGAN di-commit dengan password produksi ke repo publik).
- `mapping_state.json` & `upload_report.csv` berisi data internal — sebaiknya
  di-gitignore.
