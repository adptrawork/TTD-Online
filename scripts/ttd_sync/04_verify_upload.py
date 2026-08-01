#!/usr/bin/env python3
"""04_verify_upload.py — Stage 4: verifikasi hasil upload di Morbis.

Untuk tiap pegawai di upload_report.csv (status OK):
  cek via search API apakah id_peg tsb kini punya TTD (tidak null).
Output: output/verify_report.csv (pid, nama_display, id_peg, has_ttd, ttd_path, ok)
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent / "output"
REPORT = OUT / "upload_report.csv"
VERIFY = OUT / "verify_report.csv"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lib_morbis as L

    if not REPORT.exists():
        print("[verify] output/upload_report.csv belum ada — jalankan 03_upload_ttd.py dulu")
        return 1

    up = [r for r in csv.DictReader(open(REPORT, encoding="utf-8"))
          if r["status"] == "OK"]
    print(f"[verify] {len(up)} upload berstatus OK")
    if args.limit:
        up = up[:args.limit]

    cookies = L.morbis_login()
    print("[verify] login Morbis OK")

    rows = []
    for i, r in enumerate(up):
        nama_display = r["nama_display"]
        id_peg = r["id_peg"]
        # cari via search nama inti — cek id_peg punya TTD
        found_ttd = ""
        ok = False
        for q in L.inti_variants(nama_display):
            res = L.morbis_search(cookies, q)
            time.sleep(args.delay)
            hit = [x for x in res if str(x.get("ID")) == str(id_peg)]
            if hit:
                ttd = hit[0].get("TTD")
                found_ttd = str(ttd or "")
                ok = bool(ttd)
                break
        rows.append({"pid": r["pid"], "nama_display": nama_display,
                     "id_peg": id_peg, "has_ttd": "1" if ok else "0",
                     "ttd_path": found_ttd, "ok": "1" if ok else "0"})
        print(f"  [{i+1}/{len(up)}] {pid_txt(r)} {nama_display} -> id {id_peg}: "
              f"{'OK ' + found_ttd if ok else 'BELUM ADA TTD'}")
        if (i + 1) % 20 == 0:
            _write(rows)

    _write(rows)
    n_ok = sum(1 for r in rows if r["ok"] == "1")
    print(f"\n=== VERIFIKASI ===")
    print(f"  terverifikasi: {n_ok}/{len(rows)}")
    print(f"  belum ada TTD: {len(rows) - n_ok}")
    print(f"Laporan: {VERIFY}")
    return 0 if n_ok == len(rows) else 2


def pid_txt(r):
    return r["pid"]


def _write(rows):
    with open(VERIFY, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["pid", "nama_display", "id_peg",
                                          "has_ttd", "ttd_path", "ok"])
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    sys.exit(main())
