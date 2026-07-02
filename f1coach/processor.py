"""Processor — process 2 of the re-architected pipeline.

Reads raw.jsonl (+ raw.prev.jsonl), finds laps by scanning for last_lap_ms
changes, and writes .lap binary files to laps/. Safe to run multiple times —
stops as soon as it hits a lap already in the index.

Lap extraction:
  For each last_lap_ms change, work backward through the frame stream to find
  the start/finish line crossing (dist < 200m after dist > 3000m). That is the
  true start of the flying lap. Take everything from that point to the seal
  trigger as the lap frames.

Run modes:
  python -m f1coach process               # run once
  python -m f1coach process --watch       # run every 60s until Ctrl-C
  python -m f1coach process --watch 30    # run every 30s
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from .listener import Frame
from .analysis import Lap, fmt_ms
from .storage import write_lap, append_index, read_index, LapMeta
from . import packets as pk

import dataclasses as _dc
_FRAME_FIELDS = {f.name for f in _dc.fields(Frame)}

# Minimum frames for a lap to be worth storing (filters out partial noise)
MIN_LAP_FRAMES = 100


def _load_raw_frames(laps_dir: str) -> list[dict]:
    """Load and merge frames from raw.jsonl + raw.prev.jsonl, sorted by st."""
    rows: list[dict] = []
    latest_setup: dict | None = None
    latest_session: dict | None = None

    for fname in ("raw.prev.jsonl", "raw.jsonl"):
        path = os.path.join(laps_dir, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                t = r.get("t")
                if t == "ctx_setup":
                    latest_setup = r.get("v")
                elif t == "ctx_session":
                    latest_session = r.get("v")
                elif t == "frame":
                    # Carry the latest context into the frame
                    if latest_setup and "setup" not in r:
                        r["setup"] = latest_setup
                    if latest_session and "session" not in r:
                        r["session"] = latest_session
                    rows.append(r)

    rows.sort(key=lambda r: r.get("st", 0))
    return rows


def _frame_from_raw(r: dict) -> Frame:
    """Reconstruct a Frame from a compact raw record."""
    sr = r.get("sr", [0, 0, 0, 0])
    sa = r.get("sa", [0, 0, 0, 0])
    return Frame(
        session_time=r.get("st", 0.0),
        session_uid=r.get("uid", 0),
        lap_distance=r.get("d", 0.0),
        total_distance=r.get("d", 0.0),
        current_lap_num=r.get("lap", 0),
        current_lap_ms=0,
        last_lap_ms=r.get("llm", 0),
        current_lap_invalid=r.get("inv", 0),
        sector=0, car_position=0,
        speed=r.get("spd", 0),
        throttle=r.get("thr", 0.0),
        brake=r.get("brk", 0.0),
        steer=r.get("str", 0.0),
        gear=r.get("g", 0),
        rpm=r.get("rpm", 0),
        drs=r.get("drs", 0),
        world_x=r.get("wx", 0.0),
        world_y=r.get("wy", 0.0),
        world_z=0.0,
        g_lat=r.get("gl", 0.0),
        g_long=r.get("gn", 0.0),
        tyre_compound=0, tyre_age_laps=0, ers_store=0.0,
        slip_ratio=tuple(sr) if len(sr) == 4 else (0.0,) * 4,
        slip_angle=tuple(sa) if len(sa) == 4 else (0.0,) * 4,
        tyre_surface_temp=tuple(r.get("tst", [0, 0, 0, 0])),
        tyre_inner_temp=tuple(r.get("tit", [0, 0, 0, 0])),
        surface_type=tuple(r.get("st2", [0, 0, 0, 0])),
        offtrack=r.get("off", 0),
    )


def _extract_lap(raw_rows: list[dict], seal_idx: int) -> list[Frame]:
    """Work backward from seal_idx to find the flying lap start.

    The lap starts at the last genuine start/finish crossing:
    a frame where dist < 200m AND the previous frame had dist > 3000m.
    Everything from that crossing to seal_idx is the lap.
    """
    # Phase 1: skip current near-zero region (we just crossed the line)
    i = seal_idx
    while i > 0 and raw_rows[i].get("d", 0) < 200:
        i -= 1

    # Phase 2: walk backward through the lap body until dist wraps to near-zero
    lap_start = 0
    while i > 0:
        curr_d = raw_rows[i].get("d", 0)
        prev_d = raw_rows[i - 1].get("d", 0)
        if curr_d < 200 and prev_d > 3000:
            lap_start = i
            break
        i -= 1

    return [_frame_from_raw(r) for r in raw_rows[lap_start:seal_idx + 1]]


def process_once(laps_dir: str = "laps", verbose: bool = True) -> int:
    """Scan raw files, extract new laps, write .lap files. Returns laps written."""
    raw_rows = _load_raw_frames(laps_dir)
    if not raw_rows:
        if verbose:
            print("[processor] no raw data found")
        return 0

    if verbose:
        print(f"[processor] {len(raw_rows)} raw frames loaded")

    index_path = os.path.join(laps_dir, "index.jsonl")
    existing = {(m.lap_time_ms, m.track_id) for m in read_index(index_path)}

    # Find all last_lap_ms change points, newest first
    seals: list[tuple[int, int]] = []  # (last_lap_ms, row_index)
    prev_llm = 0
    for idx, r in enumerate(raw_rows):
        llm = r.get("llm", 0)
        if llm > 0 and llm != prev_llm:
            seals.append((llm, idx))
            prev_llm = llm

    if not seals:
        if verbose:
            print("[processor] no lap completions found in raw data")
        return 0

    if verbose:
        print(f"[processor] {len(seals)} lap completion(s) found")

    written = 0
    # Process newest first — stop when we hit one already in the index
    for lap_time_ms, seal_idx in reversed(seals):
        # Resolve track from context
        session = raw_rows[seal_idx].get("session") or {}
        track_id = session.get("track_id", 0) if session else 0
        key = (lap_time_ms, track_id)

        if key in existing:
            if verbose:
                print(f"[processor] lap {fmt_ms(lap_time_ms)} already indexed — stopping")
            break

        # Extract the flying lap
        frames = _extract_lap(raw_rows, seal_idx)
        if len(frames) < MIN_LAP_FRAMES:
            if verbose:
                print(f"[processor] lap {fmt_ms(lap_time_ms)} too short "
                      f"({len(frames)} frames) — skipping")
            continue

        # Build Lap object
        lap_num = frames[-1].current_lap_num
        invalid = any(f.current_lap_invalid for f in frames)
        # Count resets (forward dist jumps within the lap)
        reset_count = sum(
            1 for i in range(1, len(frames))
            if frames[i].lap_distance - frames[i - 1].lap_distance > 50
        )
        lap = Lap(lap_num=lap_num, frames=frames,
                  invalid=invalid, lap_time_ms=lap_time_ms)
        lap.reset_count = reset_count
        lap.setup = raw_rows[seal_idx].get("setup")
        lap.session = session if session else None
        lap.events = []

        # Resolve track slug for filename
        track_length_m = session.get("track_length", 0) if session else 0
        track_slug = pk.TRACK_IDS.get(track_id, f"t{track_id}")
        track_slug = (track_slug.split("(")[0].strip()
                      .lower().replace(" ", "_")[:12])

        from datetime import datetime
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        time_str = fmt_ms(lap_time_ms).replace(":", "m").replace(".", "s")
        fname = f"{now}_lap{lap_num:02d}_{track_slug}_{time_str}.lap"
        fpath = os.path.join(laps_dir, fname)

        wall_ms = int(time.time() * 1000)
        lap._wall_clock_ms = wall_ms
        lap._track_id = track_id
        lap._track_length_m = track_length_m

        write_lap(lap, fpath, wall_clock_ms=wall_ms,
                  track_id=track_id, track_length_m=track_length_m)

        meta = LapMeta.from_lap(lap, fname)
        append_index(meta, index_path)
        existing.add(key)

        flag = (f"RESET x{reset_count}" if reset_count
                else ("INVALID" if invalid else "clean"))
        written += 1
        if verbose:
            print(f"[processor] ✓ {fname}  {fmt_ms(lap_time_ms)} "
                  f"({flag}, {len(frames)} frames)")

    if verbose and written == 0:
        print("[processor] no new laps to process")
    return written


def watch(laps_dir: str = "laps", interval: int = 60):
    """Run process_once every `interval` seconds until Ctrl-C."""
    print(f"[processor] watching {laps_dir}/ every {interval}s  (Ctrl-C to stop)")
    try:
        while True:
            process_once(laps_dir, verbose=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("[processor] stopped")
