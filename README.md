# f1coach — an AI race engineer for F1 25

Turn the F1 25 (PS5/PC) UDP telemetry stream into a real coaching loop: record
your laps, get a per-corner debrief from Claude, and tune your car from
**actual wheel-slip and setup data** instead of guesswork.

It was built live while chasing a Red Bull Ring personal best — and it worked:
telemetry showed front-axle lock-up in Turn 3, a brake-pressure/bias change
dropped the lock-up slip ratio from −0.293 to −0.085, apex speed went up ~10 km/h,
and the lap dropped from 1:06.701 to **1:06.568**. The published record + setup
live at [tokenburner.ai/racing](https://tokenburner.ai/racing).

## What it does

```
PS5/PC (F1 25)  ──UDP 2025──▶  listener  ──▶  per-frame player state (≈60 Hz)
                                   │
                              analysis  (segment laps, resample on lap distance,
                                         detect corners, slip/lock-up, time delta)
                                   │
                               coach  (compact per-corner + setup summary ─▶ Claude)
                                   │
                              your debrief  +  publishable records
```

- **`record`** — log a whole session to a JSONL file (run it in the background while you drive).
- **`analyze`** — rebuild laps from a recording and get an AI debrief of any lap vs your best.
- **`debrief`** — coach each completed lap live as you drive.
- **`monitor` / `counts`** — sanity-check the telemetry feed.
- **`publish`** — mark a record lap as approved and emit `racing.json` (setups + tuning notes).

The coach reads **wheel slip** (front/rear lock-up + slip angle → understeer vs
oversteer) and your **actual in-game setup** (wings, brake bias, diff, ARBs,
tyre pressures), so its advice is concrete: *"brake bias is 54%, try 51 — your
fronts are locking at T3"* rather than "be smoother."

## Quick start

```bash
git clone https://github.com/joelllach/f1-25-telemetry-coach && cd f1-25-telemetry-coach
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # just the anthropic SDK

# Coaching uses Claude. Pick ONE backend:
export ANTHROPIC_API_KEY=sk-...           # direct Anthropic API
#   …or AWS Bedrock:
export CLAUDE_CODE_USE_BEDROCK=1 AWS_PROFILE=you AWS_REGION=us-west-2
```

### Set up the game (one time)

In F1 25: **Settings → Telemetry Settings**
- UDP Telemetry: **On**
- UDP Broadcast Mode: **On** (sends to your LAN), or set UDP IP to your computer's LAN IP
- UDP Port: **20777** · Send Rate: **60 Hz** · Format: **2025** · Your Telemetry: **Public**

Make sure the console/PC and the machine running f1coach are on the same network,
and that your firewall allows inbound UDP on 20777.

### Use it

```bash
python -m f1coach counts --seconds 10                 # confirm packets arrive
python -m f1coach record --out laps/session.jsonl &   # record while you drive
# …drive several laps…                                 # (Ctrl-C the recorder when done)
python -m f1coach analyze laps/session.jsonl --target 1:06.701   # debrief best lap
python -m f1coach publish laps/session.jsonl --lap 24 --date 2026-06-20 \
    --note "What changed and why"                      # approve a record for sharing
```

No PS5? Validate the whole pipeline offline:
```bash
PYTHONPATH=. python tools/selftest.py
```

## Repo layout

| Path | Role |
|------|------|
| `f1coach/packets.py` | F1 25 (format 2025) UDP packet structs + player-car parsers |
| `f1coach/listener.py` | UDP socket → merged per-frame player state + context (setup/session/events) |
| `f1coach/analysis.py` | lap segmentation, reset detection, distance-grid resample, corner + slip metrics |
| `f1coach/coach.py` | builds the compact summary and calls Claude (Anthropic or Bedrock) |
| `f1coach/__main__.py` | CLI: `monitor` / `counts` / `record` / `analyze` / `debrief` / `publish` |
| `tools/selftest.py` | offline end-to-end validation (no game needed) |
| `tools/fake_telemetry.py` | synthetic 2025-format UDP emitter for testing |
| `tools/find_setup_offsets.py` | locate setup byte offsets by matching known garage values |
| `setups/` | published car setups per track (community-shareable) |
| `CLAUDE.md` | orientation for AI assistants working in this repo — read it |

## Tuning method (the interesting part)

1. **Record** a session of flying laps.
2. **Analyze** your best clean lap — the coach flags the corner losing the most time and *why* (lock-up? understeer? early throttle?), using slip data.
3. **Change one setup area** the coach suggests (e.g. brake bias + pressure).
4. **Re-record and compare** — slip ratios and apex speeds tell you objectively whether it worked.
5. **Publish** the record + setup so others can copy it.

## Accuracy & caveats

- Packet field offsets follow the EA F1 25 UDP spec. They're validated against
  real data where possible (the setup parser was reverse-checked against known
  garage values), but EA shifts fields between titles — **verify offsets against
  the official spec** before trusting a new field. `counts` warns if the incoming
  `packetFormat` isn't 2025.
- Setup values outside plausible ranges are auto-nulled rather than shown wrong
  (see `sanitize_setup`); tyre pressures were the one field that needed offset
  correction — see `tools/find_setup_offsets.py` if you need to do the same.
- Corner detection is a brake-zone heuristic, not a track-specific corner DB, so
  corners are referenced by distance-from-start.

## License

MIT — see [LICENSE](LICENSE).
