# Verifikasi Online TTD — Dokumentasi Deploy & Operasi

> **Status: AKTIF** — 1 Agu 2026
> Jalur utama: **Tailscale Funnel** (`https://minipacs-ttd.tail8394aa.ts.net/ttd`),
> **tanpa perlu mengubah konfigurasi panel DNS Rumahweb**.
> Jalur cadangan: `http://dev.rsudkotajambi.id/v/t01` (subdomain DNS yang sudah ada).

---

## 1. Ringkasan

Verifikasi tanda tangan (TTD) pegawai secara **online** berjalan lewat:

1. **Tailscale Funnel** (utama) — HTTPS otomatis, gratis, tanpa sentuh panel hosting.
2. **Subdomain `dev`** (cadangan) — HTTP, butuh record DNS yang sudah ada.

- **27 pegawai** sudah masuk `verifier_index.json`, QR berisi URL pendek `/v/<id>`.
- Verifier = FastAPI kecil di server Docker, data dibaca per-request (update data
  tanpa restart server).
- QR **offline** (`output/qrcodes/`, payload `T1...`) tidak terpengaruh oleh semua ini.

---

## 2. Arsitektur (jalur aktif)

### Jalur A — Tailscale Funnel (utama, HTTPS, tanpa panel DNS)

```
HP scan QR ──► https://minipacs-ttd.tail8394aa.ts.net/ttd/v/t01
        │  Tailscale Funnel (node minipacs-ttd, HTTPS cert otomatis)
        ▼
tailscaled  :443 (host) ──► http://127.0.0.1:8080  (funnel → nginx)
        ▼
nginx halomanap-nginx (container)
        │  path routing:  /ttd/ → verifier · / → halomanap
        ▼
proxy_pass http://ttd-verifier:8000  (network halo-manap_default)
        ▼
verifier (FastAPI) → data /home/mini_pacs/ttd → halaman ✓ VALID + TTD
```

### Jalur B — Subdomain DNS (cadangan)

```
HP scan QR ──► http://dev.rsudkotajambi.id/v/t01
        │  DNS Rumahweb: A  dev → 103.147.236.138
        ▼
MikroTik NAT :80 → 192.168.2.220:8080 → nginx → ttd.conf (server_name dev) → verifier
```

> ⚠️ **PENTING:** `103.147.236.138:8080` dari luar adalah **webfig MikroTik**,
> bukan server. Server Ubuntu ada di belakang NAT MikroTik dengan IP lokal
> `192.168.2.220`. Akses publik lewat NAT (port 80 → 8080) atau Funnel.

---

## 3. Komponen

| Komponen | Detail |
|---|---|
| Server | `mini_pacs@103.147.236.138` (SSH via key `~/.ssh/id_ed25519`), IP LAN `192.168.2.220`, Docker 29.6.1 |
| Verifier container | `ttd-verifier` — image `signature-extractor-verifier`, port `0.0.0.0:8123→8000`, mount `/home/mini_pacs/ttd:/data:ro` |
| Network | `halo-manap_default` (join manual: `docker network connect halo-manap_default ttd-verifier`) |
| Nginx proxy | container `halomanap-nginx` — mount folder `~/projects/halo-manap/docker/nginx/conf.d/` → `/etc/nginx/conf.d` |
| Routing path | `conf.d/default.conf` — `location /ttd/` → `ttd-verifier:8000` (pola multi-proyek) |
| Routing subdomain | `conf.d/ttd.conf` — `server_name ttd.rsudkotajambi.id dev.rsudkotajambi.id` → verifier |
| Tailscale Funnel | container `tailscale` (`--network host`), node `minipacs-ttd`, serve `8080` via :443 |
| Bind port halomanap | host `80→8080`, `8443→443` (443 host dikosongkan untuk Funnel) |
| Data verifier | `/home/mini_pacs/ttd/` (rsync dari `output/`, exclude signatures/profiles/qrcodes) |
| DNS | Rumahweb (NS `nsid1-4.rumahweb.*`) — **tidak diubah** untuk jalur funnel |

