"""LapBuffer — ring buffer that seals laps on last_lap_ms change.

Replaces the old LapSegmenter-based JSONL recorder.  Feed every Frame from
TelemetryListener into LapBuffer.add(); it handles everything else:

  • Keeps the last ~4 laps of frames in a deque (no file seeking needed).
  • Detects lap completion when last_lap_ms changes (the game only sets this
    on a genuine start/finish crossing — resets do not change it).
  • On completion: seals the lap, writes a .lap binary, appends index.jsonl,
    and rewrites hot.jsonl (current in-progress lap, for live monitoring).
  • Tracks in-lap resets (lapDistance forward jump >50m, same last_lap_ms).
  • Writes a .lap file even for invalid / reset laps — Option A: store
    everything, extract clean flying lap at read time.

Checkpoint / crash recovery
  A session restart or process kill between the lap crossing and the NEXT
  crossing (which triggers the seal) would lose the lap. To prevent this:

  • When last_lap_ms changes we immediately write a binary checkpoint:
      <laps_dir>/pending.lap   (full .lap file, correct time)
  • Once the final .lap file + index entry are safely written, pending.lap
    is deleted.
  • On __init__, if pending.lap exists it means a previous run was killed
    before sealing. We recover it: move it to a dated .lap file and add it
    to the index, then delete pending.lap.

File naming:  <laps_dir>/<YYYYMMDD_HHMMSS>_lap<NN>_<track>_<time>.lap
Index file:   <laps_dir>/index.jsonl
Hot file:     <laps_dir>/hot.jsonl  (current lap only, ephemeral)
Checkpoint:   <laps_dir>/pending.lap  (transient, deleted after seal)
"""
from __future__ import annotations

import collections
import json
import os
import time
from datetime import datetime

from .listener import Frame
from .analysis import Lap, fmt_ms
from .storage import write_lap, append_index, LapMeta
from . import packets as pk

# 4 laps at 60 Hz, up to 120s each = 28,800 frames.  We keep a bit more.
_DEQUE_MAXLEN = 30_000
_RESET_JUMP_M = 50.0       # lapDistance forward jump threshold for reset detection
_RESET_DIST_JUMP_M = 50.0  # same value, aliased for clarity


