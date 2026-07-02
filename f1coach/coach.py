"""Build a compact lap summary and get a coaching debrief from Claude.

We never send raw 60 Hz telemetry to the model -- we send a small per-corner
JSON summary plus lap/reference deltas. That keeps tokens low and lets Claude
focus on prioritized, plain-English advice and cross-corner patterns.
"""
from __future__ import annotations

import json
import os

from .analysis import (Lap, resample, detect_corners, time_delta, fmt_ms,
                       Corner)

# Model resolution
# ----------------
# Sonnet is plenty for this task (small per-corner summary, well-structured
# coaching) and is cheaper/faster -- which matters if you later add live
# between-lap calls. On Bedrock the model is addressed via a cross-region
# inference profile ID. Override with env vars:
#
#   F1COACH_MODEL                 explicit model/inference-profile id
#   ANTHROPIC_MODEL               Claude Code's own override, reused if set
#
# Backend selection mirrors Claude Code:
#   CLAUDE_CODE_USE_BEDROCK=1  ->  AnthropicBedrock  (uses AWS_PROFILE/AWS_REGION)
#   otherwise                  ->  Anthropic         (uses ANTHROPIC_API_KEY)
# Verified against `aws bedrock list-inference-profiles` in us-west-2.
# (Opus 4.8 alternative: us.anthropic.claude-opus-4-8)
BEDROCK_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-6"
DIRECT_DEFAULT_MODEL = "claude-sonnet-4-6"


def use_bedrock() -> bool:
    return os.environ.get("CLAUDE_CODE_USE_BEDROCK", "").lower() in ("1", "true", "yes")


def default_model() -> str:
    env = os.environ.get("F1COACH_MODEL") or os.environ.get("ANTHROPIC_MODEL")
    if env:
        return env
    return BEDROCK_DEFAULT_MODEL if use_bedrock() else DIRECT_DEFAULT_MODEL


MODEL = default_model()

SYSTEM = """You are an expert F1 sim-racing driver coach analyzing F1 25 telemetry.
You receive a JSON summary of one lap (the "current" lap), optionally compared to
the driver's best lap ("reference"). Corners are auto-detected brake zones.

Give a debrief that is:
- Prioritized by time lost (biggest opportunity first).
- Specific and actionable ("brake ~10m later into T4 and carry 8 km/h more apex
  speed" -- not "be smoother").
- Pattern-aware: call out habits repeated across corners (e.g. over-slowing every
  medium corner, early throttle causing wheelspin, lifting on entry).
- Use the per-corner "slip" block when present (from wheel slip telemetry):
  front_lock/rear_lock are slip ratios under braking (more negative = that axle
  locking up); front/rear_slip_angle (radians) show which end is sliding (front
  bigger = understeer/push, rear bigger = oversteer/loose). "handling" is a
  rough auto-label. Use these to separate a DRIVER issue (e.g. braking too hard =
  front lock-up, get off the brake) from a likely SETUP issue (e.g. persistent
  understeer across many corners = add front wing / soften front; snap oversteer
  on entry = brake bias forward, soften rear). Say which it looks like.
- Honest about uncertainty: corner detection is approximate, so reference corner
  numbers by their distance-from-start if helpful.

If the lap was RESET (reset_count > 0), the driver lost control and used a
reset/flashback to recover. Open by acknowledging it was a reset lap (so the
"lap time" is not a real continuous lap), then still coach the driving: focus on
where control was lost and what likely caused it (entry speed, mid-corner
throttle, trail-braking, kerb), since that is the recurring problem to fix.

If a target lap time is provided, frame the advice around beating it and say
roughly where the needed time can be found.

If a "setup" block is present, make setup advice CONCRETE using the actual
values (e.g. brake_bias, on/off_throttle_diff, front/rear wing, ARBs): say the
current value and a specific direction/amount to try. If "conditions" is present
(track/air temp, weather), factor grip into the read. If "events" includes
flashback, that confirms a reset. Tyre temps in the corner data flag whether the
car was in the grip window.

The lap's "track" field is the authoritative circuit name. If it is null/absent,
say "this track" and refer to corners by their distance-from-start — do NOT guess
the circuit or invent corner names (e.g. from lap length). Never name a track or
named corner unless the "track" field provides it.

Keep it to the 3-4 highest-value points. End with one single thing to focus on
next lap. Be direct and encouraging, like a race engineer on the radio."""


