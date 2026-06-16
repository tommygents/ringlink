"""Phase 0 smoke test.

Exists so CI is green from day 1 (implementation plan, Phase 0) — a home for
the Phase 5 trace-replay + conformance tests to land in, not a retrofit. Keep
it dependency-free (no hidapi, no hardware).
"""

from ringlink_server import PROTOCOL_VERSION, __version__


def test_version_present():
    assert isinstance(__version__, str) and __version__


def test_protocol_version_pinned():
    # The wire version the server advertises in `hello`. Changing this is a
    # protocol break — update PROTOCOL.md and this assertion together.
    assert PROTOCOL_VERSION == "1.0"


def test_entry_point_runs():
    # `python -m ringlink_server --version` must succeed (Phase 0 criterion).
    # argparse's version action raises SystemExit(0); assert that contract.
    from ringlink_server.__main__ import main

    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
