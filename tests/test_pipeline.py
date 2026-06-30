"""L4 frame-assembly + trace-replay tests (design [N6], plan §Phase 5).

The pure cookers are proven in `test_cook` / `test_calibrate`; this proves the
`CookPipeline` that *assembles* them into the wire `frame` (combined state + events,
per HID report) and the `replay_trace` harness that runs a whole Phase-3 trace
through calibration -> cooking -> events with **no hardware**. The assertions are
grounded in the real captured traces: flex saturates on the squeeze segment, the
leg trace reaches run/sprint and counts squats, and the squat/gait invariant holds.
"""

from __future__ import annotations

import json
from pathlib import Path

from ringlink_server.pipeline import CookPipeline, replay_trace

RIGHT = Path(__file__).parent.parent / "traces" / "right-pad.jsonl"
LEFT = Path(__file__).parent.parent / "traces" / "left-pad.jsonl"

STATE_KEYS = {
    "flex", "squeeze", "pull", "lean", "gait", "squatting",
    "squat_reps", "buttons", "stick",
}


def _frames(trace: Path) -> list[dict]:
    return [json.loads(l) for l in trace.read_text(encoding="utf-8").splitlines() if l.strip()]


# --------------------------------------------------------------------------- #
# frame shape / conformance
# --------------------------------------------------------------------------- #

def test_process_emits_one_conformant_frame_per_report():
    raw = _frames(RIGHT)[1:]  # skip meta
    pipe = CookPipeline()
    out = [pipe.process(f["side"], f, f["t"]) for f in raw]

    assert len(out) == len(raw)
    for i, fr in enumerate(out):
        assert fr["type"] == "frame"
        assert fr["seq"] == i                      # per-session monotonic from 0
        assert isinstance(fr["t"], float)
        assert set(fr["state"]) == STATE_KEYS
        assert set(fr["state"]["lean"]) == {"pitch", "roll"}
        assert isinstance(fr["events"], list)


def test_event_payloads_carry_a_timestamp():
    # Every emitted event must carry `t` (PROTOCOL §Events) so a client can order them.
    frames = replay_trace(RIGHT)
    saw_event = False
    for fr in frames:
        for e in fr["events"]:
            saw_event = True
            assert "type" in e and "t" in e
    assert saw_event  # the right trace's squeeze/pull segments must produce events


# --------------------------------------------------------------------------- #
# replay drives calibration, so cooked values are real (right pad)
# --------------------------------------------------------------------------- #

def test_replay_right_calibrates_then_flex_saturates_and_fires_edges():
    frames = replay_trace(RIGHT)
    by_seg = {}
    raw = _frames(RIGHT)[1:]
    for fr, r in zip(frames, raw):
        by_seg.setdefault(r["seg"], []).append(fr)

    # Calibrated rest -> flex ~0 during the rest segment.
    assert max(abs(fr["state"]["flex"]) for fr in by_seg["rest"]) < 0.2
    # Squeeze segment pushed to the ceiling -> flex saturates near +1.
    assert max(fr["state"]["flex"] for fr in by_seg["squeeze"]) >= 0.99
    # Pull segment goes clearly negative.
    assert min(fr["state"]["flex"] for fr in by_seg["pull"]) < -0.5
    # And discrete squeeze events fire (server-side edge guarantee).
    squeezes = sum(1 for fr in frames for e in fr["events"] if e["type"] == "squeeze")
    pulls = sum(1 for fr in frames for e in fr["events"] if e["type"] == "pull")
    assert squeezes >= 1 and pulls >= 1


def test_replay_right_resolves_lean_on_distinct_axes():
    frames = replay_trace(RIGHT)
    raw = _frames(RIGHT)[1:]
    by_seg = {}
    for fr, r in zip(frames, raw):
        by_seg.setdefault(r["seg"], []).append(fr)
    # Forward drives pitch, right drives roll (gravity-excluded axis resolution).
    assert max(abs(fr["state"]["lean"]["pitch"]) for fr in by_seg["lean-forward"]) > 0.5
    assert max(abs(fr["state"]["lean"]["roll"]) for fr in by_seg["lean-right"]) > 0.5


# --------------------------------------------------------------------------- #
# leg pad: gait, squat counting, and the load-bearing invariant
# --------------------------------------------------------------------------- #

def test_replay_left_reaches_run_and_sprint_and_counts_squats():
    frames = replay_trace(LEFT)
    gaits = {fr["state"]["gait"] for fr in frames}
    assert "run" in gaits
    assert "sprint" in gaits
    # The squat segment must register completed reps.
    assert frames[-1]["state"]["squat_reps"] >= 1


def test_replay_left_never_runs_mid_squat_and_reps_are_monotonic():
    frames = replay_trace(LEFT)
    prev_reps = 0
    for fr in frames:
        st = fr["state"]
        # G1 invariant: gait is never run/sprint while squatting.
        if st["squatting"]:
            assert st["gait"] == "rest"
        # squat_reps is monotonic non-decreasing within a session.
        assert st["squat_reps"] >= prev_reps
        prev_reps = st["squat_reps"]


def test_squat_rep_and_gait_change_events_emitted():
    frames = replay_trace(LEFT)
    reps = sum(1 for fr in frames for e in fr["events"] if e["type"] == "squat_rep")
    changes = sum(1 for fr in frames for e in fr["events"] if e["type"] == "gait_change")
    assert reps >= 1
    assert changes >= 1


# --------------------------------------------------------------------------- #
# replay without calibration is still well-formed (defaults hold)
# --------------------------------------------------------------------------- #

def test_replay_without_calibration_uses_defaults_and_stays_conformant():
    frames = replay_trace(RIGHT, calibrate=False)
    assert len(frames) == len(_frames(RIGHT)) - 1
    for fr in frames:
        assert set(fr["state"]) == STATE_KEYS
