# CLAUDE.md — ringlink

Guidance for Claude Code (or any contributor) working in this repo.

## What this is

A language-neutral input **protocol** for Nintendo Ring Fit hardware, plus a
reference Python **server** and thin per-engine **clients**. Games declare they
want ring inputs and receive a calibrated, normalized vocabulary (`flex`, `lean`,
`gait`, `squat`) with reliable discrete events. The semantic work happens **once,
server-side**; clients are dumb consumers.

This repo is the from-scratch successor to `recurse-ringcon-hacking/` (the first
build): its `monitor.py` driver is the proven L1 to lift, and its three games
(`flappy`, `ski`, `doom_ring`) move in under `examples/` as the acceptance test.

## Source-of-truth docs (in the Obsidian vault, project 11.01)

- **Design spec:** `2026-06-15 Ring Protocol — design` — *why* the protocol is
  shaped this way (decisions D1–D12, findings `[Cn]`/`[Mn]`/`[Nn]`/`[Gn]`).
- **Implementation plan:** `2026-06-15 ringlink — implementation plan` — the
  phase sequence (0–7) this repo is built along, with success criteria.
- **`protocol/PROTOCOL.md`** — the *normative* wire contract. Server and every
  client validate against it; bump it whenever the vocabulary changes (Phases 4, 6).

Read the spec before changing semantics; read PROTOCOL.md before changing the wire.

## Layer split (matters — it mirrors the real coupling)

- **L1** = `JoyCon` (HID + MCU init) + `parse_report` -> **raw**
  `{strain, accel, gyro, buttons, stick}`. Pure transport + byte-unpacking, **no
  cooking**. Lifted from `monitor.py`.
- **L2** = pad discovery, reader threads, clean shutdown, staleness watchdog,
  singleton lockfile + endpoint file.
- **L3** = `LegTracker` + flex/lean calibration + directed-gesture axis
  resolution + edge detection -> the semantic vocabulary. **The craft.**
- **L4** = WS server: frames, events, calibration commands, backpressure
  coalescing, multi-client fan-out.

## Conventions / gotchas (read before editing)

- **Preserve the frame-1 IMU coupling.** `parse_report` reads accel/gyro from IMU
  **frame 1** (not frame 2) because enabling Ring-Con strain polling corrupts
  frame 2 (`monitor.py:276–288`). The right pad's `lean` and `flex` are
  co-available *only* because of this. Do not "clean it up" — it will reintroduce
  the corruption. Keep the explaining comment when you lift it.
- **Raw fields are nullable.** `strain`/`accel`/`gyro` can be `None` when a report
  is too short (`monitor.py:274–288`). L3 must not assume presence.
- **The reader thread never serializes.** It hands raw frames to L4 via a queue;
  JSON encoding + asyncio fan-out happen off the read path so a slow client can't
  stall the HID reader. This is load-bearing for the latency budget.
- **HID binding is cython `hidapi`, never apmorton `hid`.** They collide on the
  `hid` import name. `pip install hidapi` ships prebuilt wheels (bundled native
  lib). If you see "can't load hidapi", someone installed the wrong package.
- **Stop reader threads before exit** (`stop()` + bounded `join(timeout=0.5)`).
  Tearing down the interpreter mid-`read()` segfaults (exit 139). Keep the bound
  so a hung HID read can't block exit.
- **The vocabulary is the oracle.** If a ported game needs to reach *under* the
  vocabulary, fix the **protocol** (add a cooked gesture in L3) and bump
  PROTOCOL.md — don't patch the game.

## The doom submodule

`examples/doom_ring/doom/` is a pinned git **submodule**
([`stanislavPetrovV/DOOM-style-game`](https://github.com/stanislavPetrovV/DOOM-style-game)).

- **Never edit files inside it.** Edits won't travel and dirty the submodule.
  `doom_ring` customizes the game entirely by **runtime patching** (`settings`,
  `Player`, `Weapon`, per-instance overrides). Extend that pattern.
- Clone with `--recurse-submodules`; CI checks it out recursively.

## Setup & run

```bash
python -m venv .venv && source .venv/Scripts/activate   # or .venv/bin/activate
pip install -e ".[dev]"
python -m ringlink_server --version
pytest -q                          # no hardware needed
```
