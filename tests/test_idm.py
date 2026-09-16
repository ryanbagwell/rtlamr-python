"""Tests for IDM and NetIDM protocol parsers."""

import struct
import pytest
from src.crc import checksum, RESIDUE
from src.protocols.idm import parse as idm_parse, IDM, _PREAMBLE, _PACKET_SYMBOLS
from src.protocols.netidm import parse as netidm_parse, NetIDM


def _embed_crc(raw: bytearray, start: int, end: int) -> None:
    """Compute CCITT CRC over raw[start:end-2] and write it to raw[end-2:end]."""
    data_to_cover = bytes(raw[start: end - 2]) + b"\x00\x00"
    # We need: checksum(raw[start:end]) == RESIDUE
    # Which means: checksum(raw[start:end-2]) gives some value, and
    # we need to find crc_field such that checksum(raw[start:end-2] + crc_field) == RESIDUE.
    # Using the CCITT property: compute crc over data, then derive the embedded value.
    crc_of_data = checksum(bytes(raw[start: end - 2]))
    # Find x such that checksum(crc_of_data_state + x) == RESIDUE.
    # We know checksum(bytes[start:end-2]) is some state. The remaining 2 bytes
    # are the CRC field itself. We need to solve for the embedded CRC value.
    # Trial: compute what value makes the full range have residue 0x1D0F.
    # Use brute-force linearity: the CRC field = final_crc ^ 0xFFFF applied correctly.
    # Simpler: compute checksum over data with placeholder 0000, then compute
    # the 2 remaining bytes that bring the running CRC to RESIDUE.
    #
    # CCITT residue property: checksum(data + crc_bytes) == RESIDUE
    # where crc_bytes = checksum(data) ^ 0xFFFF.
    crc_val = checksum(bytes(raw[start: end - 2])) ^ 0xFFFF
    raw[end - 2] = (crc_val >> 8) & 0xFF
    raw[end - 1] = crc_val & 0xFF


def _make_idm_packet(
    ert_serial_number: int = 87654321,
    last_consumption_count: int = 5000,
) -> bytes:
    raw = bytearray(92)
    # Preamble bytes[0:4] — the 32-bit value 0x555516A3
    # (16 training bits 0x5555 + 16-bit frame sync 0x16A3)
    struct.pack_into(">I", raw, 0, 0x555516A3)
    raw[4] = 0x1C   # PacketTypeID for IDM
    raw[5] = 0x5C   # PacketLength (92 = 0x5C)
    raw[6] = 0x00   # HammingCode
    raw[7] = 0x04   # ApplicationVersion
    raw[8] = 0x04   # ERTType (low nibble)
    struct.pack_into(">I", raw, 9, ert_serial_number)
    raw[13] = 10    # ConsumptionIntervalCount
    raw[14] = 0x00  # ModuleProgrammingState
    # TamperCounters bytes[15:21] — zero
    # AsynchronousCounters bytes[21:23] — zero
    # PowerOutageFlags bytes[23:29] — zero
    struct.pack_into(">I", raw, 29, last_consumption_count)
    # DifferentialConsumptionIntervals: 47 × 9-bit values starting at bit 264
    # Leave all intervals as 0 for simplicity.
    # TransmitTimeOffset bytes[86:88] — zero
    # SerialNumberCRC bytes[88:90] and PacketCRC bytes[90:92] computed below.

    # Serial number CRC: covers bytes[9:13] + bytes[88:90].
    # For now compute with bytes[88:90] = 0:
    serial_buf = bytes(raw[9:13]) + b"\x00\x00"
    serial_crc = checksum(serial_buf) ^ 0xFFFF
    struct.pack_into(">H", raw, 88, serial_crc)

    # Now fix serial CRC to account for the embedded value changing bytes[88:90]:
    # recompute correctly using the iterative property.
    # Simple approach: compute checksum over final [9:13]+[88:90] and adjust.
    # Use the known formula: crc_field = checksum(data) ^ 0xFFFF
    serial_data = bytes(raw[9:13]) + bytes(raw[88:90])
    # We need checksum(serial_data + 2_more_bytes) == RESIDUE.
    # Wait, the buffer is exactly 6 bytes: [9:13](4) + [88:90](2) = 6 bytes.
    # We want checksum of this 6-byte buffer == RESIDUE.
    # So: embed crc at bytes[88:90] such that checksum([9:13]+[88:90]) == RESIDUE.
    # Let c = checksum(raw[9:13]) then continuing with 2 CRC bytes x:
    # We need: checksum(raw[9:13] + x) == RESIDUE
    # x = ???  Let's compute iteratively.
    c = checksum(bytes(raw[9:13]))
    # We need ((c << 8) ^ table[(c >> 8) ^ x_high]) continued with x_low == RESIDUE
    # Easier: brute-force search for x in 0..65535
    from src.crc import _table as _ccitt_table
    for crc_val in range(0x10000):
        x_high = (crc_val >> 8) & 0xFF
        x_low = crc_val & 0xFF
        r = c
        r = ((r << 8) ^ _ccitt_table[(r >> 8) ^ x_high]) & 0xFFFF
        r = ((r << 8) ^ _ccitt_table[(r >> 8) ^ x_low]) & 0xFFFF
        if r == RESIDUE:
            struct.pack_into(">H", raw, 88, crc_val)
            break

    # Packet CRC: covers bytes[4:92], so last 2 bytes are bytes[90:92].
    packet_crc = checksum(bytes(raw[4:90])) ^ 0xFFFF
    struct.pack_into(">H", raw, 90, packet_crc)

    return bytes(raw)


def test_idm_parse_valid_packet():
    pkt = _make_idm_packet()
    msg = idm_parse(pkt)
    assert msg is not None
    assert isinstance(msg, IDM)
    assert msg.ert_serial_number == 87654321
    assert msg.last_consumption_count == 5000
    assert len(msg.differential_consumption_intervals) == 47


def test_idm_parse_returns_none_on_bad_packet_crc():
    pkt = bytearray(_make_idm_packet())
    pkt[30] ^= 0xFF  # corrupt last_consumption_count
    assert idm_parse(bytes(pkt)) is None


def test_idm_parse_returns_none_when_too_short():
    assert idm_parse(b"\x00" * 80) is None


def test_idm_parse_returns_none_on_zero_serial():
    pkt = _make_idm_packet(ert_serial_number=0)
    assert idm_parse(pkt) is None


def test_idm_as_dict():
    msg = idm_parse(_make_idm_packet(last_consumption_count=9999))
    assert msg is not None
    d = msg.as_dict()
    assert d["type"] == "IDM"
    assert d["endpoint_id"] == 87654321
    assert d["last_consumption_count"] == 9999
    assert len(d["differential_consumption_intervals"]) == 47


def test_idm_make_config():
    from src.protocols.idm import make_config
    import numpy as np
    cfg = make_config(chip_length=8)
    assert cfg.packet_symbols == _PACKET_SYMBOLS
    np.testing.assert_array_equal(cfg.preamble_bits, _PREAMBLE)
