"""L1 regression tests for ``hid_driver.parse_report``.

The fixtures in ``fixtures/raw_report_samples.json`` are **real** right-pad
Ring-Con input reports captured from live hardware (2026-06-30), with the expected
decode produced by the proven ``recurse-ringcon-hacking/monitor.py`` decoder. If a
refactor drifts the byte offsets — most dangerously the frame-1 IMU coupling — the
captured-vs-decoded comparison fails. No hardware needed to run these.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ringlink_server.hid_driver import (
    JOYCON_L_PID,
    JOYCON_R_PID,
    NINTENDO_VID,
    parse_report,
)

FIXTURE = Path(__file__).parent / "fixtures" / "raw_report_samples.json"


def _load_samples():
    data = json.loads(FIXTURE.read_text())
    return data["samples"]


def test_constants():
    assert NINTENDO_VID == 0x057E
    assert JOYCON_R_PID == 0x2007
    assert JOYCON_L_PID == 0x2006


@pytest.mark.parametrize("sample", _load_samples())
def test_parse_matches_captured_decode(sample):
    """Each live buffer decodes to exactly what the proven monitor.py produced.

    The fixture was captured before the L1 lift, so a passing assertion proves the
    lifted decoder is byte-for-byte equivalent — including the frame-1 accel/gyro
    offsets (25/31) that the Ring-Con-polling corruption forces.
    """
    buf = bytes.fromhex(sample["hex"])
    got = parse_report(buf)
    assert got is not None
    exp = sample["expected"]
    # Fixture used monitor's `right_stick`; the raw L1 contract renames it `stick`.
    assert list(got["buttons"]) == exp["buttons"]
    assert list(got["stick"]) == exp["right_stick"]
    assert got["strain"] == exp["strain"]
    assert list(got["accel"]) == exp["accel"]
    assert list(got["gyro"]) == exp["gyro"]


def test_non_input_report_returns_none():
    # A subcommand-reply report (0x21), not a full input report.
    assert parse_report(bytes([0x21] + [0] * 48)) is None
    assert parse_report(b"") is None


def test_nullable_contract_on_short_report():
    """Per the protocol's nullable raw-field contract: a too-short report yields
    None for strain/accel/gyro while still decoding buttons/stick. L3 must not
    assume presence."""
    # 30 bytes: enough for buttons (3-5) and stick (9-11), but < 31 (accel), < 37
    # (gyro), and < 41 (strain at offset 40).
    short = bytes([0x30] + [0] * 29)
    rep = parse_report(short)
    assert rep is not None
    assert rep["strain"] is None
    assert rep["accel"] is None
    assert rep["gyro"] is None
    assert rep["buttons"] == (0, 0, 0)
    assert rep["stick"] == (0, 0)


def test_accel_only_when_gyro_absent():
    """A report long enough for frame-1 accel (>=31) but not gyro (>=37): accel
    present, gyro None — the two unpack at distinct length thresholds."""
    buf = bytes([0x30] + [0] * 33)  # len 34: >=31 accel, <37 gyro
    rep = parse_report(buf)
    assert rep["accel"] == (0, 0, 0)
    assert rep["gyro"] is None
