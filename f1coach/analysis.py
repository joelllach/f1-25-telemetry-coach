"""Lap segmentation, distance-grid resampling, and corner metrics.

A Lap holds the raw frames plus channels resampled onto a uniform lapDistance
grid so two laps can be compared point-for-point. Corner detection is a simple,
robust brake-zone finder -- good enough to anchor a coaching summary without a
track-specific corner database.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from .listener import Frame


# A reset/flashback teleports the car: lapDistance jumps forward by a large
# amount in a single frame with NO lap-number change. (A lap-number increment
# with distance wrapping to ~0 is a normal lap completion, not a reset.)
# Validated on real data: a genuine reset jumped distance +2643 m in one frame
# while world position and speed stayed momentarily frozen, then snapped the car
# to a full-speed racing-line state. Real laps creep ~1-5 m/frame.
RESET_DIST_JUMP_M = 50.0


@dataclass
class Lap:
    lap_num: int
    frames: list[Frame] = field(default_factory=list)
    invalid: bool = False
    lap_time_ms: int = 0  # filled from next lap's last_lap_ms when known
    reset_count: int = 0  # number of resets/flashbacks detected within this lap

    @property
    def length_m(self) -> float:
        return self.frames[-1].lap_distance if self.frames else 0.0

    @property
    def was_reset(self) -> bool:
        return self.reset_count > 0

    @property
    def is_clean(self) -> bool:
        """A lap usable as a 'best lap' reference: valid, timed, no resets."""
        return (not self.invalid) and self.reset_count == 0 and self.lap_time_ms > 0


class LapSegmenter:
    """Feed frames in; calls on_lap_complete(Lap) when a lap boundary passes.

    Detects in-lap resets/flashbacks (large forward lapDistance jump with no
    lap-number change) and records them on the Lap as reset_count.
    """

    def __init__(self, on_lap_complete=None, reset_jump_m: float = RESET_DIST_JUMP_M):
        self.on_lap_complete = on_lap_complete
        self.reset_jump_m = reset_jump_m
        self._cur: Lap | None = None
        self._prev_dist: float | None = None

    def add(self, f: Frame):
        if self._cur is None:
            self._cur = Lap(lap_num=f.current_lap_num)
            self._prev_dist = f.lap_distance
        if f.current_lap_num != self._cur.lap_num:
            # lap boundary: finalize previous
            done = self._cur
            done.lap_time_ms = f.last_lap_ms  # last_lap_ms now reflects the lap we just finished
            self._cur = Lap(lap_num=f.current_lap_num)
            self._cur.frames.append(f)
            self._prev_dist = f.lap_distance
            if self.on_lap_complete and done.frames:
                self.on_lap_complete(done)
            return
        # reset/flashback detection within the same lap
        if (self._prev_dist is not None
                and f.lap_distance - self._prev_dist > self.reset_jump_m):
            self._cur.reset_count += 1
        self._prev_dist = f.lap_distance
        if f.current_lap_invalid:
            self._cur.invalid = True
        self._cur.frames.append(f)


# ---- distance-grid resampling -------------------------------------------------

CHANNELS = ("speed", "throttle", "brake", "steer", "gear", "rpm", "drs",
            "g_lat", "g_long", "world_x", "world_y", "session_time")


def resample(lap: Lap, step_m: float = 5.0) -> dict:
    """Return {'dist': [...], 'speed': [...], ...} on a uniform distance grid."""
    fr = [f for f in lap.frames if f.lap_distance >= 0]
    fr.sort(key=lambda f: f.lap_distance)
    if len(fr) < 5:
        return {}
    dists = [f.lap_distance for f in fr]
    end = dists[-1]
    grid = []
    d = 0.0
    while d <= end:
        grid.append(d)
        d += step_m
    out = {"dist": grid}
    for ch in CHANNELS:
        vals = [getattr(f, ch) for f in fr]
        out[ch] = [_interp(grid_d, dists, vals) for grid_d in grid]
    # derived slip channels (wheel order [RL,RR,FL,FR]); JSON may give lists.
    # front_lock/rear_lock: most-negative slip ratio on that axle (lock-up).
    # front_slip/rear_slip: max |slip angle| on that axle (under/oversteer).
    def axle(f, attr, idxs, reducer):
        v = getattr(f, attr, None)
        if not v or len(v) < 4:
            return 0.0
        return reducer(v[i] for i in idxs)
    front_lock = [axle(f, "slip_ratio", (2, 3), min) for f in fr]   # FL,FR
    rear_lock = [axle(f, "slip_ratio", (0, 1), min) for f in fr]    # RL,RR
    front_slip = [axle(f, "slip_angle", (2, 3), lambda g: max(abs(x) for x in g)) for f in fr]
    rear_slip = [axle(f, "slip_angle", (0, 1), lambda g: max(abs(x) for x in g)) for f in fr]
    for name, vals in (("front_lock", front_lock), ("rear_lock", rear_lock),
                       ("front_slip", front_slip), ("rear_slip", rear_slip)):
        out[name] = [_interp(gd, dists, vals) for gd in grid]
    return out


def _interp(x: float, xs: list[float], ys: list[float]) -> float:
    i = bisect.bisect_left(xs, x)
    if i <= 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = ys[i - 1], ys[i]
    if x1 == x0:
        return y0
    t = (x - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


# ---- corner / brake-zone detection -------------------------------------------

@dataclass
class Corner:
    index: int
    brake_start_m: float
    apex_m: float
    min_speed: float
    entry_speed: float
    exit_speed: float
    peak_brake: float
    throttle_reapply_m: float
    gear_at_apex: int
    # slip diagnostics (from MotionEx; 0.0 if that packet wasn't recorded)
    front_lock: float = 0.0   # worst (most negative) front slip ratio: lock-up
    rear_lock: float = 0.0    # worst front-vs-rear tells lock-up balance
    front_slip: float = 0.0   # peak front slip angle (rad): understeer if > rear
    rear_slip: float = 0.0    # peak rear slip angle (rad): oversteer if > front
    handling: str = ""        # heuristic label: understeer/oversteer/lockup/clean


def detect_corners(grid: dict, brake_thresh: float = 0.15,
                   min_gap_m: float = 60.0) -> list[Corner]:
    """Find brake zones and summarize each as a corner."""
    if not grid:
        return []
    dist = grid["dist"]
    brake = grid["brake"]
    speed = grid["speed"]
    throttle = grid["throttle"]
    gear = grid["gear"]
    n = len(dist)

    # find contiguous regions where brake > threshold
    zones = []
    i = 0
    while i < n:
        if brake[i] > brake_thresh:
            j = i
            while j < n and brake[j] > brake_thresh:
                j += 1
            zones.append((i, j - 1))
            i = j
        else:
            i += 1

    # merge zones that are very close together
    merged = []
    for z in zones:
        if merged and dist[z[0]] - dist[merged[-1][1]] < min_gap_m:
            merged[-1] = (merged[-1][0], z[1])
        else:
            merged.append(list(z))

    corners = []
    for idx, (a, b) in enumerate(merged, start=1):
        # apex = min speed point from brake start to a window after brake release
        search_end = min(n - 1, b + int(40 / max(1.0, dist[1] - dist[0])))
        seg = range(a, search_end + 1)
        apex_i = min(seg, key=lambda k: speed[k])
        # throttle reapplication: first point after apex where throttle > 0.5
        reapply_i = apex_i
        for k in range(apex_i, n):
            if throttle[k] > 0.5:
                reapply_i = k
                break
        exit_i = min(n - 1, reapply_i + int(50 / max(1.0, dist[1] - dist[0])))
        # slip over the corner span (brake start -> exit), if MotionEx present
        span = slice(a, exit_i + 1)
        fl = grid.get("front_lock"); rl = grid.get("rear_lock")
        fs = grid.get("front_slip"); rs = grid.get("rear_slip")
        front_lock = round(min(fl[span]), 3) if fl else 0.0
        rear_lock = round(min(rl[span]), 3) if rl else 0.0
        front_slip = round(max(fs[span]), 3) if fs else 0.0
        rear_slip = round(max(rs[span]), 3) if rs else 0.0
        corners.append(Corner(
            index=idx,
            brake_start_m=round(dist[a], 1),
            apex_m=round(dist[apex_i], 1),
            min_speed=round(speed[apex_i], 1),
            entry_speed=round(speed[max(0, a - 1)], 1),
            exit_speed=round(speed[exit_i], 1),
            peak_brake=round(max(brake[a:b + 1]), 2),
            throttle_reapply_m=round(dist[reapply_i], 1),
            gear_at_apex=int(round(gear[apex_i])),
            front_lock=front_lock, rear_lock=rear_lock,
            front_slip=front_slip, rear_slip=rear_slip,
            handling=_handling_label(front_lock, rear_lock, front_slip, rear_slip),
        ))
    return corners


def _handling_label(front_lock, rear_lock, front_slip, rear_slip,
                    lock_thresh=-0.10, slip_thresh=0.10) -> str:
    """Heuristic corner behaviour from slip channels.

    slip ratio < ~-0.10 on an axle = that axle locking under braking.
    slip angle (rad): the axle with the larger angle is the one sliding --
    front sliding => understeer (push), rear sliding => oversteer (loose).
    Thresholds are rough; this is a hint, not a measurement.
    """
    labels = []
    if front_lock <= lock_thresh and front_lock <= rear_lock - 0.04:
        labels.append("front lock-up")
    elif rear_lock <= lock_thresh and rear_lock <= front_lock - 0.04:
        labels.append("rear lock-up")
    if front_slip >= slip_thresh or rear_slip >= slip_thresh:
        if front_slip >= rear_slip + 0.03:
            labels.append("understeer")
        elif rear_slip >= front_slip + 0.03:
            labels.append("oversteer")
    return ", ".join(labels) if labels else "clean"


def time_delta(ref_grid: dict, cur_grid: dict) -> list[float]:
    """Cumulative time delta (cur - ref) in seconds vs distance.

    Approximates per-segment time as step / speed, integrated along the lap.
    Negative delta = current lap is ahead (faster).
    """
    if not ref_grid or not cur_grid:
        return []
    dist = ref_grid["dist"]
    step = (dist[1] - dist[0]) if len(dist) > 1 else 5.0
    out = []
    cum = 0.0
    for i, d in enumerate(dist):
        if i >= len(cur_grid["dist"]):
            break
        rs = max(1.0, ref_grid["speed"][i]) / 3.6  # km/h -> m/s
        cs = max(1.0, cur_grid["speed"][i]) / 3.6
        cum += step / cs - step / rs
        out.append(round(cum, 3))
    return out


def fmt_ms(ms: int) -> str:
    if ms <= 0:
        return "--:--.---"
    m, rem = divmod(ms, 60_000)
    s, msec = divmod(rem, 1000)
    return f"{m}:{s:02d}.{msec:03d}"
