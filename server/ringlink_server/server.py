"""L4 — the reference WS server: fan-out, backpressure, and the calibration control
plane (design [C3]/[C5], PROTOCOL §Messages, §Calibration; plan §Phase 5).

One server owns one cooked `CookPipeline` and fans its `frame`s out to any number of
clients. Each client gets a `ClientChannel` (coalescing under backpressure) and a
writer task; a single *consume* task pumps raw reports from an injected `frame_source`
through the pipeline and broadcasts the result. The control plane handles `calibrate`:
calibration is a **global** operation (one HID handle, one rest pose), so concurrent
requests are **serialized by rejection** — a second `calibrate` while one is running
gets an `error: calibration_busy`, and the in-progress `calibrating` stream is
broadcast to *every* client (PROTOCOL §Calibration; the protocol permits queued *or*
rejected — v1 rejects, which is sufficient on loopback).

The frame source is injected (an async iterable of `(side, raw, t)`) so the whole
server runs hardware-free in CI against the Phase-3 traces; production wires it to a
live `RingHub` via `hub_frame_source`.

Wire builders here are **PROTOCOL 0.2**: no `pose`, and calibration completion is the
`calibrating` message with `step: null`.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import PROTOCOL_VERSION
from .channel import ClientChannel
from .cook import FRAMES_PER_STEP, Calibrator, WeakGestureError
from .pipeline import CookPipeline
from .transport import (
    ALLOWED_ORIGINS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_RATE_HZ,
    server_port,
    set_nodelay,
)

DEFAULT_CALIB_BEAT_S = 0.5  # reaction beat between showing a step prompt and arming
DEFAULT_CALIB_STEP_TIMEOUT_S = 5.0  # per-step wait before declaring the pad stalled
# (A live pad streams ~66 Hz, so a frame arrives every ~15 ms during a normal step;
# only a pad that stopped streaming hits this bound. Far above a real step's cadence.)


# --------------------------------------------------------------------------- #
# Wire builders (PROTOCOL 0.2)
# --------------------------------------------------------------------------- #

def make_hello(session_id: str, pads: dict, rate_hz: int = DEFAULT_RATE_HZ) -> dict:
    return {
        "type": "hello",
        "protocol": PROTOCOL_VERSION,
        "session_id": session_id,
        "pads": pads,
        "calibration": "ready",
        "max_rate_hz": rate_hz,
    }


def make_calibrating(pad: str, step, pct: int) -> dict:
    """`calibrating` progress (no `pose`, per 0.2). `step is None` => completed."""
    return {"type": "calibrating", "pad": pad, "step": step, "pct": pct}


def make_status(pads: dict) -> dict:
    return {"type": "status", "pads": pads}


def make_error(code: str, message: str) -> dict:
    return {"type": "error", "code": code, "message": message}


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

class RingServer:
    """Owns the cooked pipeline, the client set, and the calibration control plane.

    `frame_source` is an async iterable of `(side, raw, t)`. `client_maxsize` bounds
    each client's outbound frame buffer before coalescing. `calib_beat_s` is the
    reaction beat the server waits after prompting a step before arming the capture;
    `calib_frames_per_step` overrides the capture-window length (tests use a small one).
    """

    def __init__(
        self,
        frame_source,
        *,
        pipeline: CookPipeline | None = None,
        client_maxsize: int = 1,
        calib_beat_s: float = DEFAULT_CALIB_BEAT_S,
        calib_frames_per_step: int = FRAMES_PER_STEP,
        calib_step_timeout_s: float = DEFAULT_CALIB_STEP_TIMEOUT_S,
        rate_hz: int = DEFAULT_RATE_HZ,
        pads: dict | None = None,
        writer_delay: float = 0.0,
    ):
        self.frame_source = frame_source
        self.pipeline = pipeline or CookPipeline()
        self.client_maxsize = client_maxsize
        self.calib_beat_s = calib_beat_s
        self.calib_frames_per_step = calib_frames_per_step
        # Liveness guard: if the calibrating pad stops streaming (slept/disconnected)
        # mid-capture, a step's `q.get()` would block forever and leak the global
        # calibration slot. Bound each step's wait; on timeout, abort + free the slot.
        self.calib_step_timeout_s = calib_step_timeout_s
        # A per-send pause modelling a slow consumer (a slow client makes the
        # server's `send()` block — same effect as this delay). 0.0 in production;
        # tests set it so backpressure coalescing engages deterministically instead
        # of depending on OS socket-buffer timing.
        self.writer_delay = writer_delay
        self.rate_hz = rate_hz
        self.pads = pads or {"R": True, "L": False}
        self.session_id = secrets.token_hex(3)
        self.clients: set[ClientChannel] = set()
        self.port: int | None = None

        # Calibration state. `_cal_pad` is the single in-progress pad (None = idle);
        # the check-and-set in `_on_control` runs without an await, so two racing
        # `calibrate`s can never both start (asyncio is cooperative — no preemption
        # between the read and the write).
        self._cal_pad: str | None = None
        self._cal_queue: asyncio.Queue | None = None
        self._cal_task: asyncio.Task | None = None

    # ---- fan-out --------------------------------------------------------- #

    def _broadcast_frame(self, frame: dict) -> None:
        for ch in list(self.clients):
            ch.push_frame(frame)

    def _broadcast_control(self, msg: dict) -> None:
        for ch in list(self.clients):
            ch.push_control(msg)

    def broadcast_status(self, pads: dict) -> None:
        """Public: broadcast a `status` to all clients (production wires the
        `RingHub` watchdog to this via `loop.call_soon_threadsafe`)."""
        self._broadcast_control(make_status(pads))

    # ---- the frame pump -------------------------------------------------- #

    async def _consume(self) -> None:
        """Pull raw reports, cook each into a wire frame, broadcast it, and (while a
        calibration is active for that pad) route the raw frame to the calibrator."""
        async for side, raw, t in self.frame_source:
            frame = self.pipeline.process(side, raw, t)
            self._broadcast_frame(frame)
            if self._cal_pad == side and self._cal_queue is not None:
                self._cal_queue.put_nowait(raw)

    # ---- control plane --------------------------------------------------- #

    def _on_control(self, data: dict, ch: ClientChannel) -> None:
        """Handle one client->server message. Synchronous (no await) so the
        calibration-busy check-and-set is atomic against racing requests."""
        if data.get("type") != "calibrate":
            return  # unknown control messages are ignored in v1
        if self._cal_pad is not None:
            ch.push_control(
                make_error("calibration_busy", "A calibration is already in progress.")
            )
            return
        pad = data.get("pad", "R")
        if pad not in ("R", "L"):
            ch.push_control(make_error("bad_pad", f"Unknown pad {pad!r}."))
            return
        # `seconds` is an optional hint mapped onto the per-step capture window
        # (PROTOCOL §Client->server); falls back to the configured default.
        frames_per_step = self.calib_frames_per_step
        seconds = data.get("seconds")
        if isinstance(seconds, (int, float)) and seconds > 0:
            frames_per_step = max(1, round(seconds * self.rate_hz))
        # Claim the calibration slot synchronously, THEN spawn the runner.
        self._cal_pad = pad
        self._cal_queue = asyncio.Queue()
        self._cal_task = asyncio.create_task(self._run_calibration(pad, frames_per_step))

    async def _run_calibration(self, pad: str, frames_per_step: int) -> None:
        """Drive the directed-gesture state machine over live frames, broadcasting
        `calibrating` to all clients. Arms each step after a reaction beat, draining
        pre-arm frames so the gate isn't fed stale reaction-gap frames. Completion is
        a `calibrating` with `step: null`."""
        calib = Calibrator(
            pad,
            flex=self.pipeline.flex,
            lean=self.pipeline.lean,
            leg=self.pipeline.leg,
            frames_per_step=frames_per_step,
        )
        q = self._cal_queue
        assert q is not None
        try:
            while not calib.done:
                cur = calib.step
                self._broadcast_control(make_calibrating(pad, cur, 0))
                await asyncio.sleep(self.calib_beat_s)  # reaction beat
                calib.arm()
                _drain_queue(q)  # discard frames captured during the beat (pre-gesture)
                last_pct = 0
                while True:
                    try:
                        raw = await asyncio.wait_for(q.get(), timeout=self.calib_step_timeout_s)
                    except asyncio.TimeoutError:
                        # The calibrating pad stopped streaming — abort rather than wedge
                        # the global slot. The `finally` frees `_cal_pad`.
                        self._broadcast_control(make_error(
                            "calibration_timeout",
                            f"Calibration timed out waiting for {pad} frames.",
                        ))
                        return
                    try:
                        calib.feed(raw)
                    except WeakGestureError:
                        # Defensive: Calibrator catches this internally and sets `weak`;
                        # this guards a future refactor that lets it escape.
                        break
                    if calib.done or calib.step != cur or calib.weak:
                        break  # advanced, finished, or weak -> re-prompt same step
                    if calib.pct != last_pct:
                        last_pct = calib.pct
                        self._broadcast_control(make_calibrating(pad, cur, calib.pct))
            self._broadcast_control(make_calibrating(pad, None, 100))
        finally:
            self._cal_pad = None
            self._cal_queue = None
            self._cal_task = None

    # ---- per-connection -------------------------------------------------- #

    async def _writer(self, conn: ServerConnection, ch: ClientChannel) -> None:
        try:
            while True:
                for msg in await ch.wait_drain():
                    await conn.send(json.dumps(msg))
                if self.writer_delay:
                    await asyncio.sleep(self.writer_delay)
        except ConnectionClosed:
            pass

    async def handler(self, conn: ServerConnection) -> None:
        set_nodelay(conn)
        ch = ClientChannel(maxsize=self.client_maxsize)
        # hello goes out directly, before fan-out registration, so it is always first.
        await conn.send(json.dumps(make_hello(self.session_id, self.pads, self.rate_hz)))
        self.clients.add(ch)
        writer = asyncio.create_task(self._writer(conn, ch))
        try:
            async for msg in conn:
                try:
                    data = json.loads(msg)
                except (ValueError, TypeError):
                    continue
                self._on_control(data, ch)
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(ch)
            writer.cancel()


@asynccontextmanager
async def run_server(
    frame_source,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    sock=None,
    **kwargs,
):
    """Async context manager: bind the WS server, start the frame pump, yield the
    running `RingServer` (with `.port` bound). Cancels cleanly on exit.

    Pass `sock` (an already-bound, listening socket — the singleton lock) to listen
    on it instead of binding `host`/`port`; binding the same port twice fails, so
    lock and listener must be the same socket (see `lifecycle.acquire_singleton`)."""
    app = RingServer(frame_source, **kwargs)
    bind = {"sock": sock} if sock is not None else {"host": host, "port": port}
    async with serve(app.handler, origins=ALLOWED_ORIGINS, **bind) as ws_server:
        app.port = server_port(ws_server)
        consume = asyncio.create_task(app._consume())
        try:
            yield app
        finally:
            consume.cancel()
            if app._cal_task is not None:
                app._cal_task.cancel()
            for task in (consume, app._cal_task):
                if task is not None:
                    try:
                        await task
                    except (asyncio.CancelledError, ConnectionClosed):
                        pass


def _drain_queue(q: asyncio.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            return


async def hub_frame_source(hub, poll_timeout: float = 0.2):
    """Production frame source: bridge a (thread-fed) `RingHub.frames` queue into an
    async stream of `(side, raw, t)`. Each blocking `get` runs in a worker thread so
    the event loop never stalls on the HID reader."""
    import queue as _queue

    while True:
        try:
            item = await asyncio.to_thread(hub.frames.get, True, poll_timeout)
        except _queue.Empty:
            continue
        yield item


__all__ = [
    "RingServer",
    "run_server",
    "hub_frame_source",
    "make_hello",
    "make_calibrating",
    "make_status",
    "make_error",
    "DEFAULT_CALIB_BEAT_S",
    "DEFAULT_CALIB_STEP_TIMEOUT_S",
]
