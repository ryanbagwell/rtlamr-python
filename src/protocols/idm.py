"""IDM (Interval Data Message) protocol parser.

Port of rtlamr-go/idm/idm.go.

Packet: 92 bytes (736 bits total).
Preamble: 32 symbols — 16 training bits + 16-bit frame sync (0x16A3).
CRC: CCITT-16 (same as SCM+), two checks:
  1. checksum(bytes[4:92]) == RESIDUE   (packet CRC)
  2. checksum(bytes[9:13] + bytes[88:90]) == RESIDUE   (serial number CRC)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from src import crc

_PREAMBLE = np.array(
    [int(b) for b in "01010101010101010001011010100011"], dtype=np.uint8
)
_PACKET_SYMBOLS = 92 * 8  # 736 bits


def make_config(chip_length: int = 72):
    from src.decoder import Config
    return Config(
        chip_length=chip_length,
        preamble_bits=_PREAMBLE.copy(),
        packet_symbols=_PACKET_SYMBOLS,
    )


@dataclass
class IDM:
    preamble: int                              # bytes[0:4]
    packet_type_id: int                        # bytes[4]
    packet_length: int                         # bytes[5]
    hamming_code: int                          # bytes[6]
    application_version: int                   # bytes[7]
    ert_type: int                              # bytes[8] & 0x0F
    ert_serial_number: int                     # bytes[9:13]
    consumption_interval_count: int            # bytes[13]
    module_programming_state: int              # bytes[14]
    tamper_counters: bytes                     # bytes[15:21]
    asynchronous_counters: int                 # bytes[21:23]
    power_outage_flags: bytes                  # bytes[23:29]
    last_consumption_count: int                # bytes[29:33]
    differential_consumption_intervals: list   # 47 × 9-bit values
    transmit_time_offset: int                  # bytes[86:88]
    serial_number_crc: int                     # bytes[88:90]
    packet_crc: int                            # bytes[90:92]

    def as_dict(self) -> dict:
        return {
            "type": "IDM",
            "endpoint_id": self.ert_serial_number,
            "ert_type": self.ert_type,
            "consumption_interval_count": self.consumption_interval_count,
            "last_consumption_count": self.last_consumption_count,
            "differential_consumption_intervals": self.differential_consumption_intervals,
        }


def _serial_crc_buf(raw: bytes) -> bytes:
    """Build the 6-byte buffer used for the serial number CRC check."""
    buf = bytearray(6)
    buf[0:4] = raw[9:13]
    buf[4:6] = raw[88:90]
    return bytes(buf)


def parse(raw: bytes | bytearray) -> IDM | None:
    if len(raw) < 92:
        return None
    if crc.checksum(raw[4:92]) != crc.RESIDUE:
        return None
    if crc.checksum(_serial_crc_buf(raw)) != crc.RESIDUE:
        return None

    preamble = struct.unpack_from(">I", raw, 0)[0]
    packet_type_id = raw[4]
    packet_length_b = raw[5]
    hamming_code = raw[6]
    application_version = raw[7]
    ert_type = raw[8] & 0x0F
    ert_serial_number = struct.unpack_from(">I", raw, 9)[0]

    if ert_serial_number == 0:
        return None

    consumption_interval_count = raw[13]
    module_programming_state = raw[14]
    tamper_counters = bytes(raw[15:21])
    asynchronous_counters = struct.unpack_from(">H", raw, 21)[0]
    power_outage_flags = bytes(raw[23:29])
    last_consumption_count = struct.unpack_from(">I", raw, 29)[0]

    # 47 differential intervals packed at 9 bits each, starting at bit 264.
    bits = "".join(f"{b:08b}" for b in raw[:92])
    intervals = []
    offset = 264
    for _ in range(47):
        intervals.append(int(bits[offset:offset + 9], 2))
        offset += 9

    transmit_time_offset = struct.unpack_from(">H", raw, 86)[0]
    serial_number_crc = struct.unpack_from(">H", raw, 88)[0]
    packet_crc = struct.unpack_from(">H", raw, 90)[0]

    return IDM(
        preamble=preamble,
        packet_type_id=packet_type_id,
        packet_length=packet_length_b,
        hamming_code=hamming_code,
        application_version=application_version,
        ert_type=ert_type,
        ert_serial_number=ert_serial_number,
        consumption_interval_count=consumption_interval_count,
        module_programming_state=module_programming_state,
        tamper_counters=tamper_counters,
        asynchronous_counters=asynchronous_counters,
        power_outage_flags=power_outage_flags,
        last_consumption_count=last_consumption_count,
        differential_consumption_intervals=intervals,
        transmit_time_offset=transmit_time_offset,
        serial_number_crc=serial_number_crc,
        packet_crc=packet_crc,
    )
