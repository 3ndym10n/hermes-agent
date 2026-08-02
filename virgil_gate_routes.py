"""Private human-gate viewer backed by agent-browser's native pair stream."""

from __future__ import annotations

import asyncio
import html
import hmac
import json
import math
import re
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.asyncio.client import connect as stream_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from commerce_browser import (
    BrowserLifecycleError,
    browser_origin as _browser_origin,
    insert_text as _insert_text,
    stream_url as _stream_url,
    switch_tab as _switch_tab,
    validate_browser_binding,
    validate_entry_url,
    validate_tab_id,
)
from commerce_jobs import (
    CommerceJobError,
    CommerceJobStore,
    CommerceNotFoundError,
)


_MAX_CLIENT_MESSAGE = 16 * 1024
_MAX_FRAME_MESSAGE = 6 * 1024 * 1024
_MAX_NATIVE_NOTICE = 64 * 1024
_NO_VIEWER_GATES = frozenset({"action_approval", "facts"})
_GONE_CODES = frozenset({
    "gate_expired",
    "gate_not_active_handoff",
    "gate_not_open",
    "gate_session_mismatch",
    "handoff_expired",
})


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_input_event")
    result = float(value)
    if not math.isfinite(result) or not -100_000 <= result <= 100_000:
        raise ValueError("invalid_input_event")
    return result


def _short_text(value: Any, *, limit: int = 80) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid_input_event")
    return value


def _validated_input(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ValueError("invalid_input_event")
    kind = message["type"]
    if kind == "input_mouse":
        event_type = _short_text(message.get("eventType"))
        if event_type not in {
            "mousePressed",
            "mouseReleased",
            "mouseMoved",
            "mouseWheel",
        }:
            raise ValueError("invalid_input_event")
        event = {
            "type": kind,
            "eventType": event_type,
            "x": _number(message.get("x")),
            "y": _number(message.get("y")),
        }
        if event_type in {"mousePressed", "mouseReleased"}:
            button = _short_text(message.get("button"))
            if button not in {"left", "middle", "right", "back", "forward"}:
                raise ValueError("invalid_input_event")
            count = message.get("clickCount", 1)
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or not 1 <= count <= 3
            ):
                raise ValueError("invalid_input_event")
            event.update(button=button, clickCount=count)
        if event_type == "mouseWheel":
            event.update(
                deltaX=_number(message.get("deltaX", 0)),
                deltaY=_number(message.get("deltaY", 0)),
            )
        return event
    if kind == "input_keyboard":
        event_type = _short_text(message.get("eventType"))
        if event_type not in {"keyDown", "keyUp"}:
            raise ValueError("invalid_input_event")
        key = _short_text(message.get("key"))
        code = _short_text(message.get("code"))
        if not key or not code:
            raise ValueError("invalid_input_event")
        return {"type": kind, "eventType": event_type, "key": key, "code": code}
    if kind == "input_touch":
        event_type = _short_text(message.get("eventType"))
        if event_type not in {"touchStart", "touchMove", "touchEnd", "touchCancel"}:
            raise ValueError("invalid_input_event")
        points = message.get("touchPoints")
        if not isinstance(points, list) or len(points) > 10:
            raise ValueError("invalid_input_event")
        clean = []
        for point in points:
            if not isinstance(point, dict):
                raise ValueError("invalid_input_event")
            clean.append({"x": _number(point.get("x")), "y": _number(point.get("y"))})
        if event_type in {"touchStart", "touchMove"} and not clean:
            raise ValueError("invalid_input_event")
        return {"type": kind, "eventType": event_type, "touchPoints": clean}
    return None


def _valid_frame(raw: Any) -> str | None:
    if not isinstance(raw, str) or len(raw) > _MAX_FRAME_MESSAGE:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("type") != "frame":
        return None
    data = payload.get("data")
    metadata = payload.get("metadata")
    if not isinstance(data, str) or not data or len(data) > _MAX_FRAME_MESSAGE - 1024:
        return None
    if not isinstance(metadata, dict):
        return None
    for field, lower, upper in (
        ("deviceWidth", 1, 8192),
        ("deviceHeight", 1, 8192),
        ("pageScaleFactor", 0.01, 100),
        ("offsetTop", -100_000, 100_000),
        ("scrollOffsetX", -10_000_000, 10_000_000),
        ("scrollOffsetY", -10_000_000, 10_000_000),
    ):
        value = metadata.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not lower <= value <= upper
        ):
            return None
    return raw


