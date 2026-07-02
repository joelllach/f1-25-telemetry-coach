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


def test_storage_roundtrip():
    """Write a Lap to a .lap binary, read it back, confirm fields match."""
    import os, tempfile
    from f1coach.analysis import Lap
    from f1coach.storage import write_lap, read_lap, FRAME_SIZE, HDR_SIZE

    print("== storage: binary .lap round-trip ==")
    print(f"  FRAME_SIZE={FRAME_SIZE} bytes (expect 60)")
    assert FRAME_SIZE == 60

    # Build a synthetic lap with varied frame values
    frames = []
    for i in range(200):
        d = float(i * 22)
        frames.append(Frame(
            session_time=float(i) * (1/60), session_uid=42,
            lap_distance=d, total_distance=d,
            current_lap_num=3, speed=100+i, throttle=min(1.0, i/100),
            brake=max(0.0, 1.0 - i/100), steer=(i % 20 - 10) / 100.0,
            gear=max(1, min(8, i//25+1)), rpm=8000+i*10, drs=i%2,
            world_x=float(i*5), world_y=float(i*2), world_z=0.0,
            g_lat=0.1, g_long=-0.2,
            tyre_compound=16, tyre_age_laps=3, ers_store=2e6,
            slip_ratio=(-0.05, -0.04, -0.15, -0.18),
            slip_angle=(0.05, 0.06, 0.12, 0.13),
            tyre_surface_temp=(90, 91, 88, 89),
            tyre_inner_temp=(95, 96, 93, 94),
            surface_type=(0, 0, 0, 0),
            current_lap_invalid=0, offtrack=0,
        ))

    lap = Lap(lap_num=3, frames=frames, invalid=False, lap_time_ms=89253)
    lap.reset_count = 2
    lap.setup = {"brake_bias": 51, "front_wing": 12, "rear_wing": 0}
    lap.session = {"track_id": 7, "track_length": 5891}
    lap.events = [{"code": "BUTN", "lap_distance": 500.0}]
    lap._wall_clock_ms = 1_700_000_000_000
    lap._track_id = 7
    lap._track_length_m = 5891

    fh = tempfile.NamedTemporaryFile(suffix=".lap", delete=False)
    fh.close()
    try:
        write_lap(lap, fh.name, session_uid=99,
                  wall_clock_ms=1_700_000_000_000,
                  track_id=7, track_length_m=5891)
        sz = os.path.getsize(fh.name)
        print(f"  wrote {sz} bytes "
              f"({HDR_SIZE}hdr + meta + {FRAME_SIZE}×{len(frames)}frames)")

        lap2 = read_lap(fh.name)
        checks = [
            ("lap_num",      lap2.lap_num,        3),
            ("lap_time_ms",  lap2.lap_time_ms,     89253),
            ("frame_count",  len(lap2.frames),     200),
            ("reset_count",  lap2.reset_count,     2),
            ("invalid",      lap2.invalid,         False),
            ("setup.bias",   lap2.setup["brake_bias"] if lap2.setup else None, 51),
            ("track_id",     lap2._track_id,       7),
        ]
        for name, got, exp in checks:
            ok = "OK " if got == exp else "FAIL"
            if got != exp: print(f"  [{ok}] {name}: got={got!r} exp={exp!r}")
            assert got == exp, f"{name} mismatch"

        # Spot-check a frame
        f0 = lap2.frames[0]; f99 = lap2.frames[99]
        assert abs(f0.lap_distance - 0.0) < 1.0, f"f0.lap_dist={f0.lap_distance}"
        assert abs(f99.lap_distance - 99*22.0) < 1.0, f"f99.lap_dist={f99.lap_distance}"
        assert abs(f99.throttle - 0.99) < 0.01, f"throttle={f99.throttle}"
        assert f99.slip_ratio[2] == round(-0.15 * 100) / 100 or abs(f99.slip_ratio[2] - (-0.15)) < 0.015
        print(f"  frame spot-checks ✔  (f0.dist={f0.lap_distance:.0f}m, "
              f"f99.dist={f99.lap_distance:.0f}m, thr={f99.throttle:.2f})")
        print("  .lap round-trip ✔\n")
    finally:
        os.unlink(fh.name)


def test_last_lap_ms_boundary():
    """LapBuffer seals on last_lap_ms change, NOT on a reset."""
    import tempfile, os
    from f1coach.lap_buffer import LapBuffer

    print("== LapBuffer: last_lap_ms boundary signal ==")

    td = tempfile.mkdtemp()
    buf = LapBuffer(laps_dir=td, verbose=False)

    def _frame(lap_num, dist, last_lap_ms, invalid=0):
        return Frame(
            session_time=0.0, session_uid=1,
            lap_distance=dist, total_distance=dist,
            current_lap_num=lap_num, speed=200,
            throttle=1.0, brake=0.0, steer=0.0,
            gear=6, rpm=11000, drs=0,
            world_x=dist, world_y=0.0, world_z=0.0,
            g_lat=0.0, g_long=0.0,
            tyre_compound=16, tyre_age_laps=3, ers_store=2e6,
            last_lap_ms=last_lap_ms,
            current_lap_invalid=invalid,
        )

    # Lap 1: drive 0..4320m, no seal yet (last_lap_ms still 0)
    for d in range(0, 4320, 50):
        buf.add(_frame(1, float(d), 0))
    assert buf._sealed == 0, "should not have sealed yet"

    # In-lap reset at 800m: dist jumps forward — should NOT seal
    buf.add(_frame(1, 800.0, 0))    # reset lands here
    buf.add(_frame(1, 3200.0, 0))   # forward jump, same last_lap_ms
    assert buf._sealed == 0, "reset must NOT trigger a seal"
    assert buf._reset_count == 1, "reset_count should be 1"

    # Lap completion: last_lap_ms changes to 89253
    for d in range(3200, 5890, 50):
        buf.add(_frame(1, float(d), 0))
    buf.add(_frame(2, 10.0, 89253))   # crosses line, new lap starts
    assert buf._sealed == 1, f"should have sealed exactly 1 lap, got {buf._sealed}"

    # Verify the .lap file exists and has the right time
    from f1coach.storage import read_index
    metas = read_index(os.path.join(td, "index.jsonl"))
    assert len(metas) == 1, f"expected 1 index entry, got {len(metas)}"
    assert metas[0].lap_time_ms == 89253, f"lap_time_ms={metas[0].lap_time_ms}"
    assert metas[0].reset_count == 1, f"reset_count={metas[0].reset_count}"
    print(f"  sealed 1 lap: {metas[0].lap_time_str()} "
          f"(reset_count={metas[0].reset_count}) ✔")

    # Another reset (no last_lap_ms change) should NOT seal
    buf.add(_frame(2, 200.0, 89253))   # still same last_lap_ms
    buf.add(_frame(2, 3500.0, 89253))  # reset forward
    assert buf._sealed == 1, "second reset must not seal"
    print("  reset with unchanged last_lap_ms does not seal ✔")

    # Deque rolling: push 15 more laps through to force the deque to roll over
    # old frames. Each lap seals when last_lap_ms changes. The key check:
    # frame extraction must still work correctly after rolling.
    base_t = 200.0
    base_ms = 89253
    for lap_i in range(3, 18):
        lap_t = base_t + (lap_i - 2) * 90.0       # ~90s per lap
        for d in range(0, 5890, 50):
            buf.add(_frame(lap_i, float(d), base_ms,
                           # session_time increments so deque fills up
                           # (reuse _frame but we need session_time — patch it)
                           ))
        # Cross the line
        new_ms = base_ms + (lap_i - 2) * 1000
        next_frame = _frame(lap_i + 1, 10.0, new_ms)
        next_frame = next_frame.__class__(**{**next_frame.__dataclass_fields__,
                                             **{k: getattr(next_frame, k)
                                                for k in next_frame.__dataclass_fields__}})
        buf.add(_frame(lap_i + 1, 10.0, new_ms))
        base_ms = new_ms

    assert buf._sealed > 1, f"should have sealed multiple laps after deque rolling, got {buf._sealed}"
    metas2 = read_index(os.path.join(td, "index.jsonl"))
    # Every sealed lap should have frames
    empty = [m for m in metas2 if m.frame_count == 0]
    assert not empty, f"sealed laps with 0 frames after deque roll: {empty}"
    print(f"  deque-rolling: {buf._sealed} laps sealed, none empty ✔\n")

    import shutil; shutil.rmtree(td)


if __name__ == "__main__":
    test_udp_roundtrip()
    test_analysis_and_coach()
    test_storage_roundtrip()
    test_last_lap_ms_boundary()
    print("ALL SELFTESTS PASSED ✔")
