"""Tests for R900 protocol: GF arithmetic, parse_bits, and R900Decoder config."""

import pytest
import numpy as np
from src.r900_decoder import _GF32, _r900_filter, _r900_quantize
from src.protocols.r900 import parse_bits, R900


# ---------------------------------------------------------------------------
# GF(32, 37, 2) arithmetic tests
# ---------------------------------------------------------------------------

def test_gf_exp_log_roundtrip():
    """exp(log(x)) == x for all non-zero elements."""
    for x in range(1, 32):
        assert _GF32.exp(_GF32.log(x)) == x


def test_gf_log_zero_returns_minus_one():
    assert _GF32.log(0) == -1


def test_gf_exp_negative_returns_zero():
    assert _GF32.exp(-1) == 0


def test_gf_mul_identity():
    # Multiplying by 1 (= alpha^0) is identity.
    for x in range(1, 32):
        assert _GF32.mul(x, 1) == x


def test_gf_mul_zero():
    for x in range(32):
        assert _GF32.mul(x, 0) == 0
        assert _GF32.mul(0, x) == 0


def test_gf_syndrome_all_zeros():
    """Syndrome of an all-zero codeword should be zero."""
    message = [0] * 31
    syndromes = _GF32.syndrome(message, 5, 29)
    assert syndromes == [0, 0, 0, 0, 0]


# ---------------------------------------------------------------------------
# R900 filter / quantize unit tests
# ---------------------------------------------------------------------------

def test_r900_filter_output_shape():
    chip = 8
    n_samples = chip * 4 * 10 + chip * 4  # room for 10 filter outputs
    signal = np.random.rand(n_samples).astype(np.float64)
    filt = _r900_filter(signal, chip)
    assert filt.ndim == 2
    assert filt.shape[1] == 3


def test_r900_quantize_range():
    """All quantized values should be in 0-5."""
    chip = 8
    signal = np.random.rand(chip * 4 * 50 + chip * 4).astype(np.float64)
    filt = _r900_filter(signal, chip)
    q = _r900_quantize(filt)
    assert np.all(q >= 0)
    assert np.all(q <= 5)


# ---------------------------------------------------------------------------
# parse_bits unit tests
# ---------------------------------------------------------------------------

def _make_r900_bits(
    meter_id: int = 0xDEADBEEF,
    consumption: int = 123456,
) -> str:
    unkn1 = 0x00
    no_use = 0
    backflow = 0
    unkn3 = 0
    leak = 0
    leak_now = 0
    bits = (
        f"{meter_id:032b}"
        f"{unkn1:08b}"
        f"{no_use:06b}"
        f"{backflow:02b}"
        f"{consumption:024b}"
        f"{unkn3:02b}"
        f"{leak:04b}"
        f"{leak_now:02b}"
    )
    assert len(bits) == 80
    return bits


def test_parse_bits_valid():
    bits = _make_r900_bits(meter_id=0x12345678, consumption=999)
    msg = parse_bits(bits)
    assert msg is not None
    assert isinstance(msg, R900)
    assert msg.meter_id == 0x12345678
    assert msg.consumption == 999


def test_parse_bits_zero_id_returns_none():
    bits = _make_r900_bits(meter_id=0)
    assert parse_bits(bits) is None


def test_parse_bits_too_short_returns_none():
    assert parse_bits("0" * 79) is None


def test_r900_as_dict():
    msg = parse_bits(_make_r900_bits(meter_id=111, consumption=222))
    assert msg is not None
    d = msg.as_dict()
    assert d["type"] == "R900"
    assert d["endpoint_id"] == 111
    assert d["consumption"] == 222


# ---------------------------------------------------------------------------
# R900Decoder config
# ---------------------------------------------------------------------------

def test_r900_decoder_config():
    from src.r900_decoder import R900Decoder
    d = R900Decoder(chip_length=8)
    assert d.center_freq == 912_380_000
    assert d.sample_rate == 32_768 * 8
    # block_size2 should be positive and even
    assert d.block_size2 > 0
    assert d.block_size2 % 2 == 0
