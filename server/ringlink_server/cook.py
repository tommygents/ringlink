"""L3 — the semantic core (the craft): raw frames -> cooked vocabulary.

Turns L1's raw nullable `{strain, accel, gyro, ...}` into the normative vocabulary
(`flex`/`squeeze`/`pull`, `lean.pitch`/`lean.roll`, and — added in the next L3
increment — `gait`/`squatting`/`squat_reps` + edge events). Per the design spec this
is where the *craft* lives; the mappings are **normative** (one sign flip silently
inverts every game — [N1]), so they are pinned and trace-verified.

This module is **pure and stateful-but-hardware-free**: every class is driven by
fed-in samples, so the whole cooked pipeline is tested by replaying the Phase-3
traces with no Joy-Cons (design [N6]). Calibration here **snaps** a rest pose from a
still capture (the spec's explicit `calibrate`, C5) rather than the old continuous
EMA — the drift-EMA runs only *between* snaps (added when L4 drives calibration).

Nullable contract: `cook(None)` returns `None`; the caller (L4) holds the last cooked
value for a missing raw input and emits no event.
"""

from __future__ import annotations

# Strain envelope (design vocabulary table): raw 0x00 fully pulled -> -1, calibrated
# rest -> 0, 0x14 fully pushed -> +1. The rest point is per-grip (calibrated); the
# floor/ceiling are the hardware-observed extremes.
PULL_FLOOR = 0x00
PUSH_CEIL = 0x14  # 20
REST_DEFAULT = 0x0A  # 10, the documented neutral until a snap calibrates it


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


class FlexCal:
    """Signed `flex` from raw strain, calibrated to a per-grip rest.

    `snap()` sets the rest from a still capture; `cook()` maps strain -> [-1, 1],
    scaling the pull side by (rest - floor) and the push side by (ceil - rest) so a
    rest of, say, 12 still reaches -1 at full pull and +1 at full push.
    """

    def __init__(self, rest: float = REST_DEFAULT):
        self.rest = float(rest)

    def snap(self, strains: list) -> None:
        s = [float(v) for v in strains if v is not None]
        if s:
            self.rest = _mean(s)

    def cook(self, strain) -> float | None:
        if strain is None:
            return None  # nullable: caller holds the last cooked value
        s = float(strain)
        if s >= self.rest:
            denom = PUSH_CEIL - self.rest
            flex = (s - self.rest) / denom if denom > 0 else 0.0
        else:
            denom = self.rest - PULL_FLOOR
            flex = -(self.rest - s) / denom if denom > 0 else 0.0
        return clamp(flex)


def squeeze_of(flex: float) -> float:
    """Convenience half: `max(0, flex)` (design D7)."""
    return max(0.0, flex)


def pull_of(flex: float) -> float:
    """Convenience half: `max(0, -flex)` (design D7)."""
    return max(0.0, -flex)


def gravity_axis(rest_accel: tuple) -> int:
    """The accel axis carrying gravity at rest (largest |rest component|).

    A still pad reads ~1 g along one axis; that axis deflects under *any* lean
    (gravity rotates off it), so it is a poor discriminator and must be excluded from
    directed-gesture resolution — see `resolve_axis`.
    """
    return max(range(3), key=lambda i: abs(rest_accel[i]))


class WeakGestureError(Exception):
    """A directed-gesture capture didn't deflect enough to resolve an axis — the user
    likely hadn't started moving yet (capture armed before the lean) or the window was
    empty. The calibration state machine catches this and re-prompts the step rather
    than committing a near-arbitrary axis (see `Calibrator`)."""


