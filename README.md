# TTD-OK — Ekstraktor Tanda Tangan dari Tabel Dokumen

Pipeline otomatis untuk mengekstrak tanda tangan pegawai dari tabel berbingkai pada dokumen scan (JPEG/PNG), menghasilkan **PNG transparan siap tempel** ke Word/surat, lengkap dengan metadata per pegawai.

Dibangun dengan **Python + OpenCV + Tesseract OCR** di dalam Docker.

```
input/contoh_ttd.jpg
        │
        ▼
┌─────────────────────────────┐
│  Deteksi garis tabel        │
│  (morphological + proyeksi) │
├─────────────────────────────┤
│  Grid 15×6 → 2 blok         │
│  (No | Nama | TTD) × 2      │
├─────────────────────────────┤
│  Skip baris header          │
│  + baris gabungan (ANASTESI)│
├─────────────────────────────┤
│  OCR nama (tesseract-ind)   │
├─────────────────────────────┤
│  Crop tanda tangan presisi  │
│  (bounding box tinta)       │
├─────────────────────────────┤
│  Background → alpha channel │
│  → PNG transparan           │
└─────────────────────────────┘
        │
        ▼
output/
├── 01_Ali_Akbar_S_Kep/
│   ├── signature.png      # PNG transparan (tanda tangan)
│   ├── profile.png        # nama + tanda tangan (verifikasi)
│   └── metadata.json      # no, nama, gelar, sumber
├── 02_Bambang_Putra_S_Kep/
└── ... (hingga 28 pegawai)
```

---

## Fitur

- ✅ **Deteksi grid otomatis** — garis horizontal/vertikal via morphological opening + projection profile
- ✅ **Dua blok tabel** (kiri/kanan) dideteksi dan diproses terpisah — 2 pegawai per baris
- ✅ **Klasifikasi kolom otomatis** — kolom sempit = No, kemudian Nama & TTD (state machine `no → nama → ttd`)
- ✅ **Baris header otomatis di-skip** (baris dengan tinggi ≪ median, mis. header kolom / baris gabungan "ANASTESI")
- ✅ **Crop presisi mengikuti tinta** — bounding box kontur, bukan ukuran sel tetap; margin 20px
- ✅ **PNG transparan (RGBA)** — background dihapus, tinta dipertahankan, tepi di-feather
- ✅ **Tahan terhadap scan berbayang** — threshold adaptif berbasis median background per-cell (bukan putih murni)
- ✅ **OCR nama bahasa Indonesia** (`tesseract-ocr-ind`), upscale 3× untuk akurasi
- ✅ **Reusable** — cukup taruh gambar baru di `input/`, jalankan ulang

---

## Struktur Proyek

```
signature-extractor/
│
├── docker-compose.yml       # mount input/output/src/debug
├── Dockerfile               # python:3.13-slim + tesseract-ocr-ind
├── requirements.txt
│
├── input/                   # letakkan gambar di sini
├── output/                  # hasil ekstraksi (per pegawai)
├── debug/                   # visualisasi grid & crop (untuk pengecekan)
├── models/                  # (cadangan untuk model OCR custom)
│
└── src/
    ├── main.py              # orkestrasi pipeline
    ├── detect_table.py      # deteksi grid, klasifikasi kolom, identifikasi header
    ├── crop_cells.py        # pemotongan cell + bounding box tinta
    ├── extract_signature.py # konversi ke PNG transparan (RGBA)
    ├── ocr_name.py          # OCR nama + parsing gelar + slug nama folder
    └── export.py            # penulisan output & ringkasan CSV
```

---

## Cara Pakai

```bash
# 1. Taruh gambar di input/
cp "dokumen_ttd.jpg" signature-extractor/input/

# 2. Jalankan (build image otomatis saat pertama kali)
cd signature-extractor
docker compose up --build

# Untuk proses ulang tanpa rebuild (cukup taruh gambar baru):
docker compose run --rm signature
```

Hasil:

```
output/
├── 01_Ali_Akbar_S_Kep/
│   ├── signature.png      # tempel ke Word / surat
│   ├── profile.png        # nama + ttd utk verifikasi cepat
│   └── metadata.json
├── ...
└── ringkasan.csv          # daftar semua pegawai + status
```

### Format metadata.json

```json
{
  "no": 1,
  "nama": "Ali Akbar",
  "gelar": "S.Kep",
  "nama_file": "Ali_Akbar_S_Kep",
  "ok_ocr": true,
  "sumber": "contoh ttd tgn pegawai ok.jpg.jpeg",
  "keterangan": "ok"
}
```

---

## Penanganan Kasus Khusus

| Kasus | Penanganan |
|---|---|
| Dua blok tabel (kiri & kanan) | Deteksi 6 kolom, proses per blok — 2 pegawai/baris |
| Baris gabungan / label grup ("ANASTESI") | Tinggi baris ≪ median → di-skip sebagai header; jika tetap muncul di data, folder-nya bertanda `TANPA_TTD` |
| Scan berbayang / background abu-abu | Threshold berbasis median background per-cell, bukan nilai tetap |
| Tanda tangan tipis / kecil | Threshold adaptif + bounding box tinta + margin 20px |
| OCR nama salah baca | `ringkasan.csv` siap dikoreksi manual sekali; perbaiki nama di folder & metadata |

---

## Keterbatasan yang Perlu Diketahui

1. **OCR nama tidak 100% akurat** — tesseract pada dokumen 150 DPI masih bisa salah baca (mis. `5. Kep` untuk `S. Kep`, `Amd. Keh` untuk `Amd. Keb`). **Verifikasi manual satu putaran tetap disarankan** — cukup koreksi nama folder & `metadata.json`.
2. **Nomor urut diambil dari posisi baris** (deterministik), bukan dari OCR kolom No — lebih andal, tetapi pastikan urutan baris di dokumen memang berurutan 1..N.
3. **`input/`, `output/`, `debug/` sengaja tidak di-commit** ke git karena gambar berisi tanda tangan (data pribadi). Salin/backup sendiri.

---

## Catatan Teknis

- `opencv-python-headless==4.12.0.88` membutuhkan `numpy<2.3.0` → pakai `numpy==2.2.6` (jangan dinaikkan ke 2.3.x).
- Deteksi garis: `cv2.morphologyEx` + projection profile; posisi garis = titik tengah proyeksi, sehingga crop sel selalu menyisakan ±2px garis → dihapus dengan zero-kan tepi 4px + penghapusan baris/kolom kontinu ≥70%.
- Tanda tangan diekstrak sebagai RGBA; alpha di-feather (GaussianBlur + normalize) agar tepi lembut saat ditempel.
