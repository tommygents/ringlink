"""L4 WebSocket transport — the skeleton the real server grows into.

Phase 1 ships this as a *stub*: it streams fake `frame`s at a target rate so the
transport premise (cross-process latency over loopback) can be measured before
the real server exists. Per the plan this is **not throwaway** — the loopback
bind + `TCP_NODELAY` + `Origin` allowlist + the per-tick send loop are the actual
L4 seed; later phases replace `make_stub_frame` with cooked frames from L3 and add
fan-out, backpressure coalescing, and the calibration control plane.

Built against `websockets` 16.x (the asyncio API).
"""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import socket
import time

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import PROTOCOL_VERSION

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 28412
DEFAULT_RATE_HZ = 66

# Origin allowlist (design [N4]). A loopback WS is reachable by any local process,
# including a browser tab on a hostile page. Native clients (the pygame client,
# the contract-check harness) send no Origin header -> `None` must be allowed. The
# bundled browser client, served over http for local dev, sends a localhost Origin
# -> the conventional `python -m http.server` ports are allowed here. websockets
# rejects any Origin not in this list with HTTP 403 before the handshake completes.
# (Phase 5 generalizes this to an any-localhost-port predicate; exact strings now.)
ALLOWED_ORIGINS: list[str | None] = [
    None,
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]


def set_nodelay(connection) -> bool:
    """Disable Nagle on a connection's socket (design [M1] — mandatory).

    A 66 Hz stream of small JSON over loopback is the classic Nagle/delayed-ACK
    pathology (~40 ms stalls). Returns True if applied. Best-effort: a transport
    without an underlying socket (shouldn't happen on TCP) is tolerated.
    """
    try:
        sock = connection.transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return True
    except Exception:
        pass
    return False


def make_hello(session_id: str, rate_hz: int = DEFAULT_RATE_HZ) -> dict:
    """The `hello` handshake message (design §Message types)."""
    return {
        "type": "hello",
        "protocol": PROTOCOL_VERSION,
        "session_id": session_id,
        "pads": {"R": True, "L": False},
        "calibration": "ready",
        "max_rate_hz": rate_hz,
    }


def make_stub_frame(seq: int, t: float) -> dict:
    """A fake `frame` shaped exactly like the real wire contract.

    The shape (and therefore the JSON size) matches a real cooked frame so the
    latency harness measures realistic encode/decode cost, not a toy payload.
    Values are deterministic functions of `seq` (no RNG — reproducible).
    """
    flex = round(math.sin(seq / 20.0), 4)
    return {
        "type": "frame",
        "seq": seq,
        "t": round(t, 6),
        "state": {
            "flex": flex,
            "squeeze": round(max(0.0, flex), 4),
            "pull": round(max(0.0, -flex), 4),
            "lean": {
                "pitch": round(0.3 * math.sin(seq / 35.0), 4),
                "roll": round(0.3 * math.cos(seq / 40.0), 4),
            },
            "gait": "run" if (seq // 66) % 2 else "rest",
            "squatting": False,
            "squat_reps": seq // 198,
            "buttons": [],
            "stick": [0.0, 0.0],
        },
        "events": [],
    }


def make_calibrating(pad: str, pose: str, step: str, pct: int) -> dict:
    """A `calibrating` progress message (design §Calibration / §Message types)."""
    return {"type": "calibrating", "pad": pad, "pose": pose, "step": step, "pct": pct}


def make_status(r: str = "live", l: str = "lost") -> dict:
    """A `status` liveness message (design [M5])."""
    return {"type": "status", "pads": {"R": r, "L": l}}


# Scripted calibration the stub plays back on a `calibrate` request. The real L3
# (Phase 4) replaces this with the directed-gesture axis-resolution flow; for the
# spike it exercises the client's `calibrating`-overlay handling. The terminal
# step "done" lets the client clear the overlay without a timer.
_STUB_CALIBRATION_STEPS = ("hold-still", "lean-forward", "lean-right")


async def serve_stub(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    rate_hz: int = DEFAULT_RATE_HZ,
    stop: asyncio.Event | None = None,
    simulate_status: bool = False,
):
    """Run the stub L4 server: `hello`, then `frame`s at ~`rate_hz` per client.

    Production-shaped streaming path (open-loop fire-hose), distinct from the
    closed-loop ping in `latency.py`. Binds loopback only; sets `TCP_NODELAY`;
    enforces the Origin allowlist. Also handles the `calibrate` control message by
    playing back a scripted `calibrating` sequence (Phase 2 spike), and — when
    `simulate_status` — emits a scripted `pad_lost` -> `live` `status` transition
    so a client's wake/reconnect affordance can be exercised hardware-free.

    Runs until `stop` is set (or forever if `stop is None`).
    """
    session_id = secrets.token_hex(3)

    async def handler(conn: ServerConnection) -> None:
        set_nodelay(conn)
        await conn.send(json.dumps(make_hello(session_id, rate_hz)))

        async def frame_sender() -> None:
            period = 1.0 / rate_hz
            start = time.perf_counter()
            next_tick = start
            seq = 0
            while True:
                now = time.perf_counter()
                await conn.send(json.dumps(make_stub_frame(seq, now - start)))
                seq += 1
                next_tick += period
                delay = next_tick - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)

        async def control_receiver() -> None:
            # Frames keep flowing concurrently during calibration (mirrors real L4).
            async for msg in conn:
                try:
                    data = json.loads(msg)
                except (ValueError, TypeError):
                    continue
                if data.get("type") == "calibrate":
                    pad = data.get("pad", "R")
                    for step in _STUB_CALIBRATION_STEPS:
                        for pct in (0, 50, 100):
                            await conn.send(json.dumps(make_calibrating(pad, "rest", step, pct)))
                            await asyncio.sleep(0.04)
                    await conn.send(json.dumps(make_calibrating(pad, "rest", "done", 100)))

        async def status_sim() -> None:
            await asyncio.sleep(1.0)
            await conn.send(json.dumps(make_status(r="lost", l="lost")))
            await asyncio.sleep(0.6)
            await conn.send(json.dumps(make_status(r="live", l="lost")))

        tasks = [asyncio.create_task(frame_sender()), asyncio.create_task(control_receiver())]
        if simulate_status:
            tasks.append(asyncio.create_task(status_sim()))
        try:
            await asyncio.gather(*tasks)
        except ConnectionClosed:
            pass
        finally:
            for t in tasks:
                t.cancel()

    async with serve(handler, host, port, origins=ALLOWED_ORIGINS):
        if stop is None:
            await asyncio.get_event_loop().create_future()  # run forever
        else:
            await stop.wait()


def server_port(server) -> int:
    """The actual bound port of a running `serve(...)` server (handles port=0)."""
    return server.sockets[0].getsockname()[1]


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_RATE_HZ",
    "ALLOWED_ORIGINS",
    "set_nodelay",
    "make_hello",
    "make_stub_frame",
    "make_calibrating",
    "make_status",
    "serve_stub",
    "server_port",
]