**Routing di nginx** (keduanya aktif sekaligus):

```nginx
# conf.d/default.conf  (server default — path-based multi-proyek)
location /ttd/ {
    proxy_pass http://ttd-verifier:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30;
}
# sisanya / → halomanap (Laravel)

# conf.d/ttd.conf  (server block subdomain → verifier)
server {
    listen 80;
    server_name ttd.rsudkotajambi.id dev.rsudkotajambi.id;
    location / { proxy_pass http://ttd-verifier:8000; ... }
}
```

---

## 4. URL & Endpoint Aktif

| Endpoint | Fungsi | Status |
|---|---|---|
| `https://minipacs-ttd.tail8394aa.ts.net/ttd/v/t01` | halaman VALID + TTD (utama) | ✅ AKTIF |
| `https://minipacs-ttd.tail8394aa.ts.net/ttd/v/t01/img` | PNG tanda tangan | ✅ 200 |
| `https://minipacs-ttd.tail8394aa.ts.net/ttd/` | halaman verifier | ✅ |
| `https://minipacs-ttd.tail8394aa.ts.net/` | halomanap (path lain tidak terganggu) | ✅ 200 |
| `http://dev.rsudkotajambi.id/v/t01` | cadangan (HTTP) | ✅ |
| `http://dev.rsudkotajambi.id/healthz` | health check | ✅ `{"status":"ok"}` |
| ID tidak dikenal (`/ttd/v/t999`) | | ✅ 404 |

**QR web** (`output/qrcodes_web/`, 27 file) berisi `https://minipacs-ttd.tail8394aa.ts.net/ttd/v/tXX`.
**QR offline** (`output/qrcodes/`) tetap payload `T1...` — tidak berubah.

> ⚠️ `dev` dipakai sebagai alias verifier → `http://dev.rsudkotajambi.id/` menampilkan
> verifier, **bukan** halomanap (sebelumnya halomanap). Kalau `dev` dibutuhkan lagi
> untuk halomanap, lepas `dev` dari `server_name` di `ttd.conf` (§6).

---

## 5. Operasi Harian

### 5.1 Update data pegawai (ada dokumen baru)

```bash
cd "/mnt/DiskD/Projects/TTD OK/signature-extractor"
docker compose run --rm signature          # bila perlu proses ekstraksi ulang

VERIFY_BASE_URL='https://minipacs-ttd.tail8394aa.ts.net/ttd' \
TTD_SERVER='mini_pacs@103.147.236.138' \
TTD_DIR='/home/mini_pacs/ttd' \
./deploy.sh sync
```

### 5.2 Cek status verifier

```bash
curl -s https://minipacs-ttd.tail8394aa.ts.net/ttd/v/t01 | grep -o VALID
curl -s http://dev.rsudkotajambi.id/healthz                       # {"status":"ok"}
ssh mini_pacs@103.147.236.138 'docker ps | grep ttd-verifier'
ssh mini_pacs@103.147.236.138 'docker logs --tail 30 ttd-verifier'
```

### 5.3 Deploy verifier dari nol (server baru / container hilang)

```bash
cd "/mnt/DiskD/Projects/TTD OK/signature-extractor"
TTD_SERVER='mini_pacs@103.147.236.138' ./deploy.sh setup
# wajib join network agar nama ttd-verifier resolve dari nginx:
ssh mini_pacs@103.147.236.138 'docker network connect halo-manap_default ttd-verifier'
VERIFY_BASE_URL='https://minipacs-ttd.tail8394aa.ts.net/ttd' \
TTD_SERVER='mini_pacs@103.147.236.138' TTD_DIR='/home/mini_pacs/ttd' ./deploy.sh sync
```

### 5.4 Operasional Tailscale Funnel (bila perlu)

```bash
ssh mini_pacs@103.147.236.138
# status / aktifkan ulang (bila container tailscale di-recreate):
docker exec tailscale tailscale status
docker exec tailscale tailscale funnel 8080      # serve :443 → 127.0.0.1:8080
# syarat: host port 443 harus kosong — halomanap-nginx bind di 8443 (lihat §3)
```

