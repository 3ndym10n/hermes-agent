"""Read-only, injectable verification for the commerce V1 checklist §9.3."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from commerce_content import scan_forbidden_claims

MAX_HTML_BYTES = 2_097_152
PLACEHOLDER = re.compile(r"⟨[^⟩]+⟩")
_DOMAIN = re.compile(
    r"(?a)^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
Fetch = Callable[[str], "HTTPResult"]
DNSLookup = Callable[[str, str], Mapping[str, Sequence[str]]]


class VerificationConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class HTTPResult:
    status: int
    body: bytes
    final_url: str
    redirects: tuple[str, ...] = ()
    tls_valid: bool = True


class _StorefrontHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.commerce_controls: list[str] = []
        self._button_depth = 0
        self._button_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        destination = (
            values.get("action", "") if tag == "form" else values.get("href", "")
        )
        path = urlsplit(destination).path.lower()
        if path.startswith(("/cart", "/checkout", "/products/")):
            self.commerce_controls.append(f"{tag}:{path}")
        identity = " ".join((values.get("id", ""), values.get("class", ""))).lower()
        if re.search(r"(?:^|[-_\s])price(?:$|[-_\s])", identity):
            self.commerce_controls.append(f"{tag}:price")
        if any(
            marker in identity
            for marker in ("buy-button", "product-form", "shopify-payment-button")
        ):
            self.commerce_controls.append(f"{tag}:commerce")
        if values.get("itemprop", "").lower() == "price" or any(
            "price" in key.lower() for key in values if key.lower().startswith("data-")
        ):
            self.commerce_controls.append(f"{tag}:price")
        if tag == "button":
            self._button_depth += 1
        if tag == "input" and values.get("type", "").lower() in {"submit", "button"}:
            if re.search(
                r"(?i)\b(?:buy now|add to cart|checkout|pre-?order|reserve|deposit)\b",
                values.get("value", ""),
            ):
                self.commerce_controls.append("input:commerce")

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._button_depth:
            text = " ".join(self._button_text)
            if re.search(
                r"(?i)\b(?:buy now|add to cart|checkout|pre-?order|reserve|deposit)\b",
                text,
            ):
                self.commerce_controls.append("button:commerce")
            self._button_depth -= 1
            self._button_text.clear()

    def handle_data(self, data: str) -> None:
        if self._button_depth:
            self._button_text.append(data.strip())


def _host(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.lower()
        or not _DOMAIN.fullmatch(value)
    ):
        raise VerificationConfigurationError("storefront domain is invalid")
    return value


def _validated_result(
    result: object, requested_url: str, allowed_hosts: set[str]
) -> HTTPResult:
    if not isinstance(result, HTTPResult):
        raise VerificationConfigurationError("fetch must return HTTPResult")
    if (
        isinstance(result.status, bool)
        or not isinstance(result.status, int)
        or not 100 <= result.status <= 599
        or not isinstance(result.body, bytes)
        or len(result.body) > MAX_HTML_BYTES
        or not isinstance(result.final_url, str)
        or not result.final_url
        or len(result.final_url) > 2048
        or not isinstance(result.redirects, tuple)
        or len(result.redirects) > 20
        or any(not isinstance(url, str) or len(url) > 2048 for url in result.redirects)
        or not isinstance(result.tls_valid, bool)
    ):
        raise VerificationConfigurationError("HTTPResult is invalid or too large")
    for url in (result.final_url, *result.redirects):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in allowed_hosts
            or parts.username
            or parts.password
        ):
            raise VerificationConfigurationError(
                "storefront probe crossed the pinned hosts"
            )
    requested = urlsplit(requested_url)
    if requested.scheme != "https" or requested.hostname not in allowed_hosts:
        raise VerificationConfigurationError(
            "storefront request crossed the pinned hosts"
        )
    return result


def _html(body: bytes) -> tuple[str, _StorefrontHTML]:
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        return "", _StorefrontHTML()
    parser = _StorefrontHTML()
    try:
        parser.feed(text)
    except (AssertionError, ValueError):
        return "", _StorefrontHTML()
    return text, parser


def _no_pii_probe(raw: Mapping[str, object]) -> dict[str, bool]:
    required = {
        "customer_found",
        "consent_recorded",
        "confirmation_shown",
        "subscriber_deleted",
    }
    if not isinstance(raw, Mapping) or set(raw) != required:
        raise VerificationConfigurationError("waitlist probe has an invalid shape")
    if any(not isinstance(raw[key], bool) for key in required):
        raise VerificationConfigurationError("waitlist probe values must be boolean")
    return {key: raw[key] for key in sorted(required)}


def _dns_green(
    dns_lookup: DNSLookup,
    domain: str,
    expected_dns: Mapping[str, Sequence[str]],
) -> bool:
    if set(expected_dns) != {"A", "AAAA", "CNAME"}:
        raise VerificationConfigurationError("expected_dns requires A, AAAA and CNAME")
    for record_type, host in (
        ("A", domain),
        ("AAAA", domain),
        ("CNAME", f"www.{domain}"),
    ):
        expected = expected_dns[record_type]
        if (
            not isinstance(expected, Sequence)
            or isinstance(expected, (str, bytes))
            or not expected
            or any(
                not isinstance(value, str) or not 1 <= len(value) <= 253
                for value in expected
            )
        ):
            raise VerificationConfigurationError("expected DNS values are invalid")
        observed = dns_lookup(host, record_type)
        if (
            not isinstance(observed, Mapping)
            or len(observed) < 2
            or not all(isinstance(resolver, str) and resolver for resolver in observed)
        ):
            return False
        canonical_expected = {value.rstrip(".").lower() for value in expected}
        for answers in observed.values():
            if (
                not isinstance(answers, Sequence)
                or isinstance(answers, (str, bytes))
                or any(
                    not isinstance(value, str) or not 1 <= len(value) <= 253
                    for value in answers
                )
            ):
                return False
            canonical_answers = {value.rstrip(".").lower() for value in answers}
            if canonical_answers != canonical_expected:
                return False
    return True


def verify_launch(
    *,
    domain: str,
    approved_html: str,
    fetch: Fetch,
    dns_lookup: DNSLookup,
    expected_dns: Mapping[str, Sequence[str]],
    registrar_active: bool,
    waitlist_probe: Mapping[str, object],
    mobile_screenshot_ok: bool,
    products_count: int,
    payment_provider_configured: bool,
) -> dict:
    """Return a PII-free red/green report; no callback here is allowed to mutate."""
    domain = _host(domain)
    if not isinstance(approved_html, str) or not approved_html:
        raise VerificationConfigurationError("approved_html is required")
    if not isinstance(registrar_active, bool) or not isinstance(
        mobile_screenshot_ok, bool
    ):
        raise VerificationConfigurationError("boolean provider probes are invalid")
    if (
        isinstance(products_count, bool)
        or not isinstance(products_count, int)
        or products_count < 0
    ):
        raise VerificationConfigurationError("products_count is invalid")
    if not isinstance(payment_provider_configured, bool):
        raise VerificationConfigurationError("payment provider probe must be boolean")
    waitlist = _no_pii_probe(waitlist_probe)
    allowed_hosts = {domain, f"www.{domain}"}
    cache: dict[str, HTTPResult] = {}

    def get(url: str) -> HTTPResult:
        if url not in cache:
            cache[url] = _validated_result(fetch(url), url, allowed_hosts)
        return cache[url]

    root_url = f"https://{domain}/"
    www_url = f"https://www.{domain}/"
    root = get(root_url)
    www = get(www_url)
    root_text, root_parser = _html(root.body)
    canonical_green = (
        root.status == 200
        and root.tls_valid
        and urlsplit(root.final_url).hostname == domain
        and not root.redirects
        and www.status == 200
        and www.tls_valid
        and urlsplit(www.final_url).hostname == domain
        and bool(www.redirects)
    )
    content_green = approved_html.encode("utf-8") in root.body
    placeholders_green = PLACEHOLDER.search(root_text) is None
    claims_green = not scan_forbidden_claims(root_text)
    links_green = True
    for href in sorted(set(root_parser.links)):
        parts = urlsplit(href)
        if parts.scheme == "mailto" or href.startswith("#"):
            continue
        target = urljoin(root_url, href)
        target_parts = urlsplit(target)
        if target_parts.scheme != "https" or target_parts.hostname not in allowed_hosts:
            links_green = False
            continue
        without_fragment = target_parts._replace(fragment="").geturl()
        link_result = get(without_fragment)
        links_green = links_green and link_result.status < 400 and link_result.tls_valid

    cart = get(f"https://{domain}/cart")
    cart_text, cart_parser = _html(cart.body)
    cart_empty = cart.status in {404, 410} or (
        cart.status == 200
        and "your cart is empty" in re.sub(r"\s+", " ", cart_text).lower()
        and not cart_parser.commerce_controls
    )
    checkout = get(f"https://{domain}/checkout")
    checkout_path = urlsplit(checkout.final_url).path.rstrip("/") or "/"
    checkout_absent = checkout.status in {404, 410} or (
        checkout.status == 200
        and checkout.redirects
        and checkout_path in {"/", "/cart"}
        and cart_empty
    )
    missing_product = get(f"https://{domain}/products/__virgil_missing__")
    commerce_absent = (
        not root_parser.commerce_controls
        and cart_empty
        and checkout_absent
        and missing_product.status in {404, 410}
        and products_count == 0
        and payment_provider_configured is False
    )
    waitlist_green = all(waitlist.values())
    dns_green = _dns_green(dns_lookup, domain, expected_dns)

    checks = (
        (
            "public_ssl_and_redirects",
            canonical_green,
            "apex and www terminate on the pinned HTTPS canonical host",
        ),
        (
            "approved_content",
            content_green,
            "approved page bytes occur unchanged in rendered HTML",
        ),
        ("placeholders", placeholders_green, "no unresolved fact placeholders"),
        ("mobile_viewport", mobile_screenshot_ok, "mobile screenshot probe"),
        (
            "waitlist",
            waitlist_green,
            "test subscriber, consent, confirmation and deletion provider probes",
        ),
        (
            "links",
            links_green,
            "same-store links resolve; mail links remain non-network",
        ),
        (
            "checkout_absent",
            commerce_absent,
            "zero products, no commerce controls, empty reserved cart, no checkout, no payment provider",
        ),
        ("dns", dns_green, "A, AAAA and www CNAME match at two or more resolvers"),
        ("registrar", registrar_active, "registrar reports domain active"),
        (
            "forbidden_claims",
            claims_green,
            "rendered HTML passes the launch claim scanner",
        ),
    )
    report = {
        "checklist": "9.3",
        "checks": [
            {"name": name, "passed": passed, "evidence": evidence}
            for name, passed, evidence in checks
        ],
        "all_green": all(passed for _, passed, _ in checks),
        "checkout_absent_verified": commerce_absent,
        "no_payment_collected": commerce_absent,
        "waitlist": waitlist,
    }
    return report
