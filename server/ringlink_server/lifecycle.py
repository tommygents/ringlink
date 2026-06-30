"""L2 — lifecycle: pad discovery, reader threads, shutdown, watchdog, singleton.

Wraps L1 (``hid_driver``) in the runtime machinery a long-lived server needs:

* **Discovery + init** — find the requested pad(s), bring each up (right pad: full
  Ring-Con MCU init; left pad: standard input + IMU).
* **Reader threads** — one per pad. The reader **never serializes**: it parses raw
  reports and hands the raw dict to a queue. JSON/asyncio fan-out (L4) happens off
  this path so a slow client can't stall the HID read (load-bearing for latency).
* **Self-healing** — if a read arrives after a >``stale_timeout`` gap, the pad was
  asleep and its MCU may have reset; the reader re-inits (idempotent) before
  trusting frames again. This is what lets the server "survive pad-sleep".
* **Watchdog** — observes each reader's last-read time and emits ``pad_lost`` /
  ``live`` status transitions. Observability only; recovery is the reader's job.
* **Bounded shutdown** — ``stop()`` signals + ``join(0.5)``. The bound matters: a
  hung HID ``read()`` must not block process exit, and tearing the interpreter down
  mid-``read()`` segfaults (exit 139). See ``protocol/PROTOCOL.md`` and the project
  gotcha ``gotcha_joycon_hid_from_python``.
* **Singleton** — the fixed loopback port, bound as a lock (no ``SO_REUSEADDR`` →
  a second instance's ``bind`` fails), plus ``endpoint.json`` for client discovery.

L2 is still **raw** — no cooking. Cooked vocabulary (``flex``/``lean``/``gait``)
is L3 (Phase 4); the WS fan-out is L4 (Phase 5).
"""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from .hid_driver import JoyCon, find_joycon, parse_report

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 28412

READ_TIMEOUT_MS = 200          # per-read HID timeout; keeps the reader responsive to stop()
DEFAULT_STALE_TIMEOUT_S = 1.0  # no good frame for this long -> pad considered lost / asleep
JOIN_TIMEOUT_S = 0.5           # bounded reader-thread join (a hung read can't block exit)
DEFAULT_QUEUE_MAXSIZE = 256    # large enough that disk/IO consumers never drop in practice

# A raw frame as it travels the queue: (side, raw_dict, t) where t is monotonic
# seconds since the hub started.
Frame = "tuple[str, dict, float]"


# --------------------------------------------------------------------------- #
# Singleton + endpoint discovery
# --------------------------------------------------------------------------- #

class AlreadyRunning(RuntimeError):
    """Raised when another ringlink server already holds the singleton port."""


def endpoint_path() -> Path:
    """``%LOCALAPPDATA%/ring-server/endpoint.json`` (XDG-ish fallback off-Windows)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(base) / "ring-server" / "endpoint.json"


class Singleton:
    """Holds the bound singleton socket + the discovery file; release on exit."""

    def __init__(self, sock: socket.socket, host: str, port: int, path: Path):
        self._sock = sock
        self.host = host
        self.port = port
        self.path = path

    def release(self) -> None:
        try:
            self._sock.close()
        finally:
            try:
                self.path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "Singleton":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire_singleton(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> Singleton:
    """Bind the fixed loopback port as a lock and write ``endpoint.json``.

    Deliberately omits ``SO_REUSEADDR``: we *want* the bind to fail if another
    instance is up, so the OS enforces one-holder-of-the-HID-handle. Phase 5's L4
    WS server binds this same port — lock and listener are the same socket then.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise AlreadyRunning(
            f"ringlink server already running on {host}:{port} ({exc})"
        ) from exc
    # Phase 3: this socket is a pure lock — we listen() so a second bind fails, but
    # never accept(). PHASE 5 NOTE: a client probing the port gets a successful TCP
    # connect (the backlog accepts it) with no WS handshake, so a bare connect-probe
    # can't distinguish "WS server up" from "lock held by stream/record". The L4 WS
    # server must BE this accepting socket, or the client's probe must complete a
    # real handshake before deciding not to spawn.
    sock.listen(1)
    path = endpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # On a hard kill, release() never runs and this file is left stale. It self-heals
    # on the next start (the port frees, bind wins, the file is overwritten), and a
    # client reading a stale endpoint just fails to connect -> spawns. `pid` is
    # recorded for a future staleness check (unused in Phase 3).
    path.write_text(json.dumps({"host": host, "port": port, "pid": os.getpid()}))
    return Singleton(sock, host, port, path)


