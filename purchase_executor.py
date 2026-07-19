"""Restricted Purchase Executor V0 — one-shot checkout runner (issue #65).

Runs OUTSIDE hermes-gateway as its own short-lived process. Deterministic
scripted code only — no model call anywhere. Per run it:

  1. preflights the local browser config (local backend, sandbox enabled,
     recording off) BEFORE claiming anything,
  2. claims exactly ONE Cogitator execution ticket over the authenticated
     bridge (ticket token from stdin or ``$CREDENTIALS_DIRECTORY``, never
     argv/env),
  3. drives one isolated local browser task (``purchase_<ticket_id>``),
  4. revalidates price/currency/item/quantity/recurrence before submitting,
  5. submits at most once,
  6. reports exactly one terminal result — completed / definitive failure /
     uncertain — through the bridge, spooling locally (0600) if the callback
     transport fails,
  7. always calls ``cleanup_browser(task_id)`` — success, failure, exception,
     and SIGTERM.

Payment fields are read from ``$CREDENTIALS_DIRECTORY`` only at form-fill
time and never enter logs, audit records, spool files, receipts, or bridge
payloads. There is no retry loop anywhere: not on claim, navigation, submit,
or callbacks.

Fake-E2E mode (``--fake-e2e``) confines EVERY outbound URL (bridge and
checkout) to loopback so the flag can never be aimed at a real merchant.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import select
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

BRIDGE_PATH = "/api/cogitator_bridge"
BRIDGE_TOKEN_ENV = "COGITATOR_BRIDGE_TOKEN"
CREDENTIAL_FIELDS = ("card_number", "card_expiry", "card_cvv", "card_name")
RECEIPT_PREFIX = "restricted/purchase_receipts"
DEFAULT_STATE_DIR = "~/.hermes/purchase_executor"
POST_SUBMIT_WAIT_SECONDS = 20
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

EXIT_COMPLETED = 0
EXIT_PREFLIGHT = 2
EXIT_CLAIM_FAILED = 3
EXIT_DEFINITIVE_FAILURE = 4
EXIT_UNCERTAIN = 5
EXIT_SPOOLED = 6

# --- redaction (mirrors Cogitator governance _redact_sensitive_text) --------

_PAN_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_EXPIRY_RE = re.compile(r"\b(?:0[1-9]|1[0-2])\s*/\s*(?:\d{2}|\d{4})\b")
_CVV_RE = re.compile(r"(?i)\b(?:cvv|cvc|csc|security\s*code)\b\D{0,4}\d{3,4}")
_OTP_RE = re.compile(
    r"(?i)\b(?:otp|one[- ]?time\s+(?:password|code)|verification\s+code)\b\D{0,4}\d{4,8}"
)


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def redact(text: str) -> str:
    """Strip card-shaped, expiry, CVV, and OTP values from ``text``."""
    text = _CVV_RE.sub("[REDACTED_PAYMENT_VALUE]", text)
    text = _OTP_RE.sub("[REDACTED_AUTH_VALUE]", text)
    text = _EXPIRY_RE.sub("[REDACTED_PAYMENT_VALUE]", text)
    for match in reversed(list(_PAN_RE.finditer(text))):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            text = text[: match.start()] + "[REDACTED_PAYMENT_VALUE]" + text[match.end():]
    return text


def redact_payment_values(text: str, values: dict) -> str:
    for value in values.values():
        if value:
            text = re.sub(re.escape(value), "[REDACTED_PAYMENT_VALUE]", text, flags=re.IGNORECASE)
    return redact(text)


def redact_payment_data(value, payment_values: dict):
    if isinstance(value, str):
        return redact_payment_values(value, payment_values)
    if isinstance(value, dict):
        return {
            key: redact_payment_data(item, payment_values)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_payment_data(item, payment_values) for item in value]
    return value


# --- deterministic page scripts ---------------------------------------------
# All reads are text-only: no script here ever reads an <input>.value, and the
# executor never takes a snapshot or screenshot after payment fields are
# filled.

PAGE_PROBE_JS = (
    "(() => { const t = document.body ? document.body.innerText : '';"
    " const m = document.querySelector('#merchant, [data-merchant]');"
    " const f = document.querySelector('form');"
    " return JSON.stringify({url: location.href, text: t.slice(0, 20000),"
    " merchant: (m && m.textContent || document.title || '').trim(),"
    " has_form: !!f, form_action: f ? f.action : '',"
    " filled: document.documentElement.dataset.hermesPurchaseFilled === '1'}); })()"
)

def _origin_guard_js(canonical_domain: str, fake_e2e: bool) -> str:
    return (
        "const allowed = raw => { try { const u = new URL(raw);"
        f" const canonical = {json.dumps(canonical_domain.lower())};"
        f" if ({json.dumps(fake_e2e)}) return ['127.0.0.1','localhost','::1'].includes(u.hostname);"
        " return u.protocol === 'https:' && u.hostname === canonical;"
        " } catch { return false; } };"
    )


def submit_expression(canonical_domain: str, fake_e2e: bool) -> str:
    return (
        "(() => {"
        + _origin_guard_js(canonical_domain, fake_e2e)
        + " const f = document.querySelector('form');"
        " if (!f || !allowed(location.href) || !allowed(f.action))"
        " return JSON.stringify({ok: false});"
        " HTMLFormElement.prototype.submit.call(f);"
        " return JSON.stringify({ok: true}); })()"
    )


def fill_expression(values: dict, canonical_domain: str, fake_e2e: bool) -> str:
    # ponytail: fixed input[name=...] selector map (mock-merchant contract);
    # per-merchant selector maps are the upgrade path before any real checkout.
    return (
        "(() => { try { %s const f = document.querySelector('form');"
        " if (!f || !allowed(location.href) || !allowed(f.action))"
        " return JSON.stringify({ok: false, reason: 'wrong_origin'});"
        " const v = %s;"
        " for (const [name, val] of Object.entries(v)) {"
        " const el = document.querySelector(`input[name='${name}']`);"
        " if (!el) return JSON.stringify({ok: false, missing: name});"
        " el.value = val; }"
        " document.documentElement.dataset.hermesPurchaseFilled = '1';"
        " return JSON.stringify({ok: true});"
        " } catch { return JSON.stringify({ok: false}); } })()"
        % (_origin_guard_js(canonical_domain, fake_e2e), json.dumps(values))
    )


CHALLENGE_RE = re.compile(
    r"(?i)captcha|are you a robot|one[- ]?time\s+(?:password|code)|"
    r"verification code|multi[- ]?factor|\bmfa\b"
)
POST_SUBMIT_CHALLENGE_RE = re.compile(
    r"(?i)3[- ]?d[- ]?secure|\b3ds\b|verify your card|authentication required|"
    r"bank authentication"
)
SUCCESS_RE = re.compile(
    r"(?i)order (?:confirmed|complete)|thank you for your (?:order|purchase)|"
    r"payment (?:received|successful)"
)
RECURRING_RE = re.compile(
    r"(?i)subscription|auto[- ]?renew|recurring|billed (?:monthly|annually|yearly)"
)
AUTO_RENEW_RE = re.compile(r"(?i)auto(?:matically)?[- ]?renew")
TOTAL_RE = re.compile(r"(?i)total\D{0,10}(\d+\.\d{2})")


# --- control-flow signals ----------------------------------------------------


class BridgeTransportError(RuntimeError):
    pass


class _Terminated(Exception):
    pass


class _Stop(Exception):
    """Deterministic terminal outcome decided before/without completion."""

    def __init__(self, kind: str, category: str):
        super().__init__(category)
        self.kind = kind  # "definitive" | "uncertain"
        self.category = category


# --- seams -------------------------------------------------------------------


def real_browser() -> SimpleNamespace:
    from tools import browser_tool as bt

    def _call(fn, /, *args, **kwargs) -> dict:
        secret_env = {
            key: os.environ.pop(key)
            for key in list(os.environ)
            if key == "CREDENTIALS_DIRECTORY"
            or key.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
            or key in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
        }
        try:
            result = fn(*args, **kwargs)
            return json.loads(result) if isinstance(result, str) else result
        finally:
            os.environ.update(secret_env)

    return SimpleNamespace(
        navigate=lambda url, task_id: _call(bt.browser_navigate, url, task_id=task_id),
        eval_js=lambda exp, task_id: _call(bt._browser_eval, exp, task_id=task_id, stdin=True),
        eval_sensitive=lambda exp, task_id: _call(
            bt._browser_eval, exp, task_id=task_id, stdin=True, suppress_output=True
        ),
        cleanup=lambda task_id: _call(bt.cleanup_browser, task_id),
    )


def preflight_browser_config() -> list[str]:
    """Fail-closed config checks; runs BEFORE any ticket is claimed."""
    from hermes_cli.config import cfg_get, read_raw_config
    from tools import browser_tool as bt

    problems = []
    if not bt._is_local_mode() or bt._is_camofox_mode():
        problems.append("browser must run locally (no cloud provider, Camofox, or CDP override)")
    if bt._get_browser_engine() not in {"auto", "chrome"}:
        problems.append("browser engine must be Chromium (auto or chrome)")
    if bt._get_sandbox_bypass_mode() != "never":
        problems.append("browser.sandbox_bypass must be 'never'")
    for name in ("AGENT_BROWSER_ARGS", "AGENT_BROWSER_CHROME_FLAGS"):
        if os.environ.get(name, "").strip():
            problems.append(f"{name} must be unset")
    cfg = read_raw_config()
    if cfg_get(cfg, "browser", "record_sessions", default=False):
        problems.append("browser.record_sessions must be disabled")
    return problems


def bridge_post_factory(base_url: str, token: str, timeout: float = 30.0):
    if not token.strip():
        raise BridgeTransportError("bridge token is not configured")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)

    def post(action: str, context: dict, user_intent: str) -> tuple[int, dict]:
        packet = {
            "source_agent": "hermes",
            "requested_action": action,
            "user_intent": user_intent,
            "context": context,
        }
        body = json.dumps(packet).encode("utf-8")
        request = urllib.request.Request(
            base_url.rstrip("/") + BRIDGE_PATH,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            except Exception:
                return error.code, {"status": "transport_error"}
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise BridgeTransportError(redact(str(error))) from error

    return post


# --- helpers -----------------------------------------------------------------


def strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in LOOPBACK_HOSTS


class _LoopbackProxy(BaseHTTPRequestHandler):
    """Tiny fake-E2E proxy: loopback targets only, no logging or persistence."""

    def log_message(self, *args):
        pass

    def _target(self, raw: str):
        parts = urlsplit(raw if "://" in raw else "//" + raw)
        host = (parts.hostname or "").lower()
        if host not in LOOPBACK_HOSTS:
            self.send_error(403)
            return None
        return parts, host

    def do_CONNECT(self):
        target = self._target(self.path)
        if not target:
            return
        parts, host = target
        try:
            remote = socket.create_connection((host, parts.port or 443), timeout=10)
            self.send_response(200, "Connection Established")
            self.end_headers()
            peers = (self.connection, remote)
            while True:
                readable, _, _ = select.select(peers, (), (), 10)
                if not readable:
                    break
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (remote if source is self.connection else self.connection).sendall(data)
        except OSError:
            self.send_error(502)
        finally:
            if "remote" in locals():
                remote.close()

    def _forward(self):
        target = self._target(self.path)
        if not target:
            return
        parts, host = target
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"connection", "proxy-connection", "transfer-encoding"}
        }
        path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        connection = http.client.HTTPConnection(host, parts.port or 80, timeout=15)
        try:
            connection.request(self.command, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "transfer-encoding", "content-length"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            self.send_error(502)
        finally:
            connection.close()

    do_GET = _forward
    do_POST = _forward


@contextmanager
def loopback_browser_proxy():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackProxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def origin_allowed(url: str, canonical_domain: str, *, fake_e2e: bool) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if fake_e2e:
        return host in LOOPBACK_HOSTS
    if parts.scheme != "https":
        return False
    # V0 is bound to the exact host approved by governance. This is stricter
    # than registrable-domain matching and needs no ambient Public Suffix List.
    return host == canonical_domain


def page_origin_allowed(page: dict, canonical_domain: str, *, fake_e2e: bool) -> bool:
    return origin_allowed(page.get("url", ""), canonical_domain, fake_e2e=fake_e2e) and (
        not page.get("has_form")
        or origin_allowed(page.get("form_action", ""), canonical_domain, fake_e2e=fake_e2e)
    )


def check_terms(page_text: str, claim: dict) -> list[str]:
    problems = []
    approved_total = claim["maximum_total"]
    totals = TOTAL_RE.findall(page_text)
    if not totals:
        problems.append("total_missing")
    elif any(total != approved_total for total in totals):
        problems.append("total_mismatch")
    if claim["currency"] not in page_text:
        problems.append("currency_missing")
    if claim["approved_item"].lower() not in page_text.lower():
        problems.append("item_missing")
    quantity_match = re.search(r"(?i)quantity\D{0,5}(\d+)", page_text)
    if not quantity_match:
        problems.append("quantity_missing")
    elif int(quantity_match.group(1)) != int(claim["quantity"]):
        problems.append("quantity_mismatch")
    recurrence = claim["recurrence_authorization"]
    is_recurring_page = bool(RECURRING_RE.search(page_text))
    if recurrence["commitment_type"] == "one_time" and is_recurring_page:
        problems.append("unexpected_recurrence")
    if recurrence["commitment_type"] != "one_time" and not is_recurring_page:
        problems.append("recurrence_not_shown")
    if recurrence["commitment_type"] != "one_time":
        normalized_text = " ".join(page_text.lower().split())
        labeled_fields = {
            "billing_interval": r"billing\s+interval|billed",
            "renewal_amount": r"renewal\s+(?:amount|price|total)|renews\s+at",
            "renewal_date": r"renewal\s+date|renews\s+on",
            "cancellation_deadline": r"cancellation\s+deadline|cancel\s+by",
            "contract_duration": r"contract\s+duration|contract\s+term",
            "cancellation_terms": r"cancellation\s+terms",
        }
        commitment = str(recurrence.get("commitment_type") or "").lower()
        if commitment and not re.search(rf"\b{re.escape(commitment)}\b", normalized_text):
            problems.append("commitment_type_mismatch")
        for field, label in labeled_fields.items():
            value = " ".join(str(recurrence.get(field) or "").lower().split())
            if value and not re.search(
                rf"(?:{label})\D{{0,20}}{re.escape(value)}", normalized_text
            ):
                problems.append(f"{field}_mismatch")
        if bool(recurrence.get("auto_renew")) != bool(AUTO_RENEW_RE.search(page_text)):
            problems.append("auto_renew_mismatch")
    return problems


def load_payment_fields() -> dict:
    """Read synthetic/real payment fields; $CREDENTIALS_DIRECTORY only."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not directory:
        raise _Stop("definitive", "credentials_unavailable")
    values = {}
    for field in CREDENTIAL_FIELDS:
        path = Path(directory) / field
        if not path.is_file():
            raise _Stop("definitive", "credentials_unavailable")
        values[field] = path.read_text(encoding="utf-8").strip()
    return values


