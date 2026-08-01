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
    POST /api/<pid>/update       -> upload versi baru TTD utk pegawai existing
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

from process import process_image, png_size

from db import (_lock, build_cache, connect, create_signature, ensure_folders,
                get_audit, get_signature, get_versions, list_signatures,
                rel_signature, set_status, update_signature_version,
                write_metadata, signature_dir)

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
<style>
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
.file{display:flex;align-items:center;gap:.6rem}
.file input[type=file]{font-size:.8rem;color:var(--ink2);max-width:220px}
.file .fname{font-family:var(--mono);font-size:.68rem;color:var(--ink3);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px}
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
.b-up{background:var(--accent-soft);color:var(--accent-ink)}
.b-up:hover{opacity:.85}
.b-off{background:var(--danger-soft);color:var(--danger)}
.b-off:hover{opacity:.85}
.b-on{background:var(--accent);color:#fff}
.b-on:hover{opacity:.85}
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
.upd-file{display:none}
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
  <p class="desc">Isi nama + gelar dan pilih gambar TTD (jpg/png). Bisa tambah
  banyak sekaligus — tiap baris otomatis diproses, dibuatkan pid (tXX) dari
  urutan AUTOINCREMENT, QR dibuat on-the-fly, dan langsung aktif di verifikasi.</p>
  <div id="rows"></div>
  <div class="acts">
    <button class="btn btn-ghost" onclick="addRow()" type="button">+ Tambah baris</button>
    <button class="btn btn-primary" id="uploadBtn" onclick="upload()" type="button">Upload &amp; Daftarkan</button>
  </div>
  <div id="res"></div>
</div>

<div class="card">
  <h2>Arsip tanda tangan</h2>
  <p class="desc">Re-upload = versi baru (versi lama otomatis di-archive).
  Nonaktifkan = soft delete (tetap tersimpan, tidak ditampilkan publik).</p>
  <table><thead><tr><th>No</th><th>Nama</th><th>Status</th><th>Versi</th>
  <th>QR</th><th>Tanda tangan</th><th>Aksi</th></tr></thead>
  <tbody id="tbody"></tbody></table>
</div>

<div class="card">
  <h2>Audit trail</h2>
  <p class="desc">Riwayat setiap aksi admin (create / update / activate / deactivate).</p>
  <table><thead><tr><th>Waktu</th><th>Aksi</th><th>Target</th><th>Detail</th></tr></thead>
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

<script>
const BASE = "";
let rows = 0;
function addRow(nama="", gelar="") {
  rows++;
  const d = document.createElement("div");
  d.className = "row"; d.id = "row" + rows;
  d.innerHTML = `
    <div class="fields">
      <input type="text" placeholder="Nama pegawai *" value="${esc(nama)}" class="i-nama">
      <input type="text" placeholder="Gelar (opsional)" value="${esc(gelar)}" class="i-gelar">
      <div class="file">
        <input type="file" accept="image/*" class="i-file">
        <span class="fname"></span>
      </div>
    </div>
    <button class="rm" onclick="this.closest('.row').remove()" type="button" title="Hapus baris">&times;</button>`;
  d.querySelector(".i-file").addEventListener("change", e => {
    const f = e.target.files[0];
    d.querySelector(".fname").textContent = f ? f.name : "";
  });
  document.getElementById("rows").appendChild(d);
  d.querySelector(".i-nama").focus();
}
function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
function setBtn(on, txt){const b=document.getElementById("uploadBtn");b.disabled=on;b.textContent=txt||(on?"Memproses…":"Upload &amp; Daftarkan")}
function msg(item, r) {
  return r.ok
    ? `<span><b>${esc(r.nama)}</b> &rarr; ${r.pid} v${r.version} (${r.w}x${r.h})</span>
       <a class="link" href="${r.url}" target="_blank">buka &darr;</a>`
    : `<span>${esc(r.nama || "(tanpa nama)")}</span><span>${esc(r.error || "gagal")}</span>`;
}

async function upload() {
  const items = [...document.querySelectorAll(".row")].map(r => ({
    nama: r.querySelector(".i-nama").value.trim(),
    gelar: r.querySelector(".i-gelar").value.trim(),
    file: r.querySelector(".i-file").files[0]
  })).filter(i => i.nama || i.file);
  if (!items.length) return alert("Isi minimal satu baris (nama + file).");
  const bad = items.filter(i => !i.nama || !i.file);
  if (bad.length) return alert("Setiap baris wajib punya nama dan file gambar.");
  setBtn(true);
  const fd = new FormData();
  items.forEach(i => { fd.append("nama", i.nama); fd.append("gelar", i.gelar); fd.append("file", i.file); });
  const res = await fetch(BASE + "/api/upload", { method:"POST", body: fd });
  const data = await res.json();
  setBtn(false);
  const box = document.getElementById("res");
  box.style.display = "block"; box.innerHTML = "";
  (data.results || []).forEach(r => {
    const el = document.createElement("div");
    el.className = "item " + (r.ok ? "ok" : "err");
    el.innerHTML = msg(el, r);
    box.appendChild(el);
  });
  document.getElementById("rows").innerHTML = ""; rows = 0;
  loadList(); loadAudit();
}

async function act(pid, action) {
  await fetch(`${BASE}/api/${pid}/${action}`, { method:"POST" });
  loadList(); loadAudit();
}

function updRow(pid) {
  const tr = document.getElementById("tr-" + pid);
  const file = tr.querySelector(".upd-file");
  if (!file) return;
  file.style.display = (file.style.display === "none" ? "inline-block" : "none");
  if (file.style.display !== "none") file.querySelector("input[type=file]").click();
}
async function upd(pid, input) {
  const f = input.files[0];
  if (!f) { input.closest(".upd-file").style.display = "none"; return; }
  const fd = new FormData();
  fd.append("file", f);
  const res = await fetch(`${BASE}/api/${pid}/update`, { method:"POST", body: fd });
  const r = await res.json();
  alert(r.ok ? `${pid} → v${r.version} OK` : "Gagal: " + (r.error || "?"));
  input.value = "";
  input.closest(".upd-file").style.display = "none";
  loadList(); loadAudit();
}

async function loadList() {
  const r = await fetch(BASE + "/api/list");
  const d = await r.json();
  const active = d.filter(p => p.status === "active").length;
  document.getElementById("countBadge").textContent = active + " aktif / " + d.length + " total";
  const tb = document.getElementById("tbody");
  tb.innerHTML = [...d].sort((a,b)=>a.no-b.no).map(p => {
    const st = p.status === "active"
      ? `<span class="st on">aktif</span>`
      : `<span class="st off">nonaktif</span>`;
    return `<tr id="tr-${p.pid}" style="${p.status === 'active' ? '' : 'opacity:.55'}">
    <td style="font-family:var(--mono);font-size:.75rem;color:var(--ink3)">${p.no}</td>
    <td><div class="nm">${esc(p.nama_display)}</div><div class="sl">${p.pid} · ${esc(p.gelar || "—")}</div></td>
    <td>${st}</td>
    <td><span class="ver">v${p.version}</span></td>
    <td><a class="pill" href="${BASE}/api/${p.pid}/qr" target="_blank">QR</a></td>
    <td><a class="pill" href="${BASE}/api/${p.pid}/img" target="_blank">lihat</a></td>
    <td style="white-space:nowrap">
      <span class="upd-file"><input type="file" accept="image/*" onchange="upd('${p.pid}', this)"></span>
      <button class="btn-mini b-up" onclick="updRow('${p.pid}')">update</button>
      ${p.status === 'active'
        ? `<button class="btn-mini b-off" onclick="act('${p.pid}','deactivate')">nonaktifkan</button>`
        : `<button class="btn-mini b-on" onclick="act('${p.pid}','activate')">aktifkan</button>`}
    </td></tr>`;
  }).join("");
}

async function loadAudit() {
  const r = await fetch(BASE + "/api/audit?limit=30");
  const d = await r.json();
  const tb = document.getElementById("auditBody");
  tb.innerHTML = d.map(a => `<tr>
    <td style="font-family:var(--mono);font-size:.68rem;color:var(--ink3)">${esc(a.ts)}</td>
    <td style="font-family:var(--mono);font-size:.72rem">${esc(a.action)}</td>
    <td style="font-family:var(--mono);font-size:.72rem;color:var(--accent-ink)">${esc(a.target)}</td>
    <td style="color:var(--ink2);font-size:.8rem">${esc(a.detail)}</td></tr>`).join("");
}

async function loadLogs() {
  const box = document.getElementById("logBox");
  try {
    const r = await fetch(BASE + "/api/logs?lines=150");
    const d = await r.json();
    box.textContent = d.logs && d.logs.length
      ? d.logs.join("\n") : "(belum ada log)";
  } catch (e) {
    box.textContent = "gagal memuat log: " + e;
  }
}

addRow(); loadList(); loadAudit(); loadLogs();
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
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "url": f"{PUBLIC_BASE_URL}/v/{row['pid']}",
        })
    return out


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


