#!/usr/bin/env python3
"""02_build_mapping.py — Stage 2: cocokkan pegawai TTD-Online → id_peg Morbis.

Input : output/pegawai.csv
Output: output/mapping.csv
    pid, nama_display, gelar, id_peg, id_peg_nama, match_type, status
    status: match | no_match | ambiguous | already  (already = Morbis sudah punya TTD)
Fitur:
  - Resume: state per-pegawai di output/mapping_state.json — hentikan/lanjutkan bebas.
  - Autosave mapping.csv setiap 25 pegawai (backup bila JSON rusak).
  - ETA + progress bar + statistik kumulatif di log.
  - Error log terpisah: output/mapping_errors.log (request gagal/timeout).
"""
from __future__ import annotations

import csv
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent / "output"
STATE = OUT / "mapping_state.json"
CSV = OUT / "mapping.csv"
ERRLOG = OUT / "mapping_errors.log"
FIELDS = ["pid", "nama_display", "gelar", "id_peg", "id_peg_nama",
          "match_type", "status"]


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {}


def save_state(st):
    STATE.write_text(json.dumps(st, indent=1, ensure_ascii=False))


def write_csv(rows):
    with open(CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def log_err(line):
    with open(ERRLOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def progress_bar(done, total, width=22):
    filled = int(width * done / total) if total else width
    return "█" * filled + "░" * (width - filled)


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lib_morbis as L

    if not (OUT / "pegawai.csv").exists():
        print("[mapping] output/pegawai.csv belum ada — jalankan 01_fetch_pegawai.py dulu")
        return 1

    cookies = L.morbis_login()
    print(f"[mapping] login Morbis OK · {datetime.now():%H:%M:%S}")

    rows = list(csv.DictReader(open(OUT / "pegawai.csv", encoding="utf-8")))
    total = len(rows)
    state = load_state()
    done_seed = len(state)
    print(f"[mapping] {total} pegawai, {done_seed} sudah diproses (resume)\n")

    stats = Counter(v["status"] for v in state.values())
    t0 = time.time()
    for i, p in enumerate(rows):
        pid = p["pid"]
        nama_display = p["nama_display"] or p["nama"]
        if pid in state and state[pid]["status"] in (
                "match", "no_match", "ambiguous", "already"):
            continue

        # override manual
        ov = L.OVERRIDES.get(L.norm(nama_display))
        if ov:
            res = {"pid": pid, "nama_display": nama_display, "gelar": p["gelar"],
                   "id_peg": ov, "id_peg_nama": nama_display,
                   "match_type": "override", "status": "match"}
        else:
            try:
                r, amb = L.morbis_find(cookies, nama_display)
            except Exception as e:
                # error network — catat, tandai error (tidak resume sebagai selesai)
                log_err(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {pid} | {nama_display} | {e}")
                res = {"pid": pid, "nama_display": nama_display, "gelar": p["gelar"],
                       "id_peg": "", "id_peg_nama": "", "match_type": "",
                       "status": "error"}
                state[pid] = res
                save_state(state)
                print(f"  [{i+1}] ERROR {pid} {nama_display}: {e}")
                time.sleep(1)
                continue
            if amb:
                res = {"pid": pid, "nama_display": nama_display, "gelar": p["gelar"],
                       "id_peg": "", "id_peg_nama": "",
                       "match_type": "ambiguous", "status": "ambiguous",
                       "candidates": [x["ID"] for x in amb]}
            elif r is None:
                res = {"pid": pid, "nama_display": nama_display, "gelar": p["gelar"],
                       "id_peg": "", "id_peg_nama": "", "match_type": "",
                       "status": "no_match"}
            else:
                res = {"pid": pid, "nama_display": nama_display, "gelar": p["gelar"],
                       "id_peg": r["ID"], "id_peg_nama": r["NAMA"],
                       "match_type": "auto",
                       "status": "already" if r.get("TTD") else "match"}

        state[pid] = res
        stats[res["status"]] += 1

        if (i + 1) % 25 == 0:
            save_state(state)
            write_csv([state[k] for k in sorted(state)])
            elapsed = time.time() - t0
            rate = (i + 1 - done_seed) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate if rate > 0 else 0
            print(f"\n  {progress_bar(i+1, total)} {i+1}/{total} ({100*(i+1)//total}%)")
            print(f"  elapsed {int(elapsed//60)}:{elapsed%60:04.1f} m "
                  f"ETA {int(eta//60)}:{eta%60:04.1f} m")
            print(f"  match {stats['match']} · already {stats['already']} · "
                  f"no_match {stats['no_match']} · ambiguous {stats['ambiguous']} · "
                  f"error {stats['error']}\n")
            sys.stdout.flush()

    save_state(state)
    write_csv([state[k] for k in sorted(state)])

    print(f"\n=== MAPPING SELESAI ===")
    for k in ("match", "already", "no_match", "ambiguous", "error"):
        print(f"  {k}: {stats[k]}")
    print(f"CSV: {CSV}")
    print(f"Error log: {ERRLOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
