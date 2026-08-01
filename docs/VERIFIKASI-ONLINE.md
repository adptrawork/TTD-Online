# Verifikasi Online TTD — Dokumentasi Deploy & Operasi

> **Status: AKTIF** (URL sementara `dev.rsudkotajambi.id`) — 1 Agu 2026
> Dokumen ini mencatat kondisi pemasangan **saat ini**. Lihat bagian
> [Pindah ke Subdomain Final](#pindah-ke-subdomain-final) untuk migrasi ke URL permanen.

---

## 1. Ringkasan

Verifikasi tanda tangan (TTD) pegawai secara **online** berjalan lewat subdomain
`dev.rsudkotajambi.id` (sementara) hingga URL permanen (`ttd` / `rs`) tersedia.

- **27 pegawai** sudah masuk `verifier_index.json`, QR berisi URL pendek `/v/<id>`.
- Verifier = FastAPI kecil di server Docker, data dibaca per-request (update data
  tanpa restart server).
- QR **offline** (`output/qrcodes/`, payload `T1...`) tidak terpengaruh oleh semua ini.

---

## 2. Arsitektur (kondisi sekarang)

```
HP scan QR ──► http://dev.rsudkotajambi.id/v/t01
        │  DNS Rumahweb: A  dev → 103.147.236.138   (sudah ada, dipakai sementara)
        ▼
MikroTik 103.147.236.138  NAT :80 → 192.168.2.220:8080
        ▼
nginx halomanap-nginx (container, port 8080 host → 80)
        │  conf.d/ttd.conf  (server_name: ttd.rsudkotajambi.id  dev.rsudkotajambi.id)
        ▼
proxy_pass http://ttd-verifier:8000   (network halo-manap_default)
        ▼
verifier (FastAPI) → data /home/mini_pacs/ttd → halaman ✓ VALID + TTD
```

> ⚠️ **PENTING:** `103.147.236.138:8080` dari luar adalah **webfig MikroTik**,
> bukan server. Server Ubuntu ada di belakang NAT MikroTik dengan IP lokal
> `192.168.2.220`. Akses publik hanya lewat NAT MikroTik (port 80 → 8080).

---

## 3. Komponen

| Komponen | Detail |
|---|---|
| Server | `mini_pacs@103.147.236.138` (SSH via key `~/.ssh/id_ed25519`), IP LAN `192.168.2.220`, Docker 29.6.1 |
| Verifier container | `ttd-verifier` — image `signature-extractor-verifier`, port `0.0.0.0:8123→8000`, mount `/home/mini_pacs/ttd:/data:ro` |
| Network | `halo-manap_default` (join manual: `docker network connect halo-manap_default ttd-verifier`) |
| Nginx proxy | container `halomanap-nginx` — mount folder `~/projects/halo-manap/docker/nginx/conf.d/` → `/etc/nginx/conf.d` |
| Konfigurasi proxy | `conf.d/ttd.conf` (server block, proxy ke `ttd-verifier:8000`) |
| Data verifier | `/home/mini_pacs/ttd/` (rsync dari `output/`, exclude signatures/profiles/qrcodes) |
| DNS | Rumahweb (NS `nsid1-4.rumahweb.*`), panel user |

`conf.d/ttd.conf` saat ini:

```nginx
# TTD-OK verifier — verifikasi tanda tangan internal (RSUD Kota Jambi)
server {
    listen 80;
    server_name ttd.rsudkotajambi.id dev.rsudkotajambi.id;   # dev = sementara
    location / {
        proxy_pass http://ttd-verifier:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30;
    }
}
```

---

## 4. URL & Endpoint Aktif

| Endpoint | Fungsi | Status |
|---|---|---|
| `http://dev.rsudkotajambi.id/v/t01` | halaman VALID + TTD (contoh id `t01`) | ✅ AKTIF (sementara) |
| `http://dev.rsudkotajambi.id/v/t01/img` | PNG tanda tangan | ✅ 200 |
| `http://dev.rsudkotajambi.id/v/t999` | id tidak dikenal | ✅ 404 |
| `http://dev.rsudkotajambi.id/` | halaman verifier (sejak alias dev aktif, halomanap tidak lagi tampil di dev) | ✅ |
| `http://dev.rsudkotajambi.id/healthz` | health check | ✅ `{"status":"ok"}` |

**QR web** (`output/qrcodes_web/`, 27 file) berisi `http://dev.rsudkotajambi.id/v/tXX`.
**QR offline** (`output/qrcodes/`) tetap berisi payload `T1...` — tidak berubah.

> ⚠️ Selama `dev` dipakai sebagai alias verifier, `http://dev.rsudkotajambi.id/`
> menampilkan verifier, **bukan** Halo MANAP. Kalau `dev` dibutuhkan lagi untuk
> halomanap, lepas `dev` dari `server_name` (lihat §7) dan pindahkan verifier ke
> subdomain lain.

---

## 5. Operasi Harian

### 5.1 Update data pegawai (ada dokumen baru)

```bash
cd "/mnt/DiskD/Projects/TTD OK/signature-extractor"
# proses ekstraksi dulu (bila perlu):
docker compose run --rm signature

# regenerate QR (URL aktif) + kirim data ke server:
VERIFY_BASE_URL='http://dev.rsudkotajambi.id' \
TTD_SERVER='mini_pacs@103.147.236.138' \
TTD_DIR='/home/mini_pacs/ttd' \
./deploy.sh sync
```

### 5.2 Cek status verifier

```bash
curl -s http://dev.rsudkotajambi.id/v/t01 | grep -o VALID     # → VALID
curl -s http://dev.rsudkotajambi.id/healthz                   # → {"status":"ok"}
ssh mini_pacs@103.147.236.138 'docker ps | grep ttd-verifier'
ssh mini_pacs@103.147.236.138 'docker logs --tail 30 ttd-verifier'
```

### 5.3 Deploy verifier dari nol (server baru / container hilang)

```bash
cd "/mnt/DiskD/Projects/TTD OK/signature-extractor"
TTD_SERVER='mini_pacs@103.147.236.138' ./deploy.sh setup   # kirim image + jalankan
# lalu wajib: join network halomanap agar nama ttd-verifier resolve dari nginx:
ssh mini_pacs@103.147.236.138 'docker network connect halo-manap_default ttd-verifier'
VERIFY_BASE_URL='http://dev.rsudkotajambi.id' TTD_SERVER='mini_pacs@103.147.236.138' \
  TTD_DIR='/home/mini_pacs/ttd' ./deploy.sh sync
```

---

## 6. Pindah ke Subdomain Final

Saat sudah bisa membuat subdomain baru di panel (mis. `rs` atau `ttd`):

1. **Panel DNS** — buat record seperti `dev`:
   ```
   nama:  rs      (atau ttd)
   URL/IP: http://103.147.236.138/
   ```
2. **Server** (ganti `server_name`, lepas `dev` bila sudah tidak perlu):
   ```bash
   ssh mini_pacs@103.147.236.138
   cd ~/projects/halo-manap/docker/nginx/conf.d
   sed -i 's/server_name ttd.rsudkotajambi.id dev.rsudkotajambi.id;/server_name rs.rsudkotajambi.id;/' ttd.conf
   docker exec halomanap-nginx nginx -t && docker exec halomanap-nginx nginx -s reload
   ```
3. **QR** — regenerate dengan URL final:
   ```bash
   VERIFY_BASE_URL='http://rs.rsudkotajambi.id' \
   TTD_SERVER='mini_pacs@103.147.236.138' TTD_DIR='/home/mini_pacs/ttd' \
   ./deploy.sh sync
   ```
4. **Tes**: `curl -s http://rs.rsudkotajambi.id/v/t01 | grep -o VALID`

Total ±5 menit; versi QR tetap ~3, mudah discan. Data di server tidak berubah.

---

## 7. Backup & Rollback

| Item | Lokasi |
|---|---|
| Backup nginx sebelum pola conf.d | `~/projects/halo-manap/docker/nginx.bak-20260801/` |
| Repo halomanap2 (commit konfigurasi) | `github.com/Robbialbert87/halomanap2` (commit `c50ebd1`) |
| Repo TTD-OK (deploy/verifier/docs) | `github.com/adptrawork/TTD-OK` |
| Sertifikat SSL Sectigo (belum terpasang) | `/home/adptra01/Downloads/domain_manap/` (valid s/d 30 Nov 2026) |

Rollback ke sebelum pola conf.d: `docker compose up -d nginx` dengan mount lama
(`docker/nginx/default.conf` single file) — file masih ada di `nginx.bak-20260801`.

---

## 8. Catatan & Troubleshooting

| Gejala | Penyebab | Solusi |
|---|---|---|
| QR tidak terbuka / DNS error | Record `dev` dihapus/diubah di panel | Cek panel Rumahweb, record A `dev → 103.147.236.138` |
| `502 Bad Gateway` dari `/v/...` | Container `ttd-verifier` tidak di network `halo-manap_default` (mis. setelah recreate) | `docker network connect halo-manap_default ttd-verifier` |
| `504` / lambat | Verifier down | `docker logs ttd-verifier`, `docker ps` |
| `103.147.236.138:8080` tampil RouterOS | Itu webfig MikroTik, bukan server | Gunakan port 80 / subdomain |
| HTTPS belum aktif | Port 443 publik tertutup; cert belum dipasang | Butuh buka port 443 di MikroTik + pasang cert Sectigo (rencana) |

---

## 9. Akses

- SSH: `mini_pacs@103.147.236.138` (key `~/.ssh/id_ed25519`, tanpa password).
- User server **tanpa sudo passwordless** — hindari operasi yang butuh sudo.
- Semua data verifier bersifat internal; jangan expose 8123 ke publik tanpa proteksi.
