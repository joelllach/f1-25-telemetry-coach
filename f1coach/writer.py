"""Writer — process 1 of the re-architected pipeline.

Listens on UDP 20777 and appends deduplicated telemetry frames to raw.jsonl.
Completely dumb: no lap detection, no ring buffer, no state beyond the last
written lapDistance.

Dedup rule: skip a frame if |lap_distance - prev_written_distance| < DEDUP_M.
This silently drops garage/menu frames (car frozen at one spot) without needing
a speed check. Real driving always moves the car, so nothing meaningful is lost.

File rolling: when raw.jsonl exceeds MAX_BYTES, it is moved to raw.prev.jsonl
(overwriting any previous .prev), and a fresh raw.jsonl is started. This caps
disk usage to ~2 × MAX_BYTES.

Format: one JSON object per line (JSONL).
  {"t":"frame", "session_time":..., "lap_distance":..., "speed":..., ... }
  {"t":"session_uid", "uid":...}   (written when session_uid changes)
  {"t":"start", "wall":...}        (first line on open)
  {"t":"roll", "wall":...}         (written just before rolling)

Run:
    python -m f1coach writer [--laps-dir laps] [--port 20777]
"""
from __future__ import annotations

import dataclasses
import json
import os
import time

from .listener import TelemetryListener, Frame

DEDUP_M = 0.5          # skip frame if dist moved less than this (metres)
MAX_BYTES = 950_000_000  # roll at 950 MB → max ~1.9 GB on disk


class TelemetryWriter:
    """Writes deduplicated raw frames to raw.jsonl."""

    def __init__(self, laps_dir: str = "laps", port: int = 20777,
                 dedup_m: float = DEDUP_M, max_bytes: int = MAX_BYTES):
        self.laps_dir = laps_dir
        self.port = port
        self.dedup_m = dedup_m
        self.max_bytes = max_bytes
        os.makedirs(laps_dir, exist_ok=True)
        self.raw_path = os.path.join(laps_dir, "raw.jsonl")
        self.prev_path = os.path.join(laps_dir, "raw.prev.jsonl")
        self._fh = None
        self._prev_dist: float | None = None
        self._prev_uid: int = -1
        self._frames_written = 0
        self._frames_skipped = 0

    def _open(self):
        self._fh = open(self.raw_path, "a", buffering=1)  # line-buffered
        self._write({"t": "start", "wall": time.time()})

    def _write(self, obj: dict):
        self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def _roll(self):
        """Move raw.jsonl → raw.prev.jsonl and start fresh."""
        self._write({"t": "roll", "wall": time.time()})
        self._fh.close()
        if os.path.exists(self.prev_path):
            os.unlink(self.prev_path)
        os.rename(self.raw_path, self.prev_path)
        self._fh = open(self.raw_path, "w", buffering=1)
        self._write({"t": "start", "wall": time.time(), "rolled": True})
        print(f"[writer] rolled: raw.jsonl → raw.prev.jsonl")

    def _on_frame(self, f: Frame):
        # Dedup: skip if car hasn't moved enough
        if (self._prev_dist is not None
                and abs(f.lap_distance - self._prev_dist) < self.dedup_m):
            self._frames_skipped += 1
            return

        self._prev_dist = f.lap_distance

        # Roll if needed
        try:
            sz = os.path.getsize(self.raw_path)
            if sz >= self.max_bytes:
                self._roll()
        except FileNotFoundError:
            pass

        # Session UID change marker (helps processor locate session boundaries)
        if f.session_uid != self._prev_uid and f.session_uid != 0:
            self._write({"t": "session_uid", "uid": f.session_uid,
                         "wall": time.time()})
            self._prev_uid = f.session_uid

        # Write the frame — only fields the processor needs
        rec = {
            "t": "frame",
            "st":  round(f.session_time, 4),     # session_time
            "uid": f.session_uid,
            "lap": f.current_lap_num,
            "llm": f.last_lap_ms,                # last_lap_ms — the seal trigger
            "inv": f.current_lap_invalid,
            "d":   round(f.lap_distance, 2),
            "spd": f.speed,
            "thr": round(f.throttle, 3),
            "brk": round(f.brake, 3),
            "str": round(f.steer, 3),
            "g":   f.gear,
            "rpm": f.rpm,
            "drs": f.drs,
            "wx":  round(f.world_x, 2),
            "wy":  round(f.world_y, 2),
            "gl":  round(f.g_lat, 3),
            "gn":  round(f.g_long, 3),
            "sr":  [round(x, 3) for x in f.slip_ratio],
            "sa":  [round(x, 3) for x in f.slip_angle],
            "st2": list(f.surface_type),
            "tst": list(f.tyre_surface_temp),
            "tit": list(f.tyre_inner_temp),
            "off": f.offtrack,
        }
        # Slowly-changing context attached by the listener
        if hasattr(f, "_setup") and f._setup:
            rec["setup"] = f._setup
        if hasattr(f, "_session") and f._session:
            rec["session"] = f._session

        self._write(rec)
        self._frames_written += 1

    def _on_context(self, **kwargs):
        """Called by listener when setup/session packets arrive."""
        # Tag the most-recently-appended frame's context by writing a context line
        if kwargs.get("setup"):
            self._write({"t": "ctx_setup", "v": kwargs["setup"],
                         "wall": time.time()})
        if kwargs.get("session"):
            self._write({"t": "ctx_session", "v": kwargs["session"],
                         "wall": time.time()})

    def run(self):
        self._open()
        lis = TelemetryListener(port=self.port, on_frame=self._on_frame)
        lis.on_context = self._on_context
        print(f"[writer] → {self.raw_path}  "
              f"(dedup {self.dedup_m}m, roll at {self.max_bytes//1_000_000}MB)")
        try:
            lis.run()
        except KeyboardInterrupt:
            pass
        finally:
            if self._fh:
                self._write({"t": "end", "wall": time.time(),
                             "written": self._frames_written,
                             "skipped": self._frames_skipped})
                self._fh.close()
            print(f"[writer] done: {self._frames_written} written, "
                  f"{self._frames_skipped} skipped (dedup)")
