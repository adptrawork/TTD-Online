"""crop_cells.py - Pemotongan cell dan deteksi bounding box tinta.

Fungsi utama:
    crop_cell(grid, row_band, col, margin)      -> potongan cell (BGR)
    ink_bbox(image_bgr, margin)                 -> bounding box tinta di dalam crop
    crop_ink(grid, row_band, col, margin)       -> crop presisi mengikuti tinta
"""
from __future__ import annotations

from typing import Optional, Tuple

import cv2
import numpy as np

from detect_table import ColumnInfo, GridInfo, cell_bbox

DEFAULT_MARGIN = 20  # px — mencegah tanda tangan tipis terpotong


# --------------------------------------------------------------------------- #
#  Bounding box tinta
# --------------------------------------------------------------------------- #

def _ink_mask(img_bgr: np.ndarray, bg_offset: int = 40) -> np.ndarray:
    """Mask biner: 255 = piksel tinta (gelap), 0 = background.

    Threshold berbasis median background per-cell (adaptif terhadap scan yang
    berbayang / tidak putih murni): tinta = gray < median - bg_offset.
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    bg = float(np.median(gray))
    _, mask = cv2.threshold(gray, max(10.0, bg - bg_offset), 255, cv2.THRESH_BINARY_INV)
    # buang garis bingkai tabel (baris/kolom kontinu penuh tinta)
    mask = _remove_grid_lines(mask)
    return mask


def _remove_grid_lines(mask: np.ndarray, ratio: float = 0.7) -> np.ndarray:
    """Hapus garis bingkai tabel di mana pun dalam mask.

    Garis tabel adalah baris/kolom yang tinta-nya kontinu sepanjang >= ratio
    dari dimensi mask. Penghapusan berbasis proyeksi ini lebih menyeluruh
    daripada hanya menghapus tepi crop, sehingga bbox tinta tidak pernah
    menyentuh garis tabel.
    """
    h, w = mask.shape
    out = mask.copy()
    row_ink = (mask > 0).sum(axis=1)
    for y in np.where(row_ink >= w * ratio)[0]:
        out[y, :] = 0
    col_ink = (mask > 0).sum(axis=0)
    for x in np.where(col_ink >= h * ratio)[0]:
        out[:, x] = 0
    return out


def ink_bbox(img_bgr: np.ndarray, margin: int = DEFAULT_MARGIN) -> Optional[Tuple[int, int, int, int]]:
    """Bounding box tinta (x0, y0, x1, y1) relatif terhadap crop, dengan margin.

    Menggunakan kontur (bukan ukuran sel tetap) agar crop presisi mengikuti
    bentuk tanda tangan / teks. Mengembalikan None jika cell kosong.
    """
    mask = _ink_mask(img_bgr)
    if int(mask.sum()) < 200:  # praktis kosong (noise minimal)
        return None

    # Zero-kan tepi mask: crop cell dimulai dari tengah garis tabel, sehingga
    # 1-3px sisa garis selalu menempel di tepi. Teks tidak pernah sedekat itu.
    pad = 4
    mask[:pad, :] = 0
    mask[-pad:, :] = 0
    mask[:, :pad] = 0
    mask[:, -pad:] = 0

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    xs0, ys0, xs1, ys1 = [], [], [], []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w * h < 16:  # buang noise titik tunggal
            continue
        xs0.append(x); ys0.append(y); xs1.append(x + w); ys1.append(y + h)

    if not xs0:
        return None

    h_img, w_img = img_bgr.shape[:2]
    # margin tidak boleh melewati tepi sel (4px) — garis tabel ada di sana
    x0 = max(4, min(xs0) - margin)
    y0 = max(4, min(ys0) - margin)
    x1 = min(w_img - 4, max(xs1) + margin)
    y1 = min(h_img - 4, max(ys1) + margin)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return x0, y0, x1, y1


# --------------------------------------------------------------------------- #
#  API publik
# --------------------------------------------------------------------------- #

def crop_cell(grid: GridInfo, row_band: Tuple[int, int], col: ColumnInfo,
              margin: int = 0) -> np.ndarray:
    """Potong cell sesuai batas grid (tanpa presisi tinta)."""
    x0, y0, x1, y1 = cell_bbox(grid, row_band, col)
    x0 = max(0, x0 - margin); y0 = max(0, y0 - margin)
    x1 = min(grid.image.shape[1], x1 + margin)
    y1 = min(grid.image.shape[0], y1 + margin)
    return grid.image[y0:y1, x0:x1].copy()


def crop_ink(grid: GridInfo, row_band: Tuple[int, int], col: ColumnInfo,
             margin: int = DEFAULT_MARGIN) -> Optional[np.ndarray]:
    """Potong cell lalu sempitkan ke bounding box tinta (presisi).

    Mengembalikan None jika cell tidak berisi tinta yang berarti.
    """
    cell = crop_cell(grid, row_band, col)
    bbox = ink_bbox(cell, margin=margin)
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    return cell[y0:y1, x0:x1].copy()
