"""db.py — SQLite layer untuk Signature Management Service (TTD RSUD).

Prinsip (sesuai keputusan desain):
  * SQLite (data/database/signatures.db) adalah source of truth.
  * data/cache/verifier_index.json hanyalah cache untuk verifier — dibangun
    ulang (atomik) setiap kali ada mutasi agar verifier tetap cepat.
  * Folder per pegawai berbasis pid (tXX), BUKAN nama/slug, sehingga rename /
    update nama tidak memindahkan folder:
        signatures/t01/original/upload.jpg        (original, versi terbaru)
        signatures/t01/processed/signature.png   (hasil proses, versi terbaru)
        signatures/t01/archive/original_v1.jpg   (versi lama, saat update)
        signatures/t01/archive/signature_v1.png
        signatures/t01/metadata.json             (ringkasan human-readable)
  * UUID = id internal; pid (tXX) = identitas pendek utk URL/QR; seq = urutan
    AUTOINCREMENT yang TIDAK pernah dipakai ulang (soft-delete tidak reuse).
  * Semua mutasi dilindungi file lock (fcntl.flock) + transaksi SQLite.
  * Setiap aksi admin dicatat di tabel audit_log (audit trail).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data"))
DB_DIR = DATA_ROOT / "database"
DB_PATH = DB_DIR / "signatures.db"
CACHE_DIR = DATA_ROOT / "cache"
CACHE_PATH = CACHE_DIR / "verifier_index.json"
SIGNATURES_DIR = DATA_ROOT / "signatures"
LOCK_PATH = DB_DIR / ".lock"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signatures (
    id             TEXT PRIMARY KEY,
    seq            INTEGER NOT NULL UNIQUE,
    pid            TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    slug           TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active',
    version        INTEGER NOT NULL DEFAULT 1,
    source         TEXT NOT NULL DEFAULT '',
    original_rel   TEXT NOT NULL DEFAULT '',
    processed_rel  TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signature_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    signature_id   TEXT NOT NULL REFERENCES signatures(id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    original_rel   TEXT NOT NULL DEFAULT '',
    processed_rel  TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    UNIQUE(signature_id, version)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor    TEXT NOT NULL DEFAULT 'admin',
    action   TEXT NOT NULL,
    target   TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
"""


# --------------------------------------------------------------------------
# helpers dasar
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_uuid() -> str:
    return uuid.uuid4().hex


def connect() -> "sqlite3.Connection":
    """Koneksi SQLite dengan pengaturan aman untuk ro-reader (verifier)."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    # DELETE (bukan WAL) agar DB bisa dibaca aman dari mount :ro (verifier).
    conn.execute("PRAGMA journal_mode = DELETE")
    return conn


def init_db() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SIGNATURES_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


class _lock:
    """Context manager file lock — mencegah race antar proses admin/cron."""

    def __enter__(self):
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(LOCK_PATH, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        return False


# --------------------------------------------------------------------------
# CRUD signature
# --------------------------------------------------------------------------

def _next_seq(conn) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM signatures").fetchone()
    return int(row["n"])


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def create_signature(name: str, title: str, slug: str, source: str,
                     original_rel: str, processed_rel: str) -> dict:
    """Buat entri baru. pid dialokasikan dari urutan AUTOINCREMENT (tidak reuse)."""
    init_db()
    with _lock():
        with connect() as conn:
            ts = now_iso()
            seq = _next_seq(conn)
            pid = f"t{seq:02d}"
            sid = new_uuid()
            conn.execute(
                """INSERT INTO signatures
                   (id, seq, pid, name, title, slug, status, version,
                    source, original_rel, processed_rel, created_at, updated_at)
                   VALUES (?,?,?,?,?,?, 'active', 1, ?,?,?,?,?)""",
                (sid, seq, pid, name, title, slug, source,
                 original_rel, processed_rel, ts, ts),
            )
            conn.execute(
                """INSERT INTO signature_versions
                   (signature_id, version, original_rel, processed_rel, source, created_at)
                   VALUES (?,1,?,?,?,?)""",
                (sid, original_rel, processed_rel, source, ts),
            )
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                (ts, "admin", "create", pid, f"{name} ({source})"),
            )
            row = conn.execute(
                "SELECT * FROM signatures WHERE id = ?", (sid,)).fetchone()
            return _row_to_dict(row)


def update_signature_version(pid: str, name: str | None, title: str | None,
                             source: str, original_rel: str,
                             processed_rel: str) -> Optional[dict]:
    """Upload versi baru utk pegawai existing. Versi lama di-archive + tercatat."""
    init_db()
    with _lock():
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM signatures WHERE pid = ?", (pid,)).fetchone()
            if row is None:
                return None
            ts = now_iso()
            new_ver = int(row["version"]) + 1
            conn.execute(
                """UPDATE signatures SET name = ?, title = ?, slug = ?,
                       version = ?, source = ?, original_rel = ?,
                       processed_rel = ?, updated_at = ?
                   WHERE pid = ?""",
                (name if name is not None else row["name"],
                 title if title is not None else row["title"],
                 row["slug"], new_ver, source,
                 original_rel, processed_rel, ts, pid),
            )
            conn.execute(
                """INSERT INTO signature_versions
                   (signature_id, version, original_rel, processed_rel, source, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (row["id"], new_ver, original_rel, processed_rel, source, ts),
            )
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                (ts, "admin", "update", pid, f"v{new_ver} — {source}"),
            )
            row = conn.execute(
                "SELECT * FROM signatures WHERE pid = ?", (pid,)).fetchone()
            return _row_to_dict(row)


