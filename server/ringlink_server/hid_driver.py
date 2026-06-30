"""L1 — HID transport + byte-unpacking. Joy-Con MCU protocol -> raw frames.

This is the proven driver lifted from ``recurse-ringcon-hacking/monitor.py`` (the
first build), itself ported from `ringrunnermg/Ringcon-Driver` (joycon.hpp). The
Ring-Con only appears on subcommand ``0x59`` after the Joy-Con's MCU has been
powered on and switched into "Ringcon" mode — that requires CRC-checked output
reports, not the plain subcommand wrapper the rest of the protocol uses.

**L1 is pure transport + unpacking — no cooking.** ``parse_report`` returns the
*raw* nullable vocabulary ``{strain, accel, gyro, buttons, stick}``; turning that
into ``flex`` / ``lean`` / ``gait`` (calibration, axis resolution, edge detection)
is L3 (Phase 4). Keep this layer hardware-shaped and decision-free.

See ``protocol/PROTOCOL.md`` §"Nullable raw-field contract" and the project gotcha
``gotcha_joycon_hid_from_python`` (transport + binding + Windows DLL traps).
"""

from __future__ import annotations

import struct
import threading
import time

import hid

NINTENDO_VID = 0x057E
JOYCON_R_PID = 0x2007
JOYCON_L_PID = 0x2006

# Nintendo's MCU CRC-8 (poly 0x07) lookup table. Lifted verbatim from
# Ringcon-Driver's tools.hpp. Used for MCU "Set Configuration" (0x21) writes.
MCU_CRC8_TABLE = [
    0x00, 0x07, 0x0E, 0x09, 0x1C, 0x1B, 0x12, 0x15, 0x38, 0x3F, 0x36, 0x31, 0x24, 0x23, 0x2A, 0x2D,
    0x70, 0x77, 0x7E, 0x79, 0x6C, 0x6B, 0x62, 0x65, 0x48, 0x4F, 0x46, 0x41, 0x54, 0x53, 0x5A, 0x5D,
    0xE0, 0xE7, 0xEE, 0xE9, 0xFC, 0xFB, 0xF2, 0xF5, 0xD8, 0xDF, 0xD6, 0xD1, 0xC4, 0xC3, 0xCA, 0xCD,
    0x90, 0x97, 0x9E, 0x99, 0x8C, 0x8B, 0x82, 0x85, 0xA8, 0xAF, 0xA6, 0xA1, 0xB4, 0xB3, 0xBA, 0xBD,
    0xC7, 0xC0, 0xC9, 0xCE, 0xDB, 0xDC, 0xD5, 0xD2, 0xFF, 0xF8, 0xF1, 0xF6, 0xE3, 0xE4, 0xED, 0xEA,
    0xB7, 0xB0, 0xB9, 0xBE, 0xAB, 0xAC, 0xA5, 0xA2, 0x8F, 0x88, 0x81, 0x86, 0x93, 0x94, 0x9D, 0x9A,
    0x27, 0x20, 0x29, 0x2E, 0x3B, 0x3C, 0x35, 0x32, 0x1F, 0x18, 0x11, 0x16, 0x03, 0x04, 0x0D, 0x0A,
    0x57, 0x50, 0x59, 0x5E, 0x4B, 0x4C, 0x45, 0x42, 0x6F, 0x68, 0x61, 0x66, 0x73, 0x74, 0x7D, 0x7A,
    0x89, 0x8E, 0x87, 0x80, 0x95, 0x92, 0x9B, 0x9C, 0xB1, 0xB6, 0xBF, 0xB8, 0xAD, 0xAA, 0xA3, 0xA4,
    0xF9, 0xFE, 0xF7, 0xF0, 0xE5, 0xE2, 0xEB, 0xEC, 0xC1, 0xC6, 0xCF, 0xC8, 0xDD, 0xDA, 0xD3, 0xD4,
    0x69, 0x6E, 0x67, 0x60, 0x75, 0x72, 0x7B, 0x7C, 0x51, 0x56, 0x5F, 0x58, 0x4D, 0x4A, 0x43, 0x44,
    0x19, 0x1E, 0x17, 0x10, 0x05, 0x02, 0x0B, 0x0C, 0x21, 0x26, 0x2F, 0x28, 0x3D, 0x3A, 0x33, 0x34,
    0x4E, 0x49, 0x40, 0x47, 0x52, 0x55, 0x5C, 0x5B, 0x76, 0x71, 0x78, 0x7F, 0x6A, 0x6D, 0x64, 0x63,
    0x3E, 0x39, 0x30, 0x37, 0x22, 0x25, 0x2C, 0x2B, 0x06, 0x01, 0x08, 0x0F, 0x1A, 0x1D, 0x14, 0x13,
    0xAE, 0xA9, 0xA0, 0xA7, 0xB2, 0xB5, 0xBC, 0xBB, 0x96, 0x91, 0x98, 0x9F, 0x8A, 0x8D, 0x84, 0x83,
    0xDE, 0xD9, 0xD0, 0xD7, 0xC2, 0xC5, 0xCC, 0xCB, 0xE6, 0xE1, 0xE8, 0xEF, 0xFA, 0xFD, 0xF4, 0xF3,
]


