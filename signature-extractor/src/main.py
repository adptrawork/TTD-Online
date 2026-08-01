"""main.py - Pipeline utama signature-extractor.

Flow:
    input/*.jpg|png
        -> detect_grid            (deteksi garis, grid, klasifikasi kolom)
        -> per baris data:
              OCR nomor & nama    (tesseract ind)
              crop TTD presisi    (bounding box tinta)
              PNG transparan      (alpha channel)
        -> output/<no>_<nama_slug>/  (signature.png, profile.png, metadata.json)
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from detect_table import GridInfo, detect_grid, summarize          # noqa: E402
from crop_cells import crop_ink                                    # noqa: E402
from extract_signature import save_rgba, signature_to_rgba         # noqa: E402
from export import (export_pegawai, save_debug, write_summary,  # noqa: E402
                    collect_unique)
from qr_export import generate_all                              # noqa: E402
import shutil                                                   # noqa: E402
from ocr_name import extract_pegawai                     # noqa: E402

INPUT_DIR = Path("/app/input")
OUTPUT_DIR = Path("/app/output")
TESSERACT_LANG = "ind"
MARGIN = 20
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}


def _process_image(path: Path, lang: str) -> list:
    print(f"\n=== Proses: {path.name} ===")
    grid = detect_grid(path)
    print(summarize(grid))

    # visualisasi grid ke debug/ utk verifikasi
    debug = grid.image.copy()
    for y in grid.h_lines:
        cv2.line(debug, (0, y), (grid.image.shape[1], y), (0, 0, 255), 2)
    for x in grid.v_lines:
        cv2.line(debug, (x, 0), (x, grid.image.shape[0]), (0, 255, 0), 2)
    save_debug(f"grid_{path.stem}.jpg", debug)

    rows: list = []
    for row_idx, (y0, y1) in enumerate(grid.data_row_bands):
        for block_idx, (bx0, bx1) in enumerate(grid.block_bounds):
            # kolom di dalam blok ini
            block_cols = [c for c in grid.columns if bx0 <= c.x0 < bx1]
            col_no = next((c for c in block_cols if c.role == "no"), None)
            col_nama = next((c for c in block_cols if c.role == "nama"), None)
            col_ttd = next((c for c in block_cols if c.role == "ttd"), None)
            if col_nama is None or col_ttd is None:
                continue

            row_band = (y0, y1)
            # nomor deterministik dari posisi: blok kiri = 1..n, kanan = n+1..2n
            no = row_idx + 1 if block_idx == 0 else row_idx + 1 + len(grid.data_row_bands)

            crop_nama = crop_ink(grid, row_band, col_nama, MARGIN)
            crop_ttd = crop_ink(grid, row_band, col_ttd, MARGIN)

            pegawai = extract_pegawai(crop_nama, crop_ttd, no, lang=lang)

            rgba = None
            if crop_ttd is not None:
                rgba = signature_to_rgba(crop_ttd)

            # debug: potongan nama & ttd utk pengecekan cepat
            save_debug(f"cell_{no:02d}_nama.jpg", crop_nama) if crop_nama is not None else None
            save_debug(f"cell_{no:02d}_ttd.jpg", crop_ttd) if crop_ttd is not None else None

            status = "OK"
            if rgba is None:
                status = "TANPA_TTD"
            elif not pegawai["ok_ocr"]:
                status = "OCR_GAGAL"

            folder = export_pegawai(pegawai, rgba, crop_nama, crop_ttd, source_image=path.name)
            rows.append({**pegawai, "folder_name": folder.name, "status": status})
            print(f"  [{no:02d}] {pegawai['nama'] or '(nama kosong)'} "
                  f"{pegawai['gelar'] or ''} -> {folder.name} [{status}]")

    return rows


def main() -> None:
    INPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    images = sorted(
        p for p in INPUT_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not images:
        print(f"Tidak ada gambar di {INPUT_DIR} (ekstensi: {sorted(IMAGE_EXTS)}).")
        sys.exit(1)

    all_rows: list = []
    for img in images:
        try:
            all_rows.extend(_process_image(img, TESSERACT_LANG))
        except Exception as exc:
            print(f"[ERROR] Gagal memproses {img.name}: {exc}")

    write_summary(all_rows)
    sig_dir, pro_dir = collect_unique()
    ok = sum(1 for r in all_rows if r["status"] == "OK")
    print(f"\nSelesai: {len(all_rows)} pegawai diproses ({ok} OK). "
          f"Ringkasan: {OUTPUT_DIR / 'ringkasan.csv'}")
    print(f"File unik: {sig_dir.name}/ ({len(list(sig_dir.glob('*.png')))}), "
          f"{pro_dir.name}/ ({len(list(pro_dir.glob('*.png')))})")

    # QR Code berisi gambar TTD + viewer utk scan
    qr_dir, qr_rows = generate_all(OUTPUT_DIR)
    viewer = Path(__file__).parent / "scan_ttd.html"
    if viewer.exists():
        shutil.copy2(viewer, OUTPUT_DIR / "scan_ttd.html")
    n_ok = sum(1 for r in qr_rows if r["status"] == "ok")
    print(f"QR Code: {qr_dir.name}/ ({n_ok} dari {len(qr_rows)} TTD) + viewer scan_ttd.html")
    for r in all_rows:
        if r["status"] != "OK":
            print(f"  perlu perhatian: {r['no']:02d} {r['nama'] or ''} -> {r['status']}")


if __name__ == "__main__":
    main()
