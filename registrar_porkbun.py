"""Porkbun API v3 adapter for governed commerce operations.

Every call is single-attempt and redirects are refused. Real writes require
an explicit idempotency key; an unvalidated response after dispatch is exposed
as an uncertain outcome for caller-side provider reconciliation. Credentials
are accepted only from ``PORKBUN_API_KEY``/``PORKBUN_SECRET_KEY`` or a
mode-0600 JSON file named by ``PORKBUN_CREDENTIALS_FILE``, and exceptions never
include response bodies or credential values.

``PORKBUN_API_BASE`` exists only for tests and must identify a loopback host.
The ``--check`` entrypoint starts its own loopback fake and requires no real
credentials.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit

DEFAULT_API_BASE = "https://api.porkbun.com/api/json/v3"
MAX_CREDENTIAL_BYTES = 16_384
MAX_RESPONSE_BYTES = 1_048_576
TIMEOUT_SECONDS = 15

_DOMAIN_LABEL = re.compile(r"(?i)^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DNS_NAME_LABEL = re.compile(r"(?i)^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
_DNS_RECORD_ID = re.compile(r"^[1-9]\d*$")
_IDEMPOTENCY_KEY = re.compile(r"^[\x20-\x7e]{1,255}$")
_DNS_TYPES = frozenset({
    "A",
    "AAAA",
    "MX",
    "CNAME",
    "ALIAS",
    "TXT",
    "NS",
    "SRV",
    "TLSA",
    "CAA",
    "SSHFP",
})
_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")
_MONEY = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d{1,2})?$")
_AUTH_ERROR_CODES = frozenset({
    "API_KEY_REQUIRED",
    "DOMAIN_NOT_ALLOWED",
    "INVALID_API_KEYS_001",
    "INVALID_API_KEYS_002",
    "INVALID_TOKEN",
    "INVALID_USER",
    "IP_NOT_ALLOWED",
    "MISSING_SECRETAPIKEY",
})

_IDEMPOTENCY_ERROR_CODES = frozenset({
    "IDEMPOTENCY_KEY_IN_USE",
    "IDEMPOTENCY_KEY_MISMATCH",
})
_RATE_LIMIT_ERROR_CODES = frozenset({"RATE_LIMIT_EXCEEDED"})
MAX_RETRY_SECONDS = 86_400
_READ_ONLY_ROUTES = (
    ("GET", True, re.compile(r"^ping$")),
    ("GET", False, re.compile(r"^pricing/get$")),
    ("POST", True, re.compile(r"^domain/checkDomain/[a-z0-9.-]+$")),
    ("GET", True, re.compile(r"^domain/getRegistrationRequirements/[a-z0-9.-]+$")),
    ("GET", True, re.compile(r"^dns/retrieve/[a-z0-9.-]+$")),
    ("GET", True, re.compile(r"^domain/getNs/[a-z0-9.-]+$")),
    ("GET", True, re.compile(r"^domain/listAll(?:\?start=\d+)?$")),
)


_WRITE_ROUTES = (
    re.compile(r"^domain/create/[a-z0-9.-]+$"),
    re.compile(r"^dns/create/[a-z0-9.-]+$"),
    re.compile(r"^dns/edit/[a-z0-9.-]+/[1-9]\d*$"),
    re.compile(r"^dns/delete/[a-z0-9.-]+/[1-9]\d*$"),
)


class PorkbunError(RuntimeError):
    """Base class for safe Porkbun adapter failures."""


class PorkbunConfigurationError(PorkbunError):
    """Local configuration or credential-file failure."""


class PorkbunTransportError(PorkbunError):
    """HTTP or network failure without a valid provider error."""


class PorkbunResponseError(PorkbunError):
    """Malformed or undocumented provider response."""


class PorkbunAPIError(PorkbunError):
    """Validated provider-declared error.

    The safe diagnostic fields travel on the exception because the caller
    persists them: a stable provider code, the HTTP status, the retry budget
    the provider itself declared and its request id. Never a header block, a
    response body or a credential.
    """

    def __init__(
        self,
        code: str,
        http_status: int | None = None,
        *,
        ttl_remaining: int | None = None,
        rate_limit_reset: int | None = None,
        request_id: str = "",
    ):
        self.code = code
        self.http_status = http_status
        self.ttl_remaining = ttl_remaining
        self.rate_limit_reset = rate_limit_reset
        self.request_id = request_id
        suffix = f" (HTTP {http_status})" if http_status is not None else ""
        super().__init__(f"Porkbun API error: {code}{suffix}")


class PorkbunAuthenticationError(PorkbunAPIError):
    """Validated authentication or API-key scope failure."""


class PorkbunRateLimitError(PorkbunAPIError):
    """Provider-declared rate limit; retry only after the declared window."""


class PorkbunIdempotencyError(PorkbunAPIError):
    """Provider-declared 409 for a reused or still-in-flight key."""

    @property
    def outcome_uncertain(self) -> bool:
        return self.code == "IDEMPOTENCY_KEY_IN_USE"


class PorkbunMutationUncertainError(PorkbunTransportError):
    """A write was dispatched but its provider outcome could not be validated."""

    def __init__(self, operation: str):
        self.operation = operation
        super().__init__(
            "Porkbun mutation outcome is uncertain; reconcile provider state "
            "before any retry"
        )


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_MAX_EPOCH_SECONDS = 4_102_444_800  # year 2100


def _header_integer(headers: Any, name: str, maximum: int) -> int | None:
    """Read one plain non-negative integer header; never keep the raw text."""

    getter = getattr(headers, "get", None)
    raw = getter(name) if callable(getter) else None
    if not isinstance(raw, str) or not raw.strip().isdigit():
        return None
    value = int(raw.strip())
    return value if value <= maximum else None


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove known Porkbun credential values from diagnostic text."""
    known = (
        *secrets,
        os.environ.get("PORKBUN_API_KEY", ""),
        os.environ.get("PORKBUN_SECRET_KEY", ""),
    )
    for secret in known:
        if secret:
            text = text.replace(secret, "[REDACTED_PORKBUN_CREDENTIAL]")
    return re.sub(
        r"(?i)(?:secretapi(?:key)?|api[_-]?key)\s*[:=]\s*[^\s,;]+",
        "[REDACTED_PORKBUN_CREDENTIAL]",
        text,
    )


