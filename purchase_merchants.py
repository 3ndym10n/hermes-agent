"""Merchant policy hints for the Restricted Purchase Executor (issues #65/#73).

Generic semantic discovery is primary. Adapters only bind a merchant domain,
known checkout paths, exact hosted-payment origins, and optional exact wording
hints. They cannot bypass visibility, uniqueness, form-action, origin, or
commercial-term validation.

V0 live allowlist: **Porkbun only**. Any other domain resolves to ``None`` and
the executor fails closed with ``merchant_not_supported`` before it opens
credential files, fills, or submits. The loopback mock merchant is available
only in fake/staging mode.

No normal workflow requires Cal to inspect merchant HTML or maintain CSS
selectors. Porkbun's hosted-processor allowlist remains empty until a real,
non-purchase integration establishes an exact origin; cross-origin fields fail
closed meanwhile.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from purchase_discovery import FIELD_NAMES as CARD_FIELDS


@dataclass(frozen=True)
class MerchantAdapter:
    key: str
    canonical_domain: str
    checkout_paths: tuple[str, ...]
    cart_paths: dict[str, str] | None = None
    fake_session_handoff_path: str = ""
    processor_origins: tuple[str, ...] = ()
    field_hints: dict[str, tuple[str, ...]] | None = None
    submit_hints: tuple[str, ...] = ()
    fixture: str = ""


# ponytail: one live merchant, so a module-level dict, not a plugin registry.
PORKBUN = MerchantAdapter(
    key="porkbun",
    canonical_domain="porkbun.com",
    checkout_paths=("/checkout", "/cart"),
    cart_paths={},
    processor_origins=(),
    field_hints={},
    fixture="tests/fixtures/porkbun_checkout_v0.html (sanitized 2026-07-19; "
    "prototype merchant shape only — no selectors, secrets, or payment data)",
)

# Loopback mock used only by fake-E2E/staging.
MOCK = MerchantAdapter(
    key="mock",
    canonical_domain="mock.local",
    checkout_paths=("/checkout", "/cart", "/"),
    cart_paths={
        "domain_registration": "/checkout?product_kind=domain_registration&product_id={product_id}&quantity={quantity}",
        "merchant_sku": "/checkout?product_kind=merchant_sku&product_id={product_id}&quantity={quantity}",
    },
    fake_session_handoff_path="/__hermes_session_handoff",
    field_hints={},
    fixture="in-repo mock merchant (fake-E2E only)",
)

_LIVE_ALLOWLIST = {PORKBUN.canonical_domain: PORKBUN}


def cart_path(adapter: MerchantAdapter, product_kind: str, product_id: str, quantity: int) -> str:
    """Build the exact adapter-owned cart path, or fail closed with ``""``."""
    template = (adapter.cart_paths or {}).get(product_kind)
    if not template or not product_id or isinstance(quantity, bool) or quantity < 1:
        return ""
    return template.format(product_id=quote(product_id, safe=""), quantity=quantity)


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
    assert not PORKBUN.processor_origins
    assert not cart_path(PORKBUN, "domain_registration", "example.com", 1)
    assert cart_path(MOCK, "domain_registration", "example.com", 2) == (
        "/checkout?product_kind=domain_registration&product_id=example.com&quantity=2"
    )
    assert set((PORKBUN.field_hints or {})).issubset(CARD_FIELDS)
    print("purchase_merchants demo ok")


if __name__ == "__main__":
    _demo()
