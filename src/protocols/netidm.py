"""NetIDM (Net Meter Interval Data Message) protocol parser.

Port of rtlamr-go/netidm/netidm.go.

Same preamble, packet size, and CRC checks as IDM.  Fields differ starting
at byte 15: the NetIDM carries LastGeneration, LastConsumption,
LastConsumptionNet, and 27 × 14-bit differential intervals (vs IDM's
47 × 9-bit intervals).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from src import crc
from src.protocols.idm import _PREAMBLE, _PACKET_SYMBOLS, _serial_crc_buf


def make_config(chip_length: int = 72):
    from src.decoder import Config
    return Config(
        chip_length=chip_length,
        preamble_bits=_PREAMBLE.copy(),
        packet_symbols=_PACKET_SYMBOLS,
    )


@dataclass
class NetIDM:
    preamble: int
    protocol_id: int
    packet_length: int
    hamming_code: int
    application_version: int
    ert_type: int
    ert_serial_number: int
    consumption_interval_count: int
    programming_state: int
    last_generation: int    # 3 bytes, bytes[28:31]
    last_consumption: int   # 3 bytes, bytes[25:28]
    last_consumption_net: int  # 4 bytes, bytes[34:38]
    differential_consumption_intervals: list   # 27 × 14-bit values
    transmit_time_offset: int
    serial_number_crc: int
    packet_crc: int

    def as_dict(self) -> dict:
        return {
            "type": "NetIDM",
            "endpoint_id": self.ert_serial_number,
            "ert_type": self.ert_type,
            "consumption_interval_count": self.consumption_interval_count,
            "last_consumption": self.last_consumption,
            "last_generation": self.last_generation,
            "last_consumption_net": self.last_consumption_net,
            "differential_consumption_intervals": self.differential_consumption_intervals,
        }


def parse(raw: bytes | bytearray) -> NetIDM | None:
    if len(raw) < 92:
        return None
    if crc.checksum(raw[4:92]) != crc.RESIDUE:
        return None
    if crc.checksum(_serial_crc_buf(raw)) != crc.RESIDUE:
        return None

    preamble = struct.unpack_from(">I", raw, 0)[0]
    protocol_id = raw[4]
    packet_length_b = raw[5]
    hamming_code = raw[6]
    application_version = raw[7]
    ert_type = raw[8] & 0x0F
    ert_serial_number = struct.unpack_from(">I", raw, 9)[0]

    if ert_serial_number == 0:
        return None

    consumption_interval_count = raw[13]
    programming_state = raw[14]

    # 3-byte big-endian values not aligned to 4-byte boundaries.
    last_consumption = (raw[25] << 16) | (raw[26] << 8) | raw[27]
    last_generation = (raw[28] << 16) | (raw[29] << 8) | raw[30]
    last_consumption_net = struct.unpack_from(">I", raw, 34)[0]

    # 27 differential intervals packed at 14 bits each, starting at bit 304.
    bits = "".join(f"{b:08b}" for b in raw[:92])
    intervals = []
    offset = 304
    for _ in range(27):
        intervals.append(int(bits[offset:offset + 14], 2))
        offset += 14

    transmit_time_offset = struct.unpack_from(">H", raw, 86)[0]
    serial_number_crc = struct.unpack_from(">H", raw, 88)[0]
    packet_crc = struct.unpack_from(">H", raw, 90)[0]

    return NetIDM(
        preamble=preamble,
        protocol_id=protocol_id,
        packet_length=packet_length_b,
        hamming_code=hamming_code,
        application_version=application_version,
        ert_type=ert_type,
        ert_serial_number=ert_serial_number,
        consumption_interval_count=consumption_interval_count,
        programming_state=programming_state,
        last_generation=last_generation,
        last_consumption=last_consumption,
        last_consumption_net=last_consumption_net,
        differential_consumption_intervals=intervals,
        transmit_time_offset=transmit_time_offset,
        serial_number_crc=serial_number_crc,
        packet_crc=packet_crc,
    )
