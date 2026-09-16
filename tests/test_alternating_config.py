"""Tests for multi-protocol _apply_config normalization."""
import argparse
from main import _apply_config, _DEFAULTS, _CONFIG_KEYS


def _args(**kw):
    ns = argparse.Namespace(**{k: None for k in _CONFIG_KEYS})
    ns.__dict__.update(kw)
    return ns


# ── protocol normalization ────────────────────────────────────────────────────

def test_protocol_string_normalized_to_list():
    args = _args()
    _apply_config(args, {"protocol": "scmplus"})
    assert args.protocol == ["scmplus"]


def test_protocol_comma_string_split():
    args = _args()
    _apply_config(args, {"protocol": "scmplus,r900"})
    assert args.protocol == ["scmplus", "r900"]


def test_protocol_comma_string_with_spaces():
    args = _args()
    _apply_config(args, {"protocol": "scmplus, r900"})
    assert args.protocol == ["scmplus", "r900"]


def test_protocol_toml_array_passed_through():
    args = _args()
    _apply_config(args, {"protocol": ["scmplus", "r900"]})
    assert args.protocol == ["scmplus", "r900"]


def test_protocol_cli_not_overwritten_by_toml():
    args = _args(protocol=["scm"])
    _apply_config(args, {"protocol": ["scmplus", "r900"]})
    assert args.protocol == ["scm"]


def test_protocol_absent_stays_none():
    args = _args()
    _apply_config(args, {})
    assert args.protocol is None


# ── switch_timeout default ────────────────────────────────────────────────────

def test_switch_timeout_default_applied():
    args = _args()
    _apply_config(args, {})
    assert args.switch_timeout == 60.0


def test_switch_timeout_from_toml():
    args = _args()
    _apply_config(args, {"switch_timeout": 30.0})
    assert args.switch_timeout == 30.0


def test_switch_timeout_cli_not_overwritten():
    args = _args(switch_timeout=15.0)
    _apply_config(args, {"switch_timeout": 30.0})
    assert args.switch_timeout == 15.0
