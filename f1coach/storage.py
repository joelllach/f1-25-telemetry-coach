"""Binary .lap file format for f1coach.

Each .lap file is a single completed lap: header + frame rows.

Layout
------
HEADER (fixed, 64 bytes):
  4s  magic          b"LAP1"
  H   version        format version (currently 1)
  H   frame_size     bytes per frame row (currently 60)
  Q   session_uid    from the game
  Q   wall_clock     unix timestamp (ms) when lap completed
  I   lap_num        game's lap counter
  I   lap_time_ms    official game time (last_lap_ms) — the authoritative value
  I   frame_count    number of frame rows following
  H   track_id       from Session packet (pk.TRACK_IDS)
  H   track_length_m approximate circuit length in metres
  H   reset_count    in-lap resets detected
  B   invalid        1 if game set the invalid flag on any frame
  B   _pad           reserved
  I   meta_len       bytes of JSON metadata blob that follows immediately

METADATA (variable, meta_len bytes):
  UTF-8 JSON: {"setup": {...}, "session": {...}, "events": [...]}

FRAME ROWS (frame_size × frame_count bytes):
  Each row is a fixed struct — see _FRAME_STRUCT below.
  Wheel order throughout: [RL, RR, FL, FR]

Reading back is version-safe: if version > 1 we still parse what we know
(header fields are additive), and we clamp frame parsing to min(stored, known).
"""
from __future__ import annotations

import json
import os
import struct
import time
from dataclasses import dataclass, field

from .listener import Frame
from .analysis import Lap

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAGIC = b"LAP1"
FORMAT_VERSION = 1

# Header struct (everything up to meta_len, exclusive of meta blob)
_HDR = struct.Struct("<4s H H Q Q I I I H H H B B I")
HDR_SIZE = _HDR.size  # 48 bytes

# Per-frame struct — 60 bytes
# lap_distance     f32      4
# session_time     f32      4
# speed            u16      2   km/h
# throttle         u8       1   0–100
# brake            u8       1   0–100
# steer            i8       1   -100..+100 (×100 of -1..+1)
# gear             i8       1   -1..8
# rpm              u16      2
# drs              u8       1   0/1
# current_lap_invalid u8    1   0/1
# offtrack         u8       1   0/1
# _pad             u8       1   reserved
# world_x          f32      4
# world_y          f32      4
# g_lat            f16      2   (half precision — ±8g sufficient)
# g_long           f16      2
# slip_ratio       4×i8     4   ×100, clamp ±1.27
# slip_angle       4×i8     4   ×100, clamp ±1.27
# surface_type     4×u8     4
# tyre_surface_temp 4×u8    4   0–255 °C
# tyre_inner_temp  4×u8     4   0–255 °C
# _reserved        8×u8     8   future fields, zero-filled
# Total                    60
_FRAME = struct.Struct("<f f H B B b b H B B B B f f e e 4b 4b 4B 4B 4B 8B")
FRAME_SIZE = _FRAME.size  # must be 60 — validated at module load

assert FRAME_SIZE == 60, f"FRAME_SIZE mismatch: {FRAME_SIZE}"


# ---------------------------------------------------------------------------
# Frame encode/decode helpers
# ---------------------------------------------------------------------------

def _clamp_i8_x100(v: float) -> int:
    return max(-127, min(127, int(round(v * 100))))


def _encode_frame(f: Frame) -> bytes:
    sr = getattr(f, "slip_ratio", (0.0,) * 4)
    sa = getattr(f, "slip_angle", (0.0,) * 4)
    st = getattr(f, "surface_type", (0,) * 4)
    tst = getattr(f, "tyre_surface_temp", (0,) * 4)
    tit = getattr(f, "tyre_inner_temp", (0,) * 4)
    return _FRAME.pack(
        f.lap_distance,
        f.session_time,
        min(65535, max(0, int(f.speed))),
        min(100, max(0, int(round(f.throttle * 100)))),
        min(100, max(0, int(round(f.brake * 100)))),
        _clamp_i8_x100(f.steer),
        max(-1, min(8, int(f.gear))),
        min(65535, max(0, int(f.rpm))),
        int(bool(f.drs)),
        int(bool(f.current_lap_invalid)),
        int(bool(getattr(f, "offtrack", 0))),
        0,  # pad
        f.world_x,
        f.world_y,
        f.g_lat,
        f.g_long,
        *[_clamp_i8_x100(v) for v in sr],
        *[_clamp_i8_x100(v) for v in sa],
        *[min(255, max(0, int(v))) for v in st],
        *[min(255, max(0, int(v))) for v in tst],
        *[min(255, max(0, int(v))) for v in tit],
        *([0] * 8),  # reserved for future fields
    )


