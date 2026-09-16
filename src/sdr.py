"""SDR sample sources.

Two implementations share the SampleSource protocol:
  UsbSdr        — live RTL-SDR dongle via pyrtlsdr
  FileSampleSource — reads raw IQ bytes from a file (loops on EOF)

Select via the --sample-file CLI flag; omit it for live USB.
"""

from __future__ import annotations

import io
from typing import Protocol, runtime_checkable


@runtime_checkable
class SampleSource(Protocol):
    def read_block(self, n_bytes: int) -> bytes:
        """Return exactly *n_bytes* of raw uint8 IQ data."""
        ...

    def set_center_freq(self, freq: int) -> None:
        """Retune the source to *freq* Hz."""
        ...

    def close(self) -> None:
        ...


class UsbSdr:
    """Live RTL-SDR dongle via pyrtlsdr."""

    def __init__(self, center_freq: int, sample_rate: int, gain: str | float = "auto", ppm: int = 0) -> None:
        try:
            # pyrtlsdr ≤ 0.3.0 does `import pkg_resources` at module level for
            # version detection. setuptools ≥ 80 removed pkg_resources, so
            # inject a minimal stub when the real module is absent.
            import sys
            if 'pkg_resources' not in sys.modules:
                import importlib.util
                import types
                if importlib.util.find_spec('pkg_resources') is None:
                    sys.modules['pkg_resources'] = types.ModuleType('pkg_resources')
            from rtlsdr import RtlSdr
        except ImportError:
            raise ImportError(
                "pyrtlsdr is required for live hardware. "
                "Install it with: pip install 'rtlamr-python'"
            )
        self._sdr = RtlSdr()
        self._sdr.center_freq = center_freq
        self._sdr.sample_rate = sample_rate
        self._sdr.ppm_error = ppm
        if gain == "auto":
            self._sdr.gain = "auto"
        else:
            self._sdr.gain = float(gain)

    def read_block(self, n_bytes: int) -> bytes:
        """Read *n_bytes* uint8 IQ samples from the dongle."""
        return bytes(self._sdr.read_bytes(n_bytes))

    def set_center_freq(self, freq: int) -> None:
        self._sdr.center_freq = freq

    def close(self) -> None:
        self._sdr.close()


class FileSampleSource:
    """Read raw IQ bytes from a binary file; loop on EOF for continuous testing."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._fh = open(path, "rb")

    def read_block(self, n_bytes: int) -> bytes:
        data = self._fh.read(n_bytes)
        if len(data) < n_bytes:
            # Loop back to the start of the file.
            self._fh.seek(0)
            data = self._fh.read(n_bytes)
        return data

    def set_center_freq(self, freq: int) -> None:
        pass  # no-op; file captures are frequency-agnostic

    def close(self) -> None:
        self._fh.close()


def open_source(
    center_freq: int,
    sample_rate: int,
    gain: str | float = "auto",
    ppm: int = 0,
    sample_file: str | None = None,
) -> SampleSource:
    """Return the appropriate SampleSource based on whether a file path was given."""
    if sample_file:
        return FileSampleSource(sample_file)
    return UsbSdr(center_freq, sample_rate, gain, ppm)
