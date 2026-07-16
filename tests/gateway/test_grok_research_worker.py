import json

from gateway import grok_research_worker as worker


class Response:
    def __init__(self, text, status=200, *, usage=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = {
            "output_text": text,
            "usage": usage
            or {
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 30,
            },
        }

    def json(self):
        return self._payload


def credentials(provider="xai-oauth"):
    return {
        "provider": provider,
        "api_key": "not-exposed",
        "base_url": "https://api.x.ai/v1",
    }


def valid_text(count=1):
    return json.dumps({
        "sources": [
            {
                "url": f"https://docs.example/{index}",
                "title": f"Source {index}",
                "claim": "Candidate claim only",
                "quotation": "A candidate quotation requiring verification.",
                "stance": "supporting",
            }
            for index in range(count)
        ],
        "contradictions": [],
    })


def test_oauth_only_discovery_uses_grok_45_and_both_search_tools():
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(
            valid_text(12),
            headers={"x-ratelimit-remaining-requests": "9"},
        )

    result = worker.discover(
        claim="Verify Acme TurboCache latency.",
        questions=["What does the official benchmark report?"],
        max_sources=99,
        post=post,
        credential_resolver=credentials,
    )

    assert result["status"] == "ok"
    assert result["provider_path"] == "xai-oauth:grok-4.5"
    assert result["paid_api_used"] is False
    assert result["discovered_count"] == 10
    assert result["usage"]["total_tokens"] == 30
    assert len(calls) == 1
    payload = calls[0][1]["json"]
    assert payload["model"] == "grok-4.5"
    assert payload["store"] is False
    assert payload["tools"] == [{"type": "web_search"}, {"type": "x_search"}]


def test_api_key_fallback_is_refused_before_provider_call():
    called = []
    result = worker.discover(
        claim="Verify a public product fact.",
        post=lambda *args, **kwargs: called.append(True),
        credential_resolver=lambda: credentials("xai"),
    )
    assert result == {
        "status": "error",
        "failure_category": "oauth_required",
    }
    assert called == []


def test_one_structural_repair_is_allowed_without_search_tools():
    calls = []

    def post(_url, **kwargs):
        calls.append(kwargs["json"])
        return Response("not json" if len(calls) == 1 else valid_text())

    result = worker.discover(
        claim="Verify a market adoption statistic.",
        post=post,
        credential_resolver=credentials,
    )

    assert result["status"] == "ok"
    assert result["passes"] == 2
    assert "tools" in calls[0]
    assert "tools" not in calls[1]


def test_invalid_second_response_fails_after_exactly_two_passes():
    calls = []
    result = worker.discover(
        claim="Verify a technical architecture claim.",
        post=lambda *_args, **_kwargs: calls.append(True) or Response("not json"),
        credential_resolver=credentials,
    )
    assert result["status"] == "error"
    assert result["failure_category"] == "invalid_structured_response"
    assert result["passes"] == 2
    assert len(calls) == 2


def test_empty_or_html_response_is_not_schema_repaired():
    for value in ("", "<!doctype html><html>provider error</html>"):
        calls = []
        result = worker.discover(
            claim="Verify a business opportunity.",
            post=lambda *_args, **_kwargs: calls.append(True) or Response(value),
            credential_resolver=credentials,
        )
        assert result["failure_category"] == "invalid_provider_response"
        assert len(calls) == 1


def test_sensitive_brief_is_rejected_without_provider_call():
    called = []
    result = worker.discover(
        claim="Inspect this customer secret and CRM record.",
        post=lambda *_args, **_kwargs: called.append(True),
        credential_resolver=credentials,
    )
    assert result["failure_category"] == "invalid_or_sensitive_brief"
    assert called == []


def test_sensitive_question_is_rejected_without_provider_call():
    called = []
    result = worker.discover(
        claim="Verify a public product fact.",
        questions=["Check private.person@example.com for the missing detail."],
        post=lambda *_args, **_kwargs: called.append(True),
        credential_resolver=credentials,
    )
    assert result["failure_category"] == "invalid_or_sensitive_brief"
    assert called == []


def test_invalid_source_bound_falls_back_to_the_hard_cap():
    result = worker.discover(
        claim="Verify a public product fact.",
        max_sources="not-a-number",
        post=lambda *_args, **_kwargs: Response(valid_text(12)),
        credential_resolver=credentials,
    )
    assert result["status"] == "ok"
    assert result["discovered_count"] == worker.MAX_SOURCES