def mcu_crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = MCU_CRC8_TABLE[crc ^ b]
    return crc


def find_joycons() -> list[dict]:
    """Every connected Joy-Con (right and/or left)."""
    return [d for d in hid.enumerate(NINTENDO_VID, 0)
            if d["product_id"] in (JOYCON_R_PID, JOYCON_L_PID)]


def find_joycon(side: str | None = None) -> dict | None:
    """First Joy-Con, optionally restricted to a side ('R' or 'L').

    With two Joy-Cons connected (right = Ring-Con, left = leg strap), pass side=
    to target one deterministically instead of grabbing whichever happens to
    enumerate first.
    """
    want = {"R": JOYCON_R_PID, "L": JOYCON_L_PID}.get(side) if side is not None else None
    for dev in find_joycons():
        if want is None or dev["product_id"] == want:
            return dev
    return None


class JoyCon:
    OUTPUT_LEN = 49

    def __init__(self, path):
        # cython-hidapi: construct, then open by the bytes path from
        # hid.enumerate() (apmorton's hid.Device(path=...) constructor is gone).
        self.dev = hid.device()
        self.dev.open_path(path)
        self._count = 0
        # Optional threading.Event; when set, `_send_and_wait` (and therefore a full
        # init) bails promptly so a reader thread can abort a long re-init at
        # shutdown instead of blocking past the join bound (see lifecycle.RingHub.stop).
        self.abort: threading.Event | None = None

    def close(self):
        self.dev.close()

    def _next(self):
        c = self._count & 0xF
        self._count += 1
        return c

    def _write(self, buf):
        # cython-hidapi write() takes a list of ints (report id is byte 0).
        self.dev.write(list(bytes(buf).ljust(self.OUTPUT_LEN, b"\x00")))

    def read(self, timeout_ms: int = 200) -> bytes:
        # cython-hidapi returns a list of ints (empty list on timeout) and raises
        # OSError on a transient BT stutter. Convert to bytes so the parsing layer
        # (struct.unpack_from + byte indexing) is unchanged, and treat any spurious
        # failure as an empty read so callers (init retries, the streaming loop)
        # just try again on the next tick.
        try:
            data = self.dev.read(362, timeout_ms)
        except OSError:
            # A live read blocks for ~timeout_ms, but OSError (a BT stutter, or a
            # dropped/re-enumerated node) returns *instantly*. Back off here so a dead
            # handle can't busy-spin a core; a transient stutter just costs ~50 ms.
            time.sleep(0.05)
            return b""
        return bytes(data) if data else b""

    def send_subcommand(self, sub_id, data=b""):
        # Standard output report 0x01: timing + neutral rumble + subcommand.
        pkt = bytearray(self.OUTPUT_LEN)
        pkt[0] = 0x01
        pkt[1] = self._next()
        pkt[2:10] = b"\x00\x01\x40\x40\x00\x01\x40\x40"
        pkt[10] = sub_id
        d = bytes(data)
        pkt[11:11 + len(d)] = d
        self._write(pkt)

    def _send_and_wait(self, sub_id, data, check, label, timeout=1.5, retries=8):
        """Send subcommand, drain reports until check() passes or timeout.

        Retries are the *healthy* path, not failure: the MCU does not ACK
        synchronously, so the subcommand is re-sent until its reply marker
        appears. A flaky BT link needing 1–2 resends is normal.
        """
        for attempt in range(retries):
            if self.abort is not None and self.abort.is_set():
                return None  # shutdown requested mid-init — bail promptly
            self.send_subcommand(sub_id, data)
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.abort is not None and self.abort.is_set():
                    return None
                buf = self.read(timeout_ms=200)
                if not buf:
                    continue
                if buf[0] == 0x21 and check(buf):
                    return buf
            print(f"  [{label}] retry {attempt + 1}")
        return None

    def set_input_mode_standard(self):
        self._send_and_wait(
            0x03, [0x30],
            check=lambda b: len(b) > 14 and b[14] == 0x03,
            label="set input mode 0x30",
        )

    def enable_imu(self, value=0x01):
        self._send_and_wait(
            0x40, [value],
            check=lambda b: len(b) > 14 and b[14] == 0x40,
            label=f"enable IMU {value}",
        )

    def enable_mcu(self):
        # Subcommand 0x22 [0x01] — MCU resume. ACK byte is 0x80 at buf[13] with
        # subcommand 0x22 echoed at buf[14].
        return self._send_and_wait(
            0x22, [0x01],
            check=lambda b: len(b) > 14 and b[13] == 0x80 and b[14] == 0x22,
            label="enable MCU 0x22",
        )

    def set_mcu_config(self, mcu_cmd, mcu_subcmd, mcu_mode, check, label):
        # Subcommand 0x21 ("Set MCU configuration") needs a 38-byte payload in
        # bytes 11..48 of the output, with byte 48 = CRC-8 over bytes 12..47.
        data = bytearray(38)
        data[0] = mcu_cmd       # byte 11
        data[1] = mcu_subcmd    # byte 12
        data[2] = mcu_mode      # byte 13
        # bytes 14..47 stay zero
        data[37] = mcu_crc8(bytes(data[1:37]))  # CRC over data[1..36]
        return self._send_and_wait(0x21, data, check=check, label=label)

    def set_mcu_mode_ringcon(self):
        # cmd=0x21 (Set MCU mode), subcmd=0x00, mode=0x03 (Ringcon).
        # Reply marker: buf[15]==0x01 and buf[22]==0x03.
        return self.set_mcu_config(
            0x21, 0x00, 0x03,
            check=lambda b: len(b) > 22 and b[15] == 0x01 and b[22] == 0x03,
            label="MCU mode = Ringcon",
        )

    def set_mcu_external_ready(self):
        # cmd=0x21, subcmd=0x01, mode=0x01.
        # Reply marker: buf[15]==0x09 and buf[17]==0x01.
        return self.set_mcu_config(
            0x21, 0x01, 0x01,
            check=lambda b: len(b) > 17 and b[15] == 0x09 and b[17] == 0x01,
            label="MCU external ready",
        )

    def probe_ringcon(self):
        # 0x59 reply has subcommand id at buf[14], and the device id at buf[16].
        # Ring-Con detected => buf[16] == 0x20.
        return self._send_and_wait(
            0x59, b"",
            check=lambda b: len(b) > 16 and b[14] == 0x59 and b[16] == 0x20,
            label="probe Ring-Con 0x59",
            retries=10,
        )

    def set_ringcon_format_config(self):
        # Subcommand 0x5C with the specific 37-byte payload the Switch sends.
        # bytes are positioned at output offsets 11..47.
        payload = bytearray(37)
        # Fill by output offset; subtract 11 to get index into payload.
        for off, val in [
            (11, 0x06), (12, 0x03), (13, 0x25), (14, 0x06),
            (19, 0x1C), (20, 0x16), (21, 237), (22, 52), (23, 54),
            (27, 10),   (28, 100),  (29, 11),  (30, 230), (31, 169), (32, 34),
            (35, 0x04),
            (43, 0x90), (44, 0xA8), (45, 225), (46, 52), (47, 54),
        ]:
            payload[off - 11] = val
        return self._send_and_wait(
            0x5C, payload,
            check=lambda b: len(b) > 14 and b[14] == 0x5C,
            label="ring-con format config 0x5C",
        )

    def enable_ringcon_polling(self):
        return self._send_and_wait(
            0x5A, [0x04, 0x01, 0x01, 0x02],
            check=lambda b: len(b) > 14 and b[14] == 0x5A,
            label="enable polling 0x5A",
        )

    def set_ext_config(self):
        return self._send_and_wait(
            0x58, [0x04, 0x04, 0x12, 0x02],
            check=lambda b: len(b) > 14 and b[14] == 0x58,
            label="ext config 0x58",
        )

    def init_ringcon(self) -> bool:
        """Full right-pad Ring-Con bring-up: MCU power-on, Ringcon mode, strain
        polling. Returns True once the Ring-Con is detected and polling is armed.
        """
        print("Setting input mode 0x30...")
        self.set_input_mode_standard()
        print("Enabling MCU...")
        if not self.enable_mcu():
            print("  ! MCU enable did not ACK")
            return False
        print("Setting MCU mode = Ringcon...")
        if not self.set_mcu_mode_ringcon():
            print("  ! MCU mode set did not confirm")
            return False
        print("Setting MCU external-ready...")
        if not self.set_mcu_external_ready():
            print("  ! MCU external-ready did not confirm")
            return False
        print("Probing for Ring-Con (0x59)...")
        if not self.probe_ringcon():
            print("  ! Ring-Con not detected. Reseat Joy-Con in Ring-Con.")
            return False
        print("Enabling IMU...")
        self.enable_imu(0x03)
        self.enable_imu(0x02)
        self.enable_imu(0x01)
        print("Configuring Ring-Con format (0x5C)...")
        if not self.set_ringcon_format_config():
            print("  ! 0x5C did not ACK")
            return False
        print("Starting polling (0x5A)...")
        if not self.enable_ringcon_polling():
            print("  ! 0x5A did not ACK")
            return False
        print("Final ext config (0x58)...")
        self.set_ext_config()
        return True

    def init_imu_only(self) -> bool:
        """Lightweight init for a bare Joy-Con (e.g. the leg strap): standard full
        input report + IMU, no MCU/Ring-Con handshake. Leg detection is pure IMU,
        so this is all the left pad needs."""
        self.set_input_mode_standard()
        self.enable_imu(0x01)
        return True