# --------------------------------------------------------------------------- #
# Per-pad reader thread
# --------------------------------------------------------------------------- #

class PadReader:
    """Reads one Joy-Con on a daemon thread; emits raw frames to a shared queue.

    Self-heals across pad sleep: a read after a long gap triggers a re-init before
    frames are trusted again. Tracks ``last_read`` (monotonic) for the watchdog.
    """

    def __init__(
        self,
        side: str,
        jc: JoyCon,
        frames: "queue.Queue",
        t0: float,
        stale_timeout_s: float = DEFAULT_STALE_TIMEOUT_S,
    ):
        self.side = side
        self.jc = jc
        self.frames = frames
        self._t0 = t0
        self.stale_timeout_s = stale_timeout_s
        self._stop = threading.Event()
        # Let a long re-init abort promptly on shutdown — without this, stop()'s
        # bounded join would abandon a thread still mid-init and RingHub.stop could
        # close the handle out from under it (-> exit 139).
        jc.abort = self._stop
        self._thread = threading.Thread(target=self._run, name=f"pad-{side}", daemon=True)
        # last_read is written by the reader thread and read by the watchdog thread
        # WITHOUT a lock: it relies on CPython's atomic attribute store/load, NOT on
        # RingHub._lock (which only guards _status). Revisit for a free-threaded build.
        self.last_read = time.monotonic()
        self.dropped = 0
        self.reinits = 0
        self._consec_reinit_fail = 0
        self._heal_failed = False  # gave up healing a dead/re-enumerated handle

    def start(self) -> None:
        self.last_read = time.monotonic()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Bounded join: a hung HID read must not block exit. The thread is a daemon,
        # so abandoning it on timeout is safe (process is tearing down anyway).
        self._thread.join(timeout=JOIN_TIMEOUT_S)

    def _reinit(self) -> bool:
        # init is idempotent; right pad needs the full MCU/Ring-Con bring-up (MCU
        # state resets on sleep), left pad just standard input + IMU. Returns whether
        # the handle actually came back: a fully-dropped (re-enumerated) node fails
        # every subcommand -> False. Re-initing the SAME handle cannot recover a new
        # device path (hub-level re-discovery is the real fix; tracked follow-up), so
        # only a genuine heal counts.
        try:
            ok = self.jc.init_ringcon() if self.side == "R" else self.jc.init_imu_only()
        except OSError:
            return False
        if ok:
            self.reinits += 1
        return bool(ok)

    def _emit(self, frame: dict, t: float) -> None:
        item = (self.side, frame, t)
        try:
            self.frames.put_nowait(item)
        except queue.Full:
            # The reader MUST NOT block on a slow consumer. Drop the oldest raw
            # frame, keep the latest. (Phase 5's L4 does proper event-preserving
            # coalescing; here we only guarantee the HID read path never stalls.)
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self.frames.put_nowait(item)
            except queue.Full:
                pass
            self.dropped += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            buf = self.jc.read(timeout_ms=READ_TIMEOUT_MS)
            now = time.monotonic()
            if not buf:
                continue  # timeout / BT stutter / asleep — watchdog tracks staleness
            if now - self.last_read > self.stale_timeout_s and not self._heal_failed:
                # Data after a long gap: the pad was asleep and may have reset.
                # Try to heal (re-init), then skip this (possibly stale) buffer.
                if self._reinit():
                    self._consec_reinit_fail = 0
                else:
                    self._consec_reinit_fail += 1
                    if self._consec_reinit_fail >= 2:
                        # The handle is dead (node fully dropped / re-enumerated).
                        # Stop hammering it; stay 'lost' until real data or exit.
                        self._heal_failed = True
                self.last_read = time.monotonic()
                continue
            frame = parse_report(buf)
            if frame is None:
                continue
            # Genuine data: the handle is healthy again.
            self._heal_failed = False
            self._consec_reinit_fail = 0
            self.last_read = now
            self._emit(frame, now - self._t0)


