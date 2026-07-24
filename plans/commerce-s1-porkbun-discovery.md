# Commerce S1: Porkbun API discovery

Retrieved 2026-07-24. This is documentation-only discovery. No authenticated
Porkbun request, registration, mutation, purchase, or account change was made.

## Official sources

- [Porkbun API v3.9 documentation](https://porkbun.com/api/json/v3/documentation)
- [Official OpenAPI 3.0 specification](https://porkbun.com/api/json/v3/spec)
- [Official Porkbun MCP safety and funding guide](https://kb.porkbun.com/article/296-how-to-install-the-porkbun-mcp-server-in-claude-desktop)
- Official TLD pages for [.com](https://porkbun.com/tld/com),
  [.net](https://porkbun.com/tld/net), and
  [.com.au](https://porkbun.com/tld/com.au)

## Registration contract (not implemented in S1)

The exact registration endpoint is `POST /domain/create/{domain}` at
`https://api.porkbun.com/api/json/v3`. The OpenAPI request schema requires:

- authentication via `X-API-Key` and `X-Secret-API-Key` headers, or the
  `apikey` and `secretapikey` body fields;
- `cost`: integer USD cents, exactly matching the total quote from
  `/domain/checkDomain/{domain}` for the registry-minimum duration;
- `agreeToTerms`: `"yes"` or `"1"`.

Optional fields are `whoisPrivacy` and `dryRun`. Registration requires a
verified account email and phone, sufficient account credit, at least one
previous domain registration, a currently available non-premium domain, and
the exact current cost. Registrations use the registry-minimum term.

`dryRun: true` is an official validation-only mode. It runs availability,
price/cost, eligibility, funds, and spend-limit checks without creating an
order, charging, registering, or consuming the create rate-limit budget. The
preview includes `wouldSucceed`, `operation`, `domain`, `tld`, availability,
premium status, duration, `cost`, `costDisplay`, `balance`,
`sufficientFunds`, and configured monthly-spend-limit fields.

S1 deliberately implements neither live registration nor its dry run. Both
are mutation-surface operations and belong behind later governance.

## Funding, cost, and evidence

Official documentation states that registrations, renewals, and inbound
transfers draw down Porkbun account credit. The live registration response
documents `domain`, charged `cost` in cents, `orderId`, remaining `balance`,
rate-limit state, `ttlRemaining`, and `requestId`. A successful
`domain.registered` webhook also carries an event ID, timestamp, domain, TLD,
and expiry date.

The specification documents per-account monthly API spend limits,
low-balance alerts, and auto top-up. It does not establish from public
documentation whether auto top-up uses a stored card, which stored instrument
is selected, or when it can run. Those payment-instrument details remain
unresolved and must not be inferred.

## Idempotency and limits

All v3 POST endpoints except partner-only routes accept `Idempotency-Key`.
Keys are non-empty strings up to 255 characters. Porkbun retains the response
for 24 hours: the same key and body replays the original response; a changed
body or in-flight duplicate returns HTTP 409.

Documented domain limits are:

- availability: default one check per 10 seconds per account, configurable
  per API key, with current state returned in `limits`;
- registration: default one attempt per 10 seconds per account and 50
  successful registrations per 86,400 seconds per account, both configurable
  per API key;
- other endpoints may return HTTP 429 and rate-limit headers, but the public
  specification does not state one universal numeric limit.

An API key can be restricted to exact source IPv4/IPv6 addresses or CIDR
ranges and exact target domains. Disallowed calls return `IP_NOT_ALLOWED` or
`DOMAIN_NOT_ALLOWED`. The documented monthly spend cap is per account, not
per key; no separate per-key spend cap is established by the public
documentation.

## TLD findings

Porkbun's official TLD pages show that `.com`, `.net`, and `.com.au` are
offered for registration. The API provides the authoritative read-only
`GET /domain/getRegistrationRequirements/{tld}` response, including
`apiRegisterable`, registration duration, privacy/address flags, request JSON
Schema, registry eligibility Schema, and a reason when API registration is
not supported.

The public specification does not enumerate the current `apiRegisterable`
value for `.com`, `.net`, or `.com.au`. Therefore:

| TLD | Offered by Porkbun | API-registerable |
|---|---:|---|
| `.com` | yes | pending authenticated read-only check |
| `.net` | yes | pending authenticated read-only check |
| `.com.au` | yes | pending authenticated read-only check |

No TLD-specific eligibility, especially `.com.au` registrant eligibility, is
assumed until the official requirements response is read in a later approved
live-read window.

## Unresolved authenticated read-only checks

The following require a later explicitly approved authenticated read-only
session:

- the current requirements responses and `apiRegisterable` flags for `.com`,
  `.net`, and `.com.au`;
- the account's actual API-key IP/domain scopes and configurable rate limits;
- the account's balance, monthly spend controls, and auto-top-up configuration;
- whether the account satisfies the prior-registration and verification
  prerequisites;
- the exact account-history evidence available in addition to documented
  `orderId`, `requestId`, and registration webhooks.

No authenticated check is needed or permitted to complete S1 code review.
