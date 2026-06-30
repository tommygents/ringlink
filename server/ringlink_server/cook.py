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


def resolve_axis(rest_accel: tuple, samples: list, exclude: int | None = None) -> tuple[int, int]:
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
    sign = 1 if peak[idx] >= 0 else -1
    return idx, sign


# Raw accel delta (along a resolved axis) that maps to a full-scale lean of ±1.
# ~2048 ≈ half a g ≈ a firm ~30° tilt — tunable; saturating before 90° keeps lean
# responsive without demanding extreme motion. Verified against the Phase-3 trace.
LEAN_FULL_SCALE = 2048.0


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

    def resolve_pitch(self, samples: list) -> None:
        # Exclude the gravity axis: it deflects under any lean and would resolve
        # spuriously (and could collide pitch with roll). See resolve_axis.
        self.pitch = resolve_axis(self.rest_accel, samples,
                                  exclude=gravity_axis(self.rest_accel))

    def resolve_roll(self, samples: list) -> None:
        self.roll = resolve_axis(self.rest_accel, samples,
                                 exclude=gravity_axis(self.rest_accel))

    def cook(self, accel) -> dict | None:
        if accel is None:
            return None  # nullable: caller holds the last cooked lean

        def comp(axis_sign: tuple[int, int]) -> float:
            i, sign = axis_sign
            return clamp(sign * (accel[i] - self.rest_accel[i]) / LEAN_FULL_SCALE)

        return {"pitch": comp(self.pitch), "roll": comp(self.roll)}


__all__ = [
    "PULL_FLOOR",
    "PUSH_CEIL",
    "REST_DEFAULT",
    "LEAN_FULL_SCALE",
    "clamp",
    "FlexCal",
    "squeeze_of",
    "pull_of",
    "gravity_axis",
    "resolve_axis",
    "LeanCal",
]
