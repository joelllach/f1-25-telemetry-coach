# F1 25 Car Setups

Telemetry-tuned setups, one JSON file per track. Each was validated with the
`f1coach` tool in this repo — the `tuning_notes` explain *why* each value is what
it is (which corner problem it solves), so you can adapt rather than blindly copy.

## How to use

1. In F1 25, go to the car setup screen for the track.
2. Dial in the values from the matching `*.json` file (wings, diff, ARBs,
   suspension, brakes, tyre pressures).
3. Tyre pressures are listed `[RL, RR, FL, FR]` in psi.
4. Read `tuning_notes` — if your driving style differs, adjust in the direction
   the notes describe.

## Contributing your own

Record a session, publish a clean record lap, and the setup is captured straight
from the game's telemetry (no manual transcription):

```bash
python -m f1coach record --out laps/mysession.jsonl &
# drive…
python -m f1coach publish laps/mysession.jsonl --lap <N> --date YYYY-MM-DD \
    --note "what you changed and which corner it fixed"
```

Then copy the relevant record from `racing.json` into a `setups/<track>.json`
file and open a PR. Setups with telemetry-backed notes are the most useful.

## Files

| Track | Record | File |
|-------|--------|------|
| Austria (Red Bull Ring) | 1:06.568 | `austria-red-bull-ring.json` |
