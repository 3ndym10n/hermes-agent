from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import serve

import commerce_browser as browser
import virgil_gate_routes as gates
from commerce_jobs import CommerceJobStore
from virgil_mobile_server import create_app


PUBLIC_URL = "https://virgil.example.ts.net:8443"
HEADERS = {
    "host": "virgil.example.ts.net:8443",
    "tailscale-user-login": "cal@example.com",
}


def open_gate(
    tmp_path: Path,
    *,
    issued_at: datetime | None = None,
    entry_url: str | None = "https://example.com/provider/login",
    gate_type: str = "merchant_login",
):
    now = issued_at or datetime.now(timezone.utc)
    db = tmp_path / "commerce" / "commerce_jobs.db"
    store = CommerceJobStore(db)
    job = store.create_or_attach_job(
        requester="telegram:42", objective="Open a harmless browser gate", now=now
    )
    planning = store.transition(
        job["job_id"],
        "planning",
        expected_state="requested",
        expected_version=job["row_version"],
        actor="worker",
        reason_code="prepare_handoff",
        now=now,
    )
    gate = store.open_gate(
        job["job_id"],
        gate_type=gate_type,
        human_action="Complete the provider login, then request verification.",
        provider_truth_reference="provider-account-status-ref",
        opening_evidence={"entry_url": entry_url} if entry_url else {},
        now=now,
    )
    store.transition(
        job["job_id"],
        "awaiting_cal",
        expected_state="planning",
        expected_version=planning["row_version"],
        actor="worker",
        reason_code="human_action_required",
        gate_id=gate["gate_id"],
        now=now,
    )
    issued, token = store.issue_gate_handoff(gate["gate_id"], now=now)
    return store, job, issued, token


def client_for(tmp_path: Path, commerce_db: Path) -> TestClient:
    return TestClient(
        create_app(
            "cal@example.com",
            PUBLIC_URL,
            db_path=tmp_path / "attention.db",
            commerce_db_path=commerce_db,
            trusted_proxy_hosts=frozenset({"testclient"}),
        )
    )


def csrf(client: TestClient) -> str:
    return client.get("/api/session", headers=HEADERS).json()["csrf_token"]


def write_headers(client: TestClient) -> dict[str, str]:
    return {**HEADERS, "origin": PUBLIC_URL, "x-csrf-token": csrf(client)}


def test_handoff_page_uses_hashed_ttl_token_and_replay_is_gone(tmp_path):
    store, job, gate, token = open_gate(tmp_path)
    client = client_for(tmp_path, store.path)
    link = f"/gate/{gate['gate_id']}?t={token}"

    page = client.get(link, headers=HEADERS)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "Complete the provider login" in page.text
    assert 'id="browser-tabs"' in page.text
    assert token not in page.text

    with sqlite3.connect(store.path) as connection:
        token_hash = connection.execute(
            "SELECT handoff_token_hash FROM gates WHERE gate_id=?",
            (gate["gate_id"],),
        ).fetchone()[0]
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token.encode() not in store.path.read_bytes()

    done = client.post(
        f"/api/gate/{gate['gate_id']}/done?t={token}",
        headers=write_headers(client),
        json={},
    )
    assert done.status_code == 200
    assert done.json() == {"status": "open", "done_requested": True}
    requested = store.get_gate(gate["gate_id"])
    assert requested["status"] == "open"
    assert requested["done_requested_at"]

    store.complete_gate(
        gate["gate_id"],
        evidence={"provider_truth_verified": True, "probe": "passed"},
        actor="worker",
    )
    replay = client.get(link, headers=HEADERS)
    assert replay.status_code == 410
    assert store.get_job(job["job_id"])["current_state"] in replay.text


@pytest.mark.parametrize(
    ("entry_url", "gate_type"),
    [
        (None, "merchant_login"),
        ("file:///tmp/provider", "merchant_login"),
        ("https://example.com/provider/login", "facts"),
    ],
)
def test_viewer_rejects_missing_unsafe_and_no_viewer_gate_entries(
    tmp_path, entry_url, gate_type
):
    store, _, gate, token = open_gate(
        tmp_path, entry_url=entry_url, gate_type=gate_type
    )
    client = client_for(tmp_path, store.path)
    link = f"/gate/{gate['gate_id']}?t={token}"

    assert client.get(link, headers=HEADERS).status_code == 403
    done = client.post(
        f"/api/gate/{gate['gate_id']}/done?t={token}",
        headers=write_headers(client),
        json={},
    )
    assert done.status_code == 403
    assert store.get_gate(gate["gate_id"])["done_requested_at"] is None


