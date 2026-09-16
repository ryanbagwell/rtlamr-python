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

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