def _valid_native_notice(raw: Any) -> str | None:
    """Return a bounded status/tab message with URLs and extra fields removed."""

    if not isinstance(raw, str) or len(raw) > _MAX_NATIVE_NOTICE:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("type") == "status":
        connected = payload.get("connected")
        screencasting = payload.get("screencasting")
        width = payload.get("viewportWidth")
        height = payload.get("viewportHeight")
        if (
            not isinstance(connected, bool)
            or not isinstance(screencasting, bool)
            or isinstance(width, bool)
            or not isinstance(width, int)
            or not 1 <= width <= 8_192
            or isinstance(height, bool)
            or not isinstance(height, int)
            or not 1 <= height <= 8_192
        ):
            return None
        clean: dict[str, Any] = {
            "type": "status",
            "connected": connected,
            "screencasting": screencasting,
            "viewportWidth": width,
            "viewportHeight": height,
        }
        return json.dumps(clean, separators=(",", ":"))
    if payload.get("type") != "tabs" or not isinstance(payload.get("tabs"), list):
        return None
    tabs = payload["tabs"]
    if len(tabs) > 32:
        return None
    clean_tabs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tab in tabs:
        if not isinstance(tab, dict) or tab.get("type") != "page":
            continue
        try:
            tab_id = validate_tab_id(tab.get("tabId"))
        except BrowserLifecycleError:
            return None
        active = tab.get("active")
        title = tab.get("title", "")
        if not isinstance(active, bool) or not isinstance(title, str):
            return None
        if tab_id in seen:
            return None
        seen.add(tab_id)
        safe_title = " ".join(
            "".join(char if ord(char) >= 32 else " " for char in title).split()
        )[:120]
        clean_tabs.append({
            "tabId": tab_id,
            "active": active,
            "title": safe_title or "Browser tab",
        })
    return json.dumps({"type": "tabs", "tabs": clean_tabs}, separators=(",", ":"))


def _gate_entry_url(gate: dict[str, Any]) -> str:
    if gate.get("gate_type") in _NO_VIEWER_GATES:
        raise BrowserLifecycleError("gate_viewer_not_required")
    evidence = gate.get("opening_evidence")
    entry_url = evidence.get("entry_url") if isinstance(evidence, dict) else None
    return validate_entry_url(entry_url)


