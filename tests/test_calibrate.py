"""Calibration state-machine tests — the Phase-4 directed-gesture sequence driven over
LIVE frames, verified hardware-free against the Phase-3 traces (design [N6]).

`test_cook.py` proves the pure snap/resolve primitives in isolation; this proves the
`Calibrator` that *drives* them from a frame stream: the arming gate (captures only count
while armed), the per-step snap/resolve wiring, weak-gesture re-prompting on a no-motion
capture, advance/`done`/`pct`, and pad-independence. The leg pad calibrates with the rest
step alone.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from ringlink_server.cook import Calibrator, FlexCal, LeanCal
from ringlink_server.leg import LegCooker

RIGHT = Path(__file__).parent.parent / "traces" / "right-pad.jsonl"
LEFT = Path(__file__).parent.parent / "traces" / "left-pad.jsonl"

WINDOW = 90  # frames per step (matches Calibrator.FRAMES_PER_STEP default)


def _segments(trace: Path) -> dict[str, list[dict]]:
    lines = [json.loads(l) for l in trace.read_text(encoding="utf-8").splitlines() if l.strip()]
    byseg: dict[str, list[dict]] = defaultdict(list)
    for f in lines[1:]:  # skip meta
        byseg[f["seg"]].append(f)
    return byseg


RSEG = _segments(RIGHT)
LSEG = _segments(LEFT)


def _drive_step(calib: Calibrator, frames: list[dict]) -> None:
    """Simulate L4 driving one step: prompt shown, user reacts, server arms and feeds one
    capture window. (The arm models the reaction beat the real loop waits out.)"""
    calib.arm()
    for f in frames[:WINDOW]:
        calib.feed(f)


# --------------------------------------------------------------------------- #
# right pad: rest -> lean-forward -> lean-right
# --------------------------------------------------------------------------- #

def test_R_full_sequence_snaps_rest_and_resolves_distinct_axes():
    flex, lean = FlexCal(), LeanCal()
    calib = Calibrator("R", flex=flex, lean=lean, frames_per_step=WINDOW)

    assert calib.step == "rest"
    _drive_step(calib, RSEG["rest"])
    assert calib.step == "lean-forward"
    _drive_step(calib, RSEG["lean-forward"])
    assert calib.step == "lean-right"
    _drive_step(calib, RSEG["lean-right"])
    assert calib.done
    assert calib.step is None
    assert calib.flex_snapped

    # Rest snapped: cooking the rest strains now reads ~0 flex.
    rest_flex = [flex.cook(f["strain"]) for f in RSEG["rest"]]
    assert max(abs(x) for x in rest_flex) < 0.15

    # Directed gesture resolved pitch and roll to DISTINCT, gravity-excluded axes
    # (forward->Z, right->X on this grip).
    assert lean.pitch[0] != lean.roll[0]
    assert lean.pitch[0] == 2
    assert lean.roll[0] == 0

    # And lean now actually responds on the right axis for each direction.
    fwd = [lean.cook(f["accel"]) for f in RSEG["lean-forward"]]
    right = [lean.cook(f["accel"]) for f in RSEG["lean-right"]]
    assert max(abs(l["pitch"]) for l in fwd) > 0.5
    assert max(abs(l["roll"]) for l in right) > 0.5


def test_R_step_and_pct_progress_within_a_window():
    calib = Calibrator("R", lean=LeanCal(), frames_per_step=WINDOW)
    assert calib.pct == 0 and calib.step == "rest" and not calib.armed
    calib.arm()
    assert calib.armed
    for f in RSEG["rest"][: WINDOW // 2]:
        calib.feed(f)
    assert calib.step == "rest"
    assert 40 <= calib.pct <= 60  # ~halfway through the rest window
    for f in RSEG["rest"][WINDOW // 2 : WINDOW]:
        calib.feed(f)
    assert calib.step == "lean-forward"  # advanced once the window filled
    assert calib.pct == 0 and not calib.armed  # fresh window, disarmed until re-prompt


# --------------------------------------------------------------------------- #
# arming gate + weak-gesture guard  (continuous-stream realism; R1/R2/R5)
# --------------------------------------------------------------------------- #

def test_unarmed_feed_is_ignored():
    # A naive L4 that just pumps frames without arming captures nothing — the gate is
    # what stops the next directed window opening before the user has reacted.
    calib = Calibrator("R", lean=LeanCal(), frames_per_step=WINDOW)
    for f in RSEG["rest"][: WINDOW * 2]:
        calib.feed(f)
    assert calib.step == "rest" and calib.pct == 0  # never advanced


def test_no_motion_directed_window_is_weak_and_does_not_advance():
    # Arm a directed step but feed STILL frames (rest segment) -> no real lean -> the
    # machine must NOT commit a near-arbitrary axis; it flags weak and stays put.
    lean = LeanCal()
    calib = Calibrator("R", lean=lean, frames_per_step=WINDOW)
    _drive_step(calib, RSEG["rest"])           # rest snaps gravity
    assert calib.step == "lean-forward"
    calib.arm()
    for f in RSEG["rest"][:WINDOW]:            # feed STILL frames into the lean window
        calib.feed(f)
    assert calib.step == "lean-forward"        # did not advance
    assert calib.weak                          # flagged for re-prompt
    # Re-prompt with a real forward lean -> resolves and advances.
    _drive_step(calib, RSEG["lean-forward"])
    assert calib.step == "lean-right" and not calib.weak


def test_arm_clears_weak_flag():
    calib = Calibrator("R", lean=LeanCal(), frames_per_step=WINDOW)
    _drive_step(calib, RSEG["rest"])
    calib.arm()
    for f in RSEG["rest"][:WINDOW]:
        calib.feed(f)
    assert calib.weak
    calib.arm()
    assert not calib.weak


# --------------------------------------------------------------------------- #
# gravity-axis exclusion actually changes the outcome  (R4 — moved to test_cook,
# kept here as the wiring check it always was)
# --------------------------------------------------------------------------- #

def test_R_resolution_runs_after_gravity_snapped_at_rest():
    # Wiring check: rest snaps gravity (Y, axis 1) BEFORE the leans resolve, so neither
    # resolved axis is the gravity axis. (That exclusion *matters* — proven on a
    # collision fixture in test_cook.test_exclusion_separates_axes_a_bare_rule_collides.)
    lean = LeanCal()
    calib = Calibrator("R", lean=lean, frames_per_step=WINDOW)
    _drive_step(calib, RSEG["rest"])
    _drive_step(calib, RSEG["lean-forward"])
    _drive_step(calib, RSEG["lean-right"])
    assert lean.pitch[0] != 1
    assert lean.roll[0] != 1


# --------------------------------------------------------------------------- #
# left/leg pad: rest only
# --------------------------------------------------------------------------- #

def test_L_calibrates_with_rest_step_alone():
    leg = LegCooker()
    calib = Calibrator("L", leg=leg, frames_per_step=WINDOW)
    assert calib.step == "rest"
    _drive_step(calib, LSEG["rest"])
    assert calib.done
    # The leg tracker's rest gravity vector is now set from the worn still capture.
    assert leg.lt.rest_accel is not None
    # And it points where the trace's rest gravity points (X-dominant for this strap).
    assert max(range(3), key=lambda i: abs(leg.lt.rest_accel[i])) == 0


# --------------------------------------------------------------------------- #
# nullable contract + guards
# --------------------------------------------------------------------------- #

def test_null_accel_frames_do_not_count_toward_a_step():
    calib = Calibrator("R", lean=LeanCal(), frames_per_step=WINDOW)
    calib.arm()
    for _ in range(WINDOW * 2):
        calib.feed({"accel": None, "strain": 12})
    assert calib.step == "rest"  # never advanced
    assert calib.pct == 0


def test_feed_after_done_is_a_noop():
    calib = Calibrator("L", leg=LegCooker(), frames_per_step=WINDOW)
    _drive_step(calib, LSEG["rest"])
    assert calib.done
    calib.arm()                  # arm after done is a no-op too
    calib.feed(LSEG["rest"][0])  # must not raise or reopen
    assert calib.done


def test_invalid_pad_rejected():
    import pytest

    with pytest.raises(ValueError):
        Calibrator("X")


def test_zero_frames_per_step_rejected():
    import pytest

    with pytest.raises(ValueError):
        Calibrator("R", frames_per_step=0)
