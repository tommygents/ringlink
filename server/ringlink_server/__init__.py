"""ringlink reference server.

The reference implementation of the *ringlink protocol*: it owns the single
HID handle to the Nintendo Ring Fit Joy-Cons, cooks raw strain/IMU into the
normalized ring vocabulary (``flex`` / ``lean`` / ``gait`` / ``squat``), and
fans calibrated state + reliable discrete events out to any number of clients
over loopback WebSocket-JSON.

Layering (see ``protocol/PROTOCOL.md`` and the design spec):

* **L1** transport + unpack — JoyCon HID + MCU protocol -> raw frames.
* **L2** lifecycle — pad discovery, reader threads, shutdown, watchdog, singleton.
* **L3** semantic core — calibration, axis resolution, edge detection (the craft).
* **L4** WS server — frames, events, commands, backpressure coalescing.

Phase 0 is scaffold only; the layers land in later phases per the plan.
"""

__version__ = "0.0.1"

# Wire-level protocol version advertised in the ``hello`` message. The contract
# itself is ``protocol/PROTOCOL.md``; bump both together when the wire changes.
PROTOCOL_VERSION = "1.0"

__all__ = ["__version__", "PROTOCOL_VERSION"]
