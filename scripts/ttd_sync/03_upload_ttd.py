#!/usr/bin/env python3
"""03_upload_ttd.py — Stage 3: upload QR tanda tangan ke Morbis.

Input : output/mapping.csv (status=match)
Output: output/upload_report.csv  (pid, nama_display, id_peg, status, msg, ts)
Mode:
    --dry-run          : siapkan payload + ambil QR, TANPA POST (verifikasi payload)
    --dry-run N        : dry-run hanya N pegawai pertama
    --limit N          : upload maks N pegawai pada run ini
    --delay S          : jeda antar-request (default 0.5s)
Resume: pegawai berstatus OK/SKIP di upload_report.csv dilewati otomatis.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "output"
REPORT = OUT / "upload_report.csv"
QR_DIR = OUT / "qr"

FIELDS = ["pid", "nama_display", "gelar", "id_peg", "id_peg_nama",
          "match_type", "status"]


def load_report():
    if not REPORT.exists():
        return {}
    out = {}
    for r in csv.DictReader(open(REPORT, encoding="utf-8")):
        out[r["pid"]] = r
    return out


def save_report(rep):
    rows = sorted(rep.values(), key=lambda x: x["pid"])
    with open(REPORT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["pid", "nama_display", "id_peg",
                                          "status", "msg", "ts"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", nargs="?", const=999999, type=int,
                    help="uji payload tanpa POST (opsional: jumlah pegawai)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.5)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lib_morbis as L

    if not (OUT / "mapping.csv").exists():
        print("[upload] output/mapping.csv belum ada — jalankan 02_build_mapping.py dulu")
        return 1

    queue = [r for r in csv.DictReader(open(OUT / "mapping.csv", encoding="utf-8"))
             if r["status"] == "match"]
    print(f"[upload] antrian upload: {len(queue)} pegawai (status=match)")

    report = load_report()
    done = {pid for pid, r in report.items() if r["status"] in ("OK", "SKIP")}
    todo = [r for r in queue if r["pid"] not in done]
    print(f"[upload] sudah diproses: {len(done)}, tersisa: {len(todo)}")

    if args.limit:
        todo = todo[:args.limit]
        print(f"[upload] dibatasi {args.limit} pegawai")

    cookies = None if args.dry_run else L.morbis_login()
    if cookies:
        print("[upload] login Morbis OK")

    QR_DIR.mkdir(parents=True, exist_ok=True)
    ok = skip = fail = 0
    for i, r in enumerate(todo):
        pid = r["pid"]
        nama_display = r["nama_display"]
        id_peg = r["id_peg"]

        # 1. ambil QR dari TTD-Online
        qr = L.fetch_qr(pid)
        if not qr:
            report[pid] = {"pid": pid, "nama_display": nama_display,
                           "id_peg": id_peg, "status": "FAIL",
                           "msg": "QR tidak tersedia di TTD-Online",
                           "ts": datetime.now(timezone.utc).isoformat()}
            fail += 1
            continue

        fname = L.nama_file_ttd(nama_display)
        (QR_DIR / f"{pid}_{fname}").write_bytes(qr)

        if args.dry_run:
            # verifikasi payload tanpa POST
            if i >= args.dry_run:
                break
            report[pid] = {"pid": pid, "nama_display": nama_display,
                           "id_peg": id_peg, "status": "DRYRUN",
                           "msg": f"payload siap: {fname}, {len(qr)} bytes",
                           "ts": datetime.now(timezone.utc).isoformat()}
            ok += 1
            print(f"  [{i+1}] DRYRUN {pid} {nama_display} -> {id_peg} ({len(qr)}B)")
            continue

        # 2. POST ke Morbis
        url = L.MORBIS_BASE + L.MORBIS_PATH + "/control?sub=simpan"
        try:
            st, body, _ = L.http_post(
                url,
                data={"pegawai": r["id_peg_nama"] or nama_display,
                      "id_peg": id_peg, "id": ""},
                files={"img": (fname, qr, "image/png")},
                cookies=cookies)
            text = body.decode("utf-8", "replace")
            try:
                j = json.loads(text)
                ok_resp = bool(j.get("status"))
                msg = j.get("message", text)[:200]
            except Exception:
                ok_resp = st < 400
                msg = text[:200]
            if ok_resp:
                report[pid] = {"pid": pid, "nama_display": nama_display,
                               "id_peg": id_peg, "status": "OK",
                               "msg": msg, "ts": datetime.now(timezone.utc).isoformat()}
                ok += 1
                print(f"  [{i+1}] OK {pid} {nama_display} -> {id_peg} | {msg[:60]}")
            else:
                report[pid] = {"pid": pid, "nama_display": nama_display,
                               "id_peg": id_peg, "status": "FAIL",
                               "msg": f"HTTP {st}: {msg}",
                               "ts": datetime.now(timezone.utc).isoformat()}
                fail += 1
                print(f"  [{i+1}] GAGAL {pid} {nama_display}: HTTP {st} {msg[:80]}")
        except Exception as e:
            report[pid] = {"pid": pid, "nama_display": nama_display,
                           "id_peg": id_peg, "status": "FAIL",
                           "msg": f"EXC {e}", "ts": datetime.now(timezone.utc).isoformat()}
            fail += 1
            print(f"  [{i+1}] GAGAL {pid} {nama_display}: {e}")

        save_report(report)
        time.sleep(max(0.0, args.delay - 0.05))     # minus waktu fetch QR

    save_report(report)
    mode = "DRYRUN" if args.dry_run else "UPLOAD"
    print(f"\n=== {mode} SELESAI ===")
    print(f"  ok: {ok} | skip: {skip} | fail: {fail}")
    print(f"Laporan: {REPORT}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
