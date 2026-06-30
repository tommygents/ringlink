"""Canonical raw-trace recorder (Phase 3 deliverable).

Records **raw** L1+L2 frames from one pad to a JSONL trace, segment-labelled by a
guided gesture script, so the downstream cooked pipeline (L3/L4) can be tested
**hardware-free** by replaying these traces (plan [N6], Phase 5).

Format — JSONL, one object per line:

* **line 1** metadata:
  ``{"type":"trace-meta","schema":1,"pad":"R","created":"...","segments":[...]}``
* **each frame**:
  ``{"side":"R","t":1.234,"seg":"lean-right","strain":12,"accel":[..],"gyro":[..],
     "buttons":[0,0,0],"stick":[2208,1988]}``

JSONL (not CSV) because the raw fields are **nullable** (a too-short report gives
``strain/accel/gyro == null``) and the records are segment-tagged and multi-field —
a flat CSV would smear nulls and nested vectors. Phase 5 replays these line-by-line.

The directed-gesture motions (**still rest → lean-forward → lean-right**) are the
load-bearing captures: without them in the trace, Phase 5 cannot test [G2] axis
resolution without hardware, and they are the hardest to redo later.

**Countdown-paced, not keypress-gated.** Each segment counts down ("...3, 2, 1,
GO") then records for a fixed window — the user's hands/legs are busy doing the
gesture, so no Enter-presses between segments. Just watch the prompt and move.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .lifecycle import AlreadyRunning, RingHub, acquire_singleton

# Guided segment scripts. (label, prompt, seconds | None). seconds=None -> use the
# open-ended default (for "do as many as you can" segments like squats).
SEGMENTS: dict[str, list[tuple[str, str, float | None]]] = {
    "R": [
        ("rest",         "Hold the Ring-Con STILL at a neutral grip",            3.0),
        ("lean-forward", "Tilt the Ring-Con FORWARD and back, repeatedly",       7.0),
        ("lean-right",   "Tilt the Ring-Con to the RIGHT and back, repeatedly",  7.0),
        ("squeeze",      "SQUEEZE (push in) and release, repeatedly",            7.0),
        ("pull",         "PULL (stretch out) and release, repeatedly",           7.0),
    ],
    "L": [
        ("rest",   "Stand STILL with the leg strap on",        3.0),
        ("squat",  "Do slow SQUATS, all the way down and up",  None),
        ("run",    "RUN in place at a steady jog",             8.0),
        ("sprint", "SPRINT in place (fast knees)",             7.0),
    ],
}

DEFAULT_TRACE_NAME = {"R": "right-pad", "L": "left-pad"}
OPEN_DEFAULT_S = 12.0   # duration for open-ended (seconds=None) segments
COUNTDOWN_S = 3


def _drain(hub: RingHub) -> None:
    """Discard queued frames so pre-segment idle/countdown doesn't leak into a segment."""
    while True:
        try:
            hub.frames.get_nowait()
        except Exception:
            return


def _countdown(label: str, prompt: str, seconds: float) -> None:
    print(f"\n[{label}] {prompt}  ({seconds:.0f}s)")
    for i in range(COUNTDOWN_S, 0, -1):
        print(f"  get ready... {i}", end="\r", flush=True)
        time.sleep(1.0)
    print("  GO!" + " " * 30)


def _record_segment(hub: RingHub, side: str, label: str, seconds: float, out) -> int:
    """Record labelled frames for a fixed window; return the count written."""
    deadline = time.monotonic() + seconds
    n = 0
    last_print = 0.0
    while time.monotonic() < deadline:
        try:
            s, frame, t = hub.frames.get(timeout=0.2)
        except Exception:
            continue
        if s != side:
            continue
        rec = {
            "side": s,
            "t": round(t, 6),
            "seg": label,
            "strain": frame["strain"],
            "accel": list(frame["accel"]) if frame["accel"] is not None else None,
            "gyro": list(frame["gyro"]) if frame["gyro"] is not None else None,
            "buttons": list(frame["buttons"]),
            "stick": list(frame["stick"]),
        }
        out.write(json.dumps(rec) + "\n")
        n += 1
        now = time.monotonic()
        if now - last_print > 0.3:
            last_print = now
            remaining = max(0.0, deadline - now)
            print(f"  recording [{label}] {n:4d} frames  {remaining:4.1f}s left  "
                  f"strain={frame['strain']} accel={frame['accel']}   ", end="\r", flush=True)
    print(f"  [{label}] captured {n} frames" + " " * 30)
    return n


def record_trace(
    side: str,
    out_dir: Path,
    name: str | None = None,
    created: str | None = None,
    open_seconds: float = OPEN_DEFAULT_S,
) -> Path:
    """Run the guided, countdown-paced capture for one pad, writing a JSONL trace.
    Returns the trace path. Raises SystemExit on no-pad / already-running.
    """
    side = side.upper()
    if side not in SEGMENTS:
        raise ValueError(f"side must be 'R' or 'L', got {side!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name or DEFAULT_TRACE_NAME[side]}.jsonl"
    created = created or time.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        sing = acquire_singleton()
    except AlreadyRunning as exc:
        print(f"! {exc}\n  Stop the other ringlink server first.")
        raise SystemExit(1)

    print(f"Bringing up pad {side} (this runs the MCU init; ~3s)...")
    hub = RingHub(sides=(side,))
    try:
        up = hub.start()
        if not up.get(side):
            print(
                f"! Pad {side} did not come up. Press a button on the Joy-Con to "
                f"wake it, then retry."
            )
            raise SystemExit(2)
        segs = SEGMENTS[side]
        print(f"\nPad {side} live. {len(segs)} segments -> {path.name}.")
        print("Follow each prompt; it counts down then records a fixed window.\n")

        with path.open("w", encoding="utf-8") as out:
            out.write(json.dumps({
                "type": "trace-meta",
                "schema": 1,
                "pad": side,
                "created": created,
                "segments": [lbl for lbl, _, _ in segs],
                "note": "ringlink Phase 3 canonical raw trace (L1+L2, pre-cooking).",
            }) + "\n")

            total = 0
            for label, prompt, seconds in segs:
                secs = seconds if seconds is not None else open_seconds
                _countdown(label, prompt, secs)
                _drain(hub)  # start the window clean, after the countdown
                total += _record_segment(hub, side, label, secs, out)

        print(f"\nDone. {total} frames across {len(segs)} segments -> {path}")
        return path
    finally:
        hub.stop()
        sing.release()
