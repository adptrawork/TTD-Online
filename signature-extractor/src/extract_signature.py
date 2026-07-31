"""extract_signature.py - Konversi crop tanda tangan menjadi PNG transparan.

Output: gambar RGBA dengan background transparan; hanya tinta yang dipertahankan.
Langkah: threshold -> mask -> alpha channel -> feather tepi untuk hasil rapi.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from crop_cells import _remove_grid_lines

# feather radius untuk melembutkan tepi alpha (menghilangkan "halo" putih)
FEATHER = 2


def signature_to_rgba(crop_bgr: np.ndarray,
                      feather: int = FEATHER) -> Optional[np.ndarray]:
    """Ubah crop BGR menjadi RGBA transparan.

    Mengembalikan None jika crop kosong / tidak ada tinta.
    """
    if crop_bgr is None or crop_bgr.size == 0:
        return None

    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    bg = float(np.median(gray))  # background scan (mungkin berbayang)
    # threshold adaptif menangkap tinta tipis sekalipun
    mask = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 21, 10,
    )
    # buang piksel yang lebih terang dari background (kertas) yang lolos
    # threshold — tinta asli selalu lebih gelap dari median background
    mask = cv2.bitwise_and(mask, (gray < bg - 20).astype(np.uint8) * 255)
    # buang garis bingkai tabel yang ikut ter-crop
    mask = _remove_grid_lines(mask)

    # bersihkan noise kecil
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)))
    if int(mask.sum()) < 200:
        return None

    # feather: blur alpha lalu remap agar tepi lembut tanpa memudarkan inti
    if feather > 0:
        a = cv2.GaussianBlur(mask, (0, 0), sigmaX=feather)
        a = cv2.normalize(a, None, 0, 255, cv2.NORM_MINMAX)
    else:
        a = mask

    rgba = cv2.merge([crop_bgr[:, :, 2], crop_bgr[:, :, 1], crop_bgr[:, :, 0], a])
    return rgba


def save_rgba(rgba: np.ndarray, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), rgba)  # cv2 menulis PNG dengan channel alpha
