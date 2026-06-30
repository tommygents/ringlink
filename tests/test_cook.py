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
    FlexEdges,
    LeanCal,
    cook_stick,
    decode_buttons,
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


def test_exclusion_separates_axes_a_bare_rule_collides():
    # The headline Phase-4 refinement: excluding the gravity axis must CHANGE the result,
    # not just agree with the bare rule on the canonical trace (where Z/X already beat Y).
    # Synthesize a grip where gravity (Y) deflects MOST under both leans, so the bare
    # "axis that changed most" rule resolves BOTH to Y -> pitch collides with roll -> lean
    # is ambiguous. Excluding Y resolves to the distinct secondary axes (Z, then X).
    rest = (0.0, 4000.0, 0.0)  # gravity on Y (axis 1)
    assert gravity_axis(rest) == 1
    # forward lean: gravity rotates off Y hard (-3200) while Z grows (+2500).
    fwd = [(0.0, 800.0, 2500.0)]
    # right lean: gravity off Y hard (-3200) while X grows (+2500).
    right = [(2500.0, 800.0, 0.0)]

    # Bare maximum-deflection rule (no exclusion) picks the gravity axis for BOTH.
    bare_pitch = resolve_axis(rest, fwd)[0]
    bare_roll = resolve_axis(rest, right)[0]
    assert bare_pitch == 1 and bare_roll == 1
    assert bare_pitch == bare_roll  # collision: lean would be ambiguous

    # Excluding the gravity axis resolves to DISTINCT secondary axes.
    ex_pitch = resolve_axis(rest, fwd, exclude=1)[0]
    ex_roll = resolve_axis(rest, right, exclude=1)[0]
    assert ex_pitch == 2 and ex_roll == 0
    assert ex_pitch != ex_roll  # the guarantee the exclusion buys


def test_resolve_axis_weak_gesture_raises():
    import pytest

    from ringlink_server.cook import WeakGestureError

    rest = (0.0, 4000.0, 0.0)
    still = [(0.0, 4005.0, 3.0), (1.0, 3998.0, -2.0)]  # jitter only, no real lean
    with pytest.raises(WeakGestureError):
        resolve_axis(rest, still, exclude=1, min_deflection=1024.0)


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


# --------------------------------------------------------------------------- #
# flex edges (squeeze / pull events)
# --------------------------------------------------------------------------- #

def test_flex_edges_fire_per_press_with_refractory():
    cal = FlexCal()
    cal.snap([f["strain"] for f in SEG["rest"]])
    edges = FlexEdges()

    # The squeeze segment is repeated presses -> several squeeze events, no pulls.
    sq_events = pl_events = 0
    for f in SEG["squeeze"]:
        for e in edges.update(cal.cook(f["strain"])):
            if e["type"] == "squeeze":
                sq_events += 1
                assert 0.0 < e["strength"] <= 1.0
            else:
                pl_events += 1
    assert sq_events >= 1
    assert pl_events == 0

    # The pull segment fires pulls.
    edges = FlexEdges()
    pulls = sum(1 for f in SEG["pull"] for e in edges.update(cal.cook(f["strain"]))
                if e["type"] == "pull")
    assert pulls >= 1


def test_flex_edges_refractory_blocks_machinegun():
    # A held squeeze (flex pinned high) is exactly ONE event, not one per tick.
    edges = FlexEdges()
    fired = sum(len(edges.update(0.9)) for _ in range(100))
    assert fired == 1
    # Relaxing past the release threshold re-arms for the next press.
    edges.update(0.0)
    assert len(edges.update(0.9)) == 1


def test_flex_edges_nullable():
    assert FlexEdges().update(None) == []


# --------------------------------------------------------------------------- #
# buttons / stick (right-pad only; the rest of the cooked frame)  [N7]
# --------------------------------------------------------------------------- #

def test_decode_buttons_none_down_is_empty():
    # The whole rest segment has no buttons pressed -> always [].
    for f in SEG["rest"]:
        assert decode_buttons(tuple(f["buttons"])) == []


def test_decode_buttons_face_and_shoulder():
    # Right-pad face/shoulder bits live in the first button byte (offset 3):
    # Y=0x01 X=0x02 B=0x04 A=0x08 SR=0x10 SL=0x20 R=0x40 ZR=0x80.
    assert decode_buttons((0x08, 0, 0)) == ["A"]
    assert decode_buttons((0x04, 0, 0)) == ["B"]
    assert decode_buttons((0xC0, 0, 0)) == ["R", "ZR"]


def test_decode_buttons_shared_byte():
    # Plus / R-stick / Home live in the shared byte (offset 4): Plus=0x02,
    # RStick=0x04, Home=0x10. (Minus/Capture/L-stick are left-pad, omitted.)
    assert decode_buttons((0, 0x02, 0)) == ["plus"]
    assert decode_buttons((0, 0x10, 0)) == ["home"]


def test_decode_buttons_nullable():
    assert decode_buttons(None) == []


def test_cook_stick_rest_is_centered_within_deadzone():
    # The captured rest stick (~[2210, 1974]) sits inside the deadzone -> [0, 0].
    for f in SEG["rest"]:
        x, y = cook_stick(tuple(f["stick"]))
        assert x == 0.0 and y == 0.0


def test_cook_stick_extremes_saturate_signed():
    # Raw 12-bit, center ~2048: full-left (0) -> -1 on X, full-right (4095) -> +1.
    x_lo, _ = cook_stick((0, 2048))
    x_hi, _ = cook_stick((4095, 2048))
    assert x_lo <= -0.99
    assert x_hi >= 0.99


def test_cook_stick_nullable():
    assert cook_stick(None) == [0.0, 0.0]
