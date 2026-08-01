"""qr_export.py - Ubah tanda tangan menjadi QR Code yang berisi gambar TTD.

Format payload v2 (anti-corruption, 100% ASCII printable):

    payload_bytes = b"TTD1" + len(nama):1 + nama_utf8 + png_grayscale_resized
    payload_qr    = b"T1" + base64(payload_bytes)

Alasan base64 ekstra: scanner (zxing .text, jsQR .data, dll.) mengubah byte
kontrol (0x00-0x1F) di dalam QR byte-mode menjadi simbol teks (mis. 0x0F -> "<SI>"),
yang merusak parsing. Karena base64 hanya berisi A-Za-z0-9+/=, payload_qr selalu
di-decode identik oleh scanner manapun.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import List, Optional, Tuple

import qrcode
from qrcode.constants import ERROR_CORRECT_L
from PIL import Image

MAGIC = b"TTD1"
QR_PREFIX = b"T1"
# payload_bytes maks (sebelum base64): payload_qr = 2 + b64(payload) harus muat
# QR v40 level L (2953 byte). 2100 -> payload_qr ~= 2802, ada margin.
MAX_PAYLOAD = 2100


def _resize_ttd(img: Image.Image, scale: float) -> Image.Image:
    w = max(16, int(img.width * scale))
    h = max(16, int(img.height * scale))
    g = img.resize((w, h), Image.LANCZOS).convert("L")
    # TTD = tinta hitam di atas kertas putih -> threshold biner 1-bit.
    # PNG biner 30-50x lebih kecil dari grayscale/RGBA, jadi QR jauh lebih
    # rendah versinya (mudah di-scan HP) dan TTD bisa ditampilkan sebesar asli.
    return g.point(lambda x: 255 if x >= 180 else 0, mode="1")


def _png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def make_payload(png_bytes: bytes, nama: str) -> bytes:
    """Bangun payload biner: magic + panjang nama + nama + PNG (tanpa base64)."""
    nama_b = nama.encode("utf-8")
    if len(nama_b) > 255:
        nama_b = nama_b[:255]
    return MAGIC + bytes([len(nama_b)]) + nama_b + png_bytes


def encode_qr_data(payload_bytes: bytes) -> bytes:
    """Bungkus payload menjadi ASCII printable agar aman di semua scanner."""
    return QR_PREFIX + base64.b64encode(payload_bytes)


def decode_qr_data(text: str | bytes) -> Optional[dict]:
    """Kebalikan encode_qr_data. Mengembalikan {'nama', 'b64'} atau None."""
    if isinstance(text, bytes):
        text = text.decode("latin-1")
    if not text.startswith(QR_PREFIX.decode("ascii")):
        return None
    try:
        payload = base64.b64decode(text[len(QR_PREFIX):], validate=False)
    except Exception:
        return None
    if not payload.startswith(MAGIC) or len(payload) < 5:
        return None
    nlen = payload[4]
    nama = payload[5:5 + nlen].decode("utf-8", "replace")
    png = payload[5 + nlen:]
    if not png:
        return None
    return {"nama": nama, "b64": base64.b64encode(png).decode("ascii")}


def signature_to_qr(png_path: str | Path, nama: str,
                    max_payload: int = MAX_PAYLOAD) -> Tuple[bytes, Tuple[int, int]]:
    """Baca PNG tanda tangan, kompres hingga muat payload, encode ke QR.

    Mengembalikan (bytes_gambar_qr, (lebar, tinggi) resolusi TTD akhir).
    """
    img = Image.open(png_path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    scale = 1.0
    while scale >= 0.15:
        small = _resize_ttd(img, scale)
        png = _png_bytes(small)
        payload = make_payload(png, nama)
        if len(payload) <= max_payload:
            data_qr = encode_qr_data(payload)
            qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_L,
                               box_size=8, border=2)
            qr.add_data(data_qr.decode("ascii"), optimize=0)
            try:
                qr.make(fit=True)
            except ValueError:
                scale -= 0.05  # data tetap terlalu besar -> coba lebih kecil
                continue
            img_qr = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img_qr.save(buf, "PNG")
            return buf.getvalue(), small.size
        scale -= 0.05

    raise ValueError(f"TTD tidak bisa dikompres cukup kecil: {png_path}")


def generate_all(output_dir: Path) -> Tuple[Path, List[dict]]:
    """Generate QR untuk semua folder pegawai di output/.

    Mengembalikan (qr_dir, ringkasan tiap pegawai).
    """
    import re
    import shutil

    qr_dir = output_dir / "qrcodes"
    qr_dir.mkdir(exist_ok=True)

    rows = []
    for folder in sorted(p for p in output_dir.iterdir() if p.is_dir()):
        if folder.name in ("qrcodes", "signatures", "profiles"):
            continue
        sig = folder / "signature.png"
        if not sig.exists():
            rows.append({"nama": folder.name, "status": "tanpa_ttd", "file": ""})
            continue
        nama = re.sub(r"^\d+_", "", folder.name)
        try:
            data, (w, h) = signature_to_qr(sig, nama)
            out = qr_dir / f"qr_{nama}.png"
            out.write_bytes(data)
            rows.append({"nama": nama, "status": "ok", "file": out.name,
                         "ttd_res": f"{w}x{h}"})
        except ValueError as exc:
            rows.append({"nama": nama, "status": "gagal", "file": "", "err": str(exc)})

    return qr_dir, rows