class _Controllers:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_gate: dict[str, WebSocket] = {}

    async def claim(self, gate_id: str, websocket: WebSocket, *, force: bool = False):
        async with self._lock:
            current = self._by_gate.get(gate_id)
            if current is None or current is websocket or force:
                self._by_gate[gate_id] = websocket
                return True, current if current is not websocket else None
            return False, None

    async def owns(self, gate_id: str, websocket: WebSocket) -> bool:
        async with self._lock:
            return self._by_gate.get(gate_id) is websocket

    async def release(self, gate_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            if self._by_gate.get(gate_id) is websocket:
                self._by_gate.pop(gate_id, None)


def _error_status(exc: CommerceJobError) -> int:
    if isinstance(exc, CommerceNotFoundError):
        return 404
    return 410 if exc.code in _GONE_CODES else 403


def _status_page(store: CommerceJobStore, gate_id: str, status: int) -> HTMLResponse:
    current = "unavailable"
    if status == 410:
        with suppress(CommerceJobError):
            gate = store.get_gate(gate_id)
            current = str(store.get_job(gate["job_id"])["current_state"])
    body = (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<link rel='stylesheet' href='/gate.css'><title>Gate unavailable</title>"
        "<main class='gate-error'><h1>This handoff link is no longer available.</h1>"
        f"<p>Current job status: {html.escape(current)}</p>"
        "<p>Return to Telegram for the current private link.</p></main></html>"
    )
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _viewer_page(gate: dict[str, Any]) -> HTMLResponse:
    action = html.escape(str(gate["human_action"]))
    gate_id = html.escape(str(gate["gate_id"]), quote=True)
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="robots" content="noindex,nofollow">
  <title>Virgil secure handoff</title>
  <link rel="stylesheet" href="/gate.css">
  <script src="/gate.js" defer></script>
</head>
<body data-gate-id="{gate_id}">
  <header><strong>Virgil secure handoff</strong><span id="browser-origin">Connecting…</span></header>
  <main>
    <section class="task" aria-labelledby="gate-action-title">
      <h1 id="gate-action-title">Action needed</h1><p>{action}</p>
      <p class="hint">Virgil renders this private browser session but never stores its frames or what you type.</p>
    </section>
    <div class="browser-toolbar">
      <label for="browser-tabs">Browser tab</label>
      <select id="browser-tabs" disabled><option>Waiting for tabs…</option></select>
    </div>
    <section class="viewer" aria-label="Provider browser session">
      <canvas id="browser-frame" tabindex="0" aria-label="Interactive provider page"></canvas>
      <div id="control-banner" role="status">Connecting to the browser…</div>
    </section>
    <form id="text-form" autocomplete="off">
      <label for="secure-text">Type into the focused browser field</label>
      <div><input id="secure-text" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="8192"><button type="submit">Send securely</button></div>
    </form>
    <div class="actions">
      <button id="take-control" type="button" hidden>Take control</button>
      <button id="renew" type="button">Keep open 30 minutes</button>
      <button id="done" class="primary" type="button">DONE — verify</button>
    </div>
    <p id="status" role="status">Loading secure session…</p>
    <p class="hint">DONE requests a provider check. Only that check—not this button—can complete the gate.</p>
  </main>
</body>
</html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


def install_gate_routes(
    app: FastAPI,
    *,
    commerce_db_path: Path | str | None,
    authorized_user: str,
    public_url: str,
    expected_host: str,
    trusted_proxy_hosts: frozenset[str],
    audit: Callable[[str, str, str], None],
) -> None:
    """Add identity-bound gate routes without widening Virgil Mobile's surface."""

    store = CommerceJobStore(commerce_db_path)
    controllers = _Controllers()

    @app.get("/gate/{gate_id}")
    async def gate_page(gate_id: str, request: Request):
        token = request.query_params.get("t", "")
        try:
            gate = await asyncio.to_thread(store.authorize_gate_handoff, gate_id, token)
            _gate_entry_url(gate)
        except CommerceJobError as exc:
            audit("gate_open", gate_id, exc.code)
            return _status_page(store, gate_id, _error_status(exc))
        except BrowserLifecycleError as exc:
            audit("gate_open", gate_id, str(exc))
            return _status_page(store, gate_id, 403)
        audit("gate_open", gate_id, "ok")
        return _viewer_page(gate)

    @app.post("/api/gate/{gate_id}/renew")
    async def renew_gate(gate_id: str, request: Request):
        token = request.query_params.get("t", "")
        try:
            payload = await request.json()
            if payload != {}:
                return JSONResponse({"error": "invalid_payload"}, status_code=400)
            current = await asyncio.to_thread(
                store.authorize_gate_handoff, gate_id, token
            )
            _gate_entry_url(current)
            gate, renewed = await asyncio.to_thread(
                store.renew_gate_handoff, gate_id, token, actor="cal:gate_viewer"
            )
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid_payload"}, status_code=400)
        except CommerceJobError as exc:
            return JSONResponse({"error": exc.code}, status_code=_error_status(exc))
        except BrowserLifecycleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        audit("gate_renew", gate_id, "ok")
        return {"token": renewed, "expires_at": gate["handoff_expires_at"]}

    @app.post("/api/gate/{gate_id}/done")
    async def done_gate(gate_id: str, request: Request):
        token = request.query_params.get("t", "")
        try:
            payload = await request.json()
            if payload != {}:
                return JSONResponse({"error": "invalid_payload"}, status_code=400)
            current = await asyncio.to_thread(
                store.authorize_gate_handoff, gate_id, token
            )
            _gate_entry_url(current)
            gate = await asyncio.to_thread(
                store.request_gate_done, gate_id, token, actor="cal:gate_viewer"
            )
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid_payload"}, status_code=400)
        except CommerceJobError as exc:
            return JSONResponse({"error": exc.code}, status_code=_error_status(exc))
        except BrowserLifecycleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        audit("gate_done", gate_id, "requested")
        return {"status": gate["status"], "done_requested": True}

    async def deny_socket(websocket: WebSocket, gate_id: str, reason: str) -> None:
        audit("gate_stream", gate_id, reason)
        with suppress(RuntimeError):
            await websocket.close(code=1008, reason=reason)

    async def browser_to_viewer(upstream, websocket: WebSocket) -> None:
        try:
            async for raw in upstream:
                outgoing = _valid_frame(raw) or _valid_native_notice(raw)
                if outgoing is not None:
                    await websocket.send_text(outgoing)
        except (ConnectionClosed, RuntimeError, WebSocketDisconnect):
            return

    async def viewer_to_browser(
        upstream, websocket: WebSocket, gate_id: str, session: str
    ) -> None:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            if len(raw) > _MAX_CLIENT_MESSAGE:
                await websocket.close(code=1009, reason="message_too_large")
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "code": "invalid_input_event",
                })
                continue
            if isinstance(message, dict) and message.get("type") == "take_control":
                claimed, previous = await controllers.claim(
                    gate_id, websocket, force=True
                )
                if previous is not None:
                    with suppress(RuntimeError):
                        await previous.close(code=4001, reason="control_transferred")
                await websocket.send_json({"type": "control", "controller": claimed})
                continue
            if not await controllers.owns(gate_id, websocket):
                await websocket.send_json({
                    "type": "control",
                    "controller": False,
                    "message": "session controlled elsewhere",
                })
                continue
            if isinstance(message, dict) and message.get("type") == "select_tab":
                if set(message) != {"type", "tab_id"}:
                    await websocket.send_json({
                        "type": "error",
                        "code": "invalid_tab_selection",
                    })
                    continue
                try:
                    tab_id = validate_tab_id(message.get("tab_id"))
                    await asyncio.to_thread(_switch_tab, session, tab_id)
                    selected_origin = await asyncio.to_thread(_browser_origin, session)
                except RuntimeError:
                    await websocket.send_json({
                        "type": "error",
                        "code": "invalid_tab_selection",
                    })
                else:
                    await websocket.send_json({
                        "type": "tab_ack",
                        "tab_id": tab_id,
                        "origin": selected_origin,
                    })
                continue
            if isinstance(message, dict) and message.get("type") == "input_text":
                if set(message) != {"type", "text"}:
                    await websocket.send_json({
                        "type": "error",
                        "code": "invalid_input_event",
                    })
                    continue
                try:
                    await asyncio.to_thread(_insert_text, session, message.get("text"))
                except (RuntimeError, ValueError):
                    await websocket.send_json({
                        "type": "error",
                        "code": "text_input_failed",
                    })
                else:
                    await websocket.send_json({"type": "input_ack"})
                continue
            try:
                event = _validated_input(message)
            except ValueError:
                event = None
            if event is None:
                await websocket.send_json({
                    "type": "error",
                    "code": "invalid_input_event",
                })
                continue
            try:
                await upstream.send(json.dumps(event, separators=(",", ":")))
            except ConnectionClosed:
                return

    async def watch_gate(websocket: WebSocket, gate_id: str, token: str) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                await asyncio.to_thread(store.authorize_gate_handoff, gate_id, token)
            except CommerceJobError:
                with suppress(RuntimeError, WebSocketDisconnect):
                    await websocket.send_json({"type": "gate_closed"})
                    await websocket.close(code=4004, reason="gate_closed")
                return

    @app.websocket("/api/gate/{gate_id}/stream")
    async def gate_stream(websocket: WebSocket, gate_id: str):
        peer = websocket.client.host if websocket.client else ""
        host = websocket.headers.get("host", "").casefold()
        login = websocket.headers.get("tailscale-user-login", "").strip().casefold()
        origin = websocket.headers.get("origin", "").rstrip("/").casefold()
        if peer not in trusted_proxy_hosts:
            await deny_socket(websocket, gate_id, "untrusted_proxy")
            return
        if host != expected_host:
            await deny_socket(websocket, gate_id, "invalid_host")
            return
        if not login or not hmac.compare_digest(login, authorized_user):
            await deny_socket(websocket, gate_id, "unauthorized")
            return
        if origin != public_url.casefold():
            await deny_socket(websocket, gate_id, "invalid_origin")
            return
        token = websocket.query_params.get("t", "")
        try:
            gate = await asyncio.to_thread(store.authorize_gate_handoff, gate_id, token)
            job = await asyncio.to_thread(store.get_job, gate["job_id"])
            _gate_entry_url(gate)
        except CommerceJobError as exc:
            await deny_socket(websocket, gate_id, exc.code)
            return
        except BrowserLifecycleError as exc:
            await deny_socket(websocket, gate_id, str(exc))
            return
        session = str(gate["browser_session"])
        try:
            validate_browser_binding(str(job["job_id"]), session)
        except BrowserLifecycleError:
            await deny_socket(websocket, gate_id, "gate_session_mismatch")
            return
        if session != job["browser_session"]:
            await deny_socket(websocket, gate_id, "gate_session_mismatch")
            return

        await websocket.accept()
        controller, _ = await controllers.claim(gate_id, websocket)
        try:
            stream_url = await asyncio.to_thread(_stream_url, session)
            browser_origin = await asyncio.to_thread(_browser_origin, session)
            await websocket.send_json({
                "type": "session",
                "controller": controller,
                "origin": browser_origin,
            })
            async with stream_connect(
                stream_url,
                open_timeout=5,
                close_timeout=2,
                max_size=_MAX_FRAME_MESSAGE,
                compression=None,
            ) as upstream:
                tasks = {
                    asyncio.create_task(browser_to_viewer(upstream, websocket)),
                    asyncio.create_task(
                        viewer_to_browser(upstream, websocket, gate_id, session)
                    ),
                    asyncio.create_task(watch_gate(websocket, gate_id, token)),
                }
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*done, *pending, return_exceptions=True)
        except (OSError, RuntimeError, WebSocketDisconnect, WebSocketException):
            if websocket.application_state == WebSocketState.CONNECTED:
                with suppress(RuntimeError, WebSocketDisconnect):
                    await websocket.send_json({
                        "type": "error",
                        "code": "browser_session_unavailable",
                    })
                    await websocket.close(
                        code=1011, reason="browser_session_unavailable"
                    )
        finally:
            await controllers.release(gate_id, websocket)
