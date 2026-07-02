"""One-shot migration: convert legacy JSONL session files to per-lap .lap files.

Reads old JSONL recordings, detects laps via last_lap_ms change (the new
canonical signal), writes individual .lap binary files, and builds index.jsonl.

Usage:
    python tools/migrate.py                          # migrate all laps/*.jsonl
    python tools/migrate.py laps/specific.jsonl      # single file
    python tools/migrate.py --out-dir laps/migrated  # different output dir

The original JSONL files are NOT modified or deleted — this is read-only.
"""
from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from f1coach.listener import Frame
from f1coach.analysis import Lap, fmt_ms
from f1coach.storage import write_lap, append_index, LapMeta
from f1coach import packets as pk


_FRAME_FIELDS = {f.name for f in dataclasses.fields(Frame)}
_RESET_JUMP_M = 50.0


def _migrate_jsonl(src: str, out_dir: str) -> int:
    """Process one JSONL file. Returns number of .lap files written."""
    print(f"\n→ {src}  ({os.path.getsize(src)//1_000_000}MB)")
    os.makedirs(out_dir, exist_ok=True)
    index_path = os.path.join(out_dir, "index.jsonl")

    # Accumulate frames in memory keyed by lap_num.
    # On last_lap_ms change → seal the lap that just finished.
    frames_by_lap: dict[int, list[Frame]] = {}
    prev_last_lap_ms = 0
    prev_dist: dict[int, float] = {}
    reset_counts: dict[int, int] = {}
    latest_setup: dict | None = None
    latest_session: dict | None = None
    sealed = 0

    def seal(lap_num: int, lap_time_ms: int) -> None:
        nonlocal sealed
        frames = frames_by_lap.pop(lap_num, [])
        if not frames:
            return
        invalid = any(f.current_lap_invalid for f in frames)
        lap = Lap(lap_num=lap_num, frames=frames,
                  invalid=invalid, lap_time_ms=lap_time_ms)
        lap.reset_count = reset_counts.pop(lap_num, 0)
        lap.setup = latest_setup
        lap.session = latest_session
        lap.events = []

        track_id = (latest_session or {}).get("track_id", 0)
        track_length_m = (latest_session or {}).get("track_length", 0)
        track_slug = pk.TRACK_IDS.get(track_id, f"t{track_id}")
        track_slug = track_slug.split("(")[0].strip().lower().replace(" ", "_")[:12]

        wall_ms = int(time.time() * 1000)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        time_str = fmt_ms(lap_time_ms).replace(":", "m").replace(".", "s")
        fname = f"{ts}_lap{lap_num:02d}_{track_slug}_{time_str}.lap"
        fpath = os.path.join(out_dir, fname)

        lap._wall_clock_ms = wall_ms
        lap._track_id = track_id
        lap._track_length_m = track_length_m

        write_lap(lap, fpath, wall_clock_ms=wall_ms,
                  track_id=track_id, track_length_m=track_length_m)
        meta = LapMeta.from_lap(lap, fname)
        append_index(meta, index_path)

        flag = (f"RESET x{lap.reset_count}" if lap.reset_count else
                "INVALID" if invalid else "clean")
        print(f"  lap {lap_num:3d}: {fmt_ms(lap_time_ms)}  {flag:12}  "
              f"{len(frames):5d}fr  -> {fname}")
        sealed += 1

    with open(src, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                r = json.loads(raw)
            except Exception:
                continue

            t = r.get("t")

            if t == "frame":
                kwargs = {k: r[k] for k in _FRAME_FIELDS if k in r}
                f = Frame(**kwargs)
                ln = f.current_lap_num
                frames_by_lap.setdefault(ln, []).append(f)

                # reset detection
                pd = prev_dist.get(ln)
                if pd is not None and f.lap_distance - pd > _RESET_JUMP_M:
                    reset_counts[ln] = reset_counts.get(ln, 0) + 1
                prev_dist[ln] = f.lap_distance

                # lap completion: last_lap_ms changed
                if (f.last_lap_ms > 0
                        and f.last_lap_ms != prev_last_lap_ms):
                    # The lap that just completed is the one whose last frame
                    # was NOT the current lap_num but the previous.
                    # Find the lap_num whose frames precede this one.
                    # Heuristic: the completed lap is the one with the most frames
                    # that isn't the current lap_num (which just started).
                    completed = max(
                        (n for n in frames_by_lap if n != ln),
                        key=lambda n: len(frames_by_lap[n]),
                        default=None,
                    )
                    if completed is not None:
                        seal(completed, f.last_lap_ms)
                    prev_last_lap_ms = f.last_lap_ms

            elif t == "lap":
                # Old-style lap marker: use as fallback if we haven't
                # detected this lap via last_lap_ms yet
                ln = r.get("lap_num")
                lt = r.get("lap_time_ms", 0)
                if lt > 0 and ln in frames_by_lap and frames_by_lap[ln]:
                    # Only seal if frames exist and not already sealed above
                    seal(ln, lt)

            elif t == "session" or (t == "frame" and r.get("session")):
                pass  # handled inline

    # Handle any leftover unsealable frames (partial laps, session end)
    for ln, frames in list(frames_by_lap.items()):
        if frames:
            print(f"  lap {ln}: {len(frames)} frames with no completion signal — skipped")

    print(f"  → {sealed} laps written to {out_dir}/")
    return sealed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="*",
                    help="JSONL session files (default: all laps/*.jsonl)")
    ap.add_argument("--out-dir", default="laps/migrated",
                    help="output directory for .lap files (default: laps/migrated)")
    args = ap.parse_args()

    sources = args.files or sorted(glob.glob("laps/session_*.jsonl"))
    if not sources:
        print("No JSONL files found."); return

    total = 0
    for src in sources:
        if src.endswith(".jsonl") and os.path.exists(src):
            total += _migrate_jsonl(src, args.out_dir)

    print(f"\n✓ Migration complete: {total} laps -> {args.out_dir}/")


if __name__ == "__main__":
    main()
