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
        assessment={
            "decision": {"recommended_disposition": "save_as_context"},
            "understanding": {"content_type": "reference_material"},
        },
        final_state="partial_assessment",
    ) == irb.PENDING
    assert irb.recommended_action(
        assessment={
            "decision": {"recommended_disposition": "promotion_candidate"},
            "understanding": {"content_type": "operating_playbook"},
        },
        final_state="complete_assessment",
    ) == irb.APPROVE
    assert irb.recommended_action(
        assessment={
            "decision": {"recommended_disposition": "research_later"},
            "understanding": {"content_type": "factual_claim"},
        },
        final_state="complete_assessment",
    ) == irb.RESEARCH
    assert irb.recommended_action(
        assessment={
            "decision": {"recommended_disposition": "ignore"},
            "understanding": {"content_type": "noise"},
        },
        final_state="complete_assessment",
    ) == irb.ARCHIVE
    layout = irb.button_layout(irb.RESEARCH)
    labels = [lbl for row in layout for lbl, _ in row]
    assert sum(lbl.startswith("⭐") for lbl in labels) == 1
    assert any(lbl.startswith("⭐") and "Research" in lbl for lbl in labels)


@pytest.mark.parametrize(
    (
        "assessment",
        "final_state",
        "expected_action",
        "expected_button",
        "expected_label",
        "expected_effect",
    ),
    [
        (
            {
                "decision": {"recommended_disposition": "save_as_context"},
                "understanding": {"content_type": "reference_material"},
                "judgment": {"evidence_quality": "moderate"},
            },
            "complete_assessment",
            irb.APPROVE,
            "✅ Approve",
            "APPROVE AS REFERENCE CONTEXT",
            "Makes this item retrievable for future relevant work. It does not "
            "install or implement anything or verify unproven factual claims.",
        ),
        (
            {
                "decision": {"recommended_disposition": "create_playbook_candidate"},
                "understanding": {"content_type": "operating_playbook"},
            },
            "complete_assessment",
            irb.APPROVE,
            "✅ Approve",
            "APPROVE AS OPERATING PLAYBOOK",
            "Treats the bounded method as approved operating guidance. It does "
            "not start implementation or validate every factual claim in the source.",
        ),
        (
            {
                "decision": {"recommended_disposition": "promotion_candidate"},
                "understanding": {"content_type": "risk_or_warning"},
            },
            "complete_assessment",
            irb.APPROVE,
            "✅ Approve",
            "APPROVE AS SAFETY WARNING",
            "Adds the risk and prevention rule to approved safety context. It "
            "does not authorize implementation.",
        ),
        (
            {
                "decision": {"recommended_disposition": "promotion_candidate"},
                "understanding": {"content_type": "technical_insight"},
            },
            "complete_assessment",
            irb.APPROVE,
            "✅ Approve",
            "APPROVE AS TECHNICAL INSIGHT",
            "Makes the pattern retrievable as technical guidance. It does not "
            "authorize architecture changes or implementation.",
        ),
        (
            {
                "decision": {"recommended_disposition": "test_as_hypothesis"},
                "understanding": {"content_type": "hypothesis"},
            },
            "complete_assessment",
            irb.APPROVE,
            "✅ Approve",
            "APPROVE AS BUSINESS HYPOTHESIS",
            "Preserves it as a labelled hypothesis, not verified fact. It does "
            "not authorize implementation.",
        ),
        (
            {
                "decision": {"recommended_disposition": "save_as_context"},
                "understanding": {"content_type": "factual_claim"},
                "judgment": {"evidence_quality": "weak"},
            },
            "complete_assessment",
            irb.RESEARCH,
            "🔎 Research",
            "RESEARCH BEFORE APPROVAL",
            "Creates an explicit research request. It must not silently start "
            "paid research.",
        ),
        (
            {
                "decision": {"recommended_disposition": "save_as_context"},
                "understanding": {"content_type": "reference_material"},
            },
            "partial_assessment",
            irb.PENDING,
            "⏸ Leave Pending",
            "LEAVE PENDING",
            "Leaves the item unapproved for later review. It does not promote "
            "or implement anything.",
        ),
        (
            {
                "decision": {"recommended_disposition": "save_as_context"},
                "understanding": {"content_type": "reference_material"},
                "memory_comparison": {
                    "duplication_status": "contradiction",
                    "contradictions": [
                        "Conflicts with approved operating guidance."
                    ],
                },
            },
            "complete_assessment",
            irb.DETAILS,
            "📄 Details",
            "REVIEW CONTRADICTION",
            "Shows the conflicting knowledge before Cal decides.",
        ),
        (
            {
                "decision": {"recommended_disposition": "archive"},
                "understanding": {"content_type": "reference_material"},
                "judgment": {"value_level": "low"},
            },
            "complete_assessment",
            irb.ARCHIVE,
            "🗄 Archive",
            "ARCHIVE",
            "Removes the item from normal review and retrieval without deleting "
            "the source.",
        ),
    ],
)
def test_explicit_recommendation_footer(
    assessment,
    final_state,
    expected_action,
    expected_button,
    expected_label,
    expected_effect,
):
    assessment = {
        **assessment,
        "decision": {
            **assessment["decision"],
            "why_it_matters": "Specific reason.",
        },
    }
    footer = irb.build_review_footer(
        assessment, "inote-5", final_state=final_state
    )
    action = irb.recommended_action(
        assessment=assessment, final_state=final_state
    )
    assert action == expected_action
    assert footer.splitlines()[-5:] == [
        f"RECOMMENDED DECISION: {expected_label}",
        f"ACTION: Tap {expected_button}",
        f"EFFECT: {expected_effect}",
        "WHY: Specific reason.",
        "REVIEW ID: inote-5",
    ]
    starred = [
        label
        for row in irb.button_layout(action)
        for label, _button_action in row
        if label.startswith("⭐ ")
    ]
    assert starred == [f"⭐ {expected_button}"]


def test_saved_to_strip():
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
    res = await _runner_instance().handle_intelligent_button_action(_entry(irb.RESEARCH))
    assert [c.action for c in bridge_calls] == ["request_explicit_research"]
    assert res["status"] == "research_requested"
    assert res["remove_buttons"] is False


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

    async def handle_intelligent_button_action(self, entry):
        self.actions.append(entry.action)
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
