"""Entry point for ``python -m ringlink_server`` / the ``ringlink-server`` script.

Subcommands: ``--version``; ``latency`` (Phase 1 transport GO/NO-GO harness);
``serve`` (the L4 server — cooked by default, ``--stub`` for the Phase 1/2 fake-data
demo); ``replay`` (Phase 5 hardware-free trace-replay through the cooked pipeline);
``stream`` (Phase 3 raw L1+L2 readout from a live pad); ``record`` (Phase 3 guided
canonical raw-trace capture).
"""

from __future__ import annotations

import argparse
import sys

from . import PROTOCOL_VERSION, __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ringlink-server",
        description="Reference server for the ringlink protocol.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ringlink-server {__version__} (protocol {PROTOCOL_VERSION})",
    )
    sub = parser.add_subparsers(dest="command")

    p_lat = sub.add_parser(
        "latency",
        help="measure added WS transport round-trip latency on loopback (Phase 1 spike)",
    )
    p_lat.add_argument(
        "-n", type=int, default=2000, help="measured samples (default 2000)"
    )
    p_lat.add_argument(
        "--warmup", type=int, default=200, help="warmup samples to discard (default 200)"
    )

    p_serve = sub.add_parser(
        "serve",
        help="run the L4 server (cooked, real pads by default; --stub for fake data)",
    )
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=28412)
    p_serve.add_argument("--rate", type=int, default=66, help="frame rate Hz (default 66)")
    p_serve.add_argument(
        "--stub",
        action="store_true",
        help="run the Phase 1/2 fake-data stub instead of the cooked server (no hardware)",
    )
    p_serve.add_argument(
        "--pad", choices=["R", "L", "both"], default="both",
        help="which pad(s) the cooked server brings up (default both)",
    )
    p_serve.add_argument(
        "--simulate-status",
        action="store_true",
        help="(stub only) emit a scripted pad_lost->live status transition",
    )

    p_replay = sub.add_parser(
        "replay",
        help="replay a canonical trace through the cooked pipeline (Phase 5; no hardware)",
    )
    p_replay.add_argument("trace", help="path to a Phase-3 trace JSONL (e.g. traces/right-pad.jsonl)")
    p_replay.add_argument(
        "--no-calibrate", action="store_true",
        help="skip driving calibration from the trace's directed segments",
    )
    p_replay.add_argument(
        "--show", type=int, default=0,
        help="also print the first N cooked frames as JSON (default 0)",
    )

    p_stream = sub.add_parser(
        "stream",
        help="L1+L2 raw readout from a live pad (Phase 3 verification; needs hardware)",
    )
    p_stream.add_argument("--pad", choices=["R", "L", "both"], default="both")
    p_stream.add_argument(
        "--seconds", type=float, default=0.0,
        help="auto-stop after N seconds (0 = until Ctrl-C)",
    )

    p_record = sub.add_parser(
        "record",
        help="guided canonical raw-trace capture for one pad (Phase 3; needs hardware)",
    )
    p_record.add_argument("--pad", choices=["R", "L"], required=True)
    p_record.add_argument(
        "--out", default="traces", help="output directory (default ./traces)"
    )
    p_record.add_argument("--name", default=None, help="trace basename (default right-pad/left-pad)")
    return parser


def _cmd_latency(args: argparse.Namespace) -> int:
    # Imported lazily so `--version` / help stay dependency-free and instant.
    from .latency import format_report, run

    result = run(n=args.n, warmup=args.warmup)
    print(format_report(result))
    # Non-zero exit on a red gate so CI / scripts can branch on it.
    return 0 if result.verdict in ("GREEN", "ACCEPTABLE") else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    import asyncio

    if args.stub:
        from .transport import serve_stub

        print(
            f"ringlink STUB server on ws://{args.host}:{args.port} "
            f"@ {args.rate} Hz (fake data; Ctrl-C to stop)"
        )
        try:
            asyncio.run(
                serve_stub(
                    host=args.host,
                    port=args.port,
                    rate_hz=args.rate,
                    simulate_status=args.simulate_status,
                )
            )
        except KeyboardInterrupt:
            pass
        return 0

    return _cmd_serve_cooked(args)