def read_ticket_token(stdin=None) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if directory:
        candidate = Path(directory) / "ticket_token"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    stream = stdin if stdin is not None else sys.stdin
    return stream.readline().strip()


def read_bridge_token() -> str:
    token = os.environ.get(BRIDGE_TOKEN_ENV, "").strip()
    if token:
        return token
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    candidate = Path(directory) / "cogitator_bridge_token" if directory else None
    return candidate.read_text(encoding="utf-8").strip() if candidate and candidate.is_file() else ""


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


def _append_private(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(content)


def write_receipt(
    state_dir: Path,
    ticket_id: str,
    page_text: str,
    claim: dict,
    merchant_name: str,
    payment_values: dict,
) -> dict:
    relative = f"{RECEIPT_PREFIX}/{ticket_id}.txt"
    content = redact_payment_values(page_text, payment_values)[:4000]
    _write_private(state_dir / relative, content)
    return {
        "restricted_artifact_path": relative,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "artifact_type": "receipt",
        "sanitized_summary": redact(
            f"{claim['approved_item']} purchased from {merchant_name}"
        )[:300],
        "sanitized_metadata": {
            "merchant": redact(merchant_name)[:100],
            "item": claim["approved_item"],
            "amount": claim["maximum_total"],
            "currency": claim["currency"],
            "issued_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def spool_result(state_dir: Path, ticket_id: str, action: str, context: dict) -> Path:
    path = state_dir / "spool" / f"{ticket_id}.json"
    _write_private(path, redact(json.dumps({"requested_action": action, "context": context}, default=str)))
    return path


def audit_factory(state_dir: Path):
    path = state_dir / "audit.jsonl"
    if not path.exists():
        _write_private(path, "")

    def audit(phase: str, **fields) -> None:
        line = {"ts": datetime.now(timezone.utc).isoformat(), "phase": phase, **fields}
        _append_private(path, redact(json.dumps(line, default=str)) + "\n")

    return audit


def build_completion_context(claim: dict, merchant_name: str, receipt: dict) -> dict:
    recurrence = claim["recurrence_authorization"]
    return {
        "proposal_id": claim["proposal_id"],
        "ticket_id": claim["ticket_id"],
        "idempotency_key": f"exec-{claim['ticket_id']}",
        "merchant_display_name": merchant_name,
        "merchant_domain": claim["canonical_merchant_domain"],
        "product_or_service": claim["approved_item"],
        "quantity": claim["quantity"],
        # Page total was revalidated equal to the approved maximum pre-submit;
        # governance separately enforces exact equality with the approval.
        "final_amount": claim["maximum_total"],
        "currency": claim["currency"],
        "commitment_type": recurrence["commitment_type"],
        "billing_interval": recurrence["billing_interval"],
        "renewal_amount": recurrence["renewal_amount"],
        "renewal_date": recurrence["renewal_date"],
        "cancellation_deadline": recurrence["cancellation_deadline"],
        "contract_duration": recurrence["contract_duration"],
        "auto_renew": recurrence["auto_renew"],
        "cancellation_terms": recurrence["cancellation_terms"],
        "receipt": receipt,
    }


def failure_context(claim: dict, category: str) -> dict:
    return {
        "proposal_id": claim["proposal_id"],
        "ticket_id": claim["ticket_id"],
        "idempotency_key": f"result-{claim['ticket_id']}",
        "failure_category": category,
        "external_reference_token": "",
    }


# --- the single run ----------------------------------------------------------


def run_once(
    *,
    bridge_post,
    browser,
    audit,
    state_dir: Path,
    checkout_url_for,
    fake_e2e: bool,
    stdin=None,
    post_submit_wait: float = POST_SUBMIT_WAIT_SECONDS,
    sleep=time.sleep,
) -> int:
    """One attempt: claim → checkout → exactly one terminal report. No retry."""
    intent = "Restricted Purchase Executor V0 run for one claimed execution ticket."

    termination = {"active": True, "pending": False}

    def handle_sigterm(signum, frame):
        if termination["active"]:
            raise _Terminated()
        termination["pending"] = True

    previous_handler = signal.signal(signal.SIGTERM, handle_sigterm)
    claimed = False
    try:
        ticket_token = read_ticket_token(stdin)
        if not ticket_token:
            audit("claim_refused", reason="empty_ticket_token")
            return EXIT_CLAIM_FAILED
        try:
            status, claim = bridge_post(
                "claim_execution_ticket", {"ticket_token": ticket_token}, intent
            )
        except BridgeTransportError:
            audit("claim_transport_failed")
            return EXIT_CLAIM_FAILED
        if status != 200 or claim.get("status") != "ok":
            audit("claim_rejected", http_status=status, reason_code=claim.get("reason_code", ""))
            return EXIT_CLAIM_FAILED
        termination["active"] = False
        claimed = True
    except _Terminated:
        audit("terminated_before_claim")
        return EXIT_CLAIM_FAILED
    finally:
        if not claimed:
            signal.signal(signal.SIGTERM, previous_handler)

    ticket_id = claim["ticket_id"]
    task_id = f"purchase_{ticket_id}"
    terminal_sent = {"done": False}
    payment_values = {}

    def terminal(action: str, context: dict) -> None:
        # Exactly one terminal report per run, ever.
        if terminal_sent["done"]:
            raise AssertionError("terminal result already reported")
        terminal_sent["done"] = True
        safe_context = redact_payment_data(context, payment_values)
        try:
            status_code, result = bridge_post(action, safe_context, intent)
            if not isinstance(result, dict) or status_code != 200 or result.get("status") != "ok":
                raise BridgeTransportError("terminal callback rejected")
            audit(
                "terminal_reported",
                action=action,
                http_status=status_code,
                status=redact_payment_values(str(result.get("status", "")), payment_values),
                reason_code=redact_payment_values(
                    str(result.get("reason_code", "")), payment_values
                ),
            )
        except BridgeTransportError:
            spool_path = spool_result(state_dir, ticket_id, action, safe_context)
            audit("terminal_spooled", action=action, spool=str(spool_path))
            raise

    submitted = False
    try:
        try:
            termination["active"] = True
            if termination["pending"]:
                raise _Terminated()
            audit("claimed", ticket_id=ticket_id, proposal_id=claim["proposal_id"], task_id=task_id)
            checkout_url = checkout_url_for(claim)
            if not origin_allowed(
                checkout_url, claim["canonical_merchant_domain"], fake_e2e=fake_e2e
            ):
                raise _Stop("definitive", "wrong_origin")
            navigation = browser.navigate(checkout_url, task_id)
            audit("navigated", url=strip_query(checkout_url), success=bool(navigation.get("success")))
            if not navigation.get("success"):
                raise _Stop("definitive", "navigation_failed")

            probe = browser.eval_js(PAGE_PROBE_JS, task_id)
            if not probe.get("success"):
                raise _Stop("definitive", "page_read_failed")
            page = probe["result"]
            if not page_origin_allowed(
                page, claim["canonical_merchant_domain"], fake_e2e=fake_e2e
            ):
                audit("origin_rejected", url=strip_query(page.get("url", "")))
                raise _Stop("definitive", "wrong_origin")
            page_text = page.get("text", "")
            if CHALLENGE_RE.search(page_text):
                raise _Stop("definitive", "human_challenge_required")
            mismatches = check_terms(page_text, claim)
            if mismatches:
                audit("terms_rejected", mismatches=mismatches)
                raise _Stop("definitive", "terms_changed")
            if not page.get("has_form"):
                raise _Stop("definitive", "checkout_form_missing")
            if page.get("filled"):
                raise _Stop("definitive", "invalid_checkout_state")
            merchant_name = page.get("merchant", "")
            audit("revalidated", total=claim["maximum_total"], currency=claim["currency"])

            payment_values = load_payment_fields()
            fill = browser.eval_sensitive(
                fill_expression(
                    payment_values, claim["canonical_merchant_domain"], fake_e2e
                ),
                task_id,
            )
            if not fill.get("success") or not fill.get("result", {}).get("ok"):
                category = (
                    "wrong_origin"
                    if fill.get("result", {}).get("reason") == "wrong_origin"
                    else "fill_failed"
                )
                raise _Stop("definitive", category)
            audit("filled")

            # Post-fill, pre-submit revalidation: text-only re-read, never a
            # snapshot/screenshot; the probe reads no input values.
            recheck = browser.eval_js(PAGE_PROBE_JS, task_id)
            if not recheck.get("success"):
                raise _Stop("definitive", "page_read_failed")
            recheck_page = recheck["result"]
            if not recheck_page.get("filled"):
                raise _Stop("definitive", "fill_failed")
            if not page_origin_allowed(
                recheck_page, claim["canonical_merchant_domain"], fake_e2e=fake_e2e
            ):
                raise _Stop("definitive", "wrong_origin")
            recheck_text = recheck_page.get("text", "")
            if CHALLENGE_RE.search(recheck_text):
                raise _Stop("definitive", "human_challenge_required")
            if check_terms(recheck_text, claim):
                raise _Stop("definitive", "terms_changed")

            submitted = True
            submit = browser.eval_js(
                submit_expression(claim["canonical_merchant_domain"], fake_e2e), task_id
            )
            if not submit.get("success") or not submit.get("result", {}).get("ok"):
                raise _Stop("uncertain", "submit_outcome_unknown")
            audit("submitted")

            deadline = time.monotonic() + post_submit_wait
            final_text = ""
            while True:
                outcome = browser.eval_js(PAGE_PROBE_JS, task_id)
                if outcome.get("success"):
                    final_text = outcome["result"].get("text", "")
                    if SUCCESS_RE.search(final_text):
                        break
                    if POST_SUBMIT_CHALLENGE_RE.search(final_text) or CHALLENGE_RE.search(final_text):
                        raise _Stop("uncertain", "post_submit_challenge")
                if time.monotonic() >= deadline:
                    raise _Stop("uncertain", "outcome_unknown")
                sleep(1)

            receipt = write_receipt(
                state_dir, ticket_id, final_text, claim, merchant_name, payment_values
            )
            terminal("record_completed_purchase", build_completion_context(claim, merchant_name, receipt))
            return EXIT_COMPLETED
        except _Stop as stop:
            audit("stopped", kind=stop.kind, category=stop.category, submitted=submitted)
            if stop.kind == "uncertain":
                terminal("record_uncertain_result", failure_context(claim, stop.category))
                return EXIT_UNCERTAIN
            terminal("record_definitive_failure", failure_context(claim, stop.category))
            return EXIT_DEFINITIVE_FAILURE
        except _Terminated:
            audit("terminated", submitted=submitted)
            if submitted:
                terminal("record_uncertain_result", failure_context(claim, "terminated_after_submit"))
                return EXIT_UNCERTAIN
            terminal("record_definitive_failure", failure_context(claim, "terminated_before_submit"))
            return EXIT_DEFINITIVE_FAILURE
        except BridgeTransportError:
            raise
        except Exception as error:
            audit("executor_error", submitted=submitted, error_type=type(error).__name__)
            action = "record_uncertain_result" if submitted else "record_definitive_failure"
            category = "executor_error_after_submit" if submitted else "executor_error_before_submit"
            terminal(action, failure_context(claim, category))
            return EXIT_UNCERTAIN if submitted else EXIT_DEFINITIVE_FAILURE
    except BridgeTransportError:
        return EXIT_SPOOLED
    finally:
        termination["active"] = False
        try:
            browser.cleanup(task_id)
            audit("cleaned_up", task_id=task_id)
        except Exception as error:  # cleanup must never mask the outcome
            audit("cleanup_failed", task_id=task_id, error_type=type(error).__name__)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)


# --- CLI ---------------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Restricted Purchase Executor V0 (one-shot).")
    parser.add_argument("--bridge-url", required=True, help="Cogitator bridge base URL")
    parser.add_argument("--fake-e2e", action="store_true",
                        help="loopback-only acceptance mode; refuses any non-loopback URL")
    parser.add_argument("--checkout-url", default="",
                        help="explicit checkout URL; only valid with --fake-e2e")
    parser.add_argument("--checkout-path", default="/checkout",
                        help="path appended to https://<canonical merchant domain>")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                        help="audit/spool/receipt directory")
    args = parser.parse_args(argv)
    if args.fake_e2e:
        if not args.checkout_url or not _is_loopback(args.checkout_url):
            parser.error("--fake-e2e requires a loopback --checkout-url")
        if not _is_loopback(args.bridge_url):
            parser.error("--fake-e2e requires a loopback --bridge-url")
    elif args.checkout_url:
        parser.error("--checkout-url is only valid with --fake-e2e")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    problems = preflight_browser_config()
    if problems:
        for problem in problems:
            print(f"preflight: {problem}", file=sys.stderr)
        return EXIT_PREFLIGHT

    state_dir = Path(args.state_dir).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        bridge_post = bridge_post_factory(args.bridge_url, read_bridge_token())
    except BridgeTransportError as error:
        print(f"preflight: {error}", file=sys.stderr)
        return EXIT_PREFLIGHT

    def checkout_url_for(claim: dict) -> str:
        if args.fake_e2e:
            return args.checkout_url
        return f"https://{claim['canonical_merchant_domain']}{args.checkout_path}"

    def execute() -> int:
        return run_once(
            bridge_post=bridge_post,
            browser=real_browser(),
            audit=audit_factory(state_dir),
            state_dir=state_dir,
            checkout_url_for=checkout_url_for,
            fake_e2e=args.fake_e2e,
        )

    if not args.fake_e2e:
        return execute()
    with loopback_browser_proxy() as proxy_port:
        os.environ["AGENT_BROWSER_ARGS"] = (
            f"--proxy-server=http://127.0.0.1:{proxy_port},"
            "--proxy-bypass-list=<-loopback>"
        )
        try:
            return execute()
        finally:
            os.environ.pop("AGENT_BROWSER_ARGS", None)


if __name__ == "__main__":
    sys.exit(main())
