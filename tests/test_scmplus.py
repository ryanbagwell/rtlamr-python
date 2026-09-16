import struct
import pytest
from src.crc import checksum
from src.protocols.scmplus import parse, SCMPlus, _PROTOCOL_ID


def _make_packet(
    frame_sync: int = 0x1234,
    protocol_id: int = _PROTOCOL_ID,
    endpoint_type: int = 0x04,
    endpoint_id: int = 12345678,
    consumption: int = 987654,
    tamper: int = 0x0000,
) -> bytes:
    header = struct.pack(
        ">HBBIIHH",
        frame_sync, protocol_id, endpoint_type, endpoint_id, consumption, tamper, 0,
    )
    crc_value = checksum(header[2:14]) ^ 0xFFFF  # xorout=0xFFFF (CRC-16/GENIBUS)
    return header[:14] + struct.pack(">H", crc_value)


def test_parse_valid_packet():
    pkt = _make_packet()
    msg = parse(pkt)
    assert msg is not None
    assert isinstance(msg, SCMPlus)
    assert msg.endpoint_id == 12345678
    assert msg.consumption == 987654
    assert msg.endpoint_type == 0x04
    assert msg.protocol_id == _PROTOCOL_ID


def test_parse_returns_none_on_bad_crc():
    pkt = bytearray(_make_packet())
    pkt[8] ^= 0xFF  # corrupt consumption field
    assert parse(bytes(pkt)) is None


def test_parse_returns_none_on_wrong_protocol_id():
    pkt = _make_packet(protocol_id=0x00)
    assert parse(pkt) is None


def test_parse_returns_none_on_zero_endpoint_id():
    pkt = _make_packet(endpoint_id=0)
    assert parse(pkt) is None


def test_parse_returns_none_when_too_short():
    assert parse(b"\x00" * 10) is None


def test_as_dict_contains_required_fields():
    msg = parse(_make_packet())
    d = msg.as_dict()
    assert d["type"] == "SCM+"
    assert d["endpoint_id"] == 12345678
    assert d["consumption"] == 987654
