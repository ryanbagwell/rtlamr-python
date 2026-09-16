"""SCM (Standard Consumption Message) protocol parser.

Port of rtlamr-go/scm/scm.go.

Packet: 12 bytes (96 bits total, including 21-bit preamble).
CRC: BCH-16 (poly=0x6F63, init=0, xorout=0) over bytes[2:12].
Fields are extracted from bit positions within the 96-bit packet because
the 21-bit preamble is not byte-aligned.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src import crc

_PREAMBLE = np.array([int(b) for b in "111110010101001100000"], dtype=np.uint8)
_PACKET_SYMBOLS = 96  # 12 bytes × 8 bits


def make_config(chip_length: int = 72):
    from src.decoder import Config
    return Config(
        chip_length=chip_length,
        preamble_bits=_PREAMBLE.copy(),
        packet_symbols=_PACKET_SYMBOLS,
    )


@dataclass
class SCM:
    endpoint_id: int    # 26-bit ERT ID
    endpoint_type: int  # 4-bit meter type
    tamper_phy: int     # 2-bit physical tamper indicator
    tamper_enc: int     # 2-bit encoder tamper indicator
    consumption: int    # 24-bit consumption value
    packet_crc: int     # 16-bit checksum

    def as_dict(self) -> dict:
        return {
            "type": "SCM",
            "endpoint_id": self.endpoint_id,
            "endpoint_type": self.endpoint_type,
            "tamper_phy": self.tamper_phy,
            "tamper_enc": self.tamper_enc,
            "consumption": self.consumption,
        }


def parse(raw: bytes | bytearray) -> SCM | None:
    if len(raw) < 12:
        return None
    if not crc.bch_valid(raw):
        return None
    bits = "".join(f"{b:08b}" for b in raw[:12])
    # Fields are at non-byte-aligned positions within the 96-bit packet.
    # Bit layout from rtlamr-go/scm/scm.go NewSCM():
    #   bits[21:23] + bits[56:80] → 26-bit ERT ID
    #   bits[24:26]  → TamperPhy
    #   bits[26:30]  → ERTType
    #   bits[30:32]  → TamperEnc
    #   bits[32:56]  → Consumption
    #   bits[80:96]  → Checksum
    endpoint_id = int(bits[21:23] + bits[56:80], 2)
    if endpoint_id == 0:
        return None
    endpoint_type = int(bits[26:30], 2)
    tamper_phy = int(bits[24:26], 2)
    tamper_enc = int(bits[30:32], 2)
    consumption = int(bits[32:56], 2)
    packet_crc = int(bits[80:96], 2)
    return SCM(endpoint_id, endpoint_type, tamper_phy, tamper_enc, consumption, packet_crc)
