"""L4 WS server: protocol conformance + the two bug-prone concurrency properties
(design [C3]/[C5], plan §Phase 5). All hardware-free — the frame source is injected,
so a real loopback server is exercised with traces instead of Joy-Cons.

* **[C5]** two clients race a `calibrate`: the second is rejected (`calibration_busy`)
  and *both* still receive the `calibrating` broadcast (calibration is global).
* **[C3]** a deliberately slow consumer: its pending frames coalesce (server-side
  channel stat) yet it loses **zero** events, and it never stalls the producer
  (`test_producer_is_not_stalled_by_a_slow_consumer`).
* **Liveness:** a stalled source times out a calibration instead of wedging the
  global slot forever.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from ringlink_server.pipeline import replay_trace
from ringlink_server.server import run_server

RIGHT = Path(__file__).parent.parent / "traces" / "right-pad.jsonl"


def _right_frames() -> list[tuple]:
    lines = [json.loads(l) for l in RIGHT.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [("R", f, f["t"]) for f in lines[1:]]


def _seg_frames(*segs: str) -> list[tuple]:
    lines = [json.loads(l) for l in RIGHT.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [("R", f, f["t"]) for f in lines[1:] if f["seg"] in segs]


async def _paced_source(frames, start_evt):
    """Yield frames after the test signals start (so none is broadcast to zero
    clients), spaced out so a finite source outlasts a short calibration. NOTE: on
    Windows asyncio's sleep granularity is ~15 ms, so this is the *slow* pacing — used
    where the source must persist, not where speed matters."""
    await start_evt.wait()
    for item in frames:
        await asyncio.sleep(0.002)
        yield item


async def _flood_source(frames, start_evt):
    """Yield frames as fast as the loop allows (`sleep(0)` yields control with no timer
    penalty — avoids the Windows ~15 ms floor). Used to outrun a slow writer so
    backpressure coalescing engages deterministically."""
    await start_evt.wait()
    for item in frames:
        await asyncio.sleep(0)
        yield item


async def _recv_until(ws, predicate, *, timeout=5.0):
    """Collect messages until `predicate(collected)` is true; return the list."""
    collected: list = []

    async def loop():
        while not predicate(collected):
            collected.append(json.loads(await ws.recv()))
        return collected

    return await asyncio.wait_for(loop(), timeout=timeout)


# --------------------------------------------------------------------------- #
# conformance: hello + frames
# --------------------------------------------------------------------------- #

def test_client_gets_hello_then_frames():
    async def scenario():
        start = asyncio.Event()
        src = _paced_source(_right_frames()[:50], start)
        async with run_server(src, port=0) as app:
            async with connect(f"ws://127.0.0.1:{app.port}") as ws:
                hello = json.loads(await ws.recv())
                assert hello["type"] == "hello"
                assert "session_id" in hello
                start.set()
                frame = json.loads(await ws.recv())
                assert frame["type"] == "frame"
                assert frame["seq"] == 0
                assert set(frame["state"]) >= {"flex", "lean", "gait", "squat_reps"}

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# [C5] concurrent calibration: serialize (reject second) + broadcast to all
# --------------------------------------------------------------------------- #

def test_concurrent_calibrate_rejects_second_and_broadcasts_to_all():
    async def scenario():
        start = asyncio.Event()
        # rest -> lean-forward -> lean-right gives a full R calibration sequence.
        src = _paced_source(_seg_frames("rest", "lean-forward", "lean-right"), start)
        async with run_server(src, port=0, calib_beat_s=0.05, calib_frames_per_step=10) as app:
            async with connect(f"ws://127.0.0.1:{app.port}") as a, \
                       connect(f"ws://127.0.0.1:{app.port}") as b:
                # drain both hellos, then let frames flow
                assert json.loads(await a.recv())["type"] == "hello"
                assert json.loads(await b.recv())["type"] == "hello"
                start.set()

                # client A kicks off calibration
                await a.send(json.dumps({"type": "calibrate", "pad": "R"}))

                # wait until A actually sees calibration underway, THEN B races in — so
                # B's request deterministically lands while calibration is in progress.
                a_msgs = await _recv_until(
                    a, lambda c: any(m.get("type") == "calibrating" for m in c)
                )
                await b.send(json.dumps({"type": "calibrate", "pad": "R"}))

                # B must be rejected (busy) AND still receive the calibrating broadcast.
                b_msgs = await _recv_until(
                    b,
                    lambda c: any(m.get("type") == "error" for m in c)
                    and any(m.get("type") == "calibrating" for m in c),
                )
                errors = [m for m in b_msgs if m.get("type") == "error"]
                assert errors and errors[0]["code"] == "calibration_busy"
                assert any(m.get("type") == "calibrating" for m in a_msgs)
                assert any(m.get("type") == "calibrating" for m in b_msgs)

    asyncio.run(scenario())


def test_calibration_completion_is_signaled_by_step_null():
    async def scenario():
        start = asyncio.Event()
        src = _paced_source(_seg_frames("rest", "lean-forward", "lean-right"), start)
        async with run_server(src, port=0, calib_beat_s=0.02, calib_frames_per_step=10) as app:
            async with connect(f"ws://127.0.0.1:{app.port}") as ws:
                assert json.loads(await ws.recv())["type"] == "hello"
                start.set()
                await ws.send(json.dumps({"type": "calibrate", "pad": "R"}))
                msgs = await _recv_until(
                    ws,
                    lambda c: any(
                        m.get("type") == "calibrating" and m.get("step") is None for m in c
                    ),
                )
                cal = [m for m in msgs if m.get("type") == "calibrating"]
                # The sequence walks rest -> lean-forward -> lean-right, then step: null.
                steps = [m["step"] for m in cal]
                assert "rest" in steps
                assert steps[-1] is None  # completion sentinel (PROTOCOL 0.2)
                # No `pose` field anywhere (0.2 removed it).
                assert all("pose" not in m for m in cal)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# [C3] slow consumer: coalesces, loses zero events, doesn't stall a fast peer
# --------------------------------------------------------------------------- #

def test_slow_consumer_coalesces_but_loses_no_events():
    # The [C3] guarantee: a slow consumer's pending frames coalesce to the latest
    # state while ZERO events are dropped. A slow client manifests server-side as a
    # slow `send()`, modelled here by `writer_delay` so the property is deterministic
    # (loopback TCP buffers otherwise absorb the whole stream and nothing coalesces).
    async def scenario():
        frames = _right_frames()
        expected = sum(len(fr["events"]) for fr in replay_trace(RIGHT, calibrate=False))
        assert expected > 0

        def count_events(collected):
            return sum(len(m["events"]) for m in collected if m.get("type") == "frame")

        start = asyncio.Event()
        src = _flood_source(frames, start)
        # Fast source (~1 ms/frame x 2k) against a slow writer (10 ms/send) => the
        # client's outbound channel backs up and coalesces.
        async with run_server(src, port=0, client_maxsize=1, writer_delay=0.01) as app:
            async with connect(f"ws://127.0.0.1:{app.port}") as slow:
                assert json.loads(await slow.recv())["type"] == "hello"
                start.set()
                msgs = await _recv_until(slow, lambda c: count_events(c) >= expected, timeout=10.0)

                # Zero events lost despite heavy coalescing.
                assert count_events(msgs) == expected
                # Coalescing actually engaged (the slow consumer backed up).
                assert sum(ch.coalesced for ch in app.clients) > 0
                # And it received far fewer frames than were produced — the same events
                # folded into coalesced frames carrying the latest state.
                got_frames = sum(1 for m in msgs if m.get("type") == "frame")
                assert got_frames < len(frames)

    asyncio.run(scenario())


async def _stalling_source(frames, start_evt):
    """Emit a handful of frames, then stall forever (never yields again) — models a
    pad that stops streaming mid-calibration (slept/disconnected)."""
    await start_evt.wait()
    for item in frames:
        await asyncio.sleep(0)
        yield item
    await asyncio.Event().wait()  # never set -> hang


def test_calibration_times_out_and_frees_the_slot_when_source_stalls():
    # Important #1: a stalled frame source must not wedge the global calibration slot
    # forever. The per-step capture times out, an `error` is broadcast, and `_cal_pad`
    # resets so future calibrations are accepted.
    async def scenario():
        start = asyncio.Event()
        src = _stalling_source(_seg_frames("rest")[:5], start)
        async with run_server(
            src, port=0, calib_beat_s=0.02, calib_frames_per_step=10,
            calib_step_timeout_s=0.3,
        ) as app:
            async with connect(f"ws://127.0.0.1:{app.port}") as ws:
                assert json.loads(await ws.recv())["type"] == "hello"
                start.set()
                await ws.send(json.dumps({"type": "calibrate", "pad": "R"}))
                msgs = await _recv_until(
                    ws, lambda c: any(m.get("type") == "error" for m in c), timeout=3.0
                )
                err = [m for m in msgs if m.get("type") == "error"][0]
                assert err["code"] == "calibration_timeout"
                # Slot freed: the in-progress pad is cleared.
                await asyncio.sleep(0.05)
                assert app._cal_pad is None

    asyncio.run(scenario())


def test_producer_is_not_stalled_by_a_slow_consumer():
    # Important #2: a slow consumer must not stall the frame producer. With a slow
    # writer, the consume loop still drains the whole finite source promptly (push is
    # non-blocking, per-client) — proven by the pipeline seq advancing to the full
    # count well before a slow client could have received it all.
    async def scenario():
        frames = _right_frames()[:300]
        start = asyncio.Event()
        src = _flood_source(frames, start)
        async with run_server(src, port=0, client_maxsize=1, writer_delay=0.05) as app:
            async with connect(f"ws://127.0.0.1:{app.port}") as ws:
                assert json.loads(await ws.recv())["type"] == "hello"
                start.set()
                # The producer should consume all 300 frames quickly despite the 50ms
                # writer delay (which would need 15s+ to actually send 300 frames).
                async def wait_consumed():
                    while app.pipeline.seq < len(frames):
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_consumed(), timeout=3.0)
                assert app.pipeline.seq == len(frames)

    asyncio.run(scenario())


@pytest.mark.parametrize("origin", [None])
def test_native_client_with_no_origin_is_accepted(origin):
    async def scenario():
        start = asyncio.Event()
        src = _paced_source(_right_frames()[:5], start)
        async with run_server(src, port=0) as app:
            async with connect(f"ws://127.0.0.1:{app.port}") as ws:
                assert json.loads(await ws.recv())["type"] == "hello"

    asyncio.run(scenario())
