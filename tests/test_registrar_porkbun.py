from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest  # ty: ignore[unresolved-import]

import registrar_porkbun as porkbun

FIXTURES = Path(__file__).parent / "fixtures" / "porkbun_api_v3"
FAKE_API_KEY = "pk1_fake_S1_only"
FAKE_SECRET_KEY = "sk1_fake_S1_only"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakePorkbunHandler(BaseHTTPRequestHandler):
    routes: dict[tuple[str, str], tuple[int, bytes]] = {}
    requests: list[tuple[str, str, str | None, str | None]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _reply(self):
        self.requests.append((
            self.command,
            self.path,
            self.headers.get("X-API-Key"),
            self.headers.get("X-Secret-API-Key"),
        ))
        status, body = self.routes.get((self.command, self.path), (404, b"not found"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_POST = _reply


@contextmanager
def fake_server(monkeypatch, routes):
    FakePorkbunHandler.routes = routes
    FakePorkbunHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePorkbunHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PORKBUN_API_BASE", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("PORKBUN_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("PORKBUN_SECRET_KEY", FAKE_SECRET_KEY)
    monkeypatch.delenv("PORKBUN_CREDENTIALS_FILE", raising=False)
    try:
        yield porkbun.PorkbunClient(timeout=2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def happy_routes():
    return {
        ("GET", "/ping"): (200, fixture("ping.json")),
        ("GET", "/pricing/get"): (200, fixture("pricing.json")),
        ("POST", "/domain/checkDomain/example.com"): (
            200,
            fixture("availability.json"),
        ),
        ("GET", "/domain/getRegistrationRequirements/com"): (
            200,
            fixture("requirements_com.json"),
        ),
        ("GET", "/dns/retrieve/example.com"): (200, fixture("dns_records.json")),
        ("GET", "/domain/getNs/example.com"): (200, fixture("nameservers.json")),
        ("GET", "/domain/listAll"): (200, fixture("domains.json")),
    }


def test_all_read_only_methods_against_fake(monkeypatch):
    with fake_server(monkeypatch, happy_routes()) as client:
        assert client.ping()["credentialsValid"] is True
        assert client.get_default_pricing()["pricing"]["com"]["registration"] == "11.08"
        assert client.check_domain("example.com")["response"]["avail"] == "yes"
        assert client.get_registration_requirements("com")["apiRegisterable"] is True
        assert client.get_dns_records("example.com")["records"][0]["type"] == "A"
        assert client.get_nameservers("example.com")["ns"][0] == "ns1.porkbun.com"
        assert client.list_domains()["domains"][0]["domain"] == "example.com"

    assert {
        (method, path) for method, path, _, _ in FakePorkbunHandler.requests
    } == set(happy_routes())
    for method, path, api_key, secret_key in FakePorkbunHandler.requests:
        assert method == ("POST" if "checkDomain" in path else "GET")
        if path == "/pricing/get":
            assert api_key is None and secret_key is None
        else:
            assert (api_key, secret_key) == (FAKE_API_KEY, FAKE_SECRET_KEY)


def test_mode_0600_credential_file(monkeypatch, tmp_path):
    credentials = tmp_path / "porkbun.json"
    credentials.write_text(
        json.dumps({"apikey": FAKE_API_KEY, "secretapikey": FAKE_SECRET_KEY})
    )
    credentials.chmod(0o600)
    monkeypatch.setenv("PORKBUN_CREDENTIALS_FILE", str(credentials))
    monkeypatch.delenv("PORKBUN_API_KEY", raising=False)
    monkeypatch.delenv("PORKBUN_SECRET_KEY", raising=False)
    monkeypatch.setenv("PORKBUN_API_BASE", "http://127.0.0.1:1")
    assert porkbun.PorkbunClient().base_url == "http://127.0.0.1:1"

    credentials.chmod(0o640)
    with pytest.raises(porkbun.PorkbunConfigurationError, match="mode-0600"):
        porkbun.PorkbunClient()


def test_authentication_failure_is_typed(monkeypatch):
    routes = {("GET", "/ping"): (400, fixture("auth_error.json"))}
    with fake_server(monkeypatch, routes) as client:
        with pytest.raises(porkbun.PorkbunAuthenticationError) as error:
            client.ping()
    assert error.value.code == "INVALID_API_KEYS_001"
    assert error.value.http_status == 400


@pytest.mark.parametrize(
    ("fixture_name", "message"),
    [
        ("malformed.json", "not valid JSON"),
        ("missing_fields.json", "missing fields"),
    ],
)
def test_malformed_or_missing_response_is_typed(monkeypatch, fixture_name, message):
    routes = {("GET", "/ping"): (200, fixture(fixture_name))}
    with fake_server(monkeypatch, routes) as client:
        with pytest.raises(porkbun.PorkbunResponseError, match=message):
            client.ping()


def test_unknown_response_field_is_rejected(monkeypatch):
    payload = json.loads(fixture("ping.json"))
    payload["providerSurprise"] = "no implicit schema expansion"
    routes = {("GET", "/ping"): (200, json.dumps(payload).encode())}
    with fake_server(monkeypatch, routes) as client:
        with pytest.raises(porkbun.PorkbunResponseError, match="unknown fields"):
            client.ping()


def test_provider_declared_failure_is_typed(monkeypatch):
    routes = {
        ("POST", "/domain/checkDomain/example.com"): (
            200,
            fixture("provider_error.json"),
        )
    }
    with fake_server(monkeypatch, routes) as client:
        with pytest.raises(porkbun.PorkbunAPIError) as error:
            client.check_domain("example.com")
    assert error.value.code == "INVALID_DOMAIN"
    assert error.value.http_status is None


def test_http_failure_is_typed(monkeypatch):
    routes = {("GET", "/ping"): (503, b"temporarily unavailable")}
    with fake_server(monkeypatch, routes) as client:
        with pytest.raises(porkbun.PorkbunTransportError, match="HTTP failure.*503"):
            client.ping()


def test_credentials_are_redacted_from_errors_and_output(monkeypatch, capsys):
    leaked = {
        "status": "ERROR",
        "message": f"bad api_key={FAKE_API_KEY} secretapikey={FAKE_SECRET_KEY}",
        "code": "INVALID_API_KEYS_001",
    }
    routes = {("GET", "/ping"): (400, json.dumps(leaked).encode())}
    with fake_server(monkeypatch, routes) as client:
        with pytest.raises(porkbun.PorkbunAuthenticationError) as error:
            client.ping()
    captured = capsys.readouterr()
    combined = f"{error.value}\n{captured.out}\n{captured.err}"
    assert FAKE_API_KEY not in combined
    assert FAKE_SECRET_KEY not in combined
    assert FAKE_API_KEY not in porkbun.redact(f"api_key={FAKE_API_KEY}")
    assert FAKE_SECRET_KEY not in porkbun.redact(f"secretapikey={FAKE_SECRET_KEY}")


@pytest.mark.parametrize(
    "base",
    [
        "http://127.0.0.1:8000/api",
        "http://[::1]:8000/api",
        "http://localhost:8000/api",
    ],
)
def test_loopback_override_allowed(monkeypatch, base):
    monkeypatch.setenv("PORKBUN_API_BASE", base)
    monkeypatch.setenv("PORKBUN_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("PORKBUN_SECRET_KEY", FAKE_SECRET_KEY)
    assert porkbun.PorkbunClient().base_url == base


@pytest.mark.parametrize(
    "base",
    [
        "https://api.porkbun.com/api/json/v3",
        "https://example.com",
        "http://127.0.0.1.evil.example",
        "http://user@127.0.0.1",
    ],
)
def test_non_loopback_override_rejected(monkeypatch, base):
    monkeypatch.setenv("PORKBUN_API_BASE", base)
    monkeypatch.setenv("PORKBUN_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("PORKBUN_SECRET_KEY", FAKE_SECRET_KEY)
    with pytest.raises(porkbun.PorkbunConfigurationError, match="loopback|invalid"):
        porkbun.PorkbunClient()


def test_self_check_uses_internal_fake_only(monkeypatch, capsys):
    monkeypatch.setenv("PORKBUN_API_BASE", "https://not-loopback.example")
    monkeypatch.setenv("PORKBUN_CREDENTIALS_FILE", "/not/read")
    porkbun.run_self_check()
    assert capsys.readouterr().out == "Porkbun read-only self-check: PASS\n"
    assert os.environ["PORKBUN_API_BASE"] == "https://not-loopback.example"
    assert os.environ["PORKBUN_CREDENTIALS_FILE"] == "/not/read"
    assert all(
        path in porkbun._CHECK_RESPONSES for _, path in porkbun._CheckHandler.seen
    )


def test_check_entrypoint_requires_no_credentials():
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(Path(porkbun.__file__).parent),
    }
    result = subprocess.run(
        [sys.executable, str(Path(porkbun.__file__)), "--check"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "Porkbun read-only self-check: PASS\n"


def test_no_mutation_endpoint_is_callable(monkeypatch):
    monkeypatch.setenv("PORKBUN_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("PORKBUN_SECRET_KEY", FAKE_SECRET_KEY)
    client = porkbun.PorkbunClient()
    for name in (
        "register_domain",
        "create_domain",
        "renew_domain",
        "transfer_domain",
        "create_dns_record",
        "edit_dns_record",
        "delete_dns_record",
        "update_nameservers",
    ):
        assert not hasattr(client, name)


def test_private_transport_refuses_mutation_route(monkeypatch):
    routes = {
        ("POST", "/domain/create/example.com"): (
            200,
            b'{"status":"SUCCESS"}',
        )
    }
    with fake_server(monkeypatch, routes) as client:
        with pytest.raises(porkbun.PorkbunConfigurationError, match="read-only route"):
            client._request("domain/create/example.com", method="POST")
    assert FakePorkbunHandler.requests == []
