"""L4 frame assembly + trace replay — raw frames -> the wire `frame`.

The L3 cookers (`FlexCal`, `FlexEdges`, `LeanCal`, `LegCooker`, `Calibrator`) each
own one slice of the vocabulary. `CookPipeline` is the thing that **assembles** them
into the protocol's combined `frame`: it holds one mutable "current state" and, on
each incoming raw HID report (from *either* pad), updates the fields that report
carries, runs that pad's edge detectors, and emits a `frame` with the **full current
state** plus the events generated this tick. Right reports drive
`flex`/`squeeze`/`pull`/`lean`/`buttons`/`stick` (+ squeeze/pull events); left reports
drive `gait`/`squatting`/`squat_reps` (+ squat_rep/gait_change events). This mirrors
the real coupling: two pads stream independently at ~66 Hz, and a wire frame goes out
per report carrying the latest of each (PROTOCOL §Cadence, §Core model).

`replay_trace` is the design's hardware-free [N6] harness: it drives calibration from
a Phase-3 trace's labelled directed-gesture segments, then runs every frame through the
pipeline — so the entire cooked path (calibration -> cooking -> events) is CI-tested
with no Joy-Cons. The pipeline is the same one the L4 server (`server.py`) feeds from a
live `RingHub`; only the frame *source* differs.
"""

from __future__ import annotations

import json
from pathlib import Path

from .cook import (
    Calibrator,
    FlexCal,
    FlexEdges,
    LeanCal,
    cook_stick,
    decode_buttons,
    pull_of,
    squeeze_of,
)
from .leg import LegCooker


def _initial_state() -> dict:
    """The combined cooked state before any report (and the held-value baseline)."""
    return {
        "flex": 0.0,
        "squeeze": 0.0,
        "pull": 0.0,
        "lean": {"pitch": 0.0, "roll": 0.0},
        "gait": "rest",
        "squatting": False,
        "squat_reps": 0,
        "buttons": [],
        "stick": [0.0, 0.0],
    }


class CookPipeline:
    """Assembles raw per-pad reports into the protocol's combined cooked `frame`.

    Holds one mutable current state (the "as of now" continuous values) plus the L3
    cookers. `process(side, raw, t)` updates the state for that pad, collects any
    discrete events, and returns one wire `frame`. The cookers are exposed
    (`flex`/`lean`/`leg`) so a `Calibrator` can be wired to the *same* instances the
    live stream uses — calibration snaps these in place, exactly as on hardware.

    Nullable contract (PROTOCOL): a missing raw field holds its last cooked value and
    emits no event for that field.
    """

    def __init__(self):
        self.seq = 0
        self.state = _initial_state()
        # L3 cookers — the live ones; calibration mutates these in place.
        self.flex = FlexCal()
        self.lean = LeanCal()
        self.leg = LegCooker()
        self._flex_edges = FlexEdges()

    def _process_right(self, raw: dict) -> list:
        events: list = []
        f = self.flex.cook(raw.get("strain"))
        if f is not None:
            self.state["flex"] = f
            self.state["squeeze"] = squeeze_of(f)
            self.state["pull"] = pull_of(f)
            events.extend(self._flex_edges.update(f))
        lean = self.lean.cook(raw.get("accel"))
        if lean is not None:
            self.state["lean"] = lean
        # buttons/stick are always present on a right report (offsets below the
        # nullable IMU/strain region); decode every tick.
        self.state["buttons"] = decode_buttons(raw.get("buttons"))
        self.state["stick"] = cook_stick(raw.get("stick"))
        return events

    def _process_left(self, raw: dict) -> list:
        leg_state, events = self.leg.update(raw.get("accel"), raw.get("gyro"))
        if leg_state is not None:
            self.state["gait"] = leg_state["gait"]
            self.state["squatting"] = leg_state["squatting"]
            self.state["squat_reps"] = leg_state["squat_reps"]
        return events

    def process(self, side: str, raw: dict, t: float) -> dict:
        """Cook one raw report into a wire `frame` (full current state + this tick's
        events). `side` selects which fields update; the other pad's fields hold."""
        events = self._process_right(raw) if side == "R" else self._process_left(raw)
        ts = round(t, 6)
        for e in events:
            e.setdefault("t", ts)  # PROTOCOL: every event carries `t`
        frame = {
            "type": "frame",
            "seq": self.seq,
            "t": ts,
            # copy so a later mutation of self.state can't retro-edit an emitted frame
            "state": _copy_state(self.state),
            "events": events,
        }
        self.seq += 1
        return frame


def _copy_state(state: dict) -> dict:
    out = dict(state)
    out["lean"] = dict(state["lean"])
    out["buttons"] = list(state["buttons"])
    out["stick"] = list(state["stick"])
    return out


def _calibrate_from_segments(pipe: CookPipeline, pad: str, by_seg: dict) -> None:
    """Drive a `Calibrator` wired to `pipe`'s cookers using a trace's labelled
    directed-gesture segments — the replay analogue of L4 walking the user through
    rest -> lean-forward -> lean-right (R) / rest (L). Arms once per step (modelling
    the reaction beat) and feeds that segment's frames.

    Assumes **well-formed canonical segments**: each holds >= `frames_per_step` real
    frames for its gesture, so one arm()+feed advances exactly one step. A weak/short
    segment would leave the Calibrator's step unadvanced while this loop moves on,
    desyncing them — the live `server._run_calibration` is the re-prompt-faithful path;
    this fast trace path trusts the recorded gestures (the Phase-3 traces satisfy it;
    `test_pipeline` confirms rest/lean resolve)."""
    calib = Calibrator(pad, flex=pipe.flex, lean=pipe.lean, leg=pipe.leg)
    steps = Calibrator.R_STEPS if pad == "R" else Calibrator.L_STEPS
    for step in steps:
        seg = by_seg.get(step, [])
        calib.arm()
        for f in seg[: calib.frames_per_step]:
            calib.feed(f)


def replay_trace(path, calibrate: bool = True, pipeline: CookPipeline | None = None) -> list:
    """Replay a Phase-3 raw trace through the whole cooked pipeline, hardware-free
    (design [N6]). Returns the list of wire `frame`s.

    When `calibrate` (default), the trace's `rest`/`lean-forward`/`lean-right` (R) or
    `rest` (L) segments first drive calibration on the pipeline's cookers, so the
    replayed cooked values are real — flex calibrated to rest, lean on resolved axes,
    leg rest gravity snapped. With `calibrate=False` the pipeline runs on defaults
    (still well-formed, just uncalibrated).
    """
    lines = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    meta, raw_frames = lines[0], lines[1:]
    pad = meta["pad"]
    pipe = pipeline or CookPipeline()

    if calibrate:
        by_seg: dict[str, list] = {}
        for f in raw_frames:
            by_seg.setdefault(f["seg"], []).append(f)
        _calibrate_from_segments(pipe, pad, by_seg)

    return [pipe.process(f["side"], f, f["t"]) for f in raw_frames]


__all__ = ["CookPipeline", "replay_trace"]
