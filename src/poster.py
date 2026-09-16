"""HTTP poster — ships decoded meter readings to the REST API in the background.

The main SDR loop calls ApiPoster.submit(record) which is non-blocking.
A daemon thread drains the internal queue and POSTs each reading.
Errors are logged but never propagated to the caller.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.error
import urllib.request
from typing import Optional

LOG = logging.getLogger(__name__)


def _to_payload(record: dict) -> dict | None:
    """Map a decoder record dict to a MeterReadingIn payload dict.

    Returns None if a required field is missing.
    """
    consumption = record.get("consumption") or record.get("last_consumption_count")
    if consumption is None:
        return None

    tamper: int | None = None
    if "tamper" in record and record["tamper"] is not None:
        raw = record["tamper"]
        tamper = int(raw, 16) if isinstance(raw, str) else int(raw)
    elif "tamper_phy" in record or "tamper_enc" in record:
        tamper = (record.get("tamper_phy", 0) << 2) | record.get("tamper_enc", 0)

    return {
        "timestamp": record["time"],
        "endpoint_id": record["endpoint_id"],
        "protocol": record.get("type", ""),
        "endpoint_type": str(raw_et) if (raw_et := record.get("endpoint_type")) is not None else record.get("type", ""),
        "consumption": consumption,
        "tamper": tamper,
    }


class ApiPoster:
    """Daemon thread that POSTs meter readings to the API without blocking the SDR loop."""

    def __init__(self, api_url: str, api_key: Optional[str] = None):
        self._url = api_url
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            self._headers["X-API-Key"] = api_key

        self._queue: queue.Queue[dict] = queue.Queue(maxsize=1000)
        self._thread = threading.Thread(target=self._run, daemon=True, name="api-poster")
        self._thread.start()
        LOG.info("API poster started → %s", self._url)

    def submit(self, record: dict) -> None:
        """Non-blocking enqueue. Drops silently if the queue is full."""
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            LOG.warning(
                "API poster queue full — dropping reading for endpoint %s",
                record.get("endpoint_id"),
            )

    def _run(self) -> None:
        while True:
            self._post(self._queue.get())

    def _post(self, record: dict) -> None:
        payload = _to_payload(record)
        if payload is None:
            return
        try:
            req = urllib.request.Request(
                self._url,
                data=json.dumps(payload).encode(),
                headers=self._headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status not in (200, 201):
                    LOG.warning(
                        "API returned HTTP %d for endpoint %s",
                        resp.status,
                        payload["endpoint_id"],
                    )
        except urllib.error.URLError as exc:
            LOG.warning("API post failed for endpoint %s: %s", payload["endpoint_id"], exc)
        except Exception as exc:
            LOG.warning("Unexpected error posting to API: %s", exc)