def _decode_frame(data: bytes, offset: int, lap_num: int) -> Frame:
    v = _FRAME.unpack_from(data, offset)
    (lap_distance, session_time, speed, thr100, brk100, steer100, gear,
     rpm, drs, invalid, offtrack, _pad,
     world_x, world_y, g_lat, g_long,
     sr0, sr1, sr2, sr3,
     sa0, sa1, sa2, sa3,
     st0, st1, st2, st3,
     tst0, tst1, tst2, tst3,
     tit0, tit1, tit2, tit3,
     *_reserved) = v
    return Frame(
        session_time=session_time,
        session_uid=0,  # not stored per-frame; available in header
        lap_distance=lap_distance,
        total_distance=0.0,
        current_lap_num=lap_num,
        current_lap_ms=0,
        last_lap_ms=0,
        current_lap_invalid=invalid,
        sector=0,
        car_position=0,
        speed=speed,
        throttle=thr100 / 100.0,
        brake=brk100 / 100.0,
        steer=steer100 / 100.0,
        gear=gear,
        rpm=rpm,
        drs=drs,
        world_x=world_x,
        world_y=world_y,
        world_z=0.0,
        g_lat=g_lat,
        g_long=g_long,
        tyre_compound=0,
        tyre_age_laps=0,
        ers_store=0.0,
        slip_ratio=(sr0 / 100.0, sr1 / 100.0, sr2 / 100.0, sr3 / 100.0),
        slip_angle=(sa0 / 100.0, sa1 / 100.0, sa2 / 100.0, sa3 / 100.0),
        tyre_surface_temp=(tst0, tst1, tst2, tst3),
        tyre_inner_temp=(tit0, tit1, tit2, tit3),
        surface_type=(st0, st1, st2, st3),
        offtrack=offtrack,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_lap(lap: Lap, path: str,
              session_uid: int = 0,
              wall_clock_ms: int | None = None,
              track_id: int = 0,
              track_length_m: int = 0) -> None:
    """Serialise a Lap to a binary .lap file."""
    if wall_clock_ms is None:
        wall_clock_ms = int(time.time() * 1000)

    # Metadata blob
    meta = {
        "setup": getattr(lap, "setup", None),
        "session": getattr(lap, "session", None),
        "events": getattr(lap, "events", None),
    }
    meta_bytes = json.dumps(meta, separators=(",", ":")).encode()

    invalid_flag = 1 if lap.invalid else 0
    reset_count = min(65535, getattr(lap, "reset_count", 0))

    hdr = _HDR.pack(
        MAGIC, FORMAT_VERSION, FRAME_SIZE,
        session_uid, wall_clock_ms,
        lap.lap_num, lap.lap_time_ms, len(lap.frames),
        track_id, track_length_m,
        reset_count, invalid_flag, 0,  # pad
        len(meta_bytes),
    )

    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(hdr)
        fh.write(meta_bytes)
        for f in lap.frames:
            fh.write(_encode_frame(f))


def read_lap(path: str) -> Lap:
    """Deserialise a .lap file back into a Lap (with metadata attached)."""
    with open(path, "rb") as fh:
        raw = fh.read()

    if raw[:4] != MAGIC:
        raise ValueError(f"Not a .lap file: {path}")

    v = _HDR.unpack_from(raw, 0)
    (magic, version, frame_size, session_uid, wall_clock_ms,
     lap_num, lap_time_ms, frame_count,
     track_id, track_length_m,
     reset_count, invalid_flag, _pad, meta_len) = v

    meta_start = HDR_SIZE
    meta_end = meta_start + meta_len
    meta = json.loads(raw[meta_start:meta_end]) if meta_len else {}

    frames_start = meta_end
    read_size = min(frame_size, FRAME_SIZE)  # version-safe: read what we know
    frames = []
    for i in range(frame_count):
        off = frames_start + i * frame_size
        if off + read_size > len(raw):
            break
        frames.append(_decode_frame(raw, off, lap_num))

    lap = Lap(lap_num=lap_num, frames=frames,
              invalid=bool(invalid_flag), lap_time_ms=lap_time_ms)
    lap.reset_count = reset_count
    lap.setup = meta.get("setup")
    lap.session = meta.get("session")
    lap.events = meta.get("events") or []
    lap._session_uid = session_uid
    lap._wall_clock_ms = wall_clock_ms
    lap._track_id = track_id
    lap._track_length_m = track_length_m
    return lap


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

@dataclass
class LapMeta:
    """Lightweight record written to index.jsonl — no frame data."""
    file: str             # relative path to the .lap file
    wall_clock_ms: int
    session_uid: int
    lap_num: int
    lap_time_ms: int
    track_id: int
    track_length_m: int
    reset_count: int
    invalid: bool
    frame_count: int

    def lap_time_str(self) -> str:
        ms = self.lap_time_ms
        if ms <= 0:
            return "--:--.---"
        m, rem = divmod(ms, 60_000)
        s, msec = divmod(rem, 1_000)
        return f"{m}:{s:02d}.{msec:03d}"

    def to_dict(self) -> dict:
        return {
            "file": self.file, "wall_clock_ms": self.wall_clock_ms,
            "session_uid": self.session_uid, "lap_num": self.lap_num,
            "lap_time_ms": self.lap_time_ms, "track_id": self.track_id,
            "track_length_m": self.track_length_m,
            "reset_count": self.reset_count, "invalid": self.invalid,
            "frame_count": self.frame_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LapMeta":
        return cls(**d)

    @classmethod
    def from_lap(cls, lap: Lap, file: str) -> "LapMeta":
        return cls(
            file=file,
            wall_clock_ms=getattr(lap, "_wall_clock_ms", int(time.time() * 1000)),
            session_uid=getattr(lap, "_session_uid", 0),
            lap_num=lap.lap_num,
            lap_time_ms=lap.lap_time_ms,
            track_id=getattr(lap, "_track_id", 0),
            track_length_m=getattr(lap, "_track_length_m", 0),
            reset_count=getattr(lap, "reset_count", 0),
            invalid=lap.invalid,
            frame_count=len(lap.frames),
        )


def append_index(meta: LapMeta, index_path: str) -> None:
    """Append one LapMeta line to the JSONL index file."""
    with open(index_path, "a", buffering=1) as fh:
        fh.write(json.dumps(meta.to_dict()) + "\n")


def read_index(index_path: str) -> list[LapMeta]:
    """Read all entries from an index.jsonl file."""
    if not os.path.exists(index_path):
        return []
    metas = []
    with open(index_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    metas.append(LapMeta.from_dict(json.loads(line)))
                except Exception:
                    pass
    return metas
