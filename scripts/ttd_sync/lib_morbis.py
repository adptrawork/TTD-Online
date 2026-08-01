"""lib_morbis.py — modul bersama pipeline sinkronisasi TTD → Morbis.

Berisi: kredensial, login, search pegawai, mapping nama→id_peg (strategi
berlapis), akses API TTD-Online (QR), util HTTP multipart, normalisasi nama.
Semua fungsi stdlib (urllib) — tanpa dependency eksternal.
"""
from __future__ import annotations

import base64
import json
import re
import ssl
import time
import unicodedata
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# KONFIGURASI
# ---------------------------------------------------------------------------
MORBIS_BASE = "http://103.147.236.140"
MORBIS_USER = "irfan"
MORBIS_PASS = "1234"
MORBIS_PATH = "/v2/master-data/ttd-pegawai"

TTD_API = "http://dev.rsudkotajambi.id/ttd-admin"
TTD_USER = "admin"
TTD_PASS = "yXyNGFlaQU6p0t"

DELAY = 0.5          # jeda antar-request Morbis (anti-spam)

# Mapping manual utk kasus ejaan beda / nama tak terdeteksi otomatis.
# key = norm(nama_display TTD-Online), value = id_peg Morbis
OVERRIDES = {
    "AHMADFAUZAN": "2299503",      # di Morbis: AHMAD PAUZAN
}

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/151.0 Safari/537.36")

HTTP_TIMEOUT = 10          # detik per request (server kadang diam)
HTTP_RETRIES = 3           # ulangi bila timeout / koneksi gagal
RETRY_BACKOFF = 2          # detik antar retry


# ---------------------------------------------------------------------------
# util HTTP
# ---------------------------------------------------------------------------
def http_get(url, cookies=None, timeout=HTTP_TIMEOUT, retries=HTTP_RETRIES):
    """GET dengan timeout pendek + retry otomatis (server kadang tidak merespons)."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            if cookies:
                req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()))
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.status, r.read(), dict(r.headers)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(RETRY_BACKOFF * attempt)
    if last_err is None:
        last_err = RuntimeError("request gagal tanpa pengecualian")
    raise last_err


def http_post(url, data=None, files=None, cookies=None, timeout=HTTP_TIMEOUT + 20,
              retries=HTTP_RETRIES):
    """POST multipart/form-data (data + files) dengan retry otomatis."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            boundary = ("----SyncTTD" + base64.b64encode(
                bytes(str(time.time()), "utf-8")).decode()[:20])
            body = b""
            if files:
                for k, (fname, content, ctype) in files.items():
                    body += (f"--{boundary}\r\n"
                             f'Content-Disposition: form-data; name="{k}"; '
                             f'filename="{fname}"\r\n'
                             f"Content-Type: {ctype}\r\n\r\n").encode()
                    body += content + b"\r\n"
            if data:
                for k, v in data.items():
                    body += (f"--{boundary}\r\n"
                             f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                             f"{v}\r\n").encode()
            body += f"--{boundary}--\r\n".encode()
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "User-Agent": UA,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            })
            if cookies:
                req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()))
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.status, r.read(), dict(r.headers)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(RETRY_BACKOFF * attempt)
    if last_err is None:
        last_err = RuntimeError("request gagal tanpa pengecualian")
    raise last_err


def _cookies_from_setcookie(sc):
    out = {}
    if not sc:
        return out
    for part in sc.split(","):
        m = re.search(r"([A-Za-z_]+)=([^;]+)", part)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _get_header(headers, key):
    for k, v in headers.items():
        if k.lower() == key:
            return v
    return None


def morbis_login():
    """Login Morbis, kembalikan dict cookie. Field login_button wajib."""
    _, _, hdr = http_get(MORBIS_BASE + MORBIS_PATH)
    cookies = _cookies_from_setcookie(_get_header(hdr, "set-cookie"))
    _, body2, hdr2 = http_post(
        MORBIS_BASE + MORBIS_PATH,
        data={"username": MORBIS_USER,
              "password": MORBIS_PASS,
              "last_link": f"103.147.236.140:80{MORBIS_PATH}",
              "login_button": "Login"},
        cookies=cookies)
    cookies.update(_cookies_from_setcookie(_get_header(hdr2, "set-cookie")))
    html = body2.decode("utf-8", "replace")
    if 'name="password"' in html:
        raise RuntimeError("Login Morbis GAGAL (form login masih tampil) — cek user/pass.")
    return cookies


