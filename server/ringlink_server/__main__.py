"""Entry point for ``python -m ringlink_server`` / the ``ringlink-server`` script.

Phase 0 wired ``--version``. Phase 1 adds the ``latency`` subcommand — the
de-risk-transport GO/NO-GO harness. The real ``serve`` command (L1–L4) arrives in
later phases per ``2026-06-15 ringlink — implementation plan``.
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
        help="run the stub L4 server (Phase 1/2 stub: hello + frames + calibrate)",
    )
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=28412)
    p_serve.add_argument("--rate", type=int, default=66, help="frame rate Hz (default 66)")
    p_serve.add_argument(
        "--simulate-status",
        action="store_true",
        help="emit a scripted pad_lost->live status transition (spike demo)",
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

    from .transport import serve_stub

    print(
        f"ringlink stub server on ws://{args.host}:{args.port} "
        f"@ {args.rate} Hz (Ctrl-C to stop)"
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
    if args.command == "stream":
        return _cmd_stream(args)
    if args.command == "record":
        return _cmd_record(args)
    # No subcommand — print help so a bare invocation is self-documenting.
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
