"""Unit tests for the Restricted Purchase Executor V0 (issue #65).

Everything runs against injected fake bridge/browser seams — no network, no
real browser, no credentials, no model.
"""

import io
import json
import os
import signal
import stat
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import purchase_executor as pe
import purchase_merchants

# Tests exercise the real Porkbun adapter (production mode) against a fake
# browser: the claimed domain is porkbun.com so adapter_for() resolves, and the
# fake fill_fields records the ordered (selector, value) pairs without a DOM.
MERCHANT = "porkbun.com"

CLAIM = {
    "status": "ok",
    "requested_action": "claim_execution_ticket",
    "ticket_id": "pt_test",
    "proposal_id": "pp_1",
    "state": "claimed",
    "audience": "virgil_website_pilot",
    "canonical_merchant_domain": MERCHANT,
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
    "url": f"https://{MERCHANT}/checkout",
    "text": CHECKOUT_TEXT,
    "merchant": "Fake Registrar",
    "has_form": True,
    "form_action": f"https://{MERCHANT}/pay",
}
CONFIRM_PAGE = {
    "url": f"https://{MERCHANT}/pay",
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
        self._filled = False

    def navigate(self, url, task_id):
        self.calls.append(("navigate", url))
        return {"success": self.nav_success}

    def fill_fields(self, task_id, ordered_fields):
        if self.on_eval:
            self.on_eval(self)
        self.calls.append(("fill", ordered_fields))
        if self.fill_ok:
            self._filled = True
        return {"success": True, "result": {"ok": self.fill_ok}}

    def eval_js(self, expression, task_id):
        if self.on_eval:
            self.on_eval(self)
        if ".click()" in expression:  # submit_expression clicks the submit control
            self.calls.append(("submit", ""))
            if self.submit_ok:
                self.index = 1
            return {"success": True, "result": {"ok": self.submit_ok}}
        self.calls.append(("probe", ""))
        page = dict(self.pages[self.index])
        page["filled"] = self._filled
        return {"success": True, "result": page}

    def cleanup(self, task_id):
        self.cleaned.append(task_id)


class FakeBridge:
    def __init__(self, claim=None, claim_status=200, terminal_transport_fails=False,
                 terminal_status=200, terminal_body=None):
        self.claim = dict(CLAIM if claim is None else claim)
        self.claim_status = claim_status
        self.terminal_transport_fails = terminal_transport_fails
        self.terminal_status = terminal_status
        self.terminal_body = terminal_body
        self.calls = []

    def __call__(self, action, context, user_intent):
        self.calls.append((action, context))
        if action == "claim_execution_ticket":
            return self.claim_status, self.claim
        if self.terminal_transport_fails:
            raise pe.BridgeTransportError("bridge unreachable")
        return self.terminal_status, self.terminal_body if self.terminal_body is not None else {
            "status": "ok" if self.terminal_status == 200 else "error",
            "requested_action": action,
        }


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
        checkout_url_for=lambda claim: "https://porkbun.com/checkout",
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
    assert completion["merchant_domain"] == MERCHANT
    # Genuine input: fill went through fill_fields with Porkbun's selectors,
    # in card-field order, carrying the credential values (never el.value).
    fill_calls = [payload for kind, payload in browser.calls if kind == "fill"]
    assert len(fill_calls) == 1
    assert fill_calls[0] == [
        (purchase_merchants.PORKBUN.selectors[f],
         {"card_number": "4242424242424242", "card_expiry": "12/29",
          "card_cvv": "123", "card_name": "Fake Holder"}[f])
        for f in purchase_merchants.CARD_FIELDS
    ]
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
    assert "quantity_missing" in pe.check_terms(
        CHECKOUT_TEXT.replace("Quantity: 1\n", ""), CLAIM)
    assert "unexpected_recurrence" in pe.check_terms(
        CHECKOUT_TEXT + "\nauto-renews yearly", CLAIM)
    monthly = {**CLAIM, "recurrence_authorization": dict(
        CLAIM["recurrence_authorization"], commitment_type="subscription",
        billing_interval="monthly", auto_renew=True)}
    assert "recurrence_not_shown" in pe.check_terms(CHECKOUT_TEXT, monthly)
    recurring_text = (
        CHECKOUT_TEXT + "\nsubscription\nBilling interval: monthly\nRenewal amount: 22.00\n"
        "Renewal date: 2027-07-18\nCancellation deadline: 2027-07-01\n"
        "Contract duration: one purchase\nCancellation terms: "
        "No recurring commitment authorized.\nAuto-renew"
    )
    assert "billing_interval_mismatch" in pe.check_terms(
        recurring_text.replace("monthly", "yearly"), monthly
    )
    assert "renewal_amount_mismatch" in pe.check_terms(
        recurring_text.replace("Renewal amount: 22.00", "Renewal amount: 50.00"), monthly
    )


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


def test_submit_error_is_uncertain_and_not_retried(tmp_path, creds):
    browser, bridge = FakeBrowser(submit_ok=False), FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_UNCERTAIN
    assert [kind for kind, _ in browser.calls].count("submit") == 1
    assert terminal_calls(bridge)[0][1]["failure_category"] == "submit_outcome_unknown"


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


def test_callback_http_failure_spools_once(tmp_path, creds):
    browser, bridge = FakeBrowser(), FakeBridge(terminal_status=500)
    assert run(browser, bridge, tmp_path) == pe.EXIT_SPOOLED
    assert len(terminal_calls(bridge)) == 1
    assert (tmp_path / "state" / "spool" / "pt_test.json").is_file()


def test_malformed_callback_body_spools_once(tmp_path, creds):
    browser, bridge = FakeBrowser(), FakeBridge(terminal_body=[])
    assert run(browser, bridge, tmp_path) == pe.EXIT_SPOOLED
    assert len(terminal_calls(bridge)) == 1
    assert (tmp_path / "state" / "spool" / "pt_test.json").is_file()


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
    # No JavaScript value assignment anywhere: production fill uses genuine
    # browser input (agent-browser fill via batch), never `element.value = ...`.
    assert ".value" not in pe.PAGE_PROBE_JS
    assert ".value" not in pe.submit_expression("button[type='submit']", "porkbun.com", False)
    assert ".value =" not in source and ".value=" not in source
    assert "el.value" not in source


def test_audit_log_is_redacted_and_private(tmp_path, creds):
    confirm = dict(
        CONFIRM_PAGE,
        text=CONFIRM_PAGE["text"] + "\ncard 4242 4242 4242 4242\nFake Holder",
    )
    browser, bridge = FakeBrowser(pages=[CHECKOUT_PAGE, confirm]), FakeBridge()
    run(browser, bridge, tmp_path)
    audit_file = tmp_path / "state" / "audit.jsonl"
    content = audit_file.read_text()
    assert "4242424242424242" not in content and "4242 4242" not in content
    assert stat.S_IMODE(audit_file.stat().st_mode) == 0o600
    receipt = (tmp_path / "state" / "restricted" / "purchase_receipts" / "pt_test.txt").read_text()
    assert "4242 4242" not in receipt and "Fake Holder" not in receipt
    assert "[REDACTED_PAYMENT_VALUE]" in receipt


def test_payment_values_are_redacted_from_callback_and_spool(tmp_path, creds):
    cardholder = 'José "Tester" \\ QA'
    (creds / "card_name").write_text(cardholder)
    checkout = dict(CHECKOUT_PAGE, merchant=cardholder)
    browser = FakeBrowser(pages=[checkout, CONFIRM_PAGE])
    bridge = FakeBridge(terminal_transport_fails=True)
    assert run(browser, bridge, tmp_path) == pe.EXIT_SPOOLED
    callback = json.dumps(terminal_calls(bridge)[0][1])
    spool = (tmp_path / "state" / "spool" / "pt_test.json").read_text()
    for value in ("4242424242424242", "12/29", "123", cardholder):
        assert value not in callback
        assert value not in spool


def test_navigation_failure_no_retry(tmp_path, creds):
    browser, bridge = FakeBrowser(nav_success=False), FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    assert [kind for kind, _ in browser.calls] == ["navigate"]
    assert terminal_calls(bridge)[0][1]["failure_category"] == "navigation_failed"
    assert browser.cleaned == ["purchase_pt_test"]


def test_unhandled_exception_reports_once_and_cleans_up(tmp_path, creds):
    browser, bridge = FakeBrowser(), FakeBridge()

    def explode(url, task_id):
        raise RuntimeError("synthetic card_name must not be logged")

    browser.navigate = explode
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    assert terminal_calls(bridge)[0][0] == "record_definitive_failure"
    assert terminal_calls(bridge)[0][1]["failure_category"] == "executor_error_before_submit"
    assert browser.cleaned == ["purchase_pt_test"]
    assert "synthetic card_name" not in (tmp_path / "state" / "audit.jsonl").read_text()


def test_form_action_and_post_fill_origin_are_rejected(tmp_path, creds):
    external_form = dict(CHECKOUT_PAGE, form_action="https://evil.example/pay")
    browser, bridge = FakeBrowser(pages=[external_form, CONFIRM_PAGE]), FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    assert "fill" not in [kind for kind, _ in browser.calls]

    # A redirect to a different registrable domain that happens during fill is
    # caught by the post-fill origin re-check before any submit.
    redirected = dict(CHECKOUT_PAGE, url="https://evil.example/pay")
    browser = FakeBrowser(pages=[CHECKOUT_PAGE, CONFIRM_PAGE])
    bridge = FakeBridge()
    original_fill = browser.fill_fields

    def redirect_after_fill(task_id, ordered_fields):
        result = original_fill(task_id, ordered_fields)
        browser.pages[0] = redirected  # page navigates away post-fill
        return result

    browser.fill_fields = redirect_after_fill
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    assert "submit" not in [kind for kind, _ in browser.calls]


def test_prefilled_marker_cannot_bypass_failed_fill(tmp_path, creds):
    class PrefilledBrowser(FakeBrowser):
        def eval_js(self, expression, task_id):
            result = super().eval_js(expression, task_id)
            if "document.body" in expression:
                result["result"]["filled"] = True
            return result

    browser, bridge = PrefilledBrowser(fill_ok=False), FakeBridge()
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    assert not any(kind in {"fill", "submit"} for kind, _ in browser.calls)
    assert terminal_calls(bridge)[0][1]["failure_category"] == "invalid_checkout_state"


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


def test_sigterm_during_claim_audit_cleans_up_and_reports(tmp_path, creds):
    browser, bridge = FakeBrowser(), FakeBridge()
    state = tmp_path / "state"
    state.mkdir()
    write_audit = pe.audit_factory(state)

    def audit(phase, **fields):
        if phase == "claimed":
            os.kill(os.getpid(), signal.SIGTERM)
        write_audit(phase, **fields)

    result = pe.run_once(
        bridge_post=bridge,
        browser=browser,
        audit=audit,
        state_dir=state,
        checkout_url_for=lambda claim: "https://porkbun.com/checkout",
        fake_e2e=False,
        stdin=io.StringIO("tok\n"),
    )
    assert result == pe.EXIT_DEFINITIVE_FAILURE
    assert terminal_calls(bridge)[0][1]["failure_category"] == "terminated_before_submit"
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


def test_preflight_rejects_camofox_lightpanda_and_browser_args(monkeypatch):
    from tools import browser_tool as bt

    monkeypatch.setattr(bt, "_is_local_mode", lambda: True)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: True)
    monkeypatch.setattr(bt, "_get_browser_engine", lambda: "lightpanda")
    monkeypatch.setattr(bt, "_get_sandbox_bypass_mode", lambda: "never")
    monkeypatch.setenv("AGENT_BROWSER_ARGS", "--no-sandbox")
    problems = pe.preflight_browser_config()
    assert any("Camofox" in problem for problem in problems)
    assert any("Chromium" in problem for problem in problems)
    assert any("AGENT_BROWSER_ARGS" in problem for problem in problems)


