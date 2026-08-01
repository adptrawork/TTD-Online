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

# Upscale & sharpening: crop asli sangat kecil (~100px) karena scan 150 DPI,
# sehingga tampil blur saat diperbesar di halaman verifier. Upscale 3x dengan
# LANCZOS4 + unsharp mask membuat stroke lebih halus dan tajam.
UPSCALE = 3
SHARPEN_AMOUNT = 1.2
SHARPEN_SIGMA = 1.5


def upscale_sharpen(rgba: np.ndarray,
                    factor: int = UPSCALE,
                    amount: float = SHARPEN_AMOUNT,
                    sigma: float = SHARPEN_SIGMA) -> np.ndarray:
    """Upscale RGBA (LANCZOS4) lalu unsharp mask pada channel warna.

    Alpha channel tidak di-sharpen (dipertahankan halus dari feather).
    """
    if factor <= 1:
        return rgba
    h, w = rgba.shape[:2]
    up = cv2.resize(rgba, (w * factor, h * factor),
                    interpolation=cv2.INTER_LANCZOS4)

    rgb = up[:, :, :3].astype(np.float32)
    blurred = cv2.GaussianBlur(rgb, (0, 0), sigmaX=sigma)
    sharp = cv2.addWeighted(rgb, 1.0 + amount, blurred, -amount, 0)
    up[:, :, :3] = np.clip(sharp, 0, 255).astype(np.uint8)
    return up


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
    return upscale_sharpen(rgba)


def save_rgba(rgba: np.ndarray, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), rgba)  # cv2 menulis PNG dengan channel alpha
