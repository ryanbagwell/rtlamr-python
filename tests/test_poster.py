"""Tests for ApiPoster field mapping (_to_payload)."""

import pytest
from src.poster import _to_payload


def _base(overrides=None):
    rec = {
        "time": "2026-05-21T00:00:00Z",
        "type": "SCM+",
        "endpoint_id": 12345,
        "endpoint_type": 7,
        "consumption": 99999,
        "tamper": "0x0301",
        "packet_crc": "0xABCD",
    }
    if overrides:
        rec.update(overrides)
    return rec


def test_scmplus_basic_mapping():
    p = _to_payload(_base())
    assert p["timestamp"] == "2026-05-21T00:00:00Z"
    assert p["endpoint_id"] == 12345
    assert p["protocol"] == "SCM+"
    assert p["endpoint_type"] == 7
    assert p["consumption"] == 99999


def test_scmplus_tamper_hex_parsed():
    p = _to_payload(_base({"tamper": "0x0301"}))
    assert p["tamper"] == 0x0301


def test_scm_tamper_packed():
    rec = _base({
        "type": "SCM",
        "tamper_phy": 3,
        "tamper_enc": 1,
    })
    del rec["tamper"]
    p = _to_payload(rec)
    assert p["tamper"] == (3 << 2) | 1


def test_no_tamper_field_gives_none():
    rec = _base()
    del rec["tamper"]
    p = _to_payload(rec)
    assert p["tamper"] is None


def test_idm_uses_last_consumption_count():
    rec = {
        "time": "2026-05-21T00:00:00Z",
        "type": "IDM",
        "endpoint_id": 87654321,
        "last_consumption_count": 5000,
        "consumption_interval_count": 10,
        "differential_consumption_intervals": [0] * 47,
    }
    p = _to_payload(rec)
    assert p["consumption"] == 5000
    assert p["endpoint_type"] is None
    assert p["tamper"] is None


def test_r900_no_endpoint_type_or_tamper():
    rec = {
        "time": "2026-05-21T00:00:00Z",
        "type": "R900",
        "endpoint_id": 111,
        "consumption": 222,
    }
    p = _to_payload(rec)
    assert p["consumption"] == 222
    assert p["endpoint_type"] is None
    assert p["tamper"] is None


def test_missing_consumption_returns_none():
    rec = {
        "time": "2026-05-21T00:00:00Z",
        "type": "IDM",
        "endpoint_id": 1,
    }
    assert _to_payload(rec) is None


def test_packet_crc_not_included():
    p = _to_payload(_base())
    assert "packet_crc" not in p
