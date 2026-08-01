"""app.py — Signature Management Service: Panel admin TTD (FastAPI).

Arsitektur (keputusan desain — lihat db.py):
  * SQLite (data/database/signatures.db) = source of truth.
  * verifier_index.json = cache utk verifier, dibangun ulang tiap mutasi.
  * Folder per pegawai berbasis pid (signatures/tXX/) — rename tidak
    memindahkan folder; original/ + processed/ + archive/ + metadata.json.
  * id internal = UUID; pid (tXX) utk URL/QR; seq tidak pernah reuse.
  * PUBLIC_BASE_URL dari env (TIDAK hardcode) — siap http & https.
  * QR dibuat on-the-fly (tidak disimpan permanen).
  * Versioning: re-upload = versi baru, versi lama di-archive.
  * Soft delete: status active/inactive (bukan hapus file).
  * Semua aksi tercatat di audit_log.

Endpoint:
    GET  /                       -> halaman admin (HTML)
    GET  /api/list               -> daftar pegawai + status (JSON)
    POST /api/upload             -> tambah 1..N pegawai baru (multipart)
    POST /api/<pid>/edit         -> edit nama/gelar, opsional ganti gambar (versi baru)
    POST /api/<pid>/deactivate   -> soft delete (status inactive)
    POST /api/<pid>/activate     -> aktifkan kembali
    GET  /api/<pid>/qr           -> PNG QR on-the-fly
    GET  /api/<pid>/img          -> PNG tanda tangan (processed terbaru)
    GET  /api/<pid>/orig         -> PNG original (upload terbaru)
    GET  /api/<pid>/json         -> metadata lengkap pegawai
    GET  /api/audit              -> audit log
    GET  /api/reindex            -> bangun ulang cache verifier (diagnostik)
"""
from __future__ import annotations

import io
import json
import logging
import logging.handlers
import os
import re
import traceback
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response

import qrcode
from qrcode.constants import ERROR_CORRECT_M

from process import (TYPE_FONTS, process_image, png_size, render_type_png,
                     trim_transparent)

from db import (_lock, build_cache, connect, create_signature, delete_signature,
                ensure_folders, get_audit, get_signature, get_versions,
                list_signatures, rel_signature, set_status,
                update_profile, update_signature_version, write_metadata,
                signature_dir)

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "http://dev.rsudkotajambi.id/ttd").rstrip("/")

LOGS_DIR = Path(os.environ.get("LOGS_DIR", "/data/logs"))

app = FastAPI(title="Signature Management Service", docs_url=None, redoc_url=None)

# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------
# Log ditulis ke dua tempat:
#   * stdout  -> terlihat via `docker logs ttd-admin`
#   * file    -> /data/logs/admin.log (rotating 2MB x 5), tetap ada walau
#                container restart; bisa dibaca langsung di server maupun
#                lewat halaman admin (GET /api/logs).

_log_configured = False


def setup_logging() -> None:
    global _log_configured
    if _log_configured:
        return
    _log_configured = True
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            LOGS_DIR / "admin.log", maxBytes=2_000_000, backupCount=5,
            encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:  # direktori tidak writable -> tetap jalan via stdout
        root.warning("tidak dapat membuka file log (%s) — hanya stdout", exc)


