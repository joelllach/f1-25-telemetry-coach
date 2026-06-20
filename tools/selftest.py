"""Fast, self-contained validation of the f1coach pipeline (no PS5, no sleeps).

1. UDP round-trip: pack real 2025-format packets, send to the listener's
   handler, confirm fields decode to what we packed.
2. In-process analysis: synthesize two laps of Frames, run segmentation,
   corner detection, and the dry-run coach summary.
"""
import json
import math

from f1coach import packets as pk
from f1coach.listener import TelemetryListener, Frame
from f1coach.analysis import LapSegmenter, resample, detect_corners, time_delta
from f1coach.coach import build_summary, coach_lap

import tools.fake_telemetry as fake


def test_udp_roundtrip():
    print("== UDP round-trip / byte-layout test ==")
    got = {}
    lis = TelemetryListener(on_frame=lambda f: got.update(frame=f))

    # telemetry packet then lap packet (lap triggers emit)
    lis._handle(fake.telem_packet(1.0, 1, speed=212, throttle=0.75, steer=-0.2,
                                  brake=0.0, gear=5, rpm=11000, drs=1))
    lis._handle(fake.motion_packet(1.0, 1, x=123.5, y=4.0, z=-67.0,
                                   g_lat=1.2, g_long=-0.8))
    lis._handle(fake.status_packet(1.0, 1))
    lis._handle(fake.lap_packet(1.0, 1, lap_dist=850.0, total_dist=850.0,
                                lap_num=2, cur_ms=42000, last_ms=95123,
                                invalid=0, sector=1))
    f = got["frame"]
    checks = [
        ("speed", f.speed, 212),
        ("throttle", round(f.throttle, 2), 0.75),
        ("gear", f.gear, 5),
        ("drs", f.drs, 1),
        ("lap_distance", f.lap_distance, 850.0),
        ("current_lap_num", f.current_lap_num, 2),
        ("current_lap_ms", f.current_lap_ms, 42000),
        ("last_lap_ms", f.last_lap_ms, 95123),
        ("world_x", round(f.world_x, 1), 123.5),
        ("tyre_compound", f.tyre_compound, 16),
        ("tyre_age_laps", f.tyre_age_laps, 3),
    ]
    ok = True
    for name, got_v, exp in checks:
        flag = "OK " if got_v == exp else "FAIL"
        if got_v != exp:
            ok = False
        print(f"  [{flag}] {name:16s} got={got_v!r:12} expect={exp!r}")
    assert ok, "UDP round-trip field mismatch"
    print("  byte layout self-consistent ✔\n")


def _make_lap(lap_num, fast):
    frames = []
    d = 0.0
    t = 0.0
    dt = 1 / 30
    while d < fake.LAP_LEN:
        spd = fake.speed_profile(d, fast)
        v = spd / 3.6
        ahead = fake.speed_profile(min(fake.LAP_LEN, d + 20), fast)
        braking = ahead < spd - 2
        brake = min(1.0, (spd - ahead) / 40) if braking else 0.0
        throttle = 0.0 if braking else min(1.0, 0.7)
        gear = max(1, min(8, int(spd / 35) + 1))
        frames.append(Frame(
            session_time=t, session_uid=1, lap_distance=d, total_distance=d,
            current_lap_num=lap_num, speed=int(spd), throttle=throttle,
            brake=brake, steer=0.0, gear=gear, rpm=int(8000 + spd * 10), drs=0,
            world_x=d, world_y=0.0,
        ))
        d += v * dt
        t += dt
    return frames


def test_analysis_and_coach():
    print("== analysis + corner detection + dry-run coach ==")
    laps = []
    seg = LapSegmenter(on_lap_complete=lambda lap: laps.append(lap))
    # feed lap 1 (slow), lap 2 (fast), then a frame on lap 3 to flush lap 2
    for fr in _make_lap(1, fast=False):
        seg.add(fr)
    for fr in _make_lap(2, fast=True):
        fr.last_lap_ms = 51000  # lap1 time, surfaced when lap2 starts
        seg.add(fr)
    flush = Frame(session_time=999, session_uid=1, lap_distance=0.0,
                  current_lap_num=3, last_lap_ms=49000)
    seg.add(flush)

    assert len(laps) >= 2, f"expected >=2 laps, got {len(laps)}"
    lap1, lap2 = laps[0], laps[1]
    print(f"  segmented {len(laps)} laps; lap1 frames={len(lap1.frames)}, "
          f"lap2 frames={len(lap2.frames)}")

    g1 = resample(lap1)
    g2 = resample(lap2)
    c1 = detect_corners(g1)
    c2 = detect_corners(g2)
    print(f"  corners detected: lap1={len(c1)}, lap2={len(c2)} (expect ~3 each)")
    assert 2 <= len(c1) <= 5, f"corner count off: {len(c1)}"
    for c in c1:
        print(f"    corner {c.index}: brake@{c.brake_start_m}m apex@{c.apex_m}m "
              f"min={c.min_speed}km/h gear={c.gear_at_apex}")

    delta = time_delta(g1, g2)
    print(f"  final time delta (lap2 vs lap1): {delta[-1] if delta else None}s "
          f"(expect negative = lap2 faster)")
    assert delta and delta[-1] < 0, "fast lap should be ahead"

    summary = build_summary(lap2, ref=lap1)
    js = coach_lap(lap2, lap1, dry_run=True)
    parsed = json.loads(js)
    assert "current_lap" in parsed and "corner_comparison" in parsed
    print(f"  dry-run summary keys: {list(parsed.keys())}")
    print(f"  corner_comparison entries: {len(parsed['corner_comparison'])}")
    print("  analysis pipeline ✔\n")


if __name__ == "__main__":
    test_udp_roundtrip()
    test_analysis_and_coach()
    print("ALL SELFTESTS PASSED ✔")