def parse_report(buf: bytes) -> dict | None:
    """Unpack a standard full input report (0x30/0x31/0x32) into the raw, nullable
    vocabulary ``{buttons, stick, strain, accel, gyro}``.

    Returns ``None`` for a non-input report. Per the protocol's nullable contract,
    ``strain`` / ``accel`` / ``gyro`` are ``None`` when the report is too short —
    L3 must not assume their presence. ``buttons`` / ``stick`` are **right-pad
    only** ([N7]); the left pad contributes IMU only.
    """
    if not buf or buf[0] not in (0x30, 0x31, 0x32):
        return None
    btn_r, btn_share, btn_l = buf[3], buf[4], buf[5]
    rx = buf[9] | ((buf[10] & 0x0F) << 8)
    ry = (buf[10] >> 4) | (buf[11] << 4)
    # Strain: one byte at offset 40. 0x00 = fully pulled, 0x0a ≈ rest, 0x14 = fully pushed.
    strain = buf[40] if len(buf) > 40 else None
    # IMU: 3 frames of 12 bytes each starting at byte 13. Each frame is accel xyz
    # then gyro xyz, all int16 LE. Ring-Con polling steals byte 40 for the strain
    # value, so frame 2 (offsets 37..48) is corrupted. Pull accel from frame 1
    # (offsets 25..30) instead — still recent (~5ms older). DO NOT "clean up" to
    # frame 2: it reintroduces the corruption, and the right pad's lean+flex are
    # co-available ONLY because of this frame-1 read.
    accel = None
    gyro = None
    if len(buf) >= 31:
        accel = struct.unpack_from("<hhh", buf, 25)
    if len(buf) >= 37:
        # Gyro xyz follows accel within the same IMU frame (offset 31). Frame 1 is
        # intact on both pads — Ring-Con polling only corrupts frame 2's byte 40,
        # so the right Joy-Con's frame-1 gyro is fine too.
        gyro = struct.unpack_from("<hhh", buf, 31)
    return {
        "buttons": (btn_r, btn_share, btn_l),
        "stick": (rx, ry),
        "strain": strain,
        "accel": accel,
        "gyro": gyro,
    }


__all__ = [
    "NINTENDO_VID",
    "JOYCON_R_PID",
    "JOYCON_L_PID",
    "mcu_crc8",
    "find_joycons",
    "find_joycon",
    "JoyCon",
    "parse_report",
]
