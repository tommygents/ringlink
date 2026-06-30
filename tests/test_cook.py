"""L3 cooking tests, verified against the real Phase-3 right-pad trace (the design's
[N6] hardware-free replay). Loads `traces/right-pad.jsonl` and checks that the cooked
vocabulary behaves as the normative table demands: flex reaches +1 on a full push and
goes negative on a pull, the directed-gesture axes resolve deterministically to
DISTINCT axes, and lean responds on the resolved axis for each direction.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from ringlink_server.cook import (
    FlexCal,
    LeanCal,
    gravity_axis,
    pull_of,
    resolve_axis,
    squeeze_of,
)

TRACE = Path(__file__).parent.parent / "traces" / "right-pad.jsonl"


def _segments() -> dict[str, list[dict]]:
    lines = [json.loads(l) for l in TRACE.read_text(encoding="utf-8").splitlines() if l.strip()]
    byseg: dict[str, list[dict]] = defaultdict(list)
    for f in lines[1:]:  # skip meta
        byseg[f["seg"]].append(f)
    return byseg


SEG = _segments()


# --------------------------------------------------------------------------- #
# flex
# --------------------------------------------------------------------------- #

def test_flex_snap_calibrates_rest_to_zero():
    cal = FlexCal()
    cal.snap([f["strain"] for f in SEG["rest"]])
    # Rest strain (~0x0c) snaps to flex ~0.
    flexes = [cal.cook(f["strain"]) for f in SEG["rest"]]
    assert max(abs(x) for x in flexes) < 0.15


def test_flex_full_push_reaches_plus_one_and_pull_goes_negative():
    cal = FlexCal()
    cal.snap([f["strain"] for f in SEG["rest"]])

    squeeze_flex = [cal.cook(f["strain"]) for f in SEG["squeeze"]]
    pull_flex = [cal.cook(f["strain"]) for f in SEG["pull"]]

    # The squeeze segment pushed to 0x14 (the ceiling) -> flex saturates at +1.
    assert max(squeeze_flex) >= 0.99
    assert max(squeeze_of(x) for x in squeeze_flex) >= 0.99
    # The pull segment reached ~0x03 -> clearly negative flex (not full -1, since the
    # capture didn't bottom out at 0x00).
    assert min(pull_flex) < -0.5
    assert max(pull_of(x) for x in pull_flex) > 0.5


def test_flex_nullable():
    assert FlexCal().cook(None) is None


# --------------------------------------------------------------------------- #
# directed-gesture axis resolution  [G2]
# --------------------------------------------------------------------------- #

def _rest_accel() -> tuple:
    accels = [f["accel"] for f in SEG["rest"] if f["accel"]]
    return tuple(sum(a[i] for a in accels) / len(accels) for i in range(3))


def test_gravity_axis_is_excluded():
    # Rest gravity sits on Y (~-4111) for this grip; it must be the excluded axis.
    assert gravity_axis(_rest_accel()) == 1


def test_directed_gesture_resolves_distinct_axes():
    rest = _rest_accel()
    grav = gravity_axis(rest)
    pitch = resolve_axis(rest, [f["accel"] for f in SEG["lean-forward"]], exclude=grav)
    roll = resolve_axis(rest, [f["accel"] for f in SEG["lean-right"]], exclude=grav)
    # The whole point: forward and right must land on DIFFERENT axes, or lean is
    # ambiguous. With the gravity axis (Y) excluded, forward -> Z (2), right -> X (0).
    assert pitch[0] != roll[0]
    assert pitch[0] == 2  # forward deflects the non-gravity axis Z most
    assert roll[0] == 0   # right deflects the non-gravity axis X most


def test_axis_resolution_is_deterministic():
    rest = _rest_accel()
    samples = [f["accel"] for f in SEG["lean-forward"]]
    grav = gravity_axis(rest)
    assert resolve_axis(rest, samples, exclude=grav) == resolve_axis(rest, samples, exclude=grav)


# --------------------------------------------------------------------------- #
# lean (against resolved axes)
# --------------------------------------------------------------------------- #

def test_lean_responds_on_resolved_axis_per_direction():
    cal = LeanCal()
    cal.snap_rest([f["accel"] for f in SEG["rest"]])
    cal.resolve_pitch([f["accel"] for f in SEG["lean-forward"]])
    cal.resolve_roll([f["accel"] for f in SEG["lean-right"]])

    # During the forward segment, |pitch| should swing hard while at rest both are ~0.
    rest_lean = [cal.cook(f["accel"]) for f in SEG["rest"]]
    fwd_lean = [cal.cook(f["accel"]) for f in SEG["lean-forward"]]
    right_lean = [cal.cook(f["accel"]) for f in SEG["lean-right"]]

    assert max(abs(l["pitch"]) for l in rest_lean) < 0.3
    assert max(abs(l["pitch"]) for l in fwd_lean) > 0.5      # forward drives pitch
    assert max(abs(l["roll"]) for l in right_lean) > 0.5     # right drives roll
