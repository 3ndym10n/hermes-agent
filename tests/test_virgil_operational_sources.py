from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import hermes_attention as attention
import virgil_operational_sources as sources


NOW = datetime(2033, 5, 18, 0, 0, tzinfo=timezone.utc)


def _event(
    event_id: str,
    start: datetime,
    end: datetime,
    **changes,
) -> dict:
    event = {
        "id": event_id,
        "summary": "Planning session",
        "status": "confirmed",
        "updated": "2033-05-17T23:00:00Z",
        "start": {"dateTime": sources._iso(start)},
        "end": {"dateTime": sources._iso(end)},
        "transparency": "opaque",
    }
    event.update(changes)
    return event


def _operational_item(**changes) -> dict:
    item = {
        "source_id": "candidate-1",
        "title": "Review candidate",
        "item_type": "decision_candidate",
        "created_at": "2033-05-10T00:00:00Z",
        "review_status": "watchlist",
        "current_action": "pending",
        "evidence_quality": "moderate",
        "research_status": "",
        "research_updated_at": "",
        "research_stalled": False,
        "research_has_artifact": False,
        "promotion_candidate_ready": False,
        "promotion_approved": False,
        "high_risk": False,
        "blocked": False,
    }
    item.update(changes)
    return item


class _Request:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _CalendarService:
    def __init__(self, account: str, events: list[dict]):
        self.account = account
        self.response = events
        self.get_calls: list[dict] = []
        self.list_calls: list[dict] = []

    def calendarList(self):
        return self

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _Request({"id": self.account})

    def events(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _Request({"items": self.response})


def _tailscale_status(*, funnel: bool = False, proxy: str = "http://127.0.0.1:8788"):
    return {
        "TCP": {"8443": {"HTTPS": True}},
        "Web": {"virgil.example.ts.net:8443": {"Handlers": {"/": {"Proxy": proxy}}}},
        "AllowFunnel": {"virgil.example.ts.net:8443": funnel},
    }


def test_calendar_maps_change_conflict_cancellation_expiry_and_privacy():
    first = _event(
        "event-a",
        NOW + timedelta(minutes=30),
        NOW + timedelta(hours=2),
        summary="Demo with cal@example.com api_key=calendar-secret",
        description="PRIVATE CUSTOMER BODY",
        location="PRIVATE CUSTOMER ADDRESS",
        attendees=[{"email": "customer@example.com", "additionalGuests": 2}],
        htmlLink="https://calendar.google.com/calendar/event?eid=safe",
        hangoutLink="https://meet.google.com/secret-room",
    )
    second = _event(
        "event-b",
        NOW + timedelta(hours=1),
        NOW + timedelta(hours=1, minutes=30),
        htmlLink="https://calendar.google.com.evil.example/event",
    )
    cancelled = _event(
        "event-c",
        NOW + timedelta(hours=4),
        NOW + timedelta(hours=5),
        status="cancelled",
    )

    facts = sources.calendar_facts([first, second, cancelled], NOW)
    by_id = {fact.record_id: fact for fact in facts}
    first_fact = by_id[f"calendar:{sources._hash('event-a')}"]
    second_fact = by_id[f"calendar:{sources._hash('event-b')}"]
    cancelled_fact = by_id[f"calendar:{sources._hash('event-c')}"]
    conflict = next(fact for fact in facts if fact.kind == "calendar_conflict")

    assert first_fact.kind == "calendar_prepare_soon"
    assert sources.policy_for(first_fact).priority == "high"
    assert sources.policy_for(first_fact).status == "prepared"
    assert first_fact.expires_at == sources._iso(NOW + timedelta(hours=2))
    assert first_fact.deep_link == first["htmlLink"]
    assert second_fact.kind == "calendar_soon"
    assert second_fact.deep_link is None
    assert cancelled_fact.kind == "calendar_cancelled"
    assert sources.policy_for(cancelled_fact).status == "resolved"
    assert conflict.due_at == sources._iso(NOW + timedelta(hours=1))
    assert conflict.expires_at == sources._iso(NOW + timedelta(hours=2))

    serialized = json.dumps([fact.__dict__ for fact in facts])
    for private in (
        "cal@example.com",
        "calendar-secret",
        "PRIVATE CUSTOMER BODY",
        "PRIVATE CUSTOMER ADDRESS",
        "customer@example.com",
        "secret-room",
    ):
        assert private not in serialized

    changed = sources.calendar_facts(
        [{**first, "updated": "2033-05-17T23:05:00Z"}], NOW
    )[0]
    moved = sources.calendar_facts(
        [
            {
                **first,
                "start": {"dateTime": sources._iso(NOW + timedelta(minutes=45))},
            }
        ],
        NOW,
    )[0]
    assert changed.record_id == first_fact.record_id == moved.record_id
    assert changed.event_id != first_fact.event_id
    assert moved.event_id != first_fact.event_id


def test_calendar_verifies_bound_account_and_uses_bounded_read_only_query(tmp_path):
    token_path = tmp_path / "google" / "google_token.json"
    state_db = token_path.parent / "incoming-autodraft" / "state.db"
    state_db.parent.mkdir(parents=True)
    account = "cal@example.com"
    with sqlite3.connect(state_db) as conn:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO meta VALUES('verified_account_fingerprint', ?)",
            (hashlib.sha256(account.encode()).hexdigest(),),
        )
    state_db.chmod(0o600)
    before = state_db.read_bytes(), state_db.stat().st_mtime_ns
    service = _CalendarService(account, [])
    context = sources.SourceContext(
        NOW, {}, {"calendar_service": (service, token_path)}
    )

    result = sources.sync_calendar(context)

    assert result.status == "active"
    assert service.get_calls == [{"calendarId": "primary", "fields": "id"}]
    assert len(service.list_calls) == 1
    query = service.list_calls[0]
    assert query["calendarId"] == "primary"
    assert query["singleEvents"] is True
    assert query["showDeleted"] is True
    assert query["orderBy"] == "startTime"
    assert query["timeMin"] == sources._iso(NOW)
    assert query["timeMax"] == sources._iso(NOW + timedelta(days=7))
    assert "description" not in query["fields"]
    assert "email" not in query["fields"]
    assert (state_db.read_bytes(), state_db.stat().st_mtime_ns) == before

    service.account = "other@example.com"
    with pytest.raises(sources.SourceError, match="calendar_wrong_account"):
        sources.sync_calendar(context)
    assert len(service.list_calls) == 1, "wrong account must fail before event access"


