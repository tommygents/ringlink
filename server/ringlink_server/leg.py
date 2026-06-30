"""L3 — leg semantics: the `gait` / `squatting` / `squat_reps` vocabulary.

`LegTracker` is the proven pure classifier ported from
`recurse-ringcon-hacking/monitor.py` (orientation-agnostic squat-by-tilt + a
rolling gyro-energy gait gate). Two changes from the lift, per the design spec:

* **Snap calibration, not an EMA.** The first build's `calibrate` was a continuous
  EMA; the spec's explicit `calibrate` (C5) **snaps** the rest gravity vector from a
  still capture and resets drift. The slow drift-EMA runs only *between* snaps (wired
  by L4) — `snap_rest` is the snap.
* **Run/sprint thresholds are instance-configurable.** The lifted constants were
  tuned to one person's capture; the Phase-3 left-pad trace showed a different jog
  reads far hotter (mean |gyro| ~3700 vs the old RUN_ON 700). They are tunable here
  and should become calibration-derived (tracked follow-up).

`LegCooker` wraps the tracker and emits the wire vocabulary via the **G1 derivation
order**, which is what makes the squat/gait invariant (never `gait:"run"` mid-squat)
a structural property rather than a guard.
"""

from __future__ import annotations

import math
from collections import deque

# Run/sprint gyro-energy thresholds (raw mean |gyro|). Defaults widened from the
# lift (700/2200) toward a real worn capture; TUNE per user — ideally derive these
# from a calibration jog rather than hardcoding (tracked follow-up).
RUN_ON_DEFAULT = 1500
SPRINT_ON_DEFAULT = 4500


class LegTracker:
    """Pure leg-motion classifier for a thigh-strapped (bare) Joy-Con.

    Feed raw int16 (accel, gyro) triples to `update()`; read `.state`
    (`rest`/`run`/`sprint`/`squat`) and `.squat_reps`. No hardware — unit-testable
    against a recorded trace.

    Squats are detected by how far the gravity vector has tilted from a calibrated
    rest pose (orientation-agnostic; counted only when the tilt PERSISTS, so a
    transient running stride's tilt never counts). Running is a rolling mean of gyro
    magnitude. The squat latch wins over the gait gate — you can't be running and
    squatting at once.
    """

    SQUAT_TILT_ON = 45.0    # deg from rest gravity -> entering a squat
    SQUAT_TILT_OFF = 20.0   # deg -> upright again (hysteresis to re-arm)
    SQUAT_MIN_FRAMES = 20   # tilt must hold this long (~0.3s @66Hz) to count
    RUN_WIN = 50            # rolling window for the gyro energy gate

    def __init__(self, run_on: int = RUN_ON_DEFAULT, sprint_on: int = SPRINT_ON_DEFAULT):
        self.run_on = run_on
        self.sprint_on = sprint_on
        self.rest_accel = None
        self._win: deque = deque(maxlen=self.RUN_WIN)
        self._tilt_frames = 0
        self._armed = True
        self._squatting = False  # latched "squat session in progress"
        self.squat_reps = 0
        self.state = "rest"
        self.tilt = 0.0
        self.energy = 0.0

    def snap_rest(self, accels: list) -> None:
        """Snap the rest gravity vector from a still, WORN capture (the explicit
        `calibrate`). Replaces the first build's continuous EMA."""
        a = [tuple(map(float, s)) for s in accels if s is not None]
        if a:
            self.rest_accel = tuple(sum(s[i] for s in a) / len(a) for i in range(3))

    @staticmethod
    def _angle(a, b) -> float:
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        c = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b)) / (na * nb)))
        return math.degrees(math.acos(c))

    def update(self, accel, gyro) -> str:
        """Feed one IMU sample; returns the current state string."""
        self._win.append(math.sqrt(sum(x * x for x in gyro)))
        self.energy = sum(self._win) / len(self._win)
        self.tilt = self._angle(accel, self.rest_accel) if self.rest_accel else 0.0

        # Squat rep counting: tilt must SUSTAIN past the threshold (distinguishes a
        # held squat from a transient running stride). Hysteresis re-arms only after
        # returning near upright.
        sustained = False
        if self.tilt >= self.SQUAT_TILT_ON:
            self._tilt_frames += 1
            sustained = self._tilt_frames >= self.SQUAT_MIN_FRAMES
            if self._armed and sustained:
                self.squat_reps += 1
                self._armed = False
        else:
            self._tilt_frames = 0
            if self.tilt <= self.SQUAT_TILT_OFF:
                self._armed = True

        # Latch a "squat session" so state stays 'squat' across the whole excursion
        # (the leg still swings between/within reps, tripping the gyro gate). Engages
        # once a lean is sustained (same signal rep-counting uses, so a running stride
        # can't engage it) and releases only when upright AND quiet.
        if sustained:
            self._squatting = True
        elif self.tilt <= self.SQUAT_TILT_OFF and self.energy < self.run_on:
            self._squatting = False

        # State: an active squat session wins over the gyro gate.
        if self._squatting:
            self.state = "squat"
        elif self.energy >= self.sprint_on:
            self.state = "sprint"
        elif self.energy >= self.run_on:
            self.state = "run"
        else:
            self.state = "rest"
        return self.state


class LegCooker:
    """Derives the wire vocabulary (`gait` / `squatting` / `squat_reps`) + `squat_rep`
    and `gait_change` events from `LegTracker`, in the normative **G1 order**.

    The order is what enforces the squat/gait invariant: `squatting` is the latch as a
    bool; `gait` is the tracker state ONLY when it's rest/run/sprint and emits `rest`
    while squatting — so `gait` is structurally never `run`/`sprint` mid-squat, and
    `squat` is never a `gait` value.
    """

    def __init__(self, tracker: LegTracker | None = None):
        self.lt = tracker or LegTracker()
        self._prev_gait = "rest"

    def snap_rest(self, accels: list) -> None:
        self.lt.snap_rest(accels)

    def update(self, accel, gyro) -> tuple[dict | None, list]:
        """Feed one raw IMU sample; return `(state_dict, events)`. Nullable: a missing
        accel/gyro holds the last cooked value and emits nothing (the caller keeps the
        previous state_dict)."""
        if accel is None or gyro is None:
            return None, []

        prev_reps = self.lt.squat_reps
        state = self.lt.update(accel, gyro)

        # G1 derivation order:
        squatting = state == "squat"
        gait = state if state in ("rest", "run", "sprint") else "rest"
        if squatting:
            gait = "rest"  # never run/sprint while squatting (no live locomotion read)

        events = []
        if self.lt.squat_reps > prev_reps:
            events.append({"type": "squat_rep"})
        if gait != self._prev_gait:
            events.append({"type": "gait_change", "from": self._prev_gait, "to": gait})
            self._prev_gait = gait

        return (
            {"gait": gait, "squatting": squatting, "squat_reps": self.lt.squat_reps},
            events,
        )


__all__ = ["RUN_ON_DEFAULT", "SPRINT_ON_DEFAULT", "LegTracker", "LegCooker"]
