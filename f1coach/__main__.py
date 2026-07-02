"""f1coach CLI.

Modes:
  monitor   Print live player-car telemetry as it arrives (sanity check).
  debrief   Segment laps; after each completed lap, coach it vs your best lap.
  counts    Print packet-id receipt counts (diagnose what the game is sending).
  record    Log every frame to a JSONL file (run as a background process).
  analyze   Read a recorded JSONL file, rebuild laps, and coach them offline.

Run from the project root (~/tb/f1):  python -m f1coach <mode> [options]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import time

from .listener import TelemetryListener, Frame
from .analysis import LapSegmenter, Lap, fmt_ms
from .coach import coach_lap


def cmd_monitor(args):
    last = {"lap": -1}

    def on_frame(f: Frame):
        # throttle console output to ~5 Hz by distance
        print(f"\rlap {f.current_lap_num} "
              f"d={f.lap_distance:7.1f}m  "
              f"spd={f.speed:3d}km/h  thr={f.throttle:0.2f} brk={f.brake:0.2f} "
              f"gear={f.gear} drs={f.drs}  pos={f.car_position}   ",
              end="", flush=True)
        if f.current_lap_num != last["lap"]:
            last["lap"] = f.current_lap_num
            print()  # newline on new lap

    lis = TelemetryListener(port=args.port, on_frame=on_frame)
    lis.run()


def cmd_counts(args):
    lis = TelemetryListener(port=args.port)
    lis.open()
    import time
    print(f"[listening {args.seconds}s on udp :{args.port}]")
    t0 = time.time()
    try:
        while time.time() - t0 < args.seconds:
            try:
                data, _ = lis.sock.recvfrom(4096)
            except Exception:
                continue
            lis._handle(data)
    finally:
        lis.close()
    names = {0: "Motion", 1: "Session", 2: "LapData", 3: "Event", 4: "Participants",
             5: "CarSetups", 6: "CarTelemetry", 7: "CarStatus", 8: "FinalClass",
             9: "Lobby", 10: "CarDamage", 11: "SessionHistory", 12: "TyreSets",
             13: "MotionEx", 14: "TimeTrial"}
    print(f"\npacketFormat seen: {lis.last_packet_format}")
    for pid, c in sorted(lis.packet_counts.items()):
        print(f"  id {pid:2d} {names.get(pid,'?'):16s} : {c}")


def cmd_debrief(args):
    state = {"best": None}  # type: dict

    def on_lap(lap: Lap):
        valid = not lap.invalid
        tag = "INVALID" if lap.invalid else "valid"
        print(f"\n{'='*60}\nLap {lap.lap_num} complete — {fmt_ms(lap.lap_time_ms)} "
              f"({tag}, {len(lap.frames)} frames)\n{'='*60}")
        best = state["best"]
        # update best (valid laps only, and must have a real time)
        if valid and lap.lap_time_ms > 0 and (best is None or lap.lap_time_ms < best.lap_time_ms):
            improved = best is not None
            state["best"] = lap
            if improved:
                print("** new best lap **")
        ref = state["best"] if (state["best"] is not None and state["best"] is not lap) else None
        out = coach_lap(lap, ref, model=args.model, dry_run=args.dry_run)
        print(out)

    seg = LapSegmenter(on_lap_complete=on_lap)
    lis = TelemetryListener(port=args.port, on_frame=seg.add)
    lis.run()


def cmd_record(args):
    """Record to per-lap binary .lap files via LapBuffer (new architecture).

    Each completed lap (detected via last_lap_ms change) is written as an
    individual .lap binary file in <laps_dir>/.  An index.jsonl tracks all
    laps.  hot.jsonl holds the current in-progress lap for live monitoring.

    The old --out JSONL mode is retained as --legacy-jsonl for backward compat.
    """
    from .lap_buffer import LapBuffer

    buf = LapBuffer(laps_dir=args.laps_dir, verbose=True)

    def on_frame(f: Frame):
        buf.add(f)

    def on_context(**kwargs):
        buf.attach_context(**kwargs)

    def on_event(code: str, f: Frame):
        pass  # events not yet forwarded to LapBuffer (future: attach to lap)

    print(f"[record] laps -> {args.laps_dir}/  "
          f"(index: index.jsonl, live: hot.jsonl)  Ctrl-C to stop")
    lis = TelemetryListener(port=args.port, on_frame=on_frame)
    lis.on_event = on_event
    lis.on_context = on_context
    try:
        lis.run()
    finally:
        print(f"[record] done: {buf._sealed} laps sealed -> {args.laps_dir}/")
    try:
        lis.run()
    finally:
        fh.write(json.dumps({"t": "end", "wall": time.time(),
                             "frames": counters["frames"], "laps": counters["laps"],
                             "events": counters["events"]}) + "\n")
        fh.close()
        print(f"[record] done: {counters['frames']} frames, {counters['laps']} laps, "
              f"{counters['events']} events -> {args.out}")


def _load_laps_binary(path: str) -> list[Lap]:
    """Load laps from a .lap file or a directory of .lap files (via index.jsonl)."""
    from .storage import read_lap, read_index
    if path.endswith(".lap"):
        return [read_lap(path)]
    # Directory: read index.jsonl and load each file
    index_path = os.path.join(path, "index.jsonl")
    metas = read_index(index_path)
    laps = []
    for m in metas:
        fpath = os.path.join(path, m.file)
        if os.path.exists(fpath):
            try:
                laps.append(read_lap(fpath))
            except Exception as e:
                print(f"[warn] could not read {fpath}: {e}")
    return laps


def _load_laps(path: str) -> list[Lap]:
    """Reconstruct laps from a recorded JSONL file.

    Frames rebuild each Lap. Lap markers carry the authoritative lap time plus
    a snapshot of setup/session/damage; events are bucketed by lap_num. The
    extra context is attached to each Lap as plain attributes (setup, session,
    events) for the coach to use.
    """
    fields = {f.name for f in dataclasses.fields(Frame)}
    laps: list[Lap] = []
    lap_meta: dict[int, dict] = {}
    events_by_lap: dict[int, list] = {}
    seg = LapSegmenter(on_lap_complete=lambda lap: laps.append(lap))
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            kind = rec.get("t")
            if kind == "frame":
                kwargs = {k: rec[k] for k in fields if k in rec}
                seg.add(Frame(**kwargs))
            elif kind == "lap":
                lap_meta[rec["lap_num"]] = rec
            elif kind == "event":
                events_by_lap.setdefault(rec.get("lap_num", -1), []).append(rec)
    # attach recorded lap times + context (more reliable than re-derived)
    for lap in laps:
        m = lap_meta.get(lap.lap_num)
        if m:
            lap.lap_time_ms = m.get("lap_time_ms", lap.lap_time_ms)
            lap.invalid = m.get("invalid", lap.invalid)
            lap.setup = _resanitize_setup(m.get("setup"))
            lap.session = m.get("session")
            lap.damage = m.get("damage")
        lap.events = events_by_lap.get(lap.lap_num, [])
    return laps


def _resanitize_setup(s: dict | None) -> dict | None:
    """Re-apply range filtering to setup dicts loaded from older recordings
    (which may have stored raw, mis-parsed tail fields)."""
    if not s:
        return s
    import f1coach.packets as pk
    out = {}
    for k, v in s.items():
        if k == "tyre_pressures":
            out[k] = v if (isinstance(v, list) and all(15.0 <= p <= 40.0 for p in v)) else None
            continue
        lo_hi = pk._SETUP_RANGES.get(k)
        out[k] = None if (lo_hi and (v is None or not (lo_hi[0] <= v <= lo_hi[1]))) else v
    return out


def parse_time(s: str | None) -> int | None:
    """Parse a target lap time -> ms. Accepts 'M:SS.mmm', 'SS.mmm', or ms int.
    e.g. '1:47.478' -> 107478, '107.478' -> 107478, '107478' -> 107478."""
    if not s:
        return None
    s = s.strip()
    if ":" in s:
        m, rest = s.split(":", 1)
        return int(m) * 60_000 + round(float(rest) * 1000)
    if "." in s:
        return round(float(s) * 1000)
    return int(s)


def cmd_analyze(args):
    # Route to binary reader for .lap files / directories, legacy for JSONL
    f = args.file
    if f.endswith(".lap") or (os.path.isdir(f) and
                              os.path.exists(os.path.join(f, "index.jsonl"))):
        laps = _load_laps_binary(f)
    else:
        laps = _load_laps(f)
    if not laps:
        print(f"[analyze] no complete laps found in {args.file}")
        return
    target_ms = parse_time(getattr(args, "target", None))
    # best reference = cleanest fast lap (valid, timed, NO resets)
    clean = [l for l in laps if l.is_clean]
    best = min(clean, key=lambda l: l.lap_time_ms) if clean else None

    print(f"[analyze] {len(laps)} lap(s) in {args.file}"
          + (f"  | target {fmt_ms(target_ms)}" if target_ms else ""))
    for l in laps:
        if l.was_reset:
            flag = f"RESET x{l.reset_count}"
        elif l.invalid:
            flag = "INVALID"
        else:
            flag = "clean"
        star = "  <- best clean" if l is best else ""
        tgt = ""
        if target_ms and l.is_clean and l.lap_time_ms > 0:
            d = (l.lap_time_ms - target_ms) / 1000.0
            tgt = f"  [{d:+.3f}s vs target]"
        print(f"   lap {l.lap_num}: {fmt_ms(l.lap_time_ms)} "
              f"({flag}, {len(l.frames)} frames){tgt}{star}")

    # which laps to coach
    if getattr(args, "all_laps", False):
        targets = laps
    elif args.lap is not None:
        targets = [l for l in laps if l.lap_num == args.lap]
        if not targets:
            print(f"[analyze] lap {args.lap} not found")
            return
    elif best is not None:
        targets = [best]  # default: coach the best clean lap
    else:
        targets = laps  # nothing clean to pick; show them all

    for lap in targets:
        ref = best if (best is not None and best is not lap) else None
        tags = []
        if lap.was_reset:
            tags.append(f"RESET x{lap.reset_count}")
        if lap.invalid and not lap.was_reset:
            tags.append("invalid")
        if ref:
            tags.append(f"vs best lap {ref.lap_num}")
        tagstr = f" ({', '.join(tags)})" if tags else ""
        print(f"\n{'='*60}\nLap {lap.lap_num} — {fmt_ms(lap.lap_time_ms)}{tagstr}\n{'='*60}")
        print(coach_lap(lap, ref, model=args.model, dry_run=args.dry_run,
                        target_ms=target_ms))


def cmd_publish(args):
    """Mark a specific lap as publish-approved and write it into racing.json.

    Only laps you explicitly name here are ever published. Nothing auto-posts.
    racing.json is the data feed the tokenburner.ai /racing page reads.
    """
    laps = {l.lap_num: l for l in _load_laps(args.file)}
    lap = laps.get(args.lap)
    if lap is None:
        print(f"[publish] lap {args.lap} not found in {args.file}")
        return
    # NOTE: In Time Trial, resetting to track / restarting a flying lap is normal
    # and the game still validates the lap — the lap_time_ms here comes from the
    # game's own timing field, so it matches your in-game record screen. We do NOT
    # block reset laps; we just note them. The lap time is the authority.
    if lap.was_reset:
        print(f"[publish] note: lap {args.lap} had {lap.reset_count} in-lap reset(s) "
              f"(normal for Time Trial — game-validated time {fmt_ms(lap.lap_time_ms)} is used).")

    track = "Unknown"
    conditions = {}
    sess = getattr(lap, "session", None)
    if sess:
        from f1coach import packets as pk
        track = pk.TRACK_IDS.get(sess.get("track_id"), f"track #{sess.get('track_id')}")
        conditions = {
            "track_temp_c": sess.get("track_temp"),
            "air_temp_c": sess.get("air_temp"),
            "weather": pk.WEATHER.get(sess.get("weather"), sess.get("weather")),
        }

    entry = {
        "track": track,
        "lap_time": fmt_ms(lap.lap_time_ms),
        "lap_time_ms": lap.lap_time_ms,
        "game": "F1 25",
        "date": args.date,           # pass explicit date (no clock in this env)
        "conditions": conditions,
        "setup": getattr(lap, "setup", None),
        "notes": args.note or [],
        "source_session": os.path.basename(args.file),
        "source_lap": lap.lap_num,
    }

    # load existing, append/replace by (track, lap_time_ms)
    racing = {"game": "F1 25", "driver": args.driver, "records": []}
    if os.path.exists(args.out):
        with open(args.out) as fh:
            racing = json.load(fh)
    racing.setdefault("records", [])
    # de-dupe: same track+time replaces
    racing["records"] = [r for r in racing["records"]
                         if not (r.get("track") == track
                                 and r.get("lap_time_ms") == lap.lap_time_ms)]
    racing["records"].append(entry)
    # sort by track then time
    racing["records"].sort(key=lambda r: (r.get("track", ""), r.get("lap_time_ms", 0)))

    with open(args.out, "w") as fh:
        json.dump(racing, fh, indent=2)
    print(f"[publish] added {track} {entry['lap_time']} -> {args.out} "
          f"({len(racing['records'])} record(s) total)")
    print("  Review the file, then copy it to the site's static/racing.json when ready.")


def cmd_write(args):
    """Write raw deduplicated telemetry to raw.jsonl (new architecture)."""
    from .writer import TelemetryWriter
    w = TelemetryWriter(laps_dir=args.laps_dir, port=args.port)
    w.run()


def cmd_process(args):
    """Extract .lap files from raw.jsonl (new architecture)."""
    from .processor import process_once, watch
    if args.watch is not None:
        watch(laps_dir=args.laps_dir, interval=args.watch)
    else:
        n = process_once(laps_dir=args.laps_dir, verbose=True)
        print(f"[process] {n} new lap(s) written")


def cmd_laps(args):
    """List laps from index.jsonl with times, flags, and optional target gap."""
    from .storage import read_index
    from f1coach import packets as pk
    target_ms = parse_time(getattr(args, "target", None))
    metas = read_index(os.path.join(args.laps_dir, "index.jsonl"))
    if not metas:
        print(f"[laps] no laps found in {args.laps_dir}/index.jsonl")
        return
    track_filter = (args.track or "").lower()
    best_ms: dict[int, int] = {}  # track_id -> best clean ms
    for m in metas:
        if not m.invalid and m.reset_count == 0 and m.lap_time_ms > 0:
            if m.lap_time_ms < best_ms.get(m.track_id, 999_999_999):
                best_ms[m.track_id] = m.lap_time_ms
    print(f"{'Track':25} {'Lap':>4}  {'Time':>10}  {'Status':12}  {'Frames':>7}  {'Note'}")
    print("-" * 80)
    for m in metas:
        track = pk.TRACK_IDS.get(m.track_id, f"track#{m.track_id}")
        if track_filter and track_filter not in track.lower():
            continue
        flag = (f"RESET x{m.reset_count}" if m.reset_count else
                "INVALID" if m.invalid else "clean")
        tgt = ""
        if target_ms and not m.invalid and m.lap_time_ms > 0:
            d = (m.lap_time_ms - target_ms) / 1000.0
            tgt = f"  [{d:+.3f}s]" + (" *** RECORD ***" if d < 0 else "")
        star = " ← best" if (m.lap_time_ms == best_ms.get(m.track_id) and
                              not m.invalid and m.reset_count == 0) else ""
        print(f"  {track[:25]:25} {m.lap_num:4d}  {m.lap_time_str():>10}  "
              f"{flag:12}  {m.frame_count:7d}{tgt}{star}")


def main():
    ap = argparse.ArgumentParser(prog="f1coach")
    ap.add_argument("--port", type=int, default=20777)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("monitor", help="print live telemetry")
    m.set_defaults(func=cmd_monitor)

    c = sub.add_parser("counts", help="count packet types received")
    c.add_argument("--seconds", type=int, default=10)
    c.set_defaults(func=cmd_counts)

    d = sub.add_parser("debrief", help="coach each completed lap")
    d.add_argument("--model", default=None,
                   help="override model id (default: resolved from backend, "
                        "Sonnet; see F1COACH_MODEL)")
    d.add_argument("--dry-run", action="store_true",
                   help="print the JSON summary instead of calling Claude")
    d.set_defaults(func=cmd_debrief)

    r = sub.add_parser("record", help="[LEGACY] record to per-lap .lap files via LapBuffer")
    r.add_argument("--laps-dir", default="laps",
                   help="directory for .lap files + index.jsonl (default: laps/)")
    r.set_defaults(func=cmd_record)

    wr = sub.add_parser("write", help="write raw deduplicated telemetry to raw.jsonl (new arch)")
    wr.add_argument("--laps-dir", default="laps", help="directory containing raw.jsonl")
    wr.set_defaults(func=cmd_write)

    pr = sub.add_parser("process", help="extract .lap files from raw.jsonl (new arch)")
    pr.add_argument("--laps-dir", default="laps")
    pr.add_argument("--watch", nargs="?", const=60, type=int, metavar="SECONDS",
                    help="run every N seconds (default 60) until Ctrl-C")
    pr.set_defaults(func=cmd_process)

    ls = sub.add_parser("laps", help="list completed laps from index.jsonl")
    ls.add_argument("laps_dir", nargs="?", default="laps",
                    help="directory containing index.jsonl (default: laps/)")
    ls.add_argument("--track", default=None, help="filter by track name")
    ls.add_argument("--target", default=None, help="show gap to this target time")
    ls.set_defaults(func=cmd_laps)

    a = sub.add_parser("analyze", help="coach laps from a .lap file, directory, or legacy JSONL")
    a.add_argument("file", help=".lap file, laps/ directory, or legacy JSONL session path")
    a.add_argument("--lap", type=int, default=None,
                   help="coach a specific lap number (default: the best lap)")
    a.add_argument("--all", dest="all_laps", action="store_true",
                   help="coach every lap (overrides --lap)")
    a.add_argument("--target", default=None,
                   help="target lap time to beat, e.g. 1:47.478 or 107.478")
    a.add_argument("--model", default=None, help="override model id")
    a.add_argument("--dry-run", action="store_true",
                   help="print the JSON summary instead of calling Claude")
    a.set_defaults(func=cmd_analyze)

    p = sub.add_parser("publish", help="mark a lap as publish-approved -> racing.json")
    p.add_argument("file", help="recorded JSONL session path")
    p.add_argument("--lap", type=int, required=True, help="lap number to publish")
    p.add_argument("--out", default="racing.json", help="output racing data file")
    p.add_argument("--driver", default="Joel Lach", help="driver name for the feed")
    p.add_argument("--date", default=None, help="date of the lap, e.g. 2026-06-20")
    p.add_argument("--note", action="append",
                   help="a note about the lap/setup (repeatable)")
    p.add_argument("--force", action="store_true",
                   help="publish even if the lap contains a reset")
    p.set_defaults(func=cmd_publish)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