def test_expired_and_rotated_links_fail_closed(tmp_path):
    past = datetime.now(timezone.utc) - timedelta(minutes=31)
    expired_store, _, expired_gate, expired_token = open_gate(
        tmp_path / "expired", issued_at=past
    )
    expired_client = client_for(tmp_path, expired_store.path)
    response = expired_client.get(
        f"/gate/{expired_gate['gate_id']}?t={expired_token}", headers=HEADERS
    )
    assert response.status_code == 410

    store, _, gate, token = open_gate(tmp_path / "renew")
    client = client_for(tmp_path, store.path)
    renewed = client.post(
        f"/api/gate/{gate['gate_id']}/renew?t={token}",
        headers=write_headers(client),
        json={},
    )
    assert renewed.status_code == 200
    replacement = renewed.json()["token"]
    assert replacement != token
    assert (
        client.get(f"/gate/{gate['gate_id']}?t={token}", headers=HEADERS).status_code
        == 403
    )
    assert (
        client.get(
            f"/gate/{gate['gate_id']}?t={replacement}", headers=HEADERS
        ).status_code
        == 200
    )


def test_http_and_websocket_enforce_same_identity_host_origin_and_csrf(tmp_path):
    store, _, gate, token = open_gate(tmp_path)
    client = client_for(tmp_path, store.path)
    link = f"/gate/{gate['gate_id']}?t={token}"

    assert client.get(link, headers={"host": HEADERS["host"]}).status_code == 401
    assert (
        client.get(
            link, headers={**HEADERS, "tailscale-user-login": "other@example.com"}
        ).status_code
        == 401
    )
    assert (
        client.get(link, headers={**HEADERS, "host": "evil.example"}).status_code == 400
    )
    assert (
        client.post(
            f"/api/gate/{gate['gate_id']}/done?t={token}",
            headers={**HEADERS, "origin": PUBLIC_URL, "x-csrf-token": "wrong"},
            json={},
        ).status_code
        == 403
    )
    malformed_headers = {**write_headers(client), "content-type": "application/json"}
    assert (
        client.post(
            f"/api/gate/{gate['gate_id']}/done?t={token}",
            headers=malformed_headers,
            content="{",
        ).status_code
        == 400
    )

    with pytest.raises(WebSocketDisconnect) as denied_identity:
        with client.websocket_connect(
            f"/api/gate/{gate['gate_id']}/stream?t={token}",
            headers={"host": HEADERS["host"], "origin": PUBLIC_URL},
        ):
            pass
    assert denied_identity.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as denied_origin:
        with client.websocket_connect(
            f"/api/gate/{gate['gate_id']}/stream?t={token}", headers=HEADERS
        ):
            pass
    assert denied_origin.value.code == 1008


def test_stream_protocol_rejects_unbounded_frames_and_unknown_input():
    frame = {
        "type": "frame",
        "data": "AA==",
        "metadata": {
            "deviceWidth": 320,
            "deviceHeight": 200,
            "pageScaleFactor": 1,
            "offsetTop": 0,
            "scrollOffsetX": 0,
            "scrollOffsetY": 0,
        },
    }
    assert gates._valid_frame(json.dumps(frame)) is not None
    frame["metadata"]["deviceWidth"] = 8193
    assert gates._valid_frame(json.dumps(frame)) is None
    assert gates._validated_input({"type": "unknown"}) is None
    with pytest.raises(ValueError, match="invalid_input_event"):
        gates._validated_input({
            "type": "input_mouse",
            "eventType": "mousePressed",
            "x": "12",
            "y": 4,
        })