def test_private_file_boundary_rejects_permissions_links_and_hardlinks(tmp_path):
    private = tmp_path / "private.db"
    private.write_bytes(b"safe")
    private.chmod(0o600)
    sources._require_private_file(private, "unsafe_file")

    public = tmp_path / "public.db"
    public.write_bytes(b"unsafe")
    public.chmod(0o644)
    with pytest.raises(sources.SourceError, match="unsafe_file"):
        sources._require_private_file(public, "unsafe_file")

    symlink = tmp_path / "link.db"
    symlink.symlink_to(private)
    with pytest.raises(sources.SourceError, match="unsafe_file"):
        sources._require_private_file(symlink, "unsafe_file")

    hardlink = tmp_path / "hardlink.db"
    os.link(private, hardlink)
    with pytest.raises(sources.SourceError, match="unsafe_file"):
        sources._require_private_file(private, "unsafe_file")


def test_cogitator_snapshot_contract_is_exact_and_bounded():
    valid = _operational_item()
    assert sources._validate_operational_item(valid) is valid

    invalid = [
        {**valid, "reason": "raw reason must not cross the boundary"},
        {**valid, "title": "x" * 501},
        {**valid, "research_stalled": 1},
        {**valid, "source_id": ""},
    ]
    for item in invalid:
        with pytest.raises(sources.SourceError, match="cogitator_snapshot_invalid"):
            sources._validate_operational_item(item)


@pytest.mark.parametrize(
    "changes,expected",
    [
        (
            {"review_status": "needs_cal_approval", "current_action": "promote"},
            "cogitator_decision",
        ),
        ({"promotion_candidate_ready": True}, "cogitator_promotion"),
        (
            {"research_status": "complete", "research_has_artifact": True},
            "cogitator_ready",
        ),
        ({"research_status": "researching"}, "cogitator_running"),
        ({"research_status": "failed"}, "cogitator_failed"),
        ({"research_stalled": True}, "cogitator_failed"),
        ({"review_status": "approved"}, "cogitator_closed"),
        ({}, "cogitator_quiet"),
    ],
)
def test_cogitator_mapping(changes, expected):
    assert sources._cogitator_kind(_operational_item(**changes)) == expected


