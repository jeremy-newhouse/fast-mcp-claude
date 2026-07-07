"""Unit tests for the limits triple (no SDK, no container needed)."""

from __future__ import annotations

import pytest

from sandbox_runner.limits import (
    DEFAULT_MAX_BUDGET_USD,
    DEFAULT_MAX_TURNS,
    DEFAULT_WALL_CLOCK_S,
    MIN_BUDGET_USD,
    Limits,
)


def test_defaults_match_worker_supervisor():
    lim = Limits()
    assert lim.wall_clock_s == DEFAULT_WALL_CLOCK_S == 1800
    assert lim.max_turns == DEFAULT_MAX_TURNS == 50
    assert lim.max_budget_usd == DEFAULT_MAX_BUDGET_USD == 10.0


def test_from_spec_none_and_empty_are_defaults():
    assert Limits.from_spec(None) == Limits()
    assert Limits.from_spec({}) == Limits()


def test_from_spec_overrides_and_coerces_types():
    lim = Limits.from_spec({"wall_clock_s": "60", "max_turns": 3, "max_budget_usd": "0.25"})
    assert lim.wall_clock_s == 60
    assert lim.max_turns == 3
    assert lim.max_budget_usd == 0.25


def test_from_spec_ignores_unknown_keys():
    lim = Limits.from_spec({"max_turns": 2, "bogus": "x"})
    assert lim.max_turns == 2
    assert lim.wall_clock_s == DEFAULT_WALL_CLOCK_S


def test_partial_none_values_fall_back_to_default():
    lim = Limits.from_spec({"wall_clock_s": None, "max_turns": 7})
    assert lim.wall_clock_s == DEFAULT_WALL_CLOCK_S
    assert lim.max_turns == 7


@pytest.mark.parametrize(
    "spec",
    [
        {"wall_clock_s": 0},
        {"max_turns": 0},
        {"max_budget_usd": 0},
        {"wall_clock_s": -5},
    ],
)
def test_nonpositive_limits_rejected(spec):
    with pytest.raises(ValueError):
        Limits.from_spec(spec)


def test_sdk_budget_floor_and_rounding():
    assert Limits(max_budget_usd=0.001).sdk_budget_usd == MIN_BUDGET_USD
    assert Limits(max_budget_usd=1.234567).sdk_budget_usd == 1.2346


def test_as_dict_roundtrips_through_from_spec():
    lim = Limits(wall_clock_s=120, max_turns=4, max_budget_usd=2.5)
    assert Limits.from_spec(lim.as_dict()) == lim