@contextmanager
def fake_stream(received: queue.Queue):
    frame = json.dumps({
        "type": "frame",
        "data": "ZmFrZS1qcGVn",
        "metadata": {
            "deviceWidth": 320,
            "deviceHeight": 200,
            "pageScaleFactor": 1,
            "offsetTop": 0,
            "scrollOffsetX": 0,
            "scrollOffsetY": 0,
        },
    })
    status = json.dumps({
        "type": "status",
        "connected": True,
        "screencasting": True,
        "viewportWidth": 320,
        "viewportHeight": 200,
        "engine": "chrome",
        "recording": False,
    })
    tabs = json.dumps({
        "type": "tabs",
        "tabs": [
            {
                "type": "page",
                "tabId": "t1",
                "active": True,
                "title": "Provider login",
                "url": "https://example.com/private/path",
            },
            {
                "type": "page",
                "tabId": "t2",
                "active": False,
                "title": "Provider popup",
                "url": "https://example.com/private/popup",
            },
        ],
    })

    def handler(connection):
        for message in (status, tabs, frame):
            connection.send(message)
        try:
            for message in connection:
                received.put(json.loads(message))
        except Exception:
            pass

    server = serve(handler, "127.0.0.1", 0, compression=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.socket.getsockname()[1]
    finally:
        server.shutdown()
        thread.join(timeout=3)


def receive_type(websocket, expected: str) -> dict:
    for _ in range(5):
        message = websocket.receive_json()
        if message.get("type") == expected:
            return message
    raise AssertionError(f"did not receive {expected}")


def test_native_stream_is_ephemeral_and_only_one_client_controls(tmp_path, monkeypatch):
    store, _, gate, token = open_gate(tmp_path)
    client = client_for(tmp_path, store.path)
    received: queue.Queue = queue.Queue()
    switched = []
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    with fake_stream(received) as port:
        monkeypatch.setattr(
            gates, "_stream_url", lambda _session: f"ws://127.0.0.1:{port}"
        )
        monkeypatch.setattr(
            gates, "_browser_origin", lambda _session: "https://example.com"
        )
        monkeypatch.setattr(
            gates,
            "_switch_tab",
            lambda session, tab_id: switched.append((session, tab_id)),
        )
        ws_headers = {**HEADERS, "origin": PUBLIC_URL}
        url = f"/api/gate/{gate['gate_id']}/stream?t={token}"
        with client.websocket_connect(url, headers=ws_headers) as first:
            assert receive_type(first, "session")["controller"] is True
            status = receive_type(first, "status")
            assert status["viewportWidth"] == 320
            assert "engine" not in status
            tabs = receive_type(first, "tabs")
            assert [tab["tabId"] for tab in tabs["tabs"]] == ["t1", "t2"]
            assert "url" not in str(tabs)
            assert receive_type(first, "frame")["data"] == "ZmFrZS1qcGVn"
            with client.websocket_connect(url, headers=ws_headers) as second:
                assert receive_type(second, "session")["controller"] is False
                assert receive_type(second, "status")["connected"] is True
                receive_type(second, "tabs")
                receive_type(second, "frame")
                second.send_json({
                    "type": "input_mouse",
                    "eventType": "mousePressed",
                    "x": 12,
                    "y": 14,
                    "button": "left",
                    "clickCount": 1,
                })
                assert receive_type(second, "control")["controller"] is False
                assert received.empty()
                second.send_json({"type": "take_control"})
                assert receive_type(second, "control")["controller"] is True
                second.send_json({"type": "select_tab", "tab_id": "t2"})
                tab_ack = receive_type(second, "tab_ack")
                assert tab_ack["tab_id"] == "t2"
                assert tab_ack["origin"] == "https://example.com"
                assert switched == [(gate["browser_session"], "t2")]
                second.send_json({
                    "type": "input_keyboard",
                    "eventType": "keyDown",
                    "key": "Enter",
                    "code": "Enter",
                })
                assert received.get(timeout=3)["type"] == "input_keyboard"

                store.request_gate_done(gate["gate_id"], token, actor="cal:gate_viewer")
                store.complete_gate(
                    gate["gate_id"],
                    evidence={"provider_truth_verified": True},
                    actor="worker",
                )
    assert list(evidence.iterdir()) == []


def test_sensitive_text_uses_batch_stdin_only(monkeypatch):
    secret = "not-for-process-arguments"
    captured = {}

    def fake_run(arguments, **kwargs):
        captured.update(arguments=arguments, kwargs=kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(browser, "browser_binary", lambda: "/safe/agent-browser")
    monkeypatch.setattr(
        browser, "browser_env", lambda: {"AGENT_BROWSER_SOCKET_DIR": "/safe"}
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    gates._insert_text("commerce_cj_12345678_1234_1234_1234_123456789abc", secret)

    assert all(secret not in argument for argument in captured["arguments"])
    assert json.loads(captured["kwargs"]["input"]) == [
        ["keyboard", "inserttext", secret]
    ]
    assert captured["kwargs"]["stdout"] is subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is subprocess.DEVNULL


def test_gate_assets_are_not_service_worker_cached_and_unit_is_confined():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "virgil_mobile" / "sw.js").read_text()
    assets = worker.split("const ASSETS =", 1)[1].split(";", 1)[0]
    assert "/gate/" not in assets
    assert "/gate.js" not in assets
    assert "/gate.css" not in assets

    unit = (
        root
        / "packaging"
        / "virgil-mobile"
        / "virgil-mobile.service.d"
        / "commerce.conf"
    ).read_text()
    assert "ReadWritePaths=/home/v0id/.hermes/commerce" in unit
    assert "AGENT_BROWSER_SOCKET_DIR=/home/v0id/.hermes/commerce/ab" in unit