def test_cogitator_sync_persists_no_raw_snapshot_or_creation_time_as_due(
    tmp_path, monkeypatch
):
    raw = _operational_item(
        source_id="private-source-id",
        title="Decision for cal@example.com api_key=super-secret",
        review_status="needs_cal_approval",
        current_action="promote",
        research_updated_at="2033-05-17T23:59:00Z",
    )
    seen = {}

    def request(**kwargs):
        seen.update(kwargs)
        return {"operational_items": [raw], "legacy_field": "ignored"}

    monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setattr(sources, "request_decision_batch", request)
    context = sources.SourceContext(
        NOW,
        {"decision_batch": {"enabled": True, "base_url": "https://bridge.invalid"}},
    )

    result = sources.sync_cogitator(context)
    fact = result.facts[0]
    assert seen == {
        "base_url": "https://bridge.invalid",
        "token": "bridge-secret",
    }
    assert fact.kind == "cogitator_decision"
    assert fact.due_at is None
    persisted = sources.apply_fact(fact, db_path=tmp_path / "attention.db")
    serialized = json.dumps(persisted["item"])
    for raw_value in (
        "cal@example.com",
        "super-secret",
        "private-source-id",
        "2033-05-10T00:00:00Z",
        "2033-05-17T23:59:00Z",
        "bridge-secret",
    ):
        assert raw_value not in serialized


def test_graphql_unresolved_threads_are_bounded_and_classify_review(monkeypatch):
    calls = []

    def complete(args, **_kwargs):
        calls.append(args)
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {"isResolved": False},
                                {"isResolved": True},
                                {"isResolved": False},
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }
        }

    monkeypatch.setattr(sources, "_run_json", complete)
    assert sources._unresolved_review_threads("3ndym10n/hermes-agent", 42) == 2
    command = calls[0]
    assert command[:3] == ["gh", "api", "graphql"]
    assert "number=42" in command
    assert any("reviewThreads(first:100)" in part for part in command)

    facts = sources.github_pr_facts(
        "3ndym10n/hermes-agent",
        [
            {
                "number": 42,
                "title": "Operational sources",
                "url": "https://github.com/3ndym10n/hermes-agent/pull/42",
                "reviewDecision": "APPROVED",
                "unresolvedReviewThreads": 2,
                "statusCheckRollup": [],
            }
        ],
        [],
    )
    assert facts[0].kind == "github_review"

    monkeypatch.setattr(
        sources,
        "_run_json",
        lambda *_args, **_kwargs: {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": True},
                        }
                    }
                }
            }
        },
    )
    with pytest.raises(sources.SourceError, match="github_review_response_incomplete"):
        sources._unresolved_review_threads("3ndym10n/hermes-agent", 42)


def test_github_maps_failures_reviews_merges_and_suppresses_noise():
    repository = "3ndym10n/hermes-agent"
    open_prs = [
        {
            "number": 1,
            "title": "Broken CI",
            "url": f"https://github.com/{repository}/pull/1",
            "statusCheckRollup": [{"conclusion": "FAILURE"}],
            "reviewDecision": "CHANGES_REQUESTED",
        },
        {
            "number": 2,
            "title": "Review findings",
            "url": "https://evil.example/pull/2",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "reviewDecision": "CHANGES_REQUESTED",
        },
        {
            "number": 3,
            "title": "Active implementation",
            "url": f"https://github.com/{repository}/pull/3",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "isDraft": True,
        },
        {
            "number": 4,
            "title": "Ready implementation",
            "url": f"https://github.com/{repository}/pull/4",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        },
        {
            "number": 5,
            "title": "Governed ecommerce executor",
            "headRefName": "feat/ecommerce",
            "url": f"https://github.com/{repository}/pull/5",
        },
        {"number": True, "title": "invalid identity"},
    ]
    merged = [
        {
            "number": 6,
            "title": "Merged result",
            "url": f"https://github.com/{repository}/pull/6",
            "mergedAt": "2033-05-17T20:00:00Z",
            "statusCheckRollup": [{"conclusion": "FAILURE"}],
        }
    ]

    facts = sources.github_pr_facts(repository, open_prs, merged)
    by_title = {fact.title: fact for fact in facts}
    assert [fact.kind for fact in facts] == [
        "github_failing",
        "github_review",
        "github_open",
        "github_ready",
        "github_merged",
    ]
    review = next(fact for fact in facts if "PR #2" in fact.title)
    assert review.deep_link is None
    assert all("ecommerce" not in title.casefold() for title in by_title)
    merged_fact = next(fact for fact in facts if "PR #6" in fact.title)
    assert sources.policy_for(merged_fact).status == "resolved"

    cogitator_fact = sources.github_pr_facts(
        "3ndym10n/Cogitator",
        [{"number": 7, "title": "Ready", "statusCheckRollup": []}],
        [],
    )[0]
    assert sources.policy_for(cogitator_fact).project == "cogitator"


