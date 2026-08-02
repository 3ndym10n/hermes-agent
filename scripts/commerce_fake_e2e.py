#!/usr/bin/env python3
"""Offline Virgil commerce acceptance rehearsal.

Runs the production store, operator, workflow, adapters, gate-card path, native
browser-stream validators, and receipt builder against repository fakes only.
No provider hostname is resolved and no external mutation is possible.

Usage: .venv/bin/python scripts/commerce_fake_e2e.py
"""

from __future__ import annotations

import copy
import json
import queue
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import virgil_gate_routes as gate_routes  # noqa: E402
from commerce_browser import validate_browser_binding  # noqa: E402
from commerce_jobs import CommerceJobStore  # noqa: E402
from commerce_operator import CommerceOperator  # noqa: E402
from commerce_receipt import (  # noqa: E402
    build_execution_receipt,
    persist_execution_receipt,
)
from commerce_workflow import (  # noqa: E402
    CANDIDATE_DOMAINS,
    production_gate_verifiers,
    production_handlers,
    production_plan,
)
from gateway import commerce_watcher  # noqa: E402
from gateway.commerce_buttons import (  # noqa: E402
    DEFAULT_TTL_SECONDS,
    CommerceButtonStore,
)
from gateway.commerce_watcher import commerce_gate_status  # noqa: E402
from shopify_admin import ShopifyAdminClient  # noqa: E402
from tests.test_commerce_workflow import FACTS, Porkbun  # noqa: E402
from tests.test_shopify_admin import FakeShopify, TOKEN  # noqa: E402
from tests.test_virgil_gate import fake_stream  # noqa: E402
from websockets.sync.client import connect as stream_connect  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "acceptance"
PUBLIC_GATE_URL = "https://virgil-server.tailce4511.ts.net:8443"
FACT_KEYS = (
    "checkout_absent_verified",
    "dns",
    "domain",
    "no_payment_collected",
    "public_url",
    "shopify",
    "total_spend",
    "unresolved",
    "verification",
    "waitlist_test",
)


def require(condition: bool, invariant: str) -> None:
    if not condition:
        raise SystemExit(f"COMMERCE FAKE E2E FAIL: {invariant}")


def load_json(name: str) -> dict:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{name} is an object")
    return value


class GoldenStore:
    """Minimal immutable ledger view used to deep-check the receipt golden."""

    def __init__(self, receipt: dict):
        facts = {key: copy.deepcopy(receipt[key]) for key in FACT_KEYS}
        self._snapshot = {
            "job": {
                "job_id": receipt["job_id"],
                "state_machine_version": receipt["state_machine_version"],
                "current_state": "verifying",
                "original_objective": receipt["objective"],
            },
            "actions": [
                {
                    "action_type": item["step"],
                    "action_status": "succeeded",
                    "effect_class": "consequential" if index == 0 else "read_only",
                    "uncertainty": False,
                    "terminal_at": item["at"],
                    "result": {
                        "evidence_ref": item["evidence"],
                        **(
                            {
                                "provider_truth_verified": True,
                                "receipt_facts": facts,
                            }
                            if index == 1
                            else {}
                        ),
                    },
                }
                for index, item in enumerate(receipt["actions_completed"])
            ],
            "gates": [
                {
                    "gate_id": item["gate_id"],
                    "gate_type": item["type"],
                    "status": "consumed",
                    "opened_at": item["opened"],
                    "completed_at": item["verified"],
                }
                for item in receipt["human_gates_completed"]
            ],
            "events": [],
        }

    def delivery_snapshot(self, _job_id: str) -> dict:
        return copy.deepcopy(self._snapshot)


def assert_exact_golden(receipt: dict, root: Path) -> None:
    rebuilt = build_execution_receipt(GoldenStore(receipt), receipt["job_id"])
    require(rebuilt == receipt, "receipt builder reproduces the exact golden")
    path = persist_execution_receipt(
        GoldenStore(receipt), receipt["job_id"], receipts_root=root
    )
    require(json.loads(path.read_text(encoding="utf-8")) == receipt, "golden persists")
    require(stat.S_IMODE(path.stat().st_mode) == 0o600, "golden receipt mode is 0600")


