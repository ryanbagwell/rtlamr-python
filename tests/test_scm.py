"""Tests for the SCM (Standard Consumption Message) protocol parser."""

import struct
import pytest
from src.crc import bch_checksum
from src.protocols.scm import parse, SCM, _PREAMBLE, _PACKET_SYMBOLS


def _make_scm_packet(
    endpoint_id: int = 12345678,
    endpoint_type: int = 7,
    tamper_phy: int = 0,
    tamper_enc: int = 0,
    consumption: int = 98765,
) -> bytes:
    """Build a valid 12-byte SCM packet with the correct BCH checksum."""
    # The preamble is 21 bits: "111110010101001100000"
    # Fields are at bit positions within the 96-bit packet (preamble included).
    # We construct the 96-bit integer directly.
    preamble_str = "111110010101001100000"
    # Encode fields into their bit positions.
    eid_hi = (endpoint_id >> 24) & 0x3    # bits[21:23]
    eid_lo = endpoint_id & 0xFFFFFF       # bits[56:80]
    phy = tamper_phy & 0x3                # bits[24:26]
    etype = endpoint_type & 0xF           # bits[26:30]
    enc = tamper_enc & 0x3                # bits[30:32]
    cons = consumption & 0xFFFFFF         # bits[32:56]

    # Build the 96-bit string (CRC placeholder = 0 for now).
    # Bit layout (preamble included):
    #   0:21  preamble
    #   21:23 ertid high (2 bits)
    #   23    reserved (1 bit, always 0)
    #   24:26 tamperphy
    #   26:30 erttype
    #   30:32 tamperenc
    #   32:56 consumption
    #   56:80 ertid low (24 bits)
    #   80:96 checksum
    bits = (
        preamble_str           # 21 bits
        + f"{eid_hi:02b}"      # bits[21:23]
        + "0"                  # bit[23] reserved
        + f"{phy:02b}"         # bits[24:26]
        + f"{etype:04b}"       # bits[26:30]
        + f"{enc:02b}"         # bits[30:32]
        + f"{cons:024b}"       # bits[32:56]
        + f"{eid_lo:024b}"     # bits[56:80]
        + "0" * 16             # bits[80:96] — CRC placeholder
    )
    assert len(bits) == 96

    raw = int(bits, 2).to_bytes(12, "big")

    # Compute BCH CRC over bytes[2:12] and embed it.
    # The CRC occupies the last 16 bits (bytes[10:12]).
    # We need to place the CRC value such that bch_checksum(packet[2:12]) == 0.
    # Compute CRC over bytes[2:10] (with last 2 bytes zero), then embed that value.
    crc_val = bch_checksum(raw[2:10])
    raw = raw[:10] + crc_val.to_bytes(2, "big")
    # Verify: bch_checksum over all 10 bytes [2:12] must be 0.
    assert bch_checksum(raw[2:12]) == 0, f"BCH checksum failed: got {bch_checksum(raw[2:12]):#x}"
    return raw


def test_parse_valid_packet():
    pkt = _make_scm_packet()
    msg = parse(pkt)
    assert msg is not None
    assert isinstance(msg, SCM)
    assert msg.endpoint_id == 12345678
    assert msg.consumption == 98765


def test_parse_returns_none_on_bad_crc():
    pkt = bytearray(_make_scm_packet())
    pkt[5] ^= 0xFF  # corrupt a byte in the checksum-covered region
    assert parse(bytes(pkt)) is None


def test_parse_returns_none_on_zero_endpoint_id():
    # endpoint_id = 0 should be rejected
    pkt = _make_scm_packet(endpoint_id=0)
    assert parse(pkt) is None


def test_parse_returns_none_when_too_short():
    assert parse(b"\x00" * 10) is None


def test_as_dict_contains_required_fields():
    msg = parse(_make_scm_packet(consumption=55555))
    assert msg is not None
    d = msg.as_dict()
    assert d["type"] == "SCM"
    assert d["endpoint_id"] == 12345678
    assert d["consumption"] == 55555


def test_make_config_produces_correct_preamble():
    from src.protocols.scm import make_config
    import numpy as np
    cfg = make_config(chip_length=8)
    assert cfg.packet_symbols == _PACKET_SYMBOLS
    np.testing.assert_array_equal(cfg.preamble_bits, _PREAMBLE)
    assert cfg.symbol_length == 16
