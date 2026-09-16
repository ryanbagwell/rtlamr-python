"""CCITT-16 CRC used by the SCM+ protocol (CRC-16/GENIBUS variant).

Port of rtlamr-go/crc/crc.go.

Polynomial : 0x1021
Init value : 0xFFFF
XOR-out    : 0xFFFF  (meters complement the CRC before embedding it as PacketCRC)
Valid residue: 0x1D0F  (checksum over bytes[2:] of a valid SCM+ packet equals this)

To embed a valid PacketCRC when constructing a packet:
    pkt_crc = checksum(bytes[2:14]) ^ 0xFFFF

To verify a received packet:
    valid(packet)  — checks checksum(packet[2:]) == RESIDUE
"""

_POLY = 0x1021
_INIT = 0xFFFF
RESIDUE = 0x1D0F

# Build 256-entry lookup table once at import time.
_table: list[int] = []
for _i in range(256):
    _crc = _i << 8
    for _ in range(8):
        if _crc & 0x8000:
            _crc = (_crc << 1) ^ _POLY
        else:
            _crc <<= 1
        _crc &= 0xFFFF
    _table.append(_crc)


def checksum(data: bytes | bytearray, init: int = _INIT) -> int:
    """Return the CCITT-16 CRC of *data* starting from *init*."""
    crc = init
    for byte in data:
        crc = ((crc << 8) ^ _table[(crc >> 8) ^ byte]) & 0xFFFF
    return crc


def valid(packet: bytes | bytearray) -> bool:
    """Return True when the CRC of packet[2:] equals the expected residue."""
    return checksum(packet[2:]) == RESIDUE


# ---------------------------------------------------------------------------
# BCH-16 CRC used by the SCM protocol
# ---------------------------------------------------------------------------

_BCH_POLY = 0x6F63

_bch_table: list[int] = []
for _i in range(256):
    _crc = _i << 8
    for _ in range(8):
        if _crc & 0x8000:
            _crc = (_crc << 1) ^ _BCH_POLY
        else:
            _crc <<= 1
        _crc &= 0xFFFF
    _bch_table.append(_crc)


def bch_checksum(data: bytes | bytearray, init: int = 0) -> int:
    """Return the BCH-16 CRC of *data* (poly=0x6F63, init=0, xorout=0)."""
    crc = init
    for byte in data:
        crc = ((crc << 8) ^ _bch_table[(crc >> 8) ^ byte]) & 0xFFFF
    return crc


def bch_valid(packet: bytes | bytearray) -> bool:
    """Return True when bch_checksum(packet[2:12]) == 0 (SCM validation)."""
    return bch_checksum(packet[2:12]) == 0
