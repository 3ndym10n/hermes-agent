# Commerce live discovery — 2026-08-02

This is the sanitized WP0 discovery record required by
`docs/VIRGIL_ECOMMERCE_OPERATOR_MASTER_PLAN.md`. Discovery was read-only. No
provider mutation, purchase, DNS change, contractual acceptance, or public
publication occurred.

## Repository baseline

- The authoritative plan was merged on `main` at `c0ef275904`.
- Commerce job-store PR #88 was rebased, independently audited, fully green in
  CI, and merged on `main` at `e6c687e857`.
- The two superseded untracked plan drafts remain untouched in the original
  worktree.

## Porkbun

- Porkbun credential material was absent from the permitted Hermes runtime
  environment. No credential values were requested, read, retained, or logged.
- The adapter's loopback `--check` path passed.
- A public pricing attempt reached the provider but failed closed in the strict
  response validator because `pricing.ws.coupons` was not an object. The
  rejected body was not retained.
- The authenticated `ping`, registration requirements for `com`, `net`, and
  `com.au`, account-domain list, exact pricing, and `domain/create` dry-run
  balance checks are deferred to WP5's documented key gate.
- The current purchase browser executor is not checkout-ready without a human
  login. If the API leg is unavailable, the plan's C-P4 viewer contingency is
  required.

The only canonical candidate selected by the approved content package is
`siliconcurrent.com`; availability, collision evidence, and an exact quote
remain provider truth to collect before any approval packet is created.

## Cogitator policy gap [U-1]

- The deployed purchase-operator bridge is disabled, so no purchase-status or
  balance action was invoked.
- The deployed policy supports an AUD-only ordinary purchase class, while
  Porkbun returns and charges an exact USD amount. Reusing that policy would
  lose exact-currency and exact-cents binding.
- The single additive policy insert permitted by the master plan is therefore
  necessary: a USD-only domain-registration class with a US$30.00 total and
  per-purchase ceiling, one ordinary non-premium domain, its disclosed annual
  auto-renewal, exact-cents approval binding, ticket binding, and separate
  AUD/USD ledgers.
- That insert was not applied because creating financial authorization requires
  Cal's explicit bound policy approval. Hermes engineering continues through
  WP1–WP4; WP5 remains fail-closed until the policy and provider-key gates are
  satisfied.

## Contingencies exercised

- Missing provider credentials: authenticated discovery moved behind WP5's key
  gate, as required by WP0 and §13.
- API registration unavailable or first-registration-manual: use the existing
  browser executor when it becomes checkout-ready, otherwise C-P4 in the gate
  viewer. Neither contingency authorizes an irreversible click.
- Cogitator bridge disabled: build and test pure packet/transport wiring with
  fakes; no live purchase action is possible until the bridge and policy are
  explicitly enabled.