# --------------------------------------------------------------------------- #
# Hub: discovery, readers, watchdog
# --------------------------------------------------------------------------- #

class RingHub:
    """Owns the pad readers + a staleness watchdog, exposing one raw-frame queue.

    Usage::

        with RingHub(sides=("R", "L")) as hub:
            up = hub.start()                 # {"R": True, "L": True}
            side, frame, t = hub.frames.get()
            ...                              # hub.stop() on exit
    """

    def __init__(
        self,
        sides: tuple[str, ...] = ("R", "L"),
        on_status: Callable[[dict], None] | None = None,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        stale_timeout_s: float = DEFAULT_STALE_TIMEOUT_S,
    ):
        self.sides = sides
        self.on_status = on_status
        self.stale_timeout_s = stale_timeout_s
        self.frames: "queue.Queue" = queue.Queue(maxsize=queue_maxsize)
        self._readers: dict[str, PadReader] = {}
        self._status: dict[str, str] = {}   # side -> "live" | "lost" (present pads only)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, name="watchdog", daemon=True)
        self._t0 = time.monotonic()
        self._started = False

    def start(self) -> dict[str, bool]:
        """Discover + bring up each requested pad; start readers + watchdog.

        Returns ``{side: came_up}``. A requested pad that is absent or fails init
        is simply omitted from the readers (``came_up == False``).
        """
        up: dict[str, bool] = {}
        for side in self.sides:
            info = find_joycon(side)
            if not info:
                up[side] = False
                continue
            jc = JoyCon(info["path"])
            ok = jc.init_ringcon() if side == "R" else jc.init_imu_only()
            if not ok:
                jc.close()
                up[side] = False
                continue
            reader = PadReader(side, jc, self.frames, self._t0, self.stale_timeout_s)
            self._readers[side] = reader
            self._status[side] = "live"
            up[side] = True
        for reader in self._readers.values():
            reader.start()
        self._watchdog.start()
        self._started = True
        return up

    def status(self) -> dict[str, str]:
        with self._lock:
            return dict(self._status)

    def _watch(self) -> None:
        # Poll each reader's last-read time; emit live/lost transitions. Pure
        # observability — the reader self-heals; the watchdog only reports.
        while not self._stop.is_set():
            self._stop.wait(0.2)
            now = time.monotonic()
            changed = False
            with self._lock:
                for side, reader in self._readers.items():
                    live = (now - reader.last_read) <= self.stale_timeout_s
                    new = "live" if live else "lost"
                    if self._status.get(side) != new:
                        self._status[side] = new
                        changed = True
                snapshot = dict(self._status)
            if changed and self.on_status is not None:
                self.on_status(snapshot)

    def stop(self) -> None:
        """Idempotent: stop watchdog + readers (bounded joins), close HID handles.

        Each reader's `abort` Event (= its `_stop`) lets a long re-init bail
        promptly, so threads normally finish within the join bound. If one is STILL
        alive after the bound (stuck in a native HID call), we deliberately do NOT
        close its handle: closing a handle a live thread is mid-read/write on is
        undefined behavior and segfaults (exit 139). Leak it — the daemon thread is
        abandoned and the OS reclaims the handle at process exit.
        """
        if not self._started:
            return
        self._stop.set()
        self._watchdog.join(timeout=JOIN_TIMEOUT_S)
        for reader in self._readers.values():
            reader.stop()
        for reader in self._readers.values():
            if reader._thread.is_alive():
                continue  # stuck in a native HID call; closing would race it -> 139
            try:
                reader.jc.close()
            except OSError:
                pass
        self._readers.clear()
        self._started = False

    def __enter__(self) -> "RingHub":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_STALE_TIMEOUT_S",
    "AlreadyRunning",
    "Singleton",
    "acquire_singleton",
    "endpoint_path",
    "PadReader",
    "RingHub",
]