def receipt_facts(golden: dict, job_id: str) -> dict:
    facts = {key: copy.deepcopy(golden[key]) for key in FACT_KEYS}
    facts["domain"]["order_id"] = "1234"
    facts["verification"]["evidence_bundle"] = f"evidence/{job_id}/verification/"
    return facts


def assert_decision_packet(
    fixture: dict,
    store: CommerceJobStore,
    job: dict,
    evidence_root: Path,
) -> None:
    plan = job["plan"]
    require(plan["workflow"] == fixture["workflow"], "workflow name is exact")
    require(plan["availability"] == fixture["availability"], "ten-candidate table")
    require(len(plan["availability"]) == len(CANDIDATE_DOMAINS) == 10, "ten candidates")
    require(plan["recommendation"] == fixture["recommendation"], "recommendation")

    registration = next(
        step for step in plan["steps"] if step["step_id"] == "s02_porkbun_register"
    )["request"]
    quote = fixture["quote"]
    for field in (
        "auto_renew",
        "cancellation_deadline",
        "currency",
        "quote_timestamp",
        "renewal_date",
        "renewal_usd_cents",
        "whois_privacy",
    ):
        require(registration[field] == quote[field], f"quote field {field}")
    require(registration["domain"] == fixture["recommendation"], "quote domain")
    require(
        registration["cost_usd_cents"] == quote["registration_usd_cents"],
        "registration quote cents",
    )
    aud = (
        Decimal(registration["cost_usd_cents"])
        / 100
        * Decimal(quote["fixture_fx_rate_usd_to_aud"])
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    require(quote["display_usd"] == "US$11.98", "USD display")
    require(quote["display_aud"] == f"A${aud}", "AUD display")

    evidence_ref = Path(plan["discovery_evidence"])
    require(evidence_ref.parts[0] == "evidence", "discovery evidence is relative")
    discovery = json.loads(
        (evidence_root.joinpath(*evidence_ref.parts[1:])).read_text(encoding="utf-8")
    )
    require(discovery["dry_run"] == fixture["dry_run"], "exact dry-run result")

    action = next(
        item
        for item in store.list_actions(job["job_id"])
        if item["action_type"] == "porkbun_register_domain"
    )
    gate = next(
        item
        for item in store.list_gates(job["job_id"])
        if item["approval_fingerprint"] == action["action_fingerprint"]
    )
    schema = fixture["approval_buttons"]["callback_data"]
    require(schema["ttl_seconds"] == DEFAULT_TTL_SECONDS, "button TTL")
    buttons = CommerceButtonStore(default_ttl_seconds=schema["ttl_seconds"])
    tokens = buttons.mint_group(
        job_id=job["job_id"],
        gate_id=gate["gate_id"],
        action_fingerprint=action["action_fingerprint"],
        plan_fingerprint=job["plan_fingerprint"],
        expected_row_version=job["row_version"],
        user_id="4242",
        chat_id="-10042",
        message_id="501",
        actions=fixture["approval_buttons"]["actions"],
    )
    require(set(tokens) == {"approve", "deny"}, "approval and deny tokens")
    for raw in tokens.values():
        require(re.fullmatch(r"[A-Za-z0-9_-]{32}", raw) is not None, "opaque token")
        binding = buttons.resolve(
            raw, user_id="4242", chat_id="-10042", message_id="501"
        )
        require(
            binding.action_fingerprint == action["action_fingerprint"], "token binding"
        )
    require(
        all(
            isinstance(digest, bytes) and len(digest) == 32
            for digest in buttons._entries
        ),
        "button store retains SHA-256 digests only",
    )


def request_human_gate_done(
    store: CommerceJobStore, job_id: str, *, match_fixture: bool
) -> None:
    expected = (FIXTURES / "gate_card.md").read_text(encoding="utf-8").strip()
    original = commerce_watcher.attention_public_url
    commerce_watcher.attention_public_url = lambda: PUBLIC_GATE_URL
    try:
        card = commerce_gate_status(store, job_id, actor="fake-e2e")
    finally:
        commerce_watcher.attention_public_url = original
    require(len(re.findall(r"https://", card)) == 1, "gate card has one HTTPS link")
    require(
        "password" not in card.casefold() and "secret" not in card.casefold(),
        "gate card has no secret request",
    )
    normalized = re.sub(
        r"/gate/cg_[A-Za-z0-9_]+\?t=cgh_[A-Za-z0-9_-]+",
        "/gate/cg_fixture?t=cgh_fixture",
        card,
    )
    if match_fixture:
        require(normalized == expected, "rendered gate card matches fixture")
    link = next(line for line in card.splitlines() if line.startswith("https://"))
    token = parse_qs(urlsplit(link).query, strict_parsing=True)["t"][0]
    gate_id = urlsplit(link).path.rsplit("/", 1)[1]
    store.request_gate_done(gate_id, token, actor="cal:fake-viewer")


# Progress-aware driver ------------------------------------------------------
#
# A fixed tick count cannot tell "still working" from "wedged": it fails late,
# with no diagnosis, and it flakes whenever the flow legitimately needs one
# more pass. Drive on durable progress instead -- keep ticking while the job
# row, its actions or its gates change, and stop the moment they stop.

MAX_NO_PROGRESS_TICKS = 3
# Safety net only. Reaching this is itself a failure, never the way we finish.
ABSOLUTE_TICK_CEILING = 400


def durable_fingerprint(store: CommerceJobStore, job_id: str) -> tuple:
    """Everything that must change for the job to be making real progress."""
    job = store.get_job(job_id)
    return (
        job["current_state"],
        int(job["row_version"]),
        job["current_step"],
        job.get("current_gate_id") or "",
        tuple(
            (item["action_id"], item["action_status"])
            for item in store.list_actions(job_id)
        ),
        tuple(
            (item["gate_id"], item["status"], bool(item.get("done_requested_at")))
            for item in store.list_gates(job_id)
        ),
    )


def stall_report(store: CommerceJobStore, job_id: str, reason: str, ticks: int) -> str:
    job = store.get_job(job_id)
    return (
        f"{reason} after {ticks} ticks: "
        f"state={job['current_state']} step={job['current_step']!r} "
        f"gate={job.get('current_gate_id') or None} version={job['row_version']}\n"
        f"  actions={[(i['action_type'], i['action_status']) for i in store.list_actions(job_id)]}\n"
        f"  gates={[(i['gate_type'], i['status'], bool(i.get('done_requested_at'))) for i in store.list_gates(job_id)]}\n"
        f"  events={[(i['to_state'], i['reason_code']) for i in store.list_events(job_id)[-8:]]}"
    )


def drive_to_completion(worker, store: CommerceJobStore, job_id: str, on_state) -> int:
    """Tick until `completed`, failing precisely when durable progress stops."""
    stalls = 0
    for tick_index in range(ABSOLUTE_TICK_CEILING):
        before = durable_fingerprint(store, job_id)
        worker.tick()
        on_state(store.get_job(job_id)["current_state"])
        after = durable_fingerprint(store, job_id)
        if store.get_job(job_id)["current_state"] == "completed":
            return tick_index + 1
        if after == before:
            stalls += 1
            if stalls >= MAX_NO_PROGRESS_TICKS:
                raise SystemExit(
                    "COMMERCE FAKE E2E FAIL: "
                    + stall_report(
                        store,
                        job_id,
                        f"no durable progress for {stalls} consecutive ticks",
                        tick_index + 1,
                    )
                )
        else:
            stalls = 0
    raise SystemExit(
        "COMMERCE FAKE E2E FAIL: "
        + stall_report(
            store, job_id, "absolute tick ceiling reached", ABSOLUTE_TICK_CEILING
        )
    )


def make_browser_ensure(port: int, received: queue.Queue, attaches: list[str]):
    def ensure(job_id: str, session: str, _entry_url: str) -> dict:
        validate_browser_binding(job_id, session)
        with stream_connect(f"ws://127.0.0.1:{port}", compression=None) as upstream:
            seen = set()
            for _ in range(3):
                raw = upstream.recv()
                require(isinstance(raw, str), "native stream sends text")
                safe = gate_routes._valid_frame(
                    raw
                ) or gate_routes._valid_native_notice(raw)
                require(
                    safe is not None, "gate viewer accepts fake native stream message"
                )
                payload = json.loads(safe)
                seen.add(payload["type"])
                require("url" not in safe, "tab URL is stripped")
                require("engine" not in safe, "browser engine is stripped")
            require(seen == {"status", "tabs", "frame"}, "status/tabs/frame stream")
            event = gate_routes._validated_input({
                "type": "input_keyboard",
                "eventType": "keyDown",
                "key": "Enter",
                "code": "Enter",
            })
            require(event is not None, "viewer input validates")
            upstream.send(json.dumps(event, separators=(",", ":")))
            require(
                received.get(timeout=3)["type"] == "input_keyboard",
                "input reaches fake CDP",
            )
        attaches.append(session)
        return {"profile": "fake", "reattached": True, "session": session}

    return ensure


def assert_actual_receipt(actual: dict, golden: dict, request: dict) -> None:
    require(
        actual["objective"] == request["text"], "Telegram objective reaches receipt"
    )
    require(
        len(actual["actions_completed"]) == 15, "all production workflow steps complete"
    )
    require(
        [item["step"] for item in actual["actions_completed"]]
        == [
            "porkbun_discover",
            "porkbun_register_domain",
            "porkbun_dns_snapshot",
            "porkbun_dns_apply",
            "shopify_credentials",
            "shopify_identity",
            "shopify_build",
            "shopify_theme_verify",
            "shopify_plan_gate",
            "shopify_plan_verify",
            "shopify_domain_gate",
            "shopify_domain_verify",
            "commerce_prepublish_verify",
            "shopify_publish",
            "commerce_final_verify",
        ],
        "production steps stay ordered",
    )
    for field in (
        "checkout_absent_verified",
        "dns",
        "no_payment_collected",
        "public_url",
        "shopify",
        "total_spend",
        "unresolved",
        "waitlist_test",
    ):
        require(actual[field] == golden[field], f"receipt contract field {field}")
    require(actual["domain"]["name"] == golden["domain"]["name"], "receipt domain")
    require(actual["domain"]["spend"] == golden["domain"]["spend"], "receipt spend")
    require(actual["domain"]["auto_renew"] is True, "receipt auto-renew")
    require(actual["domain"]["whois_privacy"] is True, "receipt privacy")
    require(actual["verification"]["checklist"] == "9.3", "verification checklist")
    require(actual["verification"]["all_green"] is True, "all verification green")


def main() -> int:
    request = load_json("telegram_request.json")
    decision = load_json("decision_packet.json")
    golden = load_json("receipt.json")
    require(
        request["text"] == "Set up the AMD GPU waitlist store.", "acceptance sentence"
    )

    with tempfile.TemporaryDirectory(prefix="commerce-fake-e2e-") as directory:
        root = Path(directory)
        assert_exact_golden(golden, root / "golden-receipts")
        store = CommerceJobStore(root / "commerce.db")
        store.initialize()
        job = store.create_or_attach_job(
            requester=f"telegram:{request['user_id']}",
            objective=request["text"],
            origin={
                "platform": "telegram",
                "chat_id": request["chat_id"],
                "user_id": request["user_id"],
                "message_id": request["message_id"],
            },
        )
        porkbun = Porkbun()
        porkbun.price = "11.98"
        shopify_transport = FakeShopify()
        shopify = ShopifyAdminClient(
            "silicon-current.myshopify.com",
            TOKEN,
            transport=shopify_transport,
            storefront_transport=lambda _url, _timeout, _limit: (
                200,
                {"Content-Type": "text/html"},
                b"<html><body>Silicon Current</body></html>",
            ),
        )
        evidence_root = root / "evidence"

        def verify(current_job, _client, _package, phase):
            report = {
                "all_green": True,
                "checkout_absent_verified": True,
                "no_payment_collected": True,
                "checklist": "9.3",
            }
            if phase == "final":
                report["receipt_facts"] = receipt_facts(golden, current_job["job_id"])
            return report

        persisted: dict[str, object] = {}

        def complete(job_id: str, payload: dict) -> dict:
            path = persist_execution_receipt(
                store,
                job_id,
                verified_facts=payload["verified_facts"],
                receipts_root=root / "receipts",
            )
            persisted["path"] = path
            persisted["receipt"] = json.loads(path.read_text(encoding="utf-8"))
            return {"receipt_ref": f"receipts/{path.name}"}

        handlers = production_handlers(
            store,
            porkbun_factory=lambda: porkbun,
            shopify_factory=lambda: shopify,
            facts_loader=lambda _job: FACTS,
            verify=verify,
            publish=lambda _job, _client: {"public": True},
            clock=lambda: datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
            sleep=lambda _seconds: None,
            evidence_root=evidence_root,
        )
        verifiers = production_gate_verifiers(
            shopify_factory=lambda: shopify,
            settings_verify=lambda _job, _client: True,
            theme_verify=lambda _job, _client: True,
        )
        received: queue.Queue = queue.Queue()
        attaches: list[str] = []
        decision_checked = False
        human_gates_checked = 0

        with fake_stream(received) as port:
            worker = CommerceOperator(
                store=store,
                planner=production_plan,
                step_handlers=handlers,
                enabled_fn=lambda: True,
                lock_path=root / "operator.lock",
                browser_ensure=make_browser_ensure(port, received, attaches),
                approved_facts_loader=lambda: FACTS,
                gate_verifiers=verifiers,
                completion_handler=complete,
            )

            def respond(state: str) -> None:
                """Play Cal: approvals and gate DONEs, exactly once each."""
                nonlocal decision_checked, human_gates_checked
                current = store.get_job(job["job_id"])
                if state == "awaiting_purchase_approval":
                    if not decision_checked:
                        assert_decision_packet(decision, store, current, evidence_root)
                        decision_checked = True
                    gate = next(
                        item
                        for item in store.list_gates(job["job_id"])
                        if item["status"] == "open"
                    )
                    store.complete_gate(
                        gate["gate_id"],
                        evidence={
                            "provider_truth_verified": True,
                            "approval_granted": True,
                            "proposal_id": "pp_receipt_test",
                            "approval_reference": "pa_receipt_test",
                        },
                        actor="cal:fake-approval",
                    )
                elif state == "awaiting_publication_approval":
                    gate = next(
                        item
                        for item in store.list_gates(job["job_id"])
                        if item["status"] == "open"
                    )
                    store.complete_gate(
                        gate["gate_id"],
                        evidence={
                            "provider_truth_verified": True,
                            "approval_granted": True,
                        },
                        actor="cal:fake-approval",
                    )
                elif state == "awaiting_cal":
                    request_human_gate_done(
                        store, job["job_id"], match_fixture=human_gates_checked == 0
                    )
                    human_gates_checked += 1

            ticks = drive_to_completion(worker, store, job["job_id"], respond)
            final = store.get_job(job["job_id"])

        require(
            final["current_state"] == "completed",
            "job reaches completed "
            f"(state={final['current_state']}, actions="
            f"{[(item['action_type'], item['action_status'], item['result']) for item in store.list_actions(job['job_id'])]}, events="
            f"{[(item['to_state'], item['reason_code'], item['evidence']) for item in store.list_events(job['job_id'])[-8:]]}, gates="
            f"{[(item['gate_type'], item['status'], item['done_requested_at']) for item in store.list_gates(job['job_id'])]})",
        )
        require(decision_checked, "decision packet exercised")
        require(
            human_gates_checked == len(attaches) == 2,
            "settings and theme fake CDP handoffs",
        )
        require(porkbun.registrations == 1, "domain mutation executes exactly once")
        require(porkbun.writes == 3, "three DNS records execute exactly once")
        require(len(shopify_transport.pages) == 2, "two Shopify pages upserted")
        require(len(shopify_transport.menus) == 1, "one Shopify menu upserted")
        actual = persisted.get("receipt")
        require(isinstance(actual, dict), "workflow receipt persisted")
        assert_actual_receipt(actual, golden, request)

        sensitive = (TOKEN, FACTS["contact_email"])
        disk = b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file())
        require(
            all(value.encode("utf-8") not in disk for value in sensitive),
            "no fake token or contact address persisted",
        )
        print(
            json.dumps(
                {
                    "fake_e2e": "PASS",
                    "ticks_to_complete": ticks,
                    "tick_ceiling": ABSOLUTE_TICK_CEILING,
                    "actions": len(actual["actions_completed"]),
                    "browser_handoffs": len(attaches),
                    "candidate_domains": len(decision["availability"]),
                    "dns_writes": porkbun.writes,
                    "golden_receipt": "exact",
                    "network": "loopback-only",
                    "provider_mutations": "fake-only",
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
