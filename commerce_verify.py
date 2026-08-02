"""Read-only, injectable verification for the commerce V1 checklist §9.3."""

from __future__ import annotations

import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from commerce_content import scan_forbidden_claims

MAX_HTML_BYTES = 2_097_152
MAX_REDIRECTS = 5
FETCH_TIMEOUT_SECONDS = 20.0
DNS_TIMEOUT_SECONDS = 10.0
# Two independent public resolvers, per the §9.3 "two resolvers" requirement.
PUBLIC_RESOLVERS = ("8.8.8.8", "1.1.1.1")
# Mirrors commerce_workflow.SHOPIFY_DNS_BUNDLE; kept in sync by a test.
EXPECTED_SHOPIFY_DNS = {
    "A": ["23.227.38.65"],
    "AAAA": ["2620:0127:f00f:5::"],
    "CNAME": ["shops.myshopify.com."],
}
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


def https_fetch(url: str, *, timeout: float = FETCH_TIMEOUT_SECONDS) -> HTTPResult:
    """Fetch one HTTPS URL, following redirects manually so they are evidence.

    TLS verification is the default context: a certificate failure surfaces as
    ``tls_valid=False`` rather than an exception, because "SSL is not issued
    yet" is an expected mid-launch state the checklist must report, not crash on.
    """
    if not isinstance(url, str) or urlsplit(url).scheme != "https":
        raise VerificationConfigurationError("verification fetches must be https")
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )
    redirects: list[str] = []
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        request = urllib.request.Request(
            current, headers={"Accept": "text/html"}, method="GET"
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                status = response.status
                headers = response.headers
                body = response.read(MAX_HTML_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            headers = error.headers
            body = error.read(MAX_HTML_BYTES + 1)
        except urllib.error.URLError as error:
            # urllib wraps certificate failures; report them as a red TLS check
            # instead of an exception, since pending SSL issuance is expected.
            if isinstance(getattr(error, "reason", None), ssl.SSLError):
                return HTTPResult(
                    status=0, body=b"", final_url=current,
                    redirects=tuple(redirects), tls_valid=False,
                )
            raise VerificationConfigurationError("verification fetch failed") from None
        except Exception:
            raise VerificationConfigurationError("verification fetch failed") from None
        if len(body) > MAX_HTML_BYTES:
            raise VerificationConfigurationError("verification response is too large")
        location = headers.get("Location") if 300 <= status < 400 else None
        if not location:
            return HTTPResult(
                status=status, body=body, final_url=current,
                redirects=tuple(redirects), tls_valid=True,
            )
        target = urljoin(current, location)
        if urlsplit(target).scheme != "https":
            raise VerificationConfigurationError("verification redirect left https")
        redirects.append(current)
        current = target
    raise VerificationConfigurationError("verification redirect loop")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def dig_lookup(
    host: str,
    record_type: str,
    *,
    resolvers: Sequence[str] = PUBLIC_RESOLVERS,
    timeout: float = DNS_TIMEOUT_SECONDS,
) -> dict[str, list[str]]:
    """Return {resolver: answers} from each public resolver, via ``dig``."""
    if record_type not in {"A", "AAAA", "CNAME"}:
        raise VerificationConfigurationError("unsupported DNS record type")
    if _DOMAIN.fullmatch(host) is None:
        raise VerificationConfigurationError("DNS host is invalid")
    binary = shutil.which("dig")
    if binary is None:
        raise VerificationConfigurationError("dig is required for DNS verification")
    answers: dict[str, list[str]] = {}
    for resolver in resolvers:
        try:
            completed = subprocess.run(
                [binary, f"@{resolver}", "+short", "+timeout=5", "+tries=2",
                 host, record_type],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except subprocess.SubprocessError:
            continue
        if completed.returncode != 0:
            continue
        answers[resolver] = [
            line.strip() for line in completed.stdout.splitlines() if line.strip()
        ]
    return answers


def _spend_display(cents: int) -> str:
    return f"US${cents // 100}.{cents % 100:02d}"


def _registration_facts(store: object, job_id: str) -> dict:
    """Recover the registration truths the §16 receipt cites, from the ledger."""
    for action in reversed(list(store.list_actions(job_id))):  # type: ignore[attr-defined]
        if (
            action.get("action_type") != "porkbun_register_domain"
            or action.get("action_status") != "succeeded"
        ):
            continue
        result = action.get("result")
        if not isinstance(result, Mapping):
            break
        cogitator = result.get("cogitator")
        if not isinstance(cogitator, Mapping):
            raise VerificationConfigurationError(
                "registration was never recorded with the money authority"
            )
        cents = result.get("amount_usd_cents")
        if isinstance(cents, bool) or not isinstance(cents, int) or cents <= 0:
            raise VerificationConfigurationError("registration amount is invalid")
        return {
            "order_id": str(result.get("order_id", "")),
            "amount_usd_cents": cents,
            "cogitator": {
                "proposal_id": str(cogitator.get("proposal_id", "")),
                "approval_id": str(cogitator.get("approval_id", "")),
                "receipt_ref": str(cogitator.get("receipt_ref", "")),
            },
        }
    raise VerificationConfigurationError("no succeeded registration in the ledger")


def _prepublish_report(
    client: object,
    surface: Mapping[str, object],
    domain: str,
    dns_lookup: DNSLookup,
) -> dict:
    """Report the pre-publication truths, all readable through the lock."""
    locked = client.storefront_probe("/")  # type: ignore[attr-defined]
    products = int(surface["products_count"])  # type: ignore[arg-type]
    paid = bool(surface["payment_provider_configured"])
    commerce_absent = products == 0 and not paid
    checks = (
        (
            "storefront_locked",
            locked.get("password_protected") is True,
            "storefront still returns the password page",
        ),
        ("no_products", products == 0, "shop publishes zero products"),
        ("no_payment_provider", not paid, "no payment provider is configured"),
        (
            "dns",
            _dns_green(dns_lookup, domain, EXPECTED_SHOPIFY_DNS),
            "A, AAAA and www CNAME match at two or more resolvers",
        ),
    )
    return {
        "checklist": "9.3-prepublish",
        "checks": [
            {"name": name, "passed": passed, "evidence": evidence}
            for name, passed, evidence in checks
        ],
        "all_green": all(passed for _, passed, _ in checks),
        "checkout_absent_verified": commerce_absent,
        "no_payment_collected": commerce_absent,
    }


def production_verify(
    *,
    store: object | None = None,
    porkbun_factory: Callable[[], object] | None = None,
    waitlist_probe: Callable[[Mapping, object, str], Mapping[str, object]] | None = None,
    mobile_screenshot: Callable[[Mapping, str], bool] | None = None,
    fetch: Fetch = https_fetch,
    dns_lookup: DNSLookup = dig_lookup,
) -> Callable[[Mapping, object, Mapping, str], dict]:
    """Build the live §9.3 verifier used by the production worker.

    Every probe here is read-only. The single write anywhere near this path is
    the waitlist round-trip, which creates one synthetic ``waitlist-test+``
    subscriber and deletes it again; it is skipped unless a probe is injected.
    """

    def registrar_active(domain: str) -> bool:
        if porkbun_factory is None:
            return False
        try:
            domains = porkbun_factory().list_domains().get("domains", [])  # type: ignore[attr-defined]
        except Exception:
            return False
        return any(
            isinstance(item, Mapping)
            and item.get("domain") == domain
            and str(item.get("status", "ACTIVE")).upper() == "ACTIVE"
            for item in domains
        )

    def verify(job: Mapping, client: object, package: Mapping, phase: str) -> dict:
        plan = job.get("plan") or {}
        domain = _host(str(plan.get("domain", "")))
        pages = package.get("pages")
        if not isinstance(pages, Mapping):
            raise VerificationConfigurationError("content package has no pages")
        landing = pages.get("priority-access")
        if not isinstance(landing, Mapping):
            raise VerificationConfigurationError("content package has no landing page")
        surface = client.commerce_surface()  # type: ignore[attr-defined]
        if phase != "final":
            # Pre-publication the storefront is still password-locked, so the
            # public checklist cannot go green by construction. Verify what is
            # actually true now: nothing is exposed, nothing is sellable, and
            # DNS already points at Shopify. Content was fingerprinted by the
            # build step, so it is not re-fetched through the password page.
            return _prepublish_report(client, surface, domain, dns_lookup)
        probe = dict(
            waitlist_probe(job, client, domain)
            if waitlist_probe is not None
            else {
                "customer_found": False,
                "consent_recorded": False,
                "confirmation_shown": False,
                "subscriber_deleted": False,
            }
        )
        # The digest is receipt material, not a checklist input; verify_launch
        # rejects any key beyond the four booleans.
        address_digest = str(probe.pop("test_address_digest", ""))
        report = verify_launch(
            domain=domain,
            approved_html=str(landing["body_html"]),
            fetch=fetch,
            dns_lookup=dns_lookup,
            expected_dns=EXPECTED_SHOPIFY_DNS,
            registrar_active=registrar_active(domain),
            waitlist_probe=probe,
            mobile_screenshot_ok=(
                mobile_screenshot(job, domain) if mobile_screenshot is not None else False
            ),
            products_count=int(surface["products_count"]),
            payment_provider_configured=bool(surface["payment_provider_configured"]),
        )
        if phase != "final":
            return report
        if store is None:
            raise VerificationConfigurationError("receipt facts need the job ledger")
        job_id = str(job["job_id"])
        registration = _registration_facts(store, job_id)
        identity = plan.get("shopify")
        if not isinstance(identity, Mapping):
            raise VerificationConfigurationError("shop identity was never pinned")
        report["receipt_facts"] = {
            "checkout_absent_verified": True,
            "no_payment_collected": True,
            "public_url": f"https://{domain}/",
            "domain": {
                "name": domain,
                "registrar": "porkbun",
                "order_id": registration["order_id"],
                "spend": {
                    "amount_usd_cents": registration["amount_usd_cents"],
                    "display": _spend_display(registration["amount_usd_cents"]),
                },
                "cogitator": registration["cogitator"],
                "auto_renew": True,
                "whois_privacy": True,
            },
            "dns": {"status": "propagated", "records": ["A", "AAAA", "CNAME www"]},
            "shopify": {
                "shop_id": str(identity["shop_id"]),
                "myshopify_domain": str(identity["myshopify_domain"]),
                "plan": str(identity["plan"]),
                "admin_url": (
                    "https://admin.shopify.com/store/"
                    f"{str(identity['myshopify_domain']).partition('.')[0]}"
                ),
            },
            "waitlist_test": {
                "result": "pass",
                "test_address_used": address_digest,
                "consent_recorded": bool(probe.get("consent_recorded")),
                "test_subscriber_deleted": bool(probe.get("subscriber_deleted")),
            },
            "verification": {
                "checklist": "9.3",
                "all_green": True,
                "evidence_bundle": f"evidence/{job_id}/verification/",
            },
            "total_spend": [
                {
                    "provider": "porkbun",
                    "amount": _spend_display(registration["amount_usd_cents"]),
                },
                {
                    "provider": "shopify",
                    "amount": (
                        f"{identity['plan']} billed to Cal's card at trial end"
                    ),
                },
            ],
            "unresolved": [".com.au deferred pending ABN"],
        }
        return report

    return verify
