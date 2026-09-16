"""Signal processing pipeline for ERT smart meter packets.

Port of rtlamr-go/protocol/decode.go.

Pipeline per block:
  1. magnitude()      — uint8 IQ pairs → float64 signal via MagLUT
  2. matched_filter() — Manchester-coded cumulative-sum filter → quantized bits
  3. _search()        — find preamble positions in quantized signal
  4. _slice_packets() — extract packet bytes at each preamble position
  5. parser callable  — protocol-specific CRC check and field extraction

The Decoder class maintains two rolling buffers (signal + quantized) so that
packets spanning a block boundary are not lost.

Each protocol supplies its own Config (via make_config()) and parse callable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

# SCM+ protocol constants (from rtlamr-go/scmplus/scmplus.go)
_CENTER_FREQ = 912_600_155  # Hz
_DATA_RATE = 32_768         # bps

# Default preamble for SCM+ — 0x16A3 = 0001011010100011
_PREAMBLE_BITS = np.array(
    [int(b) for b in "0001011010100011"], dtype=np.uint8
)
_PACKET_SYMBOLS = 128  # 16 bytes × 8 bits


@dataclass
class Config:
    chip_length: int = 72
    preamble_bits: np.ndarray = field(
        default_factory=lambda: _PREAMBLE_BITS.copy()
    )
    packet_symbols: int = _PACKET_SYMBOLS
    center_freq: int = _CENTER_FREQ

    # All fields below are derived from the above in __post_init__.
    symbol_length: int = field(init=False)
    sample_rate: int = field(init=False)
    preamble_symbols: int = field(init=False)
    preamble_length: int = field(init=False)
    packet_length: int = field(init=False)
    block_size: int = field(init=False)
    block_size2: int = field(init=False)
    buffer_length: int = field(init=False)

    def __post_init__(self) -> None:
        self.preamble_symbols = len(self.preamble_bits)
        self.symbol_length = self.chip_length * 2
        self.sample_rate = _DATA_RATE * self.chip_length
        self.preamble_length = self.preamble_symbols * self.symbol_length
        self.packet_length = self.packet_symbols * self.symbol_length
        self.block_size = _next_power_of_2(self.preamble_length)
        self.block_size2 = self.block_size * 2
        self.buffer_length = self.packet_length + self.block_size


def _next_power_of_2(v: int) -> int:
    return 1 << math.ceil(math.log2(v))


# ---------------------------------------------------------------------------
# MagLUT — pre-computed normalised squared magnitudes for each uint8 value
# ---------------------------------------------------------------------------

_mag_lut: np.ndarray = ((127.5 - np.arange(256, dtype=np.float64)) / 127.5) ** 2


def magnitude(block: np.ndarray) -> np.ndarray:
    """Convert raw uint8 IQ block to float64 magnitude array.

    Input shape: (2N,) — interleaved I, Q bytes.
    Output shape: (N,) — sum of squared normalised I and Q.
    """
    samples = _mag_lut[block]
    return samples[0::2] + samples[1::2]


# ---------------------------------------------------------------------------
# Matched filter for Manchester-coded signal
# ---------------------------------------------------------------------------

def matched_filter(signal: np.ndarray, chip_length: int) -> np.ndarray:
    """Apply Manchester-coded matched filter; return quantized 0/1 uint8 array.

    Uses a cumulative-sum approach identical to Decoder.Filter in the Go code.
    """
    symbol_length = chip_length * 2
    csum = np.empty(len(signal) + 1, dtype=np.float64)
    csum[0] = 0.0
    np.cumsum(signal, out=csum[1:])

    n_out = len(signal) - symbol_length + 1
    lower = csum[chip_length: chip_length + n_out]
    base = csum[:n_out]
    upper = csum[symbol_length: symbol_length + n_out]

    f = (lower - base) - (upper - lower)
    return (f >= 0).astype(np.uint8)[:n_out]


# ---------------------------------------------------------------------------
# Preamble search
# ---------------------------------------------------------------------------

def _search(quantized: np.ndarray, preamble: np.ndarray, cfg: Config) -> list[int]:
    """Return sample indices where *preamble* exists in *quantized*.

    Two-pass approach ported from Decoder.Search:
      1. Coarse byte-level pass — eliminate bytes that can't contain preamble start.
      2. Fine bit-level pass — verify exact preamble match at symbol boundaries.
    """
    sym = cfg.symbol_length
    block = cfg.block_size

    # Pack quantized signal to bytes (8 bits each).
    packed = np.packbits(quantized[:block + cfg.preamble_length])

    sym_bytes = sym >> 3

    # Coarse pass: find byte positions where the first preamble bit might start.
    first_bit_mask = 0x00 if preamble[0] == 1 else 0xFF
    candidates = [i for i, b in enumerate(packed[:block >> 3]) if b != first_bit_mask]

    # Eliminate byte-level candidates for each subsequent preamble bit.
    for p_idx in range(1, len(preamble)):
        bit_mask = 0x00 if preamble[p_idx] == 1 else 0xFF
        offset = p_idx * sym_bytes
        candidates = [i for i in candidates if packed[i + offset] != bit_mask]
        if not candidates:
            return []

    # Expand byte candidates to bit-level indices.
    bit_candidates = [byte_idx * 8 + bit_off
                      for byte_idx in candidates
                      for bit_off in range(8)]

    # Fine pass: verify the full preamble at each bit-level index.
    valid: list[int] = []
    q = quantized
    for idx in bit_candidates:
        for p_idx, p_bit in enumerate(preamble):
            if q[idx + p_idx * sym] != p_bit:
                break
        else:
            valid.append(idx)

    return valid


# ---------------------------------------------------------------------------
# Packet slicing
# ---------------------------------------------------------------------------

def _slice_packets(quantized: np.ndarray, indices: list[int], cfg: Config) -> list[bytes]:
    """Extract and pack packet bits at each preamble index."""
    packets = []
    for idx in indices:
        if idx > cfg.block_size:
            # Packet will be fully available in the next block.
            continue
        bits = quantized[idx: idx + cfg.packet_symbols * cfg.symbol_length: cfg.symbol_length]
        if len(bits) < cfg.packet_symbols:
            continue
        packed = np.packbits(bits[:cfg.packet_symbols])
        packets.append(packed.tobytes())
    return packets


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------

class Decoder:
    """Stateful decoder that accumulates sample blocks and emits messages.

    Matches Go's rolling-buffer design:
      _signal    — length block_size + symbol_length  (short window for filter continuity)
      _quantized — length buffer_length               (shifted left each block, tail filled)

    Pass a protocol-specific parser callable to decode into a specific message type.
    Defaults to SCM+ if no parser is provided.
    """

    def __init__(self, cfg: Config | None = None, parser: Callable | None = None) -> None:
        self.cfg = cfg or Config()

        if parser is None:
            from src.protocols.scmplus import parse as _default_parse
            self._parser: Callable = _default_parse
        else:
            self._parser = parser

        # Signal window: [symbol_length carry-over | block_size new magnitudes]
        self._signal = np.zeros(self.cfg.block_size + self.cfg.symbol_length, dtype=np.float64)

        # Quantized history: buffer_length, shifted left each block.
        self._quantized = np.zeros(self.cfg.buffer_length, dtype=np.uint8)

        # De-duplicate messages seen in consecutive blocks (same packet at block boundary).
        self._prev_digests: set[bytes] = set()
        self._next_digests: set[bytes] = set()

        # Diagnostic counters — reset never; read externally for logging.
        self.stats: dict[str, int] = {
            "candidates": 0,   # preamble positions found by _search
            "raw_packets": 0,  # packets sliced from those positions
            "parse_ok": 0,     # packets accepted by the parser
            "dedup_drop": 0,   # valid packets suppressed as duplicates
        }

    @property
    def block_size2(self) -> int:
        return self.cfg.block_size2

    def reset(self) -> None:
        """Zero rolling buffers and clear dedup sets. Call after an SDR retune."""
        self._signal[:] = 0
        self._quantized[:] = 0
        self._prev_digests.clear()
        self._next_digests.clear()

    def decode(self, raw_block: bytes) -> list[Any]:
        """Process one raw IQ block; return any newly decoded messages."""
        cfg = self.cfg
        block = np.frombuffer(raw_block, dtype=np.uint8)

        # Shift signal left by block_size, keeping symbol_length samples as carry-over.
        self._signal[:cfg.symbol_length] = self._signal[cfg.block_size:]
        mag = magnitude(block)  # block_size2 bytes → block_size magnitudes
        self._signal[cfg.symbol_length:] = mag

        # Filter signal window → trim to exactly block_size quantized outputs.
        filt = matched_filter(self._signal, cfg.chip_length)[:cfg.block_size]

        # Shift quantized left by block_size, fill tail with new filter output.
        self._quantized[:cfg.packet_length] = self._quantized[cfg.block_size:]
        self._quantized[cfg.packet_length:] = filt

        indices = _search(self._quantized, cfg.preamble_bits, cfg)
        raw_packets = _slice_packets(self._quantized, indices, cfg)

        self.stats["candidates"] += len(indices)
        self.stats["raw_packets"] += len(raw_packets)

        self._next_digests.clear()
        messages: list[Any] = []

        for raw in raw_packets:
            msg = self._parser(raw)
            if msg is None:
                continue

            self.stats["parse_ok"] += 1

            # Use raw bytes as dedup key — same transmission → same bytes.
            if raw in self._prev_digests or raw in self._next_digests:
                self.stats["dedup_drop"] += 1
                continue

            self._next_digests.add(raw)
            messages.append(msg)

        self._prev_digests, self._next_digests = self._next_digests, self._prev_digests

        return messages