def resolve_axis(rest_accel: tuple, samples: list, exclude: int | None = None,
                 min_deflection: float = 0.0) -> tuple[int, int]:
    """Directed-gesture axis resolution [G2].

    Given the rest accel (gravity vector) and accel samples from ONE directed lean
    (forward, or right), return `(axis_index, sign)`: the accel axis whose signed
    deflection from rest reached the largest magnitude, and the sign of that peak.
    That (axis, sign) is what `lean.pitch` / `.roll` are computed against thereafter —
    replacing the first build's runtime axis-guess keys with a deterministic capture.

    `exclude` drops a candidate axis (the gravity axis): because a still pad rests at
    ~1 g on one axis, every lean rotates gravity off it, so it deflects in *both*
    directions and would otherwise win spuriously and collide pitch with roll. The
    robust discriminator is the secondary axis (~0 at rest, grows with one lean).

    `min_deflection` is a floor on the winning peak's magnitude: below it the capture
    saw no real lean (a still or empty window) and `max()` would still hand back a
    near-arbitrary axis — so we raise `WeakGestureError` instead of resolving garbage.
    Default 0.0 keeps the pure primitive permissive; the state machine supplies a floor.
    """
    peak = [0.0, 0.0, 0.0]  # signed delta of largest |.| seen per axis
    for a in samples:
        if a is None:
            continue
        for i in range(3):
            if i == exclude:
                continue
            d = a[i] - rest_accel[i]
            if abs(d) > abs(peak[i]):
                peak[i] = d
    candidates = [i for i in range(3) if i != exclude]
    idx = max(candidates, key=lambda i: abs(peak[i]))
    if abs(peak[idx]) < min_deflection:
        raise WeakGestureError(
            f"peak deflection {abs(peak[idx]):.0f} < floor {min_deflection:.0f}"
        )
    sign = 1 if peak[idx] >= 0 else -1
    return idx, sign


# Raw accel delta (along a resolved axis) that maps to a full-scale lean of ±1.
# ~2048 ≈ half a g ≈ a firm ~30° tilt — tunable; saturating before 90° keeps lean
# responsive without demanding extreme motion. Verified against the Phase-3 trace.
LEAN_FULL_SCALE = 2048.0

# Floor (raw accel delta) a directed lean must clear to resolve an axis. ~1024 ≈ ¼ g ≈
# a clear ~15° tilt — comfortably under a real directed lean (Phase-3 peaks were
# 6000–8000) and well over still-pose jitter, so a capture that saw no real motion
# raises `WeakGestureError` rather than committing a near-arbitrary axis.
MIN_LEAN_DEFLECTION = 1024.0


class LeanCal:
    """`lean.pitch` / `lean.roll` for the right pad, against directed-gesture-resolved
    axes. `snap_rest()` captures the gravity vector; `resolve_pitch/roll()` consume a
    directed-lean capture; `cook()` projects live accel onto the resolved axes.
    """

    def __init__(self):
        self.rest_accel: tuple = (0.0, 0.0, 0.0)
        self.pitch: tuple[int, int] = (2, 1)  # (axis_index, sign); set by resolution
        self.roll: tuple[int, int] = (0, 1)

    def snap_rest(self, accels: list) -> None:
        a = [s for s in accels if s is not None]
        if a:
            self.rest_accel = tuple(_mean([s[i] for s in a]) for i in range(3))

    def resolve_pitch(self, samples: list, min_deflection: float = 0.0) -> None:
        # Exclude the gravity axis: it deflects under any lean and would resolve
        # spuriously (and could collide pitch with roll). See resolve_axis. A
        # min_deflection floor (supplied by the state machine) raises WeakGestureError
        # on a no-motion capture rather than committing a near-arbitrary axis.
        self.pitch = resolve_axis(self.rest_accel, samples,
                                  exclude=gravity_axis(self.rest_accel),
                                  min_deflection=min_deflection)

    def resolve_roll(self, samples: list, min_deflection: float = 0.0) -> None:
        self.roll = resolve_axis(self.rest_accel, samples,
                                 exclude=gravity_axis(self.rest_accel),
                                 min_deflection=min_deflection)

    def cook(self, accel) -> dict | None:
        if accel is None:
            return None  # nullable: caller holds the last cooked lean

        def comp(axis_sign: tuple[int, int]) -> float:
            i, sign = axis_sign
            return clamp(sign * (accel[i] - self.rest_accel[i]) / LEAN_FULL_SCALE)

        return {"pitch": comp(self.pitch), "roll": comp(self.roll)}


