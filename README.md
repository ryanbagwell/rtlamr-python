# rtlamr-python

A Python implementation of [rtlamr](https://github.com/bemasher/rtlamr) — a multi-protocol
ERT (Encoder Receiver Transmitter) smart meter receiver. Reads IQ samples from an RTL-SDR
dongle (or a raw sample file), decodes ERT packets, and prints each message as a JSON line
to stdout.

## Supported protocols

| Protocol  | Description                                                    |
|-----------|-----------------------------------------------------------------|
| `scmplus` | Standard Consumption Message Plus (16 bytes, most electric meters) |
| `scm`     | Standard Consumption Message (12 bytes, older electric meters)  |
| `idm`     | Interval Data Message (92 bytes, hourly interval data)          |
| `netidm`  | Net Meter Interval Data Message (92 bytes, net-metering variant)|
| `r900`    | Neptune R900 water meters (different center freq: 912.38 MHz)   |

By default, all Manchester-encoded protocols (`scmplus`, `scm`, `idm`, `netidm`) are decoded
simultaneously. `r900` uses a different center frequency and must be selected explicitly
(alone, or alternated with the Manchester set).

## Installation

Requires Python 3.11+ and an RTL-SDR dongle (or [librtlsdr](https://github.com/librtlsdr/librtlsdr) installed) for live capture.

```bash
pip install rtlamr-python
```

This installs an `rtlamr` command on your `PATH`.

## Usage

```bash
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
```

Run `rtlamr --help` for the full list of options (gain, frequency correction, chip length,
posting readings to a REST API, etc.).

### Configuration file

Options can also be supplied via a TOML config file with `--config path/to/rtlamr.toml`.
Command-line flags always take precedence over the config file. Example:

```toml
meter_ids = [12345678, 87654321]
protocol = ["scmplus", "scm"]
gain = "auto"
api_url = "http://localhost:8000/api"
api_key = "secret"
switch_timeout = 60.0
```

## Library usage

`rtlamr-python` can also be used as a library rather than a CLI, e.g. to decode readings
from within another application. All three forms below share the same options as the CLI
(`protocols`, `meter_id`, `chip_length`, `gain`, `freq_correction`, `sample_file`,
`switch_timeout`, `duration`, `verbose`).

### Iterate over readings as they arrive

```python
from rtlamr_python import listen

for reading in listen(protocols=["scmplus"], meter_id=[12345678]):
    print(reading)
    # break whenever you've got what you need — the SDR is closed on exit
```

### Block until a single reading decodes

```python
from rtlamr_python import listen_once

reading = listen_once(protocols=["scmplus"], meter_id=[12345678])
print(reading)  # SDR is already closed here
```

### Run in the background with a callback

```python
from rtlamr_python import start_listening

def on_message(reading):
    print(reading)

handle = start_listening(on_message, protocols=["scmplus"])
...
handle.stop()  # signals the background thread and waits for it to exit
```

Each reading is a dict, e.g.:

```json
{"time": "2026-01-01T00:00:00Z", "type": "SCM+", "endpoint_id": 12345678,
 "endpoint_type": 4, "consumption": 112233, "tamper": "0x0000", "packet_crc": "0x972F"}
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