def _cmd_serve_cooked(args: argparse.Namespace) -> int:
    """The real L4 server: bring up the pad(s), cook live frames, fan out over WS.

    Composes the Phase-5 pieces verified hardware-free (`run_server` +
    `hub_frame_source`) onto the Phase-3 `RingHub`. Needs a physical Ring-Con to
    actually stream; on-hardware verification is a tracked follow-up (the fan-out,
    coalescing, and calibration control plane are CI-tested via injected sources)."""
    import asyncio

    from .lifecycle import AlreadyRunning, RingHub, acquire_singleton
    from .server import hub_frame_source, run_server

    sides = ("R", "L") if args.pad == "both" else (args.pad,)
    try:
        sing = acquire_singleton(args.host, args.port)
    except AlreadyRunning as exc:
        print(f"! {exc}", file=sys.stderr)
        return 1

    # Hold the app in a one-element box so the status callback (wired at hub
    # construction, before start()'s watchdog thread spins up) can reach it once the
    # event loop and server exist. This closes the startup-window status-drop gap.
    box: dict = {"app": None, "loop": None}

    def on_status(snap: dict) -> None:
        app, loop = box["app"], box["loop"]
        if app is not None and loop is not None:
            loop.call_soon_threadsafe(app.broadcast_status, snap)

    hub = RingHub(sides=sides, on_status=on_status)
    up = hub.start()
    live = [s for s, ok in up.items() if ok]
    if not live:
        print("! No requested pad came up. Press a button to wake the Joy-Con(s).",
              file=sys.stderr)
        hub.stop()
        sing.release()
        return 2

    pads = {s: (s in live) for s in ("R", "L")}
    print(f"ringlink server on ws://{args.host}:{args.port} — pads {live} (Ctrl-C to stop)")

    async def main() -> None:
        loop = asyncio.get_running_loop()
        # The WS server listens on the singleton lock socket itself (binding the
        # same port twice would fail) — lock and listener are one socket, so a
        # second instance's bind still fails and a probing client gets a real
        # WS handshake.
        async with run_server(
            hub_frame_source(hub), sock=sing.sock,
            rate_hz=args.rate, pads=pads,
        ) as app:
            # Publish app+loop so the already-wired on_status callback can broadcast.
            box["app"], box["loop"] = app, loop
            await loop.create_future()  # run until cancelled

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        sing.release()
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    import json as _json
    from collections import Counter

    from .pipeline import replay_trace

    frames = replay_trace(args.trace, calibrate=not args.no_calibrate)
    event_counts: Counter = Counter()
    for fr in frames:
        for e in fr["events"]:
            event_counts[e["type"]] += 1
    last = frames[-1]["state"] if frames else {}

    print(f"ringlink replay - {args.trace}")
    print(f"  calibrated : {not args.no_calibrate}")
    print(f"  frames     : {len(frames)}")
    print(f"  events     : {dict(event_counts) or '(none)'}")
    if last:
        print(f"  final gait : {last['gait']}  squat_reps={last['squat_reps']}")
    for fr in frames[: args.show]:
        print(_json.dumps(fr))
    return 0


def _cmd_stream(args: argparse.Namespace) -> int:
    import time

    from .lifecycle import AlreadyRunning, RingHub, acquire_singleton

    sides = ("R", "L") if args.pad == "both" else (args.pad,)
    try:
        sing = acquire_singleton()
    except AlreadyRunning as exc:
        print(f"! {exc}", file=sys.stderr)
        return 1

    def on_status(s: dict) -> None:
        print(f"  [status] {s}")

    hub = RingHub(sides=sides, on_status=on_status)
    try:
        up = hub.start()
        live = [s for s, ok in up.items() if ok]
        if not live:
            print("! No requested pad came up. Press a button to wake the Joy-Con(s).",
                  file=sys.stderr)
            return 2
        print(f"Streaming raw frames from {live} (Ctrl-C to stop).\n")
        deadline = time.monotonic() + args.seconds if args.seconds > 0 else None
        while deadline is None or time.monotonic() < deadline:
            try:
                side, frame, t = hub.frames.get(timeout=0.5)
            except Exception:
                continue
            s = frame["strain"]
            s_str = f"0x{s:02x}" if s is not None else " n/a"
            print(f"{side} t={t:7.2f} strain={s_str} accel={frame['accel']} gyro={frame['gyro']}")
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        hub.stop()
        sing.release()
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .record import record_trace

    record_trace(args.pad, Path(args.out), name=args.name)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "latency":
        return _cmd_latency(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "stream":
        return _cmd_stream(args)
    if args.command == "record":
        return _cmd_record(args)
    # No subcommand — print help so a bare invocation is self-documenting.
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
