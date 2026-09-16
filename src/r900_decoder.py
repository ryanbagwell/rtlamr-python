"""R900 signal processing pipeline.

Port of rtlamr-go/r900/r900.go and rtlamr-go/r900/gf/gf.go.

R900 uses a 4-chip/6-symbol encoding rather than Manchester coding:

  Six symbols (each 4 chips):
    0: 0011   3: 1100  (inverse of 0)
    1: 0101   4: 1010  (inverse of 1)
    2: 0110   5: 1001  (inverse of 2)

A matched filter computes three correlation values per sample position,
corresponding to the three base chip patterns.  The symbol with the highest
absolute correlation value is selected; the sign determines whether it is
the base (0-2) or inverted (3-5) form.

Reed-Solomon verification over GF(32, polynomial=37, generator=2) is applied
to the 21-symbol payload before field extraction.

Preamble search uses the Manchester-quantized signal (same rolling-buffer
architecture as the other decoders) since the R900 training sequence produces
a predictable bit pattern through the Manchester filter.  The 4-chip filter
is applied in parallel to the same magnitude signal, and the preamble hit
indices from the Manchester pass are used to locate payloads in the 6-symbol
quantized buffer.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from src.decoder import magnitude, matched_filter, _search, _next_power_of_2
from src.protocols.r900 import R900, parse_bits, _R900_CENTER_FREQ

# R900 protocol constants (from rtlamr-go/r900/r900.go)
_DATA_RATE = 32_768
_PREAMBLE_BITS = np.array(
    [int(b) for b in "00000000000000001110010101100100"], dtype=np.uint8
)
_PREAMBLE_SYMBOLS = 32
_PACKET_SYMBOLS = 116   # in Manchester-symbol units (ChipLength * 2 per symbol)
_PAYLOAD_SYMBOLS = 42   # R900 4-chip symbols in the payload (21 pairs)


# ---------------------------------------------------------------------------
# Galois Field GF(32, 37, 2) — port of rtlamr-go/r900/gf/gf.go
# ---------------------------------------------------------------------------

class _GF:
    """Galois Field arithmetic for Reed-Solomon syndrome computation."""

    def __init__(self, order: int, poly: int, alpha: int) -> None:
        n = order - 1
        self._n = n
        log = [0] * order
        exp = [0] * (2 * n)
        x = 1
        for i in range(n):
            exp[i] = x
            exp[i + n] = x
            log[x] = i
            x = self._mul_gf(x, alpha, order, poly)
        log[0] = n
        self._log = log
        self._exp = exp

    @staticmethod
    def _mul_gf(x: int, y: int, order: int, poly: int) -> int:
        z = 0
        while x > 0:
            if x & 1:
                z ^= y
            x >>= 1
            y <<= 1
            if y & order:
                y ^= poly
        return z

    def exp(self, e: int) -> int:
        if e < 0:
            return 0
        return self._exp[e % self._n]

    def log(self, x: int) -> int:
        return -1 if x == 0 else self._log[x]

    def mul(self, x: int, y: int) -> int:
        if x == 0 or y == 0:
            return 0
        return self._exp[self._log[x] + self._log[y]]

    def syndrome(self, message: list[int], parity_count: int, offset: int) -> list[int]:
        result = []
        for i in range(parity_count):
            syn = message[0]
            for v in message[1:]:
                syn = self.mul(syn, self.exp(offset + i)) ^ v
            result.append(syn)
        return result


_GF32 = _GF(32, 37, 2)


# ---------------------------------------------------------------------------
# 4-chip matched filter for R900
# ---------------------------------------------------------------------------

def _r900_filter(signal: np.ndarray, chip_length: int) -> np.ndarray:
    """Compute the 3-value 4-chip matched filter output.

    Returns shape (N, 3) where each row is [f0, f1, f2] corresponding to
    the three base chip patterns: 1100, 1010, 1001.
    """
    cl = chip_length
    n = len(signal) - 4 * cl + 1
    if n <= 0:
        return np.zeros((0, 3), dtype=np.float64)

    csum = np.empty(len(signal) + 1, dtype=np.float64)
    csum[0] = 0.0
    np.cumsum(signal, out=csum[1:])

    c0 = csum[:n]
    c1 = csum[cl: cl + n] * 2
    c2 = csum[2 * cl: 2 * cl + n] * 2
    c3 = csum[3 * cl: 3 * cl + n] * 2
    c4 = csum[4 * cl: 4 * cl + n]

    f = np.empty((n, 3), dtype=np.float64)
    f[:, 0] = c2 - c4 - c0           # 1100
    f[:, 1] = c1 - c2 + c3 - c4 - c0  # 1010
    f[:, 2] = c1 - c3 + c4 - c0      # 1001
    return f


def _r900_quantize(filtered: np.ndarray) -> np.ndarray:
    """Map each filtered row to a 6-symbol value (0-5)."""
    abs_f = np.abs(filtered)
    argmax = np.argmax(abs_f, axis=1).astype(np.uint8)
    signs = filtered[np.arange(len(filtered)), argmax]
    quantized = argmax.copy()
    quantized[signs > 0] += 3
    return quantized


# ---------------------------------------------------------------------------
# R900Decoder
# ---------------------------------------------------------------------------

class R900Decoder:
    """Stateful R900 decoder.

    Architecture mirrors Decoder but maintains two parallel signal chains:
      - Manchester chain: preamble search (same rolling-buffer logic)
      - R900 4-chip chain: payload symbol extraction

    Both are shifted by block_size each call, keeping them in sync so that
    a preamble hit index from the Manchester chain maps to the same position
    in the R900 quantized buffer.
    """

    def __init__(self, chip_length: int = 72) -> None:
        chip = chip_length
        sym = chip * 2  # Manchester symbol length

        preamble_len = _PREAMBLE_SYMBOLS * sym
        packet_len = _PACKET_SYMBOLS * sym
        block_size = _next_power_of_2(preamble_len)
        buf_len = packet_len + block_size

        self._chip = chip
        self._sym = sym
        self._block_size = block_size
        self._preamble_len = preamble_len
        self._packet_len = packet_len
        self._buf_len = buf_len
        self.center_freq = _R900_CENTER_FREQ
        self.sample_rate = _DATA_RATE * chip
        self.block_size2 = block_size * 2

        # Manchester chain (for preamble search)
        self._man_signal = np.zeros(block_size + sym, dtype=np.float64)
        self._man_quantized = np.zeros(buf_len, dtype=np.uint8)

        # R900 signal chain
        # block_size extra samples at the front so the rolling shift can read
        # buf_len elements starting at block_size without going out of bounds.
        # The chip*4 tail provides filter continuity beyond buf_len.
        self._r900_signal = np.zeros(block_size + buf_len + chip * 4, dtype=np.float64)
        self._r900_quantized = np.zeros(buf_len, dtype=np.uint8)

        self._prev_seen: set[str] = set()
        self._next_seen: set[str] = set()

    def reset(self) -> None:
        """Zero rolling buffers and clear dedup sets. Call after an SDR retune."""
        self._man_signal[:] = 0
        self._man_quantized[:] = 0
        self._r900_signal[:] = 0
        self._r900_quantized[:] = 0
        self._prev_seen.clear()
        self._next_seen.clear()

    def decode(self, raw_block: bytes) -> list[R900]:
        chip = self._chip
        sym = self._sym
        block_size = self._block_size
        preamble_len = self._preamble_len
        packet_len = self._packet_len
        buf_len = self._buf_len

        block = np.frombuffer(raw_block, dtype=np.uint8)
        mag = magnitude(block)  # block_size2 bytes → block_size magnitude samples

        # --- Manchester chain (preamble search) ---
        self._man_signal[:sym] = self._man_signal[block_size:]
        self._man_signal[sym:] = mag
        filt_man = matched_filter(self._man_signal, chip)[:block_size]
        self._man_quantized[:packet_len] = self._man_quantized[block_size:]
        self._man_quantized[packet_len:] = filt_man

        # --- R900 4-chip chain (payload decoding) ---
        # Roll the signal buffer left by block_size.  The buffer is
        # (block_size + buf_len + chip*4) samples wide so [block_size:] has exactly
        # (buf_len + chip*4) elements — the same count as [:-block_size].
        # New magnitudes fill position [packet_len:packet_len+block_size] so that
        # _r900_signal[packet_len+k] == mag[k], matching the Manchester chain's
        # alignment (_man_quantized[packet_len+k] also comes from mag[k]).
        self._r900_signal[:-block_size] = self._r900_signal[block_size:]
        self._r900_signal[packet_len: packet_len + block_size] = mag

        r900_filt = _r900_filter(self._r900_signal[:buf_len + chip * 4], chip)
        r900_q = _r900_quantize(r900_filt)
        # Trim to buf_len to match the rolling buffer size.
        self._r900_quantized[:] = r900_q[:buf_len]

        # --- Preamble search in Manchester-quantized buffer ---
        # Fake a minimal Config-like object for _search.
        cfg = _SearchCfg(block_size, preamble_len, sym, _PREAMBLE_SYMBOLS)
        indices = _search(self._man_quantized, _PREAMBLE_BITS, cfg)

        self._next_seen.clear()
        messages: list[R900] = []

        for idx in indices:
            if idx > block_size:
                continue

            # Payload starts after the 32-symbol preamble in the 4-chip quantized
            # stream.  The index into the Manchester buffer maps to the same
            # position in the R900 buffer since both shift by block_size each call.
            # Adjust: preamble_len samples → skip preamble, subtract one sym for
            # alignment (matches Go: payloadIdx = pkt.Idx + preambleLength - SymbolLength).
            payload_idx = idx + preamble_len - sym

            # Extract 42 symbols (21 pairs) at 4-chip intervals.
            symbols: list[int] = []
            for k in range(_PAYLOAD_SYMBOLS):
                q_idx = payload_idx + k * chip * 4
                if q_idx >= buf_len:
                    break
                symbols.append(int(self._r900_quantized[q_idx]))
            if len(symbols) < _PAYLOAD_SYMBOLS:
                continue

            # Convert pairs of base-6 symbols to base-6 digit string.
            digits = "".join(str(s) for s in symbols)

            # Decode symbol pairs into 5-bit GF(32) elements.
            gf_symbols: list[int] = []
            bad = False
            bits_str = ""
            for k in range(0, len(digits), 2):
                val = int(digits[k: k + 2], 6)
                if val > 31:
                    bad = True
                    break
                gf_symbols.append(val)
                bits_str += f"{val:05b}"
            if bad:
                continue

            # Reed-Solomon check: 5 parity symbols at positions 26-30 (offset 29).
            rs_buf = [0] * 31
            rs_buf[:16] = gf_symbols[:16]
            rs_buf[26:] = gf_symbols[16:]
            syndromes = _GF32.syndrome(rs_buf, 5, 29)
            if any(s != 0 for s in syndromes):
                continue

            if digits in self._prev_seen or digits in self._next_seen:
                continue
            self._next_seen.add(digits)

            msg = parse_bits(bits_str)
            if msg is None:
                continue
            messages.append(msg)

        self._prev_seen, self._next_seen = self._next_seen, self._prev_seen
        return messages


class _SearchCfg:
    """Minimal duck-typed Config for _search()."""
    __slots__ = ("block_size", "preamble_length", "symbol_length", "preamble_symbols")

    def __init__(self, block_size: int, preamble_length: int, symbol_length: int, preamble_symbols: int) -> None:
        self.block_size = block_size
        self.preamble_length = preamble_length
        self.symbol_length = symbol_length
        self.preamble_symbols = preamble_symbols