class LapBuffer:
    """Feed frames; laps seal and write themselves on last_lap_ms change."""

    def __init__(self, laps_dir: str = "laps", verbose: bool = False):
        self.laps_dir = laps_dir
        self.verbose = verbose
        os.makedirs(laps_dir, exist_ok=True)

        self._buf: collections.deque[Frame] = collections.deque(maxlen=_DEQUE_MAXLEN)
        self._prev_last_lap_ms: int = 0    # last known value of last_lap_ms
        self._prev_dist: float | None = None
        self._reset_count: int = 0
        self._lap_start_time: float = 0.0  # session_time of first frame of current lap
        self._sealed: int = 0              # count of laps sealed this session

        self.index_path = os.path.join(laps_dir, "index.jsonl")
        self.hot_path = os.path.join(laps_dir, "hot.jsonl")
        self.pending_path = os.path.join(laps_dir, "pending.lap")

        # Recover any lap that was checkpointed but not fully sealed
        self._recover_pending()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def add(self, f: Frame) -> None:
        """Process one frame from the listener."""
        # --- reset detection (forward jump in lapDistance, same last_lap_ms) ---
        if (self._prev_dist is not None
                and f.lap_distance - self._prev_dist > _RESET_DIST_JUMP_M
                and f.last_lap_ms == self._prev_last_lap_ms):
            self._reset_count += 1

        self._prev_dist = f.lap_distance

        # --- lap completion detection ---
        if (f.last_lap_ms > 0
                and f.last_lap_ms != self._prev_last_lap_ms):
            # Seal: extract all buffered frames belonging to the lap that just finished
            self._seal_lap(f.last_lap_ms, f)
            self._prev_last_lap_ms = f.last_lap_ms
            self._reset_count = 0
            # Mark start of the NEW lap by session_time — immune to deque rolling
            self._lap_start_time = f.session_time

        self._buf.append(f)
        self._write_hot(f)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _recover_pending(self) -> None:
        """On startup: if pending.lap exists, a previous run was killed before
        the next lap crossed the line and triggered the final seal. Recover it
        by moving it to a properly dated .lap file and adding it to the index.
        """
        if not os.path.exists(self.pending_path):
            return
        from .storage import read_lap
        try:
            lap = read_lap(self.pending_path)
            track_id = getattr(lap, "_track_id", 0)
            track_slug = pk.TRACK_IDS.get(track_id, f"t{track_id}")
            track_slug = track_slug.split("(")[0].strip().lower().replace(" ", "_")[:12]
            now = datetime.now().strftime("%Y%m%d_%H%M%S")
            time_str = fmt_ms(lap.lap_time_ms).replace(":", "m").replace(".", "s")
            fname = f"{now}_lap{lap.lap_num:02d}_{track_slug}_{time_str}_recovered.lap"
            fpath = os.path.join(self.laps_dir, fname)
            import shutil
            shutil.move(self.pending_path, fpath)
            meta = LapMeta.from_lap(lap, fname)
            append_index(meta, self.index_path)
            print(f"[lap recovered] {fname}  {fmt_ms(lap.lap_time_ms)}  "
                  f"(was pending from previous run)")
        except Exception as e:
            print(f"[warn] could not recover pending.lap: {e}")
            os.unlink(self.pending_path)

    def _seal_lap(self, lap_time_ms: int, trigger_frame: Frame) -> None:
        """Extract current lap frames, write checkpoint, then final .lap file."""
        # Collect frames from the deque whose session_time >= lap start.
        # Using session_time is immune to the deque rolling — an absolute index
        # would drift as old frames are evicted from the front of the deque.
        frames = [f for f in self._buf if f.session_time >= self._lap_start_time]
        if not frames:
            return

        # Gather context from the most recent frame that has it
        setup = None; session = None; events_list: list = []
        for fr in reversed(frames):
            if setup is None and hasattr(fr, "_setup"):
                setup = fr._setup
            if session is None and hasattr(fr, "_session"):
                session = fr._session
            if setup and session:
                break

        # Build Lap
        lap_num = frames[-1].current_lap_num
        invalid = any(fr.current_lap_invalid for fr in frames)
        lap = Lap(lap_num=lap_num, frames=frames,
                  invalid=invalid, lap_time_ms=lap_time_ms)
        lap.reset_count = self._reset_count
        lap.setup = setup
        lap.session = session
        lap.events = events_list

        # Resolve track info
        track_id = 0; track_length_m = 0
        if session:
            track_id = session.get("track_id", 0)
            track_length_m = session.get("track_length", 0)

        wall_ms = int(time.time() * 1000)
        lap._wall_clock_ms = wall_ms
        lap._track_id = track_id
        lap._track_length_m = track_length_m

        # --- CHECKPOINT: write pending.lap immediately ---
        # If the process is killed or the session restarts before the NEXT
        # lap crosses the line (which would trigger the final seal), this file
        # survives and is recovered on the next startup.
        write_lap(lap, self.pending_path,
                  wall_clock_ms=wall_ms,
                  track_id=track_id,
                  track_length_m=track_length_m)

        # --- Final .lap file + index ---
        track_slug = pk.TRACK_IDS.get(track_id, f"t{track_id}")
        track_slug = track_slug.split("(")[0].strip().lower().replace(" ", "_")[:12]
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        time_str = fmt_ms(lap_time_ms).replace(":", "m").replace(".", "s")
        fname = f"{now}_lap{lap_num:02d}_{track_slug}_{time_str}.lap"
        fpath = os.path.join(self.laps_dir, fname)

        write_lap(lap, fpath,
                  wall_clock_ms=wall_ms,
                  track_id=track_id,
                  track_length_m=track_length_m)

        meta = LapMeta.from_lap(lap, fname)
        append_index(meta, self.index_path)

        # --- Delete checkpoint only after index is safely written ---
        try:
            os.unlink(self.pending_path)
        except FileNotFoundError:
            pass

        self._sealed += 1
        flag = "RESET x%d" % self._reset_count if self._reset_count else (
            "INVALID" if invalid else "clean")
        if self.verbose:
            print(f"\n[lap sealed] {fname}  {fmt_ms(lap_time_ms)} "
                  f"({flag}, {len(frames)}fr)")

    def _write_hot(self, f: Frame) -> None:
        """Append the current frame to hot.jsonl (live preview, current lap only).

        hot.jsonl is rewritten from scratch each new lap to keep it bounded.
        Within a lap we append.  The file is intentionally ephemeral — it is
        not used for analysis, only for live 'tail -f' monitoring.
        """
        if f.session_time == self._lap_start_time:
            # First frame of a new lap — truncate the file
            open(self.hot_path, "w").close()

        rec = {
            "t": "frame",
            "lap_num": f.current_lap_num,
            "lap_distance": round(f.lap_distance, 1),
            "speed": f.speed,
            "throttle": round(f.throttle, 2),
            "brake": round(f.brake, 2),
            "gear": f.gear,
            "invalid": f.current_lap_invalid,
        }
        with open(self.hot_path, "a", buffering=1) as fh:
            fh.write(json.dumps(rec) + "\n")

    def attach_context(self, setup: dict | None = None,
                       session: dict | None = None) -> None:
        """Called by the listener when slowly-changing context packets arrive.
        We tag the most-recently-appended frame so _seal_lap can find it.
        """
        if not self._buf:
            return
        f = self._buf[-1]
        if setup is not None:
            f._setup = setup
        if session is not None:
            f._session = session