setup_logging()
log = logging.getLogger("admin")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def slugify(nama: str) -> str:
    """Nama -> slug aman (konsisten dengan pipeline lama). Hanya metadata."""
    s = nama.strip().replace(" ", "_")
    s = re.sub(r"[^A-Za-z0-9_.-]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "pegawai"


def build_qr(url: str) -> bytes:
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M,
                       box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def store_upload(pid: str, png_bytes: bytes, original_bytes: bytes,
                 orig_ext: str, source: str) -> dict[str, str]:
    """Simpan original + processed ke folder signatures/<pid>/.

    Versi saat ini selalu ditulis ke:
        original/upload.<ext>  processed/signature.png
    Jika folder sudah punya versi lama, versi lama di-archive dulu ke
    archive/{kind}_v<N>.<ext> (dipanggil dari handler update).
    """
    folders = ensure_folders(pid)
    orig_name = f"upload.{orig_ext}"
    folders["original"] / orig_name
    (folders["original"] / orig_name).write_bytes(original_bytes)
    (folders["processed"] / "signature.png").write_bytes(png_bytes)
    return {"original": f"signatures/{pid}/original/{orig_name}",
            "processed": rel_signature(pid)}


def archive_previous(pid: str, version: int) -> None:
    """Pindahkan original+processed versi lama ke archive/ (sebelum overwrite)."""
    folders = ensure_folders(pid)
    for kind, filename, ext in (("original", "upload.jpg", "jpg"),
                                ("original", "upload.png", "png"),
                                ("processed", "signature.png", "png")):
        src = folders[kind] / filename
        if not src.exists():
            continue
        dst = folders["archive"] / f"{kind}_v{version}.{ext}"
        if not dst.exists():
            src.rename(dst)


def write_full_metadata(pid: str) -> None:
    """metadata.json folder diperbarui (via db.write_metadata)."""
    write_metadata(pid)


def finalize_upload(pid: str, row_id: str, version: int,
                    png: bytes, original: bytes, ext: str,
                    source: str, method: str) -> tuple[int, int]:
    """Simpan file + perbaiki rel path di DB + cache + metadata (aksi baru/update).

    Mengembalikan (w, h) PNG yang tersimpan.
    """
    paths = store_upload(pid, png, original, ext, source)
    with _lock():
        with connect() as conn:
            conn.execute(
                "UPDATE signatures SET original_rel=?, processed_rel=? WHERE pid=?",
                (paths["original"], paths["processed"], pid))
            conn.execute(
                "UPDATE signature_versions SET original_rel=?, processed_rel=? "
                "WHERE signature_id=? AND version=?",
                (paths["original"], paths["processed"], row_id, version))
    write_full_metadata(pid)
    build_cache()
    return png_size(png)


def _source_label(method: str, source: str) -> str:
    """Label sumber untuk log/audit per metode."""
    if method == "draw":
        return "draw (canvas)"
    if method == "type":
        return f"type ({source})"
    return source


# --------------------------------------------------------------------------
# halaman admin
# --------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="id"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signature Management — Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="static/css/jquery.dataTables.min.css">
<style>
@font-face{font-family:'Dancing Script';src:url('static/fonts/DancingScript.ttf') format('truetype')}
@font-face{font-family:'Great Vibes';src:url('static/fonts/GreatVibes-Regular.ttf') format('truetype')}
@font-face{font-family:'Allura';src:url('static/fonts/Allura-Regular.ttf') format('truetype')}
@font-face{font-family:'Pacifico';src:url('static/fonts/Pacifico-Regular.ttf') format('truetype')}
@font-face{font-family:'Alex Brush';src:url('static/fonts/AlexBrush-Regular.ttf') format('truetype')}
@font-face{font-family:'Satisfy';src:url('static/fonts/Satisfy.ttf') format('truetype')}
@font-face{font-family:'Shadows Into Light';src:url('static/fonts/ShadowsIntoLight.ttf') format('truetype')}
@font-face{font-family:'Kaushan Script';src:url('static/fonts/KaushanScript-Regular.ttf') format('truetype')}
@font-face{font-family:'Sacramento';src:url('static/fonts/Sacramento-Regular.ttf') format('truetype')}
@font-face{font-family:'Parisienne';src:url('static/fonts/Parisienne-Regular.ttf') format('truetype')}
:root{--bg:#f9fafb;--surface:#fff;--border:#e2e8f0;--ink:#0f172a;--ink2:#475569;
--ink3:#94a3b8;--accent:#059669;--accent-soft:rgba(5,150,105,.08);--accent-ink:#047857;
--danger:#dc2626;--danger-soft:rgba(220,38,38,.07);
--shadow:0 20px 40px -15px rgba(15,23,42,.08);
--font:'Outfit',system-ui,sans-serif;--mono:'JetBrains Mono',monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:var(--font);min-height:100dvh;
-webkit-font-smoothing:antialiased;padding:2rem 1rem}
.wrap{max-width:66rem;margin:0 auto}
header{display:flex;align-items:center;justify-content:space-between;gap:1rem;
flex-wrap:wrap;margin-bottom:1.5rem}
.brand{display:flex;align-items:center;gap:.75rem;font-weight:800;letter-spacing:-.01em}
.brand .mark{width:2.2rem;height:2.2rem;border-radius:.7rem;background:var(--ink);
color:#fff;display:grid;place-items:center}
.brand .mark svg{width:1.15rem;height:1.15rem}
.brand .kicker{font-family:var(--mono);font-size:.6rem;color:var(--ink3);
letter-spacing:.18em;text-transform:uppercase;font-weight:500}
.badge{font-family:var(--mono);font-size:.7rem;color:var(--accent-ink);
background:var(--accent-soft);padding:.4rem .8rem;border-radius:999px}
.card{background:var(--surface);border:1px solid rgba(226,232,240,.5);
border-radius:1.75rem;box-shadow:var(--shadow);padding:1.5rem;margin-bottom:1.5rem}
.card h2{font-size:1.05rem;font-weight:700;letter-spacing:-.01em;margin-bottom:.3rem}
.card .desc{color:var(--ink2);font-size:.85rem;margin-bottom:1.25rem;line-height:1.5}
.row{display:grid;grid-template-columns:1fr;gap:.6rem;padding:.9rem 0;
border-top:1px solid var(--border);animation:rise .4s cubic-bezier(.16,1,.3,1) both}
.row:first-of-type{border-top:0}
.fields{display:grid;grid-template-columns:1fr 1fr auto;gap:.6rem}
@media(max-width:640px){.fields{grid-template-columns:1fr}}
input[type=text]{font-family:var(--font);font-size:.9rem;padding:.65rem .85rem;
border:1px solid var(--border);border-radius:.75rem;background:#fcfdfc;color:var(--ink);
outline:none;transition:border-color .2s}
input[type=text]:focus{border-color:var(--accent)}
.rm{border:0;background:transparent;color:var(--ink3);font-size:1.1rem;cursor:pointer;
padding:.3rem;border-radius:.5rem;line-height:1}
.rm:hover{color:#dc2626;background:#fef2f2}
.acts{display:flex;gap:.75rem;margin-top:1.25rem;flex-wrap:wrap}
.btn{font-family:var(--font);font-weight:600;font-size:.88rem;padding:.7rem 1.3rem;
border-radius:999px;border:0;cursor:pointer;display:inline-flex;align-items:center;
gap:.5rem;transition:transform .3s cubic-bezier(.16,1,.3,1),opacity .3s}
.btn:active{transform:scale(.97)}
.btn-primary{background:var(--ink);color:#fff}
.btn-primary:hover{opacity:.92}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--ink2)}
.btn-ghost:hover{border-color:var(--accent);color:var(--accent-ink)}
.btn-danger{background:transparent;border:1px solid var(--border);color:var(--danger)}
.btn-danger:hover{background:var(--danger-soft);border-color:var(--danger)}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.btn-mini{font-size:.72rem;padding:.32rem .7rem;border-radius:999px;border:0;
cursor:pointer;font-family:var(--font);font-weight:600;transition:opacity .2s}
.btn-mini:active{transform:scale(.97)}
.b-off{background:var(--danger-soft);color:var(--danger)}
.b-off:hover{opacity:.85}
.b-up{background:var(--accent-soft);color:var(--accent-ink)}
.b-up:hover{opacity:.85}
.b-on{background:var(--accent);color:#fff}
.b-on:hover{opacity:.85}
.b-del{background:#fef2f2;color:#b91c1c;border:1px solid #fecaca}
.b-del:hover{background:#fee2e2}
.b-edit{background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe}
.b-edit:hover{background:#dbeafe}
.b-draw{background:#fdf4ff;color:#a21caf;border:1px solid #f5d0fe}
.b-type{background:#fffbeb;color:#b45309;border:1px solid #fde68a}
.methods{display:flex;gap:.4rem;flex-wrap:wrap;margin:.4rem 0}
.m-method{padding:.32rem .8rem;border-radius:999px;border:1px solid var(--border);
background:#fff;color:var(--ink2);font-size:.75rem;font-weight:600;cursor:pointer;
font-family:var(--font);transition:all .2s}
.m-method.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.m-panel{display:none;margin-top:.5rem}
.m-panel.on{display:block}
.sig-canvas-wrap{display:grid;gap:.5rem}
.sig-canvas{border:2px dashed var(--border);border-radius:1rem;background:#fff;
touch-action:none;width:100%;height:200px;cursor:crosshair;display:block;
box-sizing:border-box;image-rendering:auto}
.sig-canvas.has-ink{border-style:solid;border-color:var(--accent)}
.dropzone{border:2px dashed var(--border);border-radius:1rem;background:#fcfdfc;
padding:1.1rem 1.25rem;cursor:pointer;display:grid;place-items:center;gap:.3rem;
transition:all .2s;text-align:center}
.dropzone:hover{border-color:var(--accent);background:rgba(5,150,105,.04)}
.dropzone.drag{border-color:var(--accent);background:rgba(5,150,105,.08)}
.dropzone .dz-ico{color:var(--ink3);width:1.4rem;height:1.4rem}
.dropzone .dz-main{font-size:.85rem;font-weight:600;color:var(--ink2)}
.dropzone .dz-sub{font-size:.72rem;color:var(--ink3)}
.dropzone.has-file{border-style:solid;border-color:var(--accent)}
.dropzone .dz-file{display:none;font-family:var(--mono);font-size:.72rem;color:var(--accent-ink);
word-break:break-all;margin-top:.2rem}
.dropzone.has-file .dz-main,.dropzone.has-file .dz-ico{display:none}
.dropzone.has-file .dz-sub{display:none}
.dropzone.has-file .dz-file{display:block}
.type-controls{display:grid;gap:.55rem}
.type-controls input[type=text],.type-controls select{font-family:var(--font);
font-size:.9rem;padding:.6rem .85rem;border:1px solid var(--border);border-radius:.75rem;
background:#fcfdfc;color:var(--ink);outline:none;transition:border-color .2s}
.type-controls input:focus,.type-controls select:focus{border-color:var(--accent)}
.type-prev{border:1px dashed var(--border);border-radius:1rem;background:#fff;
min-height:96px;display:grid;place-items:center;padding:1rem;overflow:hidden}
.type-prev span{color:var(--ink);line-height:1.2;text-align:center;word-break:break-word}
input[type=range]{accent-color:var(--accent)}
.hint{font-size:.7rem;color:var(--ink3);margin-top:.25rem}
/* ---- DataTables ---- */
table.dataTable{font-family:var(--font);border-collapse:separate;border-spacing:0;
width:100%!important}
table.dataTable thead th{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;
color:var(--ink3);font-weight:700;border-bottom:2px solid var(--border);
padding:.65rem .7rem}
table.dataTable thead th.sorting_asc,table.dataTable thead th.sorting_desc{color:var(--accent-ink)}
table.dataTable thead .sorting:after,table.dataTable thead .sorting_asc:after,
table.dataTable thead .sorting_desc:after{opacity:.45}
table.dataTable td{font-size:.82rem;padding:.6rem .7rem;border-bottom:1px solid
rgba(226,232,240,.55);vertical-align:middle}
table.dataTable tbody tr:hover{background:var(--accent-soft)}
table.dataTable tbody tr.even{background:rgba(249,250,251,.6)}
table.dataTable.row-border tbody th,table.dataTable.row-border tbody td,table.dataTable.display tbody th,table.dataTable.display tbody td{border-top:1px solid rgba(226,232,240,.4)}
table.dataTable.stripe tbody tr.odd,table.dataTable.display tbody tr.odd{background:transparent}
.dataTables_wrapper{font-family:var(--font)}
.dataTables_wrapper .dataTables_filter input,.dataTables_wrapper .dataTables_length select{
font-family:var(--font);font-size:.8rem;padding:.42rem .65rem;border:1px solid var(--border);
border-radius:.6rem;background:#fcfdfc;color:var(--ink);outline:none;margin-left:.4rem;
transition:border-color .2s}
.dataTables_wrapper .dataTables_filter input:focus,.dataTables_wrapper .dataTables_length select:focus{border-color:var(--accent)}
.dataTables_wrapper .dataTables_filter label,.dataTables_wrapper .dataTables_length label{font-size:.8rem;color:var(--ink2)}
.dataTables_wrapper .dataTables_filter{color:var(--ink2)}
.dataTables_wrapper .dataTables_info{font-size:.75rem;color:var(--ink3);padding-top:.7rem}
.dataTables_wrapper .dataTables_paginate .paginate_button{font-size:.75rem;padding:.32rem .7rem;
margin:0 .1rem;border:1px solid var(--border)!important;border-radius:.55rem!important;
background:#fff!important;color:var(--ink2)!important;font-family:var(--font)}
.dataTables_wrapper .dataTables_paginate .paginate_button:hover{background:var(--accent-soft)!important;
border-color:var(--accent)!important;color:var(--accent-ink)!important}
.dataTables_wrapper .dataTables_paginate .paginate_button.current{background:var(--ink)!important;
border-color:var(--ink)!important;color:#fff!important}
.dataTables_wrapper .dataTables_paginate .paginate_button.disabled{opacity:.4!important}
.dataTables_wrapper .dataTables_scrollHead table.dataTable thead th{border-bottom:2px solid var(--border)}
dialog{border:0;border-radius:1.25rem;box-shadow:0 25px 60px -15px rgba(15,23,42,.35);
width:min(30rem,92vw);padding:1.5rem;font-family:var(--font);margin:auto;inset:0;
max-height:90dvh;overflow:auto}
dialog::backdrop{background:rgba(15,23,42,.45);backdrop-filter:blur(2px)}
dialog h3{font-size:1.05rem;font-weight:700;margin-bottom:.2rem}
dialog .e-pid{font-family:var(--mono);font-size:.7rem;color:var(--ink3);margin-bottom:1rem}
dialog .fld{display:grid;gap:.35rem;margin-bottom:.9rem}
dialog .fld label{font-size:.78rem;font-weight:600;color:var(--ink2)}
dialog .fld input[type=text]{font-family:var(--font);font-size:.9rem;padding:.65rem .85rem;
border:1px solid var(--border);border-radius:.75rem;background:#fcfdfc;color:var(--ink);
outline:none;transition:border-color .2s}
dialog .fld input[type=text]:focus{border-color:var(--accent)}
dialog .fld input[type=file]{font-size:.8rem;color:var(--ink2)}
dialog .dacts{display:flex;gap:.6rem;justify-content:flex-end;margin-top:1.25rem}
.e-hint{font-size:.72rem;color:var(--ink3);line-height:1.4}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th{font-family:var(--mono);font-size:.62rem;letter-spacing:.14em;text-transform:uppercase;
color:var(--ink3);text-align:left;padding:.5rem .75rem;border-bottom:1px solid var(--border)}
td{padding:.6rem .75rem;border-bottom:1px solid rgba(226,232,240,.5);vertical-align:middle}
td .nm{font-weight:600}
td .sl{font-family:var(--mono);font-size:.66rem;color:var(--ink3)}
td .ver{font-family:var(--mono);font-size:.66rem;color:var(--ink3);
background:#f1f5f9;padding:.15rem .45rem;border-radius:.4rem}
a.pill{font-family:var(--mono);font-size:.66rem;color:var(--accent-ink);
background:var(--accent-soft);padding:.25rem .6rem;border-radius:999px;text-decoration:none}
a.pill:hover{background:rgba(5,150,105,.16)}
.st{font-family:var(--mono);font-size:.62rem;letter-spacing:.08em;text-transform:uppercase;
padding:.2rem .55rem;border-radius:999px;font-weight:600}
.st.on{background:var(--accent-soft);color:var(--accent-ink)}
.st.off{background:var(--danger-soft);color:var(--danger)}
#res{margin-top:1rem;display:none}
#res .item{padding:.7rem .9rem;border-radius:.9rem;margin-bottom:.5rem;font-size:.85rem;
display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap}
#res .ok{background:rgba(5,150,105,.08);color:var(--accent-ink)}
#res .err{background:var(--danger-soft);color:#b91c1c}
#res .link{font-family:var(--mono);font-size:.72rem;color:var(--accent-ink);text-decoration:none}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
</style></head><body><div class="wrap">
<header>
  <div class="brand">
    <span class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l8 3.5v5.5c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V5.5z"/><path d="M9 12l2 2 4-4"/></svg></span>
    <div><span class="kicker">RSUD H. Abdul Manap Kota Jambi</span><br>Signature Management</div>
  </div>
  <span class="badge" id="countBadge">memuat…</span>
</header>

<div class="card">
  <h2>Tambah tanda tangan baru</h2>
  <p class="desc">Isi nama + gelar, pilih <b>jenis</b> (Upload gambar / Draw di
  canvas / Type dengan font tanda tangan), lalu Simpan. Tiap baris diproses
  otomatis, dibuatkan pid (tXX), QR on-the-fly, dan langsung aktif di verifikasi.</p>
  <div id="rows"></div>
  <div class="acts">
    <button class="btn btn-ghost" onclick="addRow()" type="button">+ Tambah baris</button>
    <button class="btn btn-primary" id="uploadBtn" onclick="upload()" type="button">Simpan</button>
  </div>
  <div id="res"></div>
</div>

<div class="card">
  <h2>Arsip tanda tangan</h2>
  <p class="desc">Ganti gambar = versi baru (versi lama otomatis di-archive) — lewat
  tombol <b>edit</b>. Nonaktifkan = soft delete (tetap tersimpan, tidak ditampilkan publik).</p>
  <table id="tbl-list"><thead><tr><th>No</th><th>Nama</th><th>Status</th><th>Jenis</th><th>Versi</th>
  <th>QR</th><th>Tanda tangan</th><th>Aksi</th></tr></thead>
  <tbody id="tbody"></tbody></table>
</div>

<dialog id="editDlg">
  <h3>Edit pegawai</h3>
  <div class="e-pid" id="editPid">—</div>
  <div class="fld"><label for="editNama">Nama lengkap *</label>
    <input type="text" id="editNama" placeholder="Nama pegawai"></div>
  <div class="fld"><label for="editGelar">Gelar (opsional)</label>
    <input type="text" id="editGelar" placeholder="mis. S.Kep, Ns"></div>
  <div class="fld"><label>Ganti tanda tangan (opsional)</label>
    <div class="methods" id="editMethods">
      <button class="m-method" data-m="upload" type="button" onclick="setEditMethod('upload')">Upload</button>
      <button class="m-method" data-m="draw" type="button" onclick="setEditMethod('draw')">Draw</button>
      <button class="m-method" data-m="type" type="button" onclick="setEditMethod('type')">Type</button>
    </div>
    <div class="m-panel" data-m="upload">
      <label class="dropzone">
        <input type="file" id="editFile" accept="image/*" style="display:none">
        <svg class="dz-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>
        <span class="dz-main">Tarik file ke sini atau klik untuk pilih</span>
        <span class="dz-sub">jpg / png — ganti gambar (versi baru)</span>
        <span class="dz-file"></span>
      </label>
      <span class="e-hint" style="display:block;margin-top:.4rem">Kosongkan jika hanya mengubah nama/gelar.</span>
    </div>
    <div class="m-panel" data-m="draw">
      <div class="sig-canvas-wrap">
        <canvas class="sig-canvas" id="editCanvas" height="380" width="1120"></canvas>
        <div class="acts" style="margin-top:.4rem">
          <button class="btn btn-ghost" type="button" onclick="clearEditSig()">Bersihkan</button>
        </div>
        <span class="e-hint">Gambar ulang tanda tangan — versi baru.</span>
      </div>
    </div>
    <div class="m-panel" data-m="type">
      <div class="type-controls">
        <input type="text" id="editTypeText" placeholder="Teks tanda tangan" oninput="updateEditPreview()">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.6rem">
          <select id="editTypeFont" onchange="updateEditPreview()">
            <option value="alex-brush">Alex Brush</option>
            <option value="dancing-script">Dancing Script</option>
            <option value="great-vibes">Great Vibes</option>
            <option value="pacifico">Pacifico</option>
            <option value="satisfy">Satisfy</option>
            <option value="allura">Allura</option>
            <option value="shadows-into-light">Shadows Into Light</option>
            <option value="kaushan-script">Kaushan Script</option>
            <option value="sacramento">Sacramento</option>
            <option value="parisienne">Parisienne</option>
          </select>
          <div style="display:flex;align-items:center;gap:.6rem">
            <input type="range" min="40" max="160" value="96" id="editTypeSize" oninput="updateEditPreview()">
            <span class="t-size-lbl" id="editTypeSizeLbl" style="font-family:var(--mono);font-size:.75rem;color:var(--ink3)">96</span>
          </div>
        </div>
        <div class="type-prev"><span class="t-prev-text" id="editTypePrev"></span></div>
        <span class="e-hint">Ketik teks + font — versi baru.</span>
      </div>
    </div>
    <span class="e-hint" id="editNoImageHint" style="display:none">Kosongkan semua jika hanya mengubah nama/gelar.</span>
  </div>
  <div class="dacts">
    <button class="btn btn-ghost" onclick="document.getElementById('editDlg').close()">Batal</button>
    <button class="btn btn-primary" id="editSave" onclick="saveEdit()">Simpan</button>
  </div>
</dialog>

<div class="card">
  <h2>Audit trail</h2>
  <p class="desc">Riwayat setiap aksi admin (create / edit / activate / deactivate / delete).</p>
  <table id="tbl-audit"><thead><tr><th>Waktu</th><th>Aksi</th><th>Target</th><th>Detail</th></tr></thead>
  <tbody id="auditBody"></tbody></table>
</div>

<div class="card">
  <h2>Log sistem</h2>
  <p class="desc">Baris terakhir dari <span style="font-family:var(--mono);font-size:.75rem">/data/logs/admin.log</span> — untuk memantau error.
  File lengkap juga bisa dibaca di server: <span style="font-family:var(--mono);font-size:.75rem">docker logs ttd-admin</span>.</p>
  <pre id="logBox" style="background:#0f172a;color:#cbd5e1;font-family:var(--mono);font-size:.7rem;
    padding:1rem;border-radius:1rem;overflow:auto;max-height:20rem;line-height:1.55;
    white-space:pre-wrap;word-break:break-word"></pre>
</div>
</div>

<script src="static/js/jquery.min.js"></script>
<script src="static/js/jquery.dataTables.min.js"></script>
<script src="static/js/signature_pad.min.js"></script>
<script>
// BASE otomatis dari path halaman (mis. /ttd-admin saat diakses lewat nginx,
// atau "" saat container diakses langsung) — semua fetch jadi benar di dua mode.
const BASE = location.pathname.replace(/\\/+$/, "");
let rows = 0;
const FONTS = {
  "alex-brush": "'Alex Brush'",
  "dancing-script": "'Dancing Script'",
  "great-vibes": "'Great Vibes'",
  "pacifico": "'Pacifico'",
  "satisfy": "'Satisfy'",
  "allura": "'Allura'",
  "shadows-into-light": "'Shadows Into Light'",
  "kaushan-script": "'Kaushan Script'",
  "sacramento": "'Sacramento'",
  "parisienne": "'Parisienne'"
};
function resizeSigCanvas(canvas) {
  // Sinkronkan ukuran internal canvas dgn ukuran tampil (× devicePixelRatio)
  // supaya koordinat goresan SignaturePad persis sejajar dgn kursor.
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const ratio = Math.max(window.devicePixelRatio || 1, 1);
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  canvas.getContext("2d").setTransform(ratio, 0, 0, ratio, 0, 0);
}
function sigPadInit(canvas) {
  // inisialisasi signature_pad dengan dotSize minimum agar goresan halus
  try {
    resizeSigCanvas(canvas);
    return new SignaturePad(canvas, { penColor: "#1e3a5f",
      minWidth: 1, maxWidth: 3, dotSize: 1.2 });
  } catch (e) { return null; }
}
function bindDropzone(label, input, fileSpan) {
  // klik label -> buka file picker (input hidden)
  label.addEventListener("click", e => { if (e.target !== input) input.click(); });
  input.addEventListener("change", () => {
    const f = input.files[0];
    fileSpan.textContent = f ? f.name : "";
    label.classList.toggle("has-file", !!f);
  });
  // drag & drop
  ["dragenter", "dragover"].forEach(ev => label.addEventListener(ev, e => {
    e.preventDefault(); label.classList.add("drag");
  }));
  ["dragleave", "drop"].forEach(ev => label.addEventListener(ev, e => {
    e.preventDefault(); label.classList.remove("drag");
  }));
  label.addEventListener("drop", e => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) {
      input.files = null;
      // assign file secara manual
      const dt = new DataTransfer();
      dt.items.add(f);
      input.files = dt.files;
      input.dispatchEvent(new Event("change"));
    }
  });
}
function setRowMethod(row, method) {
  row.querySelectorAll(".m-method").forEach(b =>
    b.classList.toggle("on", b.dataset.m === method));
  row.querySelectorAll(".m-panel").forEach(p =>
    p.classList.toggle("on", p.dataset.m === method));
  if (method === "draw") {
    const cv = row.querySelector(".sig-canvas");
    if (cv && !cv.dataset.pad) {
      const pad = sigPadInit(cv);
      if (pad) { cv.dataset.pad = "1"; cv._pad = pad; }
    }
  }
  if (method === "type") updateTypePreview(row);
}
function clearSig(row) {
  const cv = row.querySelector(".sig-canvas");
  if (cv && cv._pad) cv._pad.clear();
  cv.classList.remove("has-ink");
}
function updateTypePreview(row) {
  const txt = row.querySelector(".t-text").value;
  const font = row.querySelector(".t-font").value;
  const size = row.querySelector(".t-size").value;
  const span = row.querySelector(".t-prev-text");
  span.style.fontFamily = FONTS[font] || "serif";
  span.style.fontSize = size + "px";
  span.textContent = txt || "Preview tanda tangan";
}
function addRow(nama="", gelar="", method="upload") {
  rows++;
  const d = document.createElement("div");
  d.className = "row"; d.id = "row" + rows;
  d.innerHTML = `
    <div class="fields">
      <input type="text" placeholder="Nama pegawai *" value="${esc(nama)}" class="i-nama">
      <input type="text" placeholder="Gelar (opsional)" value="${esc(gelar)}" class="i-gelar">
    </div>
    <div class="methods">
      <button class="m-method" data-m="upload" type="button" onclick="setRowMethod(this.closest('.row'),'upload')">Upload</button>
      <button class="m-method" data-m="draw" type="button" onclick="setRowMethod(this.closest('.row'),'draw')">Draw</button>
      <button class="m-method" data-m="type" type="button" onclick="setRowMethod(this.closest('.row'),'type')">Type</button>
    </div>
    <div class="m-panel" data-m="upload">
      <label class="dropzone">
        <input type="file" accept="image/*" class="i-file" style="display:none">
        <svg class="dz-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>
        <span class="dz-main">Tarik file ke sini atau klik untuk pilih</span>
        <span class="dz-sub">jpg / png — tinta otomatis diekstrak</span>
        <span class="dz-file"></span>
      </label>
    </div>
    <div class="m-panel" data-m="draw">
      <div class="sig-canvas-wrap">
        <canvas class="sig-canvas" height="380" width="1120"></canvas>
        <div class="acts" style="margin-top:.4rem">
          <button class="btn btn-ghost" type="button" onclick="clearSig(this.closest('.row'))">Bersihkan</button>
        </div>
        <div class="hint">Tanda tangan di sini — mouse / jari / stylus.</div>
      </div>
    </div>
    <div class="m-panel" data-m="type">
      <div class="type-controls">
        <input type="text" class="t-text" placeholder="Teks tanda tangan (default: nama)" value="${esc(nama)}" oninput="updateTypePreview(this.closest('.row'))">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.6rem">
          <select class="t-font" onchange="updateTypePreview(this.closest('.row'))">
            <option value="alex-brush">Alex Brush</option>
            <option value="dancing-script">Dancing Script</option>
            <option value="great-vibes">Great Vibes</option>
            <option value="pacifico">Pacifico</option>
            <option value="satisfy">Satisfy</option>
            <option value="allura">Allura</option>
            <option value="shadows-into-light">Shadows Into Light</option>
            <option value="kaushan-script">Kaushan Script</option>
            <option value="sacramento">Sacramento</option>
            <option value="parisienne">Parisienne</option>
          </select>
          <div style="display:flex;align-items:center;gap:.6rem">
            <input type="range" min="40" max="160" value="96" class="t-size" oninput="updateTypePreview(this.closest('.row'))">
            <span class="t-size-lbl" style="font-family:var(--mono);font-size:.75rem;color:var(--ink3)">96</span>
          </div>
        </div>
        <div class="type-prev"><span class="t-prev-text"></span></div>
        <div class="hint">Font di-render server (Pillow) menjadi PNG transparan.</div>
      </div>
    </div>
    <button class="rm" onclick="this.closest('.row').remove()" type="button" title="Hapus baris">&times;</button>`;
  d.querySelector(".t-size").addEventListener("input", e => {
    e.target.closest(".row").querySelector(".t-size-lbl").textContent = e.target.value;
  });
  document.getElementById("rows").appendChild(d);
  const dz = d.querySelector(".dropzone");
  if (dz) bindDropzone(dz, d.querySelector(".i-file"), d.querySelector(".dz-file"));
  setRowMethod(d, method);
  d.querySelector(".i-nama").focus();
}
function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
function setBtn(on, txt){const b=document.getElementById("uploadBtn");b.disabled=on;b.textContent=txt||(on?"Memproses…":"Simpan")}
function msg(item, r) {
  return r.ok
    ? `<span><b>${esc(r.nama)}</b> &rarr; ${r.pid} v${r.version} (${r.w}x${r.h})</span>
       <a class="link" href="${r.url}" target="_blank">buka &darr;</a>`
    : `<span>${esc(r.nama || "(tanpa nama)")}</span><span>${esc(r.error || "gagal")}</span>`;
}
function rowToFormData(row) {
  const nama = row.querySelector(".i-nama").value.trim();
  const gelar = row.querySelector(".i-gelar").value.trim();
  const method = [...row.querySelectorAll(".m-method")].find(b => b.classList.contains("on")).dataset.m;
  const fd = new FormData();
  fd.append("nama", nama);
  fd.append("gelar", gelar);
  fd.append("method", method);
  if (method === "draw") {
    const cv = row.querySelector(".sig-canvas");
    if (!cv || !cv._pad) return { fd, nama, method, ok: false, err: "canvas belum siap" };
    if (cv._pad.isEmpty()) return { fd, nama, method, ok: false, err: "tanda tangan belum digambar" };
    return { fd, nama, method, ok: true, cv };
  }
  if (method === "type") {
    fd.append("text", row.querySelector(".t-text").value.trim());
    fd.append("font", row.querySelector(".t-font").value);
    fd.append("size", row.querySelector(".t-size").value);
    return { fd, nama, method, ok: true };
  }
  const f = row.querySelector(".i-file").files[0];
  if (!f) return { fd, nama, method, ok: false, err: "file belum dipilih" };
  fd.append("file", f);
  return { fd, nama, method, ok: true };
}
async function upload() {
  const rowsEl = [...document.querySelectorAll(".row")];
  if (!rowsEl.length) return alert("Tambah minimal satu baris.");
  // validasi: tiap baris wajib nama + payload sesuai metode
  for (const r of rowsEl) {
    const nama = r.querySelector(".i-nama").value.trim();
    if (!nama) return alert("Setiap baris wajib punya nama.");
  }
  setBtn(true);
  const box = document.getElementById("res");
  box.style.display = "block"; box.innerHTML = "";
  let failed = 0;
  for (const row of rowsEl) {
    const item = rowToFormData(row);
    if (!item.ok) {
      failed++;
      const el = document.createElement("div");
      el.className = "item err";
      el.innerHTML = `<span>${esc(item.nama || "(tanpa nama)")}</span><span>${esc(item.err)}</span>`;
      box.appendChild(el);
      continue;
    }
    if (item.method === "draw") {
      // toBlob async — tunggu blob masuk
      const fd = await new Promise(res => {
        const cv = item.cv;
        cv.toBlob(blob => {
          const f = item.fd; f.append("file", blob, "signature.png"); res(f);
        }, "image/png");
      });
      await postSign(fd, item.nama, box);
    } else {
      await postSign(item.fd, item.nama, box);
    }
  }
  setBtn(false);
  document.getElementById("rows").innerHTML = ""; rows = 0;
  loadList(); loadAudit();
  if (failed) alert(failed + " baris gagal — periksa hasil di bawah.");
}
async function postSign(fd, nama, box) {
  try {
    const res = await fetch(BASE + "/api/sign", { method:"POST", body: fd });
    const r = await res.json();
    const el = document.createElement("div");
    el.className = "item " + (r.ok ? "ok" : "err");
    el.innerHTML = msg(el, r);
    box.appendChild(el);
  } catch (e) {
    const el = document.createElement("div");
    el.className = "item err";
    el.innerHTML = `<span>${esc(nama)}</span><span>gagal: ${esc(e.message)}</span>`;
    box.appendChild(el);
  }
}

async function act(pid, action) {
  await fetch(`${BASE}/api/${pid}/${action}`, { method:"POST" });
  loadList(); loadAudit();
}

async function del(pid, nama) {
  if (!confirm(`Hapus permanen ${pid} (${nama})?\n\nData, gambar, dan riwayat versi akan dihapus. Tindakan ini TIDAK bisa dibatalkan.`)) return;
  const res = await fetch(`${BASE}/api/${pid}/delete`, { method:"POST" });
  const r = await res.json();
  if (r.ok) { loadList(); loadAudit(); loadLogs(); }
  else alert("Gagal hapus: " + (r.detail || r.error || "?"));
}

let editPid = null;
let editPad = null;
function setEditMethod(method) {
  document.querySelectorAll("#editMethods .m-method").forEach(b =>
    b.classList.toggle("on", b.dataset.m === method));
  document.querySelectorAll("#editDlg .m-panel").forEach(p =>
    p.classList.toggle("on", p.dataset.m === method));
  if (method === "draw") {
    const cv = document.getElementById("editCanvas");
    if (cv && !cv._pad) {
      const pad = sigPadInit(cv);
      if (pad) { cv._pad = pad; editPad = pad; }
    }
  }
  if (method === "type") updateEditPreview();
  document.getElementById("editNoImageHint").style.display =
    method === "upload" ? "block" : "none";
}
function clearEditSig() {
  const cv = document.getElementById("editCanvas");
  if (cv && cv._pad) cv._pad.clear();
  cv.classList.remove("has-ink");
}
function updateEditPreview() {
  const txt = document.getElementById("editTypeText").value;
  const font = document.getElementById("editTypeFont").value;
  const size = document.getElementById("editTypeSize").value;
  const span = document.getElementById("editTypePrev");
  span.style.fontFamily = FONTS[font] || "serif";
  span.style.fontSize = size + "px";
  span.textContent = txt || "Preview tanda tangan";
  document.getElementById("editTypeSizeLbl").textContent = size;
}
async function editRow(pid) {
  const r = await fetch(`${BASE}/api/list`);
  const d = await r.json();
  const p = d.find(x => x.pid === pid);
  if (!p) return alert("Data tidak ditemukan");
  editPid = pid;
  document.getElementById("editPid").textContent = pid + " · v" + p.version +
    (p.status === "active" ? "" : " · nonaktif");
  document.getElementById("editNama").value = p.nama_display;
  document.getElementById("editGelar").value = p.gelar || "";
  document.getElementById("editFile").value = "";
  const editDz = document.querySelector("#editDlg .dropzone");
  if (editDz) {
    editDz.classList.remove("has-file");
    const fs = editDz.querySelector(".dz-file");
    if (fs) fs.textContent = "";
  }
  const cv = document.getElementById("editCanvas");
  if (cv && cv._pad) cv._pad.clear();
  cv.classList.remove("has-ink");
  document.getElementById("editTypeText").value = p.nama_display;
  document.getElementById("editTypeFont").value = "dancing-script";
  document.getElementById("editTypeSize").value = 96;
  setEditMethod("upload");
  document.getElementById("editSave").disabled = false;
  document.getElementById("editSave").textContent = "Simpan";
  document.getElementById("editDlg").showModal();
  document.getElementById("editNama").focus();
}

async function saveEdit() {
  const nama = document.getElementById("editNama").value.trim();
  const gelar = document.getElementById("editGelar").value.trim();
  if (!nama) return alert("Nama tidak boleh kosong");
  // tentukan metode aktif
  const active = [...document.querySelectorAll("#editMethods .m-method")]
    .find(b => b.classList.contains("on")).dataset.m;
  const btn = document.getElementById("editSave");
  btn.disabled = true; btn.textContent = "Menyimpan…";
  const fd = new FormData();
  fd.append("nama", nama);
  fd.append("gelar", gelar);
  fd.append("method", active);
  let hasImage = false;
  if (active === "upload") {
    const f = document.getElementById("editFile").files[0];
    if (f) { fd.append("file", f); hasImage = true; }
  } else if (active === "draw") {
    const cv = document.getElementById("editCanvas");
    if (cv && cv._pad && !cv._pad.isEmpty()) {
      fd.append("method", "draw");
      const blob = await new Promise(res => cv.toBlob(b => res(b), "image/png"));
      if (blob) { fd.append("file", blob, "signature.png"); hasImage = true; }
    }
  } else {
    const txt = document.getElementById("editTypeText").value.trim();
    if (txt) {
      fd.append("method", "type");
      fd.append("text", txt);
      fd.append("font", document.getElementById("editTypeFont").value);
      fd.append("size", document.getElementById("editTypeSize").value);
      hasImage = true;
    }
  }
  if (!hasImage) fd.append("method", "upload"); // metadata-only
  const res = await fetch(`${BASE}/api/${editPid}/edit`, { method:"POST", body: fd });
  const r = await res.json();
  btn.disabled = false; btn.textContent = "Simpan";
  if (!r.ok) return alert("Gagal: " + (r.error || r.detail || "?"));
  document.getElementById("editDlg").close();
  alert(r.image_changed
    ? `${editPid} → v${r.version} OK (nama/gelar + gambar baru)`
    : `${editPid} OK (nama/gelar diubah)`);
  editPid = null;
  loadList(); loadAudit(); loadLogs();
}

function initListTable() {
  if ($.fn.DataTable.isDataTable("#tbl-list")) {
    $("#tbl-list").DataTable().destroy();
  }
  $("#tbl-list").DataTable({
    pageLength: 25,
    lengthMenu: [[10, 25, 50, 100, -1], [10, 25, 50, 100, "Semua"]],
    order: [[0, "asc"]],
    columnDefs: [{ orderable: false, targets: [5, 6, 7] }],
    language: {
      search: "Cari:",
      lengthMenu: "Tampilkan _MENU_ baris",
      info: "Menampilkan _START_–_END_ dari _TOTAL_ entri",
      infoEmpty: "Tidak ada data",
      infoFiltered: "(disaring dari _MAX_ total)",
      zeroRecords: "Tidak ditemukan",
      emptyTable: "Belum ada data",
      paginate: { first: "«", last: "»", next: "›", previous: "‹" },
      loadingRecords: "Memuat…"
    }
  });
}
function initAuditTable() {
  if ($.fn.DataTable.isDataTable("#tbl-audit")) {
    $("#tbl-audit").DataTable().destroy();
  }
  $("#tbl-audit").DataTable({
    pageLength: 10,
    lengthMenu: [[10, 25, 50, 100], [10, 25, 50, 100]],
    order: [[0, "desc"]],
    language: {
      search: "Cari:",
      lengthMenu: "Tampilkan _MENU_ baris",
      info: "Menampilkan _START_–_END_ dari _TOTAL_ entri",
      infoEmpty: "Tidak ada data",
      infoFiltered: "(disaring dari _MAX_ total)",
      zeroRecords: "Tidak ditemukan",
      paginate: { first: "«", last: "»", next: "›", previous: "‹" }
    }
  });
}
async function loadList() {
  // destroy instance DataTables lama SEBELUM menimpa innerHTML
  if ($.fn.DataTable.isDataTable("#tbl-list")) {
    $("#tbl-list").DataTable().destroy();
  }
  const r = await fetch(BASE + "/api/list");
  const d = await r.json();
  const active = d.filter(p => p.status === "active").length;
  document.getElementById("countBadge").textContent = active + " aktif / " + d.length + " total";
  const tb = document.getElementById("tbody");
  tb.innerHTML = [...d].sort((a,b)=>a.no-b.no).map(p => {
    const st = p.status === "active"
      ? `<span class="st on">aktif</span>`
      : `<span class="st off">nonaktif</span>`;
    const meth = (p.method || "upload") === "upload" ? "upload"
      : (p.method || "") === "draw" ? "draw" : "type";
    const methCls = meth === "draw" ? "b-draw" : meth === "type" ? "b-type" : "b-up";
    return `<tr id="tr-${p.pid}" style="${p.status === 'active' ? '' : 'opacity:.55'}">
    <td style="font-family:var(--mono);font-size:.75rem;color:var(--ink3)">${p.no}</td>
    <td><div class="nm">${esc(p.nama_display)}</div><div class="sl">${p.pid} · ${esc(p.gelar || "—")}</div></td>
    <td>${st}</td>
    <td><span class="btn-mini ${methCls}" style="cursor:default">${meth}</span></td>
    <td><span class="ver">v${p.version}</span></td>
    <td><a class="pill" href="${BASE}/api/${p.pid}/qr" target="_blank">QR</a></td>
    <td><a class="pill" href="${BASE}/api/${p.pid}/img" target="_blank">lihat</a></td>
    <td style="white-space:nowrap">
      <button class="btn-mini b-edit" onclick="editRow('${p.pid}')">edit</button>
      ${p.status === 'active'
        ? `<button class="btn-mini b-off" onclick="act('${p.pid}','deactivate')">nonaktifkan</button>`
        : `<button class="btn-mini b-on" onclick="act('${p.pid}','activate')">aktifkan</button>`}
      <button class="btn-mini b-del" onclick="del('${p.pid}', '${esc(p.nama_display)}')">hapus</button>
    </td></tr>`;
  }).join("");
  initListTable();
}

async function loadAudit() {
  // destroy instance DataTables lama SEBELUM menimpa innerHTML
  if ($.fn.DataTable.isDataTable("#tbl-audit")) {
    $("#tbl-audit").DataTable().destroy();
  }
  const r = await fetch(BASE + "/api/audit?limit=30");
  const d = await r.json();
  const tb = document.getElementById("auditBody");
  tb.innerHTML = d.map(a => `<tr>
    <td style="font-family:var(--mono);font-size:.68rem;color:var(--ink3)">${esc(a.ts)}</td>
    <td style="font-family:var(--mono);font-size:.72rem">${esc(a.action)}</td>
    <td style="font-family:var(--mono);font-size:.72rem;color:var(--accent-ink)">${esc(a.target)}</td>
    <td style="color:var(--ink2);font-size:.8rem">${esc(a.detail)}</td></tr>`).join("");
  initAuditTable();
}

async function loadLogs() {
  const box = document.getElementById("logBox");
  try {
    const r = await fetch(BASE + "/api/logs?lines=150");
    const d = await r.json();
    box.textContent = d.logs && d.logs.length
      ? d.logs.join("\\n") : "(belum ada log)";
  } catch (e) {
    box.textContent = "gagal memuat log: " + e;
  }
}

addRow(); loadList(); loadAudit(); loadLogs();
const editDzLabel = document.querySelector("#editDlg .dropzone");
if (editDzLabel) bindDropzone(editDzLabel, document.getElementById("editFile"),
  editDzLabel.querySelector(".dz-file"));
// saat layar di-resize: sinkronkan ulang ukuran canvas + pertahankan goresan
window.addEventListener("resize", () => {
  document.querySelectorAll(".sig-canvas").forEach(cv => {
    if (!cv._pad) return;
    const hasInk = !cv._pad.isEmpty();
    const data = hasInk ? cv._pad.toData() : null;
    resizeSigCanvas(cv);
    if (hasInk && data) cv._pad.fromData(data);
  });
});
setInterval(loadLogs, 15000);
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/api/list")
def api_list() -> list:
    out = []
    for row in list_signatures():
        out.append({
            "id": row["id"],
            "pid": row["pid"],
            "no": row["seq"],
            "nama": row["slug"],
            "nama_display": row["name"],
            "gelar": row["title"],
            "status": row["status"],
            "version": row["version"],
            "method": row["method"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "url": f"{PUBLIC_BASE_URL}/v/{row['pid']}",
        })
    return out


def _process_payload(method: str, file: UploadFile | None,
                     text: str, font: str, size: int) -> tuple[bytes, bytes, str, str]:
    """Proses payload gambar sesuai metode -> (png, original, ext, source).

    method: upload | draw | type
    """
    if method == "draw":
        raw = file.file.read() if file is not None else b""
        if not raw:
            raise ValueError("canvas kosong — gambar tanda tangan dulu")
        png = trim_transparent(raw)
        if png is None:
            raise ValueError("tanda tangan kosong / tidak valid")
        return png, raw, "png", "draw"
    if method == "type":
        txt = (text or "").strip()
        if not txt:
            raise ValueError("teks tanda tangan kosong")
        if font not in TYPE_FONTS:
            raise ValueError("font tidak dikenal")
        png = render_type_png(txt, font, size)
        if png is None:
            raise ValueError("gagal render teks (font tidak tersedia?)")
        return png, png, "png", font
    # upload
    raw = file.file.read() if file is not None else b""
    if not raw:
        raise ValueError("file kosong")
    png = process_image(raw)
    if png is None:
        raise ValueError("tidak terdeteksi tinta / format tidak dikenali")
    orig_name = (file.filename or "upload.jpg") if file is not None else "upload.jpg"
    ext = orig_name.rsplit(".", 1)[-1].lower() if "." in orig_name else "jpg"
    if ext not in ("jpg", "jpeg", "png"):
        ext = "jpg"
    return png, raw, ext, orig_name


@app.post("/api/sign")
async def api_sign(nama: str = Form(...),
                   gelar: str = Form(default=""),
                   method: str = Form("upload"),
                   file: UploadFile | None = File(default=None),
                   text: str = Form(default=""),
                   font: str = Form(default="dancing-script"),
                   size: int = Form(96)) -> dict:
    """Tambah 1 tanda tangan baru dgn metode upload | draw | type.

    - upload: file gambar (jpg/png) -> process_image (OpenCV)
    - draw:   png dari canvas (blob PNG transparan) -> trim_transparent
    - type:   text + font + size -> render_type_png (Pillow)
    Semua ujungnya signature.png -> pipeline sama.
    """
    method = method.strip().lower() or "upload"
    n = ""
    try:
        n = nama.strip()
        g = gelar.strip()
        if not n:
            raise ValueError("nama kosong")

        png, original, ext, src = _process_payload(method, file, text, font, size)
        label = _source_label(method, src)

        row = create_signature(name=n, title=g, slug=slugify(n),
                               source=label,
                               original_rel="", processed_rel="",
                               method=method)
        pid = row["pid"]
        w, h = finalize_upload(pid, row["id"], 1, png, original, ext, label,
                               method)
        log.info("%s %s | %s | %dx%d | src=%s", method.upper(),
                 pid, n, w, h, label)
        return {"ok": True, "nama": n, "pid": pid, "version": 1,
                "w": w, "h": h, "url": f"{PUBLIC_BASE_URL}/v/{pid}",
                "method": method}
    except Exception as exc:
        log.error("SIGN GAGAL | method=%s | nama=%s | %s\n%s",
                  method, n, exc, traceback.format_exc())
        return {"ok": False, "nama": n,
                "error": str(exc)}


@app.post("/api/upload")
async def api_upload(nama: list[str] = Form(...),
                     gelar: list[str] = Form(default=[]),
                     file: list[UploadFile] = File(...)) -> dict:
    results = []
    for i, (n, f) in enumerate(zip(nama, file)):
        try:
            n = n.strip()
            g = (gelar[i] if i < len(gelar) else "").strip()
            if not n:
                raise ValueError("nama kosong")
            raw = await f.read()
            if not raw:
                raise ValueError("file kosong")

            # deteksi ekstensi dari filename asli (default jpg)
            orig_name = f.filename or "upload.jpg"
            ext = orig_name.rsplit(".", 1)[-1].lower() if "." in orig_name else "jpg"
            if ext not in ("jpg", "jpeg", "png"):
                ext = "jpg"

            png = process_image(raw)
            if png is None:
                raise ValueError("tidak terdeteksi tinta / format tidak dikenali")

            row = create_signature(name=n, title=g, slug=slugify(n),
                                   source=orig_name,
                                   original_rel="", processed_rel="")
            pid = row["pid"]
            paths = store_upload(pid, png, raw, ext, orig_name)
            # perbaiki rel path (file asli belum diketahui saat create)
            with _lock():
                with connect() as conn:
                    conn.execute(
                        "UPDATE signatures SET original_rel=?, processed_rel=? WHERE pid=?",
                        (paths["original"], paths["processed"], pid))
                    conn.execute(
                        "UPDATE signature_versions SET original_rel=?, processed_rel=? "
                        "WHERE signature_id=? AND version=1",
                        (paths["original"], paths["processed"], row["id"]))
            write_full_metadata(pid)
            build_cache()

            w, h = png_size(png)
            results.append({"ok": True, "nama": n, "pid": pid, "version": 1,
                            "w": w, "h": h,
                            "url": f"{PUBLIC_BASE_URL}/v/{pid}"})
            log.info("CREATE %s | %s | %dx%d | src=%s",
                     pid, n, w, h, orig_name)
        except Exception as exc:
            log.error("CREATE GAGAL | nama=%s | %s\n%s",
                      n if 'n' in dir() else "-", exc,
                      traceback.format_exc())
            results.append({"ok": False, "nama": n if 'n' in dir() else "",
                            "error": str(exc)})
    ok_count = sum(1 for r in results if r["ok"])
    log.info("UPLOAD selesai: %d ok / %d gagal dari %d", ok_count,
             len(results) - ok_count, len(results))
    return {"total": len(results), "ok": ok_count,
            "failed": len(results) - ok_count, "results": results}


@app.post("/api/{pid}/edit")
async def api_edit(pid: str,
                   nama: str = Form(...),
                   gelar: str = Form(default=""),
                   method: str = Form(default=""),
                   file: UploadFile | None = File(default=None),
                   text: str = Form(default=""),
                   font: str = Form(default="dancing-script"),
                   size: int = Form(96)) -> dict:
    """Edit metadata (nama + gelar). Jika payload gambar ikut dikirim
    (upload file / draw canvas / type) -> versi baru + archive versi lama."""
    row = get_signature(pid)
    if row is None:
        raise HTTPException(404, "pid tidak ditemukan")
    nama = nama.strip()
    if not nama:
        raise HTTPException(422, "nama tidak boleh kosong")
    gelar = gelar.strip()
    method = (method.strip().lower() or "upload") if method.strip() else ""
    try:
        # deteksi apakah ada payload gambar (file OR draw OR type)
        has_file = file is not None and bool((file.filename or "").strip())
        has_type = method == "type" and (text or "").strip()
        has_draw = method == "draw" and file is not None
        if has_file or has_draw or has_type:
            if has_draw:
                m = "draw"
            elif has_type:
                m = "type"
            else:
                m = "upload"
            png, original, ext, src = _process_payload(m, file, text, font, size)
            label = _source_label(m, src)

            old_ver = row["version"]
            archive_previous(pid, old_ver)
            updated = update_signature_version(
                pid, name=nama, title=gelar, source=label,
                original_rel="", processed_rel="", slug=slugify(nama),
                method=m)
            if updated is None:
                raise HTTPException(500, "gagal memperbarui versi")
            new_ver = updated["version"]
            w, h = finalize_upload(pid, updated["id"], new_ver,
                                   png, original, ext, label, m)
            log.info("EDIT+%s %s -> v%d | %s | %dx%d | src=%s",
                     m.upper(), pid, new_ver, nama, w, h, label)
            return {"ok": True, "pid": pid, "version": new_ver, "w": w, "h": h,
                    "url": f"{PUBLIC_BASE_URL}/v/{pid}", "image_changed": True,
                    "method": m}

        # ---- edit metadata saja (nama + gelar) ----
        updated = update_profile(pid, nama, gelar, slugify(nama))
        if updated is None:
            raise HTTPException(500, "gagal memperbarui profil")
        write_full_metadata(pid)
        build_cache()
        log.info("EDIT %s | %s | gelar: %s", pid, nama, gelar)
        return {"ok": True, "pid": pid, "version": updated["version"],
                "url": f"{PUBLIC_BASE_URL}/v/{pid}", "image_changed": False}
    except HTTPException:
        raise
    except Exception as exc:
        log.error("EDIT GAGAL %s | %s\n%s", pid, exc, traceback.format_exc())
        return {"ok": False, "error": str(exc)}


@app.post("/api/{pid}/deactivate")
def api_deactivate(pid: str) -> dict:
    row = set_status(pid, "inactive")
    if row is None:
        raise HTTPException(404, "pid tidak ditemukan")
    build_cache()
    write_metadata(pid)
    log.info("DEACTIVATE %s | %s", pid, row["name"])
    return {"ok": True, "pid": pid, "status": row["status"]}


@app.post("/api/{pid}/activate")
def api_activate(pid: str) -> dict:
    row = set_status(pid, "active")
    if row is None:
        raise HTTPException(404, "pid tidak ditemukan")
    build_cache()
    write_metadata(pid)
    log.info("ACTIVATE %s | %s", pid, row["name"])
    return {"ok": True, "pid": pid, "status": row["status"]}


@app.post("/api/{pid}/delete")
def api_delete(pid: str) -> dict:
    row = delete_signature(pid)
    if row is None:
        raise HTTPException(404, "pid tidak ditemukan")
    build_cache()
    log.info("DELETE %s | %s (permanen)", pid, row["name"])
    return {"ok": True, "pid": pid, "deleted": True,
            "nama": row["name"]}


@app.get("/api/{pid}/qr")
def api_qr(pid: str) -> Response:
    if get_signature(pid) is None:
        raise HTTPException(404, "pid tidak ditemukan")
    url = f"{PUBLIC_BASE_URL}/v/{pid}"
    return Response(build_qr(url), media_type="image/png",
                    headers={"Content-Disposition": f"inline; filename={pid}.png"})


@app.get("/api/{pid}/img")
def api_img(pid: str) -> Response:
    row = get_signature(pid)
    if row is None:
        raise HTTPException(404, "pid tidak ditemukan")
    path = signature_dir(pid) / "processed" / "signature.png"
    if not path.exists():
        raise HTTPException(404, "gambar tidak ditemukan")
    return Response(path.read_bytes(), media_type="image/png")


@app.get("/api/{pid}/orig")
def api_orig(pid: str) -> Response:
    row = get_signature(pid)
    if row is None:
        raise HTTPException(404, "pid tidak ditemukan")
    if not row["original_rel"]:
        raise HTTPException(404, "original tidak tersedia")
    path = signature_dir(pid) / "original" / Path(row["original_rel"]).name
    if not path.exists():
        raise HTTPException(404, "original tidak ditemukan")
    return Response(path.read_bytes(), media_type="image/png")


@app.get("/api/{pid}/json")
def api_json(pid: str) -> dict:
    row = get_signature(pid)
    if row is None:
        raise HTTPException(404, "pid tidak ditemukan")
    return {**row, "versions": get_versions(pid)}


@app.get("/api/audit")
def api_audit(limit: int = 200) -> list:
    return get_audit(limit)


@app.post("/api/reindex")
def api_reindex() -> dict:
    cache = build_cache()
    log.info("REINDEX cache -> %s", cache)
    return {"ok": True, "cache": str(cache)}


@app.get("/api/logs")
def api_logs(lines: int = 200) -> dict:
    """Baca N baris terakhir file log admin (untuk viewer di halaman)."""
    log_file = LOGS_DIR / "admin.log"
    if not log_file.exists():
        return {"path": str(log_file), "logs": []}
    try:
        content = log_file.read_text(encoding="utf-8", errors="replace")
        tail = content.splitlines()[-int(lines):]
        return {"path": str(log_file), "logs": tail}
    except OSError as exc:
        log.error("gagal baca log file: %s", exc)
        return {"path": str(log_file), "error": str(exc), "logs": []}


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------
# static assets (signature_pad + font preview) — dilindungi Path Traversal
# --------------------------------------------------------------------------

STATIC_DIR = Path(__file__).resolve().parent / "static"
FONTS_DIR = Path(__file__).resolve().parent / "fonts"


@app.get("/static/{kind}/{name}")
def static_file(kind: str, name: str) -> Response:
    """Serve file statis: /static/js/<file>, /static/css/<file>, /static/fonts/<file>.

    Hanya mengizinkan nama file dasar (tanpa path traversal) dari folder
    yang sudah ditentukan.
    """
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise HTTPException(400, "nama file tidak valid")
    if kind == "js":
        base = STATIC_DIR / "js"
        media = "application/javascript"
    elif kind == "css":
        base = STATIC_DIR / "css"
        media = "text/css"
    elif kind == "fonts":
        base = FONTS_DIR
        media = "font/ttf"
    else:
        raise HTTPException(404, "folder statis tidak dikenal")
    path = base / name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "file tidak ditemukan")
    return Response(path.read_bytes(), media_type=media)
