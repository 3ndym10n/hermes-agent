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

from purchase_discovery import FIELD_NAMES as CARD_FIELDS


@dataclass(frozen=True)
class MerchantAdapter:
    key: str
    canonical_domain: str
    checkout_paths: tuple[str, ...]
    processor_origins: tuple[str, ...] = ()
    field_hints: dict[str, tuple[str, ...]] | None = None
    submit_hints: tuple[str, ...] = ()
    fixture: str = ""


# ponytail: one live merchant, so a module-level dict, not a plugin registry.
PORKBUN = MerchantAdapter(
    key="porkbun",
    canonical_domain="porkbun.com",
    checkout_paths=("/checkout", "/cart"),
    processor_origins=(),
    field_hints={},
    fixture="tests/fixtures/porkbun_checkout_v0.html (sanitized 2026-07-19; "
    "prototype merchant shape only — no selectors, secrets, or payment data)",
)

# Loopback mock used only by fake-E2E/staging.
MOCK = MerchantAdapter(
    key="mock",
    canonical_domain="mock.local",
    checkout_paths=("/checkout", "/"),
    field_hints={},
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
    assert not PORKBUN.processor_origins
    assert set((PORKBUN.field_hints or {})).issubset(CARD_FIELDS)
    print("purchase_merchants demo ok")


if __name__ == "__main__":
    _demo()
