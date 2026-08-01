"""process.py - Pemrosesan gambar TTD untuk panel admin upload.

Mengubah file gambar TTD (jpg/png) menjadi PNG transparan (hanya tinta),
di-upscale 3x (LANCZOS4) + unsharp mask agar tajam — hasil yang sama dengan
pipeline signature-extractor.

Output: bytes PNG RGBA transparan + resolusi akhir.
"""
from __future__ import annotations

import io
from typing import Optional

import cv2
import numpy as np

UPSCALE = 3
SHARPEN_AMOUNT = 1.2
SHARPEN_SIGMA = 1.5


def _remove_grid_lines(mask: np.ndarray) -> np.ndarray:
    """Buang garis tabel horizontal/vertikal yang panjang (bukan tinta)."""
    out = mask.copy()
    h, w = mask.shape
    min_len = int(min(h, w) * 0.7)

    horiz = cv2.morphologyEx(out, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (min_len, 1)))
    vert = cv2.morphologyEx(out, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_len)))
    lines = cv2.bitwise_or(horiz, vert)
    lines = cv2.dilate(lines, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    return cv2.bitwise_and(out, cv2.bitwise_not(lines))


def _upscale_sharpen(rgba: np.ndarray,
                     factor: int = UPSCALE,
                     amount: float = SHARPEN_AMOUNT,
                     sigma: float = SHARPEN_SIGMA) -> np.ndarray:
    """Upscale RGBA (LANCZOS4) lalu unsharp mask pada channel warna."""
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


def signature_to_rgba(img_bgr: np.ndarray, feather: int = 2) -> Optional[np.ndarray]:
    """Ubah crop BGR menjadi RGBA transparan (hanya tinta)."""
    if img_bgr is None or img_bgr.size == 0:
        return None

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    bg = float(np.median(gray))
    mask = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 21, 10,
    )
    mask = cv2.bitwise_and(mask, (gray < bg - 20).astype(np.uint8) * 255)
    mask = _remove_grid_lines(mask)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)))
    if int(mask.sum()) < 200:
        return None

    if feather > 0:
        a = cv2.GaussianBlur(mask, (0, 0), sigmaX=feather)
        a = cv2.normalize(a, None, 0, 255, cv2.NORM_MINMAX)
    else:
        a = mask

    rgba = cv2.merge([img_bgr[:, :, 2], img_bgr[:, :, 1], img_bgr[:, :, 0], a])
    return _upscale_sharpen(rgba)


def process_image(data: bytes) -> Optional[bytes]:
    """Terima bytes gambar, kembalikan bytes PNG RGBA transparan (upscaled).

    Mengembalikan None jika tidak terdeteksi tinta.
    """
    arr = np.frombuffer(data, np.uint8)
    img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img_bgr is None:
        return None

    rgba = signature_to_rgba(img_bgr)
    if rgba is None:
        return None

    ok, encoded = cv2.imencode(".png", rgba)
    if not ok:
        return None
    return encoded.tobytes()


def png_size(png_bytes: bytes) -> tuple[int, int]:
    img = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        return (0, 0)
    h, w = img.shape[:2]
    return (w, h)
