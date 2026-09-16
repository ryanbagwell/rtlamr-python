"""R900 (Neptune R900) water meter protocol.

Port of rtlamr-go/r900/r900.go.

R900 uses a completely different signal chain from the Manchester protocols:
  - 4-chip symbols (6 possible values) instead of 2-chip Manchester
  - Reed-Solomon error correction over GF(32, 37, 2)
  - Different center frequency: 912,380,000 Hz

Signal decoding is handled by R900Decoder in src/r900_decoder.py.
This module only contains the message dataclass and bit-field extraction.
"""

from __future__ import annotations

from dataclasses import dataclass


_R900_CENTER_FREQ = 912_380_000  # Hz — different from other ERT protocols


@dataclass
class R900:
    meter_id: int       # 32 bits
    unkn1: int          # 8 bits
    no_use: int         # 6 bits — day bins with no usage
    backflow: int       # 2 bits — backflow detected past 35 days (hi/lo)
    consumption: int    # 24 bits
    unkn3: int          # 2 bits
    leak: int           # 4 bits — day bins with leak
    leak_now: int       # 2 bits — leak past 24 hours (hi/lo)

    def as_dict(self) -> dict:
        return {
            "type": "R900",
            "endpoint_id": self.meter_id,
            "consumption": self.consumption,
            "no_use": self.no_use,
            "backflow": self.backflow,
            "leak": self.leak,
            "leak_now": self.leak_now,
        }


def parse_bits(bits: str) -> R900 | None:
    """Parse an R900 message from a 80-bit string of '0'/'1' characters.

    Input is the payload bits after Reed-Solomon verification, derived from
    21 base-6 symbols (each encoded as 5 bits).
    """
    if len(bits) < 80:
        return None
    meter_id = int(bits[0:32], 2)
    if meter_id == 0:
        return None
    unkn1 = int(bits[32:40], 2)
    no_use = int(bits[40:46], 2)
    backflow = int(bits[46:48], 2)
    consumption = int(bits[48:72], 2)
    unkn3 = int(bits[72:74], 2)
    leak = int(bits[74:78], 2)
    leak_now = int(bits[78:80], 2)
    return R900(meter_id, unkn1, no_use, backflow, consumption, unkn3, leak, leak_now)