class FlexEdges:
    """Discrete `squeeze` / `pull` events from the continuous `flex`.

    Fires once when flex crosses the onset, then **refractory**: it won't re-fire
    until flex relaxes back past a lower `release` threshold (hysteresis), so a single
    held press is one event and sensor jitter near the onset can't machine-gun. This
    is the server-side edge guarantee the games used to hand-roll (flappy's reader).
    `squeeze` and `pull` are tracked independently (opposite flex signs).
    """

    def __init__(self, onset: float = 0.5, release: float = 0.2):
        self.onset = onset
        self.release = release
        self._sq_armed = True
        self._pl_armed = True

    def update(self, flex) -> list:
        if flex is None:
            return []  # nullable: no flex this tick, no edge
        events = []
        # squeeze (positive flex)
        if self._sq_armed and flex >= self.onset:
            events.append({"type": "squeeze", "strength": clamp(flex, 0.0, 1.0)})
            self._sq_armed = False
        elif not self._sq_armed and flex <= self.release:
            self._sq_armed = True
        # pull (negative flex)
        if self._pl_armed and flex <= -self.onset:
            events.append({"type": "pull", "strength": clamp(-flex, 0.0, 1.0)})
            self._pl_armed = False
        elif not self._pl_armed and flex >= -self.release:
            self._pl_armed = True
        return events


# Frames captured per calibration step. ~90 @66Hz ≈ 1.4s — long enough for a steady
# gravity read / a full directed lean, short enough not to tire the user. Tunable; L4
# may map a `seconds` request onto it. Trace-replay tests feed exactly this many.
FRAMES_PER_STEP = 90