def test_stale_worktree_requires_dirty_old_branch_without_open_pr(
    monkeypatch, tmp_path
):
    repository_path = tmp_path / "repo"
    stale_path = tmp_path / "stale"
    repository_path.mkdir()
    stale_path.mkdir()
    listed = (
        f"worktree {repository_path}\nHEAD {'a' * 40}\nbranch refs/heads/main\n\n"
        f"worktree {stale_path}\nHEAD {'b' * 40}\n"
        "branch refs/heads/feat/stale\n"
    )

    def run(args, **_kwargs):
        if args[1:] == ["worktree", "list", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout=listed)
        if args[1:] == ["log", "-1", "--format=%ct"]:
            return SimpleNamespace(
                returncode=0,
                stdout=str((NOW - timedelta(days=15)).timestamp()),
            )
        raise AssertionError(args)

    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(
        sources,
        "_repository_risk",
        lambda _repository, location, _sha: (
            object() if location == stale_path else None
        ),
    )
    facts = sources._stale_worktree_facts(
        "3ndym10n/hermes-agent", repository_path, "c" * 40, set(), NOW
    )

    assert len(facts) == 1
    assert facts[0].kind == "repo_risk"
    assert str(stale_path) not in json.dumps(facts[0].__dict__)
    assert (
        sources._stale_worktree_facts(
            "3ndym10n/hermes-agent",
            repository_path,
            "c" * 40,
            {"feat/stale"},
            NOW,
        )
        == ()
    )


def test_ecommerce_reports_truthful_no_feed(monkeypatch, tmp_path):
    monkeypatch.setattr(sources.shutil, "which", lambda _name: None)
    result = sources.sync_ecommerce(
        sources.SourceContext(
            NOW,
            {
                "attention": {
                    "operational_sources": {
                        "commerce_db_path": str(tmp_path / "missing.db")
                    }
                }
            },
        )
    )
    assert result.status == "unavailable"
    assert result.facts == ()
    assert (
        result.message
        == "No trustworthy ecommerce runtime feed is currently available."
    )


def test_ecommerce_connected_empty_store_reports_no_current_work(tmp_path):
    database = tmp_path / "commerce.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE jobs(job_id TEXT,current_state TEXT,substatus TEXT,"
            "deadline_at TEXT,active INTEGER,updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE gates(gate_id TEXT,job_id TEXT,gate_type TEXT,status TEXT,"
            "opened_at TEXT,expires_at TEXT)"
        )
    database.chmod(0o600)
    result = sources.sync_ecommerce(
        sources.SourceContext(
            NOW,
            {"attention": {"operational_sources": {"commerce_db_path": str(database)}}},
            {"github_open_prs": {"3ndym10n/hermes-agent": []}},
        )
    )

    assert result.status == "no_current_work"
    assert result.facts == ()
    assert result.message == "Ecommerce is connected with no current work."


