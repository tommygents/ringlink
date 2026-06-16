# Spike B — browser/JS client vs. the stub (Phase 2)

**Verdict: GREEN.** A non-Python client consumes the full ringlink JSON contract
over a real WebSocket — no shared types, no hardware — and the client logic is
small with **no per-message special-casing**. Completes **Checkpoint A** (both
de-risk spikes green) before the real server.

## What was validated

The reference client `clients/browser/ringlink-client.js` runs unmodified in a
browser and in Node 22+ (both expose a global `WebSocket`). Rendering is injected
via callbacks, so the *same core* powers `index.html` (DOM) and the headless
`contract-check.js` (assertions). The contract check (2026-06-16) passed 7/7:

| check | result |
|-------|--------|
| `hello` carries a `session_id` | PASS |
| drains `frame`s and `flex` actually varies | PASS (82 frames, flex span 1.79) |
| `status` reports `pad_lost` then `live` | PASS |
| `calibrate` round-trip drives lean-forward + lean-right + done | PASS |
| reconnects and observes a **new** `session_id` after server restart | PASS |
| `frame`s resume after restart | PASS |
| client logic well under ~50 lines (excl. comments/HTML/braces) | PASS (**45 lines**) |

The qualitative kill-signal — "did the JSON contract force ugly client code?" —
is clearly **no**: the whole protocol is one `switch` with no per-message
special-casing beyond the contract itself (a `session_id` change drops derived
state per [C1]/[C2]).

## How to run

Headless contract check (Node 22+, spawns the stub itself):

```bash
RINGLINK_PYTHON=/path/to/venv/python node clients/browser/contract-check.js
```

Live browser demo (two terminals):

```bash
python -m ringlink_server serve --simulate-status        # stub on ws://127.0.0.1:28412
python -m http.server 8000 --directory clients/browser   # serve index.html
# open http://localhost:8000
```

## Friction noted

- **undici's `WebSocket` (Node) fires `error` but not `close` on a failed initial
  connect.** A close-only reconnect stalls when the server isn't up yet. Fixed in
  the client: reconnect is driven by *both* `error` and `close`, idempotent per
  socket — which is also correct for browsers (they emit error+close on a drop).
  This is the kind of real cross-environment wrinkle the spike existed to surface.
- **Origin allowlist is exact-string.** A browser serving `index.html` sends a
  localhost `Origin`; the loopback-only server rejects any Origin not in
  `ALLOWED_ORIGINS`. The conventional `http.server` ports (`:8000`) are allowlisted
  now; **Phase 5 should generalize to an any-localhost-port predicate** so the demo
  isn't pinned to port 8000.
- **ES module imports need http, not file://.** Some browsers block module imports
  from `file://`; serve the client over `http.server` (above).

## What graduates from this spike

`ringlink-client.js` + `index.html` are the **v1 reference browser client**
(carried forward, not rewritten). `contract-check.js` is a reusable JSON-contract
conformance harness for any future client. The stub's `calibrate`/`status`
handling seeds the real L4 control plane (Phase 5).