def _load_upload_png(f: UploadFile, pid: str, source: str) -> tuple[bytes, bytes]:
    """Proses file upload -> (png_bytes, original_bytes)."""
    raw = f.file.read()
    if not raw:
        raise ValueError("file kosong")
    png = process_image(raw)
    if png is None:
        raise ValueError("tidak terdeteksi tinta / format tidak dikenali")
    return png, raw


@app.post("/api/{pid}/update")
async def api_update(pid: str, file: UploadFile = File(...)) -> dict:
    row = get_signature(pid)
    if row is None:
        raise HTTPException(404, "pid tidak ditemukan")
    try:
        raw = await file.read()
        if not raw:
            raise ValueError("file kosong")
        png = process_image(raw)
        if png is None:
            raise ValueError("tidak terdeteksi tinta / format tidak dikenali")

        orig_name = file.filename or "upload.jpg"
        ext = orig_name.rsplit(".", 1)[-1].lower() if "." in orig_name else "jpg"
        if ext not in ("jpg", "jpeg", "png"):
            ext = "jpg"

        old_ver = row["version"]
        # archive versi lama sebelum overwrite
        archive_previous(pid, old_ver)
        updated = update_signature_version(
            pid, name=row["name"], title=row["title"],
            source=orig_name, original_rel="", processed_rel="")
        if updated is None:
            raise HTTPException(500, "gagal memperbarui versi")
        paths = store_upload(pid, png, raw, ext, orig_name)
        new_ver = updated["version"]
        with _lock():
            with connect() as conn:
                conn.execute(
                    "UPDATE signatures SET original_rel=?, processed_rel=? WHERE pid=?",
                    (paths["original"], paths["processed"], pid))
                conn.execute(
                    "UPDATE signature_versions SET original_rel=?, processed_rel=? "
                    "WHERE signature_id=? AND version=?",
                    (paths["original"], paths["processed"], updated["id"], new_ver))
        write_full_metadata(pid)
        build_cache()
        w, h = png_size(png)
        log.info("UPDATE %s -> v%d | %s | %dx%d | src=%s",
                 pid, new_ver, row["name"], w, h, orig_name)
        return {"ok": True, "pid": pid, "version": new_ver, "w": w, "h": h,
                "url": f"{PUBLIC_BASE_URL}/v/{pid}"}
    except HTTPException:
        raise
    except Exception as exc:
        log.error("UPDATE GAGAL %s | %s\n%s", pid, exc, traceback.format_exc())
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
