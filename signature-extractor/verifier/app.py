"""app.py - Verifier internal TTD (FastAPI).

Serving data tanda tangan dari /data (output/ pipeline) untuk verifikasi
internal organisasi. QR berisi URL pendek /v/<id>; halaman menampilkan
nama + gambar TTD + status VALID.

Endpoint:
    GET /v/<id>       -> halaman verifikasi HTML
    GET /v/<id>/img   -> PNG tanda tangan (inline)
    GET /v/<id>/json  -> metadata pegawai (untuk integrasi)
    GET /             -> daftar pegawai (ringkas, internal)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

DATA = Path("/data")
INDEX = DATA / "verifier_index.json"

app = FastAPI(title="TTD-OK Verifier", docs_url=None, redoc_url=None)

TITLE = "Verifikasi Tanda Tangan"


def load_index() -> dict:
    if not INDEX.exists():
        raise HTTPException(500, "verifier_index.json belum dibuat — jalankan src/publish_qr.py")
    return json.loads(INDEX.read_text(encoding="utf-8"))


def get_pegawai(payload_id: str) -> dict:
    data = load_index()
    if payload_id not in data:
        raise HTTPException(404, "TTD tidak ditemukan / ID tidak dikenal")
    return data[payload_id]


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    data = load_index()
    rows = "".join(
        f'<li><a href="/v/{k}">{v["nama"]}'
        f'<span class="no">#{v["no"]:02d}</span></a></li>'
        for k, v in sorted(data.items(), key=lambda kv: kv[1]["no"])
    )
    return f"""<!DOCTYPE html><html lang="id"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{TITLE}</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0}}
.container{{max-width:640px;margin:0 auto;padding:20px}}
h1{{font-size:1.3rem;color:#38bdf8;text-align:center}}
.badge{{display:inline-block;background:#1e293b;color:#94a3b8;border-radius:999px;
padding:4px 12px;font-size:.75rem;margin-bottom:16px}}
ul{{list-style:none;padding:0;display:grid;grid-template-columns:1fr 1fr;gap:8px}}
a{{display:flex;justify-content:space-between;background:#1e293b;color:#e2e8f0;
padding:10px 14px;border-radius:10px;text-decoration:none;font-size:.9rem}}
.no{{color:#64748b;font-size:.75rem}}
</style></head><body><div class="container">
<h1>📋 {TITLE}</h1>
<div class="badge">Internal · {len(data)} tanda tangan terdaftar</div>
<ul>{rows}</ul>
</div></body></html>"""


@app.get("/v/{payload_id}", response_class=HTMLResponse)
def verifikasi(payload_id: str) -> str:
    p = get_pegawai(payload_id)
    nama = p.get("nama_display") or p["nama"]
    gelar = p.get("gelar") or ""
    return f"""<!DOCTYPE html><html lang="id"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{nama} — {TITLE}</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;
display:flex;align-items:center;justify-content:center;min-height:100vh}}
.card{{background:#1e293b;border-radius:16px;padding:32px;max-width:420px;width:100%;
text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.4)}}
.ok{{background:#065f46;color:#6ee7b7;border-radius:999px;padding:6px 14px;
display:inline-block;font-size:.8rem;font-weight:700;letter-spacing:.5px}}
.no{{background:#1e293b;color:#64748b;border-radius:999px;padding:6px 14px;
display:inline-block;font-size:.8rem;margin-left:8px}}
.nama{{font-size:1.4rem;font-weight:800;margin:18px 0 4px}}
.gelar{{color:#94a3b8;font-size:.95rem;margin-bottom:20px}}
.ttd-wrap{{background:#fff;border-radius:12px;padding:20px;margin:0 0 18px}}
.ttd{{max-width:100%;max-height:180px;display:block;margin:0 auto}}
.meta{{color:#64748b;font-size:.78rem;line-height:1.7}}
.meta b{{color:#94a3b8}}
.audit{{margin-top:18px;padding-top:14px;border-top:1px solid #334155;
color:#475569;font-size:.7rem}}
</style></head><body><div class="card">
<span class="ok">✓ VALID</span><span class="no">#{p['no']:02d}</span>
<div class="nama">{nama}</div>
<div class="gelar">{gelar}</div>
<div class="ttd-wrap"><img class="ttd" src="/v/{payload_id}/img" alt="Tanda tangan"></div>
<div class="meta">
dokumen: <b>{p.get('sumber', '-')}</b><br>
file: <b>{p.get('png', '-')}</b>
</div>
<div class="audit">verifikasi internal · TTD-OK</div>
</div></body></html>"""


@app.get("/v/{payload_id}/img")
def ttd_img(payload_id: str) -> FileResponse:
    p = get_pegawai(payload_id)
    path = DATA / p["png"]
    if not path.exists():
        raise HTTPException(404, "gambar tanda tangan tidak ditemukan")
    return FileResponse(path, media_type="image/png")


@app.get("/v/{payload_id}/json")
def ttd_json(payload_id: str) -> JSONResponse:
    p = get_pegawai(payload_id)
    return JSONResponse({k: v for k, v in p.items() if k != "png"})


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
