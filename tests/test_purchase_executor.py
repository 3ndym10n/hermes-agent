"""Unit tests for the Restricted Purchase Executor V0 (issue #65).

Everything runs against injected fake bridge/browser seams — no network, no
real browser, no credentials, no model.
"""

import io
import json
import os
import signal
import stat
from pathlib import Path

import pytest

import purchase_executor as pe


CLAIM = {
    "status": "ok",
    "requested_action": "claim_execution_ticket",
    "ticket_id": "pt_test",
    "proposal_id": "pp_1",
    "state": "claimed",
    "audience": "virgil_website_pilot",
    "canonical_merchant_domain": "registrar.example",
    "approved_item": "example.com domain registration",
    "quantity": 1,
    "maximum_total": "22.00",
    "currency": "AUD",
    "recurrence_authorization": {
        "commitment_type": "one_time",
        "billing_interval": "",
        "renewal_amount": "22.00",
        "renewal_date": "2027-07-18",
        "cancellation_deadline": "2027-07-01",
        "contract_duration": "one purchase",
        "cancellation_terms": "No recurring commitment authorized.",
        "auto_renew": False,
    },
}

CHECKOUT_TEXT = (
    "Fake Registrar\nexample.com domain registration\nQuantity: 1\n"
    "Total: 22.00 AUD\nPay now"
)
CHECKOUT_PAGE = {
    "url": "https://registrar.example/checkout",
    "text": CHECKOUT_TEXT,
    "merchant": "Fake Registrar",
    "has_form": True,
}
CONFIRM_PAGE = {
    "url": "https://registrar.example/pay",
    "text": "Order confirmed. Reference FAKE-123. Thank you for your purchase.",
    "merchant": "Fake Registrar",
    "has_form": False,
}


class FakeBrowser:
    def __init__(self, pages=None, nav_success=True, fill_ok=True, submit_ok=True,
                 on_eval=None):
        self.pages = pages or [CHECKOUT_PAGE, CONFIRM_PAGE]
        self.nav_success = nav_success
        self.fill_ok = fill_ok
        self.submit_ok = submit_ok
        self.on_eval = on_eval
        self.calls = []
        self.cleaned = []
        self.index = 0

    def navigate(self, url, task_id):
        self.calls.append(("navigate", url))
        return {"success": self.nav_success}

    def eval_js(self, expression, task_id):
        if self.on_eval:
            self.on_eval(self)
        if "Object.entries" in expression:
            self.calls.append(("fill", expression))
            return {"success": True, "result": {"ok": self.fill_ok}}
        if "f.submit()" in expression:
            self.calls.append(("submit", ""))
            if self.submit_ok:
                self.index = 1
            return {"success": True, "result": {"ok": self.submit_ok}}
        self.calls.append(("probe", ""))
        return {"success": True, "result": dict(self.pages[self.index])}

    def cleanup(self, task_id):
        self.cleaned.append(task_id)


class FakeBridge:
    def __init__(self, claim=None, claim_status=200, terminal_transport_fails=False):
        self.claim = dict(CLAIM if claim is None else claim)
        self.claim_status = claim_status
        self.terminal_transport_fails = terminal_transport_fails
        self.calls = []

    def __call__(self, action, context, user_intent):
        self.calls.append((action, context))
        if action == "claim_execution_ticket":
            return self.claim_status, self.claim
        if self.terminal_transport_fails:
            raise pe.BridgeTransportError("bridge unreachable")
        return 200, {"status": "ok", "requested_action": action}


@pytest.fixture()
def creds(tmp_path, monkeypatch):
    directory = tmp_path / "creds"
    directory.mkdir()
    for field, value in (
        ("card_number", "4242424242424242"),
        ("card_expiry", "12/29"),
        ("card_cvv", "123"),
        ("card_name", "Fake Holder"),
    ):
        (directory / field).write_text(value)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))
    return directory


def run(browser, bridge, tmp_path, *, fake_e2e=False, token="tok\n"):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return pe.run_once(
        bridge_post=bridge,
        browser=browser,
        audit=pe.audit_factory(state),
        state_dir=state,
        checkout_url_for=lambda claim: "https://registrar.example/checkout",
        fake_e2e=fake_e2e,
        stdin=io.StringIO(token),
        post_submit_wait=0.2,
        sleep=lambda seconds: None,
    )


def terminal_calls(bridge):
    return [call for call in bridge.calls if call[0] != "claim_execution_ticket"]


