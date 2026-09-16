"""Tests for the decoder signal processing pipeline.

Rather than capturing real SDR data, we synthesise a minimal IQ stream that
encodes a known SCM+ packet using Manchester coding, feed it through the
decoder, and verify we recover the correct message.
"""

import struct
import numpy as np
import pytest

from src.crc import checksum
from src.decoder import Config, Decoder, magnitude, matched_filter, _PREAMBLE_BITS
from src.protocols.scmplus import _PROTOCOL_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FRAME_SYNC = 0x16A3  # preamble pattern 0001011010100011 as a uint16


def _make_scmplus_bytes(endpoint_id: int = 99887766, consumption: int = 112233) -> bytes:
    # FrameSync must be 0x16A3 — this is the preamble pattern the decoder searches for.
    header = struct.pack(
        ">HBBIIHH",
        _FRAME_SYNC, _PROTOCOL_ID, 0x04, endpoint_id, consumption, 0x0000, 0,
    )
    crc_value = checksum(header[2:14]) ^ 0xFFFF  # xorout=0xFFFF (CRC-16/GENIBUS)
    return header[:14] + struct.pack(">H", crc_value)


def _encode_manchester(bits: np.ndarray, chip_length: int) -> np.ndarray:
    """Encode a 0/1 bit array into a Manchester-coded sample array.

    Manchester: 1 → high-chip then low-chip, 0 → low-chip then high-chip.
    Returns a float64 signal suitable for testing matched_filter.
    """
    symbol_length = chip_length * 2
    signal = np.zeros(len(bits) * symbol_length, dtype=np.float64)
    for i, bit in enumerate(bits):
        start = i * symbol_length
        if bit == 1:
            signal[start: start + chip_length] = 1.0
            signal[start + chip_length: start + symbol_length] = 0.0
        else:
            signal[start: start + chip_length] = 0.0
            signal[start + chip_length: start + symbol_length] = 1.0
    return signal


def _bits_from_bytes(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


# ---------------------------------------------------------------------------
# Unit tests — magnitude
# ---------------------------------------------------------------------------

def test_magnitude_dc_offset():
    # A perfect DC-offset signal (all 127) should produce near-zero magnitude.
    block = np.full(256, 127, dtype=np.uint8)
    mag = magnitude(block)
    assert mag.shape == (128,)
    assert np.all(mag < 0.01)


def test_magnitude_max_excursion():
    # Values at 0 or 255 produce the maximum squared magnitude ≈ 1.0 each.
    block = np.zeros(4, dtype=np.uint8)  # I=0, Q=0, I=0, Q=0
    mag = magnitude(block)
    assert np.allclose(mag, 1.0 + 1.0, atol=0.01)


# ---------------------------------------------------------------------------
# Unit tests — matched_filter
# ---------------------------------------------------------------------------

def test_matched_filter_recovers_bits():
    chip = 8
    bits = np.array([1, 0, 1, 1, 0, 0, 1, 0], dtype=np.uint8)
    signal = _encode_manchester(bits, chip)
    quantized = matched_filter(signal, chip)
    # The filter output aligns at symbol boundaries; sample the middle of each symbol.
    sym = chip * 2
    recovered = np.array([quantized[i * sym] for i in range(len(bits))])
    np.testing.assert_array_equal(recovered, bits)


# ---------------------------------------------------------------------------
# Unit tests — Config derived values
# ---------------------------------------------------------------------------

def test_config_defaults():
    cfg = Config(chip_length=72)
    assert cfg.symbol_length == 144
    assert cfg.sample_rate == 32_768 * 72
    assert cfg.block_size2 == cfg.block_size * 2
    assert cfg.buffer_length == cfg.packet_length + cfg.block_size


def test_config_small_chip():
    cfg = Config(chip_length=8)
    assert cfg.symbol_length == 16
    assert cfg.sample_rate == 32_768 * 8


# ---------------------------------------------------------------------------
# Integration test — Decoder recovers a synthetic SCM+ message
# ---------------------------------------------------------------------------

def test_decoder_recovers_scmplus_message():
    chip = 8  # small chip length keeps the test fast
    cfg = Config(chip_length=chip)
    decoder = Decoder(cfg)

    pkt_bytes = _make_scmplus_bytes()

    # The FrameSync (first 2 bytes) IS the preamble — no separate preamble prefix needed.
    packet_bits = _bits_from_bytes(pkt_bytes)  # 128 bits including FrameSync=0x16A3
    signal = _encode_manchester(packet_bits, chip)

    # Encode as uint8 IQ pairs: high chip → I=Q=0, low chip → I=Q=200.
    raw_iq = np.empty(len(signal) * 2, dtype=np.uint8)
    for i, v in enumerate(signal):
        sample = 0 if v > 0.5 else 200
        raw_iq[i * 2] = sample
        raw_iq[i * 2 + 1] = sample

    # block_size2 is the byte count per block (= 2 × block_size samples).
    n_bytes = cfg.block_size2
    # The decoder shifts data through a buffer of depth buffer_length//block_size blocks.
    # We need that many total blocks so the preamble reaches the search window.
    n_blocks = max(
        (len(raw_iq) + n_bytes - 1) // n_bytes,
        cfg.buffer_length // cfg.block_size,
    )
    padded = np.zeros(n_blocks * n_bytes, dtype=np.uint8)
    padded[:len(raw_iq)] = raw_iq

    found: list = []
    for offset in range(0, len(padded), n_bytes):
        found.extend(decoder.decode(padded[offset: offset + n_bytes].tobytes()))

    assert len(found) > 0, "Decoder produced no messages from synthetic IQ stream"
    msg = found[0]
    assert msg.endpoint_id == 99887766
    assert msg.consumption == 112233