> ⚠️ Jika container `tailscale` di-recreate/restart, pastikan masih
> `--network host` dan jalankan `tailscale funnel 8080` lagi setelah up.

---

## 6. Pindah ke Subdomain Final (opsional, URL lebih rapi)

Saat subdomain baru bisa dibuat di panel (mis. `rs` atau `ttd`):

1. **Panel DNS** — buat record seperti `dev`: nama `rs` → URL `http://103.147.236.138/`.
2. **Server** — ganti `server_name` (lepas `dev` bila tidak perlu lagi):
   ```bash
   ssh mini_pacs@103.147.236.138
   cd ~/projects/halo-manap/docker/nginx/conf.d
   sed -i 's/server_name ttd.rsudkotajambi.id dev.rsudkotajambi.id;/server_name rs.rsudkotajambi.id;/' ttd.conf
   docker exec halomanap-nginx nginx -t && docker exec halomanap-nginx nginx -s reload
   ```
3. **QR** — `VERIFY_BASE_URL='http://rs.rsudkotajambi.id' ... ./deploy.sh sync`
4. **Tes** — `curl -s http://rs.rsudkotajambi.id/v/t01 | grep -o VALID`

Total ±5 menit; versi QR tetap kecil, data di server tidak berubah.
*(Funnel dapat tetap aktif sebagai cadangan; tidak perlu dimatikan.)*

---

## 7. Backup & Rollback

| Item | Lokasi |
|---|---|
| Backup nginx sebelum pola conf.d | `~/projects/halo-manap/docker/nginx.bak-20260801/` |
| Repo halomanap2 (config nginx, branch `docker`) | `github.com/Robbialbert87/halomanap2` |
| Repo TTD-OK (deploy/verifier/docs) | `github.com/adptrawork/TTD-OK` |
| Sertifikat SSL Sectigo (belum terpasang) | `/home/adptra01/Downloads/domain_manap/` (valid s/d 30 Nov 2026) |
| Sebelum Funnel (halomanap bind 443) | `.env` server: hapus baris `HTTPS_PORT=8443` → `docker compose up -d nginx` |

Rollback pola conf.d: gunakan mount lama (`docker/nginx/default.conf` single file,
masih ada di `nginx.bak-20260801`).

---

## 8. Catatan & Troubleshooting

| Gejala | Penyebab | Solusi |
|---|---|---|
| `https://...ts.net/ttd/...` tidak terbuka | Funnel mati / tailscale container down | `docker exec tailscale tailscale funnel 8080`; cek `docker ps` |
| `502 Bad Gateway` dari `/ttd/...` | `ttd-verifier` tidak di network `halo-manap_default` (mis. setelah recreate) | `docker network connect halo-manap_default ttd-verifier` |
| `502` / lambat | Verifier down | `docker logs ttd-verifier` |
| QR `http://dev...` tidak terbuka | Record `dev` dihapus/diubah di panel | Cek panel Rumahweb, record A `dev → 103.147.236.138` |
| `tailscale funnel` gagal "listener already exists for port 443" | Ada yang bind host :443 (halomanap bind lama / sisa funnel) | Pastikan `HTTPS_PORT=8443` di `.env` + `docker compose up -d nginx`; lalu `tailscale funnel reset` |
| `103.147.236.138:8080` tampil RouterOS | Itu webfig MikroTik, bukan server | Gunakan port 80 / Funnel / subdomain |
| HTTPS pakai domain sendiri | Port 443 publik MikroTik tertutup; cert belum dipasang | Butuh buka 443 di MikroTik + pasang cert Sectigo (rencana) |

---

## 9. Akses

- SSH: `mini_pacs@103.147.236.138` (key `~/.ssh/id_ed25519`, tanpa password).
- User server **tanpa sudo passwordless** — hindari operasi yang butuh sudo.
- Funnel URL (`*.ts.net`) adalah HTTPS publik — boleh dipakai untuk QR internal;
  jangan expose data di luar kebutuhan (verifier hanya berisi nama + TTD pegawai).
