#!/usr/bin/env bash
# deploy.sh — Deploy verifier TTD-OK ke server terpisah (Linux + SSH + Docker).
#
# Alur: image verifier dikirim via `docker save|load`, container dijalankan di
# server, lalu data pegawai + verifier_index.json di-sync (rsync) dari mesin
# lokal. Verifier membaca data per-request -> update data tidak perlu restart.
#
# Variabel (bisa via env):
#   TTD_SERVER        alamat SSH server, mis. "user@203.0.113.5"
#   TTD_DIR           direktori data di server (default /opt/ttd)
#   VERIFY_BASE_URL   URL publik verifier, mis. "http://203.0.113.5:8123"
#                     atau "https://ttd.kantor.id" (wajib utk perintah sync)
#
# Pemakaian:
#   TTD_SERVER='user@ip' ./deploy.sh setup
#       pasang/update backend di server (image + container, port 8123)
#   VERIFY_BASE_URL='http://ip:8123' TTD_SERVER='user@ip' ./deploy.sh sync
#       generate QR URL server + kirim data pegawai ke server
#   TTD_SERVER='user@ip' VERIFY_BASE_URL='http://ip:8123' ./deploy.sh full
#       setup lalu sync (jalankan pertama kali)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SERVER="${TTD_SERVER:-}"
TTD_DIR="${TTD_DIR:-/opt/ttd}"
BASE_URL="${VERIFY_BASE_URL:-}"
IMG="ttd-verifier"
IMAGE_LOCAL="signature-extractor-verifier"
CMD="${1:-help}"

require_server() {
  if [ -z "$SERVER" ]; then
    echo "ERROR: set dulu TTD_SERVER, mis. TTD_SERVER='user@203.0.113.5'"
    echo "  $0 setup | sync | full"
    exit 1
  fi
}

cmd_setup() {
  require_server
  echo ">> [1/3] kirim image verifier ke $SERVER (docker save|load)..."
  docker save "$IMAGE_LOCAL" | ssh "$SERVER" "docker load"

  echo ">> [2/3] siapkan direktori data $TTD_DIR di server..."
  ssh "$SERVER" "mkdir -p '$TTD_DIR'"

  echo ">> [3/3] jalankan container ttd-verifier (port 8123)..."
  ssh "$SERVER" "docker rm -f ttd-verifier >/dev/null 2>&1 || true; \
    docker run -d --name ttd-verifier --restart unless-stopped \
      -p 8123:8000 -v '$TTD_DIR:/data:ro' '$IMG' >/dev/null"
  echo "   OK. Cek akses:  curl http://<IP-ATAU-DOMAIN-SERVER>:8123/healthz"
}

cmd_sync() {
  require_server
  if [ -z "$BASE_URL" ]; then
    echo "ERROR: set VERIFY_BASE_URL dulu, mis. VERIFY_BASE_URL='http://203.0.113.5:8123'"
    exit 1
  fi

  echo ">> [1/3] generate QR berisi URL server ($BASE_URL)..."
  VERIFY_BASE_URL="$BASE_URL" docker compose run --rm signature \
    python src/publish_qr.py | tail -6

  echo ">> [2/3] kirim data pegawai + index ke server (rsync)..."
  rsync -az --delete \
    --exclude='signatures/' --exclude='profiles/' \
    --exclude='qrcodes/' --exclude='qrcodes_web/' \
    --exclude='scan_ttd.html' --exclude='ringkasan.csv' \
    output/ "$SERVER:$TTD_DIR/"

  echo ">> [3/3] verifikasi dari server..."
  ssh "$SERVER" "curl -sf http://localhost:8123/healthz && echo '  verifier OK' || echo '  GAGAL: verifier tidak merespon'"
  echo "   Uji:  curl -s $BASE_URL/v/t01 | grep -o VALID"
  echo "   QR siap dipakai di output/qrcodes_web/"
}

cmd_full() {
  require_server
  cmd_setup
  cmd_sync
}

case "$CMD" in
  setup) cmd_setup ;;
  sync)  cmd_sync ;;
  full)  cmd_full ;;
  *)
    echo "Pemakaian: $0 {setup|sync|full}"
    echo "  setup  — pasang/update backend verifier di server"
    echo "  sync   — generate QR (URL server) + kirim data ke server"
    echo "  full   — setup lalu sync"
    ;;
esac