def test_happy_path_completes_once(tmp_path, creds):
    browser, bridge = FakeBrowser(), FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_COMPLETED
    actions = [call[0] for call in bridge.calls]
    assert actions == ["claim_execution_ticket", "record_completed_purchase"]
    completion = bridge.calls[1][1]
    assert completion["merchant_display_name"] == "Fake Registrar"
    assert completion["final_amount"] == "22.00"
    assert completion["merchant_domain"] == "registrar.example"
    receipt_path = tmp_path / "state" / completion["receipt"]["restricted_artifact_path"]
    assert receipt_path.is_file()
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert browser.cleaned == ["purchase_pt_test"]


def test_claim_rejected_touches_nothing(tmp_path, creds):
    browser = FakeBrowser()
    bridge = FakeBridge(claim={"status": "rejected", "reason_code": "ticket_invalid"},
                        claim_status=400)
    assert run(browser, bridge, tmp_path) == pe.EXIT_CLAIM_FAILED
    assert browser.calls == []
    assert browser.cleaned == []
    assert terminal_calls(bridge) == []


def test_wrong_origin_hard_stop_before_fill(tmp_path, creds):
    page = dict(CHECKOUT_PAGE, url="http://registrar.example/checkout")
    browser, bridge = FakeBrowser(pages=[page, CONFIRM_PAGE]), FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    assert terminal_calls(bridge) == [
        ("record_definitive_failure", pe.failure_context(CLAIM, "wrong_origin"))
    ]
    kinds = [kind for kind, _ in browser.calls]
    assert "fill" not in kinds and "submit" not in kinds
    assert browser.cleaned == ["purchase_pt_test"]


def test_price_mismatch_aborts_without_submit(tmp_path, creds):
    page = dict(CHECKOUT_PAGE, text=CHECKOUT_TEXT.replace("22.00", "29.00"))
    browser, bridge = FakeBrowser(pages=[page, CONFIRM_PAGE]), FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    assert terminal_calls(bridge)[0][1]["failure_category"] == "terms_changed"
    assert "submit" not in [kind for kind, _ in browser.calls]


def test_check_terms_currency_item_quantity_recurrence():
    assert pe.check_terms(CHECKOUT_TEXT, CLAIM) == []
    assert "currency_missing" in pe.check_terms(CHECKOUT_TEXT.replace("AUD", "USD"), CLAIM)
    assert "item_missing" in pe.check_terms(
        CHECKOUT_TEXT.replace("example.com domain registration", "other thing"), CLAIM)
    assert "quantity_mismatch" in pe.check_terms(
        CHECKOUT_TEXT.replace("Quantity: 1", "Quantity: 3"), CLAIM)
    assert "unexpected_recurrence" in pe.check_terms(
        CHECKOUT_TEXT + "\nauto-renews yearly", CLAIM)
    monthly = {**CLAIM, "recurrence_authorization": dict(
        CLAIM["recurrence_authorization"], commitment_type="subscription")}
    assert "recurrence_not_shown" in pe.check_terms(CHECKOUT_TEXT, monthly)


def test_captcha_before_submit_is_definitive_failure(tmp_path, creds):
    page = dict(CHECKOUT_PAGE, text=CHECKOUT_TEXT + "\nPlease solve this CAPTCHA")
    browser, bridge = FakeBrowser(pages=[page, CONFIRM_PAGE]), FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    assert terminal_calls(bridge)[0][1]["failure_category"] == "human_challenge_required"
    assert "submit" not in [kind for kind, _ in browser.calls]


def test_3ds_after_submit_is_uncertain(tmp_path, creds):
    after = dict(CONFIRM_PAGE, text="3-D Secure: verify your card to continue")
    browser, bridge = FakeBrowser(pages=[CHECKOUT_PAGE, after]), FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_UNCERTAIN
    action, context = terminal_calls(bridge)[0]
    assert action == "record_uncertain_result"
    assert context["failure_category"] == "post_submit_challenge"
    assert [kind for kind, _ in browser.calls].count("submit") == 1


def test_callback_transport_failure_spools_once(tmp_path, creds):
    browser = FakeBrowser()
    bridge = FakeBridge(terminal_transport_fails=True)
    assert run(browser, bridge, tmp_path) == pe.EXIT_SPOOLED
    assert len(terminal_calls(bridge)) == 1  # no callback retry
    spool = tmp_path / "state" / "spool" / "pt_test.json"
    assert spool.is_file()
    assert stat.S_IMODE(spool.stat().st_mode) == 0o600
    record = json.loads(spool.read_text())
    assert record["requested_action"] == "record_completed_purchase"
    assert "4242424242424242" not in spool.read_text()
    assert browser.cleaned == ["purchase_pt_test"]


