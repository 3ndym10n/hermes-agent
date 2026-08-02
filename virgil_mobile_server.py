"""Loopback-only Virgil Mobile PWA and same-origin Attention API."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import secrets
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from hermes_attention import (
    AttentionError,
    attention_brief,
    available_projects,
    get_attention,
    list_activity,
    list_attention,
    list_source_statuses,
    transition_attention,
    validate_public_url,
)
from hermes_cli.config import cfg_get, load_config
from virgil_gate_routes import install_gate_routes


STATIC_DIR = Path(__file__).parent / "virgil_mobile"
MAX_BODY_BYTES = 16 * 1024
READ_LIMIT_PER_MINUTE = 120
WRITE_LIMIT_PER_MINUTE = 30
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_log = logging.getLogger("virgil_mobile")


def _config() -> tuple[str, str]:
    config = load_config()
    user = str(
        cfg_get(config, "attention", "authorized_tailscale_user", default="") or ""
    ).strip()
    public_url = str(
        cfg_get(config, "attention", "public_url", default="") or ""
    ).strip()
    if not user or len(user) > 254 or any(char.isspace() for char in user):
        raise AttentionError("attention_authorized_user_missing", 503)
    return user.casefold(), validate_public_url(public_url)


def _audit(action: str, item_id: str = "", outcome: str = "ok") -> None:
    item_ref = hashlib.sha256(item_id.encode()).hexdigest()[:12] if item_id else "-"
    _log.info("attention_audit action=%s item=%s outcome=%s", action, item_ref, outcome)


def _gmail_watcher_state() -> str:
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "is-active",
                "--quiet",
                "linxio-incoming-autodraft.timer",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "paused"
    return "active" if result.returncode == 0 else "paused"


def _sync_range(sources: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    def parsed(value: Any) -> float | None:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return None

    successful = [
        (timestamp, source["last_successful_sync_at"])
        for source in sources
        if (timestamp := parsed(source.get("last_successful_sync_at"))) is not None
    ]
    scheduled = [
        (timestamp, source["next_scheduled_sync_at"])
        for source in sources
        if (timestamp := parsed(source.get("next_scheduled_sync_at"))) is not None
        and bool(source.get("enabled", True))
        and not bool(source.get("paused", False))
        and timestamp >= time.time()
    ]
    return (
        max(successful, default=(0, None))[1],
        min(scheduled, default=(0, None))[1],
    )


def create_app(
    authorized_user: str,
    public_url: str,
    *,
    db_path: Path | str | None = None,
    commerce_db_path: Path | str | None = None,
    trusted_proxy_hosts: frozenset[str] = frozenset({"127.0.0.1", "::1"}),
) -> FastAPI:
    """Build the app with an explicit identity and trusted local proxy boundary."""

    authorized = authorized_user.strip().casefold()
    if (
        not authorized
        or len(authorized) > 254
        or any(char.isspace() for char in authorized)
    ):
        raise AttentionError("attention_authorized_user_missing", 503)
    public_url = validate_public_url(public_url)
    expected_host = urlparse(public_url).netloc.casefold()
    csrf_token = secrets.token_urlsafe(32)
    requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)
    app = FastAPI(
        title="Virgil",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def secure_headers(response, path: str):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        if path.startswith(("/api/", "/gate/")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    def error(code: str, status: int, path: str, headers=None):
        return secure_headers(
            JSONResponse({"error": code}, status_code=status, headers=headers),
            path,
        )

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        path = request.url.path
        peer = request.client.host if request.client else ""
        host = request.headers.get("host", "").casefold()
        host_name = urlparse(f"//{host}").hostname or ""
        local_health = path == "/healthz" and host_name in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if peer not in trusted_proxy_hosts:
            return error("untrusted_proxy", 403, path)
        if not local_health and host != expected_host:
            return error("invalid_host", 400, path)
        if not local_health:
            login = request.headers.get("tailscale-user-login", "").strip().casefold()
            if not login or not hmac.compare_digest(login, authorized):
                _audit("auth", outcome="denied")
                return error("unauthorized", 401, path)

        write = request.method in _WRITE_METHODS
        if write:
            content_length = request.headers.get("content-length", "")
            try:
                if content_length and int(content_length) > MAX_BODY_BYTES:
                    return error("request_too_large", 413, path)
            except ValueError:
                return error("invalid_content_length", 400, path)
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip()
                != "application/json"
            ):
                return error("unsupported_media_type", 415, path)
            body = await request.body()
            if len(body) > MAX_BODY_BYTES:
                return error("request_too_large", 413, path)
            if (
                request.headers.get("origin", "").rstrip("/").casefold()
                != public_url.casefold()
            ):
                return error("invalid_origin", 403, path)
            supplied = request.headers.get("x-csrf-token", "")
            if not supplied or not hmac.compare_digest(supplied, csrf_token):
                return error("invalid_csrf_token", 403, path)

        now = time.monotonic()
        bucket_key = (authorized, "write" if write else "read")
        bucket = requests[bucket_key]
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        limit = WRITE_LIMIT_PER_MINUTE if write else READ_LIMIT_PER_MINUTE
        if len(bucket) >= limit:
            return error("rate_limited", 429, path, {"Retry-After": "60"})
        bucket.append(now)

        try:
            response = await call_next(request)
        except AttentionError as exc:
            response = JSONResponse({"error": exc.code}, status_code=exc.status)
        return secure_headers(response, path)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/api/session")
    def session():
        source_statuses = list_source_statuses(db_path=db_path)
        last_source_sync, next_source_sync = _sync_range(source_statuses)
        latest = next(
            (
                event
                for event in list_activity(limit=200, db_path=db_path)
                if event["actor"] != "cal"
            ),
            None,
        )
        return {
            "authenticated": True,
            "app": "Virgil",
            "csrf_token": csrf_token,
            "connection": {
                "virgil": "connected",
                "gmail_watcher": _gmail_watcher_state(),
                "last_successful_queue_ingestion_at": (
                    latest["timestamp"] if latest else None
                ),
                "last_successful_source_sync_at": last_source_sync,
                "next_scheduled_source_sync_at": next_source_sync,
                "sources": [
                    {
                        key: source.get(key)
                        for key in (
                            "source",
                            "status",
                            "message",
                            "last_successful_sync_at",
                        )
                    }
                    for source in source_statuses
                ],
            },
            "projects": available_projects(db_path=db_path),
            "brief": attention_brief(db_path=db_path),
        }

    @app.get("/api/items")
    def items(view: str = "today", project: str = "all", limit: int = 200):
        return {
            "items": list_attention(
                view=view, project=project, limit=limit, db_path=db_path
            )
        }

    @app.get("/api/items/{item_id}")
    def item(item_id: str):
        record = get_attention(item_id, db_path=db_path)
        events = record.pop("activity")
        return {"item": record, "events": events}

    @app.get("/api/activity")
    def activity(limit: int = 100, project: str = "all"):
        return {"events": list_activity(limit=limit, project=project, db_path=db_path)}

    @app.post("/api/items/{item_id}/action")
    async def act(item_id: str, request: Request):
        try:
            payload: Any = await request.json()
        except Exception as exc:
            raise AttentionError("invalid_payload") from exc
        if not isinstance(payload, dict) or set(payload) - {
            "action",
            "expected_row_version",
            "deferred_until",
        }:
            raise AttentionError("invalid_payload_keys")
        if "action" not in payload or "expected_row_version" not in payload:
            raise AttentionError("invalid_payload_keys")
        result = transition_attention(
            item_id,
            payload["action"],
            expected_row_version=payload["expected_row_version"],
            deferred_until=payload.get("deferred_until"),
            db_path=db_path,
        )
        _audit(str(payload["action"]), item_id)
        return {"item": result}

    install_gate_routes(
        app,
        commerce_db_path=commerce_db_path,
        authorized_user=authorized,
        public_url=public_url,
        expected_host=expected_host,
        trusted_proxy_hosts=trusted_proxy_hosts,
        audit=lambda action, gate_id, outcome: _audit(action, gate_id, outcome),
    )

    assets = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/app.css": ("app.css", "text/css; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
        "/gate.css": ("gate.css", "text/css; charset=utf-8"),
        "/gate.js": ("gate.js", "text/javascript; charset=utf-8"),
        "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
        "/icons/virgil-192.png": ("icons/virgil-192.png", "image/png"),
        "/icons/virgil-512.png": ("icons/virgil-512.png", "image/png"),
    }

    @app.get("/{path:path}")
    def static(path: str, request: Request):
        route = f"/{path}"
        if route.startswith("/item/"):
            route = "/"
        asset = assets.get(route)
        if asset is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        filename, media_type = asset
        response = FileResponse(STATIC_DIR / filename, media_type=media_type)
        if route == "/sw.js":
            response.headers["Service-Worker-Allowed"] = "/"
            response.headers["Cache-Control"] = "no-cache"
        elif route in {"/", "/index.html", "/manifest.webmanifest"}:
            response.headers["Cache-Control"] = "no-cache"
        else:
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the private Virgil Mobile PWA")
    parser.add_argument("--port", type=int, default=8788)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1024 <= args.port <= 65535:
        raise SystemExit("port must be between 1024 and 65535")
    authorized_user, public_url = _config()
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    uvicorn.run(
        create_app(authorized_user, public_url),
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
