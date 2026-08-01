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
- ✅ **QR Code verifikasi** — tiap TTD di-encode jadi QR berisi gambar TTD + nama (bisa discan kamera HP)
- ✅ **Viewer scan offline** (`scan_ttd.html`) — kamera/jsQR atau tempel teks hasil scan
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
    ├── qr_export.py         # encode TTD -> QR Code (payload T1)
    ├── scan_ttd.html        # viewer scan offline (jsQR)
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
├── signatures/            # signature_<nama>.png (satu per pegawai)
├── profiles/              # profile_<nama>.png
├── qrcodes/               # qr_<nama>.png (QR berisi TTD)
├── scan_ttd.html          # viewer scan QR (buka di browser)
├── ...
└── ringkasan.csv          # daftar semua pegawai + status
```

---

## Verifikasi Tanda Tangan via QR Code

Setiap TTD yang berhasil diekstrak otomatis di-encode menjadi **QR Code** yang
menyimpan **gambar tanda tangan** + **nama pemilik**, sehingga bisa diverifikasi
dengan scan kamera HP (offline, tanpa server).

```
output/qrcodes/qr_Ali_Akbar_S_Kep.png
        │
        ▼  scan (kamera HP / jsQR)
┌───────────────────────┐
│  T1 + base64(payload) │   ← semua ASCII printable (aman semua scanner)
│  payload = TTD1 │     │
│  nama │ PNG 1-bit     │
└───────────────────────┘
        ▼
  gambar TTD + nama ditampilkan
```

**Cara pakai:**

1. Buka `output/scan_ttd.html` di browser (dari `localhost` / file server —
   kamera butuh izin, idealnya HTTPS).
2. Klik **Mulai Kamera**, arahkan ke QR, atau
3. Tempel teks hasil scan QR (diawali `T1...`) di kolom bawah.

**Format payload** (`qr_export.py`):

```
payload_bytes = "TTD1" + len(nama):1 + nama_utf8 + png_biner(1-bit)
payload_qr    = "T1" + base64(payload_bytes)
```

TTD di-threshold ke biner 1-bit (tinta hitam di kertas putih) sehingga ukuran
PNG turun 30–50× → versi QR 16–25 (mudah discan HP) dengan resolusi TTD penuh.

---

## Verifikasi Online (Internal Organisasi)

Untuk verifikasi dari HP pegawai, QR berisi **URL pendek** (`/v/<id>`) yang
membuka halaman verifikasi di backend kecil (FastAPI). TTD **tidak pernah
dipublikasikan** — backend hanya diakses internal / via tunnel terkunci.

```
QR (qrcodes_web/) = https://<host>/v/t01
        │  scan HP → browser
        ▼
┌────────────────────────────┐
│ verifier/ (FastAPI, Docker)│   /v/<id>       halaman VALID + nama + TTD
│ port 8123, data: output/   │   /v/<id>/img   PNG tanda tangan
└────────────────────────────┘   /v/<id>/json  metadata (integrasi)
```

**Fase 1 — jalan lokal** (sudah selesai):

```bash
cd signature-extractor

# 1. Buat index + QR berisi URL (default http://localhost:8123)
docker compose run --rm signature python src/publish_qr.py

# 2. Jalankan backend verifier
docker compose up -d verifier

# 3. Buka di browser
#    http://localhost:8123/v/t01
```

**Fase 2 — akses dari HP / luar jaringan** (pilih salah satu):

| Jalur | Cara | Cocok untuk |
|---|---|---|
| **Server terpisah** ✅ (paling bersih) | Deploy verifier di server Linux + Docker; mesin lokal cukup kirim data via `deploy.sh` | Punya server sendiri, TTD di server internal |
| **LAN kantor** (paling cepat) | `VERIFY_BASE_URL="http://<ip-lan>:8123" python src/publish_qr.py`; HP di WiFi kantor scan langsung | Kantor dengan WiFi sendiri |
| **Cloudflare Tunnel** | `cloudflared tunnel --url http://localhost:8123` → URL `https://xxx.trycloudflare.com`; permanen butuh domain + [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/) (auth email, 50 user gratis) | Akses dari mana saja, terkunci |
| **Tailscale Funnel** | Install Tailscale; `tailscale funnel 8123` → `https://<machine>.ts.net`; hanya perangkat di tailnet | Tim kecil, semua perangkat di-manage |

Setelah URL final didapat, regenerate QR sekali lagi dengan `VERIFY_BASE_URL`
baru — QR versi hanya 2–3 (URL pendek), sangat mudah discan.

### Deploy ke server terpisah (Linux + SSH + Docker)

Server terpisah = host verifier; mesin lokal tetap untuk ekstraksi & kirim data.

```bash
cd signature-extractor

# 1. Pasang backend di server (kirim image via docker save/load + jalankan container)
TTD_SERVER='user@203.0.113.5' ./deploy.sh setup

# 2. Cek akses publik server (dari mana saja):
curl http://203.0.113.5:8123/healthz     # -> {"status":"ok"}

# 3. Generate QR berisi URL server + kirim data pegawai:
VERIFY_BASE_URL='http://203.0.113.5:8123' TTD_SERVER='user@203.0.113.5' ./deploy.sh sync

# (pertama kali bisa langsung: TTD_SERVER=... VERIFY_BASE_URL=... ./deploy.sh full)
```

Yang dikirim hanya yang dibutuhkan verifier: folder pegawai (`signature.png`)
+ `verifier_index.json` (dibuat `publish_qr.py`). Verifier membaca data
per-request → **update data tidak perlu restart server**. Saat ada dokumen
baru: jalankan pipeline → `./deploy.sh sync`.

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
