"""Tests for the Usage Budget Guard (V0-E2) primitive — ``agent.usage_budget``.

``UsageBudget.exceeded(api_call_count)`` is the decision the conversation loop
checks *before every provider call*: a non-None return makes the loop stop
cleanly before issuing another call (and without retrying). These tests pin that
decision down at every boundary, including the default-off behavior that must
preserve today's runs.
"""
import pytest

from agent.usage_budget import UsageBudget


class TestDefaultOff:
    def test_no_args_is_disabled(self):
        b = UsageBudget()
        assert b.enabled is False
        # Disabled guard never stops anything, at any call count or token total.
        assert b.exceeded(0) is None
        assert b.exceeded(10_000) is None
        b.record_prompt_tokens(10_000_000)
        assert b.exceeded(10_000) is None

    def test_zero_caps_are_disabled(self):
        b = UsageBudget(max_iterations=0, max_prompt_tokens=0)
        assert b.enabled is False
        assert b.exceeded(99) is None

    def test_negative_and_invalid_caps_coerce_to_disabled(self):
        assert UsageBudget(max_iterations=-5).enabled is False
        assert UsageBudget(max_prompt_tokens=-1).enabled is False
        assert UsageBudget(max_iterations="nope", max_prompt_tokens=None).enabled is False


class TestIterationCap:
    def test_stops_when_call_count_reaches_cap(self):
        """Iteration cap stops another provider call: at api_call_count == cap
        (cap calls already made), the next call is refused."""
        b = UsageBudget(max_iterations=3)
        assert b.enabled is True
        assert b.exceeded(0) is None   # before any call
        assert b.exceeded(1) is None   # 1 call made, room for more
        assert b.exceeded(2) is None   # 2 made
        assert b.exceeded(3) == "iteration_cap"   # 3 made -> stop the 4th
        assert b.exceeded(4) == "iteration_cap"   # and beyond

    def test_cap_of_one_allows_exactly_one_call(self):
        b = UsageBudget(max_iterations=1)
        assert b.exceeded(0) is None
        assert b.exceeded(1) == "iteration_cap"


class TestTokenCap:
    def test_stops_when_cumulative_prompt_tokens_reach_cap(self):
        """Token cap stops another provider call once cumulative prompt tokens
        recorded across the task reach the configured cap."""
        b = UsageBudget(max_prompt_tokens=1000)
        assert b.exceeded(0) is None
        b.record_prompt_tokens(400)
        assert b.exceeded(5) is None          # 400 < 1000
        b.record_prompt_tokens(600)
        assert b.exceeded(5) == "token_cap"   # 1000 >= 1000 -> stop
        b.record_prompt_tokens(100)
        assert b.exceeded(5) == "token_cap"   # stays stopped

    def test_record_ignores_nonpositive_and_invalid(self):
        b = UsageBudget(max_prompt_tokens=100)
        b.record_prompt_tokens(0)
        b.record_prompt_tokens(-50)
        b.record_prompt_tokens(None)
        b.record_prompt_tokens("x")
        assert b.prompt_tokens_used == 0
        assert b.exceeded(0) is None

    def test_reset_zeroes_token_counter_but_keeps_cap(self):
        b = UsageBudget(max_prompt_tokens=500)
        b.record_prompt_tokens(500)
        assert b.exceeded(0) == "token_cap"
        b.reset()
        assert b.prompt_tokens_used == 0
        assert b.exceeded(0) is None
        assert b.enabled is True  # cap survives reset


class TestBothCaps:
    def test_iteration_cap_takes_precedence_when_both_hit(self):
        b = UsageBudget(max_iterations=2, max_prompt_tokens=100)
        b.record_prompt_tokens(100)
        # Both are exceeded; iteration_cap is reported first (checked first).
        assert b.exceeded(2) == "iteration_cap"

    def test_token_cap_fires_when_only_tokens_exceeded(self):
        b = UsageBudget(max_iterations=10, max_prompt_tokens=100)
        b.record_prompt_tokens(100)
        assert b.exceeded(1) == "token_cap"

    def test_enabled_with_either_cap(self):
        assert UsageBudget(max_iterations=5).enabled is True
        assert UsageBudget(max_prompt_tokens=5).enabled is True
