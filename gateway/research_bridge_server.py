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
* ``POST /research_gather`` ``{"query": str, "max_sources": int}`` — same
  auth; runs GPT Researcher *discovery only* in its isolated pinned venv
  (``~/.hermes/gptr-venv``, see ``gateway/gptr-requirements.lock``) as a
  bounded one-shot subprocess → ``{"status": "ok", "provider": "gptr",
  "sources": [{"url", "title", "snippet"}], "count", "latency_s",
  "cost_usd_estimate"}``. Gathering material ONLY: verdicts, promotion
  recommendations, ledgers, and agent instructions never cross this
  endpoint (Cogitator #1012 Phase 1 doctrine). Default-off consumer;
  Cogitator re-fetches every URL through its own SSRF guard and never
  counts snippets alone as evidence.
* ``POST /research_note`` durably saves one completed research note through
  Hermes's existing local-copy path before Cogitator reports completion. It
  uses the same bearer authentication and accepts no arbitrary target path.

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
MAX_NOTE_BODY_BYTES = 256 * 1024

# /research_gather bounds — one GPT Researcher discovery subprocess per request.
GATHER_MAX_SOURCES_CAP = 20
GATHER_DEFAULT_SOURCES = 10
GATHER_TIMEOUT_SECONDS = 150
GATHER_SNIPPET_CHARS = 300
GPTR_PYTHON_ENV = "HERMES_GPTR_PYTHON"  # override for tests; default pinned venv
_GPTR_PYTHON_DEFAULT = "~/.hermes/gptr-venv/bin/python"

# Fixed sanitized error strings — provider internals/tokens never cross the bridge.
_ERR_NOT_CONFIGURED = "bridge token not configured"
_ERR_UNAUTHORIZED = "unauthorized"
_ERR_BAD_REQUEST = "invalid request"
_ERR_PROVIDER = "search provider failed"
_ERR_STORE = "local note store failed"
_ERR_GATHER = "gather provider failed"


def _load_token() -> str:
    """Bridge bearer token from ``~/.hermes/.env`` / environment."""
    from hermes_cli.config import get_env_value

    return str(get_env_value(TOKEN_ENV) or "").strip()


def _load_local_dir() -> str:
    """Resolve the same durable intake directory used by the gateway."""
    try:
        from gateway.run import _load_gateway_config
        from hermes_cli.config import cfg_get

        config = _load_gateway_config()
        return str(
            cfg_get(config, "intake", "local_dir", default="~/cogitator-brain") or ""
        ).strip()
    except Exception:
        return ""


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


def _default_gather(query: str, max_sources: int) -> dict:
    """Run the GPTR discovery worker as a bounded one-shot subprocess in its
    isolated pinned venv. cwd is a throwaway scratch dir so the worker never
    touches either repo or ~/cogitator-brain; the OpenRouter key travels via
    the subprocess environment only (never argv, never logs)."""
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    from hermes_cli.config import get_env_value

    python = os.path.expanduser(
        str(get_env_value(GPTR_PYTHON_ENV) or "").strip() or _GPTR_PYTHON_DEFAULT)
    worker = str(Path(__file__).resolve().parent / "gptr_gather_worker.py")
    if not Path(python).exists():
        return {"status": "error", "error": "gather venv unavailable"}
    with tempfile.TemporaryDirectory(prefix="gptr-scratch-") as scratch:
        env = {
            "HOME": scratch,
            "XDG_CACHE_HOME": os.path.join(scratch, "cache"),
            "XDG_CONFIG_HOME": os.path.join(scratch, "config"),
            "XDG_DATA_HOME": os.path.join(scratch, "data"),
            "PATH": "/usr/bin:/bin",
            "OPENROUTER_API_KEY": str(get_env_value("OPENROUTER_API_KEY") or ""),
        }
        proc = subprocess.run(
            [python, worker],
            input=json.dumps({"query": query, "max_sources": max_sources}),
            capture_output=True, text=True, timeout=GATHER_TIMEOUT_SECONDS,
            cwd=scratch, env=env,
        )
    if proc.stderr:
        # Worker diagnostics stay in the local log only.
        logger.info("gptr worker stderr (%d bytes)", len(proc.stderr))
    return json.loads(proc.stdout or "{}")


def handle_research_gather(
    body: bytes,
    auth_header: str,
    *,
    token: str,
    gather: Callable[[str, int], dict] = _default_gather,
) -> tuple[int, dict]:
    """Pure request handler for GPTR source discovery: ``(status, response)``.

    Same fail-closed auth/bounds discipline as ``handle_research_search``.
    The response is rebuilt field-by-field from a whitelist, so nothing the
    worker (or a compromised worker) emits beyond gathering material can
    ever cross the bridge."""
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
        max_sources = int(payload.get("max_sources", GATHER_DEFAULT_SOURCES))
    except (TypeError, ValueError):
        max_sources = GATHER_DEFAULT_SOURCES
    max_sources = max(1, min(max_sources, GATHER_MAX_SOURCES_CAP))

    try:
        result = gather(query, max_sources)
    except Exception:
        logger.warning("research_gather provider raised")
        return 502, {"status": "error", "error": _ERR_GATHER}
    if not (isinstance(result, dict) and result.get("status") == "ok"):
        logger.warning("research_gather provider returned error")
        return 502, {"status": "error", "error": _ERR_GATHER}
    sources = []
    for row in result.get("sources") or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        sources.append({
            "url": url,
            "title": str(row.get("title") or "")[:200],
            "snippet": str(row.get("snippet") or "")[:GATHER_SNIPPET_CHARS],
        })
        if len(sources) >= max_sources:
            break
    def _num(key: str) -> float:
        try:
            return round(float(result.get(key) or 0.0), 4)
        except (TypeError, ValueError):
            return 0.0
    return 200, {"status": "ok", "provider": "gptr", "sources": sources,
                 "count": len(sources), "latency_s": _num("latency_s"),
                 "cost_usd_estimate": _num("cost_usd_estimate")}


def handle_research_note(
    body: bytes,
    auth_header: str,
    *,
    token: str,
    base_dir: str,
) -> tuple[int, dict]:
    """Persist one bounded markdown note through the existing safe helper."""
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
    note_path = payload.get("note_path")
    note_markdown = payload.get("note_markdown")
    if (
        not isinstance(note_path, str)
        or not note_path.strip().endswith(".md")
        or len(note_path) > 500
        or not isinstance(note_markdown, str)
        or not note_markdown.strip()
    ):
        return 400, {"status": "error", "error": _ERR_BAD_REQUEST}
    if not base_dir:
        return 503, {"status": "error", "error": _ERR_STORE}
    try:
        from gateway.cogitator_intake_bridge import save_local_copies

        saved = save_local_copies(
            {"note_path": note_path, "note_markdown": note_markdown}, base_dir)
    except Exception:
        logger.warning("research_note local save failed", exc_info=True)
        return 500, {"status": "error", "error": _ERR_STORE}
    if len(saved) != 1:
        return 500, {"status": "error", "error": _ERR_STORE}
    return 200, {"status": "ok", "saved": True}


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
        if self.path not in {"/research_search", "/research_note", "/research_gather"}:
            self._send_json(404, {"status": "error", "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        limit = (
            MAX_NOTE_BODY_BYTES
            if self.path == "/research_note"
            else MAX_BODY_BYTES
        )
        if length < 0 or length > limit:
            self._send_json(413, {"status": "error", "error": _ERR_BAD_REQUEST})
            return
        body = self.rfile.read(length)
        if self.path == "/research_search":
            status, response = handle_research_search(
                body,
                self.headers.get("Authorization") or "",
                token=self.server.bridge_token,  # type: ignore[attr-defined]
                search=self.server.bridge_search,  # type: ignore[attr-defined]
            )
        elif self.path == "/research_gather":
            status, response = handle_research_gather(
                body,
                self.headers.get("Authorization") or "",
                token=self.server.bridge_token,  # type: ignore[attr-defined]
                gather=self.server.bridge_gather,  # type: ignore[attr-defined]
            )
        else:
            status, response = handle_research_note(
                body,
                self.headers.get("Authorization") or "",
                token=self.server.bridge_token,  # type: ignore[attr-defined]
                base_dir=self.server.bridge_local_dir,  # type: ignore[attr-defined]
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
    local_dir: str | None = None,
    gather: Callable[[str, int], dict] = _default_gather,
) -> ThreadingHTTPServer:
    """Build the bound server (port 0 for an ephemeral test port)."""
    server = ThreadingHTTPServer((BIND_HOST, port), _Handler)
    server.bridge_token = _load_token() if token is None else token  # type: ignore[attr-defined]
    server.bridge_search = search  # type: ignore[attr-defined]
    server.bridge_gather = gather  # type: ignore[attr-defined]
    server.bridge_local_dir = (  # type: ignore[attr-defined]
        _load_local_dir() if local_dir is None else local_dir
    )
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = make_server()
    if not server.bridge_token:  # type: ignore[attr-defined]
        # Still serve /healthz; /research_search fail-closes with 503.
        logger.warning("%s is not set — research_search will refuse all requests", TOKEN_ENV)
    if not server.bridge_local_dir:  # type: ignore[attr-defined]
        logger.warning("research_note will refuse writes: local store not configured")
    logger.info("research bridge listening on %s:%d", BIND_HOST, server.server_address[1])
    server.serve_forever()


if __name__ == "__main__":
    main()
