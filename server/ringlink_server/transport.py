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
# this harness) send no Origin header -> `None` must be allowed. The browser
# client's origins are added in Phase 2 when it lands. websockets rejects any
# Origin not in this list with HTTP 403 before the handshake completes.
ALLOWED_ORIGINS: list[str | None] = [None]


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


async def serve_stub(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    rate_hz: int = DEFAULT_RATE_HZ,
    stop: asyncio.Event | None = None,
):
    """Run the stub L4 server: `hello`, then `frame`s at ~`rate_hz` per client.

    This is the production-shaped streaming path (open-loop fire-hose), distinct
    from the closed-loop ping in `latency.py`. Binds loopback only; sets
    `TCP_NODELAY`; enforces the Origin allowlist. Runs until `stop` is set (or
    forever if `stop is None`).
    """
    session_id = secrets.token_hex(3)

    async def handler(conn: ServerConnection) -> None:
        set_nodelay(conn)
        period = 1.0 / rate_hz
        start = time.perf_counter()
        next_tick = start
        seq = 0
        try:
            await conn.send(json.dumps(make_hello(session_id, rate_hz)))
            while True:
                now = time.perf_counter()
                await conn.send(json.dumps(make_stub_frame(seq, now - start)))
                seq += 1
                next_tick += period
                delay = next_tick - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
        except ConnectionClosed:
            return

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
    "serve_stub",
    "server_port",
]
