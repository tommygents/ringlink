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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "latency":
        return _cmd_latency(args)
    if args.command == "serve":
        return _cmd_serve(args)
    # No subcommand — print help so a bare invocation is self-documenting.
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
