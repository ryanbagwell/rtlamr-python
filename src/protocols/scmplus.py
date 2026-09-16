"""SCM+ (Standard Consumption Message Plus) packet parser.

Port of rtlamr-go/scmplus/scmplus.go.

Packet layout (16 bytes, big-endian):
  offset  size  field
  0       2     FrameSync   uint16
  2       1     ProtocolID  uint8   must == 0x1E
  3       1     EndpointType uint8
  4       4     EndpointID  uint32  must be non-zero
  8       4     Consumption uint32
  12      2     Tamper      uint16
  14      2     PacketCRC   uint16

CRC covers bytes[2:14] (the 12 bytes before PacketCRC); valid when
CCITT16(bytes[2:]) == 0x1D0F (the residue includes PacketCRC itself).
"""

import struct
from dataclasses import dataclass

import numpy as np

from src import crc

_STRUCT = struct.Struct(">HBBIIHH")
_PROTOCOL_ID = 0x1E

_PREAMBLE = np.array([int(b) for b in "0001011010100011"], dtype=np.uint8)
_PACKET_SYMBOLS = 128  # 16 bytes × 8 bits


def make_config(chip_length: int = 72):
    from src.decoder import Config
    return Config(
        chip_length=chip_length,
        preamble_bits=_PREAMBLE.copy(),
        packet_symbols=_PACKET_SYMBOLS,
    )


@dataclass
class SCMPlus:
    frame_sync: int
    protocol_id: int
    endpoint_type: int
    endpoint_id: int
    consumption: int
    tamper: int
    packet_crc: int

    def msg_type(self) -> str:
        return "SCM+"

    def as_dict(self) -> dict:
        return {
            "type": self.msg_type(),
            "endpoint_id": self.endpoint_id,
            "endpoint_type": self.endpoint_type,
            "consumption": self.consumption,
            "tamper": f"0x{self.tamper:04X}",
            "packet_crc": f"0x{self.packet_crc:04X}",
        }


def parse(raw: bytes | bytearray) -> SCMPlus | None:
    """Parse 16 raw bytes into an SCMPlus message.

    Returns None if the packet fails CRC, has an invalid ProtocolID, or
    has a zero EndpointID.
    """
    if len(raw) < 16:
        return None
    if not crc.valid(raw[:16]):
        return None

    frame_sync, protocol_id, endpoint_type, endpoint_id, consumption, tamper, packet_crc = (
        _STRUCT.unpack(raw[:16])
    )

    if protocol_id != _PROTOCOL_ID or endpoint_id == 0:
        return None

    return SCMPlus(
        frame_sync=frame_sync,
        protocol_id=protocol_id,
        endpoint_type=endpoint_type,
        endpoint_id=endpoint_id,
        consumption=consumption,
        tamper=tamper,
        packet_crc=packet_crc,
    )
