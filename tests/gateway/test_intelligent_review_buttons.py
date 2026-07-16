"""Tests for Intelligent Second Brain Telegram inline review buttons.

Covers the 15 required behaviours: single-use approve, harmless repeat,
unauthorized/expired/tampered rejection, zero-provider details, no-op pending,
archive-not-delete, explicit-research-only, no paid spend, partial-item layout,
text commands still parse, no auto-promotion, and message-update/stale-button
removal. No model, provider, or paid research is ever invoked.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway import cogitator_intake_bridge as bridge
from gateway import intelligent_review_buttons as irb
from gateway.platforms.telegram import TelegramAdapter
from gateway.config import PlatformConfig
from gateway.slash_commands import GatewaySlashCommandsMixin


# --------------------------------------------------------------------------- #
# Pure store / layout (tests 2, 3, 4, 5, 11 at the state layer)
# --------------------------------------------------------------------------- #

def _mint(store, user_id="42"):
    return store.mint_group(
        review_id="inote-5", item_id="ki_" + "a" * 24,
        chat_id="1", user_id=user_id, actions=irb.BUTTON_ACTIONS,
    )


def test_approve_token_is_single_use_and_burns_siblings():
    store = irb.ReviewButtonStore()
    toks = _mint(store)
    state, entry = store.validate(toks["approve"], user_id="42")
    assert state == "ok"
    store.consume(entry)  # terminal
    assert store.validate(toks["approve"], user_id="42")[0] == "already_handled"
    assert store.validate(toks["archive"], user_id="42")[0] == "already_handled"


def test_unauthorized_and_tampered_and_expired_tokens_rejected():
    store = irb.ReviewButtonStore()
    toks = _mint(store)
    assert store.validate(toks["approve"], user_id="999")[0] == "wrong_user"
    assert store.validate("isb-tampered-token", user_id="42")[0] == "not_found"
    now = [100.0]
    exp = irb.ReviewButtonStore(ttl_seconds=10, clock=lambda: now[0])
    t = exp.mint_group(
        review_id="i", item_id="ki_" + "b" * 24, chat_id="1",
        user_id="42", actions=[irb.APPROVE],
    )
    now[0] = 200.0
    assert exp.validate(t["approve"], user_id="42")[0] == "expired"


def test_details_and_pending_are_repeatable():
    store = irb.ReviewButtonStore()
    toks = _mint(store)
    for action in (irb.DETAILS, irb.PENDING):
        _, entry = store.validate(toks[action], user_id="42")
        store.consume(entry)
        assert store.validate(toks[action], user_id="42")[0] == "ok"


def test_partial_and_recommendation_layout():
    assert irb.recommended_action(
        disposition="save_as_context", content_type="reference_material",
        final_state="partial_assessment",
    ) == irb.PENDING
    assert irb.recommended_action(
        disposition="promotion_candidate", content_type="operating_playbook",
        final_state="complete_assessment",
    ) == irb.APPROVE
    assert irb.recommended_action(
        disposition="research_later", content_type="factual_claim",
        final_state="complete_assessment",
    ) == irb.RESEARCH
    assert irb.recommended_action(
        disposition="ignore", content_type="noise",
        final_state="complete_assessment",
    ) == irb.ARCHIVE
    layout = irb.button_layout(irb.RESEARCH)
    labels = [lbl for row in layout for lbl, _ in row]
    assert sum(lbl.startswith("⭐") for lbl in labels) == 1
    assert any(lbl.startswith("⭐") and "Research" in lbl for lbl in labels)


def test_footer_and_saved_to_strip():
    footer = irb.build_review_footer(
        {"decision": {"recommended_disposition": "promotion_candidate",
                      "why_it_matters": "cost lever"}},
        "inote-5",
    )
    assert "RECOMMENDED DECISION:" in footer and "WHY:" in footer
    assert "REVIEW ID: inote-5" in footer
    assert irb.strip_saved_to(
        "VALUE: HIGH\nSAVED TO: storage/notes/ki.md\nREVIEW ID: inote-5"
    ) == "VALUE: HIGH\nREVIEW ID: inote-5"


# --------------------------------------------------------------------------- #
# Runner action executor (tests 6, 7, 8, 9, 10, 14)
# --------------------------------------------------------------------------- #

def _runner_instance():
    inst = object.__new__(GatewaySlashCommandsMixin)
    inst._intake_config = lambda: (True, "http://bridge", "tok")
    return inst


def _entry(action):
    store = irb.ReviewButtonStore()
    toks = store.mint_group(
        review_id="inote-5", item_id="ki_" + "a" * 24, chat_id="1",
        user_id="42", actions=irb.BUTTON_ACTIONS,
    )
    _, entry = store.validate(toks[action], user_id="42")
    return entry


@pytest.fixture
def bridge_calls(monkeypatch):
    calls = []

    def fake(*, base_url, token, command):
        calls.append(command)
        status = {
            "approve": "applied", "archive": "applied",
            "request_explicit_research": "research_requested",
            "view_related": "ok",
        }[command.action]
        result = {
            "requested_action": "review_intelligent_knowledge",
            "status": status, "action": command.action,
            "item_id": "ki_" + "a" * 24, "review_id": "inote-5",
            "paid_api_used": False, "research_performed": False,
            "mutation_performed": status == "applied",
        }
        if command.action in {"approve", "archive"}:
            result["lifecycle_state"] = (
                "promoted" if command.action == "approve" else "archived"
            )
            result["promoted_path"] = "storage/promoted/isb-ki.md"
        return result

    monkeypatch.setattr(bridge, "request_intelligent_review", fake)
    monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "tok")
    return calls


@pytest.mark.asyncio
async def test_pending_creates_no_mutation(bridge_calls):
    res = await _runner_instance().handle_intelligent_button_action(_entry(irb.PENDING))
    assert res["remove_buttons"] is False
    assert bridge_calls == []  # no bridge/provider call whatsoever


@pytest.mark.asyncio
async def test_details_uses_only_the_deterministic_view(bridge_calls):
    res = await _runner_instance().handle_intelligent_button_action(_entry(irb.DETAILS))
    assert len(bridge_calls) == 1
    assert bridge_calls[0].action == "view_related"
    assert bridge_calls[0].payload == "assessment"
    assert res["remove_buttons"] is False


@pytest.mark.asyncio
async def test_research_creates_only_an_explicit_request(bridge_calls):
    origin = {"platform": "telegram", "chat_id": "1", "chat_type": "dm"}
    res = await _runner_instance().handle_intelligent_button_action(
        _entry(irb.RESEARCH), origin=origin
    )
    assert [c.action for c in bridge_calls] == ["request_explicit_research"]
    # The delivery route rides on the research command (and only there).
    assert bridge_calls[0].origin == origin
    assert res["status"] == "research_requested"
    assert res["remove_buttons"] is False


@pytest.mark.asyncio
async def test_non_research_actions_never_carry_origin(bridge_calls):
    origin = {"platform": "telegram", "chat_id": "1", "chat_type": "dm"}
    await _runner_instance().handle_intelligent_button_action(
        _entry(irb.ARCHIVE), origin=origin
    )
    assert bridge_calls[0].action == "archive"
    assert bridge_calls[0].origin is None


@pytest.mark.asyncio
async def test_research_button_renders_queued_durable_job(monkeypatch):
    def fake(*, base_url, token, command):
        assert command.origin == {
            "platform": "telegram", "chat_id": "1", "chat_type": "dm",
        }
        return {
            "requested_action": "review_intelligent_knowledge",
            "status": "research_requested",
            "action": "request_explicit_research",
            "item_id": "ki_" + "a" * 24, "review_id": "inote-5",
            "paid_api_used": False, "research_performed": False,
            "research_job": {
                "job_id": "inote-5", "status": "queued", "created": True,
                "mode": "grok_oauth_pilot",
                "requested_provider": "xai-oauth:grok-4.5",
                "paid_api_used": False,
            },
        }

    monkeypatch.setattr(bridge, "request_intelligent_review", fake)
    monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "tok")
    res = await _runner_instance().handle_intelligent_button_action(
        _entry(irb.RESEARCH),
        origin={"platform": "telegram", "chat_id": "1", "chat_type": "dm"},
    )
    assert "Research queued" in res["text"]
    assert "Job: inote-5" in res["text"]
    assert "Provider: Grok OAuth" in res["text"]
    assert "Paid API: no" in res["text"]


@pytest.mark.asyncio
async def test_research_job_not_queued_is_reported_honestly(monkeypatch):
    def fake(*, base_url, token, command):
        return {
            "requested_action": "review_intelligent_knowledge",
            "status": "research_requested",
            "action": "request_explicit_research",
            "item_id": "ki_" + "a" * 24, "review_id": "inote-5",
            "paid_api_used": False, "research_performed": False,
            "research_job": {
                "status": "error", "created": False,
                "reason": "feed unavailable", "paid_api_used": False,
            },
        }

    monkeypatch.setattr(bridge, "request_intelligent_review", fake)
    monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "tok")
    res = await _runner_instance().handle_intelligent_button_action(
        _entry(irb.RESEARCH),
        origin={"platform": "telegram", "chat_id": "1", "chat_type": "dm"},
    )
    assert "NOT queued" in res["text"]
    assert "feed unavailable" in res["text"]


@pytest.mark.asyncio
async def test_archive_uses_archive_action_not_delete(bridge_calls):
    res = await _runner_instance().handle_intelligent_button_action(_entry(irb.ARCHIVE))
    assert [c.action for c in bridge_calls] == ["archive"]
    assert res["remove_buttons"] is True  # terminal, buttons removed


@pytest.mark.asyncio
async def test_approve_calls_approve_once_and_removes_buttons(bridge_calls):
    res = await _runner_instance().handle_intelligent_button_action(_entry(irb.APPROVE))
    assert [c.action for c in bridge_calls] == ["approve"]
    assert res["remove_buttons"] is True


@pytest.mark.asyncio
async def test_paid_or_research_spend_hard_fails(monkeypatch):
    def fake(*, base_url, token, command):
        return {"requested_action": "review_intelligent_knowledge",
                "status": "applied", "action": command.action,
                "item_id": "ki_" + "a" * 24, "review_id": "inote-5",
                "paid_api_used": True, "research_performed": False}

    monkeypatch.setattr(bridge, "request_intelligent_review", fake)
    monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "tok")
    with pytest.raises(RuntimeError):
        await _runner_instance().handle_intelligent_button_action(_entry(irb.APPROVE))


# --------------------------------------------------------------------------- #
# Adapter callback flow (tests 1, 2, 3, 15)
# --------------------------------------------------------------------------- #

def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t", extra={}))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    adapter.send = AsyncMock()
    return adapter


class _Runner:
    def __init__(self, store, authorized=True):
        self._intelligent_button_store = store
        self.authorized = authorized
        self.actions = []

    async def _handle_message(self, event):  # __self__ hook for the adapter
        return None

    def _is_user_authorized(self, source):
        return self.authorized

    async def handle_intelligent_button_action(self, entry, origin=None):
        self.actions.append(entry.action)
        self.origins = getattr(self, "origins", [])
        self.origins.append(origin)
        terminal = entry.action in {irb.APPROVE, irb.ARCHIVE}
        return {
            "text": "✅ Approved" if terminal else "ok",
            "remove_buttons": terminal,
            "status": "applied" if terminal else "ok",
        }


def _query(token, user_id=42):
    return SimpleNamespace(
        data=f"isb:{token}",
        from_user=SimpleNamespace(id=user_id, first_name="Cal"),
        message=SimpleNamespace(
            chat_id=1, chat=SimpleNamespace(type="private"),
            message_thread_id=None,
        ),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


async def _click(adapter, store, token, user_id=42):
    q = _query(token, user_id=user_id)
    await adapter._handle_intelligent_review_callback(
        q, q.data, query_chat_id=1, query_chat_type="private",
        query_thread_id=None, query_user_name="Cal",
    )
    return q


@pytest.mark.asyncio
async def test_callback_approve_once_edits_and_removes_buttons():
    store = irb.ReviewButtonStore()
    toks = _mint(store)
    adapter = _make_adapter()
    runner = _Runner(store, authorized=True)
    adapter._message_handler = runner._handle_message

    q = await _click(adapter, store, toks["approve"])
    q.answer.assert_awaited()  # spinner cleared immediately
    assert runner.actions == ["approve"]
    # The callback site supplies the strict delivery route for async research.
    assert runner.origins == [
        {"platform": "telegram", "chat_id": "1", "chat_type": "dm"}
    ]
    q.edit_message_text.assert_awaited()  # message updated after action
    assert q.edit_message_text.call_args.kwargs["reply_markup"] is None  # stale buttons removed

    # Repeat tap is harmless and reports already handled — no second action.
    q2 = await _click(adapter, store, toks["approve"])
    assert runner.actions == ["approve"]
    assert "handled" in str(q2.answer.call_args.kwargs.get("text", "")).lower()


@pytest.mark.asyncio
async def test_callback_unauthorized_user_cannot_act():
    store = irb.ReviewButtonStore()
    toks = _mint(store)
    adapter = _make_adapter()
    runner = _Runner(store, authorized=False)
    adapter._message_handler = runner._handle_message

    q = await _click(adapter, store, toks["approve"], user_id=42)
    assert runner.actions == []  # never executed
    assert "not authorized" in str(q.answer.call_args.kwargs.get("text", "")).lower()


@pytest.mark.asyncio
async def test_callback_wrong_user_id_rejected_even_if_authorized_role():
    # Authorized *role* but the token was minted for a different user id.
    store = irb.ReviewButtonStore()
    toks = store.mint_group(
        review_id="inote-5", item_id="ki_" + "a" * 24, chat_id="1",
        user_id="777", actions=irb.BUTTON_ACTIONS,
    )
    adapter = _make_adapter()
    runner = _Runner(store, authorized=True)
    adapter._message_handler = runner._handle_message
    q = await _click(adapter, store, toks["approve"], user_id=42)
    assert runner.actions == []


# --------------------------------------------------------------------------- #
# Existing text commands remain supported (test 12) + no auto-promotion (13)
# --------------------------------------------------------------------------- #

def test_existing_text_review_commands_still_parse():
    cmd = bridge.parse_intelligent_review_command("approve knowledge inote-5")
    assert cmd is not None and cmd.action == "approve" and cmd.review_id == "inote-5"
    assert bridge.parse_intelligent_outcome_command(
        "knowledge outcome useful ki_" + "a" * 24
    ) is not None


def test_minting_buttons_performs_no_promotion(bridge_calls):
    # Delivering the assessment only mints tokens — it never calls the review
    # bridge, so nothing is promoted without an explicit authorized click.
    store = irb.ReviewButtonStore()
    _mint(store)
    assert bridge_calls == []
