"""Per-client outbound channel with backpressure coalescing (design [C3], PROTOCOL
§Backpressure).

One `ClientChannel` per connected WS client sits between the (single, shared) cooked
frame producer and that client's socket writer. A healthy client drains it every
tick; a *slow* one (GC pause, throttled background tab) falls behind — and the
guarantee is:

* **Latest state, never stale.** Pending `frame`s collapse to one carrying the most
  recent state.
* **Zero events dropped.** Every coalesced frame's events are concatenated, in order,
  onto the survivor. Discrete events (a squeeze, a squat rep) must never be missed
  within a session.
* **Control plane is never coalesced.** `calibrating` / `status` / `error` messages
  ride a separate FIFO and are all delivered.

The producer never blocks on a slow client (the reader thread must never stall — it
is load-bearing for the latency budget): pushing is O(1) and the buffer is bounded
to `maxsize` frames, with overflow folded into a single coalesced frame.

This module is pure buffer logic plus one `asyncio.Event` for writer wake-up, so the
coalescing guarantee is unit-tested with no event loop (`test_channel`); the async
fan-out that drives it lives in `server.py`.
"""

from __future__ import annotations

import asyncio


def coalesce_frames(frames: list) -> dict:
    """Collapse several `frame`s into one: latest seq/t/state, events concatenated in
    arrival order. Assumes a non-empty list of wire frames."""
    last = frames[-1]
    events: list = []
    for f in frames:
        events.extend(f["events"])
    return {
        "type": "frame",
        "seq": last["seq"],
        "t": last["t"],
        "state": last["state"],
        "events": events,
    }


class ClientChannel:
    """Bounded, event-preserving outbound buffer for one client.

    `maxsize` is the number of distinct `frame`s held before coalescing kicks in
    (default 1 — the strongest form: a slow client always sees one up-to-date frame
    with every pending event folded in). Control messages are unbounded and never
    coalesced.
    """

    def __init__(self, maxsize: int = 1):
        if maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize}")
        self.maxsize = maxsize
        self._frames: list = []
        self._ctrl: list = []
        self.coalesced = 0  # how many frames have been coalesced away (a slowness stat)
        self._nonempty = asyncio.Event()

    def push_frame(self, frame: dict) -> None:
        """Queue a cooked frame. If this overflows `maxsize`, the whole pending run
        collapses to one frame (latest state, events concatenated) — never blocking,
        never dropping an event."""
        self._frames.append(frame)
        if len(self._frames) > self.maxsize:
            self.coalesced += len(self._frames) - 1
            self._frames = [coalesce_frames(self._frames)]
        self._nonempty.set()

    def push_control(self, msg: dict) -> None:
        """Queue a control-plane message (calibrating/status/error). Never coalesced.

        Intentionally unbounded: PROTOCOL §Backpressure bounds the *frame* queue only,
        and control messages are low-cadence (calibration is rare; `calibrating` is
        throttled to pct changes). A permanently-stuck client is a disconnect case the
        writer surfaces via `ConnectionClosed`, not something to bound here."""
        self._ctrl.append(msg)
        self._nonempty.set()

    def has_pending(self) -> bool:
        return bool(self._ctrl or self._frames)

    def drain(self) -> list:
        """Take everything pending (control messages first, then the frame[s]) and
        clear the buffer. Returns the messages to send, in order."""
        msgs = self._ctrl + self._frames
        self._ctrl = []
        self._frames = []
        self._nonempty.clear()
        return msgs

    async def wait_drain(self) -> list:
        """Await until something is pending, then `drain()`. The writer loop's pull."""
        await self._nonempty.wait()
        return self.drain()


__all__ = ["ClientChannel", "coalesce_frames"]
