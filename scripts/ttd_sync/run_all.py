#!/usr/bin/env python3
"""run_all.py — jalankan seluruh pipeline sinkronisasi TTD → Morbis dalam SATU script.

Urutan (auto, stop bila error):
  01 fetch pegawai          -> output/pegawai.csv
  02 build mapping (resume) -> output/mapping.csv
  05 rekonsiliasi           -> tandai yang SUDAH ada di Morbis (jangan upload ulang)
  03 upload sisanya         -> output/upload_report.csv
  04 verifikasi             -> output/verify_report.csv

Cara pakai:
    python3 scripts/ttd_sync/run_all.py                 # semua, aman (tanpa upload ulang)
    python3 scripts/ttd_sync/run_all.py --steps 3       # mulai dari tahap 3
    python3 scripts/ttd_sync/run_all.py --steps 1 2     # hanya tahap 1 & 2
    python3 scripts/ttd_sync/run_all.py --dry-run       # uji upload tanpa POST
    python3 scripts/ttd_sync/run_all.py --limit 50      # batasi upload run ini
    python3 scripts/ttd_sync/run_all.py --delay 1.0     # jeda antar-request

Catatan: 02 bersifat resume — pegawai yang sudah diproses dilewati.
05 selalu dijalankan sebelum 03 supaya pegawai yang sudah punya TTD di Morbis
tidak di-upload ulang (aman dijalankan berkali-kali).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

STEPS = {
    1: "01_fetch_pegawai.py",
    2: "02_build_mapping.py",
    3: "03_upload_ttd.py",
    4: "04_verify_upload.py",
    5: "05_mark_done.py",
}

# urutan eksekusi (5 rekonsiliasi dipindah sebelum 3)
ORDER = [1, 2, 5, 3, 4]


def run_step(n: int, extra: list[str]) -> bool:
    script = HERE / STEPS[n]
    cmd = [sys.executable, "-u", str(script)] + extra
    print(f"\n{'='*60}\nSTEP {n} — {STEPS[n]}\n{'='*60}", flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"\n[run_all] STEP {n} GAGAL (exit {r.returncode}) — dihentikan.", flush=True)
        return False
    print(f"[run_all] STEP {n} OK", flush=True)
    return True


def main():
    ap = argparse.ArgumentParser(description="Pipeline sinkronisasi TTD → Morbis (1 script)")
    ap.add_argument("--steps", nargs="+", type=int, choices=[1, 2, 3, 4, 5],
                    default=None, help="tahap yang dijalankan (default: semua)")
    ap.add_argument("--dry-run", nargs="?", const=999999, type=int,
                    help="oper mode dry-run utk upload (uji tanpa POST)")
    ap.add_argument("--limit", type=int, default=0, help="batasi jumlah upload")
    ap.add_argument("--delay", type=float, default=0.5, help="jeda antar-request upload")
    ap.add_argument("--skip-verify", action="store_true", help="lewati tahap 4 verifikasi")
    args = ap.parse_args()

    steps = sorted(args.steps) if args.steps else ORDER

    # argumen passthrough per tahap
    arg3 = ["--delay", str(args.delay)]
    if args.limit:
        arg3 += ["--limit", str(args.limit)]
    if args.dry_run:
        arg3 += ["--dry-run", str(args.dry_run)]
    arg4 = ["--delay", str(args.delay)] if not args.skip_verify else None

    ok = True
    for n in steps:
        if n == 3:
            extra = arg3
        elif n == 4:
            if arg4 is None:
                continue
            extra = arg4
        else:
            extra = []
        if not run_step(n, extra):
            ok = False
            break

    print(f"\n{'='*60}\nSELESAI — status: {'OK semua tahap' if ok else 'ada tahap gagal'}\n{'='*60}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
