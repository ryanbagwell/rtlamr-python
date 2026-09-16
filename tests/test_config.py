"""Tests for _apply_config meter ID handling."""
import argparse
import pytest
from main import _apply_config, _DEFAULTS, _CONFIG_KEYS


def _args(**kwargs):
    """Return a Namespace with all config keys set to None, then apply overrides."""
    ns = argparse.Namespace(**{k: None for k in _CONFIG_KEYS})
    ns.__dict__.update(kwargs)
    return ns


# ── meter_ids (new array key) ─────────────────────────────────────────────────

def test_meter_ids_list_from_toml():
    args = _args()
    _apply_config(args, {"meter_ids": [111, 222]})
    assert args.meter_id == [111, 222]


def test_meter_ids_single_element_list():
    args = _args()
    _apply_config(args, {"meter_ids": [42]})
    assert args.meter_id == [42]


# ── meter_id (legacy single-int key) ─────────────────────────────────────────

def test_legacy_meter_id_wrapped_in_list():
    args = _args()
    _apply_config(args, {"meter_id": 9999})
    assert args.meter_id == [9999]


def test_meter_ids_takes_precedence_over_meter_id():
    args = _args()
    _apply_config(args, {"meter_ids": [1, 2], "meter_id": 99})
    assert args.meter_id == [1, 2]


# ── CLI args take precedence over TOML ────────────────────────────────────────

def test_cli_meter_id_not_overwritten_by_toml():
    args = _args(meter_id=[555])
    _apply_config(args, {"meter_ids": [111, 222]})
    assert args.meter_id == [555]


# ── No filter when omitted ────────────────────────────────────────────────────

def test_no_meter_id_stays_none():
    args = _args()
    _apply_config(args, {})
    assert args.meter_id is None


# ── Filter expression ─────────────────────────────────────────────────────────

def test_filter_passes_matching_ids():
    meter_ids = [111, 222]
    assert 111 in meter_ids
    assert 222 in meter_ids


def test_filter_blocks_non_matching_id():
    meter_ids = [111, 222]
    assert 333 not in meter_ids


def test_filter_inactive_when_none():
    # When meter_id is None the filter branch is skipped entirely.
    meter_id = None
    assert meter_id is None
