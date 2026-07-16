"""Bounded xAI OAuth discovery worker for explicit Cogitator research jobs.

Grok supplies candidate leads only. Cogitator independently re-fetches every
URL and remains the evidence authority. This worker never uses an API-key
fallback, never promotes, and never exposes OAuth material.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from tools.xai_http import hermes_xai_user_agent, resolve_xai_http_credentials
from tools.x_search_tool import _extract_response_text

PROVIDER = "xai-oauth"
MODEL = "grok-4.5"
MAX_SOURCES = 10
MAX_QUESTIONS = 8
MAX_PASSES = 2
TIMEOUT_SECONDS = 120
MAX_TEXT_CHARS = 600
MAX_QUOTE_CHARS = 400
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_SENSITIVE_RE = re.compile(
    r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|authorization:"
    r"|password|customer secret|private email|crm record)\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[^@\s]+@[^@\s]+\.[^@\s]+\b")
_STANCES = frozenset({"supporting", "contradicting", "background", "unknown"})


def _clip(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


def _json_object(text: str) -> dict[str, Any] | None:
    match = _JSON_OBJECT_RE.search(str(text or ""))
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _normalize_payload(value: object, max_sources: int) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
        return None
    sources = []
    seen: set[str] = set()
    for row in value["sources"]:
        if not isinstance(row, dict):
            return None
        url = str(row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return None
        if url in seen:
            continue
        stance = str(row.get("stance") or "unknown").strip().lower()
        if stance not in _STANCES:
            return None
        seen.add(url)
        sources.append({
            "url": url,
            "title": _clip(row.get("title"), 200),
            "claim": _clip(row.get("claim")),
            "quotation": _clip(row.get("quotation"), MAX_QUOTE_CHARS),
            "stance": stance,
        })
        if len(sources) >= max_sources:
            break
    contradictions = []
    raw_contradictions = value.get("contradictions") or []
    if not isinstance(raw_contradictions, list):
        return None
    for row in raw_contradictions[:MAX_SOURCES]:
        if not isinstance(row, dict):
            return None
        urls = row.get("source_urls") or []
        if not isinstance(urls, list):
            return None
        safe_urls = [
            str(url).strip()
            for url in urls[:MAX_SOURCES]
            if str(url).strip().startswith(("http://", "https://"))
        ]
        contradictions.append({
            "claim": _clip(row.get("claim")),
            "source_urls": safe_urls,
        })
    return {"sources": sources, "contradictions": contradictions}


def _prompt(claim: str, questions: list[str], max_sources: int) -> str:
    question_text = "\n".join(f"- {q}" for q in questions) or "- Verify the claim."
    return (
        "This is an explicitly authorized, bounded research discovery job. "
        "Use web_search and x_search to find current source leads. Return only "
        "one JSON object matching this contract:\n"
        '{"sources":[{"url":"https://...","title":"string","claim":"candidate '
        'claim from this source","quotation":"candidate exact quotation or empty",'
        '"stance":"supporting|contradicting|background|unknown"}],'
        '"contradictions":[{"claim":"short description","source_urls":["https://..."]}]}\n'
        f"Return at most {max_sources} sources. Candidate claims, quotations, "
        "stances, and contradictions are unverified leads; do not make a final "
        "verdict or recommendation. Prefer official and primary sources.\n\n"
        f"Claim:\n{claim}\n\nQuestions:\n{question_text}"
    )


def _repair_prompt(text: str) -> str:
    return (
        "Repair the following structurally invalid response into JSON only. "
        "Do not add sources or facts. Required contract:\n"
        '{"sources":[{"url":"https://...","title":"string","claim":"string",'
        '"quotation":"string","stance":"supporting|contradicting|background|unknown"}],'
        '"contradictions":[{"claim":"string","source_urls":["https://..."]}]}\n\n'
        f"Malformed response:\n{text[:8000]}"
    )


def _usage(payload: dict[str, Any]) -> dict[str, int]:
    raw = payload.get("usage") if isinstance(payload, dict) else {}
    raw = raw if isinstance(raw, dict) else {}

    def number(*keys: str) -> int:
        for key in keys:
            try:
                return max(0, int(raw.get(key) or 0))
            except (TypeError, ValueError):
                continue
        return 0

    return {
        "input_tokens": number("input_tokens", "prompt_tokens"),
        "output_tokens": number("output_tokens", "completion_tokens"),
        "total_tokens": number("total_tokens"),
    }


def _quota(headers: object) -> dict[str, str]:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return {}
    keys = (
        "x-ratelimit-remaining-requests",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
    )
    return {key: str(getter(key))[:80] for key in keys if getter(key) is not None}


def discover(
    *,
    claim: str,
    questions: list[str] | None = None,
    max_sources: int = MAX_SOURCES,
    post: Callable[..., Any] | None = None,
    credential_resolver: Callable[..., dict[str, Any]] = resolve_xai_http_credentials,
) -> dict[str, Any]:
    """Run at most two Grok passes and return bounded candidate diagnostics."""
    claim = str(claim or "").strip()
    questions = [
        _clip(question)
        for question in (questions or [])[:MAX_QUESTIONS]
        if str(question or "").strip()
    ]
    brief_text = "\n".join([claim, *questions])
    if (
        not claim
        or len(claim) > 4000
        or _SENSITIVE_RE.search(brief_text)
        or _EMAIL_RE.search(brief_text)
    ):
        return {"status": "error", "failure_category": "invalid_or_sensitive_brief"}
    try:
        max_sources = int(max_sources or MAX_SOURCES)
    except (TypeError, ValueError):
        max_sources = MAX_SOURCES
    max_sources = max(1, min(max_sources, MAX_SOURCES))

    try:
        creds = credential_resolver()
    except Exception:
        return {"status": "error", "failure_category": "oauth_unavailable"}
    if str(creds.get("provider") or "") != PROVIDER:
        return {"status": "error", "failure_category": "oauth_required"}
    token = str(creds.get("api_key") or "").strip()
    base_url = str(creds.get("base_url") or "https://api.x.ai/v1").rstrip("/")
    if not token:
        return {"status": "error", "failure_category": "oauth_unavailable"}

    if post is None:
        try:
            import requests
        except ImportError:
            return {"status": "error", "failure_category": "provider_unavailable"}
        post = requests.post

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": hermes_xai_user_agent(),
    }
    started = time.monotonic()
    last_text = ""
    last_payload: dict[str, Any] = {}
    response_headers: object = {}
    for pass_number in range(1, MAX_PASSES + 1):
        payload: dict[str, Any] = {
            "model": MODEL,
            "input": [
                {
                    "role": "user",
                    "content": (
                        _prompt(claim, questions, max_sources)
                        if pass_number == 1
                        else _repair_prompt(last_text)
                    ),
                }
            ],
            "store": False,
        }
        if pass_number == 1:
            payload["tools"] = [{"type": "web_search"}, {"type": "x_search"}]
        try:
            response = post(
                f"{base_url}/responses",
                headers=headers,
                json=payload,
                timeout=TIMEOUT_SECONDS,
            )
        except Exception:
            return {
                "status": "error",
                "failure_category": "provider_unavailable",
                "provider": PROVIDER,
                "model": MODEL,
                "passes": pass_number,
                "paid_api_used": False,
            }
        if getattr(response, "status_code", 0) != 200:
            return {
                "status": "error",
                "failure_category": "provider_http_error",
                "provider": PROVIDER,
                "model": MODEL,
                "passes": pass_number,
                "http_status": int(getattr(response, "status_code", 0) or 0),
                "paid_api_used": False,
            }
        try:
            last_payload = response.json()
        except Exception:
            last_payload = {}
        if not isinstance(last_payload, dict) or isinstance(
            last_payload.get("error"), dict
        ):
            return {
                "status": "error",
                "failure_category": "provider_error_response",
                "provider": PROVIDER,
                "model": MODEL,
                "passes": pass_number,
                "paid_api_used": False,
            }
        response_headers = getattr(response, "headers", {})
        last_text = _extract_response_text(last_payload)
        normalized = _normalize_payload(_json_object(last_text), max_sources)
        if normalized is not None:
            return {
                "status": "ok",
                "provider": PROVIDER,
                "model": MODEL,
                "provider_path": f"{PROVIDER}:{MODEL}",
                "sources": normalized["sources"],
                "contradictions": normalized["contradictions"],
                "discovered_count": len(normalized["sources"]),
                "passes": pass_number,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "usage": _usage(last_payload),
                "quota": _quota(response_headers),
                "paid_api_used": False,
            }
        if not last_text or last_text.lstrip().lower().startswith("<!doctype html"):
            return {
                "status": "error",
                "failure_category": "invalid_provider_response",
                "provider": PROVIDER,
                "model": MODEL,
                "passes": pass_number,
                "paid_api_used": False,
            }
    return {
        "status": "error",
        "failure_category": "invalid_structured_response",
        "provider": PROVIDER,
        "model": MODEL,
        "passes": MAX_PASSES,
        "paid_api_used": False,
    }


__all__ = [
    "MAX_PASSES",
    "MAX_SOURCES",
    "MODEL",
    "PROVIDER",
    "discover",
]
