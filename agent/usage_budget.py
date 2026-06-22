"""Per-task usage budget guard (V0-E2) — default-off iteration / token cap.

The agent's existing :class:`~agent.iteration_budget.IterationBudget` (default
90) and context compression keep a single conversation healthy, but a long
autonomous task can still burn 90 iterations and a large cumulative prompt-token
bill before any safety net fires. :class:`UsageBudget` is a **separate,
opt-in** guard checked *before every provider call*:

- ``max_iterations`` — stop before making more than N provider calls per task.
- ``max_prompt_tokens`` — stop once the cumulative prompt tokens billed across
  the task reach the cap.

It is **default off**: a ``UsageBudget()`` with no caps (or ``enabled=False``)
never stops anything, so the agent behaves exactly as before. When a cap is
reached the conversation loop stops cleanly — it does not make another provider
call and does not retry. The gateway then renders the existing read-only
Cogitator checkpoint (if configured) and delivers a separate, non-persisted
handoff notice. The guard never resets, rotates, injects, writes storage, or
runs ``/new`` — that stays manual.

"Per task" = one ``run_conversation`` invocation. The iteration count is the
loop's own ``api_call_count`` (reset per task); the cumulative prompt-token
count lives here and is zeroed via :meth:`reset` at the start of each task and
fed via :meth:`record_prompt_tokens` after each provider response.
"""

from __future__ import annotations

import threading


def _coerce_cap(value) -> int:
    """Coerce a configured cap to a non-negative int; 0 (or invalid) = disabled."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


class UsageBudget:
    """Thread-safe, default-off per-task iteration / prompt-token budget.

    Args:
        max_iterations: stop before the (N+1)-th provider call this task.
            ``0`` / ``None`` disables the iteration cap.
        max_prompt_tokens: stop once cumulative prompt tokens reach this value.
            ``0`` / ``None`` disables the token cap.

    With both caps disabled (the default), the guard is inert.
    """

    def __init__(self, max_iterations=0, max_prompt_tokens=0):
        self.max_iterations = _coerce_cap(max_iterations)
        self.max_prompt_tokens = _coerce_cap(max_prompt_tokens)
        self._prompt_tokens_used = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """True when at least one cap is active."""
        return self.max_iterations > 0 or self.max_prompt_tokens > 0

    def reset(self) -> None:
        """Zero the cumulative prompt-token counter for a new task.

        Caps are unchanged; only per-task accounting resets. The iteration
        count is the loop's own ``api_call_count`` and resets independently.
        """
        with self._lock:
            self._prompt_tokens_used = 0

    def record_prompt_tokens(self, prompt_tokens) -> None:
        """Add the prompt tokens billed by one provider response to the task total."""
        try:
            amount = int(prompt_tokens)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        with self._lock:
            self._prompt_tokens_used += amount

    @property
    def prompt_tokens_used(self) -> int:
        with self._lock:
            return self._prompt_tokens_used

    def exceeded(self, api_call_count: int):
        """Return a stop reason BEFORE the next provider call, or ``None``.

        ``api_call_count`` is the number of provider calls already completed
        this task (the loop checks this before incrementing for the next call).
        Returns ``"iteration_cap"`` or ``"token_cap"`` when a cap is reached, so
        the loop can stop before issuing another call. Returns ``None`` when the
        guard is disabled or no cap has been reached — preserving today's
        behavior.
        """
        if not self.enabled:
            return None
        if self.max_iterations > 0 and api_call_count >= self.max_iterations:
            return "iteration_cap"
        if self.max_prompt_tokens > 0:
            with self._lock:
                used = self._prompt_tokens_used
            if used >= self.max_prompt_tokens:
                return "token_cap"
        return None


__all__ = ["UsageBudget"]