def build_summary(cur: Lap, ref: Lap | None, step_m: float = 5.0,
                  target_ms: int | None = None) -> dict:
    cur_grid = resample(cur, step_m)
    cur_corners = detect_corners(cur_grid)
    # resolve the real track name from the session, so the model never guesses it
    track = None
    sess = getattr(cur, "session", None)
    if sess:
        from f1coach import packets as pk
        track = pk.TRACK_IDS.get(sess.get("track_id"))
    summary = {
        "current_lap": {
            "lap_num": cur.lap_num,
            "lap_time": fmt_ms(cur.lap_time_ms),
            "lap_time_ms": cur.lap_time_ms,
            "track": track,  # may be None if unknown; do NOT infer from length
            "invalid": cur.invalid,
            "reset_count": cur.reset_count,
            "was_reset": cur.was_reset,
            "length_m": round(cur.length_m, 1),
            "corners": [_corner_dict(c) for c in cur_corners],
        }
    }
    # attach context recorded with the lap, if present
    setup = getattr(cur, "setup", None)
    if setup:
        summary["setup"] = setup
    session = getattr(cur, "session", None)
    if session:
        summary["conditions"] = session
    events = getattr(cur, "events", None)
    if events:
        summary["events"] = [{"code": e.get("code"), "at_m": e.get("lap_distance")}
                             for e in events]
    if ref is not None:
        ref_grid = resample(ref, step_m)
        ref_corners = detect_corners(ref_grid)
        delta = time_delta(ref_grid, cur_grid)
        summary["reference_lap"] = {
            "lap_num": ref.lap_num,
            "lap_time": fmt_ms(ref.lap_time_ms),
            "lap_time_ms": ref.lap_time_ms,
            "corners": [_corner_dict(c) for c in ref_corners],
        }
        summary["final_time_delta_s"] = delta[-1] if delta else None
        # sample the delta trace at ~10 points to show where time is gained/lost
        if delta:
            k = max(1, len(delta) // 10)
            summary["delta_trace"] = [
                {"dist_m": round(cur_grid["dist"][i], 0), "delta_s": delta[i]}
                for i in range(0, len(delta), k)
            ]
        # pair corners by nearest apex distance for direct comparison
        summary["corner_comparison"] = _pair_corners(cur_corners, ref_corners)
    if target_ms:
        summary["target"] = {
            "lap_time": fmt_ms(target_ms),
            "lap_time_ms": target_ms,
            "gap_to_target_s": round((cur.lap_time_ms - target_ms) / 1000.0, 3)
                               if cur.lap_time_ms > 0 else None,
        }
    return summary


def _corner_dict(c: Corner) -> dict:
    d = {
        "n": c.index,
        "brake_start_m": c.brake_start_m,
        "apex_m": c.apex_m,
        "min_speed_kmh": c.min_speed,
        "entry_speed_kmh": c.entry_speed,
        "exit_speed_kmh": c.exit_speed,
        "peak_brake": c.peak_brake,
        "throttle_reapply_m": c.throttle_reapply_m,
        "gear_at_apex": c.gear_at_apex,
    }
    # only include slip diagnostics if MotionEx was actually recorded
    if any((c.front_lock, c.rear_lock, c.front_slip, c.rear_slip)):
        d["slip"] = {
            "front_lock": c.front_lock, "rear_lock": c.rear_lock,
            "front_slip_angle": c.front_slip, "rear_slip_angle": c.rear_slip,
            "handling": c.handling,
        }
    return d


def _pair_corners(cur: list[Corner], ref: list[Corner]) -> list[dict]:
    out = []
    for c in cur:
        best = min(ref, key=lambda r: abs(r.apex_m - c.apex_m), default=None)
        if best is None or abs(best.apex_m - c.apex_m) > 80:
            continue
        out.append({
            "apex_m": c.apex_m,
            "min_speed_delta_kmh": round(c.min_speed - best.min_speed, 1),
            "brake_point_delta_m": round(c.brake_start_m - best.brake_start_m, 1),
            "exit_speed_delta_kmh": round(c.exit_speed - best.exit_speed, 1),
        })
    return out


def _make_client():
    """Return (client, backend_name) matching this machine's Claude session.

    Bedrock picks up credentials from AWS_PROFILE / AWS_REGION (and standard
    AWS credential resolution). The direct API uses ANTHROPIC_API_KEY.
    """
    import anthropic
    if use_bedrock():
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        kwargs = {"aws_region": region} if region else {}
        return anthropic.AnthropicBedrock(**kwargs), "bedrock"
    return anthropic.Anthropic(), "direct"


def coach_lap(cur: Lap, ref: Lap | None, model: str | None = None,
              dry_run: bool = False, target_ms: int | None = None) -> str:
    model = model or default_model()
    summary = build_summary(cur, ref, target_ms=target_ms)
    if dry_run:
        return json.dumps(summary, indent=2)

    # If we're not on Bedrock and have no key, degrade to printing the summary.
    if not use_bedrock() and not os.environ.get("ANTHROPIC_API_KEY"):
        return ("[no ANTHROPIC_API_KEY set and not on Bedrock] Lap summary below:\n"
                + json.dumps(summary, indent=2))

    client, backend = _make_client()
    user = ("Here is the lap telemetry summary. Give me my debrief.\n\n"
            + json.dumps(summary))
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=900,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        return (f"[coach error via {backend} model={model}: {e}]\n"
                "Lap summary below:\n" + json.dumps(summary, indent=2))
    return "".join(b.text for b in resp.content if b.type == "text")
