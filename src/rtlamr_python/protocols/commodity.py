"""ERT type → commodity classification.

Source: https://github.com/bemasher/rtlamr/blob/master/meters.csv
Ambiguous types resolved by predominant field usage:
  4  → electric (Itron AMI/C/R300 series; Sensus R-275 gas is the outlier)
  12 → gas (Itron 100G series; Schlumberger CENTRON electric is the outlier)
"""
from __future__ import annotations

COMMODITY_ERT_TYPES: dict[str, list[int | str]] = {
    "electric": [4, 5, 7, 8],
    "gas":      [0, 1, 2, 9, 12],
    "water":    [3, 11, 13, "r900"],
}

# Reverse lookup: int type code or lowercase string key → commodity name.
_REVERSE: dict[int | str, str] = {
    code: commodity
    for commodity, codes in COMMODITY_ERT_TYPES.items()
    for code in codes
}


def commodity_for_endpoint_type(endpoint_type: str | None) -> str | None:
    """Return "electric", "gas", "water", or None.

    Accepts the CharField value stored on MeterReading.endpoint_type:
    either a stringified integer ("7") or a protocol name ("R900", "IDM").
    """
    if endpoint_type is None:
        return None
    try:
        return _REVERSE.get(int(endpoint_type))
    except ValueError:
        pass
    return _REVERSE.get(endpoint_type.lower())
