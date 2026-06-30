"""L2 lifecycle tests — no hardware. A ``FakeJoyCon`` stands in for the HID device
so reader threads, backpressure, self-heal, and the singleton can be exercised
deterministically in CI.
"""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path

import pytest

from ringlink_server import lifecycle
from ringlink_server.lifecycle import (
    AlreadyRunning,
    PadReader,
    acquire_singleton,
)

FIXTURE = Path(__file__).parent / "fixtures" / "raw_report_samples.json"
VALID_BUF = bytes.fromhex(json.loads(FIXTURE.read_text())["samples"][0]["hex"])


class FakeJoyCon:
    """Stand-in for ``hid_driver.JoyCon`` with a controllable read schedule."""

    def __init__(self, buf: bytes = VALID_BUF, sleep_until: float = 0.0):
        self.buf = buf
        self._sleep_until = sleep_until  # monotonic time before which read() == b""
        self.reads = 0
        self.inits = 0
        self.closed = False

    def init_ringcon(self) -> bool:
        self.inits += 1
        return True

    def init_imu_only(self) -> bool:
        self.inits += 1
        return True

    def read(self, timeout_ms: int = 200) -> bytes:
        self.reads += 1
        if time.monotonic() < self._sleep_until:
            time.sleep(0.005)  # mimic an HID read timeout while "asleep"
            return b""
        return self.buf

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------------- #

def test_singleton_excludes_second_instance(monkeypatch, tmp_path):
    ep = tmp_path / "endpoint.json"
    monkeypatch.setattr(lifecycle, "endpoint_path", lambda: ep)
    port = 28477

    first = acquire_singleton(port=port)
    try:
        assert ep.exists()
        meta = json.loads(ep.read_text())
        assert meta["port"] == port and meta["host"] == lifecycle.DEFAULT_HOST
        with pytest.raises(AlreadyRunning):
            acquire_singleton(port=port)
    finally:
        first.release()
    # endpoint.json removed on release, and the port is free to re-acquire.
    assert not ep.exists()
    second = acquire_singleton(port=port)
    second.release()


# --------------------------------------------------------------------------- #
# Reader threads
# --------------------------------------------------------------------------- #

def test_reader_streams_parsed_frames():
    q: "queue.Queue" = queue.Queue(maxsize=64)
    reader = PadReader("R", FakeJoyCon(), q, t0=time.monotonic(), stale_timeout_s=1.0)
    reader.start()
    try:
        side, frame, t = q.get(timeout=2.0)
    finally:
        reader.stop()
    assert side == "R"
    assert frame["strain"] is not None
    assert isinstance(t, float)


def test_stop_is_bounded_and_clean():
    reader = PadReader("R", FakeJoyCon(), queue.Queue(), t0=time.monotonic())
    reader.start()
    t0 = time.monotonic()
    reader.stop()
    # Bounded join: stop returns well within the join bound, never hangs.
    assert time.monotonic() - t0 < 1.0
    assert not reader._thread.is_alive()


def test_backpressure_drops_oldest_without_blocking():
    # Tiny queue, no consumer -> the reader must drop, never block.
    q: "queue.Queue" = queue.Queue(maxsize=2)
    reader = PadReader("R", FakeJoyCon(), q, t0=time.monotonic())
    reader.start()
    time.sleep(0.1)
    reader.stop()
    assert reader.dropped > 0  # the read path kept moving despite a full queue


def test_reader_self_heals_after_sleep_gap():
    # Pad "asleep" (empty reads) for 0.2s, then data resumes. With a 0.05s stale
    # timeout, the first post-gap read triggers a re-init before trusting frames.
    fake = FakeJoyCon(sleep_until=time.monotonic() + 0.2)
    reader = PadReader("R", fake, queue.Queue(maxsize=64), t0=time.monotonic(),
                       stale_timeout_s=0.05)
    reader.start()
    time.sleep(0.5)
    reader.stop()
    assert reader.reinits >= 1
