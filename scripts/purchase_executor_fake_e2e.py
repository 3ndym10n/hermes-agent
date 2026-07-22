"""Fake end-to-end acceptance for semantic purchase discovery (issues #65/#73/#75).

Everything is local and synthetic:
  * temporary SQLite governance DB seeded with one approved proposal + ticket,
  * the REAL Cogitator bridge HTTP handler/auth/validation/governance modules
    (imported from a local Cogitator checkout) with
    ENABLE_PURCHASE_EXECUTOR_BRIDGE=true for this process only,
  * a loopback mock merchant with a plain HTML checkout form,
  * synthetic payment fields in a temporary $CREDENTIALS_DIRECTORY,
  * the real local browser path (tools/browser_tool → local Chromium).

No Railway, no production database, no real credentials, no systemd, no real
merchant, no purchase. Proves: claim → fill → revalidate → submit → completed
callback; ledger changed exactly once; second claim rejected; unauthenticated
bridge call rejected; zero residual browser processes.

Usage:
    venv/bin/python scripts/purchase_executor_fake_e2e.py \
        --cogitator-repo /path/to/Cogitator
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SAME_PAGE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "payment_same_page_v0.html"
HOSTED_PARENT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "payment_hosted_parent_v0.html"
HOSTED_FRAME_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "payment_hosted_frame_v0.html"


CONFIRM_HTML = b"""<!doctype html><html><body>
<h1>Order confirmed</h1>
<p>Reference FAKE-REF-1</p>
<p>Thank you for your purchase.</p>
<p>example.com domain registration - Total: 22.00 AUD</p>
</body></html>"""


class MockMerchant(BaseHTTPRequestHandler):
    checkout_html = b""
    authenticated_session = False
    requests = []

    def log_message(self, *args):
        pass

    def _send(self, body: bytes, headers=()):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        request = urllib.parse.urlsplit(self.path)
        self.requests.append(("GET", request.path))
        if request.path == "/__hermes_session_handoff" and self.authenticated_session:
            destination = urllib.parse.parse_qs(request.query).get("return", [""])[0]
            if destination == "/checkout?product_kind=domain_registration&product_id=example.com&quantity=1":
                self.send_response(302)
                self.send_header("Location", destination)
                self.send_header(
                    "Set-Cookie",
                    "hermes_mock_session=ready; Path=/; HttpOnly; SameSite=Strict",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if self.path == "/checkout?product_kind=domain_registration&product_id=example.com&quantity=1":
            body = self.checkout_html
            cookie = self.headers.get("Cookie", "")
            if self.authenticated_session and "hermes_mock_session=ready" in cookie:
                body = body.replace(
                    b'<html lang="en">',
                    b'<html lang="en" data-hermes-prepared-session="authenticated">',
                    1,
                )
            self._send(body)
            return
        self._send(b"<html>404</html>")

    def do_POST(self):
        self.requests.append(("POST", urllib.parse.urlsplit(self.path).path))
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send(CONFIRM_HTML)


class MockProcessor(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = (
            HOSTED_FRAME_FIXTURE.read_bytes()
            if self.path.startswith("/fields")
            else b"<html>404</html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(204)
        self.end_headers()


def browser_proc_count() -> int:
    result = subprocess.run(
        ["pgrep", "-u", os.environ.get("USER", "v0id"), "-f",
         "chrome|chromium|headless_shell|playwright"],
        capture_output=True, text=True,
    )
    return len([line for line in result.stdout.splitlines() if line.strip()])


def bridge_request(url: str, token: str | None, packet: dict) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(packet).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode())
        except Exception:
            return error.code, {}


def require(condition: bool, invariant: str) -> None:
    if not condition:
        raise SystemExit(f"FAKE E2E FAIL: {invariant}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cogitator-repo", required=True)
    parser.add_argument(
        "--hosted-frame", action="store_true",
        help="serve card fields from a second exact-allowlisted loopback origin",
    )
    parser.add_argument(
        "--authenticated-session", action="store_true",
        help="prove a synthetic cookie-backed prepared session in the same browser task",
    )
    parser.add_argument(
        "--credentials-dir", default="",
        help="use externally-provided synthetic credentials (e.g. the staging "
        "unit's systemd $CREDENTIALS_DIRECTORY) instead of generating them; "
        "proves the systemd credential name-binding. The card fields must be the "
        "public 4242 test values the mock merchant expects.",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.cogitator_repo).resolve()))
    import cogitator_bridge
    import cogitator_bridge_http as bridge_http
    import cogitator_purchase_governance as governance

    os.environ["ENABLE_PURCHASE_EXECUTOR_BRIDGE"] = "true"
    enabled = os.environ["ENABLE_PURCHASE_EXECUTOR_BRIDGE"].strip().lower() == "true"
    bridge_token = secrets.token_urlsafe(24)

    tmp = Path(tempfile.mkdtemp(prefix="purchase-fake-e2e-"))
    db = tmp / "governance.db"

    # --- seed: proposal → Cal approval → execution ticket --------------------
    now = datetime.now(timezone.utc)
    proposal_payload = {
        "project_id": "website_launch",
        "budget_scope": "website_launch_v1",
        "requester": "fake-e2e",
        "idempotency_key": "fake-e2e-proposal",
        "source_ref": "fake-e2e:purchase-executor-v0",
        "purpose": "Prove the executor loop against a loopback mock merchant",
        "merchant_display_name": "Fake Registrar",
        "merchant_domain": "registrar.example",
        "merchant_url": "https://registrar.example/checkout",
        "product_or_service": "example.com domain registration",
        "purchase_class": "domain",
        "quantity": 1,
        "quoted_subtotal": "20.00",
        "tax": "2.00",
        "mandatory_fees": "0.00",
        "final_quoted_total": "22.00",
        "currency": "AUD",
        "quote_timestamp": (now - timedelta(minutes=5)).isoformat(),
        "commitment_type": "one_time",
        "billing_interval": "",
        "renewal_amount": "22.00",
        "renewal_date": "2027-07-18",
        "cancellation_deadline": "2027-07-01",
        "contract_duration": "one purchase",
        "auto_renew": False,
        "refund_terms": "Refundable before registration only.",
        "cancellation_terms": "No recurring commitment authorized.",
        "premium_domain": False,
        "free_hosting_inadequate": False,
        "dns_required": False,
        "free_alternatives_considered": False,
        "necessary_to_launch": True,
        "optional_convenience": False,
        "checkout_target": {
            "merchant_id": "registrar.example",
            "product_kind": "domain_registration",
            "product_id": "example.com",
            "session_requirement": "authenticated" if args.authenticated_session else "anonymous",
        },
    }
    created = governance.create_purchase_proposal(db, proposal_payload)
    proposal_id = created["proposal_id"]
    governance.approve_and_reserve_purchase(
        db, proposal_id,
        {"approver": "cal", "approved_maximum": "22.00", "idempotency_key": "fake-e2e-approve"},
    )
    ticket = governance.issue_execution_ticket(
        db, proposal_id,
        {"audience": governance.PAYMENT_INSTRUMENT_ALIAS, "idempotency_key": "fake-e2e-ticket"},
    )
    ledger_before = governance.get_ledger_snapshot(db)
    require(ledger_before["completed_spend_cents"] == 0, "seed ledger has no spend")
    require(ledger_before["reserved_cents"] == 2200, "seed ledger reserves 22.00")

    # --- real bridge HTTP route (real handler, auth, validation, governance) -
    async def execute(packet, update=None, context=None):
        return cogitator_bridge.run_purchase_executor_bridge_packet(
            packet, db_path=db, enabled=enabled
        )

    handler = bridge_http.build_cogitator_bridge_http_request_handler(
        token_getter=lambda: bridge_token,
        max_chars_getter=lambda: 200_000,
        parse_packet=cogitator_bridge.parse_cogitator_bridge_packet,
        validate_packet=cogitator_bridge.validate_cogitator_bridge_packet,
        execute_bridge_request=execute,
    )
    bridge_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=bridge_server.serve_forever, daemon=True).start()
    bridge_url = f"http://127.0.0.1:{bridge_server.server_address[1]}"

    processor_server = None
    processor_origin = ""
    if args.hosted_frame:
        processor_server = ThreadingHTTPServer(("127.0.0.1", 0), MockProcessor)
        threading.Thread(target=processor_server.serve_forever, daemon=True).start()
        processor_origin = f"http://127.0.0.1:{processor_server.server_address[1]}"
        MockMerchant.checkout_html = HOSTED_PARENT_FIXTURE.read_text().replace(
            "{{PROCESSOR_ORIGIN}}", processor_origin
        ).encode()
    else:
        MockMerchant.checkout_html = SAME_PAGE_FIXTURE.read_bytes()

    MockMerchant.authenticated_session = args.authenticated_session
    merchant_server = ThreadingHTTPServer(("127.0.0.1", 0), MockMerchant)
    threading.Thread(target=merchant_server.serve_forever, daemon=True).start()
    checkout_url = f"http://127.0.0.1:{merchant_server.server_address[1]}"

    # --- synthetic credentials ----------------------------------------------
    # Either use externally-staged synthetic creds (systemd name-binding) or
    # generate them locally. Card values MUST be the public 4242 test set.
    expected = {
        "card_number": "4242424242424242",
        "card_expiry": "12/29",
        "card_cvv": "123",
        "card_name": "Fake Holder",
    }
    if args.credentials_dir:
        creds = Path(args.credentials_dir)
        for field in expected:
            require((creds / field).is_file(), f"staged synthetic credential missing: {field}")
        # The name-binding is what we assert; the card_name value may differ.
        require(
            (creds / "card_number").read_text().strip() == expected["card_number"],
            "staged card_number must be the public 4242 test PAN (no real card in staging)",
        )
    else:
        creds = tmp / "creds"
        creds.mkdir()
        for field, value in expected.items():
            path = creds / field
            path.write_text(value)
            path.chmod(0o600)

    procs_before = browser_proc_count()

    # --- run the executor (real local browser path) --------------------------
    env = dict(os.environ)
    env["CREDENTIALS_DIRECTORY"] = str(creds)
    env["COGITATOR_BRIDGE_TOKEN"] = bridge_token
    command = [
        sys.executable, "-m", "purchase_executor",
        "--bridge-url", bridge_url,
        "--fake-e2e", "--checkout-url", checkout_url,
        "--state-dir", str(tmp / "state"),
    ]
    if processor_origin:
        command.extend(["--fake-processor-origin", processor_origin])
    run = subprocess.run(
        command,
        input=ticket["ticket_token"], text=True, capture_output=True,
        cwd=REPO_ROOT, env=env, timeout=240,
    )
    require(
        not any(value in run.stdout + run.stderr for value in expected.values()),
        "executor output contains a payment value",
    )
    print(run.stdout, end="")
    print(run.stderr, end="", file=sys.stderr)
    require(
        run.returncode == 0,
        f"executor exit code {run.returncode} (want 0=completed); requests={MockMerchant.requests}",
    )

    # --- proofs --------------------------------------------------------------
    ledger_after = governance.get_ledger_snapshot(db)
    require(ledger_after["completed_spend_cents"] == 2200, "ledger completed exactly 22.00")
    require(ledger_after["reserved_cents"] == 0, "reservation converted, not still reserved")
    require(ledger_after["remaining_spendable_cents"]
            == ledger_before["original_budget_cents"] - 2200, "budget debited exactly once")
    final = governance.get_purchase_proposal(db, proposal_id)
    require(final["lifecycle_state"] == "completed", "proposal completed")
    registry = governance.get_asset_service_registry(db)
    assets = registry["assets"] if isinstance(registry, dict) else registry
    require(len(assets) == 1, "exactly one purchased asset registered")

    claim_packet = {
        "source_agent": "hermes",
        "requested_action": "claim_execution_ticket",
        "user_intent": "duplicate claim must be rejected",
        "context": {"ticket_token": ticket["ticket_token"]},
    }
    status, body = bridge_request(bridge_url + "/api/cogitator_bridge", bridge_token, claim_packet)
    require(status == 400 and body.get("reason_code") == "ticket_invalid",
            f"second claim rejected (got {status} {body})")
    status, _ = bridge_request(bridge_url + "/api/cogitator_bridge", None, claim_packet)
    require(status == 401, "unauthenticated bridge call rejected")

    deadline = time.monotonic() + 15
    while browser_proc_count() > procs_before and time.monotonic() < deadline:
        time.sleep(1)
    require(browser_proc_count() <= procs_before, "zero residual browser processes")

    audit = (tmp / "state" / "audit.jsonl").read_text()
    require("4242424242424242" not in audit, "audit log contains no card number")
    require('"phase": "discovered"' in audit, "semantic discovery audited")
    require('"phase": "cleaned_up"' in audit, "browser cleanup audited")
    require("hermes_mock_session" not in audit, "session cookie absent from audit")
    if args.hosted_frame:
        require(processor_origin in audit, "hosted processor origin audited")
    else:
        require('"frame_origins": []' in audit, "same-page discovery has no hosted frames")

    bridge_server.shutdown()
    merchant_server.shutdown()
    if processor_server:
        processor_server.shutdown()
    print(json.dumps({
        "fake_e2e": "PASS",
        "checkout_shape": "hosted_frame" if args.hosted_frame else "same_page",
        "session_shape": "authenticated" if args.authenticated_session else "anonymous",
        "proposal_id": proposal_id,
        "ticket_id": ticket["ticket_id"],
        "ledger_before": ledger_before, "ledger_after": ledger_after,
        "state_dir": str(tmp),
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
