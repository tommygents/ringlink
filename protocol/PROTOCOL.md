# The ringlink protocol

> **Wire version: `1.0`** (advertised in `hello.protocol`) ·
> **Document revision: 0.1** (Phase 0 — lifted from the design spec).
>
> This is the **normative** contract. The reference server and every client
> validate against it. It is bumped whenever the vocabulary or wire shape changes
> (next at Phases 4 and 6 per the implementation plan). Where this doc and code
> disagree, this doc wins — fix the code.

`ringlink` is a loopback **WebSocket-JSON** protocol. One server owns the single
HID handle to the Ring Fit Joy-Cons, cooks raw strain/IMU into a normalized
vocabulary, and fans **continuous state + discrete events** out to any number of
clients. Clients are dumb consumers; all gesture semantics live server-side.

## Transport

- **Bind `127.0.0.1` only.** Not reachable off-box; no firewall prompt.
- **`TCP_NODELAY` is mandatory** on every socket. A 66 Hz stream of small JSON
  over loopback is the classic Nagle/delayed-ACK pathology (~40 ms stalls).
- **Origin check.** A loopback WS is reachable by any local process, including a
  browser tab on a hostile page. The server checks the WS `Origin` header against
  an allowlist (localhost + bundled clients).
- **Port discovery + singleton.** Fixed loopback port (default `28412`), also
  written to `%LOCALAPPDATA%/ring-server/endpoint.json` for clients that prefer
  discovery. A singleton lockfile guards against two servers fighting over the one
  HID handle: a client **connects first, spawns only if connect fails, and
  tolerates the spawn race with bounded connect-retry**.

## Core model: state + events per frame

Two natures of input, both first-class:

- **Continuous state** — values sampled "as of now" (`flex`, `lean`, `gait`, …).
- **Discrete events** — fire once and **must not be missed** (a squeeze edge, a
  squat rep, a pull).

Each server tick (**per HID report**, see Cadence) emits a `frame` carrying the
current `state` **and** every `event` since the previous tick. A consumer reading
at its own framerate drains all frames received since its last poll, **takes the
latest `state`, and concatenates the `events`**.

### Guarantee scope

The no-missed-edge guarantee holds **within a single connected WS session**. It
does **not** survive a dropped socket, client crash, or server restart — TCP only
guarantees delivery while the connection lives, and v1 keeps no replay buffer. On
reconnect a client re-`hello`s, treats any events during the gap as lost, and
re-syncs state from the first post-reconnect `frame`. (A `resume`-since-seq buffer
is a possible fast-follow, deliberately out of v1 on loopback.)

### Backpressure

Each client has a **bounded** outbound queue. When it fills (slow client: GC
pause, throttled background tab) the server **coalesces pending frames into one —
keeping the latest `state` but concatenating all their `events`**. Events are
**never** dropped by coalescing; stale state is never delivered. The reader thread
must never block on a slow client.

### Cadence

