"""Merchant adapters for the Restricted Purchase Executor (issue #65).

The executor core must not carry checkout selectors scattered through its
control flow. A ``MerchantAdapter`` is the single place that knows one
merchant's canonical domain, checkout paths, and field selectors. Extraction
of item/total/currency/challenge/success state stays *shared* in the executor
(plain-text regex that works across merchants) — only selectors and identity
are per-merchant here.

V0 live allowlist: **Porkbun only**. Any other domain resolves to ``None`` and
the executor fails closed with ``merchant_not_supported`` before it opens
credential files, fills, or submits. The loopback mock merchant is available
only in fake/staging mode.

The Porkbun selectors below are *provisional*, sourced from the sanitized
fixture named in ``fixture`` — they are a calibration knob, not verified
against Porkbun's real logged-in checkout (which needs an account and is a
Cal-supervised gate). Tests prove they resolve against the fixture; the live
gate verifies them against the real DOM.
"""

from __future__ import annotations

from dataclasses import dataclass

# Logical payment field names — must match the executor's CREDENTIAL_FIELDS and
# the per-merchant selector keys below.
CARD_FIELDS = ("card_number", "card_expiry", "card_cvv", "card_name")


@dataclass(frozen=True)
class MerchantAdapter:
    key: str
    canonical_domain: str
    checkout_paths: tuple[str, ...]
    selectors: dict            # logical field -> CSS selector; includes "submit"
    fixture: str               # sanitized fixture provenance/version
    fixture_sha256: str = ""

    def field_selectors(self) -> list[tuple[str, str]]:
        """(logical_field, selector) for the card fields, in fill order."""
        return [(field, self.selectors[field]) for field in CARD_FIELDS]

    def submit_selector(self) -> str:
        return self.selectors["submit"]


# ponytail: one live merchant, so a module-level dict, not a plugin registry.
# Add the second merchant's adapter here (and a fixture) — no framework needed.
PORKBUN = MerchantAdapter(
    key="porkbun",
    canonical_domain="porkbun.com",
    checkout_paths=("/checkout", "/cart"),
    selectors={
        # Provisional — verify at the Cal live gate against the real checkout.
        "card_number": "input[name='cc-number']",
        "card_expiry": "input[name='cc-exp']",
        "card_cvv": "input[name='cc-cvc']",
        "card_name": "input[name='cc-name']",
        "submit": "button[type='submit']",
    },
    fixture="tests/fixtures/porkbun_checkout_v0.html (sanitized 2026-07-19; "
    "synthetic DOM only — no cookies, tokens, account data, or payment data)",
)

# Loopback mock used only by fake-E2E/staging. Selectors match the mock
# merchant page in scripts/purchase_executor_fake_e2e.py.
MOCK = MerchantAdapter(
    key="mock",
    canonical_domain="mock.local",
    checkout_paths=("/checkout", "/"),
    selectors={
        "card_number": "input[name='card_number']",
        "card_expiry": "input[name='card_expiry']",
        "card_cvv": "input[name='card_cvv']",
        "card_name": "input[name='card_name']",
        "submit": "button[type='submit']",
    },
    fixture="in-repo mock merchant (fake-E2E only)",
)

_LIVE_ALLOWLIST = {PORKBUN.canonical_domain: PORKBUN}


def adapter_for(canonical_domain: str, *, fake_e2e: bool) -> MerchantAdapter | None:
    """Resolve the adapter for a claimed merchant, or ``None`` if unsupported.

    In fake/staging mode every checkout is the loopback mock. In production the
    domain must be on the V0 live allowlist (Porkbun only); anything else →
    ``None`` so the caller fails closed with ``merchant_not_supported``.
    """
    if fake_e2e:
        return MOCK
    return _LIVE_ALLOWLIST.get((canonical_domain or "").strip().lower())


def _demo() -> None:
    assert adapter_for("porkbun.com", fake_e2e=False) is PORKBUN
    assert adapter_for("porkbun.com.evil.test", fake_e2e=False) is None
    assert adapter_for("namecheap.com", fake_e2e=False) is None
    assert adapter_for("anything", fake_e2e=True) is MOCK
    assert [f for f, _ in PORKBUN.field_selectors()] == list(CARD_FIELDS)
    assert PORKBUN.submit_selector()
    print("purchase_merchants demo ok")


if __name__ == "__main__":
    _demo()