def test_ecommerce_detects_features_without_mutating_private_database(tmp_path):
    database = tmp_path / "commerce.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE jobs(job_id TEXT,current_state TEXT,substatus TEXT,"
            "deadline_at TEXT,active INTEGER,updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE gates(gate_id TEXT,job_id TEXT,gate_type TEXT,status TEXT,"
            "opened_at TEXT,expires_at TEXT)"
        )
        conn.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?)",
            (
                "job-secret-id",
                "failed",
                "PRIVATE FAILURE DETAIL",
                "2033-05-19T00:00:00Z",
                1,
                "2033-05-17T23:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO gates VALUES(?,?,?,?,?,?)",
            (
                "gate-secret-id",
                "job-secret-id",
                "login",
                "open",
                "2033-05-17T22:00:00Z",
                "2033-05-19T00:00:00Z",
            ),
        )
    database.chmod(0o600)
    before = database.read_bytes(), database.stat().st_mtime_ns
    context = sources.SourceContext(
        NOW,
        {"attention": {"operational_sources": {"commerce_db_path": str(database)}}},
        {"github_open_prs": {"3ndym10n/hermes-agent": []}},
    )

    result = sources.sync_ecommerce(context)

    assert result.status == "blocked"
    assert {fact.kind for fact in result.facts} == {
        "commerce_failed",
        "commerce_gate",
    }
    serialized = json.dumps([fact.__dict__ for fact in result.facts])
    assert "PRIVATE FAILURE DETAIL" not in serialized
    assert "job-secret-id" not in serialized
    assert "gate-secret-id" not in serialized
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


def test_tailscale_boundary_requires_private_8443_proxy():
    assert sources._tailscale_private_ok(_tailscale_status()) is True
    assert sources._tailscale_private_ok(_tailscale_status(funnel=True)) is False
    assert (
        sources._tailscale_private_ok(_tailscale_status(proxy="http://127.0.0.1:9999"))
        is False
    )
    assert sources._tailscale_private_ok({}) is False


def test_system_health_detects_service_timer_checkpoint_and_access(monkeypatch):
    old_trigger = (NOW - timedelta(minutes=10)).strftime("%a %Y-%m-%d %H:%M:%S UTC")
    fresh_trigger = NOW.strftime("%a %Y-%m-%d %H:%M:%S UTC")

    def unit_state(unit, *, user):
        del user
        if unit == "hermes-research-bridge.service":
            return {"LoadState": "not-found"}
        state = {
            "LoadState": "loaded",
            "ActiveState": "active",
            "Result": "success",
            "NRestarts": "0",
            "ExecMainStatus": "0",
            "LastTriggerUSec": fresh_trigger,
        }
        if unit in {"virgil-mobile.service", "virgil-operational-sources.service"}:
            state["ActiveState"] = "failed"
            state["Result"] = "failed"
        elif unit == "linxio-incoming-autodraft.timer":
            state["LastTriggerUSec"] = old_trigger
        return state

    monkeypatch.setattr(sources, "_unit_state", unit_state)
    monkeypatch.setattr(sources, "_attention_db_healthy", lambda _path: True)
    monkeypatch.setattr(
        sources, "_run_json", lambda *_args, **_kwargs: _tailscale_status()
    )
    monkeypatch.setattr(
        sources.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=20 * 1024**3),
    )
    monkeypatch.setattr(
        sources,
        "_gmail_meta",
        lambda: {
            "authentication_health": "ok",
            "last_successful_poll": str(NOW.timestamp()),
            "history_watermark": "",
            "verified_account_fingerprint": "",
        },
    )

    result = sources.sync_system(sources.SourceContext(NOW, {}))
    ids = {fact.record_id for fact in result.facts}
    assert result.status == "degraded"
    assert "health:unit:virgil-mobile.service" in ids
    assert "health:unit:virgil-operational-sources.service" in ids
    assert "health:timer-stale:linxio-incoming-autodraft.timer" in ids
    assert "health:gmail-checkpoint" in ids
    assert "health:unit:hermes-research-bridge.service" not in ids
    assert "health:tailscale-8443" not in ids

    def missing_gmail():
        raise sources.SourceError("gmail_state_invalid")

    monkeypatch.setattr(sources, "_gmail_meta", missing_gmail)
    monkeypatch.setattr(sources, "_run_json", lambda *_args, **_kwargs: {})
    failed = sources.sync_system(sources.SourceContext(NOW, {}))
    failed_ids = {fact.record_id for fact in failed.facts}
    assert "health:gmail-state" in failed_ids
    assert "health:tailscale-8443" in failed_ids
    assert (
        sources.policy_for(
            next(
                fact
                for fact in failed.facts
                if fact.record_id == "health:tailscale-8443"
            )
        ).priority
        == "urgent"
    )


