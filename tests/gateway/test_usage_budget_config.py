"""Tests for gateway ``_build_usage_budget`` — config → UsageBudget mapping.

Pins the Phase-1 guarantees: the guard is default-off (ordinary chat untouched),
and when enabled it is *bounded* by sane defaults — never the observed 90-iteration
runaway — while still honoring explicit operator overrides.
"""
from gateway.run import _build_usage_budget


class TestUsageBudgetConfig:
    def test_absent_block_is_inert(self):
        assert _build_usage_budget({}).enabled is False
        assert _build_usage_budget({"usage_budget": {}}).enabled is False

    def test_enabled_false_is_inert(self):
        assert _build_usage_budget({"usage_budget": {"enabled": False}}).enabled is False

    def test_enabled_applies_sane_default_caps_never_90(self):
        b = _build_usage_budget({"usage_budget": {"enabled": True}})
        assert b.enabled is True
        # Default behavior is bounded: a hard iteration cap well under 90.
        assert 0 < b.max_iterations < 90
        # And a cumulative prompt-token cap is active by default.
        assert b.max_prompt_tokens > 0

    def test_explicit_values_are_honored(self):
        b = _build_usage_budget(
            {"usage_budget": {"enabled": True, "max_iterations": 12, "max_prompt_tokens": 500_000}}
        )
        assert b.max_iterations == 12
        assert b.max_prompt_tokens == 500_000

    def test_explicit_zero_disables_a_single_cap(self):
        b = _build_usage_budget(
            {"usage_budget": {"enabled": True, "max_iterations": 8, "max_prompt_tokens": 0}}
        )
        assert b.max_iterations == 8
        assert b.max_prompt_tokens == 0   # token cap explicitly off
        assert b.enabled is True          # iteration cap still active

    def test_malformed_block_fails_open_to_disabled(self):
        assert _build_usage_budget({"usage_budget": "nonsense"}).enabled is False
        assert _build_usage_budget({"usage_budget": {"enabled": True, "max_iterations": "x"}}).max_iterations == 0
