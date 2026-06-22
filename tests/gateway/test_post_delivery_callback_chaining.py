"""Tests for ``BasePlatformAdapter.register_post_delivery_callback`` chaining.

When two features want to run after the final response lands on the same
session (e.g. background-review release + temporary-progress cleanup), the
registration API chains them rather than clobbering. Per-callback
exceptions are swallowed so one bad callback can't sabotage the others.
Stale-generation registrations are rejected.

Chained callbacks are awaited: either side of a chain may be a coroutine
function (e.g. the V0-E auto context-checkpoint notice delivery), so the
chained wrapper is ``async`` and awaits each sub-callback. Invoking it
without awaiting would orphan the async sub-callback's coroutine
("coroutine ... was never awaited") and silently skip the delivery.
"""
import gc
import inspect
import warnings

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult


class _MinAdapter(BasePlatformAdapter):
    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


@pytest.fixture
def adapter():
    return _MinAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)


class TestPostDeliveryCallbackChaining:
    def test_single_callback_fires(self, adapter):
        fired = []
        adapter.register_post_delivery_callback("s", lambda: fired.append("A"))
        cb = adapter.pop_post_delivery_callback("s")
        cb()
        assert fired == ["A"]

    @pytest.mark.asyncio
    async def test_two_callbacks_chain_in_order(self, adapter):
        fired = []
        adapter.register_post_delivery_callback("s", lambda: fired.append("A"))
        adapter.register_post_delivery_callback("s", lambda: fired.append("B"))
        cb = adapter.pop_post_delivery_callback("s")
        await cb()
        assert fired == ["A", "B"]

    @pytest.mark.asyncio
    async def test_three_callbacks_chain_in_order(self, adapter):
        """Chain composes over an already-chained callback."""
        fired = []
        for label in ("A", "B", "C"):
            adapter.register_post_delivery_callback(
                "s", lambda x=label: fired.append(x)
            )
        cb = adapter.pop_post_delivery_callback("s")
        await cb()
        assert fired == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_exception_in_one_callback_does_not_block_next(self, adapter):
        fired = []

        def boom():
            raise ValueError("boom")

        adapter.register_post_delivery_callback("s", boom)
        adapter.register_post_delivery_callback("s", lambda: fired.append("survived"))
        cb = adapter.pop_post_delivery_callback("s")
        await cb()
        assert fired == ["survived"]

    @pytest.mark.asyncio
    async def test_same_generation_chains(self, adapter):
        fired = []
        adapter.register_post_delivery_callback(
            "s", lambda: fired.append("A"), generation=5
        )
        adapter.register_post_delivery_callback(
            "s", lambda: fired.append("B"), generation=5
        )
        cb = adapter.pop_post_delivery_callback("s", generation=5)
        await cb()
        assert fired == ["A", "B"]

    def test_stale_generation_registration_rejected(self, adapter):
        """A registration with an older generation than the existing
        entry is rejected — it doesn't clobber the newer run's slot."""
        fired = []
        adapter.register_post_delivery_callback(
            "s", lambda: fired.append("gen7"), generation=7
        )
        adapter.register_post_delivery_callback(
            "s", lambda: fired.append("stale_gen3"), generation=3
        )
        cb = adapter.pop_post_delivery_callback("s", generation=7)
        cb()
        assert fired == ["gen7"]

    def test_pop_at_wrong_generation_returns_none(self, adapter):
        adapter.register_post_delivery_callback(
            "s", lambda: None, generation=5
        )
        assert adapter.pop_post_delivery_callback("s", generation=99) is None
        # Correct generation still finds it.
        assert adapter.pop_post_delivery_callback("s", generation=5) is not None

    def test_empty_session_key_is_noop(self, adapter):
        adapter.register_post_delivery_callback("", lambda: None)
        assert adapter._post_delivery_callbacks == {}

    def test_non_callable_is_noop(self, adapter):
        adapter.register_post_delivery_callback("s", "not-callable")  # type: ignore[arg-type]
        assert adapter._post_delivery_callbacks == {}

    @pytest.mark.asyncio
    async def test_chained_async_callback_is_awaited_not_orphaned(self, adapter):
        """Regression (V0-E1): chaining a sync callback with an async one — as
        happens when the auto context-checkpoint notice is registered behind an
        existing post-delivery callback — must yield an awaitable that actually
        runs the async delivery. The old sync ``_chained`` invoked the coroutine
        function without awaiting it, leaving the notice undelivered and emitting
        "coroutine ... was never awaited".
        """
        fired = []

        async def async_notice():
            fired.append("async-delivered")

        adapter.register_post_delivery_callback("s", lambda: fired.append("sync"))
        adapter.register_post_delivery_callback("s", async_notice)
        cb = adapter.pop_post_delivery_callback("s")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = cb()
            # Call sites gate delivery on ``inspect.isawaitable`` — the chained
            # wrapper must report as awaitable or the notice is silently dropped.
            assert inspect.isawaitable(result)
            await result
            gc.collect()

        assert fired == ["sync", "async-delivered"]
        never_awaited = [
            w for w in caught
            if issubclass(w.category, RuntimeWarning)
            and "never awaited" in str(w.message)
        ]
        assert not never_awaited, [str(w.message) for w in never_awaited]

    @pytest.mark.asyncio
    async def test_chained_async_first_then_sync_both_run(self, adapter):
        """Order-independence: an async callback registered first still gets
        awaited when a sync callback is chained after it."""
        fired = []

        async def async_first():
            fired.append("async")

        adapter.register_post_delivery_callback("s", async_first)
        adapter.register_post_delivery_callback("s", lambda: fired.append("sync"))
        cb = adapter.pop_post_delivery_callback("s")
        await cb()
        assert fired == ["async", "sync"]
