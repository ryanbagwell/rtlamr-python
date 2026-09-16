"""rtlamr-python — Multi-protocol ERT smart meter receiver.

Reads IQ samples from an RTL-SDR dongle (or a raw sample file for testing),
decodes ERT packets, and prints each message as a JSON line to stdout.

Supported protocols (use --protocol to select; defaults to all Manchester ones):
  scmplus — Standard Consumption Message Plus (16 bytes, most electric meters)
  scm     — Standard Consumption Message (12 bytes, older electric meters)
  idm     — Interval Data Message (92 bytes, hourly interval data)
  netidm  — Net Meter Interval Data Message (92 bytes, net-metering variant)
  r900    — Neptune R900 water meters (different center freq: 912.38 MHz)

Usage:
  # All Manchester protocols on live hardware
  rtlamr

  # Single protocol
  rtlamr --protocol scmplus

  # From a recorded capture file
  rtlamr --sample-file /path/to/capture.bin

  # Filter to specific meters
  rtlamr --meter-id 12345678

  # Alternate between Manchester and R900 (different center frequencies)
  rtlamr --protocol scmplus r900
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from rtlamr_python.listener import ALL_PROTOCOLS, DEFAULT_PROTOCOLS, listen
from rtlamr_python.poster import ApiPoster

LOG = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Multi-protocol ERT smart meter receiver")
    p.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="TOML config file (default: none)",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        default=None,
        help="print per-decoder stats to stderr every 500 blocks",
    )
    p.add_argument(
        "--chip-length",
        type=int,
        default=None,
        metavar="N",
        help="chip length in samples (default: 72 → ~2.36 MHz sample rate)",
    )
    p.add_argument(
        "--gain",
        default=None,
        metavar="GAIN",
        help='tuner gain in dB or "auto" (default: auto)',
    )
    p.add_argument(
        "--freq-correction",
        type=int,
        default=None,
        metavar="PPM",
        dest="freq_correction",
        help="frequency correction for the RTL-SDR oscillator in parts per million "
             "(negative if signals appear below their expected frequency)",
    )
    p.add_argument(
        "--meter-id",
        type=int,
        nargs="+",
        default=None,
        metavar="ID",
        dest="meter_id",
        help="only forward readings from these endpoint IDs (space-separated); "
             "overrides meter_ids in the config file",
    )
    p.add_argument(
        "--sample-file",
        default=None,
        metavar="PATH",
        help="read raw IQ bytes from a file instead of live hardware",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help="stop after this many seconds (0 = run forever)",
    )
    p.add_argument(
        "--protocol",
        nargs="+",
        choices=list(ALL_PROTOCOLS),
        default=None,
        metavar="PROTO",
        help="protocols to decode (space-separated); default: all Manchester",
    )
    p.add_argument(
        "--api-url",
        default=None,
        metavar="URL",
        help='base URL of the REST API (e.g. "http://localhost:8000/api"); '
             "if omitted, readings are only written to stdout",
    )
    p.add_argument(
        "--api-key",
        default=None,
        metavar="KEY",
        help="value for the X-API-Key header (only needed when the API requires auth)",
    )
    p.add_argument(
        "--switch-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="alternating mode: switch frequency after this many seconds without "
             "a message (default: 60)",
    )
    return p.parse_args(argv)


def _load_config(path: str | None) -> dict:
    if path is None:
        return {}
    import tomllib
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


_DEFAULTS: dict = {
    "chip_length": 72,
    "gain": "auto",
    "freq_correction": 0,
    "duration": 0.0,
    "verbose": False,
    "switch_timeout": 60.0,
}

_CONFIG_KEYS = {
    "api_url", "api_key", "meter_id", "meter_ids", "protocol", "switch_timeout",
    "gain", "freq_correction", "chip_length", "duration", "verbose",
}


def _apply_config(args: argparse.Namespace, cfg: dict) -> None:
    """Back-fill args still at None from cfg, then apply built-in defaults."""
    # Normalize meter ID to list[int] or None.
    if args.meter_id is None:
        if "meter_ids" in cfg:
            args.meter_id = list(cfg["meter_ids"])
        elif "meter_id" in cfg:
            args.meter_id = [cfg["meter_id"]]  # legacy single-int key

    # Normalize protocol to list[str] or None.
    if args.protocol is None and "protocol" in cfg:
        raw = cfg["protocol"]
        if isinstance(raw, list):
            args.protocol = raw
        else:
            args.protocol = [p.strip() for p in str(raw).split(",")]

    for key in _CONFIG_KEYS - {"meter_id", "meter_ids", "protocol"}:
        if getattr(args, key) is None and key in cfg:
            setattr(args, key, cfg[key])
    for key, default in _DEFAULTS.items():
        if getattr(args, key) is None:
            setattr(args, key, default)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    args = parse_args(argv)
    _apply_config(args, _load_config(args.config))

    poster = ApiPoster(args.api_url, args.api_key) if args.api_url else None

    # Graceful shutdown on SIGINT.
    stop_event = threading.Event()

    def _stop(signum, frame):
        LOG.info("Received signal %s, stopping.", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)

    try:
        for record in listen(
            protocols=args.protocol or list(DEFAULT_PROTOCOLS),
            meter_id=args.meter_id,
            chip_length=args.chip_length,
            gain=args.gain,
            freq_correction=args.freq_correction,
            sample_file=args.sample_file,
            switch_timeout=args.switch_timeout,
            duration=args.duration,
            verbose=args.verbose,
            stop_event=stop_event,
        ):
            sys.stdout.write(json.dumps(record) + "\n")
            sys.stdout.flush()
            if poster:
                poster.submit(record)
    except ValueError as exc:
        LOG.error(str(exc))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