class Calibrator:
    """The directed-gesture calibration **state machine** — snap→resolve over LIVE
    frames (design [G2], C5; PROTOCOL.md "Axis resolution").

    The pure cookers (`FlexCal`, `LeanCal`, `LegCooker`) already know how to snap a rest
    or resolve an axis from a *batch* of samples. This drives that batch capture from a
    live frame stream: per step it collects `frames_per_step` frames, finalizes (snapping
    / resolving the wired cookers), and advances — exposing `step`/`pct`/`done` so L4 can
    broadcast `calibrating` messages and prompt the user. It owns no transport; L4 (Phase
    5) feeds it frames and fans its progress out.

    **Arming gate (do not pump blindly).** A capture only counts frames while *armed*.
    The machine starts disarmed and disarms after every step, so the intended L4 loop is:
    show the `step` prompt → give the user a beat to react → `arm()` → feed frames until
    the step advances. Without this, the next directed window would open the instant the
    prior one filled — before the user has moved — and capture pre-gesture frames. While
    disarmed, `feed()` is ignored, so reaction-gap frames never contaminate a capture.

    **Weak-gesture guard.** A directed step that saw no real lean (armed too early, or the
    user didn't move) does not silently commit a near-arbitrary axis: `resolve_axis`
    raises `WeakGestureError`, which the machine catches, sets `weak=True`, disarms, and
    **stays on the same step** so L4 re-prompts. `arm()` clears `weak`.

    Pads calibrate **independently** (PROTOCOL.md): R runs rest→lean-forward→lean-right
    (rest snaps flex rest + lean gravity; the two leans resolve pitch/roll against the
    gravity-excluded axes); L runs rest only (snap the worn gravity vector — `LegTracker`
    is orientation-agnostic, no directed step). Nullable contract: a frame whose `accel`
    is missing does **not** count toward a step (a too-short report can't calibrate).

    The `rest` step uses *mean* snaps (gravity + flex), so it is the contamination-
    sensitive one — prompt "hold still" and only `arm()` once the user is settled.

    Multi-client *serialization/broadcast* of calibration is L4 fan-out, scoped to Phase
    5 — this is just the single-pad engine it will drive.
    """

    R_STEPS = ("rest", "lean-forward", "lean-right")
    L_STEPS = ("rest",)

    def __init__(self, pad: str, *, flex=None, lean=None, leg=None,
                 frames_per_step: int = FRAMES_PER_STEP,
                 min_deflection: float = MIN_LEAN_DEFLECTION):
        if pad not in ("R", "L"):
            raise ValueError(f"pad must be 'R' or 'L', got {pad!r}")
        if frames_per_step < 1:
            raise ValueError(f"frames_per_step must be >= 1, got {frames_per_step}")
        self.pad = pad
        self.frames_per_step = frames_per_step
        self.min_deflection = min_deflection
        self._flex = flex
        self._lean = lean
        self._leg = leg
        self._steps = self.R_STEPS if pad == "R" else self.L_STEPS
        self._i = 0
        self._buf: list = []  # accel samples collected for the current step
        self._armed = False   # feed() counts only while armed (see class docstring)
        self.done = False
        self.weak = False         # last directed capture was too weak; re-prompt
        self.flex_snapped = False  # rest captured a usable flex rest (else stayed default)

    @property
    def step(self) -> str | None:
        """The current step's name (what to prompt the user), or `None` when done."""
        return None if self.done else self._steps[self._i]

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def pct(self) -> int:
        """0–100 progress through the current step's capture window (0 while disarmed)."""
        if self.done:
            return 100
        return min(100, round(100 * len(self._buf) / self.frames_per_step))

    def arm(self) -> None:
        """L4 calls this once it has shown the current step's prompt and given the user a
        beat to react. Only then does `feed()` accumulate. Starts a fresh window and
        clears any prior weak-gesture flag (this is a fresh attempt at the step)."""
        if self.done:
            return
        self._armed = True
        self.weak = False
        self._buf = []

    def feed(self, frame: dict) -> None:
        """Feed one raw frame. Counts toward the window only while armed; auto-finalizes
        and advances when the window fills. No-op once `done` or while disarmed."""
        if self.done or not self._armed:
            return
        if frame.get("accel") is None:
            return  # nullable: a too-short report can't contribute to calibration
        self._buf.append(frame)
        if len(self._buf) >= self.frames_per_step:
            self._finalize()

    def _finalize(self) -> None:
        step = self._steps[self._i]
        accels = [f["accel"] for f in self._buf]
        try:
            if step == "rest":
                if self.pad == "R":
                    # Rest snaps BOTH flex rest and lean gravity — and must run before
                    # the leans, since resolution excludes the gravity axis snapped here.
                    if self._flex is not None:
                        before = self._flex.rest
                        self._flex.snap([f.get("strain") for f in self._buf])
                        self.flex_snapped = self._flex.rest != before or any(
                            f.get("strain") is not None for f in self._buf
                        )
                    if self._lean is not None:
                        self._lean.snap_rest(accels)
                elif self._leg is not None:
                    self._leg.snap_rest(accels)
            elif step == "lean-forward":
                if self._lean is not None:
                    self._lean.resolve_pitch(accels, min_deflection=self.min_deflection)
            elif step == "lean-right":
                if self._lean is not None:
                    self._lean.resolve_roll(accels, min_deflection=self.min_deflection)
        except WeakGestureError:
            # The capture saw no real lean — don't commit a garbage axis. Stay on this
            # step, disarmed, flagged weak; L4 re-prompts and re-arms.
            self.weak = True
            self._armed = False
            self._buf = []
            return
        self._buf = []
        self._armed = False  # next step requires a fresh arm() after its prompt
        self._i += 1
        if self._i >= len(self._steps):
            self.done = True


__all__ = [
    "PULL_FLOOR",
    "PUSH_CEIL",
    "REST_DEFAULT",
    "LEAN_FULL_SCALE",
    "MIN_LEAN_DEFLECTION",
    "FRAMES_PER_STEP",
    "clamp",
    "FlexCal",
    "squeeze_of",
    "pull_of",
    "gravity_axis",
    "resolve_axis",
    "WeakGestureError",
    "LeanCal",
    "FlexEdges",
    "Calibrator",
]
