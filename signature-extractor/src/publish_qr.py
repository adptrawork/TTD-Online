"""publish_qr.py - Generate QR URL + index verifier untuk verifikasi online internal.

Membaca output/ (hasil pipeline), membangun `verifier_index.json`, lalu
mengenerate QR Code berisi URL pendek `/v/<id>` ke `output/qrcodes_web/`.

Pemakaian:
    VERIFY_BASE_URL="https://ttd.example.com" python src/publish_qr.py
    # default: http://localhost:8000 (uji lokal)

QR di `qrcodes/` (payload TTD penuh, offline) TIDAK disentuh — folder terpisah
`qrcodes_web/` dipakai untuk verifikasi online internal.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_M

ROOT = Path("/app")
OUTPUT = ROOT / "output"
BASE_URL = os.environ.get("VERIFY_BASE_URL", "http://localhost:8123").rstrip("/")


def main() -> None:
    index: dict = {}
    qr_web = OUTPUT / "qrcodes_web"
    qr_web.mkdir(exist_ok=True)

    rows = []
    skip_dirs = {"qrcodes", "qrcodes_web", "signatures", "profiles"}
    for folder in sorted(p for p in OUTPUT.iterdir()
                         if p.is_dir() and p.name not in skip_dirs):
        md = folder / "metadata.json"
        sig = folder / "signature.png"
        if not md.exists() or not sig.exists():
            continue

        meta = json.loads(md.read_text(encoding="utf-8"))
        no = meta.get("no")
        if no is None:
            continue
        nama = re.sub(r"^\d+_", "", folder.name)  # slug, utk nama file
        pid = f"t{no:02d}"

        index[pid] = {
            "no": no,
            "nama": nama,
            "nama_display": (meta.get("nama") or nama).strip(),
            "gelar": (meta.get("gelar") or "").strip(),
            "sumber": meta.get("sumber", ""),
            "png": f"{folder.name}/signature.png",
        }

        url = f"{BASE_URL}/v/{pid}"
        qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M,
                           box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        out = qr_web / f"{nama}.png"
        qr.make_image(fill_color="black", back_color="white").save(out)
        rows.append((nama, url, qr.version))

    (OUTPUT / "verifier_index.json").write_text(
        json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")

    for nama, url, ver in rows:
        print(f"  {nama:44s} versi~{ver:2d}  {url}")
    print(f"\n{len(rows)} QR (URL) -> {qr_web.name}/ + verifier_index.json")
    print(f"BASE_URL : {BASE_URL}")
    if "localhost" in BASE_URL or "127.0.0.1" in BASE_URL:
        print("NOTE    : QR berisi localhost — belum bisa discan HP. "
              "Set VERIFY_BASE_URL ke URL publik/tunnel lalu jalankan ulang.")


if __name__ == "__main__":
    main()
