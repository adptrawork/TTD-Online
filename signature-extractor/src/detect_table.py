"""detect_table.py - Deteksi grid tabel, klasifikasi kolom, dan identifikasi baris header.

Output utama:
    GridInfo: kumpulan batas baris (h_lines), batas kolom (v_lines), klasifikasi
    kolom (No/Nama/TTD per blok), dan daftar baris data (bukan header).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np


# --------------------------------------------------------------------------- #
#  Tipe data
# --------------------------------------------------------------------------- #

@dataclass
class ColumnInfo:
    """Satu kolom tabel dengan peran yang sudah diklasifikasi."""
    index: int
    x0: int
    x1: int
    role: str          # "no" | "nama" | "ttd" | "unknown"
    block: int         # 0 = blok kiri, 1 = blok kanan


@dataclass
class GridInfo:
    image_path: Path
    image: np.ndarray
    h_lines: List[int]
    v_lines: List[int]
    columns: List[ColumnInfo]
    header_row_indices: List[int]          # indeks baris yang merupakan header
    data_row_bands: List[Tuple[int, int]]  # [(y0, y1), ...] hanya baris data
    block_bounds: List[Tuple[int, int]]    # [(x0, x1), ...] per blok


# --------------------------------------------------------------------------- #
#  Preprocessing
# --------------------------------------------------------------------------- #

def load_image(path: str | Path) -> np.ndarray:
    img = cv2.imread(str(path))
    if img is None:
        raise FileNotFoundError(f"Tidak dapat membaca gambar: {path}")
    return img


def _binarize(img: np.ndarray) -> np.ndarray:
    """Grayscale + adaptive threshold (binary INV). Lebih tahan terhadap
    pencahayaan tidak merata daripada threshold tetap."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV, 31, 15,
    )


# --------------------------------------------------------------------------- #
#  Deteksi garis tabel
# --------------------------------------------------------------------------- #

def _find_line_positions(open_img: np.ndarray, axis: int, cover_ratio: float = 0.5) -> List[int]:
    """Cari posisi garis dari gambar hasil morphological opening.

    axis=0 → horizontal lines (proyeksi per baris, sumbu y)
    axis=1 → vertical lines (proyeksi per kolom, sumbu x)

    cover_ratio: minimal fraksi panjang yang harus ditutup garis agar dianggap
    garis tabel (bukan coretan pendek).
    """
    h, w = open_img.shape
    if axis == 0:
        proj = open_img.sum(axis=1)
        limit = w * 255 * cover_ratio
    else:
        proj = open_img.sum(axis=0)
        limit = h * 255 * cover_ratio

    lines: List[int] = []
    in_line = False
    start = 0
    for i, v in enumerate(proj):
        if v >= limit and not in_line:
            start = i
            in_line = True
        elif v < limit and in_line:
            lines.append((start + i - 1) // 2)
            in_line = False
    if in_line:
        lines.append((start + len(proj) - 1) // 2)
    return lines


def detect_lines(th: np.ndarray, min_len: int = 40) -> Tuple[List[int], List[int]]:
    """Deteksi semua garis horizontal & vertikal tabel."""
    h = cv2.getStructuringElement(cv2.MORPH_RECT, (min_len, 1))
    v = cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_len))

    h_open = cv2.morphologyEx(th, cv2.MORPH_OPEN, h)
    v_open = cv2.morphologyEx(th, cv2.MORPH_OPEN, v)

    h_lines = _find_line_positions(h_open, axis=0)
    v_lines = _find_line_positions(v_open, axis=1)
    return h_lines, v_lines


# --------------------------------------------------------------------------- #
#  Klasifikasi kolom & blok
# --------------------------------------------------------------------------- #

def _classify_columns(v_lines: List[int]) -> List[ColumnInfo]:
    """Klasifikasi peran kolom berdasarkan lebar.

    Kolom sempit (= kolom nomor) menjadi penanda awal blok; setelahnya
    Nama lalu TTD. Pola tabel dokumen resmi: No | Nama | TTD | No | Nama | TTD.
    """
    widths = [x1 - x0 for x0, x1 in zip(v_lines[:-1], v_lines[1:])]
    if not widths:
        return []
    median_w = float(np.median(widths))
    narrow_thr = max(20.0, median_w * 0.35)

    columns: List[ColumnInfo] = []
    block = -1
    state = "expect_nama"  # state machine: no -> nama -> ttd -> done

    for i, w in enumerate(widths):
        is_narrow = w <= narrow_thr
        if is_narrow:
            block += 1
            role = "no"
            state = "expect_nama"
        elif state == "expect_nama":
            role = "nama"
            state = "expect_ttd"
        elif state == "expect_ttd":
            role = "ttd"
            state = "done"
        else:
            role = "unknown"

        columns.append(ColumnInfo(i, v_lines[i], v_lines[i + 1], role, max(block, 0)))

    return columns


def _classify_rows(h_lines: List[int]) -> Tuple[List[int], List[Tuple[int, int]]]:
    """Pisahkan baris header dari baris data.

    Header (mis. No|Nama|TTD, atau baris judul gabungan seperti "ANASTESI")
    biasanya jauh lebih pendek daripada baris data.
    """
    bands = [(y0, y1) for y0, y1 in zip(h_lines[:-1], h_lines[1:])]
    heights = [y1 - y0 for y0, y1 in bands]
    if not heights:
        return [], bands

    med = float(np.median(heights))
    header_idx = [i for i, hgt in enumerate(heights) if hgt < med * 0.6]
    data_idx = [i for i in range(len(bands)) if i not in header_idx]
    data_bands = [bands[i] for i in data_idx]
    return header_idx, data_bands


# --------------------------------------------------------------------------- #
#  API publik
# --------------------------------------------------------------------------- #

def detect_grid(image_path: str | Path) -> GridInfo:
    img = load_image(image_path)
    th = _binarize(img)
    h_lines, v_lines = detect_lines(th)

    if len(h_lines) < 3 or len(v_lines) < 3:
        raise ValueError(
            f"Grid tabel tidak ditemukan (h={len(h_lines)}, v={len(v_lines)}). "
            "Periksa apakah gambar berisi tabel berbingkai."
        )

    columns = _classify_columns(v_lines)
    header_idx, data_bands = _classify_rows(h_lines)

    # batas blok: dari kolom "no" pertama s/d kolom sebelum "no" berikutnya
    block_bounds: List[Tuple[int, int]] = []
    no_idx = [c.index for c in columns if c.role == "no"]
    for i, ni in enumerate(no_idx):
        end = (no_idx[i + 1] - 1) if i + 1 < len(no_idx) else len(columns) - 1
        block_bounds.append((columns[ni].x0, columns[end].x1))

    return GridInfo(
        image_path=Path(image_path),
        image=img,
        h_lines=h_lines,
        v_lines=v_lines,
        columns=columns,
        header_row_indices=header_idx,
        data_row_bands=data_bands,
        block_bounds=block_bounds,
    )


def cell_bbox(grid: GridInfo, row_band: Tuple[int, int], col: ColumnInfo) -> Tuple[int, int, int, int]:
    """Koordinat absolut sebuah cell: (x0, y0, x1, y1)."""
    y0, y1 = row_band
    return col.x0, y0, col.x1, y1


def summarize(grid: GridInfo) -> str:
    cols = ", ".join(f"c{c.index}:{c.role}" for c in grid.columns)
    return (
        f"Grid: {len(grid.data_row_bands)} baris data x {len(grid.columns)} kolom\n"
        f"  header rows : {grid.header_row_indices}\n"
        f"  blok        : {grid.block_bounds}\n"
        f"  kolom       : {cols}"
    )
