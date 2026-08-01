"""export.py - Penulisan output ke disk.

Struktur per pegawai:

    output/
    └── 01_Ali_Akbar_S_Kep/
        ├── signature.png      # PNG transparan (tanda tangan saja)
        ├── profile.png        # nama + tanda tangan (gabungan)
        └── metadata.json      # no, nama, gelar, sumber gambar
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

OUTPUT_DIR = Path("/app/output")
DEBUG_DIR = Path("/app/debug")


def _stack_vertical(top: Optional[np.ndarray], bottom: Optional[np.ndarray]) -> np.ndarray:
    """Gabungkan dua crop (BGR) secara vertikal dengan separator putih tipis."""
    parts = [p for p in (top, bottom) if p is not None and p.size > 0]
    if not parts:
        return np.zeros((10, 10, 3), dtype=np.uint8)
    max_w = max(p.shape[1] for p in parts)
    norm = []
    for p in parts:
        h, w = p.shape[:2]
        if w < max_w:
            pad = np.full((h, max_w - w, 3), 255, dtype=np.uint8)
            p = np.hstack([p, pad])
        norm.append(p)
    return np.vstack(norm)


def save_debug(name: str, img: np.ndarray) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(DEBUG_DIR / name), img)


def export_pegawai(pegawai: Dict, signature_rgba: Optional[np.ndarray],
                   crop_nama: Optional[np.ndarray], crop_ttd: Optional[np.ndarray],
                   source_image: str) -> Path:
    """Tulis folder output untuk satu pegawai. Mengembalikan path folder."""
    safe = "".join(c for c in pegawai["nama_file"] if c.isalnum() or c in "_-")
    folder = OUTPUT_DIR / f"{pegawai['no']:02d}_{safe}"
    folder.mkdir(parents=True, exist_ok=True)

    # signature.png — PNG transparan
    if signature_rgba is not None:
        cv2.imwrite(str(folder / "signature.png"), signature_rgba)

    # profile.png — nama + ttd (BGR), untuk verifikasi cepat
    if crop_nama is not None or crop_ttd is not None:
        profile = _stack_vertical(crop_nama, crop_ttd)
        cv2.imwrite(str(folder / "profile.png"), profile)

    # metadata.json
    metadata = {
        "no": pegawai["no"],
        "nama": pegawai["nama"],
        "gelar": pegawai["gelar"],
        "nama_file": pegawai["nama_file"],
        "ok_ocr": pegawai["ok_ocr"],
        "sumber": source_image,
        "keterangan": "nama_hasil_ocr_perlu_verifikasi" if not pegawai["ok_ocr"] else "ok",
    }
    (folder / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return folder


def write_summary(rows: List[Dict]) -> Path:
    """Ringkasan seluruh hasil dalam satu CSV + JSON untuk verifikasi cepat."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "ringkasan.csv"
    lines = ["no,folder,nama,gelar,ok_ocr,status"]
    for r in rows:
        lines.append(
            f"{r['no']},{r['folder_name']},{r['nama']},{r['gelar']},{r['ok_ocr']},{r['status']}"
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def collect_unique(output_dir: Path = OUTPUT_DIR) -> Tuple[Path, Path]:
    """Kumpulkan semua PNG hasil ke folder terpusat dengan nama unik.

        output/signatures/signature_Ali_Akbar_S_Kep.png
        output/profiles/profile_Ali_Akbar_S_Kep.png

    Nama file = signature_/profile_ + nama orang (prefix nomor dilepas).
    Berguna saat semua file dikumpulkan ke satu tempat (tempel ke Word/dokumen)
    tanpa tabrakan nama. Mengembalikan (sig_dir, pro_dir).
    """
    import re
    import shutil

    sig_dir = output_dir / "signatures"
    pro_dir = output_dir / "profiles"
    sig_dir.mkdir(exist_ok=True)
    pro_dir.mkdir(exist_ok=True)

    for folder in sorted(p for p in output_dir.iterdir() if p.is_dir()):
        if folder in (sig_dir, pro_dir):
            continue
        # lepas prefix nomor: "01_Ali_Akbar_S_Kep" -> "Ali_Akbar_S_Kep"
        nama = re.sub(r"^\d+_", "", folder.name)
        for sub, ext, dst in (("signature", "signature", sig_dir),
                              ("profile", "profile", pro_dir)):
            src = folder / f"{sub}.png"
            if src.exists():
                shutil.copy2(src, dst / f"{ext}_{nama}.png")
    return sig_dir, pro_dir
