"""Library entry points for listening to ERT smart meter transmissions.

This module contains the single core SDR read/decode loop used by every
public API this package exposes:

  listen()          — generator; yields each decoded reading as a dict
  listen_once()      — blocks until exactly one reading decodes, returns it
  start_listening()  — runs listen() on a background thread, calls a callback

The CLI (rtlamr_python.cli) is a thin wrapper around listen() that adds
argument parsing, JSON-lines stdout output, and REST API posting.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator

from rtlamr_python.decoder import Config, Decoder
from rtlamr_python.protocols import idm, netidm, scm, scmplus
from rtlamr_python.r900_decoder import R900Decoder
from rtlamr_python.sdr import open_source

LOG = logging.getLogger(__name__)

_MANCHESTER_PROTOCOLS = {
    "scmplus": (scmplus.make_config, scmplus.parse),
    "scm":     (scm.make_config,     scm.parse),
    "idm":     (idm.make_config,     idm.parse),
    "netidm":  (netidm.make_config,  netidm.parse),
}

# Default set omits IDM/NetIDM — their large block sizes (16 384 bytes) add
# enough Python loop overhead to cause SDR ring-buffer overflow at 2.36 MSPS
# when combined with SCM+/SCM.  Pass protocols=["idm"] / ["netidm"] to
# decode those explicitly.
DEFAULT_PROTOCOLS = ("scmplus", "scm", "idm", "netidm")

ALL_PROTOCOLS = tuple(_MANCHESTER_PROTOCOLS) + ("r900",)

_STATS_INTERVAL = 500
_HEARTBEAT_INTERVAL = 30


def listen(
    protocols: Iterable[str] | None = None,
    meter_id: Iterable[int] | None = None,
    chip_length: int = 72,
    gain: str | float = "auto",
    freq_correction: int = 0,
    sample_file: str | None = None,
    switch_timeout: float = 60.0,
    duration: float = 0.0,
    verbose: bool = False,
    stop_event: threading.Event | None = None,
) -> Iterator[dict]:
    """Read from an RTL-SDR dongle (or *sample_file*), decode packets, and
    yield each matching reading as a dict.

    This opens the SDR (or sample file) on first iteration and closes it
    when the generator is exhausted, closed (``gen.close()``), or garbage
    collected — so use it in a ``for`` loop, or call ``.close()`` explicitly
    if you stop consuming it early.

    Args:
      protocols: protocol names to decode, e.g. ``["scmplus", "r900"]``.
        Defaults to all Manchester-encoded protocols (scmplus, scm, idm,
        netidm). Include "r900" to also (or only) decode R900 water meters;
        combining it with any Manchester protocol switches between the two
        center frequencies every *switch_timeout* seconds of silence.
      meter_id: if given, only readings from these endpoint IDs are yielded.
      chip_length: samples per chip (default 72 → ~2.36 MHz sample rate).
      gain: tuner gain in dB, or "auto".
      freq_correction: frequency correction in parts per million.
      sample_file: read raw IQ bytes from this file instead of live hardware.
      switch_timeout: alternating mode — switch frequency after this many
        seconds without a message.
      duration: stop after this many seconds (0 = run forever).
      verbose: log periodic heartbeat/stats messages.
      stop_event: if given, checked once per read/decode cycle; set it from
        another thread to stop the loop.

    Yields:
      dict records, e.g. ``{"time": "...", "type": "SCM+", "endpoint_id": 123,
      "endpoint_type": 7, "consumption": 456, "tamper": "0x00", ...}``.
    """
    protocols = list(protocols) if protocols is not None else list(DEFAULT_PROTOCOLS)
    unknown = set(protocols) - set(ALL_PROTOCOLS)
    if unknown:
        raise ValueError(f"Unknown protocol(s): {sorted(unknown)} (known: {ALL_PROTOCOLS})")
    meter_ids = set(meter_id) if meter_id is not None else None

    use_r900 = "r900" in protocols
    manchester_names = [p for p in protocols if p != "r900"]
    manchester_protos = {k: _MANCHESTER_PROTOCOLS[k] for k in manchester_names}

    decoders: list[Decoder] = []
    for name, (make_cfg, parser) in manchester_protos.items():
        cfg = make_cfg(chip_length)
        LOG.info(
            "Protocol %s: chip_length=%d sample_rate=%d center_freq=%d block_size=%d",
            name, cfg.chip_length, cfg.sample_rate, cfg.center_freq, cfg.block_size,
        )
        decoders.append(Decoder(cfg, parser))

    r900_decoder: R900Decoder | None = None
    if use_r900:
        r900_decoder = R900Decoder(chip_length)
        LOG.info(
            "Protocol r900: chip_length=%d sample_rate=%d center_freq=%d",
            chip_length, r900_decoder.sample_rate, r900_decoder.center_freq,
        )

    if not decoders and r900_decoder is None:
        raise ValueError("No protocols selected.")

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
        gain=gain,
        ppm=freq_correction,
        sample_file=sample_file,
    )

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
    _last_heartbeat = start

    def _running() -> bool:
        return stop_event is None or not stop_event.is_set()

    def _drain_decoder(d) -> Iterator[dict]:
        """Yield a dict for each message decoded from *d*'s buffered blocks."""
        buf = buffers[id(d)]
        while len(buf) >= d.block_size2:
            block_bytes = bytes(buf[: d.block_size2])
            del buf[: d.block_size2]
            _stats[id(d)]["blocks"] += 1
            for msg in d.decode(block_bytes):
                record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                record.update(msg.as_dict())
                if meter_ids is not None and record.get("endpoint_id") not in meter_ids:
                    continue
                _stats[id(d)]["messages"] += 1
                yield record

    def _log_stats_if_due(mode: str | None) -> None:
        nonlocal _last_heartbeat
        if not verbose:
            return
        now = time.monotonic()
        if now - _last_heartbeat >= _HEARTBEAT_INTERVAL:
            elapsed = now - start
            total_printed = sum(s["messages"] for s in _stats.values())
            if mode:
                LOG.info(
                    "listening… t=%ds chunks=%d messages=%d mode=%s",
                    elapsed, _total_chunks, total_printed, mode,
                )
            else:
                LOG.info(
                    "listening… t=%ds chunks=%d messages=%d",
                    elapsed, _total_chunks, total_printed,
                )
            _last_heartbeat = now
        if mode is None and _total_chunks % _STATS_INTERVAL == 0:
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

            while _running():
                if duration > 0 and (time.monotonic() - start) >= duration:
                    LOG.info("Duration reached.")
                    break

                active = decoders if mode == "manchester" else [r900_decoder]
                mode_deadline = time.monotonic() + switch_timeout
                found = False

                while _running() and not found:
                    if time.monotonic() >= mode_deadline:
                        LOG.info("Switch timeout on %s — switching.", mode)
                        break

                    chunk = source.read_block(read_size)
                    if not chunk:
                        return
                    _total_chunks += 1

                    for d in active:
                        buffers[id(d)] += chunk
                        for record in _drain_decoder(d):
                            found = True
                            yield record

                    _log_stats_if_due(mode)

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
            while _running():
                if duration > 0 and (time.monotonic() - start) >= duration:
                    LOG.info("Duration reached.")
                    break

                chunk = source.read_block(read_size)
                if not chunk:
                    break

                _total_chunks += 1

                for d in all_decoder_objs:
                    buffers[id(d)] += chunk
                    yield from _drain_decoder(d)

                _log_stats_if_due(None)

    finally:
        source.close()


