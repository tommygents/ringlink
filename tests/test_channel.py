"""Per-client send-channel coalescing tests (design [C3], plan §Phase 5).

The backpressure guarantee is the bug-prone part of L4, so it is pinned here
*deterministically* (no timing): a slow client's pending frames collapse to the
**latest state** while **every event is preserved, in order**, and control-plane
messages (calibrating/status/error) are never coalesced away. The async end-to-end
slow-consumer test lives in `test_server`; this proves the mechanism in isolation.
"""

from __future__ import annotations

import asyncio

from ringlink_server.channel import ClientChannel, coalesce_frames


def _frame(seq: int, flex: float, events: list | None = None) -> dict:
    return {
        "type": "frame",
        "seq": seq,
        "t": float(seq),
        "state": {"flex": flex},
        "events": events or [],
    }


# --------------------------------------------------------------------------- #
# coalesce_frames primitive
# --------------------------------------------------------------------------- #

def test_coalesce_keeps_latest_state_and_concatenates_events_in_order():
    merged = coalesce_frames([
        _frame(0, 0.1, [{"type": "squeeze", "t": 0.0}]),
        _frame(1, 0.2, []),
        _frame(2, 0.3, [{"type": "pull", "t": 2.0}, {"type": "squeeze", "t": 2.1}]),
    ])
    assert merged["seq"] == 2
    assert merged["state"]["flex"] == 0.3              # latest state wins
    assert [e["type"] for e in merged["events"]] == ["squeeze", "pull", "squeeze"]


# --------------------------------------------------------------------------- #
# ClientChannel: coalescing under backpressure
# --------------------------------------------------------------------------- #

def test_slow_client_coalesces_to_latest_state_dropping_zero_events():
    ch = ClientChannel(maxsize=1)  # size-1: every excess frame coalesces immediately
    ch.push_frame(_frame(0, 0.1, [{"type": "squeeze", "t": 0.0}]))
    ch.push_frame(_frame(1, 0.2, [{"type": "pull", "t": 1.0}]))
    ch.push_frame(_frame(2, 0.3, [{"type": "squeeze", "t": 2.0}]))

    msgs = ch.drain()
    assert len(msgs) == 1                               # collapsed to one frame
    assert msgs[0]["state"]["flex"] == 0.3             # latest state
    assert len(msgs[0]["events"]) == 3                 # ZERO events dropped
    assert ch.coalesced == 2


def test_no_coalescing_below_maxsize():
    ch = ClientChannel(maxsize=4)
    for i in range(3):
        ch.push_frame(_frame(i, 0.1 * i))
    msgs = ch.drain()
    assert [m["seq"] for m in msgs] == [0, 1, 2]        # delivered individually
    assert ch.coalesced == 0


def test_events_never_dropped_across_many_pushes():
    ch = ClientChannel(maxsize=1)
    for i in range(100):
        ch.push_frame(_frame(i, 0.0, [{"type": "squeeze", "t": float(i)}]))
    msgs = ch.drain()
    total_events = sum(len(m["events"]) for m in msgs)
    assert total_events == 100                          # all 100 survive coalescing


def test_control_messages_are_not_coalesced_and_precede_frames():
    ch = ClientChannel(maxsize=1)
    ch.push_control({"type": "calibrating", "pad": "R", "step": "rest", "pct": 0})
    ch.push_frame(_frame(0, 0.1))
    ch.push_frame(_frame(1, 0.2))
    ch.push_control({"type": "status", "pads": {"R": "lost"}})

    msgs = ch.drain()
    types = [m["type"] for m in msgs]
    assert types.count("calibrating") == 1
    assert types.count("status") == 1                   # both control msgs survive
    assert types.count("frame") == 1                    # frames coalesced to one


def test_drain_clears_the_buffer():
    ch = ClientChannel(maxsize=1)
    ch.push_frame(_frame(0, 0.1))
    assert ch.has_pending()
    ch.drain()
    assert not ch.has_pending()
    assert ch.drain() == []


# --------------------------------------------------------------------------- #
# async wake-up (the writer side)
# --------------------------------------------------------------------------- #

def test_wait_drain_wakes_on_push():
    async def scenario():
        ch = ClientChannel(maxsize=1)

        async def producer():
            await asyncio.sleep(0.01)
            ch.push_frame(_frame(7, 0.5))

        asyncio.create_task(producer())
        msgs = await asyncio.wait_for(ch.wait_drain(), timeout=1.0)
        assert msgs[0]["seq"] == 7

    asyncio.run(scenario())
