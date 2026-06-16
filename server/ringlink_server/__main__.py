"""Entry point for ``python -m ringlink_server`` / the ``ringlink-server`` script.

Phase 0 scaffold: only ``--version`` is wired up so the repo is verifiably
runnable and CI is green from day 1. The transport/lifecycle/semantic/WS layers
arrive in later phases per ``2026-06-15 ringlink — implementation plan``.
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    # No run subcommand yet — Phase 0 is scaffold only. Print help so an
    # accidental bare invocation is self-documenting rather than silent.
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
