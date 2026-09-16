#!/usr/bin/env python3
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
  python main.py

  # Single protocol
  python main.py --protocol scmplus

  # From a recorded capture file
  python main.py --sample-file /path/to/capture.bin

  # Filter to specific meters
  python main.py --meter-id 12345678

  # Alternate between Manchester and R900 (different center frequencies)
  python main.py --protocol scmplus r900
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from collections import defaultdict

from src.decoder import Config, Decoder
from src.poster import ApiPoster
from src.protocols import scm, scmplus, idm, netidm
from src.r900_decoder import R900Decoder
from src.sdr import open_source

LOG = logging.getLogger(__name__)

_MANCHESTER_PROTOCOLS = {
    "scmplus": (scmplus.make_config, scmplus.parse),
    "scm":     (scm.make_config,     scm.parse),
    "idm":     (idm.make_config,     idm.parse),
    "netidm":  (netidm.make_config,  netidm.parse),
}

# Default set omits IDM/NetIDM — their large block sizes (16 384 bytes) add
# enough Python loop overhead to cause SDR ring-buffer overflow at 2.36 MSPS
# when combined with SCM+/SCM.  Use --protocol idm / --protocol netidm to
# decode those explicitly.
_DEFAULT_PROTOCOLS = {"scmplus", "scm", "idm", "netidm"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Multi-protocol ERT smart meter receiver")
    p.add_argument(
        "--config",
        default="/etc/meter-reading/rtlamr.toml",
        metavar="PATH",
        help="TOML config file (default: /etc/meter-reading/rtlamr.toml)",
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
        choices=list(_MANCHESTER_PROTOCOLS) + ["r900"],
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


def _load_config(path: str) -> dict:
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
    chip = args.chip_length

    # Build the list of active decoders.
    selected = set(args.protocol) if args.protocol else None
    use_r900 = "r900" in selected if selected else False
    manchester_names = (selected - {"r900"}) if selected else set(_DEFAULT_PROTOCOLS)

    manchester_protos = {
        k: v for k, v in _MANCHESTER_PROTOCOLS.items()
        if k in manchester_names
    }

    decoders: list[Decoder] = []
    for name, (make_cfg, parser) in manchester_protos.items():
        cfg = make_cfg(chip)
        LOG.info(
            "Protocol %s: chip_length=%d sample_rate=%d center_freq=%d block_size=%d",
            name, cfg.chip_length, cfg.sample_rate, cfg.center_freq, cfg.block_size,
        )
        decoders.append(Decoder(cfg, parser))

    r900_decoder: R900Decoder | None = None
    if use_r900:
        r900_decoder = R900Decoder(chip)
        LOG.info(
            "Protocol r900: chip_length=%d sample_rate=%d center_freq=%d",
            chip, r900_decoder.sample_rate, r900_decoder.center_freq,
        )

    if not decoders and r900_decoder is None:
        LOG.error("No protocols selected.")
        return 1

    # Determine center_freq and sample_rate for the SDR source.
    # Alternating mode starts on Manchester; R900-only starts on R900.
    if decoders:
        center_freq = decoders[0].cfg.center_freq
        sample_rate = decoders[0].cfg.sample_rate
    else:
        center_freq = r900_decoder.center_freq
        sample_rate = r900_decoder.sample_rate

    source = open_source(
        center_freq=center_freq,
        sample_rate=sample_rate,
        gain=args.gain,
        ppm=args.freq_correction,
        sample_file=args.sample_file,
    )

    poster = ApiPoster(args.api_url, args.api_key) if args.api_url else None

    # Graceful shutdown on SIGINT.
    running = True

    def _stop(signum, frame):
        nonlocal running
        LOG.info("Received signal %s, stopping.", signum)
        running = False

    signal.signal(signal.SIGINT, _stop)

    start = time.monotonic()

    all_decoder_objs = decoders + ([r900_decoder] if r900_decoder else [])
    min_block = min(d.block_size2 for d in all_decoder_objs)
    read_size = min_block * 8
    buffers: dict[int, bytearray] = defaultdict(bytearray)

    proto_names = list(manchester_protos) + (["r900"] if r900_decoder else [])
    _stats: dict[int, dict] = {
        id(d): {"name": name, "blocks": 0, "messages": 0}
        for d, name in zip(all_decoder_objs, proto_names)
    }
    _total_chunks = 0
    _STATS_INTERVAL = 500
    _HEARTBEAT_INTERVAL = 30
    _last_heartbeat = start

    def _emit_record(record, d) -> bool:
        """Apply meter_id filter, write to stdout, post to API. Returns True if forwarded."""
        endpoint_id = record.get("endpoint_id")
        if args.meter_id is not None and endpoint_id not in args.meter_id:
            return False
        _stats[id(d)]["messages"] += 1
        sys.stdout.write(json.dumps(record) + "\n")
        sys.stdout.flush()
        if poster:
            poster.submit(record)
        return True

    def _drain_decoder(d) -> bool:
        """Process all complete blocks buffered for *d*. Returns True if any message forwarded."""
        buf = buffers[id(d)]
        forwarded = False
        while len(buf) >= d.block_size2:
            block_bytes = bytes(buf[: d.block_size2])
            del buf[: d.block_size2]
            _stats[id(d)]["blocks"] += 1
            for msg in d.decode(block_bytes):
                record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                record.update(msg.as_dict())
                if _emit_record(record, d):
                    forwarded = True
        return forwarded

    alternating = bool(decoders) and r900_decoder is not None

    try:
        if alternating:
            _MAN_FREQ = decoders[0].cfg.center_freq
            _R900_FREQ = r900_decoder.center_freq
            _SETTLE_BLOCKS = 4  # blocks to discard after retuning (~14 ms)

            def _retune(new_freq, reset_targets):
                source.set_center_freq(new_freq)
                for _ in range(_SETTLE_BLOCKS):
                    source.read_block(read_size)
                for d in reset_targets:
                    d.reset()
                    buffers[id(d)].clear()

            mode = "manchester"

            while running:
                if args.duration > 0 and (time.monotonic() - start) >= args.duration:
                    LOG.info("Duration reached.")
                    break

                active = decoders if mode == "manchester" else [r900_decoder]
                mode_deadline = time.monotonic() + args.switch_timeout
                found = False

                while running and not found:
                    if time.monotonic() >= mode_deadline:
                        LOG.info("Switch timeout on %s — switching.", mode)
                        break

                    chunk = source.read_block(read_size)
                    if not chunk:
                        running = False
                        break
                    _total_chunks += 1

                    for d in active:
                        buffers[id(d)] += chunk
                        if _drain_decoder(d):
                            found = True

                now = time.monotonic()
                if args.verbose and now - _last_heartbeat >= _HEARTBEAT_INTERVAL:
                    elapsed = now - start
                    total_printed = sum(s["messages"] for s in _stats.values())
                    LOG.info(
                        "listening… t=%ds chunks=%d messages=%d mode=%s",
                        elapsed, _total_chunks, total_printed, mode,
                    )
                    _last_heartbeat = now

                if mode == "manchester":
                    mode = "r900"
                    LOG.info("Switching to R900 mode (center_freq=%d).", _R900_FREQ)
                    _retune(_R900_FREQ, [r900_decoder])
                else:
                    mode = "manchester"
                    LOG.info("Switching to Manchester mode (center_freq=%d).", _MAN_FREQ)
                    _retune(_MAN_FREQ, decoders)

        else:
            # Normal loop: all selected decoders run on the same frequency concurrently.
            while running:
                if args.duration > 0 and (time.monotonic() - start) >= args.duration:
                    LOG.info("Duration reached.")
                    break

                chunk = source.read_block(read_size)
                if not chunk:
                    break

                _total_chunks += 1

                for d in all_decoder_objs:
                    buffers[id(d)] += chunk
                    _drain_decoder(d)

                now = time.monotonic()
                if args.verbose and now - _last_heartbeat >= _HEARTBEAT_INTERVAL:
                    elapsed = now - start
                    total_printed = sum(s["messages"] for s in _stats.values())
                    LOG.info(
                        "listening… t=%ds chunks=%d messages=%d",
                        elapsed, _total_chunks, total_printed,
                    )
                    _last_heartbeat = now

                if args.verbose and _total_chunks % _STATS_INTERVAL == 0:
                    elapsed = time.monotonic() - start
                    parts = [f"t={elapsed:.0f}s chunks={_total_chunks}"]
                    for d, s in zip(all_decoder_objs, _stats.values()):
                        ds = getattr(d, "stats", {})
                        parts.append(
                            f"{s['name']}:blocks={s['blocks']}"
                            f",cands={ds.get('candidates', '?')}"
                            f",ok={ds.get('parse_ok', '?')}"
                            f",dedup={ds.get('dedup_drop', '?')}"
                            f",printed={s['messages']}"
                        )
                    LOG.info("stats: %s", " | ".join(parts))

    finally:
        source.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