def test_sensitive_browser_eval_uses_stdin_and_scrubs_env(monkeypatch):
    from tools import browser_tool as bt
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    seen = {}
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "get", lambda task_id: None)

    def fake_run(task_id, command, args, **kwargs):
        seen.update(task_id=task_id, command=command, args=args, kwargs=kwargs)
        return {"success": True, "data": {"result": '{"ok": true}'}}

    monkeypatch.setattr(bt, "_run_browser_command", fake_run)
    result = json.loads(
        bt._browser_eval(
            "payment-marker", "purchase_x", stdin=True, suppress_output=True
        )
    )
    assert result["result"] == {"ok": True}
    assert seen["args"] == ["--stdin"]
    assert seen["kwargs"]["_stdin_text"] == "payment-marker"
    assert seen["kwargs"]["_suppress_output"] is True
    assert "payment-marker" not in seen["args"]

    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/test")
    monkeypatch.setenv("COGITATOR_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "model-secret")

    def fake_navigate(url, task_id=None):
        assert "CREDENTIALS_DIRECTORY" not in os.environ
        assert "COGITATOR_BRIDGE_TOKEN" not in os.environ
        assert "OPENAI_API_KEY" not in os.environ
        return json.dumps({"success": True})

    monkeypatch.setattr(bt, "browser_navigate", fake_navigate)
    pe.real_browser().navigate("https://registrar.example", "purchase_x")
    assert os.environ["COGITATOR_BRIDGE_TOKEN"] == "bridge-secret"


def test_sensitive_browser_output_is_discarded(tmp_path, monkeypatch, caplog):
    from tools import browser_tool as bt

    marker = "4111111111111111 Fake Holder 123 12/29"
    seen = {}

    class FakeProcess:
        returncode = 1

        def communicate(self, input=None, timeout=None):
            assert input == marker.encode()
            return marker.encode(), marker.encode()

        def kill(self):
            pass

    def fake_popen(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return FakeProcess()

    monkeypatch.setattr(bt, "_find_agent_browser", lambda: "/bin/agent-browser")
    monkeypatch.setattr(bt, "_chromium_installed", lambda: True)
    monkeypatch.setattr(bt, "_is_local_mode", lambda: True)
    monkeypatch.setattr(bt, "_get_session_info", lambda task_id: {"session_name": "h_test"})
    monkeypatch.setattr(bt, "_socket_safe_tmpdir", lambda: str(tmp_path))
    monkeypatch.setattr(bt, "_write_owner_pid", lambda *args: None)
    monkeypatch.setattr(bt.subprocess, "Popen", fake_popen)
    result = bt._run_browser_command(
        "purchase_x", "eval", ["--stdin"], timeout=1,
        _engine_override="auto", _stdin_text=marker, _suppress_output=True,
    )
    assert result == {"success": False, "error": "Sensitive browser evaluation failed"}
    assert marker not in " ".join(seen["args"])
    assert hasattr(seen["kwargs"]["stdout"], "write")
    assert seen["kwargs"]["stderr"] == subprocess.DEVNULL
    assert marker not in caplog.text


def test_sensitive_browser_result_is_strictly_whitelisted(tmp_path, monkeypatch, caplog):
    from tools import browser_tool as bt

    marker = "4111111111111111 Fake Holder 123 12/29"

    class FakeProcess:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

        def communicate(self, input=None, timeout=None):
            payload = {
                "success": True,
                "data": {"result": json.dumps({"ok": False, "missing": marker})},
            }
            self.stdout.write(json.dumps(payload).encode())

        def kill(self):
            pass

    def fake_popen(args, **kwargs):
        return FakeProcess(kwargs["stdout"])

    monkeypatch.setattr(bt, "_find_agent_browser", lambda: "/bin/agent-browser")
    monkeypatch.setattr(bt, "_chromium_installed", lambda: True)
    monkeypatch.setattr(bt, "_is_local_mode", lambda: True)
    monkeypatch.setattr(bt, "_get_session_info", lambda task_id: {"session_name": "h_test"})
    monkeypatch.setattr(bt, "_socket_safe_tmpdir", lambda: str(tmp_path))
    monkeypatch.setattr(bt, "_write_owner_pid", lambda *args: None)
    monkeypatch.setattr(bt.subprocess, "Popen", fake_popen)
    result = bt._run_browser_command(
        "purchase_x", "eval", ["--stdin"], timeout=1,
        _engine_override="auto", _stdin_text=marker, _suppress_output=True,
    )
    assert result == {"success": True, "data": {"result": '{"ok": false}'}}
    assert marker not in json.dumps(result)
    assert marker not in caplog.text


def test_fake_e2e_flag_is_loopback_only():
    assert pe.origin_allowed("http://127.0.0.1:8000/x", "registrar.example", fake_e2e=True)
    assert not pe.origin_allowed("https://registrar.example/x", "registrar.example", fake_e2e=True)
    assert pe.origin_allowed("https://registrar.example/x", "registrar.example", fake_e2e=False)
    assert not pe.origin_allowed(
        "https://www.registrar.example/x", "registrar.example", fake_e2e=False
    )
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


def test_unsafe_checkout_url_is_rejected_before_navigation(tmp_path, creds):
    browser, bridge = FakeBrowser(), FakeBridge()
    result = pe.run_once(
        bridge_post=bridge,
        browser=browser,
        audit=pe.audit_factory(tmp_path),
        state_dir=tmp_path,
        checkout_url_for=lambda claim: "https://registrar.example@evil.example/checkout",
        fake_e2e=False,
        stdin=io.StringIO("tok\n"),
    )
    assert result == pe.EXIT_DEFINITIVE_FAILURE
    assert browser.calls == []
    assert terminal_calls(bridge)[0][1]["failure_category"] == "wrong_origin"


def test_fake_e2e_proxy_blocks_non_loopback():
    with pe.loopback_browser_proxy() as port:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"})
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open("http://example.com/blocked", timeout=2)
    assert error.value.code == 403


def test_gateway_never_imports_executor():
    gateway_dir = Path(pe.__file__).parent / "gateway"
    for path in gateway_dir.rglob("*.py"):
        assert "purchase_executor" not in path.read_text()


def test_private_write_repairs_existing_mode(tmp_path):
    path = tmp_path / "spool.json"
    path.write_text("old")
    path.chmod(0o644)
    pe._write_private(path, "new")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- merchant adapter boundary (Phase 1) -------------------------------------


def test_unknown_merchant_fails_closed_before_credentials(tmp_path, creds, monkeypatch):
    def explode():
        raise AssertionError("credentials must not be read for an unsupported merchant")

    monkeypatch.setattr(pe, "load_payment_fields", explode)
    browser = FakeBrowser()
    bridge = FakeBridge(claim=dict(CLAIM, canonical_merchant_domain="namecheap.com"))
    assert run(browser, bridge, tmp_path) == pe.EXIT_DEFINITIVE_FAILURE
    assert terminal_calls(bridge)[0][1]["failure_category"] == "merchant_not_supported"
    # Rejected before navigation, fill, or submit.
    assert browser.calls == []
    assert browser.cleaned == ["purchase_pt_test"]


def test_adapter_allowlist_is_porkbun_only():
    assert purchase_merchants.adapter_for("porkbun.com", fake_e2e=False) is purchase_merchants.PORKBUN
    for bad in ("namecheap.com", "porkbun.com.evil.test", "evil-porkbun.com", ""):
        assert purchase_merchants.adapter_for(bad, fake_e2e=False) is None
    assert purchase_merchants.adapter_for("anything", fake_e2e=True) is purchase_merchants.MOCK


def test_porkbun_selectors_resolve_against_sanitized_fixture():
    from html.parser import HTMLParser

    fixture = Path(pe.__file__).parent / "tests" / "fixtures" / "porkbun_checkout_v0.html"
    html = fixture.read_text()

    class Collector(HTMLParser):
        def __init__(self):
            super().__init__()
            self.input_names = set()
            self.has_submit = False

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "input" and a.get("name"):
                self.input_names.add(a["name"])
            if tag == "button" and a.get("type") == "submit":
                self.has_submit = True

    collector = Collector()
    collector.feed(html)
    for field in purchase_merchants.CARD_FIELDS:
        selector = purchase_merchants.PORKBUN.selectors[field]
        name = selector.split("'")[1]  # input[name='X'] -> X
        assert name in collector.input_names, f"{field} selector {selector} not in fixture"
    assert collector.has_submit


def test_sanitized_fixture_has_no_secrets():
    import re as _re
    fixture = Path(pe.__file__).parent / "tests" / "fixtures" / "porkbun_checkout_v0.html"
    # Strip HTML comments first: the provenance note legitimately says the words
    # "cookies/tokens/session" while attesting their absence from the DOM.
    text = _re.sub(r"<!--.*?-->", "", fixture.read_text(), flags=_re.DOTALL).lower()
    for forbidden in ("cookie", "token", "password", "session", "authorization", "api_key"):
        assert forbidden not in text
    # No value-bearing inputs and no card-shaped digit runs.
    assert 'value="' not in text
    assert not _re.search(r"\b(?:\d[ -]?){13,19}\b", text)


def test_genuine_input_uses_batch_over_stdin_not_argv_or_value_assignment():
    source = Path(pe.__file__).read_text()
    # Fill is delivered as a batch whose commands (incl. the value) ride stdin.
    assert '"batch"' in source
    assert "_stdin_text=json.dumps(commands)" in source
    # And never element.value assignment.
    assert "el.value" not in source and ".value =" not in source


def test_staging_harness_is_fail_loud():
    import importlib.util

    path = Path(pe.__file__).parent / "scripts" / "purchase_executor_fake_e2e.py"
    spec = importlib.util.spec_from_file_location("pe_fake_e2e", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Any failed invariant aborts non-zero; there is no silent skip / false green.
    module.require(True, "ok path")
    with pytest.raises(SystemExit):
        module.require(False, "expected ledger mutation did not occur")
