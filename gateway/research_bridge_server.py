"""Hermes research bridge server — local OAuth-backed search execution for Cogitator.

Railway Cogitator delegates its research web-search step to this always-on
host so the default research path runs on Cal's existing xAI Grok OAuth
subscription (``plugins/web/xai``) instead of a paid API. Standalone stdlib
``ThreadingHTTPServer`` (no new deps), binds 127.0.0.1 and is fronted by the
existing Tailscale Funnel.

Surface
-------
* ``GET /healthz`` → ``{"status": "ok"}`` — no auth, no secrets; used by
  Cogitator's masked runtime smoke as a credential-free liveness probe.
* ``POST /research_search`` ``{"query": str, "max_results": int}`` — bearer
  auth against ``HERMES_RESEARCH_BRIDGE_TOKEN`` (constant-time compare,
  fail-closed 503 when the token is unset) → ``XAIWebSearchProvider.search``
  → ``{"status": "ok", "provider": "xai", "urls": [...]}``.

Trust model
-----------
Only URL strings leave this server; Cogitator independently re-fetches each
through its SSRF guard, so a hostile result never becomes evidence there.
Provider failures return a sanitized, fixed error string — upstream error
bodies and internals never cross the bridge. OAuth tokens never leave this
host; the only shared secret is the bridge bearer token.

Run: ``python -m gateway.research_bridge_server`` (see
``gateway/systemd/hermes-research-bridge.service`` for 24/7 supervision).
"""

from __future__ import annotations

import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

logger = logging.getLogger(__name__)

TOKEN_ENV = "HERMES_RESEARCH_BRIDGE_TOKEN"
BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8799

MAX_QUERY_CHARS = 500
MAX_RESULTS_CAP = 10
DEFAULT_MAX_RESULTS = 5
MAX_BODY_BYTES = 16 * 1024

# Fixed sanitized error strings — provider internals/tokens never cross the bridge.
_ERR_NOT_CONFIGURED = "bridge token not configured"
_ERR_UNAUTHORIZED = "unauthorized"
_ERR_BAD_REQUEST = "invalid request"
_ERR_PROVIDER = "search provider failed"


def _load_token() -> str:
    """Bridge bearer token from ``~/.hermes/.env`` / environment."""
    from hermes_cli.config import get_env_value

    return str(get_env_value(TOKEN_ENV) or "").strip()


def _default_search(query: str, limit: int) -> dict:
    from plugins.web.xai.provider import XAIWebSearchProvider

    return XAIWebSearchProvider().search(query, limit=limit)


def _extract_urls(result: Any, cap: int) -> list[str]:
    """Dedup http(s) URLs out of a provider result, capped. Defensive against
    any shape — unexpected payloads yield no URLs rather than raising."""
    urls: list[str] = []
    seen: set[str] = set()
    data = result.get("data") if isinstance(result, dict) else None
    rows = data.get("web") if isinstance(data, dict) else None
    for row in rows if isinstance(rows, list) else []:
        url = str(row.get("url") or "").strip() if isinstance(row, dict) else ""
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            urls.append(url)
        if len(urls) >= cap:
            break
    return urls


def handle_research_search(
    body: bytes,
    auth_header: str,
    *,
    token: str,
    search: Callable[[str, int], dict] = _default_search,
) -> tuple[int, dict]:
    """Pure request handler: ``(status_code, response_dict)``.

    Fail-closed when no token is configured; constant-time bearer compare;
    bounded query/max_results; provider failures sanitized to a fixed string.
    """
    if not token:
        return 503, {"status": "error", "error": _ERR_NOT_CONFIGURED}
    presented = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
    if not presented or not hmac.compare_digest(presented, token):
        return 401, {"status": "error", "error": _ERR_UNAUTHORIZED}
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return 400, {"status": "error", "error": _ERR_BAD_REQUEST}
    if not isinstance(payload, dict):
        return 400, {"status": "error", "error": _ERR_BAD_REQUEST}
    query = str(payload.get("query") or "").strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        return 400, {"status": "error", "error": _ERR_BAD_REQUEST}
    try:
        max_results = int(payload.get("max_results", DEFAULT_MAX_RESULTS))
    except (TypeError, ValueError):
        max_results = DEFAULT_MAX_RESULTS
    max_results = max(1, min(max_results, MAX_RESULTS_CAP))

    try:
        result = search(query, max_results)
    except Exception:
        logger.warning("research_search provider raised", exc_info=True)
        return 502, {"status": "error", "error": _ERR_PROVIDER}
    if not (isinstance(result, dict) and result.get("success")):
        # Provider error strings can carry upstream response bodies — log
        # locally, never forward across the bridge.
        logger.warning("research_search provider failed: %s",
                       result.get("error") if isinstance(result, dict) else result)
        return 502, {"status": "error", "error": _ERR_PROVIDER}
    return 200, {"status": "ok", "provider": "xai",
                 "urls": _extract_urls(result, max_results)}


class _Handler(BaseHTTPRequestHandler):
    server_version = "HermesResearchBridge/1.0"

    def _send_json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"status": "error", "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/research_search":
            self._send_json(404, {"status": "error", "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"status": "error", "error": _ERR_BAD_REQUEST})
            return
        body = self.rfile.read(length)
        status, response = handle_research_search(
            body,
            self.headers.get("Authorization") or "",
            token=self.server.bridge_token,  # type: ignore[attr-defined]
            search=self.server.bridge_search,  # type: ignore[attr-defined]
        )
        self._send_json(status, response)

    def log_message(self, format: str, *args: Any) -> None:
        # Path + status only — never headers or body (the bearer token
        # travels in a header).
        logger.info("%s %s", self.address_string(), format % args)


def make_server(
    port: int = DEFAULT_PORT,
    *,
    token: str | None = None,
    search: Callable[[str, int], dict] = _default_search,
) -> ThreadingHTTPServer:
    """Build the bound server (port 0 for an ephemeral test port)."""
    server = ThreadingHTTPServer((BIND_HOST, port), _Handler)
    server.bridge_token = _load_token() if token is None else token  # type: ignore[attr-defined]
    server.bridge_search = search  # type: ignore[attr-defined]
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = make_server()
    if not server.bridge_token:  # type: ignore[attr-defined]
        # Still serve /healthz; /research_search fail-closes with 503.
        logger.warning("%s is not set — research_search will refuse all requests", TOKEN_ENV)
    logger.info("research bridge listening on %s:%d", BIND_HOST, server.server_address[1])
    server.serve_forever()


if __name__ == "__main__":
    main()
