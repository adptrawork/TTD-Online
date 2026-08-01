#!/usr/bin/env python3
"""01_fetch_pegawai.py — Stage 1: ambil daftar pegawai dari TTD-Online.

Output: output/pegawai.csv  (pid, no, nama, nama_display, gelar, method, status)
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "output"
OUT.mkdir(parents=True, exist_ok=True)


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lib_morbis as L

    st, body = L.ttd_api("/api/list")
    if st != 200:
        print(f"[fetch] GAGAL /api/list (HTTP {st})")
        return 1
    ours = json.loads(body)
    print(f"[fetch] {len(ours)} pegawai dari TTD-Online")

    csv_path = OUT / "pegawai.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pid", "no", "nama", "nama_display", "gelar", "method", "status"])
        for p in sorted(ours, key=lambda x: x.get("pid", "")):
            w.writerow([p.get("pid", ""), p.get("no", ""),
                        p.get("nama", ""), p.get("nama_display", ""),
                        p.get("gelar", ""), p.get("method", ""),
                        p.get("status", "")])
    # simpan juga JSON mentah utk referensi
    (OUT / "pegawai_raw.json").write_text(json.dumps(ours, ensure_ascii=False, indent=1))
    print(f"[fetch] tersimpan: {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
