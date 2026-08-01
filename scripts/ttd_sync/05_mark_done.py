#!/usr/bin/env python3
"""05_mark_done.py — Rekonsiliasi: tandai di upload_report.csv pegawai yang
SUDAH punya TTD di Morbis, supaya pipeline 03 tidak upload ulang.

Sumber kebenaran (2):
  1. Ground truth Morbis: GET /v2/master-data/ttd-pegawai/data
     -> semua id_peg yang kini punya TTD (assets/ttd/...)
  2. Log upload script lama (opsional): --log file.txt
     -> baris 'OK tNN NAMA -> id_peg' ditandai bila id_peg-nya memang
        sudah terkonfirmasi punya TTD di /data (hindari false positive).

Output: output/upload_report.csv diperbarui (status OK, msg asal), dan
        output/sync_status.csv = status per-pegawai lengkap untuk audit.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "output"
REPORT = OUT / "upload_report.csv"
SYNC = OUT / "sync_status.csv"
FIELDS_REP = ["pid", "nama_display", "id_peg", "status", "msg", "ts"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="", help="path log script lama utk parse 'OK tN -> id'")
    ap.add_argument("--dry", action="store_true", help="tampilkan saja, tanpa menulis")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lib_morbis as L

    # --- 1. ground truth Morbis ---
    cookies = L.morbis_login()
    st, body, _ = L.http_post(L.MORBIS_BASE + L.MORBIS_PATH + "/data",
                              data={"start": "0", "length": "10000",
                                    "search[value]": ""},
                              cookies=cookies, timeout=20, retries=2)
    d = json.loads(body)
    morbis_ttd = {}          # id_peg -> path ttd
    for r in d.get("data", []):
        m = re.search(r'data-id="(\d+)"', str(r[3]))
        if m and r[2]:
            morbis_ttd[m.group(1)] = r[2]
    print(f"[mark] ground truth Morbis: {len(morbis_ttd)} id_peg punya TTD")

    # --- 2. parse log lama (opsional) ---
    log_ok = {}
    if args.log:
        for pid, nama, idp in re.findall(r"OK (t\d+) (.+?) -> (\d+)",
                                         open(args.log, encoding="utf-8").read()):
            log_ok[pid] = idp
        print(f"[mark] log lama: {len(log_ok)} baris OK")

    # --- 3. baca mapping ---
    if not (OUT / "mapping.csv").exists():
        print("[mark] output/mapping.csv belum ada — jalankan 02 dulu")
        return 1
    mapping = list(csv.DictReader(open(OUT / "mapping.csv", encoding="utf-8")))
    print(f"[mark] mapping: {len(mapping)} baris")

    # --- 4. bangun status per pegawai ---
    ts = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in mapping:
        pid = r["pid"]
        id_peg = r["id_peg"]
        st_map = r["status"]
        # prioritas status
        if st_map == "no_match":
            status, msg = "NO_MATCH", "tidak ditemukan di Morbis"
        elif st_map == "ambiguous":
            status, msg = "AMBIGUOUS", "lebih dari 1 kandidat — tinjau manual"
        elif st_map == "already":
            status, msg = "ALREADY", "sudah punya TTD saat mapping"
        elif id_peg in morbis_ttd:
            status, msg = "OK", f"TTD sudah ada: {morbis_ttd[id_peg]}"
            if pid in log_ok:
                msg += " (dari log lama)"
        elif pid in log_ok and log_ok[pid] == id_peg:
            # di log lama dinyatakan OK tapi belum muncul di /data -> verifikasi manual
            status, msg = "CHECK", "OK di log lama tapi belum terlihat di /data"
        else:
            status, msg = "PENDING", "belum punya TTD — siap upload"

        rows.append({"pid": pid, "nama_display": r["nama_display"],
                     "id_peg": id_peg, "status": status, "msg": msg, "ts": ts})

    if args.dry:
        from collections import Counter
        print(Counter(x["status"] for x in rows))
        return 0

    # --- 5. tulis upload_report.csv (penanda untuk pipeline 03) ---
    with open(REPORT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS_REP)
        w.writeheader()
        w.writerows(rows)

    # --- 6. tulis sync_status.csv (audit lengkap) ---
    with open(SYNC, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS_REP)
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    c = Counter(x["status"] for x in rows)
    print(f"\n=== REKONSILIASI ===")
    for k, v in sorted(c.items()):
        print(f"  {k}: {v}")
    print(f"upload_report: {REPORT}")
    print(f"sync_status : {SYNC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
