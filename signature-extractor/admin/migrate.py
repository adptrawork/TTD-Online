"""migrate.py — Migrasi data lama ke Signature Management Service (SQLite).

Mengubah struktur lama:
    /data/<no>_<slug>/signature.png
    /data/verifier_index.json

menjadi struktur baru:
    /data/database/signatures.db          (SQLite — source of truth)
    /data/signatures/<pid>/processed/signature.png
    /data/signatures/<pid>/metadata.json
    /data/cache/verifier_index.json       (cache, dibangun ulang)

PENTING: pid (t01..t28) dipertahankan persis, sehingga QR/URL lama
(/ttd/v/tXX) tetap valid setelah migrasi. UUID baru dibuat utk id internal.
File lama TIDAK dihapus — hanya disalin; folder lama dibiarkan utk diverifikasi
manual, lalu bisa dihapus sendiri.

Pemakaian (dalam container admin, DATA_ROOT=/data):
    python migrate.py            # migrasi semua entri lama yang belum ada
    python migrate.py --dry-run  # tampilkan rencana tanpa menulis
    python migrate.py --force    # jalankan ulang walau DB sudah terisi
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from db import (_lock, build_cache, connect, ensure_folders, init_db,
                list_signatures, now_iso, new_uuid, write_metadata,
                DATA_ROOT)  # noqa: E402


def migrate(dry_run: bool = False, force: bool = False) -> list[dict]:
    init_db()
    data_root = DATA_ROOT
    legacy_index = data_root / "verifier_index.json"

    if not legacy_index.exists():
        print("Tidak ada verifier_index.json lama — tidak ada yang dimigrasi.")
        return []

    index = json.loads(legacy_index.read_text(encoding="utf-8"))
    existing = {r["pid"] for r in list_signatures()}
    results = []

    for pid, v in sorted(index.items(),
                         key=lambda kv: kv[1].get("no", 0)):
        if pid in existing and not force:
            print(f"  skip  {pid} (sudah ada di DB)")
            continue

        no = v.get("no")
        if no is None:
            print(f"  skip  {pid} (tidak ada 'no')")
            continue

        slug = v.get("nama", "")
        src = v.get("png", "")
        src_path = data_root / src
        if not src_path.exists():
            print(f"  skip  {pid} — file {src} tidak ditemukan")
            continue

        nama_display = v.get("nama_display") or slug
        gelar = v.get("gelar", "")
        sumber = v.get("sumber", "legacy")

        folders = ensure_folders(pid)
        dst = folders["processed"] / "signature.png"
        rel_processed = f"signatures/{pid}/processed/signature.png"
        rel_original = ""

        if dry_run:
            print(f"  plan  {pid}  no={no:02d}  {slug}  ->  {dst}")
            results.append({"pid": pid, "ok": True, "dry_run": True})
            continue

        shutil.copy2(src_path, dst)
        ts = now_iso()
        sid = new_uuid()
        with _lock():
            with connect() as conn:
                # sisipkan dgn seq/pid eksplisit agar tXX lama dipertahankan
                conn.execute(
                    """INSERT OR IGNORE INTO signatures
                       (id, seq, pid, name, title, slug, status, version,
                        source, original_rel, processed_rel, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,'active',1,?,?,?,?,?)""",
                    (sid, no, pid, nama_display, gelar, slug, sumber,
                     rel_original, rel_processed, ts, ts))
                row = conn.execute(
                    "SELECT * FROM signatures WHERE pid = ?", (pid,)).fetchone()
                conn.execute(
                    """INSERT OR IGNORE INTO signature_versions
                       (signature_id, version, original_rel, processed_rel,
                        source, created_at)
                       VALUES (?,1,?,?,?,?)""",
                    (row["id"], rel_original, rel_processed, sumber, ts))
                conn.execute(
                    "INSERT INTO audit_log (ts, actor, action, target, detail) "
                    "VALUES (?,?,?,?,?)",
                    (ts, "migrate", "create", pid,
                     f"migrasi dari legacy ({sumber})"))
        write_metadata(pid)
        results.append({"pid": pid, "ok": True})
        print(f"  ok    {pid}  no={no:02d}  {slug}")

    if not dry_run:
        cache = build_cache()
        print(f"Cache verifier ditulis: {cache}")
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Migrasi data TTD ke SQLite")
    ap.add_argument("--dry-run", action="store_true",
                    help="tampilkan rencana tanpa menulis")
    ap.add_argument("--force", action="store_true",
                    help="jalankan ulang walau DB sudah terisi")
    args = ap.parse_args()

    print(f"DATA_ROOT = {DATA_ROOT}")
    results = migrate(dry_run=args.dry_run, force=args.force)
    ok = sum(1 for r in results if r.get("ok"))
    print(f"\nSelesai: {ok} entri dimigrasi/direncanakan.")


if __name__ == "__main__":
    main()
