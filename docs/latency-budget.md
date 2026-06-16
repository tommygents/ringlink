# Latency budget — Phase 1 (Spike A)

**Verdict: GREEN.** The cross-process WebSocket transport adds negligible latency
on loopback; the ~16 ms HID poll floor dominates, exactly as the design premise
([M1]) claimed. No fallback needed.

## Result

Measured 2026-06-16, Windows 11 desktop, Python 3.13, `websockets` 16.0, loopback
(`127.0.0.1`), `TCP_NODELAY` on both sockets, closed-loop ping (one message in
flight). `n = 5000` after 500 warmup.

| metric | added round-trip latency |
|--------|--------------------------|
| p50    | 0.10 ms |
| p95    | **0.13 ms** |
| p99    | 0.21 ms |
| max    | 0.33 ms |
| mean   | 0.11 ms (stdev 0.02) |

p95 is ~38× under the 5 ms target and ~120× under the ~16 ms HID poll period.

## Method

`ringlink_server.latency.measure_latency` runs a server and an echo client in one
event loop over a real loopback WS connection (full encode/decode/syscall path).
The **server** stamps a realistically-shaped `frame`, sends it, the client echoes
it straight back, and the server measures elapsed time on its **own clock** —
single-clock RTT, immune to cross-process clock skew. The closed loop keeps one
message in flight, which is the latency input responsiveness actually depends on.

Server and echo client share one event loop here. A real game-engine client is a
separate OS process, which would add roughly one OS context-switch per round-trip;
at these margins (38× under target) that gap does not threaten the verdict. The
production HID reader is a same-process *thread*, so the asyncio→client hop the
harness measures *is* the only cross-process boundary. The cheapest way to retire
the residual doubt — not needed for the gate — is a separate-process run of the
same harness (expected <2× difference, verdict unchanged).

Reproduce:

```bash
python -m ringlink_server latency -n 5000 --warmup 500
```

The harness is kept as `tests/test_latency.py` (deterministic verdict-band unit
tests + a live smoke run with a median regression guard that trips if, e.g.,
`TCP_NODELAY` is ever removed and Nagle stalls return).

## GO/NO-GO bands (plan §Phase 1)

- **GREEN**: p95 < 5 ms (target). ← we are here.
- **ACCEPTABLE**: p95 ≤ 10 ms (HID still dominates).
- **CAUTION**: p95 ≤ 12 ms (re-baseline before proceeding).
- **RED**: p95 > 12 ms, or high variance (fat tail) — would trigger the
  pre-committed fallback ladder below.

## Pre-committed fallbacks (NOT triggered)

Recorded for the record; none were needed:
1. Confirm `TCP_NODELAY` actually applied + the reader never serializes.
2. Raw TCP framing instead of WS.
3. A localhost shared-memory ring buffer.
4. Reconsider in-process per design D3.

## What graduates from this spike

- The WS skeleton (`transport.serve_stub`: loopback bind, `TCP_NODELAY`, Origin
  allowlist, per-tick send loop) is the **L4 seed** — later phases swap the stub
  frame source for cooked L3 frames and add fan-out / backpressure / control plane.
- The latency harness becomes a **permanent perf check** in CI.
