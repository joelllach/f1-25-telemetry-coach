# CLAUDE.md — orientation for AI assistants working in this repo

You are helping someone install, run, debug, or extend **f1coach**, an AI race
engineer for the game **F1 25**. This file gives you enough context to be useful
without re-deriving everything. Read it fully before making changes.

## What this project is

f1coach reads F1 25's **UDP telemetry broadcast** (binary packets, ~60 Hz),
reconstructs the player's laps, and produces coaching using Claude. It also
publishes record laps + car setups to a JSON feed. There is **no game-side mod** —
the game already broadcasts telemetry when enabled in its settings; we just listen.

## Mental model (how data flows)

1. **`packets.py`** — pure parsing. Each F1 25 packet has a 29-byte common header
   (`parse_header`) then a body. Most bodies are an array of 22 per-car records;
   we index the player's slot via `header.player_car_index`. A few packets
   (MotionEx id 13, Session id 1, Event id 3) are single-record, right after the
   header. We only decode the fields the coach needs.
2. **`listener.py`** — owns the UDP socket. Each packet updates a rolling `Frame`
   (the player's current state). Lap Data packets (id 2) arrive ~60 Hz and are the
   natural "emit a frame" trigger. Slowly-changing context (setup id 5, session
   id 1, damage id 10) is stored as `latest_*` dicts; events (id 3) fire `on_event`.
3. **`analysis.py`** — `LapSegmenter` cuts a new lap when the game's
   `current_lap_num` increments, and detects **resets/flashbacks** (see below).
   `resample()` puts channels on a uniform lap-distance grid so two laps align
   point-for-point. `detect_corners()` is a brake-zone heuristic that also computes
   per-corner slip metrics + a handling label (understeer/oversteer/lock-up).
4. **`coach.py`** — `build_summary()` makes a small JSON (per-corner metrics, slip,
   setup, conditions, deltas vs a reference lap). `coach_lap()` sends it to Claude.
   We deliberately **never send raw 60 Hz telemetry** to the model — only the
   compact summary — to keep tokens low and the analysis focused.
5. **`__main__.py`** — the CLI. `record` writes JSONL; `_load_laps` reads it back
   and re-attaches lap times/setup/events; `analyze`/`debrief` coach; `publish`
   emits `racing.json`.

## Things that will bite you (hard-won knowledge)

- **Packet offsets drift between F1 titles.** Everything here targets
  `packetFormat == 2025`. If a field reads as garbage (e.g. `-2.2e12`), the struct
  offset is wrong for this title. Use `tools/find_setup_offsets.py`: it captures a
  live packet and brute-force matches *known* values (read from the in-game garage)
  to locate the right byte offset. This is exactly how the setup tyre-pressure
  offset bug was fixed (pressures live at record byte 29, not where first assumed).
- **`sanitize_setup()` nulls out-of-range setup fields** rather than passing wrong
  numbers to the coach. If you add setup fields, add their plausible range to
  `_SETUP_RANGES`. The setup struct's back half is the most volatile.
- **Reset/flashback detection** (`analysis.py`): a reset teleports the car —
  `lapDistance` jumps forward >50 m in a single frame with **no** lap-number change.
  (A lap-number increment with distance wrapping to ~0 is a normal lap completion.)
  Reset laps are excluded from "best lap" via `Lap.is_clean`. An `FLBK` event
  (id 3) also confirms a flashback. Resets skip part of the track, so a reset lap's
  *time* is fake and its corner data near the reset is unreliable — say so.
- **Wheel index order is [RL, RR, FL, FR]** everywhere (slip, temps, pressures,
  surface). Front axle = indices 2,3; rear = 0,1.
- **The recorder holds UDP 20777 exclusively.** Only one process can bind it. To
  run a diagnostic that needs the port (e.g. `find_setup_offsets.py`), stop the
  recorder first.
- **Lap numbers in recordings are the game's session lap counter**, not "1,2,3
  from when you started recording." They can start high and jump around resets.

## Claude backend

`coach.py:default_model()` picks the model and `_make_client()` picks the SDK
client, mirroring how Claude Code is configured:
- `CLAUDE_CODE_USE_BEDROCK=1` → `AnthropicBedrock` (uses `AWS_PROFILE`/`AWS_REGION`).
  Bedrock model IDs are inference-profile IDs like `us.anthropic.claude-sonnet-4-6`
  — verify availability with `aws bedrock list-inference-profiles`.
- otherwise → `Anthropic` (uses `ANTHROPIC_API_KEY`).
Override the model with `F1COACH_MODEL` or `--model`. Sonnet is the default —
the task is small and well-structured, so it's plenty and cheaper/faster.
If no backend is configured, `coach_lap` degrades to printing the JSON summary
instead of crashing.

## Verifying changes

- `PYTHONPATH=. python tools/selftest.py` — offline end-to-end (synthetic frames
  through segmentation → corners → slip labels → coach summary). Run this after any
  change to packets/analysis/coach. It does **not** need the game or an API key.
- `tools/fake_telemetry.py` emits synthetic real-format UDP packets if you want to
  exercise the live socket path.

## Extending it (common asks)

- **Add a new telemetry field**: add the offset to the relevant struct in
  `packets.py` (verify against the spec / known values), surface it on `Frame` in
  `listener.py`, add it to `CHANNELS` (or a derived channel) in `analysis.py` if
  it's per-frame, then include it in `coach.py:build_summary` and update the system
  prompt so the model knows how to use it.
- **Support another F1 title**: most offsets carry over, but treat every field as
  suspect. Gate on `packet_format` and keep a per-format struct table.
- **Per-corner names per track**: currently corners are by distance. A track
  fingerprint (matching world positions) could map them to real corner numbers.
- **Save/load a reference "ghost" lap** across sessions: persist a chosen `Lap`'s
  resampled grid and feed it as the `ref` in `coach_lap`.

## The connected site (context, not in this repo)

Published records flow to a separate site (tokenburner.ai) via `racing.json`
produced by `f1coach publish`. That site reads the same JSON shape: a top-level
`{game, driver, records: [...]}` where each record has `track, lap_time,
conditions, setup, notes`. If you change the publish schema, keep it backward
compatible or update the site consumer too.

## Tone for coaching output

Be a race engineer on the radio: specific, prioritized by time lost, pattern-aware
across corners, and honest about uncertainty (corner detection is approximate;
reset laps are not clean benchmarks). Separate **driver** fixes (e.g. ease off
100% brake to stop front lock-up) from **setup** fixes (e.g. brake bias 54→51),
and name concrete values when setup data is present.
