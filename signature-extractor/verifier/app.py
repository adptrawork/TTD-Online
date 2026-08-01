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

import html
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

DATA = Path("/data")
INDEX = DATA / "verifier_index.json"

app = FastAPI(title="TTD-OK Verifier", docs_url=None, redoc_url=None)

TITLE = "Verifikasi Tanda Tangan"

# --------------------------------------------------------------------------
# Desain: baseline DESIGN_VARIANCE=8 / MOTION_INTENSITY=6 / VISUAL_DENSITY=4
# Light #f9fafb, satu accent emerald (desaturate), Outfit + JetBrains Mono.
# Tanpa emoji — semua ikon SVG inline.
# --------------------------------------------------------------------------

CSS = """
:root{
  --bg:#f9fafb; --surface:#ffffff; --border:#e2e8f0; --border-soft:rgba(226,232,240,.5);
  --ink:#0f172a; --ink-2:#475569; --ink-3:#94a3b8;
  --accent:#059669; --accent-soft:rgba(5,150,105,.08); --accent-ink:#047857;
  --shadow:0 20px 40px -15px rgba(15,23,42,.08);
  --font:'Outfit',system-ui,-apple-system,'Segoe UI',sans-serif;
  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--ink);font-family:var(--font);
  min-height:100dvh;display:flex;flex-direction:column;
  -webkit-font-smoothing:antialiased}
.shell{flex:1;width:100%;max-width:56rem;margin:0 auto;padding:1.5rem 1rem 0}

/* ---------- brand ---------- */
.brand{display:flex;align-items:center;gap:.75rem;font-weight:700;
  letter-spacing:-.01em;font-size:1.05rem}
.brand .mark{width:2rem;height:2rem;border-radius:.7rem;display:grid;place-items:center;
  background:var(--ink);color:#fff;flex:none}
.brand .mark svg{width:1.1rem;height:1.1rem}
.brand .kicker{font-family:var(--mono);font-size:.6rem;color:var(--ink-3);
  font-weight:500;letter-spacing:.18em;text-transform:uppercase}
.brand b{font-weight:800}
.brand .sub{color:var(--ink-2);font-weight:400;font-size:.85rem}

/* ---------- kartu hasil ---------- */
.result{margin-top:1.75rem;background:var(--surface);border:1px solid var(--border-soft);
  border-radius:2.25rem;box-shadow:var(--shadow);padding:1.75rem 1.25rem;
  animation:rise .55s cubic-bezier(.16,1,.3,1) both}
.result-head{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem}
.status{display:inline-flex;align-items:center;gap:.5rem;padding:.4rem .85rem;
  border-radius:999px;background:var(--accent-soft);color:var(--accent-ink);
  font-weight:700;font-size:.72rem;letter-spacing:.06em;text-transform:uppercase}
.status .dot{position:relative;width:.45rem;height:.45rem;border-radius:50%;
  background:var(--accent)}
.status .dot::after{content:"";position:absolute;inset:0;border-radius:50%;
  background:var(--accent);animation:ping 2.2s cubic-bezier(.16,1,.3,1) infinite}
.no{font-family:var(--mono);font-size:.7rem;color:var(--ink-3);
  border:1px solid var(--border);padding:.32rem .6rem;border-radius:.5rem;white-space:nowrap}
.person{margin-top:1.5rem}
.person .nama{font-size:clamp(1.6rem,6vw,2.2rem);font-weight:800;
  letter-spacing:-.03em;line-height:1.05}
.person .gelar{color:var(--ink-2);font-size:1rem;margin-top:.35rem}
.sig{margin-top:1.75rem;background:#fcfdfc;border:1px solid var(--border);
  border-radius:1.5rem;padding:1.5rem;display:grid;place-items:center;
  position:relative;overflow:hidden;animation:rise .55s cubic-bezier(.16,1,.3,1) .12s both}
.sig::before{content:"";position:absolute;inset:0;
  background:radial-gradient(60% 80% at 50% 0%,rgba(226,232,240,.5),transparent 70%);
  pointer-events:none}
.sig img{max-width:100%;max-height:190px;display:block;position:relative;
  animation:sign 1s cubic-bezier(.16,1,.3,1) .25s both}
.sig .sig-label{position:absolute;bottom:.6rem;left:50%;transform:translateX(-50%);
  font-family:var(--mono);font-size:.55rem;color:var(--ink-3);
  letter-spacing:.14em;text-transform:uppercase;white-space:nowrap}
.meta{margin-top:1.5rem;border-top:1px solid var(--border)}
.meta-row{display:flex;justify-content:space-between;gap:1rem;
  padding:.85rem .1rem;border-bottom:1px solid var(--border-soft);
  animation:rise .5s cubic-bezier(.16,1,.3,1) both}
.meta-row:last-child{border-bottom:0}
.meta-row span{color:var(--ink-3);font-size:.72rem;letter-spacing:.05em;
  text-transform:uppercase;flex:none}
.meta-row b{font-family:var(--mono);font-weight:500;font-size:.72rem;
  color:var(--ink);text-align:right;word-break:break-all}

/* ---------- daftar (index) ---------- */
.dash{display:grid;grid-template-columns:1fr;gap:2rem;margin-top:1.75rem}
.side .hi{font-size:clamp(1.7rem,6vw,2.4rem);font-weight:800;letter-spacing:-.03em;
  line-height:1.08}
.side p{color:var(--ink-2);margin-top:.75rem;max-width:34ch;line-height:1.55;font-size:.95rem}
.stats{display:flex;gap:2rem;margin-top:1.75rem}
.stat .v{font-family:var(--mono);font-size:1.6rem;font-weight:600;letter-spacing:-.02em}
.stat .l{font-family:var(--mono);font-size:.62rem;color:var(--ink-3);
  letter-spacing:.14em;text-transform:uppercase}
.list{border:1px solid var(--border-soft);border-radius:1.75rem;background:var(--surface);
  box-shadow:var(--shadow);overflow:hidden}
.list .lhead{padding:1rem 1.25rem;border-bottom:1px solid var(--border);
  font-family:var(--mono);font-size:.62rem;color:var(--ink-3);
  letter-spacing:.16em;text-transform:uppercase;
  display:flex;justify-content:space-between;align-items:center}
.list .lhead b{color:var(--accent-ink);font-weight:600}
.lrow{display:flex;align-items:center;gap:.9rem;padding:.8rem 1.25rem;
  border-bottom:1px solid var(--border-soft);text-decoration:none;color:inherit;
  animation:rise .5s cubic-bezier(.16,1,.3,1) both}
.lrow:last-child{border-bottom:0}
.lrow:active{transform:scale(.985)}
.lrow .ln{font-family:var(--mono);font-size:.68rem;color:var(--ink-3);flex:none;width:2.2rem}
.lrow .lnm{flex:1;min-width:0}
.lrow .lnm b{display:block;font-weight:600;font-size:.92rem;letter-spacing:-.01em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lrow .lnm small{display:block;color:var(--ink-3);font-size:.72rem;margin-top:.1rem}
.lrow .arr{flex:none;color:var(--ink-3);transition:transform .3s cubic-bezier(.16,1,.3,1)}
.lrow:hover .arr{transform:translateX(3px);color:var(--accent)}
.lrow:hover .lnm b{color:var(--accent-ink)}

/* ---------- error 404 ---------- */
.err{flex:1;display:grid;place-items:center;padding:2rem 1rem}
.err-inner{max-width:24rem;text-align:center;animation:rise .55s cubic-bezier(.16,1,.3,1) both}
.err .mark{width:3.5rem;height:3.5rem;margin:0 auto;border-radius:1.1rem;
  background:var(--accent-soft);color:var(--accent-ink);display:grid;place-items:center}
.err .mark svg{width:1.6rem;height:1.6rem}
.err h1{font-size:1.5rem;font-weight:800;letter-spacing:-.02em;margin-top:1.25rem}
.err p{color:var(--ink-2);margin-top:.6rem;font-size:.92rem;line-height:1.55}
.err code{font-family:var(--mono);font-size:.75rem;color:var(--ink);
  background:#f1f5f9;padding:.15rem .45rem;border-radius:.4rem}
.btn{display:inline-flex;align-items:center;gap:.5rem;margin-top:1.5rem;
  background:var(--ink);color:#fff;font-weight:600;font-size:.85rem;
  padding:.7rem 1.2rem;border-radius:999px;text-decoration:none;
  transition:transform .3s cubic-bezier(.16,1,.3,1),opacity .3s}
.btn:active{transform:scale(.97)}
.btn:hover{opacity:.92}
.btn svg{width:.95rem;height:.95rem}

/* ---------- footer ---------- */
.foot{padding:1.5rem 1rem 2.5rem;width:100%;max-width:56rem;margin:0 auto}
.foot-inner{border-top:1px solid var(--border);padding-top:1rem;
  display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;
  font-family:var(--mono);font-size:.6rem;color:var(--ink-3);
  letter-spacing:.12em;text-transform:uppercase}
.foot-inner .l{display:flex;align-items:center;gap:.5rem}
.foot-inner .l svg{width:.8rem;height:.8rem;color:var(--accent)}

/* ---------- motion ---------- */
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes sign{0%{opacity:0;transform:translateY(10px) scale(.985)}
  100%{opacity:1;transform:none}}
@keyframes ping{0%{transform:scale(1);opacity:.55}
  70%{transform:scale(2.6);opacity:0}100%{transform:scale(2.6);opacity:0}}

@media (min-width:48rem){
  .shell{padding:2.25rem 2rem 0}
  .result{padding:2.5rem;border-radius:2.75rem}
  .dash{grid-template-columns:1fr 1.7fr;gap:3rem;align-items:start}
  .sig img{max-height:220px}
}
"""

