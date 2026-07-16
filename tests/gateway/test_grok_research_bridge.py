import json
import threading
import urllib.request

from gateway.research_bridge_server import (
    GROK_RESEARCH_MODE,
    handle_research_discover,
    make_server,
)

TOKEN = "bridge-test-token"


def result(**overrides):
    base = {
        "status": "ok",
        "provider": "xai-oauth",
        "model": "grok-4.5",
        "provider_path": "xai-oauth:grok-4.5",
        "sources": [
            {
                "url": "https://docs.example/fact",
                "title": "Official fact",
                "claim": "Unverified candidate claim",
                "quotation": "Unverified candidate quotation",
                "stance": "supporting",
            }
        ],
        "contradictions": [],
        "passes": 1,
        "latency_ms": 25,
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "quota": {"x-ratelimit-remaining-requests": "8"},
        "paid_api_used": False,
    }
    base.update(overrides)
    return base


def call(body, *, auth=f"Bearer {TOKEN}", token=TOKEN, discover=lambda **_k: result()):
    return handle_research_discover(
        json.dumps(body).encode(),
        auth,
        token=token,
        discover=discover,
    )


def request_body(**overrides):
    body = {
        "mode": GROK_RESEARCH_MODE,
        "claim": "Verify Acme TurboCache latency.",
        "questions": ["What does the official benchmark report?"],
        "max_sources": 10,
    }
    body.update(overrides)
    return body


def test_discovery_requires_bearer_and_explicit_mode():
    assert call(request_body(), token="")[0] == 503
    assert call(request_body(), auth="Bearer wrong")[0] == 401
    assert call(request_body(mode="default"))[0] == 400


def test_response_is_whitelisted_and_paid_api_is_false():
    status, response = call(
        request_body(),
        discover=lambda **_kwargs: result(
            verdict="promote",
            credentials="must not cross",
        ),
    )
    assert status == 200
    assert response["provider_path"] == "xai-oauth:grok-4.5"
    assert response["paid_api_used"] is False
    assert response["discovered_count"] == 1
    assert "verdict" not in response
    assert "credentials" not in response


def test_provider_failure_is_sanitized():
    marker = "secret-provider-body"
    status, response = call(
        request_body(),
        discover=lambda **_kwargs: {
            "status": "error",
            "failure_category": "oauth_unavailable",
            "raw": marker,
        },
    )
    assert status == 502
    assert response["failure_category"] == "oauth_unavailable"
    assert marker not in json.dumps(response)


def test_http_round_trip():
    server = make_server(
        0,
        token=TOKEN,
        search=lambda *_args: {},
        gather=lambda *_args: {},
        discover=lambda **_kwargs: result(),
        local_dir="",
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/research_discover",
            data=json.dumps(request_body()).encode(),
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        assert payload["mode"] == GROK_RESEARCH_MODE
        assert payload["model"] == "grok-4.5"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_wrong_provider_or_model_is_rejected_at_bridge_boundary():
    for overrides in (
        {"provider": "xai"},
        {"model": "grok-other"},
        {"provider_path": "xai-oauth:grok-other"},
        {"paid_api_used": True},
        {"passes": 0},
    ):
        status, response = call(
            request_body(), discover=lambda **_kwargs: result(**overrides)
        )
        assert status == 502
        assert response == {"status": "error", "error": "discovery provider failed"}


def test_malformed_usage_is_safely_zeroed_without_leaking_worker_fields():
    status, response = call(
        request_body(),
        discover=lambda **_kwargs: result(
            usage={"input_tokens": "bad", "output_tokens": [], "total_tokens": -1}
        ),
    )
    assert status == 200
    assert response["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def test_ordinary_intake_stays_on_openai_codex_oauth():
    from gateway.cogitator_intake_bridge import run_subscription_assessment

    captured = {}

    def run_turn(_prompt, route):
        captured.update(route)
        return {"final_response": "{}", "tools": []}

    ordinary = run_subscription_assessment(
        "ordinary intake assessment",
        provider="openai-codex",
        api_mode="codex_responses",
        run_turn=run_turn,
    )
    assert ordinary["provider_path"] == "hermes:openai-codex:oauth"
    assert captured["providers_allowed"] == ["openai-codex"]
    assert captured["providers_ignored"] == ["openrouter"]
    assert captured["fallback_model"] is None