def test_redaction_filter():
    assert "4242" not in pe.redact("card 4242 4242 4242 4242 used")
    assert "[REDACTED_PAYMENT_VALUE]" in pe.redact("cvv: 123")
    assert "[REDACTED_PAYMENT_VALUE]" in pe.redact("expires 12/29")
    assert "[REDACTED_AUTH_VALUE]" in pe.redact("verification code 123456")
    # Non-Luhn digit runs (order refs, phone numbers) survive.
    assert "1234567890123" in pe.redact("order 1234567890123")


def test_no_snapshots_no_model_no_credstore():
    source = Path(pe.__file__).read_text()
    for forbidden in ("browser_snapshot", "browser_vision", "browser_get_images",
                      "/etc/credstore"):
        assert forbidden not in source
    # Page probes never read input values; .value appears only as fill assignment.
    assert ".value" not in pe.PAGE_PROBE_JS
    assert ".value" not in pe.SUBMIT_JS


def test_audit_log_is_redacted_and_private(tmp_path, creds):
    confirm = dict(CONFIRM_PAGE, text=CONFIRM_PAGE["text"] + "\ncard 4242 4242 4242 4242")
    browser, bridge = FakeBrowser(pages=[CHECKOUT_PAGE, confirm]), FakeBridge()
    run(browser, bridge, tmp_path)
    audit_file = tmp_path / "state" / "audit.jsonl"
    content = audit_file.read_text()
    assert "4242424242424242" not in content and "4242 4242" not in content
    assert stat.S_IMODE(audit_file.stat().st_mode) == 0o600
    receipt = (tmp_path / "state" / "restricted" / "purchase_receipts" / "pt_test.txt").read_text()
    assert "4242 4242" not in receipt and "[REDACTED_PAYMENT_VALUE]" in receipt


def test_navigation_failure_no_retry(tmp_path, creds):
    browser, bridge = FakeBrowser(nav_success=False), FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    assert [kind for kind, _ in browser.calls] == ["navigate"]
    assert terminal_calls(bridge)[0][1]["failure_category"] == "navigation_failed"
    assert browser.cleaned == ["purchase_pt_test"]


def test_sigterm_before_submit_cleans_up_and_reports(tmp_path, creds):
    def send_sigterm(fake):
        if not any(kind == "probe" for kind, _ in fake.calls):
            os.kill(os.getpid(), signal.SIGTERM)

    browser = FakeBrowser(on_eval=send_sigterm)
    bridge = FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    action, context = terminal_calls(bridge)[0]
    assert action == "record_definitive_failure"
    assert context["failure_category"] == "terminated_before_submit"
    assert browser.cleaned == ["purchase_pt_test"]


def test_missing_credentials_directory_stops_before_fill(tmp_path, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    browser, bridge = FakeBrowser(), FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    assert terminal_calls(bridge)[0][1]["failure_category"] == "credentials_unavailable"
    assert "fill" not in [kind for kind, _ in browser.calls]


def test_preflight_rejects_cloud_and_bypass_and_recording(monkeypatch):
    from tools import browser_tool as bt

    monkeypatch.setattr(bt, "_is_local_mode", lambda: False)
    monkeypatch.setattr(bt, "_get_sandbox_bypass_mode", lambda: "auto")
    problems = pe.preflight_browser_config()
    assert any("locally" in problem for problem in problems)
    assert any("sandbox_bypass" in problem for problem in problems)


def test_fake_e2e_flag_is_loopback_only():
    assert pe.origin_allowed("http://127.0.0.1:8000/x", "registrar.example", fake_e2e=True)
    assert not pe.origin_allowed("https://registrar.example/x", "registrar.example", fake_e2e=True)
    assert pe.origin_allowed("https://registrar.example/x", "registrar.example", fake_e2e=False)
    assert pe.origin_allowed("https://www.registrar.example/x", "registrar.example", fake_e2e=False)
    assert not pe.origin_allowed("https://evilregistrar.example/x", "registrar.example", fake_e2e=False)
    with pytest.raises(SystemExit):
        pe.parse_args(["--bridge-url", "http://127.0.0.1:1", "--fake-e2e",
                       "--checkout-url", "https://real.example/checkout"])
    with pytest.raises(SystemExit):
        pe.parse_args(["--bridge-url", "https://cogitator.example", "--fake-e2e",
                       "--checkout-url", "http://127.0.0.1:9/checkout"])
    with pytest.raises(SystemExit):
        pe.parse_args(["--bridge-url", "http://127.0.0.1:1",
                       "--checkout-url", "http://127.0.0.1:9/checkout"])


def test_gateway_never_imports_executor():
    gateway_dir = Path(pe.__file__).parent / "gateway"
    for path in gateway_dir.rglob("*.py"):
        assert "purchase_executor" not in path.read_text()