ICON_SHIELD = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l8 3.5v5.5c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V5.5z"/><path d="M9 12l2 2 4-4"/></svg>'

ICON_LOCK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10" width="16" height="11" rx="2.5"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>'

ICON_ARROW = '<svg class="arr" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>'

ICON_BACK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M11 18l-6-6 6-6"/></svg>'

ICON_CIRCLE_X = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.5"/><path d="M9 9l6 6M15 9l-6 6"/></svg>'


def page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
{body}
<footer class="foot"><div class="foot-inner">
  <span class="l">{ICON_LOCK} verifikasi internal &middot; TTD-OK</span>
  <span>arsip tanda tangan digital</span>
</div></footer>
</body></html>"""


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
    rows = []
    for i, (k, v) in enumerate(sorted(data.items(), key=lambda kv: kv[1]["no"])):
        nama = v.get("nama_display") or v["nama"]
        gelar = v.get("gelar") or "pegawai"
        rows.append(
            f'<a class="lrow" style="animation-delay:{i * 45}ms" href="/v/{html.escape(k)}">'
            f'<span class="ln">#{v["no"]:02d}</span>'
            f'<span class="lnm"><b>{html.escape(nama)}</b>'
            f'<small>{html.escape(gelar)}</small></span>'
            f"{ICON_ARROW}</a>"
        )
    body = f"""<div class="shell">