def listen_once(**kwargs) -> dict:
    """Block until exactly one matching reading decodes, then return it.

    Opens the SDR (or sample file), closes it before returning. Accepts the
    same keyword arguments as listen(), except *duration* and *stop_event*
    (which would risk returning nothing).
    """
    kwargs.pop("duration", None)
    kwargs.pop("stop_event", None)
    gen = listen(**kwargs)
    try:
        return next(gen)
    finally:
        gen.close()


class ListenerHandle:
    """Handle returned by start_listening(); use it to stop the background thread."""

    def __init__(self, thread: threading.Thread, stop_event: threading.Event) -> None:
        self._thread = thread
        self._stop_event = stop_event

    def stop(self, timeout: float | None = None) -> None:
        """Signal the listener to stop and wait for its thread to exit."""
        self._stop_event.set()
        self._thread.join(timeout)

    def is_running(self) -> bool:
        return self._thread.is_alive()


def start_listening(
    on_message: Callable[[dict], None],
    **kwargs,
) -> ListenerHandle:
    """Run listen() on a background daemon thread, calling on_message(reading)
    for each decoded reading. Returns a ListenerHandle; call .stop() on it to
    end the loop.

    Accepts the same keyword arguments as listen(), except *stop_event*
    (start_listening manages its own).
    """
    kwargs.pop("stop_event", None)
    stop_event = threading.Event()

    def _run() -> None:
        for record in listen(stop_event=stop_event, **kwargs):
            on_message(record)

    thread = threading.Thread(target=_run, daemon=True, name="rtlamr-listener")
    thread.start()
    return ListenerHandle(thread, stop_event)
