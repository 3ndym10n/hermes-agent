"""Tests for the deterministic Virgil repository-task preflight gate.

Proves, in particular, that ``_message_handler`` is NEVER reached on any preflight
failure (fail-closed), and that on success the order is builder -> render -> send ->
``_message_handler``, with the handler called exactly once.

Trigger parsing, the importlib loader (+ provenance/cache-reset), gate orchestration,
and the four-section failure formatter are each exercised. Telegram batching is NOT
re-proved here — that remains the job of test_telegram_text_batching.py.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import gateway.virgil_preflight_gate as gate
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key
from gateway.virgil_preflight_gate import (
    RepoOutcome,
    parse_repo_command,
    reset_preflight_cache_for_tests,
    run_gate,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_gate_state(monkeypatch, tmp_path):
    """Each test starts and ends with a pristine loader cache and no real metrics writes."""
    reset_preflight_cache_for_tests()
    gate._CACHE = None
    fake_root = tmp_path / "default_cogitator_root"
    fake_root.mkdir()
    monkeypatch.setenv("COGITATOR_REPO_ROOT", str(fake_root))
    yield
    reset_preflight_cache_for_tests()
    gate._CACHE = None


class DummyTelegramAdapter(BasePlatformAdapter):
    def __init__(self, send_behavior: str = "ok"):
        super().__init__(PlatformConfig(enabled=True, token="fake-token"), Platform.TELEGRAM)
        self.events: list = []            # ordered log: build/render/send/start_processing/handler
        self.sent: list = []              # every outgoing message
        self.send_behavior = send_behavior  # "ok" | "fail" | "raise"
        self.spawned: list = []
        self._bot = SimpleNamespace(username="hermes_bot")

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    # Override the exact method run_gate / _send_failure call so tests are isolated
    # from base's retry/fallback internals.
    async def _send_with_retry(self, chat_id, content, reply_to=None, metadata=None, **kw):
        self.events.append(("send", content))
        self.sent.append({"chat_id": chat_id, "content": content,
                          "reply_to": reply_to, "metadata": metadata})
        if self.send_behavior == "raise":
            raise RuntimeError("boom send")
        if self.send_behavior == "fail":
            return SendResult(success=False, error="nope")
        return SendResult(success=True, message_id="1")


def _make_event(text: str, *, chat_id="123", message_id="9",
                thread_id=None, platform=Platform.TELEGRAM) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND if text.startswith("/") else MessageType.TEXT,
        source=SessionSource(platform=platform, chat_id=chat_id, chat_type="dm",
                             user_id="u1", thread_id=thread_id),
        message_id=message_id,
    )


def _install_cache(adapter, *, build=None, render=None):
    """Inject (build_fn, render_fn, dirs) directly so the real Cogitator import is bypassed."""
    log = adapter.events

    def _default_build(task, failures_dir=None, patterns_dir=None):
        log.append(("build", task))
        return {"packet_type": "virgil_preflight_v0", "task_description": task}

    def _default_render(packet):
        log.append(("render",))
        return "PREFLIGHT MESSAGE for: " + packet.get("task_description", "")

    gate._CACHE = (build or _default_build, render or _default_render,
                   Path("/x/failures"), Path("/x/patterns"))


def _instrument_normal_path(adapter):
    """Replace the background-spawn entry with a synchronous recorder that invokes the
    handler, so end-to-end ordering through handle_message is observable."""
    async def _handler(event):
        adapter.events.append(("handler", event.text))
        return None

    adapter.set_message_handler(_handler)

    def _fake_start(event, session_key, *, interrupt_event=None):
        adapter.events.append(("start_processing", event.text, event.message_type))
        adapter.spawned.append(asyncio.ensure_future(adapter._message_handler(event)))
        return True

    adapter._start_session_processing = _fake_start


async def _drain(adapter):
    if adapter.spawned:
        await asyncio.gather(*adapter.spawned)


# --------------------------------------------------------------------------- #
# 1. Parser (single source of truth)                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,bot,expected,task", [
    ("hello there", "hermes_bot", RepoOutcome.NO_MATCH, ""),
    ("/status", "hermes_bot", RepoOutcome.NO_MATCH, ""),
    ("/repository do things", "hermes_bot", RepoOutcome.NO_MATCH, ""),
    ("/repo", "hermes_bot", RepoOutcome.MISSING_TASK, ""),
    ("/repo   ", "hermes_bot", RepoOutcome.MISSING_TASK, ""),
    ("/repo fix the bug", "hermes_bot", RepoOutcome.MATCHED, "fix the bug"),
    ("/repo   \t  fix the bug", "hermes_bot", RepoOutcome.MATCHED, "fix the bug"),
    ("/repo\nfix the bug", "hermes_bot", RepoOutcome.MATCHED, "fix the bug"),
    ("   /repo padded", "hermes_bot", RepoOutcome.MATCHED, "padded"),
    ("/repo@hermes_bot do it", "hermes_bot", RepoOutcome.MATCHED, "do it"),
    ("/repo@hermes_bot", "hermes_bot", RepoOutcome.MISSING_TASK, ""),
    ("/repo@OtherBot do it", "hermes_bot", RepoOutcome.NO_MATCH, ""),
    ("/repo@hermes_bot do it", None, RepoOutcome.NO_MATCH, ""),  # unverifiable suffix
])
def test_parse_repo_command(text, bot, expected, task):
    parsed = parse_repo_command(text, bot)
    assert parsed.outcome is expected
    if expected is RepoOutcome.MATCHED:
        assert parsed.task == task


# --------------------------------------------------------------------------- #
# 2. run_gate success + exact ordering                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_run_gate_success_orders_build_render_send():
    adapter = DummyTelegramAdapter()
    _install_cache(adapter)
    event = _make_event("/repo fix it")
    parsed = parse_repo_command(event.text, "hermes_bot")

    ok = await run_gate(adapter, event, parsed)

    assert ok is True
    assert [e[0] for e in adapter.events] == ["build", "render", "send"]
    assert adapter.events[0] == ("build", "fix it")
    assert "PREFLIGHT MESSAGE" in adapter.events[2][1]


@pytest.mark.asyncio
async def test_success_records_delivery_after_send_before_normal_processing(monkeypatch):
    adapter = DummyTelegramAdapter()

    def _build(task, failures_dir=None, patterns_dir=None):
        adapter.events.append(("build", task))
        return {
            "packet_type": "virgil_preflight_v0",
            "task_description": task,
            "loop_health_diagnostics": {
                "source": "cogitator_builder",
                "stage": "built",
                "outcome": "matched",
                "records_considered_by_source_type": {"failures:lesson": 2},
                "records_selected_by_source_type": {"failures:lesson": 1},
            },
        }

    records = []

    def _record(root, payload):
        adapter.events.append(("record", payload.get("stage")))
        records.append(dict(payload))

    monkeypatch.setattr(gate, "_record_loop_health_event", _record, raising=False)
    _install_cache(adapter, build=_build)
    _instrument_normal_path(adapter)
    event = _make_event("/repo fix it", chat_id="chat-secret", message_id="msg-1")

    await adapter.handle_message(event)
    await _drain(adapter)

    assert [e[0] for e in adapter.events] == ["build", "render", "send", "record", "start_processing", "handler"]
    assert len(records) == 1
    record = records[0]
    assert record["stage"] == "delivered"
    assert record["outcome"] == "matched"
    assert record["delivery_succeeded"] is True
    assert record["delivery_before_model"] is True
    assert record["render_succeeded"] is True
    assert record["failure_code"] == ""
    assert record["records_considered_by_source_type"] == {"failures:lesson": 2}
    assert record["records_selected_by_source_type"] == {"failures:lesson": 1}
    assert record["event_id"]
    assert "fix it" not in str(record)
    assert "chat-secret" not in str(record)
    assert "msg-1" not in str(record)


@pytest.mark.asyncio
async def test_delivery_failure_records_failure_and_never_reaches_handler(monkeypatch):
    adapter = DummyTelegramAdapter(send_behavior="fail")
    records = []
    monkeypatch.setattr(gate, "_record_loop_health_event", lambda root, payload: records.append(dict(payload)), raising=False)
    _install_cache(adapter)
    _instrument_normal_path(adapter)
    event = _make_event("/repo do x")

    await adapter.handle_message(event)
    await _drain(adapter)

    kinds = [e[0] for e in adapter.events]
    assert "start_processing" not in kinds
    assert "handler" not in kinds
    assert len(records) == 1
    assert records[0]["stage"] == "failed"
    assert records[0]["outcome"] == "failed"
    assert records[0]["failure_code"] == "PREFLIGHT_DELIVERY_FAILED"
    assert records[0]["delivery_succeeded"] is False
    assert records[0]["delivery_before_model"] is False


@pytest.mark.asyncio
async def test_render_failure_records_failure_without_delivery_or_handler(monkeypatch):
    adapter = DummyTelegramAdapter()
    records = []
    monkeypatch.setattr(gate, "_record_loop_health_event", lambda root, payload: records.append(dict(payload)), raising=False)

    def _boom_render(packet):
        raise ValueError("render boom")

    _install_cache(adapter, render=_boom_render)
    _instrument_normal_path(adapter)
    event = _make_event("/repo do x")

    await adapter.handle_message(event)
    await _drain(adapter)

    kinds = [e[0] for e in adapter.events]
    assert "start_processing" not in kinds
    assert "handler" not in kinds
    assert len(records) == 1
    assert records[0]["stage"] == "failed"
    assert records[0]["failure_code"] == "PREFLIGHT_RENDER_FAILED"
    assert records[0]["render_succeeded"] is False
    assert records[0]["delivery_succeeded"] is False


@pytest.mark.asyncio
async def test_import_load_failure_records_failure_when_cogitator_root_exists(monkeypatch, tmp_path):
    (tmp_path / "cogitator_virgil_preflight.py").write_text("raise RuntimeError('boom import')\n")
    monkeypatch.setenv("COGITATOR_REPO_ROOT", str(tmp_path))
    reset_preflight_cache_for_tests()
    adapter = DummyTelegramAdapter()
    records = []
    monkeypatch.setattr(gate, "_record_loop_health_event", lambda root, payload: records.append(dict(payload)), raising=False)
    event = _make_event("/repo do x")
    parsed = parse_repo_command(event.text, "hermes_bot")

    assert await run_gate(adapter, event, parsed) is False

    assert len(records) == 1
    assert records[0]["stage"] == "failed"
    assert records[0]["failure_code"] == "PREFLIGHT_IMPORT_FAILED"
    assert records[0]["event_id"]
    assert "do x" not in str(records[0])


def test_record_loop_health_event_imports_cogitator_recorder_and_writes_jsonl(tmp_path):
    (tmp_path / "cogitator_loop_health.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "def record_preflight_event(payload, *, log_path):\n"
        "    p = Path(log_path)\n"
        "    p.parent.mkdir(parents=True, exist_ok=True)\n"
        "    with p.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(json.dumps(payload, sort_keys=True) + '\\\\n')\n",
        encoding="utf-8",
    )

    gate._record_loop_health_event(
        tmp_path,
        {
            "event_id": "safe-id",
            "stage": "delivered",
            "outcome": "matched",
            "delivery_before_model": True,
        },
    )

    metrics_path = tmp_path / "storage" / "metrics" / "virgil_preflight_events.jsonl"
    assert metrics_path.exists()
    assert '"event_id": "safe-id"' in metrics_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 3. run_gate fail-closed paths (each maps to a sanitized reason code)         #
# --------------------------------------------------------------------------- #
def _last_sent(adapter):
    assert adapter.sent, "expected a failure packet to be attempted"
    return adapter.sent[-1]["content"]


@pytest.mark.asyncio
async def test_missing_task_fails_closed():
    adapter = DummyTelegramAdapter()
    event = _make_event("/repo")
    parsed = parse_repo_command(event.text, "hermes_bot")
    assert await run_gate(adapter, event, parsed) is False
    assert "MISSING_TASK" in _last_sent(adapter)


@pytest.mark.asyncio
async def test_cogitator_root_unavailable_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("COGITATOR_REPO_ROOT", str(tmp_path / "does_not_exist"))
    reset_preflight_cache_for_tests()
    adapter = DummyTelegramAdapter()
    event = _make_event("/repo do x")
    parsed = parse_repo_command(event.text, "hermes_bot")
    assert await run_gate(adapter, event, parsed) is False
    body = _last_sent(adapter)
    assert "COGITATOR_ROOT_UNAVAILABLE" in body
    # Four-section failure formatter works without importing Cogitator at all.
    for section in ("Status", "Task", "Failure", "Required Action"):
        assert section in body


@pytest.mark.asyncio
async def test_import_failure_fails_closed(monkeypatch, tmp_path):
    (tmp_path / "cogitator_virgil_preflight.py").write_text("raise RuntimeError('boom import')\n")
    monkeypatch.setenv("COGITATOR_REPO_ROOT", str(tmp_path))
    reset_preflight_cache_for_tests()
    adapter = DummyTelegramAdapter()
    event = _make_event("/repo do x")
    parsed = parse_repo_command(event.text, "hermes_bot")
    assert await run_gate(adapter, event, parsed) is False
    assert "PREFLIGHT_IMPORT_FAILED" in _last_sent(adapter)


@pytest.mark.asyncio
async def test_builder_execution_failure_fails_closed():
    adapter = DummyTelegramAdapter()

    def _boom_build(task, failures_dir=None, patterns_dir=None):
        raise RuntimeError("build boom")

    _install_cache(adapter, build=_boom_build)
    event = _make_event("/repo do x")
    parsed = parse_repo_command(event.text, "hermes_bot")
    assert await run_gate(adapter, event, parsed) is False
    assert "PREFLIGHT_BUILD_FAILED" in _last_sent(adapter)


@pytest.mark.asyncio
async def test_invalid_packet_result_fails_closed():
    adapter = DummyTelegramAdapter()

    def _bad_build(task, failures_dir=None, patterns_dir=None):
        return {"packet_type": "something_else"}

    _install_cache(adapter, build=_bad_build)
    event = _make_event("/repo do x")
    parsed = parse_repo_command(event.text, "hermes_bot")
    assert await run_gate(adapter, event, parsed) is False
    assert "PREFLIGHT_BUILD_FAILED" in _last_sent(adapter)


@pytest.mark.asyncio
async def test_render_failure_fails_closed():
    adapter = DummyTelegramAdapter()

    def _boom_render(packet):
        raise ValueError("render boom")

    _install_cache(adapter, render=_boom_render)
    event = _make_event("/repo do x")
    parsed = parse_repo_command(event.text, "hermes_bot")
    assert await run_gate(adapter, event, parsed) is False
    assert "PREFLIGHT_RENDER_FAILED" in _last_sent(adapter)


@pytest.mark.asyncio
async def test_delivery_returns_failure_fails_closed():
    # Covers BOTH success-packet delivery returning failure AND the subsequent
    # failure-packet delivery also returning failure (send_behavior="fail" affects all).
    adapter = DummyTelegramAdapter(send_behavior="fail")
    _install_cache(adapter)
    event = _make_event("/repo do x")
    parsed = parse_repo_command(event.text, "hermes_bot")
    assert await run_gate(adapter, event, parsed) is False
    assert "PREFLIGHT_DELIVERY_FAILED" in _last_sent(adapter)


@pytest.mark.asyncio
async def test_delivery_raises_fails_closed():
    # Covers BOTH success-packet delivery raising AND the failure-packet delivery
    # raising (send_behavior="raise" affects all). No exception may escape.
    adapter = DummyTelegramAdapter(send_behavior="raise")
    _install_cache(adapter)
    event = _make_event("/repo do x")
    parsed = parse_repo_command(event.text, "hermes_bot")
    result = await run_gate(adapter, event, parsed)   # must not raise
    assert result is False


# --------------------------------------------------------------------------- #
# 4. handle_message end-to-end: ordering, fail-closed, clean event            #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_handle_message_success_calls_handler_once_after_send():
    adapter = DummyTelegramAdapter()
    _install_cache(adapter)
    _instrument_normal_path(adapter)
    event = _make_event("/repo fix it")

    await adapter.handle_message(event)
    await _drain(adapter)

    kinds = [e[0] for e in adapter.events]
    # Exact order: builder -> render -> send -> normal processing -> handler.
    assert kinds == ["build", "render", "send", "start_processing", "handler"]
    assert kinds.count("handler") == 1                      # exactly once
    # The event handed to normal processing is a clean TEXT event with the token stripped.
    sp = next(e for e in adapter.events if e[0] == "start_processing")
    assert sp[1] == "fix it" and sp[2] == MessageType.TEXT
    assert adapter.events[-1] == ("handler", "fix it")


@pytest.mark.asyncio
async def test_handle_message_failure_never_reaches_handler():
    adapter = DummyTelegramAdapter()

    def _boom_build(task, failures_dir=None, patterns_dir=None):
        raise RuntimeError("build boom")

    _install_cache(adapter, build=_boom_build)
    _instrument_normal_path(adapter)
    event = _make_event("/repo fix it")

    await adapter.handle_message(event)
    await _drain(adapter)

    kinds = [e[0] for e in adapter.events]
    assert "start_processing" not in kinds   # normal processing never reached
    assert "handler" not in kinds            # model/_message_handler NEVER called
    assert "PREFLIGHT_BUILD_FAILED" in _last_sent(adapter)


@pytest.mark.asyncio
async def test_ordinary_text_bypasses_gate():
    adapter = DummyTelegramAdapter()
    _install_cache(adapter)  # would be used if the gate (wrongly) fired
    _instrument_normal_path(adapter)
    event = _make_event("hello there")

    await adapter.handle_message(event)
    await _drain(adapter)

    kinds = [e[0] for e in adapter.events]
    assert "build" not in kinds and "send" not in kinds      # no preflight
    assert kinds == ["start_processing", "handler"]          # straight to normal flow
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_active_session_still_crosses_gate():
    adapter = DummyTelegramAdapter()
    _install_cache(adapter)
    adapter.set_message_handler(lambda e: asyncio.sleep(0))   # presence enables the gate
    event = _make_event("/repo fix it")
    # Pre-seed an active session for this event's key.
    key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )
    adapter._active_sessions[key] = SimpleNamespace(done=lambda: False)
    adapter._heal_stale_session_lock = lambda k: None
    adapter._is_queue_text_debounce_candidate = lambda e: False

    await adapter.handle_message(event)

    # Even with an active session, the gate ran and delivered the preflight.
    assert ("send", "PREFLIGHT MESSAGE for: fix it") in adapter.events


@pytest.mark.asyncio
async def test_non_telegram_platform_skips_gate():
    adapter = DummyTelegramAdapter()
    adapter.platform = Platform.DISCORD  # adapter-level; event carries the platform below
    _install_cache(adapter)
    _instrument_normal_path(adapter)
    event = _make_event("/repo fix it", platform=Platform.DISCORD)

    await adapter.handle_message(event)
    await _drain(adapter)

    kinds = [e[0] for e in adapter.events]
    assert "build" not in kinds and "send" not in kinds      # gate is Telegram-only in V0


# --------------------------------------------------------------------------- #
# 5. Loader: alternate-root cannot reuse a prior import                       #
# --------------------------------------------------------------------------- #
_FAKE_ROOT = textwrap.dedent('''
    import cogitator_learning_retrieval  # noqa: F401
    import cogitator_pre_build_pattern_lookup  # noqa: F401
    import cogitator_skill_library  # noqa: F401

    MARKER = {marker!r}

    def build_virgil_preflight_packet(task_description, subsystem_hint=None,
                                      failures_dir="failures", patterns_dir="operating_patterns"):
        return {{"packet_type": "virgil_preflight_v0",
                 "marker": MARKER, "task_description": task_description}}

    def render_virgil_preflight_message(packet):
        return "PREFLIGHT[" + packet["marker"] + "]"
''')


def _make_root(tmp_path: Path, name: str, marker: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "cogitator_virgil_preflight.py").write_text(_FAKE_ROOT.format(marker=marker))
    for sib in ("cogitator_learning_retrieval", "cogitator_pre_build_pattern_lookup",
                "cogitator_skill_library"):
        (root / f"{sib}.py").write_text(f"# {marker} sibling\n")
    return root


@pytest.mark.asyncio
async def test_alternate_root_after_reset_uses_new_builder(monkeypatch, tmp_path):
    root_a = _make_root(tmp_path, "rootA", "A")
    root_b = _make_root(tmp_path, "rootB", "B")

    monkeypatch.setenv("COGITATOR_REPO_ROOT", str(root_a))
    reset_preflight_cache_for_tests()
    build_a, _, _, _ = gate._load_preflight()
    assert build_a("t")["marker"] == "A"

    # Reset clears recorded modules so the alternate root re-imports its own siblings.
    reset_preflight_cache_for_tests()
    monkeypatch.setenv("COGITATOR_REPO_ROOT", str(root_b))
    build_b, _, _, _ = gate._load_preflight()
    assert build_b("t")["marker"] == "B"   # B used, not A


@pytest.mark.asyncio
async def test_alternate_root_without_reset_fails_closed_on_provenance(monkeypatch, tmp_path):
    root_a = _make_root(tmp_path, "rootA", "A")
    root_b = _make_root(tmp_path, "rootB", "B")

    monkeypatch.setenv("COGITATOR_REPO_ROOT", str(root_a))
    reset_preflight_cache_for_tests()
    gate._load_preflight()

    # Force a reload from root B WITHOUT clearing recorded modules: A's bare-named
    # siblings are still cached, so the provenance gate must fail closed.
    gate._CACHE = None
    monkeypatch.setenv("COGITATOR_REPO_ROOT", str(root_b))
    with pytest.raises(gate.PreflightError) as ei:
        gate._load_preflight()
    assert ei.value.code == "PREFLIGHT_IMPORT_FAILED"