<header class="brand">
  <span class="mark">{ICON_SHIELD}</span>
  <div><span class="kicker">RSUD H. ABDUL MANAP KOTA JAMBI</span><br><b>{TITLE}</b></div>
</header>

<div class="dash">
  <section class="side" style="animation:rise .55s cubic-bezier(.16,1,.3,1) both">
    <h1 class="hi">Direktori tanda tangan terverifikasi</h1>
    <p>Daftar tanda tangan resmi yang dikelola unit TTD. Buka salah satu
    entri untuk melihat bukti verifikasi beserta citra tanda tangan.</p>
    <div class="stats">
      <div class="stat"><div class="v">{len(data)}</div><div class="l">Pegawai</div></div>
      <div class="stat"><div class="v">{"%.0f" % (len(data) and 100)}%</div><div class="l">Terdaftar</div></div>
    </div>
  </section>
  <section class="list">
    <div class="lhead"><span>Arsip</span><b>terverifikasi</b></div>
    {''.join(rows)}
  </section>
</div>
</div>"""
    return page(f"{TITLE} · RSUD H. Abdul Manap Kota Jambi", body)


@app.get("/v/{payload_id}", response_class=HTMLResponse)
def verifikasi(payload_id: str) -> str:
    p = get_pegawai(payload_id)
    nama = p.get("nama_display") or p["nama"]
    gelar = p.get("gelar") or ""
    body = f"""<div class="shell">
<header class="brand" style="animation:rise .5s cubic-bezier(.16,1,.3,1) both">
  <span class="mark">{ICON_SHIELD}</span>
  <div><span class="kicker">RSUD H. ABDUL MANAP KOTA JAMBI</span><br><b>{TITLE}</b></div>
</header>

<main class="result">
  <div class="result-head">
    <span class="status"><span class="dot"></span>Valid</span>
    <span class="no">#{p['no']:02d}</span>
  </div>
  <div class="person">
    <div class="nama">{html.escape(nama)}</div>
    {f'<div class="gelar">{html.escape(gelar)}</div>' if gelar else ''}
  </div>
  <div class="sig">
    <img src="{html.escape(payload_id)}/img" alt="Citra tanda tangan {html.escape(nama)}">
    <span class="sig-label">Tanda tangan digital</span>
  </div>
  <div class="meta">
    <div class="meta-row" style="animation-delay:.2s"><span>Berkas arsip</span><b>{html.escape(str(p.get('png', '-'))) }</b></div>
    <div class="meta-row" style="animation-delay:.25s"><span>Status</span><b>AKTIF &middot; terverifikasi</b></div>
  </div>
</main>
</div>"""
    return page(f"{nama} — {TITLE}", body)


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException) -> HTMLResponse | JSONResponse:
    if exc.status_code == 404 and request.url.path.startswith("/v/"):
        body = f"""<div class="shell"><main class="err"><div class="err-inner">
<div class="mark">{ICON_CIRCLE_X}</div>
<h1>Tanda tangan tidak ditemukan</h1>
<p>ID <code>{html.escape(request.path_params.get('payload_id', '-'))}</code>
tidak terdaftar di arsip verifikasi, atau tautan yang dipindai sudah tidak
berlaku. Periksa kembali QR / tautan yang digunakan.</p>
<a class="btn" href="/">{ICON_BACK} Kembali ke direktori</a>
</div></main></div>"""
        return HTMLResponse(page(f"Tidak ditemukan — {TITLE}", body), status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


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