def set_status(pid: str, status: str) -> Optional[dict]:
    """Soft delete / activate. Status: 'active' | 'inactive'."""
    init_db()
    with _lock():
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM signatures WHERE pid = ?", (pid,)).fetchone()
            if row is None:
                return None
            ts = now_iso()
            conn.execute(
                "UPDATE signatures SET status = ?, updated_at = ? WHERE pid = ?",
                (status, ts, pid),
            )
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                (ts, "admin", status, pid, ""),
            )
            row = conn.execute(
                "SELECT * FROM signatures WHERE pid = ?", (pid,)).fetchone()
            return _row_to_dict(row)


def get_signature(pid: str) -> Optional[dict]:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM signatures WHERE pid = ?", (pid,)).fetchone()
        return _row_to_dict(row) if row else None


def delete_signature(pid: str) -> Optional[dict]:
    """Hapus permanen: baris DB (+versions) + folder signatures/<pid>/.

    Pid TIDAK dipakai ulang (seq tetap), sehingga QR/URL lama otomatis 404.
    Riwayat aksi tetap tersimpan di audit_log (target pid + detail nama).
    Folder lama (NN_Slug legacy) tidak disentuh.
    """
    init_db()
    with _lock():
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM signatures WHERE pid = ?", (pid,)).fetchone()
            if row is None:
                return None
            detail = f"{row['name']} (v{row['version']})"
            ts = now_iso()
            # simpan dulu ke audit, lalu hapus (ON DELETE CASCADE versi)
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target, detail) "
                "VALUES (?,?,?,?,?)",
                (ts, "admin", "delete", pid, detail))
            conn.execute("DELETE FROM signatures WHERE pid = ?", (pid,))
    # hapus folder di luar transaksi DB (dilindungi lock yang sama)
    with _lock():
        folder = SIGNATURES_DIR / pid
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
    return _row_to_dict(row)


def list_signatures(include_inactive: bool = True) -> list[dict]:
    init_db()
    q = "SELECT * FROM signatures"
    if not include_inactive:
        q += " WHERE status = 'active'"
    q += " ORDER BY seq"
    with connect() as conn:
        return [_row_to_dict(r) for r in conn.execute(q).fetchall()]


def get_versions(pid: str) -> list[dict]:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM signatures WHERE pid = ?", (pid,)).fetchone()
        if row is None:
            return []
        rows = conn.execute(
            """SELECT version, original_rel, processed_rel, source, created_at
               FROM signature_versions WHERE signature_id = ?
               ORDER BY version""", (row["id"],)).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# audit log
# --------------------------------------------------------------------------

def get_audit(limit: int = 200) -> list[dict]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """SELECT ts, actor, action, target, detail
               FROM audit_log ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# cache verifier (atomik)
# --------------------------------------------------------------------------

def build_cache() -> Path:
    """Bangun cache/verifier_index.json dari SQLite — tulis atomik (tmp+rename)."""
    init_db()
    index: dict = {}
    with connect() as conn:
        rows = conn.execute("SELECT * FROM signatures ORDER BY seq").fetchall()
        for r in rows:
            r = dict(r)
            index[r["pid"]] = {
                "no": r["seq"],
                "id": r["id"],
                "nama": r["slug"] or r["name"],
                "nama_display": r["name"],
                "gelar": r["title"],
                "sumber": r["source"],
                "png": r["processed_rel"],
                "original": r["original_rel"],
                "status": r["status"],
                "version": r["version"],
                "updated_at": r["updated_at"],
            }
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CACHE_PATH)
    return CACHE_PATH


# --------------------------------------------------------------------------
# metadata.json per folder (human-readable sidecar)
# --------------------------------------------------------------------------

def write_metadata(pid: str) -> None:
    """Tulis signatures/<pid>/metadata.json — ringkasan utk inspeksi manual."""
    row = get_signature(pid)
    if row is None:
        return
    folder = SIGNATURES_DIR / pid
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": row["id"],
        "pid": pid,
        "seq": row["seq"],
        "nama": row["name"],
        "gelar": row["title"],
        "status": row["status"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "sumber": row["source"],
        "original": row["original_rel"],
        "signature": row["processed_rel"],
        "versions": get_versions(pid),
    }
    (folder / "metadata.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def rel_signature(pid: str) -> str:
    return f"signatures/{pid}/processed/signature.png"


def rel_original(pid: str) -> str:
    return f"signatures/{pid}/original/upload.jpg"


def rel_archive(pid: str, version: int, kind: str, ext: str) -> str:
    """Archive versi lama: signatures/<pid>/archive/{kind}_v<N>.{ext}"""
    return f"signatures/{pid}/archive/{kind}_v{version}.{ext}"


def signature_dir(pid: str) -> Path:
    return SIGNATURES_DIR / pid


def ensure_folders(pid: str) -> dict[str, Path]:
    base = signature_dir(pid)
    folders = {
        "base": base,
        "original": base / "original",
        "processed": base / "processed",
        "archive": base / "archive",
    }
    for f in folders.values():
        f.mkdir(parents=True, exist_ok=True)
    return folders