def _credentials() -> tuple[str, str]:
    path_text = os.environ.get("PORKBUN_CREDENTIALS_FILE", "").strip()
    api_key = os.environ.get("PORKBUN_API_KEY", "").strip()
    secret_key = os.environ.get("PORKBUN_SECRET_KEY", "").strip()
    if path_text and (api_key or secret_key):
        raise PorkbunConfigurationError(
            "configure Porkbun credentials using either environment values or a file"
        )
    if path_text:
        path = Path(path_text)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise PorkbunConfigurationError(
                    "PORKBUN_CREDENTIALS_FILE must be a regular mode-0600 file"
                )
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                descriptor = -1
                raw = handle.read(MAX_CREDENTIAL_BYTES + 1)
        except PorkbunConfigurationError:
            raise
        except (OSError, UnicodeError) as error:
            raise PorkbunConfigurationError(
                redact(f"cannot read PORKBUN_CREDENTIALS_FILE: {error}")
            ) from None
        finally:
            if "descriptor" in locals() and descriptor >= 0:
                os.close(descriptor)
        if len(raw.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
            raise PorkbunConfigurationError("PORKBUN_CREDENTIALS_FILE is too large")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise PorkbunConfigurationError(
                "PORKBUN_CREDENTIALS_FILE must contain valid JSON"
            ) from None
        if not isinstance(data, dict) or set(data) != {"apikey", "secretapikey"}:
            raise PorkbunConfigurationError(
                "PORKBUN_CREDENTIALS_FILE must contain only apikey and secretapikey"
            )
        api_key, secret_key = data["apikey"], data["secretapikey"]
    if not isinstance(api_key, str) or not isinstance(secret_key, str):
        raise PorkbunConfigurationError("Porkbun credentials must be strings")
    api_key, secret_key = api_key.strip(), secret_key.strip()
    if not api_key or not secret_key:
        raise PorkbunConfigurationError("Porkbun API credentials are not configured")
    return api_key, secret_key


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _api_base() -> str:
    override = os.environ.get("PORKBUN_API_BASE", "").strip()
    raw = override or DEFAULT_API_BASE
    try:
        parts = urlsplit(raw)
        _ = parts.port
    except ValueError as error:
        raise PorkbunConfigurationError(f"invalid Porkbun API base: {error}") from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise PorkbunConfigurationError("Porkbun API base is invalid")
    if override and not _is_loopback(parts.hostname):
        raise PorkbunConfigurationError(
            "PORKBUN_API_BASE overrides must use a loopback host"
        )
    return raw.rstrip("/")


def _object(
    value: object,
    where: str,
    required: set[str],
    optional: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PorkbunResponseError(f"{where} must be an object")
    data = cast(dict[str, Any], value)
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing:
        raise PorkbunResponseError(
            f"{where} missing fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise PorkbunResponseError(
            f"{where} has unknown fields: {', '.join(sorted(unknown))}"
        )
    return data


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PorkbunResponseError(f"{where} must be an object")
    return cast(dict[str, Any], value)


def _string(value: object, where: str, *, values: set[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise PorkbunResponseError(f"{where} must be a non-empty string")
    if values is not None and value not in values:
        raise PorkbunResponseError(f"{where} has an unsupported value")
    return value


def _integer(value: object, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PorkbunResponseError(f"{where} must be an integer >= {minimum}")
    return value


def _boolean(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise PorkbunResponseError(f"{where} must be a boolean")
    return value


def _money(value: object, where: str) -> str:
    text = _string(value, where)
    if not _MONEY.fullmatch(text):
        raise PorkbunResponseError(f"{where} must be a USD decimal string")
    return text


def _domain(value: str) -> str:
    if not isinstance(value, str) or len(value) > 253 or value != value.strip():
        raise PorkbunConfigurationError("domain is invalid")
    try:
        ascii_value = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise PorkbunConfigurationError("domain is invalid") from None
    labels = ascii_value.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise PorkbunConfigurationError("domain is invalid")
    return ascii_value


def _tld(value: str) -> str:
    if not isinstance(value, str) or value.startswith(".") or value != value.strip():
        raise PorkbunConfigurationError("TLD is invalid")
    try:
        ascii_value = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise PorkbunConfigurationError("TLD is invalid") from None
    if any(not _DOMAIN_LABEL.fullmatch(label) for label in ascii_value.split(".")):
        raise PorkbunConfigurationError("TLD is invalid")
    return ascii_value


def _input_integer(value: object, where: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PorkbunConfigurationError(f"{where} must be a non-negative integer")
    if maximum is not None and value > maximum:
        raise PorkbunConfigurationError(f"{where} is too large")
    return value


def _idempotency(value: str | None, *, dry_run: bool) -> str | None:
    if not isinstance(dry_run, bool):
        raise PorkbunConfigurationError("dry_run must be a boolean")
    if value is None:
        if dry_run:
            return None
        raise PorkbunConfigurationError(
            "a real Porkbun write requires an idempotency key"
        )
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _IDEMPOTENCY_KEY.fullmatch(value)
    ):
        raise PorkbunConfigurationError(
            "idempotency key must be 1-255 visible ASCII characters"
        )
    return value


def _record_id(value: str) -> str:
    if not isinstance(value, str) or not _DNS_RECORD_ID.fullmatch(value):
        raise PorkbunConfigurationError(
            "DNS record id must be a positive integer string"
        )
    return value


def _record_name(value: str, domain: str) -> str:
    if not isinstance(value, str) or len(value) > 253 or value != value.strip():
        raise PorkbunConfigurationError("DNS record name is invalid")
    if not value:
        return ""
    labels = value.lower().split(".")
    if any(
        (label == "*" and index != 0)
        or (label != "*" and not _DNS_NAME_LABEL.fullmatch(label))
        for index, label in enumerate(labels)
    ):
        raise PorkbunConfigurationError("DNS record name is invalid")
    normalized = ".".join(labels)
    if normalized == domain or normalized.endswith("." + domain):
        raise PorkbunConfigurationError(
            "DNS record name must be relative to the domain"
        )
    return normalized


def _record_content(record_type: str, value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 65_535
        or any(character in value for character in "\x00\r\n")
    ):
        raise PorkbunConfigurationError("DNS record content is invalid")
    if record_type in {"A", "AAAA"}:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise PorkbunConfigurationError("DNS record content is invalid") from None
        expected_version = 4 if record_type == "A" else 6
        if address.version != expected_version:
            raise PorkbunConfigurationError("DNS record content is invalid")
    elif record_type in {"ALIAS", "CNAME", "MX", "NS"}:
        _domain(value)
    return value


def _dns_payload(
    domain: str,
    *,
    record_type: str,
    content: str,
    name: str | None,
    ttl: int | None,
    prio: int | None,
    notes: str | None,
) -> dict[str, object]:
    if not isinstance(record_type, str) or record_type not in _DNS_TYPES:
        raise PorkbunConfigurationError("DNS record type is unsupported")
    payload: dict[str, object] = {
        "type": record_type,
        "content": _record_content(record_type, content),
    }
    if name is not None:
        payload["name"] = _record_name(name, domain)
    if ttl is not None:
        payload["ttl"] = _input_integer(ttl, "DNS ttl", maximum=2_147_483_647)
    if prio is not None:
        payload["prio"] = _input_integer(prio, "DNS priority", maximum=65_535)
    if notes is not None:
        if (
            not isinstance(notes, str)
            or len(notes) > 4096
            or any(character in notes for character in "\x00\r\n")
        ):
            raise PorkbunConfigurationError("DNS record notes are invalid")
        payload["notes"] = notes
    return payload


def _success(
    payload: object,
    where: str,
    required: set[str],
    optional: set[str] | frozenset[str] = frozenset(),
) -> dict:
    data = _object(payload, where, required | {"status"}, optional | {"requestId"})
    if data["status"] != "SUCCESS":
        raise PorkbunResponseError(f"{where}.status must be SUCCESS")
    if "requestId" in data:
        _string(data["requestId"], f"{where}.requestId")
    return data


def _validate_ping(payload: object) -> dict:
    data = _success(
        payload, "ping response", {"yourIp", "credentialsValid"}, {"xForwardedFor"}
    )
    try:
        ipaddress.ip_address(_string(data["yourIp"], "ping response.yourIp"))
    except ValueError:
        raise PorkbunResponseError(
            "ping response.yourIp must be an IP address"
        ) from None
    if data["credentialsValid"] is not True:
        raise PorkbunResponseError("ping response.credentialsValid must be true")
    if "xForwardedFor" in data:
        _string(data["xForwardedFor"], "ping response.xForwardedFor")
    return data


def _validate_pricing(payload: object) -> dict:
    data = _success(payload, "pricing response", {"pricing"})
    pricing = _mapping(data["pricing"], "pricing response.pricing")
    for tld, raw_entry in pricing.items():
        _tld(tld)
        entry = _object(
            raw_entry,
            f"pricing response.pricing.{tld}",
            {"registration", "renewal", "transfer"},
            {"specialType", "coupons"},
        )
        for field in ("registration", "renewal", "transfer"):
            _money(entry[field], f"pricing response.pricing.{tld}.{field}")
        if "specialType" in entry:
            _string(entry["specialType"], f"pricing response.pricing.{tld}.specialType")
        if "coupons" in entry:
            coupons = _mapping(
                entry["coupons"], f"pricing response.pricing.{tld}.coupons"
            )
            for name, raw_coupon in coupons.items():
                _string(name, f"pricing response.pricing.{tld}.coupon name")
                coupon = _object(
                    raw_coupon,
                    f"pricing response.pricing.{tld}.coupons.{name}",
                    {"code", "max_per_user", "first_year_only", "type", "amount"},
                )
                _string(coupon["code"], "coupon.code")
                _integer(coupon["max_per_user"], "coupon.max_per_user")
                _string(
                    coupon["first_year_only"],
                    "coupon.first_year_only",
                    values={"yes", "no"},
                )
                _string(coupon["type"], "coupon.type")
                if (
                    isinstance(coupon["amount"], bool)
                    or not isinstance(coupon["amount"], (int, float))
                    or coupon["amount"] < 0
                ):
                    raise PorkbunResponseError(
                        "coupon.amount must be a non-negative number"
                    )
    return data


def _validate_price_detail(value: object, where: str) -> None:
    detail = _object(value, where, {"type", "price", "regularPrice"})
    _string(detail["type"], f"{where}.type")
    _money(detail["price"], f"{where}.price")
    _money(detail["regularPrice"], f"{where}.regularPrice")


def _validate_limits(value: object, where: str) -> None:
    limits = _object(value, where, {"TTL", "limit", "used", "naturalLanguage"})
    for field in ("TTL", "limit", "used"):
        _integer(limits[field], f"{where}.{field}")
    _string(limits["naturalLanguage"], f"{where}.naturalLanguage")


def _validate_availability(payload: object) -> dict:
    data = _success(
        payload, "availability response", {"response"}, {"limits", "ttlRemaining"}
    )
    response = _object(
        data["response"],
        "availability response.response",
        {
            "avail",
            "type",
            "price",
            "firstYearPromo",
            "regularPrice",
            "premium",
            "minDuration",
        },
        {"additional"},
    )
    _string(
        response["avail"], "availability response.response.avail", values={"yes", "no"}
    )
    _string(
        response["type"], "availability response.response.type", values={"registration"}
    )
    _money(response["price"], "availability response.response.price")
    _string(
        response["firstYearPromo"],
        "availability response.response.firstYearPromo",
        values={"yes", "no"},
    )
    _money(response["regularPrice"], "availability response.response.regularPrice")
    _string(
        response["premium"],
        "availability response.response.premium",
        values={"yes", "no"},
    )
    _integer(
        response["minDuration"], "availability response.response.minDuration", minimum=1
    )
    if "additional" in response:
        additional = _object(
            response["additional"],
            "availability response.response.additional",
            set(),
            {"renewal", "transfer"},
        )
        for field in ("renewal", "transfer"):
            if field in additional:
                _validate_price_detail(
                    additional[field],
                    f"availability response.response.additional.{field}",
                )
    if "limits" in data:
        _validate_limits(data["limits"], "availability response.limits")
    if "ttlRemaining" in data:
        _integer(data["ttlRemaining"], "availability response.ttlRemaining")
    return data


def _validate_requirements(payload: object) -> dict:
    required = {
        "tld",
        "apiRegisterable",
        "registrationDurationYears",
        "maxRegistrationYears",
        "whoisPrivacySupported",
        "requiresValidatedAddress",
        "registrantOnly",
        "requestSchema",
        "registryRequirements",
    }
    data = _success(
        payload, "requirements response", required, {"notApiRegisterableReason"}
    )
    _tld(_string(data["tld"], "requirements response.tld"))
    _boolean(data["apiRegisterable"], "requirements response.apiRegisterable")
    _integer(
        data["registrationDurationYears"],
        "requirements response.registrationDurationYears",
        minimum=1,
    )
    if data["maxRegistrationYears"] is not None:
        _integer(
            data["maxRegistrationYears"],
            "requirements response.maxRegistrationYears",
            minimum=1,
        )
    for field in (
        "whoisPrivacySupported",
        "requiresValidatedAddress",
        "registrantOnly",
    ):
        _boolean(data[field], f"requirements response.{field}")
    if not isinstance(data["requestSchema"], dict):
        raise PorkbunResponseError(
            "requirements response.requestSchema must be an object"
        )
    if data["registryRequirements"] is not None and not isinstance(
        data["registryRequirements"], dict
    ):
        raise PorkbunResponseError(
            "requirements response.registryRequirements must be an object or null"
        )
    if data["apiRegisterable"] is False:
        _string(
            data.get("notApiRegisterableReason"),
            "requirements response.notApiRegisterableReason",
        )
    elif "notApiRegisterableReason" in data:
        raise PorkbunResponseError(
            "requirements response.notApiRegisterableReason conflicts with apiRegisterable"
        )
    return data


def _validate_dns(payload: object) -> dict:
    data = _success(payload, "DNS response", {"records"}, {"cloudflare"})
    if "cloudflare" in data:
        _string(
            data["cloudflare"],
            "DNS response.cloudflare",
            values={"enabled", "disabled"},
        )
    if not isinstance(data["records"], list):
        raise PorkbunResponseError("DNS response.records must be an array")
    for index, raw_record in enumerate(data["records"]):
        where = f"DNS response.records[{index}]"
        record = _object(
            raw_record,
            where,
            {"id", "name", "type", "content", "ttl"},
            {"prio", "notes"},
        )
        for field in ("id", "name", "type", "content", "ttl"):
            _string(record[field], f"{where}.{field}")
        for field in ("prio", "notes"):
            if field in record and record[field] is not None:
                _string(record[field], f"{where}.{field}")
    return data


def _validate_create_domain(
    payload: object, *, domain: str, cost: int, dry_run: bool
) -> dict:
    if dry_run:
        data = _success(
            payload,
            "domain-create dry-run response",
            {
                "dryRun",
                "wouldSucceed",
                "operation",
                "domain",
                "cost",
                "balance",
                "sufficientFunds",
            },
            {
                "tld",
                "available",
                "premium",
                "duration",
                "costDisplay",
                "monthlySpendLimit",
                "monthlySpendSoFar",
                "withinMonthlySpendLimit",
                "message",
                "idempotentReplayed",
            },
        )
        if data["dryRun"] is not True:
            raise PorkbunResponseError(
                "domain-create dry-run response.dryRun must be true"
            )
        would_succeed = _boolean(
            data["wouldSucceed"],
            "domain-create dry-run response.wouldSucceed",
        )
        _string(
            data["operation"],
            "domain-create dry-run response.operation",
            values={"registration"},
        )
        sufficient_funds = _boolean(
            data["sufficientFunds"],
            "domain-create dry-run response.sufficientFunds",
        )
        if would_succeed and not sufficient_funds:
            raise PorkbunResponseError(
                "domain-create dry-run response has inconsistent funds state"
            )
        _integer(data["balance"], "domain-create dry-run response.balance")
        optional_integers = (
            "duration",
            "monthlySpendLimit",
            "monthlySpendSoFar",
        )
        for field in optional_integers:
            if field in data:
                _integer(
                    data[field],
                    f"domain-create dry-run response.{field}",
                    minimum=1 if field == "duration" else 0,
                )
        if "withinMonthlySpendLimit" in data:
            _boolean(
                data["withinMonthlySpendLimit"],
                "domain-create dry-run response.withinMonthlySpendLimit",
            )
        spend_fields = {
            "monthlySpendLimit",
            "monthlySpendSoFar",
            "withinMonthlySpendLimit",
        }
        if "idempotentReplayed" in data and data["idempotentReplayed"] is not True:
            raise PorkbunResponseError(
                "domain-create dry-run response.idempotentReplayed must be true"
            )
        if spend_fields & data.keys() and not spend_fields <= data.keys():
            raise PorkbunResponseError(
                "domain-create dry-run response has incomplete spend-limit fields"
            )
        if "tld" in data:
            _tld(_string(data["tld"], "domain-create dry-run response.tld"))
        if "available" in data:
            _string(
                data["available"],
                "domain-create dry-run response.available",
                values={"available", "unavailable"},
            )
        if "premium" in data:
            _boolean(data["premium"], "domain-create dry-run response.premium")
        if "costDisplay" in data:
            display = _string(
                data["costDisplay"],
                "domain-create dry-run response.costDisplay",
            )
            expected_display = "$" + f"{cost // 100}.{cost % 100:02d}"
            if display != expected_display:
                raise PorkbunResponseError(
                    "domain-create dry-run response cost display does not match cost"
                )
        if "message" in data:
            _string(data["message"], "domain-create dry-run response.message")
    else:
        data = _success(
            payload,
            "domain-create response",
            {"domain", "cost", "orderId", "balance"},
            {"limits", "ttlRemaining", "idempotentReplayed"},
        )
        _integer(data["orderId"], "domain-create response.orderId", minimum=1)
        _integer(data["balance"], "domain-create response.balance")
        if "limits" in data:
            limits = _object(
                data["limits"],
                "domain-create response.limits",
                {"attempts", "success"},
            )
            for name in ("attempts", "success"):
                _validate_limits(limits[name], f"domain-create response.limits.{name}")
        if "ttlRemaining" in data:
            _integer(data["ttlRemaining"], "domain-create response.ttlRemaining")
        if "idempotentReplayed" in data and data["idempotentReplayed"] is not True:
            raise PorkbunResponseError(
                "domain-create response.idempotentReplayed must be true"
            )
    if _domain(_string(data["domain"], "domain-create response.domain")) != domain:
        raise PorkbunResponseError(
            "domain-create response domain does not match request"
        )
    if _integer(data["cost"], "domain-create response.cost") != cost:
        raise PorkbunResponseError("domain-create response cost does not match quote")
    return data


def _validate_dns_create(payload: object) -> dict:
    data = _success(
        payload,
        "DNS-create response",
        {"id"},
        {"idempotentReplayed"},
    )
    _record_id(_string(data["id"], "DNS-create response.id"))
    if "idempotentReplayed" in data and data["idempotentReplayed"] is not True:
        raise PorkbunResponseError(
            "DNS-create response.idempotentReplayed must be true"
        )
    return data


def _validate_dns_change(payload: object, where: str) -> dict:
    data = _success(payload, where, set(), {"message", "idempotentReplayed"})
    if "message" in data:
        _string(data["message"], f"{where}.message")
    if "idempotentReplayed" in data and data["idempotentReplayed"] is not True:
        raise PorkbunResponseError(f"{where}.idempotentReplayed must be true")
    return data


def _validate_nameservers(payload: object) -> dict:
    data = _success(payload, "nameserver response", {"ns"})
    if not isinstance(data["ns"], list):
        raise PorkbunResponseError("nameserver response.ns must be an array")
    for index, nameserver in enumerate(data["ns"]):
        _domain(_string(nameserver, f"nameserver response.ns[{index}]"))
    return data


def _validate_domains(payload: object) -> dict:
    data = _success(payload, "domain-list response", {"domains"}, {"count"})
    if "count" in data:
        _integer(data["count"], "domain-list response.count")
    if not isinstance(data["domains"], list):
        raise PorkbunResponseError("domain-list response.domains must be an array")
    required = {
        "domain",
        "status",
        "tld",
        "createDate",
        "expireDate",
        "securityLock",
        "whoisPrivacy",
        "autoRenew",
        "apiAccess",
        "notLocal",
    }
    for index, raw_domain in enumerate(data["domains"]):
        where = f"domain-list response.domains[{index}]"
        item = _object(raw_domain, where, required, {"labels"})
        _domain(_string(item["domain"], f"{where}.domain"))
        for field in ("status", "createDate", "expireDate"):
            _string(item[field], f"{where}.{field}")
        _tld(_string(item["tld"], f"{where}.tld"))
        for field in (
            "securityLock",
            "whoisPrivacy",
            "autoRenew",
            "apiAccess",
            "notLocal",
        ):
            if _integer(item[field], f"{where}.{field}") not in {0, 1}:
                raise PorkbunResponseError(f"{where}.{field} must be 0 or 1")
        if "labels" in item:
            if not isinstance(item["labels"], list):
                raise PorkbunResponseError(f"{where}.labels must be an array")
            for label_index, raw_label in enumerate(item["labels"]):
                label = _object(
                    raw_label,
                    f"{where}.labels[{label_index}]",
                    {"id", "title", "color"},
                )
                for field in ("id", "title", "color"):
                    _string(label[field], f"{where}.labels[{label_index}].{field}")
    if "count" in data and data["count"] != len(data["domains"]):
        raise PorkbunResponseError("domain-list response.count does not match domains")
    return data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PorkbunClient:
    """Single-attempt client for validated reads and explicitly keyed writes."""

    def __init__(self, *, timeout: float = TIMEOUT_SECONDS):
        self._credentials: tuple[str, str] | None = None
        self._base = _api_base()
        self._timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect
        )

    @property
    def base_url(self) -> str:
        return self._base

    def _provider_error(
        self, payload: object, http_status: int | None, headers: Any = None
    ) -> None:
        data = _object(
            payload,
            "error response",
            {"status", "message"},
            {"code", "next_action", "ttlRemaining", "requestId"},
        )
        if data["status"] != "ERROR":
            raise PorkbunResponseError("error response.status must be ERROR")
        _string(data["message"], "error response.message")
        code = data.get("code", "PROVIDER_ERROR")
        if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
            raise PorkbunResponseError("error response.code is invalid")
        if "ttlRemaining" in data:
            _integer(data["ttlRemaining"], "error response.ttlRemaining")
        if "requestId" in data:
            _string(data["requestId"], "error response.requestId")
        if "next_action" in data:
            action = _object(
                data["next_action"],
                "error response.next_action",
                {"type", "hint"},
                {"url"},
            )
            _string(action["type"], "error response.next_action.type")
            _string(action["hint"], "error response.next_action.hint")
            if "url" in action:
                _string(action["url"], "error response.next_action.url")
        ttl_remaining = data.get("ttlRemaining")
        if not isinstance(ttl_remaining, int) or ttl_remaining > MAX_RETRY_SECONDS:
            ttl_remaining = _header_integer(headers, "Retry-After", MAX_RETRY_SECONDS)
        request_id = data.get("requestId", "")
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            request_id = ""
        if code in _AUTH_ERROR_CODES:
            error_type = PorkbunAuthenticationError
        elif code in _RATE_LIMIT_ERROR_CODES:
            error_type = PorkbunRateLimitError
        elif http_status == 409 and code in _IDEMPOTENCY_ERROR_CODES:
            error_type = PorkbunIdempotencyError
        else:
            error_type = PorkbunAPIError
        raise error_type(
            code,
            http_status,
            ttl_remaining=ttl_remaining,
            rate_limit_reset=_header_integer(
                headers, "X-RateLimit-Reset", _MAX_EPOCH_SECONDS
            ),
            request_id=request_id,
        )

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        authenticated: bool = True,
        payload: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        path = path.lstrip("/")
        read_route = any(
            method == allowed_method
            and authenticated is allowed_auth
            and pattern.fullmatch(path)
            for allowed_method, allowed_auth, pattern in _READ_ONLY_ROUTES
        )
        write_route = (
            method == "POST"
            and authenticated
            and any(pattern.fullmatch(path) for pattern in _WRITE_ROUTES)
        )
        if not read_route and not write_route:
            raise PorkbunConfigurationError("unsupported Porkbun route")
        if write_route and payload is None:
            raise PorkbunConfigurationError(
                "Porkbun write route requires a validated payload"
            )
        if read_route and (payload is not None or idempotency_key is not None):
            raise PorkbunConfigurationError(
                "Porkbun read route does not accept write parameters"
            )
        mutation = (
            write_route and payload is not None and payload.get("dryRun") is not True
        )
        if write_route:
            idempotency_key = _idempotency(idempotency_key, dry_run=not mutation)
        headers = {"Accept": "application/json"}
        if authenticated:
            if self._credentials is None:
                self._credentials = _credentials()
            api_key, secret_key = self._credentials
            headers.update({
                "X-API-Key": api_key,
                "X-Secret-API-Key": secret_key,
            })
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        data = None
        if method == "POST":
            data = json.dumps(
                payload if write_route else {},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._base}/{path}", data=data, headers=headers, method=method
        )
        replayed: str | None = None
        response_headers: Any = None
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                replayed = response.headers.get("Idempotent-Replayed")
                response_headers = response.headers
        except urllib.error.HTTPError as error:
            raw = error.read(MAX_RESPONSE_BYTES + 1)
            response_headers = error.headers
            if len(raw) > MAX_RESPONSE_BYTES:
                if mutation:
                    raise PorkbunMutationUncertainError(path) from None
                raise PorkbunTransportError(
                    f"Porkbun HTTP failure for {path}: {error.code}"
                ) from None
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeError):
                if mutation:
                    raise PorkbunMutationUncertainError(path) from None
                raise PorkbunTransportError(
                    f"Porkbun HTTP failure for {path}: {error.code}"
                ) from None
            try:
                self._provider_error(payload, error.code, response_headers)
            except PorkbunResponseError:
                if mutation:
                    raise PorkbunMutationUncertainError(path) from None
                raise
            raise AssertionError("unreachable")
        except (urllib.error.URLError, OSError, ValueError) as error:
            if mutation:
                raise PorkbunMutationUncertainError(path) from None
            safe = redact(str(error), self._credentials or ())
            raise PorkbunTransportError(
                f"Porkbun transport failure for {path}: {safe}"
            ) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            if mutation:
                raise PorkbunMutationUncertainError(path)
            raise PorkbunResponseError(f"Porkbun response for {path} is too large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            if mutation:
                raise PorkbunMutationUncertainError(path) from None
            raise PorkbunResponseError(
                f"Porkbun response for {path} is not valid JSON"
            ) from None
        if isinstance(payload, dict) and payload.get("status") == "ERROR":
            try:
                self._provider_error(payload, None, response_headers)
            except PorkbunResponseError:
                if mutation:
                    raise PorkbunMutationUncertainError(path) from None
                raise
        if replayed is not None:
            if (
                replayed.lower() != "true"
                or not write_route
                or idempotency_key is None
                or not isinstance(payload, dict)
            ):
                if mutation:
                    raise PorkbunMutationUncertainError(path)
                raise PorkbunResponseError("invalid Porkbun idempotency replay header")
            payload = dict(payload)
            payload["idempotentReplayed"] = True
        return payload

    def ping(self) -> dict:
        return _validate_ping(self._request("ping"))

    def get_default_pricing(self) -> dict:
        return _validate_pricing(self._request("pricing/get", authenticated=False))

    def check_domain(self, domain: str) -> dict:
        name = quote(_domain(domain), safe="")
        return _validate_availability(
            self._request(f"domain/checkDomain/{name}", method="POST")
        )

    def get_registration_requirements(self, tld: str) -> dict:
        name = quote(_tld(tld), safe="")
        return _validate_requirements(
            self._request(f"domain/getRegistrationRequirements/{name}")
        )

    def get_dns_records(self, domain: str) -> dict:
        name = quote(_domain(domain), safe="")
        return _validate_dns(self._request(f"dns/retrieve/{name}"))

    def create_domain(
        self,
        domain: str,
        *,
        cost: int,
        dry_run: bool,
        idempotency_key: str | None = None,
    ) -> dict:
        """Preview or perform one registration attempt at the exact quoted cents."""
        normalized = _domain(domain)
        cents = _input_integer(cost, "domain cost")
        key = _idempotency(idempotency_key, dry_run=dry_run)
        payload: dict[str, object] = {
            "cost": cents,
            "agreeToTerms": "yes",
            "whoisPrivacy": True,
        }
        if dry_run:
            payload["dryRun"] = True
        raw = self._request(
            f"domain/create/{quote(normalized, safe='')}",
            method="POST",
            payload=payload,
            idempotency_key=key,
        )
        try:
            return _validate_create_domain(
                raw, domain=normalized, cost=cents, dry_run=dry_run
            )
        except (PorkbunConfigurationError, PorkbunResponseError):
            if not dry_run:
                raise PorkbunMutationUncertainError(
                    f"domain/create/{normalized}"
                ) from None
            raise

    def dns_create(
        self,
        domain: str,
        *,
        record_type: str,
        content: str,
        idempotency_key: str,
        name: str = "",
        ttl: int | None = None,
        prio: int | None = None,
        notes: str | None = None,
    ) -> dict:
        normalized = _domain(domain)
        payload = _dns_payload(
            normalized,
            record_type=record_type,
            content=content,
            name=name,
            ttl=ttl,
            prio=prio,
            notes=notes,
        )
        raw = self._request(
            f"dns/create/{quote(normalized, safe='')}",
            method="POST",
            payload=payload,
            idempotency_key=_idempotency(idempotency_key, dry_run=False),
        )
        try:
            return _validate_dns_create(raw)
        except (PorkbunConfigurationError, PorkbunResponseError):
            raise PorkbunMutationUncertainError("dns/create") from None

    def dns_edit(
        self,
        domain: str,
        record_id: str,
        *,
        record_type: str,
        content: str,
        idempotency_key: str,
        name: str | None = None,
        ttl: int | None = None,
        prio: int | None = None,
        notes: str | None = None,
    ) -> dict:
        normalized = _domain(domain)
        identifier = _record_id(record_id)
        payload = _dns_payload(
            normalized,
            record_type=record_type,
            content=content,
            name=name,
            ttl=ttl,
            prio=prio,
            notes=notes,
        )
        raw = self._request(
            f"dns/edit/{quote(normalized, safe='')}/{identifier}",
            method="POST",
            payload=payload,
            idempotency_key=_idempotency(idempotency_key, dry_run=False),
        )
        try:
            return _validate_dns_change(raw, "DNS-edit response")
        except PorkbunResponseError:
            raise PorkbunMutationUncertainError("dns/edit") from None

    def dns_delete(self, domain: str, record_id: str, *, idempotency_key: str) -> dict:
        normalized = _domain(domain)
        identifier = _record_id(record_id)
        raw = self._request(
            f"dns/delete/{quote(normalized, safe='')}/{identifier}",
            method="POST",
            payload={},
            idempotency_key=_idempotency(idempotency_key, dry_run=False),
        )
        try:
            return _validate_dns_change(raw, "DNS-delete response")
        except PorkbunResponseError:
            raise PorkbunMutationUncertainError("dns/delete") from None

    def get_nameservers(self, domain: str) -> dict:
        name = quote(_domain(domain), safe="")
        return _validate_nameservers(self._request(f"domain/getNs/{name}"))

    def list_domains(self, *, start: int = 0) -> dict:
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise PorkbunConfigurationError(
                "domain-list start must be a non-negative integer"
            )
        path = "domain/listAll" if start == 0 else f"domain/listAll?start={start}"
        return _validate_domains(self._request(path))


_CHECK_RESPONSES = {
    "/ping": {"status": "SUCCESS", "yourIp": "127.0.0.1", "credentialsValid": True},
    "/pricing/get": {
        "status": "SUCCESS",
        "pricing": {
            "com": {"registration": "10.00", "renewal": "10.00", "transfer": "10.00"}
        },
    },
    "/domain/checkDomain/example.com": {
        "status": "SUCCESS",
        "response": {
            "avail": "yes",
            "type": "registration",
            "price": "10.00",
            "firstYearPromo": "no",
            "regularPrice": "10.00",
            "premium": "no",
            "minDuration": 1,
        },
    },
    "/domain/getRegistrationRequirements/com": {
        "status": "SUCCESS",
        "tld": "com",
        "apiRegisterable": True,
        "registrationDurationYears": 1,
        "maxRegistrationYears": 10,
        "whoisPrivacySupported": True,
        "requiresValidatedAddress": False,
        "registrantOnly": False,
        "requestSchema": {},
        "registryRequirements": None,
    },
    "/dns/retrieve/example.com": {"status": "SUCCESS", "records": []},
    "/domain/getNs/example.com": {
        "status": "SUCCESS",
        "ns": ["ns1.porkbun.com", "ns2.porkbun.com"],
    },
    "/domain/listAll": {"status": "SUCCESS", "count": 0, "domains": []},
}


class _CheckHandler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _reply(self):
        self.seen.append((self.command, self.path))
        payload = _CHECK_RESPONSES.get(self.path)
        if payload is None:
            self.send_error(404)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply
    do_POST = _reply


def run_self_check() -> None:
    """Exercise every public read method against an ephemeral loopback fake."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    names = ("PORKBUN_API_BASE", "PORKBUN_API_KEY", "PORKBUN_SECRET_KEY")
    previous = {name: os.environ.get(name) for name in names}
    previous_file = os.environ.pop("PORKBUN_CREDENTIALS_FILE", None)
    _CheckHandler.seen = []
    try:
        os.environ["PORKBUN_API_BASE"] = f"http://127.0.0.1:{server.server_port}"
        os.environ["PORKBUN_API_KEY"] = "fake-self-check-key"
        os.environ["PORKBUN_SECRET_KEY"] = "fake-self-check-secret"
        client = PorkbunClient()
        client.ping()
        client.get_default_pricing()
        client.check_domain("example.com")
        client.get_registration_requirements("com")
        client.get_dns_records("example.com")
        client.get_nameservers("example.com")
        client.list_domains()
        expected = {
            (("POST" if "checkDomain" in path else "GET"), path)
            for path in _CHECK_RESPONSES
        }
        if set(_CheckHandler.seen) != expected:
            raise PorkbunResponseError(
                "self-check did not use the expected read-only routes"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if previous_file is not None:
            os.environ["PORKBUN_CREDENTIALS_FILE"] = previous_file
    print("Porkbun read-only self-check: PASS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the read-only self-check against an internal loopback fake",
    )
    args = parser.parse_args(argv)
    if not args.check:
        parser.error("--check is required")
    run_self_check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
