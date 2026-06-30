# ringlink

Turn Nintendo **Ring Fit** hardware into a low-latency, **language-neutral** PC
input source. `ringlink` is a small protocol plus a reference server: games in
**any engine** ask for ring inputs and receive a calibrated, normalized
vocabulary — `flex` (squeeze/pull), `lean` (tilt), `gait` (run/sprint), `squat` —
with reliable discrete events. The gesture logic is solved **once, in the
server**; no game re-implements it.

> **Status: Phase 0 (scaffold).** Repo structure, the protocol contract, and CI
> are in place. The transport, lifecycle, semantic core, and WebSocket layers
> land in subsequent phases. See `protocol/PROTOCOL.md` for the wire contract.

## Why a protocol (not a virtual gamepad)

A standard controller flattens `flex`/`gait`/`squat` into buttons and axes and
kills ring-first design. `ringlink` keeps the inputs **ring-native** and lets a
client optionally project them onto an engine's conventional input idiom. The
artifact is the *protocol*: a future server rewrite (Rust, C, …) changes **zero**
clients.

## Architecture

```
game engines ── loopback WebSocket-JSON (state + events + calibration) ──┐
                                                                         ▼
                                  ringlink-server (Python, headless)
                                    L1  HID + MCU unpack -> raw frames
                                    L2  pad lifecycle, watchdog, singleton
                                    L3  calibration, axis-resolve, edges (the craft)
                                    L4  WS server: frames, events, commands
```

## Layout

| Path | What |
|------|------|
| `protocol/PROTOCOL.md` | The living wire contract. Server + every client validate against it. |
| `server/ringlink_server/` | The reference Python server (L1–L4). |
| `clients/pygame/` | Reference pygame client (Phase 6). |
| `clients/browser/` | Browser/JS client — also the Phase 2 transport spike. |
| `examples/` | The three ported games (`flappy`, `ski`, `doom_ring`) — the acceptance test. |
| `examples/doom_ring/doom/` | Vendored [`DOOM-style-game`](https://github.com/stanislavPetrovV/DOOM-style-game) submodule. |
| `traces/` | Canonical raw hardware traces (recorded Phase 3) for hardware-free CI. |
| `tests/` | Test suite (trace-replay + protocol conformance land in Phase 5). |

## Setup

```bash
git clone --recurse-submodules <url> && cd ringlink
python -m venv .venv && source .venv/Scripts/activate   # or .venv/bin/activate
pip install -e ".[dev]"            # hidapi (cython) + websockets + pytest

python -m ringlink_server --version
pytest -q                          # no hardware needed
```

### Try it now (no hardware)

```bash
python -m ringlink_server latency                        # Phase 1 transport spike
python -m ringlink_server replay traces/right-pad.jsonl   # Phase 5 cooked pipeline, no pad
python -m ringlink_server serve --stub --simulate-status  # stub on ws://127.0.0.1:28412
python -m http.server 8000 --directory clients/browser   # then open http://localhost:8000
```

`serve` (no `--stub`) runs the real cooked server against live pads. See
`docs/latency-budget.md` (Phase 1) and `docs/spike-b-browser-client.md` (Phase 2).

**HID binding is cython `hidapi`, never apmorton `hid`** — they collide on the
`hid` import name. `pip install hidapi` ships prebuilt wheels (bundled native
lib), so no compiler / brew / manual DLL.

## Hardware

Two Switch-1 Joy-Cons over **Bluetooth-Classic HID** (not BLE): the **right** pad
seats in the Ring-Con (strain gauge + IMU); the **left** pad straps to the thigh
(IMU only — run-in-place + squats). Pair them in your OS Bluetooth settings; they
sleep when idle (press a button to wake).

## License

MIT.