Frames are pushed **per HID report** — a hardware-observed ~66 Hz (the rate is the
hardware's, not a server timer), capped at `max_rate_hz`. `hello.max_rate_hz` is a
cap/hint, not a promise of fixed timing.

### Time base & epoch

- `t` = seconds since server start (`time.monotonic`-based). **Monotonic within a
  process; not wall-clock; not stable across restart.**
- `session_id` changes on every server (re)start. A reconnecting client that sees
  a new `session_id` knows a restart happened and **resets all derived state**
  (`t` math, counters).
- `seq` is a per-session monotonically increasing frame counter.

## Semantic vocabulary (normative)

Raw→cooked mappings are **normative** — one sign flip silently inverts every game.

| Field | Type | Meaning |
|-------|------|---------|
| `flex` | float −1..1 | Ring-Con axis. Raw strain `0x00` (fully pulled) → **−1**, `~0x0A` rest → **0**, `0x14` (fully pushed) → **+1**, calibrated to the per-grip rest captured at calibration. |
| `squeeze` | float 0..1 | Convenience = `max(0, flex)`. |
| `pull` | float 0..1 | Convenience = `max(0, -flex)`. |
| `lean.pitch` | float −1..1 | Forward/back tilt of the **right** pad (frame-1 accel), calibrated + axis-resolved. |
| `lean.roll` | float −1..1 | Left/right tilt of the right pad. |
| `gait` | enum | `rest` \| `run` \| `sprint` (**left/leg** pad). See derivation. |
| `squatting` | bool | Currently in a sustained squat (left pad). See derivation. |
| `squat_reps` | int | Completed-rep counter. **Monotonic within a server process**; survives client reconnect and recalibration; **resets on server restart**. |
| `buttons` | string[] | **Right-pad** buttons currently down. Left-pad buttons omitted in v1. |
| `stick` | [float, float] | Right-pad analog stick, −1..1 per axis. |

### `gait` / `squatting` derivation (load-bearing order)

The driver's `LegTracker.state` is **one** field with four mutually-exclusive
values — `rest`/`run`/`sprint`/`squat` — where the squat latch *wins* over the
gait gate (you cannot be "running" and "squatting" at once; the squat latch
suppresses the gyro-energy gate). The protocol splits this into two **independent
wire fields**, but they are **not** independent internally, so derive in exactly
this order:

1. `squatting` ← `LegTracker.state == "squat"` (the sustained-squat latch as a bool).
2. `gait` ← `LegTracker.state` when it is `rest`/`run`/`sprint`; **while
   `squatting` is true, `gait` emits `rest`** (never `run`/`sprint` — there is no
   live locomotion reading during a squat). `squat` is **not** a `gait` value.
3. `squat_reps` ← `LegTracker.squat_reps`.

### Nullable raw-field contract

At the raw layer, `strain` / `accel` / `gyro` can be `None` when a report is too
short. L3 must not assume their presence; cooked fields derived from a missing raw
input hold their last value (state) and emit no event.

## Events

Each event: `{ "type", "t", …payload }`.

| Event | Payload | Fires when |
|-------|---------|-----------|
| `squeeze` | `strength` 0..1 | flex crosses the squeeze onset (server-side refractory + peak-relative re-arm). |
| `pull` | `strength` 0..1 | flex crosses the pull onset. |
| `squat_rep` | — | a sustained squat completes (the `LegTracker` rep rule). |
| `gait_change` | `from`, `to` | a rest/run/sprint transition (also reflected in state). |

## Messages

### Server → client

```json
{ "type": "hello", "protocol": "1.0", "session_id": "a1b2c3",
  "pads": { "R": true, "L": false }, "calibration": "ready",
  "max_rate_hz": 66 }

{ "type": "frame", "seq": 12345, "t": 1623.45,
  "state": { "flex": 0.42, "squeeze": 0.42, "pull": 0.0,
             "lean": { "pitch": -0.10, "roll": 0.31 },
             "gait": "run", "squatting": false, "squat_reps": 3,
             "buttons": ["A"], "stick": [0.0, 0.0] },
  "events": [ { "type": "squeeze", "strength": 0.8, "t": 1623.44 } ] }

{ "type": "calibrating", "pad": "R", "pose": "rest", "step": "lean-forward", "pct": 60 }

{ "type": "status", "pads": { "R": "live", "L": "lost" } }

{ "type": "error", "code": "no_pad", "message": "No right Joy-Con found." }
```

- `status` is emitted when a pad's liveness changes. `lost` = the pad
  slept/disconnected; the server stops advancing that pad's state until the next
  good read, when it emits `live`. Clients should show a wake/reconnect affordance.

### Client → server (control plane)

```json
{ "type": "calibrate", "pad": "R", "pose": "rest", "seconds": 3 }
```

`subscribe` / stream-thinning is **not** in v1 (66 Hz of small JSON is cheap).

## Calibration (multi-client, global)

One HID handle, one `LegTracker`, one rest pose, fanned to N clients — so
calibration is a **global, shared mutation**:

- **Any client may request** `calibrate`; it is a **global server operation**.
- The server **broadcasts `calibrating` to *all* clients** and reports
  `calibration: "ready"` to everyone in subsequent frames.
- **Concurrent requests are serialized (queued) or rejected with an `error`** —
  never run two calibrations at once.
- `calibrate` carries a **`pad` selector**: `R` (flex + lean rest) and `L` (leg
  rest gravity) calibrate independently, on independent timelines.
- **Rest-drift auto-tracking** (a slow rest-EMA) runs *between* explicit
  calibrations. An explicit `calibrate` **snaps** the rest pose and **resets** the
  EMA baseline. `squat_reps` is **not** reset by recalibration.

### Axis resolution (directed gesture)

The right pad's IMU axes can't be inferred from a still rest sample — that
uncertainty is why the first build shipped runtime axis-guessing keys. v1 replaces
guessing with a directed-gesture calibration step:

1. **Hold still** → capture rest accel (the gravity vector).
2. Server prompts `calibrating.step: "lean-forward"` → user leans forward and
   returns. The server records which accel axis changed most and its sign → that
   becomes **`pitch`** (axis index + sign).
3. Server prompts `calibrating.step: "lean-right"` → same, resolving **`roll`**.

The resolved (axis-index, sign) pairs are what `lean.pitch` / `lean.roll` are
computed against thereafter. The leg pad needs only the still rest-gravity capture
(no directed step — `LegTracker` is orientation-agnostic via tilt-from-rest).

## Deferred (protocol-compatible fast-follows)

OSC projection (state only — events stay on WS) · raw signal channel (R&D
scaffolding, not the steady-state extension path) · ViGEm virtual-gamepad sink ·
native lib delivery · `resume`-since-seq replay buffer · code signing.
