from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest  # ty: ignore[unresolved-import]

import registrar_porkbun as porkbun

FIXTURES = Path(__file__).parent / "fixtures" / "porkbun_api_v3"
FAKE_API_KEY = "pk1_fake_WP3"
FAKE_SECRET_KEY = "sk1_fake_WP3"
REGISTER_KEY = "jb_job-1_register_example.com"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


class MutationHandler(BaseHTTPRequestHandler):
    scenario = "happy"
    requests: list[dict[str, Any]] = []
    ambiguous_request: tuple[str, dict[str, Any]] | None = None
    next_record_id = 2
    zone: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _capture(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        self.requests.append({
            "method": self.command,
            "path": self.path,
            "idempotency_key": self.headers.get("Idempotency-Key"),
            "api_key": self.headers.get("X-API-Key"),
            "secret_key": self.headers.get("X-Secret-API-Key"),
            "body": body,
        })
        return body

    def _reply(
        self,
        status: int,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        self._capture()
        if self.path == "/dns/retrieve/example.com":
            self._reply(
                200,
                {
                    "status": "SUCCESS",
                    "cloudflare": "disabled",
                    "records": self.zone,
                },
            )
            return
        self._reply(404, {"status": "ERROR", "message": "not found"})

    def do_POST(self) -> None:
        body = self._capture()
        assert isinstance(body, dict)
        if self.path == "/domain/create/example.com":
            self._domain_create(body)
            return
        if self.path == "/redirect-target":
            self._reply(200, fixture("domain_create.json"))
            return
        if self.path == "/dns/create/example.com":
            self._dns_create(body)
            return
        if self.path.startswith("/dns/edit/example.com/"):
            self._dns_edit(self.path.rsplit("/", 1)[1], body)
            return
        if self.path.startswith("/dns/delete/example.com/"):
            self._dns_delete(self.path.rsplit("/", 1)[1])
            return
        self._reply(404, {"status": "ERROR", "message": "not found"})

    def _domain_create(self, body: dict[str, Any]) -> None:
        if body.get("dryRun") is True:
            if self.scenario == "insufficient":
                self._reply(400, fixture("insufficient_funds.json"))
            else:
                self._reply(200, fixture("domain_create_dry_run.json"))
            return
        if self.scenario == "timeout":
            time.sleep(0.25)
            self._reply(200, fixture("domain_create.json"))
            return
        if self.scenario == "drop":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if self.scenario == "redirect":
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.scenario == "in_use":
            self._reply(409, fixture("idempotency_in_use.json"))
            return
        if self.scenario == "mismatch":
            self._reply(409, fixture("idempotency_mismatch.json"))
            return
        if self.scenario == "leak":
            self._reply(
                400,
                {
                    "status": "ERROR",
                    "message": (
                        f"bad api_key={FAKE_API_KEY} secretapikey={FAKE_SECRET_KEY}"
                    ),
                    "code": "INVALID_API_KEYS_001",
                },
            )
            return
        if self.scenario == "wrong_cost":
            response = fixture("domain_create.json")
            response["cost"] += 1
            self._reply(200, response)
            return
        if self.scenario == "ambiguous_replay":
            current = (self.headers["Idempotency-Key"], body)
            if self.ambiguous_request is None:
                type(self).ambiguous_request = current
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if current != self.ambiguous_request:
                self._reply(409, fixture("idempotency_mismatch.json"))
                return
            self._reply(
                200,
                fixture("domain_create.json"),
                {"Idempotent-Replayed": "true"},
            )
            return
        self._reply(200, fixture("domain_create.json"))

    def _dns_create(self, body: dict[str, Any]) -> None:
        identifier = str(self.next_record_id)
        type(self).next_record_id += 1
        name = body.get("name", "")
        fqdn = "example.com" if not name else f"{name}.example.com"
        self.zone.append({
            "id": identifier,
            "name": fqdn,
            "type": body["type"],
            "content": body["content"],
            "ttl": str(body.get("ttl", 600)),
            "prio": str(body["prio"]) if "prio" in body else None,
            "notes": body.get("notes"),
        })
        response = fixture("dns_create.json")
        response["id"] = identifier
        self._reply(200, response)

    def _dns_edit(self, identifier: str, body: dict[str, Any]) -> None:
        record = next(record for record in self.zone if record["id"] == identifier)
        if "name" in body:
            record["name"] = (
                "example.com" if not body["name"] else f"{body['name']}.example.com"
            )
        record["type"] = body["type"]
        record["content"] = body["content"]
        if "ttl" in body:
            record["ttl"] = str(body["ttl"])
        if "prio" in body:
            record["prio"] = str(body["prio"])
        if "notes" in body:
            record["notes"] = body["notes"]
        self._reply(200, fixture("dns_success.json"))

    def _dns_delete(self, identifier: str) -> None:
        type(self).zone = [record for record in self.zone if record["id"] != identifier]
        self._reply(200, fixture("dns_success.json"))


@contextmanager
def fake_server(monkeypatch, *, scenario: str = "happy", timeout: float = 1):
    MutationHandler.scenario = scenario
    MutationHandler.requests = []
    MutationHandler.ambiguous_request = None
    MutationHandler.next_record_id = 2
    MutationHandler.zone = [
        {
            "id": "1",
            "name": "legacy.example.com",
            "type": "A",
            "content": "192.0.2.10",
            "ttl": "600",
            "prio": None,
            "notes": None,
        }
    ]
    server = ThreadingHTTPServer(("127.0.0.1", 0), MutationHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PORKBUN_API_BASE", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("PORKBUN_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("PORKBUN_SECRET_KEY", FAKE_SECRET_KEY)
    monkeypatch.delenv("PORKBUN_CREDENTIALS_FILE", raising=False)
    try:
        yield porkbun.PorkbunClient(timeout=timeout)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_domain_dry_run_and_live_exact_contract(monkeypatch):
    with fake_server(monkeypatch) as client:
        preview = client.create_domain("Example.COM", cost=1108, dry_run=True)
        result = client.create_domain(
            "example.com",
            cost=1108,
            dry_run=False,
            idempotency_key=REGISTER_KEY,
        )
    assert preview["wouldSucceed"] is True
    assert result["orderId"] == 12345678
    dry_request, live_request = MutationHandler.requests
    assert dry_request["body"] == {
        "agreeToTerms": "yes",
        "cost": 1108,
        "dryRun": True,
        "whoisPrivacy": True,
    }
    assert dry_request["idempotency_key"] is None
    assert live_request["body"] == {
        "agreeToTerms": "yes",
        "cost": 1108,
        "whoisPrivacy": True,
    }
    assert live_request["idempotency_key"] == REGISTER_KEY
    assert all(
        request["body"].keys().isdisjoint({"apikey", "secretapikey"})
        for request in MutationHandler.requests
    )


@pytest.mark.parametrize("scenario", ["timeout", "drop", "redirect"])
def test_mutation_transport_is_single_attempt_and_uncertain(monkeypatch, scenario):
    with fake_server(monkeypatch, scenario=scenario, timeout=0.05) as client:
        with pytest.raises(porkbun.PorkbunMutationUncertainError):
            client.create_domain(
                "example.com",
                cost=1108,
                dry_run=False,
                idempotency_key=REGISTER_KEY,
            )
    assert [request["path"] for request in MutationHandler.requests] == [
        "/domain/create/example.com"
    ]


def test_manual_replay_after_ambiguous_attempt(monkeypatch):
    with fake_server(monkeypatch, scenario="ambiguous_replay") as client:
        with pytest.raises(porkbun.PorkbunMutationUncertainError):
            client.create_domain(
                "example.com",
                cost=1108,
                dry_run=False,
                idempotency_key=REGISTER_KEY,
            )
        replay = client.create_domain(
            "example.com",
            cost=1108,
            dry_run=False,
            idempotency_key=REGISTER_KEY,
        )
    assert replay["idempotentReplayed"] is True
    assert MutationHandler.requests[0]["body"] == MutationHandler.requests[1]["body"]
    assert {request["idempotency_key"] for request in MutationHandler.requests} == {
        REGISTER_KEY
    }


def test_insufficient_funds_is_provider_error(monkeypatch):
    with fake_server(monkeypatch, scenario="insufficient") as client:
        with pytest.raises(porkbun.PorkbunAPIError) as error:
            client.create_domain("example.com", cost=1108, dry_run=True)
    assert error.value.code == "INSUFFICIENT_FUNDS"


@pytest.mark.parametrize(
    ("scenario", "code", "uncertain"),
    [
        ("in_use", "IDEMPOTENCY_KEY_IN_USE", True),
        ("mismatch", "IDEMPOTENCY_KEY_MISMATCH", False),
    ],
)
def test_idempotency_409_is_typed(monkeypatch, scenario, code, uncertain):
    with fake_server(monkeypatch, scenario=scenario) as client:
        with pytest.raises(porkbun.PorkbunIdempotencyError) as error:
            client.create_domain(
                "example.com",
                cost=1108,
                dry_run=False,
                idempotency_key=REGISTER_KEY,
            )
    assert error.value.code == code
    assert error.value.http_status == 409
    assert error.value.outcome_uncertain is uncertain


def test_live_quote_mismatch_is_uncertain(monkeypatch):
    with fake_server(monkeypatch, scenario="wrong_cost") as client:
        with pytest.raises(porkbun.PorkbunMutationUncertainError):
            client.create_domain(
                "example.com",
                cost=1108,
                dry_run=False,
                idempotency_key=REGISTER_KEY,
            )


def test_dns_crud_and_snapshot_restore(monkeypatch):
    with fake_server(monkeypatch) as client:
        snapshot = client.get_dns_records("example.com")["records"]
        client.dns_edit(
            "example.com",
            "1",
            record_type="A",
            content="192.0.2.11",
            name="legacy",
            ttl=600,
            idempotency_key="jb_1_dns_edit_1",
        )
        created = [
            client.dns_create(
                "example.com",
                record_type="A",
                content="23.227.38.65",
                idempotency_key="jb_1_dns_create_a",
            )["id"],
            client.dns_create(
                "example.com",
                record_type="AAAA",
                content="2620:127:f00f:5::",
                idempotency_key="jb_1_dns_create_aaaa",
            )["id"],
            client.dns_create(
                "example.com",
                record_type="CNAME",
                content="shops.myshopify.com.",
                name="www",
                idempotency_key="jb_1_dns_create_www",
            )["id"],
        ]
        applied = client.get_dns_records("example.com")["records"]
        assert {record["type"] for record in applied} >= {"A", "AAAA", "CNAME"}

        original = snapshot[0]
        client.dns_edit(
            "example.com",
            original["id"],
            record_type=original["type"],
            content=original["content"],
            name="legacy",
            ttl=int(original["ttl"]),
            idempotency_key="jb_1_dns_restore_1",
        )
        for identifier in created:
            client.dns_delete(
                "example.com",
                identifier,
                idempotency_key=f"jb_1_dns_restore_delete_{identifier}",
            )
        restored = client.get_dns_records("example.com")["records"]
    assert restored == snapshot
    assert (
        len({
            request["idempotency_key"]
            for request in MutationHandler.requests
            if request["method"] == "POST"
        })
        == 8
    )


def test_mutation_errors_do_not_expose_secrets(monkeypatch, capsys):
    with fake_server(monkeypatch, scenario="leak") as client:
        with pytest.raises(porkbun.PorkbunAuthenticationError) as error:
            client.create_domain(
                "example.com",
                cost=1108,
                dry_run=False,
                idempotency_key=REGISTER_KEY,
            )
    output = capsys.readouterr()
    combined = f"{error.value}\n{output.out}\n{output.err}"
    assert FAKE_API_KEY not in combined
    assert FAKE_SECRET_KEY not in combined


def test_write_inputs_fail_before_dispatch(monkeypatch):
    with fake_server(monkeypatch) as client:
        invalid_calls = [
            lambda: client.create_domain("example.com", cost=True, dry_run=True),
            lambda: client.create_domain("example.com", cost=1108, dry_run=False),
            lambda: client.create_domain(
                "example.com",
                cost=1108,
                dry_run=False,
                idempotency_key="bad\nkey",
            ),
            lambda: client.dns_create(
                "example.com",
                record_type="a",
                content="192.0.2.1",
                idempotency_key="dns-1",
            ),
            lambda: client.dns_create(
                "example.com",
                record_type="A",
                content="2001:db8::1",
                idempotency_key="dns-2",
            ),
            lambda: client.dns_create(
                "example.com",
                record_type="CNAME",
                content="target.example.",
                name="www.example.com",
                idempotency_key="dns-3",
            ),
            lambda: client.dns_delete("example.com", "0", idempotency_key="dns-4"),
        ]
        for call in invalid_calls:
            with pytest.raises(porkbun.PorkbunConfigurationError):
                call()
    assert MutationHandler.requests == []
