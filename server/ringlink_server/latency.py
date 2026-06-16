"""Phase 1 latency harness — de-risk the transport premise (design [M1], plan §Phase 1).

Measures the round-trip latency the WS transport *adds* on loopback, with no
hardware: the server stamps a realistically-shaped `frame`, sends it, the client
echoes it straight back, and the server measures elapsed time on its **own clock**
(single-clock RTT — immune to cross-process clock skew). This is the quantity the
plan's GO/NO-GO gate is about; the ~16 ms HID poll floor is separate and dominates
in the real system as long as this added latency stays well under it.

The closed-loop ping (send -> await echo -> measure) deliberately keeps one
message in flight at a time, which is what input responsiveness cares about. The
open-loop streaming path lives in `transport.serve_stub`.

`measure_latency()` is reused by `tests/test_latency.py` (a kept perf check) and by
`python -m ringlink_server latency` (the human-facing GO/NO-GO report).
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import dataclass

from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .transport import (
    ALLOWED_ORIGINS,
    DEFAULT_HOST,
    make_stub_frame,
    server_port,
    set_nodelay,
)

# GO/NO-GO thresholds on p95 added RTT, in ms (plan §Phase 1).
TARGET_P95_MS = 5.0  # green: target
ACCEPTABLE_P95_MS = 10.0  # acceptable: HID poll (~16 ms) still dominates
RED_P95_MS = 12.0  # red: transport approaching the HID floor


@dataclass
class LatencyResult:
    n: int
    warmup: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    mean_ms: float
    stdev_ms: float
    verdict: str  # GREEN | ACCEPTABLE | CAUTION | RED
    note: str


def _percentile(sorted_samples: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..1). Assumes a sorted, non-empty list."""
    if not sorted_samples:
        return float("nan")
    k = max(0, min(len(sorted_samples) - 1, round(q * (len(sorted_samples) - 1))))
    return sorted_samples[k]


def summarize(samples: list[float], warmup: int) -> LatencyResult:
    s = sorted(samples)
    p50 = _percentile(s, 0.50)
    p95 = _percentile(s, 0.95)
    p99 = _percentile(s, 0.99)
    mx = s[-1]
    mean = statistics.fmean(s)
    stdev = statistics.pstdev(s)

    # Verdict: p95 against the plan's bands, with a variance guard. "High
    # variance" (a long tail) is itself a red flag even if p95 looks fine.
    high_variance = p99 > 3 * max(p95, 0.001) and p99 > ACCEPTABLE_P95_MS
    if high_variance:
        verdict = "RED"
        note = f"high variance: p99 {p99:.2f}ms >> p95 {p95:.2f}ms"
    elif p95 < TARGET_P95_MS:
        verdict = "GREEN"
        note = f"p95 {p95:.2f}ms < {TARGET_P95_MS}ms target"
    elif p95 <= ACCEPTABLE_P95_MS:
        verdict = "ACCEPTABLE"
        note = f"p95 {p95:.2f}ms within HID-dominated band (<= {ACCEPTABLE_P95_MS}ms)"
    elif p95 <= RED_P95_MS:
        verdict = "CAUTION"
        note = f"p95 {p95:.2f}ms approaching HID floor; re-baseline before proceeding"
    else:
        verdict = "RED"
        note = f"p95 {p95:.2f}ms > {RED_P95_MS}ms — transport too costly on loopback"

    return LatencyResult(
        n=len(s),
        warmup=warmup,
        p50_ms=round(p50, 3),
        p95_ms=round(p95, 3),
        p99_ms=round(p99, 3),
        max_ms=round(mx, 3),
        mean_ms=round(mean, 3),
        stdev_ms=round(stdev, 3),
        verdict=verdict,
        note=note,
    )


async def measure_latency(
    n: int = 2000,
    warmup: int = 200,
    host: str = DEFAULT_HOST,
    port: int = 0,
) -> LatencyResult:
    """Run the closed-loop RTT measurement end-to-end and return a summary.

    Server and client run in the same event loop but communicate over a real
    loopback TCP/WebSocket connection, so the full encode/decode/syscall path is
    exercised. `port=0` binds an ephemeral port (safe for tests + parallel runs).
    """
    samples: list[float] = []
    done = asyncio.Event()

    async def server_handler(conn: ServerConnection) -> None:
        set_nodelay(conn)
        try:
            for i in range(warmup + n):
                t0 = time.perf_counter()
                await conn.send(json.dumps(make_stub_frame(i, t0)))
                await conn.recv()  # client echoes the frame straight back
                rtt_ms = (time.perf_counter() - t0) * 1000.0
                if i >= warmup:
                    samples.append(rtt_ms)
        except ConnectionClosed:
            pass
        finally:
            done.set()

    async with serve(server_handler, host, port, origins=ALLOWED_ORIGINS) as server:
        bound_port = server_port(server)

        async def echo_client() -> None:
            async with connect(f"ws://{host}:{bound_port}") as ws:
                set_nodelay(ws)
                try:
                    async for msg in ws:
                        await ws.send(msg)
                except ConnectionClosed:
                    pass

        client_task = asyncio.create_task(echo_client())
        try:
            await done.wait()
        finally:
            client_task.cancel()
            try:
                await client_task
            except (asyncio.CancelledError, ConnectionClosed):
                pass

    return summarize(samples, warmup)


def format_report(r: LatencyResult) -> str:
    # ASCII-only: this runs on plain Windows consoles (cmd.exe / cp1252) where a
    # friend might launch it; non-ASCII would risk a UnicodeEncodeError on print.
    return (
        "ringlink Phase 1 - transport latency (loopback, added round-trip)\n"
        f"  samples : {r.n}  (after {r.warmup} warmup)\n"
        f"  p50     : {r.p50_ms:.3f} ms\n"
        f"  p95     : {r.p95_ms:.3f} ms\n"
        f"  p99     : {r.p99_ms:.3f} ms\n"
        f"  max     : {r.max_ms:.3f} ms\n"
        f"  mean    : {r.mean_ms:.3f} ms  (stdev {r.stdev_ms:.3f})\n"
        f"  VERDICT : {r.verdict} - {r.note}\n"
        f"  gate    : GREEN <{TARGET_P95_MS}ms | ACCEPTABLE <={ACCEPTABLE_P95_MS}ms | "
        f"RED >{RED_P95_MS}ms or high variance"
    )


def run(n: int = 2000, warmup: int = 200) -> LatencyResult:
    """Synchronous entry point for the CLI."""
    return asyncio.run(measure_latency(n=n, warmup=warmup))


__all__ = [
    "LatencyResult",
    "measure_latency",
    "summarize",
    "format_report",
    "run",
    "TARGET_P95_MS",
    "ACCEPTABLE_P95_MS",
    "RED_P95_MS",
]
