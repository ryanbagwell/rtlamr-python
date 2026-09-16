"""Integration tests for the public library API (listen/listen_once/start_listening).

Reuses the synthetic SCM+ IQ generation from test_decoder.py, written out to a
temp file so it can be read back through the real sample-file SDR source —
exercising the full listener.listen() loop rather than just the Decoder class.
"""

import struct
import time

import numpy as np
import pytest

from rtlamr_python.crc import checksum
from rtlamr_python.listener import listen, listen_once, start_listening
from rtlamr_python.protocols.scmplus import _PROTOCOL_ID

_FRAME_SYNC = 0x16A3
_CHIP = 8  # small chip length keeps the test fast


def _make_scmplus_bytes(endpoint_id: int, consumption: int) -> bytes:
    header = struct.pack(
        ">HBBIIHH",
        _FRAME_SYNC, _PROTOCOL_ID, 0x04, endpoint_id, consumption, 0x0000, 0,
    )
    crc_value = checksum(header[2:14]) ^ 0xFFFF
    return header[:14] + struct.pack(">H", crc_value)


def _encode_manchester(bits: np.ndarray, chip_length: int) -> np.ndarray:
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


def _make_sample_file(tmp_path, endpoint_id: int, consumption: int) -> str:
    from rtlamr_python.decoder import Config

    cfg = Config(chip_length=_CHIP, packet_symbols=128)

    pkt_bytes = _make_scmplus_bytes(endpoint_id, consumption)
    packet_bits = np.unpackbits(np.frombuffer(pkt_bytes, dtype=np.uint8))
    signal = _encode_manchester(packet_bits, _CHIP)

    raw_iq = np.empty(len(signal) * 2, dtype=np.uint8)
    for i, v in enumerate(signal):
        sample = 0 if v > 0.5 else 200
        raw_iq[i * 2] = sample
        raw_iq[i * 2 + 1] = sample

    n_bytes = cfg.block_size2
    n_blocks = max(
        (len(raw_iq) + n_bytes - 1) // n_bytes,
        cfg.buffer_length // cfg.block_size,
    )
    padded = np.zeros(n_blocks * n_bytes, dtype=np.uint8)
    padded[: len(raw_iq)] = raw_iq

    path = tmp_path / "capture.bin"
    path.write_bytes(padded.tobytes())
    return str(path)


def test_listen_yields_decoded_reading(tmp_path):
    sample_file = _make_sample_file(tmp_path, endpoint_id=99887766, consumption=112233)

    gen = listen(protocols=["scmplus"], chip_length=_CHIP, sample_file=sample_file)
    try:
        record = next(gen)
    finally:
        gen.close()

    assert record["type"] == "SCM+"
    assert record["endpoint_id"] == 99887766
    assert record["consumption"] == 112233
    assert "time" in record


def test_listen_meter_id_filter_excludes_other_endpoints(tmp_path):
    sample_file = _make_sample_file(tmp_path, endpoint_id=11111111, consumption=1)

    gen = listen(
        protocols=["scmplus"],
        chip_length=_CHIP,
        sample_file=sample_file,
        meter_id=[22222222],  # does not match the encoded endpoint_id
        duration=0.2,
    )
    records = list(gen)
    assert records == []


def test_listen_once_returns_single_reading(tmp_path):
    sample_file = _make_sample_file(tmp_path, endpoint_id=55555555, consumption=999)

    record = listen_once(protocols=["scmplus"], chip_length=_CHIP, sample_file=sample_file)

    assert record["endpoint_id"] == 55555555
    assert record["consumption"] == 999


def test_listen_rejects_unknown_protocol():
    with pytest.raises(ValueError):
        next(listen(protocols=["not-a-real-protocol"]))


def test_start_listening_invokes_callback_and_stops(tmp_path):
    sample_file = _make_sample_file(tmp_path, endpoint_id=42424242, consumption=7)

    received: list[dict] = []
    handle = start_listening(
        received.append,
        protocols=["scmplus"],
        chip_length=_CHIP,
        sample_file=sample_file,
    )
    try:
        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        handle.stop(timeout=5)

    assert not handle.is_running()
    assert len(received) >= 1
    assert received[0]["endpoint_id"] == 42424242
