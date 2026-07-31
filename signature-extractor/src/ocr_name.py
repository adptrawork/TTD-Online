"""ocr_name.py - OCR nama pegawai dari cell dan sanitasi untuk nama folder.

Strategi:
    - OCR baris per baris dengan tesseract (bahasa Indonesia).
    - Gabungkan hasil, bersihkan karakter aneh.
    - Pisahkan nama & gelar (S.Kep, A.Md.Kep, Ns., dll) untuk metadata.
    - Hasilkan slug aman untuk nama folder: 01_Ali_Akbar_S_Kep
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

import cv2
import numpy as np
import pytesseract

# Gelar yang umum pada dokumen ini (agar tidak terpotong saat parsing)
GELAR_PATTERN = re.compile(
    r"\b(Ns\.?|S\.Kep|S\.Kep\.?Ns|S\.Tr\.Kep|S\.Farm|A\.Md\.?Kep|A\.Md\.?Farm|"
    r"Amd\.?Kep|Amd\.?Farm|S\.Si|S\.ST|M\.Kes|SKM|S\.KM|dr\.?)\b",
    re.IGNORECASE,
)
# Gelar di awal baris, sebelum nama (mis. "Ns. Rika Afrina", "Bdn. Fitri Aryanti",
# atau tanpa spasi: "Ns.Rika Afrina")
GELAR_DEPAN = re.compile(r"^(Ns\.?|Bdn\.?|Dr\.?)(?=\s|$|[A-Z])", re.IGNORECASE)


def _preprocess_for_ocr(crop_bgr: np.ndarray) -> np.ndarray:
    """Upscale 3x + grayscale (tanpa threshold — tesseract lebih akurat pada
    grayscale asli; threshold merusak stroke tipis)."""
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    if gray.shape[0] < 80:
        gray = cv2.resize(gray, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    else:
        gray = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    return gray


def ocr_text(crop_bgr: np.ndarray, lang: str = "ind") -> str:
    """OCR teks dari crop gambar. Mengembalikan string mentah."""
    prep = _preprocess_for_ocr(crop_bgr)
    raw = pytesseract.image_to_string(prep, lang=lang, config="--psm 6")
    if not raw.strip():
        raw = pytesseract.image_to_string(prep, lang=lang, config="--psm 7")
    return raw


def parse_nama(raw: str) -> Tuple[str, str]:
    """Pisahkan nama dan gelar dari hasil OCR.

    Mengembalikan (nama_clean, gelar). Contoh:
        "Ali Akbar, S.Kep"        -> ("Ali Akbar", "S.Kep")
        "Ns. Rika Afrina, S.Kep"  -> ("Rika Afrina", "Ns., S.Kep")
        "Bdn. Fitri Aryanti"      -> ("Fitri Aryanti", "Bdn.")
    """
    text = raw.replace("|", "").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)

    # gelar depan (Ns., Bdn., Dr.) di awal baris
    gelar_depan: list = []
    while True:
        m = GELAR_DEPAN.match(text)
        if not m:
            break
        gelar_depan.append(m.group(0).strip())
        text = text[m.end():]

    # pecah berdasarkan koma
    parts = re.split(r"[,\n]", text)
    nama_parts, gelar_parts = [], []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if GELAR_PATTERN.search(p):
            gelar_parts.append(p)
        else:
            nama_parts.append(p)

    # bila nama tidak terpisah koma: cek gelar di akhir teks
    nama = " ".join(nama_parts).strip()
    if not nama and text:
        m = re.search(r"(.*?)\s+(" + GELAR_PATTERN.pattern + r".*)$", text, re.IGNORECASE)
        if m:
            nama, gelar_rest = m.group(1), m.group(2)
            return nama.strip(), ", ".join(gelar_depan + [gelar_rest.strip()])

    semua_gelar = ", ".join(gelar_depan + gelar_parts)
    return nama, semua_gelar


def slugify(name: str) -> str:
    """Ubah nama menjadi slug aman untuk folder: 'Ali Akbar S.Kep' -> 'Ali_Akbar_S_Kep'."""
    s = name.strip().replace(".", " ").replace(",", " ").replace("-", " ")
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s


def extract_pegawai(crop_nama: np.ndarray, crop_ttd: np.ndarray, no: int,
                    lang: str = "ind") -> Dict:
    """Pipeline lengkap untuk satu pegawai.

    Mengembalikan dict metadata: no, nama, gelar, nama_file (slug),
    dan apakah OCR berhasil (ok_ocr).
    """
    ok = True
    nama = ""
    gelar = ""
    nama_file = f"Pegawai_{no:02d}"

    if crop_nama is not None and crop_nama.size > 0:
        try:
            raw = ocr_text(crop_nama, lang=lang)
            nama, gelar = parse_nama(raw)
            slug = slugify(f"{nama} {gelar}")
            if slug:
                nama_file = slug
            else:
                ok = False
        except pytesseract.TesseractError as exc:
            print(f"  [warn] OCR gagal cell nama #{no}: {exc}")
            ok = False
    else:
        ok = False

    return {
        "no": no,
        "nama": nama,
        "gelar": gelar,
        "nama_file": nama_file,
        "ok_ocr": ok,
    }
