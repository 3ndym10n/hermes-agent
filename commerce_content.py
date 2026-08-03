"""Deterministic, claim-gated content for the V1 GPU priority-access site."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping

BRAND = "Warp Supply"
FORBIDDEN_CLAIM_TERMS = (
    "supplier cost",
    "90% discount",
    "stock guarantee",
    "inventory guarantee",
    "delivery date",
    "shipping window",
    "Australian availability",
    "manufacturer relationship",
    "warranty",
    "refund terms",
    "performance claims",
    "final retail price",
    "supplier authenticity",
)

_REQUIRED_FACTS = frozenset({
    "contact_email",
    "business_identity_sentence",
    "double_opt_in",
    "brand_signoff",
    "privacy_signoff",
})
_EMAIL = re.compile(
    r"(?a)^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+$", re.I
)
_PLACEHOLDER = re.compile(r"⟨[^⟩]+⟩")
_CLAIM_PATTERNS = {
    **{term: re.compile(re.escape(term), re.I) for term in FORBIDDEN_CLAIM_TERMS},
    "stated price": re.compile(r"(?i)(?:\b(?:AUD|USD)\s*|\$)\d+(?:\.\d{1,2})?\b"),
    "in-stock claim": re.compile(r"(?i)\b(?:in stock|stock is available)\b"),
    "availability guarantee": re.compile(
        r"(?i)\bguarante(?:e|ed|es|eing)\s+(?:stock|inventory|availability|allocation)\b"
    ),
    "delivery promise": re.compile(
        r"(?i)\b(?:deliver(?:y|ed)?|ships?)\s+(?:by|on|within)\b"
    ),
    "Australian availability claim": re.compile(r"(?i)\bavailable\s+in\s+Australia\b"),
    "manufacturer affiliation claim": re.compile(
        r"(?i)\b(?:official|authori[sz]ed)\s+(?:AMD|manufacturer)\b"
    ),
    "performance claim": re.compile(
        r"(?i)\b(?:benchmark(?:s|ed|ing)?|\d+(?:\.\d+)?\s*(?:fps|%\s*faster))\b"
    ),
    "supplier-authenticity claim": re.compile(
        r"(?i)\b(?:verified|authentic|genuine)\s+supplier\b"
    ),
}


class ContentBuildError(ValueError):
    """A launch fact or rendered package violates the approved content boundary."""


def scan_forbidden_claims(text: str) -> tuple[str, ...]:
    """Return stable rule names for prohibited commercial claims in *text*."""
    if not isinstance(text, str):
        raise ContentBuildError("content must be text")
    return tuple(
        name for name, pattern in _CLAIM_PATTERNS.items() if pattern.search(text)
    )


def assert_claim_free(text: str) -> None:
    matches = scan_forbidden_claims(text)
    if matches:
        raise ContentBuildError(f"forbidden launch claim: {', '.join(matches)}")
    if _PLACEHOLDER.search(text):
        raise ContentBuildError("unresolved launch-content placeholder")


def _facts(raw: Mapping[str, object]) -> tuple[str, str, bool]:
    if not isinstance(raw, Mapping) or set(raw) != _REQUIRED_FACTS:
        raise ContentBuildError(
            "launch facts must contain exactly the approved V1 fields"
        )
    contact = raw["contact_email"]
    identity = raw["business_identity_sentence"]
    double_opt_in = raw["double_opt_in"]
    if (
        not isinstance(contact, str)
        or len(contact) > 254
        or not _EMAIL.fullmatch(contact)
    ):
        raise ContentBuildError("contact_email is invalid")
    if (
        not isinstance(identity, str)
        or not 1 <= len(identity) <= 280
        or identity != identity.strip()
        or any(ord(character) < 32 for character in identity)
    ):
        raise ContentBuildError("business_identity_sentence is invalid")
    if not isinstance(double_opt_in, bool):
        raise ContentBuildError("double_opt_in must be boolean")
    if raw["brand_signoff"] is not True or raw["privacy_signoff"] is not True:
        raise ContentBuildError("brand and privacy sign-off are required")
    assert_claim_free(contact)
    assert_claim_free(identity)
    return contact, identity, double_opt_in


def build_content(facts: Mapping[str, object]) -> dict:
    """Compile the approved package; identical facts produce identical bytes."""
    contact, identity, double_opt_in = _facts(facts)
    safe_contact = html.escape(contact, quote=True)
    safe_identity = html.escape(identity, quote=True)
    body = "\n".join((
        '<section id="priority-access">',
        "<p>Priority access list — no payment, no obligation.</p>",
        "<h1>AMD Radeon RX 9070 XT 16GB — priority access for Australia.</h1>",
        "<p>Join the list. When our first allocation is confirmed, members get first access, in order of signup.</p>",
        '<a href="#priority-signup">Join the priority list</a>',
        "<h2>How it works</h2>",
        "<ol><li>Join with your email.</li><li>We confirm your spot instantly.</li><li>When allocation is confirmed, you get an email with your access window.</li></ol>",
        "<p>No payment is taken on this site.</p>",
        "<ul><li>No payment now — joining is free.</li><li>First come, first served.</li><li>Unsubscribe anytime.</li><li>We only email you about GPU access.</li></ul>",
        "<h2>Frequently asked questions</h2>",
        "<h3>Is this a purchase?</h3><p>No — this is a free waitlist; no checkout exists on this site.</p>",
        "<h3>When will cards be available?</h3><p>We don't publish dates until an allocation is confirmed.</p>",
        "<h3>What is the price?</h3><p>It will be announced to the list when confirmed.</p>",
        f"<h3>Who are you?</h3><p>{safe_identity}</p>",
        '<section id="priority-signup">',
        "<h2>Join the priority list</h2>",
        "<p>Email me about AMD GPU priority access from Warp Supply. Unsubscribe anytime.</p>",
        "</section>",
        "<p>You're on the list. We'll email you only about AMD GPU priority access. Unsubscribe anytime.</p>",
        "<footer>",
        "<p>Warp Supply is an independent Australian retailer-in-formation. AMD and Radeon are trademarks of Advanced Micro Devices, Inc. This site is not affiliated with or endorsed by AMD. Joining the list is free and creates no obligation for either party. No payments are accepted on this site.</p>",
        f'<p>Contact: <a href="mailto:{safe_contact}">{safe_contact}</a></p>',
        '<p><a href="/policies/privacy-policy">Privacy Policy</a> · <a href="/pages/contact">Contact</a></p>',
        "</footer>",
        "</section>",
    ))
    contact_body = f'<p>{safe_identity}</p>\n<p>Email: <a href="mailto:{safe_contact}">{safe_contact}</a></p>'
    package = {
        "brand": BRAND,
        "announcement": "Priority access list — no payment, no obligation.",
        "pages": {
            "priority-access": {
                "title": "AMD Radeon RX 9070 XT 16GB priority access",
                "body_html": body,
                "is_published": True,
            },
            "contact": {
                "title": "Contact",
                "body_html": contact_body,
                "is_published": True,
            },
        },
        "navigation": {
            "handle": "footer",
            "title": "Footer",
            "items": [
                {
                    "title": "Privacy Policy",
                    "type": "HTTP",
                    "url": "/policies/privacy-policy",
                },
                {"title": "Contact", "type": "HTTP", "url": "/pages/contact"},
            ],
        },
        "email_signup": {
            "native_dawn_section": True,
            "anchor": "priority-signup",
            "consent": "Email me about AMD GPU priority access from Warp Supply. Unsubscribe anytime.",
            "confirmation": "You're on the list. We'll email you only about AMD GPU priority access. Unsubscribe anytime.",
            "double_opt_in": double_opt_in,
            "prechecked": False,
        },
        "privacy_policy": {"source": "shopify_generated", "reviewed": True},
    }
    assert_claim_free(json.dumps(package, ensure_ascii=False, sort_keys=True))
    return package


def canonical_content_bytes(package: Mapping[str, object]) -> bytes:
    return json.dumps(
        package, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_fingerprint(package: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_content_bytes(package)).hexdigest()
