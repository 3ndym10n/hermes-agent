import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import authenticated_x_source as xsource


POST_ID = "2076879176586699257"
URL = f"https://x.com/i/status/{POST_ID}"


def _session(tmp_path, *, auth=True, expired=False):
    path = tmp_path / "cookies.txt"
    expires = 1 if expired else 2_000_000_000
    rows = ["# Netscape HTTP Cookie File"]
    if auth:
        rows.append(
            f"#HttpOnly_.x.com\tTRUE\t/\tTRUE\t{expires}\tauth_token\tfake-auth"
        )
    rows.append(f".x.com\tTRUE\t/\tTRUE\t{expires}\tct0\tfake-ct0")
    path.write_text("\n".join(rows) + "\n")
    path.chmod(0o600)
    return path


def _binary(tmp_path):
    path = tmp_path / "twitter"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o700)
    return path


def _tweet(**updates):
    tweet = {
        "id": POST_ID,
        "text": "A complete ordinary post.",
        "author": {"screenName": "viveksoft77"},
        "isSubscriberOnly": False,
    }
    tweet.update(updates)
    return tweet


def _completed(tweet=None, *, returncode=0, ok=True, error=None):
    payload = {"ok": ok, "schema_version": "1"}
    if ok:
        payload["data"] = [_tweet() if tweet is None else tweet]
    else:
        payload["error"] = error or {"code": "unknown"}
    return SimpleNamespace(
        returncode=returncode, stdout=json.dumps(payload), stderr="sensitive"
    )


def _fetch(tmp_path, completed, **kwargs):
    calls = []

    def run(command, **run_kwargs):
        calls.append((command, run_kwargs))
        return completed

    result = xsource.fetch_authenticated_x_source(
        URL,
        environ={xsource.FEATURE_ENV: "true", "UNRELATED_SECRET": "no-copy"},
        cookie_path=_session(tmp_path),
        binary_path=_binary(tmp_path),
        run=run,
        now=1_900_000_000,
        **kwargs,
    )
    return result, calls


def test_gate_is_checked_before_cookie_or_provider_access(tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("provider must not run")

    result = xsource.fetch_authenticated_x_source(
        URL,
        environ={},
        cookie_path=tmp_path / "missing",
        binary_path=tmp_path / "missing",
        run=forbidden,
    )
    assert result == {"status": "provider_unavailable", "complete": False}


def test_session_ownership_check_fails_closed_without_geteuid(tmp_path, monkeypatch):
    monkeypatch.delattr(xsource.os, "geteuid")
    result = xsource.fetch_authenticated_x_source(
        URL,
        environ={xsource.FEATURE_ENV: "true"},
        cookie_path=_session(tmp_path),
        binary_path=_binary(tmp_path),
    )
    assert result == {"status": "login_required", "complete": False}


def test_fixed_read_only_command_and_minimal_child_environment(tmp_path):
    result, calls = _fetch(tmp_path, _completed())
    assert result["status"] == "fetched_full_authenticated"
    assert result["post_id"] == POST_ID
    assert result["author_handle"] == "viveksoft77"
    assert result["source_type"] == "ordinary_post"
    assert result["complete"] is True
    assert result["acquisition_mode"] == "authenticated_x_fallback"
    command, options = calls[0]
    assert command[1:] == ["tweet", URL, "--max", "1", "--full-text", "--json"]
    assert set(options["env"]) == {
        "HOME",
        "PATH",
        "LANG",
        "TWITTER_AUTH_TOKEN",
        "TWITTER_CT0",
    }
    assert "UNRELATED_SECRET" not in options["env"]
    assert options["timeout"] == 15
    assert "fake-auth" not in json.dumps(result)
    assert "fake-ct0" not in json.dumps(result)


@pytest.mark.parametrize(
    ("tweet", "source_type", "expected_text"),
    [
        (_tweet(text="x" * 281), "long_form_note", "x" * 281),
        (
            _tweet(text="https://t.co/a", articleTitle="Title", articleText="Body"),
            "article",
            "Title\n\nBody",
        ),
        (
            _tweet(text="Visible protected content", isSubscriberOnly=True),
            "protected_post",
            "Visible protected content",
        ),
    ],
)
def test_supported_full_source_types(tmp_path, tweet, source_type, expected_text):
    result, _calls = _fetch(tmp_path, _completed(tweet))
    assert result["source_type"] == source_type
    assert result["text"] == expected_text


def test_quoted_post_text_is_preserved(tmp_path):
    tweet = _tweet(
        quotedTweet={
            "id": "9",
            "text": "Quoted source",
            "author": {"screenName": "quoted_author"},
        }
    )
    result, _calls = _fetch(tmp_path, _completed(tweet))
    assert "Quoted post by @quoted_author:\nQuoted source" in result["text"]


def test_missing_or_expired_session_fails_without_provider_call(tmp_path):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))

    missing = xsource.fetch_authenticated_x_source(
        URL,
        environ={xsource.FEATURE_ENV: "true"},
        cookie_path=_session(tmp_path, auth=False),
        binary_path=_binary(tmp_path),
        run=run,
        now=1_900_000_000,
    )
    expired = xsource.fetch_authenticated_x_source(
        URL,
        environ={xsource.FEATURE_ENV: "true"},
        cookie_path=_session(tmp_path, expired=True),
        binary_path=_binary(tmp_path),
        run=run,
        now=1_900_000_000,
    )
    assert missing["status"] == "login_required"
    assert expired["status"] == "auth_session_expired"
    assert calls == []


