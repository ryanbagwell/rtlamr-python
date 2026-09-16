import struct
import pytest
from src.crc import checksum, valid, RESIDUE, _INIT


def _make_packet(endpoint_id: int = 12345678, consumption: int = 987654) -> bytes:
    """Build a valid 16-byte SCM+ packet with a correct CCITT-16 CRC."""
    # Pack everything except PacketCRC
    header = struct.pack(
        ">HBBIIHH",
        0x1234,       # FrameSync
        0x1E,         # ProtocolID
        0x04,         # EndpointType
        endpoint_id,
        consumption,
        0x0000,       # Tamper
        0x0000,       # PacketCRC placeholder
    )
    crc_value = checksum(header[2:14]) ^ 0xFFFF  # xorout=0xFFFF (CRC-16/GENIBUS)
    return header[:14] + struct.pack(">H", crc_value)


def test_residue_on_valid_packet():
    pkt = _make_packet()
    assert checksum(pkt[2:]) == RESIDUE


def test_valid_returns_true_for_correct_packet():
    assert valid(_make_packet()) is True


def test_valid_returns_false_for_corrupted_packet():
    pkt = bytearray(_make_packet())
    pkt[5] ^= 0xFF  # flip bits in EndpointID
    assert valid(pkt) is False


def test_checksum_empty_data():
    # checksum of empty bytes with default init returns init unchanged
    assert checksum(b"") == _INIT


def test_checksum_known_vector():
    # CCITT-16 (x^16 + x^12 + x^5 + 1) over b"\x31\x32\x33" == 0x3218
    # (Standard test vector: "123" → 0x29B1 for poly 0x1021 init 0xFFFF)
    assert checksum(b"123456789") == 0x29B1