def test_orchestrator_is_idempotent_pauses_and_never_notifies_temp_db(
    tmp_path, monkeypatch
):
    database = tmp_path / "attention" / "attention.db"
    fact = sources.SourceFact(
        "calendar",
        "calendar:test",
        "calendar-event-v1",
        "calendar_conflict",
        "Calendar conflict",
        "Two events overlap.",
        "Review the conflict.",
    )
    adapter = lambda _context: sources.SourceResult(
        "active", "Calendar active.", (fact,)
    )
    monkeypatch.setattr(sources, "load_config_readonly", lambda: {})
    monkeypatch.setattr(
        sources,
        "deliver_attention_result",
        lambda _result: (_ for _ in ()).throw(AssertionError("notification sent")),
    )

    first = sources.run_sources(
        force=True,
        sources=["calendar"],
        db_path=database,
        adapters={"calendar": adapter},
        now=NOW,
    )
    second = sources.run_sources(
        force=True,
        sources=["calendar"],
        db_path=database,
        adapters={"calendar": adapter},
        now=NOW,
    )
    items = attention.list_attention(
        view="needs-you", now=NOW.timestamp(), db_path=database
    )
    assert first == second == [{"source": "calendar", "status": "active", "facts": 1}]
    assert len(items) == 1
    assert items[0]["row_version"] == 1
    assert {
        event["event_type"] for event in attention.list_activity(db_path=database)
    } == {"created", "state_changed"}

    attention.control_source("calendar", "pause", db_path=database)
    skipped = sources.run_sources(
        force=True,
        sources=["calendar"],
        db_path=database,
        adapters={
            "calendar": lambda _context: (_ for _ in ()).throw(
                AssertionError("paused adapter ran")
            )
        },
        now=NOW,
    )
    assert skipped == [{"source": "calendar", "status": "skipped"}]


def test_orchestrator_retries_and_isolates_adapter_and_ingestion_failures(
    tmp_path, monkeypatch
):
    database = tmp_path / "attention.db"
    calls = {"calendar": 0, "cogitator": 0, "github": 0, "ecommerce": 0}

    def flaky(_context):
        calls["calendar"] += 1
        if calls["calendar"] == 1:
            raise sources.SourceError("temporary_calendar_failure")
        return sources.SourceResult("active", "Calendar recovered.")

    def broken(_context):
        calls["cogitator"] += 1
        raise RuntimeError("raw exception must stay bounded")

    def invalid_ingestion(_context):
        calls["github"] += 1
        return sources.SourceResult(
            "active",
            "GitHub active.",
            (
                sources.SourceFact(
                    "github",
                    "github:test",
                    "event-test",
                    "github_ready",
                    "Ready PR",
                    "Ready.",
                    "Review it.",
                    deep_link="https://evil.example/pull/1",
                ),
            ),
        )

    def healthy(_context):
        calls["ecommerce"] += 1
        return sources.SourceResult("active", "Ecommerce active.")

    monkeypatch.setattr(sources, "load_config_readonly", lambda: {})
    output = sources.run_sources(
        force=True,
        sources=["calendar", "cogitator", "github", "ecommerce"],
        db_path=database,
        adapters={
            "calendar": flaky,
            "cogitator": broken,
            "github": invalid_ingestion,
            "ecommerce": healthy,
        },
        now=NOW,
    )

    assert calls == {"calendar": 2, "cogitator": 2, "github": 2, "ecommerce": 1}
    assert output == [
        {"source": "calendar", "status": "active", "facts": 0},
        {
            "source": "cogitator",
            "status": "failed",
            "error_code": "source_sync_failed",
        },
        {
            "source": "github",
            "status": "failed",
            "error_code": "queue_ingestion_failed",
        },
        {"source": "ecommerce", "status": "active", "facts": 0},
    ]


def test_orchestrator_lock_prevents_overlap(tmp_path, monkeypatch):
    database = tmp_path / "attention.db"
    attention.list_source_statuses(db_path=database)
    lock = sources._acquire_lock(database.parent / ".operational-sources.lock")
    monkeypatch.setattr(sources, "load_config_readonly", lambda: {})
    try:
        with pytest.raises(sources.WorkerBusy, match="worker_overlap"):
            sources.run_sources(
                force=True,
                sources=["calendar"],
                db_path=database,
                adapters={
                    "calendar": lambda _context: sources.SourceResult(
                        "active", "Calendar active."
                    )
                },
                now=NOW,
            )
    finally:
        os.close(lock)