def morbis_search(cookies, nama):
    """GET search?opsi=pegawai&nama=... -> list dict [{'ID','NAMA','TTD',...}]"""
    url = MORBIS_BASE + MORBIS_PATH + "/search?opsi=pegawai&nama=" + urllib.parse.quote(nama)
    st, body, _ = http_get(url, cookies)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return []


def ttd_api(path):
    """GET endpoint TTD-Online (Basic Auth) -> (status, bytes)"""
    req = urllib.request.Request(TTD_API + path)
    req.add_header("Authorization", "Basic " + base64.b64encode(
        f"{TTD_USER}:{TTD_PASS}".encode()).decode())
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.status, r.read()


def fetch_qr(pid):
    """Ambil PNG QR dari TTD-Online (None bila gagal)."""
    st, body = ttd_api(f"/api/{pid}/qr")
    if st == 200 and body:
        return body
    return None


# ---------------------------------------------------------------------------
# normalisasi nama
# ---------------------------------------------------------------------------
def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def inti(nama):
    """Nama inti tanpa gelar + hapus prefix gelar depan (dr., Ns., Bdn., apt.)."""
    base = (nama or "").split(",")[0].strip()
    base = re.sub(r"^(dr\.|Ns\.|Prof\.|drg\.|Bdn\.|apt\.)\s+", "", base, flags=re.I)
    return base.strip()


# pola gelar menempel di belakang nama tanpa koma
# (mis. "Ali Akbar S. Kep", "Ade Marliza Amd. Kep")
_GELAR_TRAIL = re.compile(
    r"\s+(?:dr\.?|Ns\.?|drg\.?|Bdn\.?|apt\.?|Prof\.?)?"
    r"\s*[A-Z][A-Za-z]*(?:\s*\.\s*[A-Za-z]*)*\.?\s*$")


def inti_variants(nama):
    """Varian nama utk query: [nama penuh tanpa koma, nama minus trailing gelar]."""
    base = inti(nama)
    stripped = _GELAR_TRAIL.sub("", base).strip()
    out = [base]
    if stripped and stripped != base and len(stripped) >= 3:
        out.append(stripped)
    return out


def morbis_find(cookies, nama_display):
    """Cari id_peg di Morbis utk satu nama. Kembalikan (result, ambiguous_list|None).

    Strategi pencarian (berhenti di match kuat pertama):
      1. query nama penuh (tanpa koma)
      2. query nama minus trailing gelar
      3. fallback: potong stripped dari belakang, min 2 kata
      4. fallback terakhir: kata pertama, EXACT-only (>=4 huruf)
    Aturan terima:
      - exact (norm inti sama)              -> terima
      - substring (norm query >=8, searah)  -> terima kandidat terpanjang
      - >1 kandidat kuat                    -> AMBIGU
    """
    variants = inti_variants(nama_display)
    queries = list(variants)
    stripped = variants[1] if len(variants) > 1 else variants[0]
    words = stripped.split()
    for i in range(len(words) - 1, 1, -1):      # potongan min 2 kata
        q = " ".join(words[:i])
        if q not in queries:
            queries.append(q)

    for q in queries:
        qkey = norm(q)
        try:
            res = morbis_search(cookies, q)
        except Exception:
            return None, None
        time.sleep(DELAY)
        if not res:
            continue
        exact = [r for r in res if norm(inti(r.get("NAMA", ""))) == qkey]
        if exact:
            return exact[0], None
        if len(qkey) >= 8:
            subs = []
            for r in res:
                rk = norm(inti(r.get("NAMA", "")))
                if qkey in rk or rk in qkey:
                    subs.append(r)
            if subs:
                uniq = {}
                for r in subs:
                    uniq.setdefault(norm(inti(r.get("NAMA", ""))), r)
                subs = sorted(uniq.values(), key=lambda x: -len(norm(inti(x.get("NAMA", "")))))
                if len(subs) == 1 or len(set(norm(inti(x.get("NAMA", ""))) for x in subs)) == 1:
                    return subs[0], None
                return None, subs

    # fallback terakhir: kata pertama saja — exact-only, wajib >= 4 huruf
    if len(words) >= 2:
        q1 = words[0]
        if len(q1) >= 4:
            try:
                res = morbis_search(cookies, q1)
            except Exception:
                return None, None
            time.sleep(DELAY)
            q1key = norm(q1)
            exact = [r for r in res if norm(inti(r.get("NAMA", ""))) == q1key]
            if exact:
                return exact[0], None
    return None, None


def nama_file_ttd(nama_display):
    """Nama file QR dari nama inti, mis. qr_Ali_Akbar.png"""
    base = re.sub(r"[^A-Za-z0-9]+", "_", inti(nama_display)).strip("_")
    return ("qr_" + base if base else "qr") + ".png"