@pytest.mark.parametrize(
    ("tweet", "status"),
    [
        (_tweet(id="1"), "identity_mismatch"),
        (_tweet(author={"screenName": "bad handle"}), "identity_mismatch"),
        (_tweet(text=""), "partial_source_rejected"),
        (
            _tweet(text="preview", articleTitle="Title", articleText=""),
            "partial_source_rejected",
        ),
        (_tweet(text="https://t.co/only"), "partial_source_rejected"),
        (_tweet(text="x" * 18_001), "partial_source_rejected"),
    ],
)
def test_identity_and_completeness_fail_closed(tmp_path, tweet, status):
    result, _calls = _fetch(tmp_path, _completed(tweet))
    assert result == {"status": status, "complete": False}


@pytest.mark.parametrize(
    ("error_code", "status"),
    [
        ("unauthorized", "auth_session_expired"),
        ("forbidden", "forbidden_to_burner"),
        ("tweet_not_found", "source_not_found"),
        ("upstream_error", "provider_unavailable"),
    ],
)
def test_provider_errors_are_sanitized(tmp_path, error_code, status):
    result, _calls = _fetch(
        tmp_path,
        _completed(
            returncode=1, ok=False, error={"code": error_code, "message": "secret"}
        ),
    )
    assert result == {"status": status, "complete": False}
    assert "secret" not in json.dumps(result)


def test_strict_url_parser_rejects_non_status_and_wrong_host(tmp_path):
    for url in (
        "https://x.com/home",
        "https://evil.example/i/status/2076879176586699257",
        "https://x.com/i/status/not-a-number",
    ):
        result = xsource.fetch_authenticated_x_source(
            url,
            environ={xsource.FEATURE_ENV: "true"},
            cookie_path=tmp_path / "unused",
        )
        assert result["status"] == "unsupported_source_type"


def test_exact_failed_live_url_fixture_recovers_complete_source(tmp_path):
    fixture = json.loads(
        Path("tests/fixtures/x_authenticated_2076879176586699257.json").read_text()
    )
    tweet = _tweet(
        id=fixture["post_id"],
        text=fixture["text"],
        author={"screenName": fixture["author_handle"]},
    )
    result, _calls = _fetch(tmp_path, _completed(tweet))
    assert result["status"] == "fetched_full_authenticated"
    assert result["canonical_url"] == fixture["canonical_url"]
    assert result["post_id"] == fixture["post_id"]
    assert result["author_handle"] == fixture["author_handle"]
    assert result["source_type"] == "long_form_note"
    assert result["complete"] is True
    assert result["text"] == fixture["text"]
    assert len(result["text"]) == 2271
    assert result["acquisition_mode"] == "authenticated_x_fallback"
