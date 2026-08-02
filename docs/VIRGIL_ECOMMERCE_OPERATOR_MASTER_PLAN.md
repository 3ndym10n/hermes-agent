# Virgil Ecommerce Operator — Master Implementation & Launch Plan (V1)

**Produced 2026-08-02 by Fable (Claude Code). This is the single canonical, authoritative
execution package for Codex.** No other plan document governs this work; competing or
prior plan files are superseded. Sections 1–18 are the master plan; §§19–25 are the
operational runbooks, acceptance test, checklists, and exact execution/review prompts;
**Appendix A** carries the full 2026-07-24 second-pass planning report ("v2 report"),
merged here so its repository evidence, governance analysis, and security patterns remain
citable without a second document.

This plan supersedes the launch-shape decisions of the v2 report (Appendix A): the
validation method is a **no-payment waitlist**, not a full-payment preorder, and this
plan carries the human-gate/browser-handoff design the v2 report did not contain. The v2
report's repository evidence, governance analysis, and security patterns remain valid;
wherever the sections below cite "the v2 report", that citation resolves to Appendix A of
this document rather than to any separate file.

Evidence tags: **[V]** verified in repository/GitHub/runtime this session (2026-08-02) ·
**[D <date>]** verified in current official provider documentation on that date ·
**[I]** engineering inference · **[U]** unresolved, resolved by a named task in §11.

Business target: Australia · hero product **AMD Radeon RX 9070 XT 16GB** · offer =
priority-access waitlist · **no checkout, deposits, preorders, or paid reservations** ·
default platform Shopify · preferred registrar Porkbun · brand recommendation
"Silicon Current" subject to checks · candidate domains listed in §9.1.

---

## 1. Executive decision

**The complete acceptance test is achievable on the current server.** Every hard
prerequisite already exists and was verified live this session: Telegram entry with a
fail-closed user allowlist [V], a merged read-only Porkbun adapter (PR #85) [V], a complete
durable commerce job store + state machine sitting in **open PR #88** (3,047 lines,
mergeable, clean worktree) [V], a ticket-based money-governance control plane deployed in
Cogitator on Railway [V], a local Chromium automation stack (agent-browser) with named
sessions, persistent `--profile` directories, and a CDP endpoint [V], and a
**tailnet-only, Tailscale-identity-authenticated private web surface already running on
Cal's phone** (Virgil Mobile, `https://virgil-server.tailce4511.ts.net:8443` → loopback
8788, `Tailscale-User-Login` pinned to `bacon.calebz@gmail.com`) [V][D 2026-08-02].

**What can be completed immediately (no new code):** merge PR #88 after the §8 state
amendment; park the superseded branches (§3.3); run the authenticated **read-only** Porkbun
discovery (apiRegisterable flags, balance, key scopes) with the existing adapter.

**What must be implemented (four stages, one integration branch, §11):**
(1) the commerce operator worker + Telegram wiring (job creation from chat, gate cards,
approval buttons, status);
(2) the human-gate handoff: a CDP-screencast session viewer added to the existing Virgil
Mobile server behind the existing Tailscale identity boundary;
(3) Porkbun mutation legs (DNS writes + `domain/create` with `dryRun` and
`Idempotency-Key`) behind the existing Cogitator purchase governance;
(4) the Shopify Admin adapter (GraphQL `2026-07`), grounded content package, verification
engine, and launch/receipt flow.

**What requires Cal (complete list, nothing else):** Telegram approvals (domain+price, DNS
where flagged, publication); creating the Shopify account/store and picking a paid plan
with billing card (no store-creation API exists [D 2026-08-02]; password removal requires a
chosen plan [D 2026-08-02]); creating the Shopify custom app and staging its token; any
login/2FA/CAPTCHA/KYC challenge, completed in the gate viewer; supplying business facts
(brand sign-off, contact email, privacy contact); the ABN decision — **without an
ABN/ACN, `.com.au` is not registrable** (auDA Australian-presence rule [D 2026-08-02]),
so V1 defaults to `.com`.

**Critical path:** merge #88 → Stage 1 worker/Telegram → Stage 2 gate viewer → Stage 3
Porkbun legs → live domain registration (first live acceptance) → Cal's Shopify store gate
→ Stage 4 build/verify → plan+publish gates → live. Stages 3 and 4 are parallelizable
after Stage 1; Stage 2 is needed before any gate that requires the shared browser.

**Expected external waiting points:** DNS propagation ≤48 h and SSL issuance after domain
connection [D 2026-08-02]; Shopify email/phone verification during signup; Porkbun account
email+phone verification and the **first-registration-must-be-manual rule** ("at least one
previous domain registration" before API create [D 2026-07-24]) — if Cal's Porkbun account
has never registered a domain, the first registration happens through the gate viewer on
Porkbun's website instead (contingency C-P4, no schedule impact).

**Recommended final architecture (the one recommendation):** Hermes owns everything
operational — one SQLite job store (`commerce_jobs.py`, PR #88), one long-running user-
service worker (`commerce_operator.py`) that executes deterministic steps against provider
adapters and one persistent agent-browser session per job, a gateway watcher that renders
job events/gates/approvals into Cal's existing Telegram chat, and a gate viewer mounted in
the existing Virgil Mobile server for human handoff. Cogitator remains exactly what it is
today: the money authority (proposal → exact-quote approval → single-use ticket → receipt)
and the approved-facts store, reached over the existing Railway bridge. No new frameworks,
no new datastores beyond the #88 database, no new public exposure, at most one new
loopback port.

---

## 2. Product boundary — what "Virgil is up to par" means for V0

**Virgil performs automatically (safe/reversible):** job creation and resumption from a
plain Telegram sentence; loading approved launch facts; Porkbun availability + pricing
checks for all candidate domains; brand/domain recommendation with evidence; dry-run
registration previews; DNS record creation on the purchased domain (Shopify A/AAAA/CNAME);
all Shopify Admin-API reads and idempotent content upserts (pages, theme settings, policies,
navigation) against the password-protected store; waitlist form configuration via Dawn's
native email-signup section; every verification check in §9.3; screenshots and evidence
capture; the final receipt.

**Requires Cal (each is a recorded human gate):** exact domain+price purchase approval;
Shopify account/store creation, plan selection, billing card; custom-app token staging;
login/2FA/CAPTCHA/KYC challenges (completed in the gate viewer); DNS-mutation approval
where the mandate requires it (V1 policy: the initial record creation on the freshly
purchased, otherwise-empty zone is bundled into the domain-purchase approval packet; any
later deletion/change of existing records needs its own diff approval); contractual/ToS
acceptances; final publication approval.

**Prohibited in V0 (hard-fail, not configurable):** enabling checkout, payments, deposits,
preorders, or paid reservations; publishing any claim from the mandate's forbidden list
(supplier cost, "90% discount", stock/inventory guarantees, delivery dates, shipping
windows, Australian availability, manufacturer relationship, warranty, refund terms,
performance claims, final retail price, supplier authenticity) — the content builder
refuses any copy containing an ungrounded claim (test T-content); registering `.com.au`
without a verified ABN/ACN; any secret through Telegram; any provider mutation without its
recorded approval; retrying an irreversible action.

**After a human gate:** job and browser state are preserved; Cal gets one private link +
one plain-English action; after Cal's DONE (button in the viewer or reply in Telegram),
Virgil **verifies gate completion from provider truth** (never from the DONE alone),
invalidates the link, and resumes the same job and browser session automatically.

**Completed means:** the §16 receipt is persisted and delivered — public URL live on the
purchased domain with SSL, waitlist signup verified end-to-end with a test subscriber,
no checkout path reachable, all gates and spend itemized.

**Explicitly deferred (not failures):** `.com.au` (pending ABN); checkout/preorders and
everything payment-side; supplier verification workflow; multi-store; theme development
beyond Dawn settings; renewal automation (renewal **alerting** via Cogitator
`purchase_assets` stays in scope); file-upload support inside the gate viewer (V0 gates
are designed to avoid uploads; KYC uploads happen on Cal's own device, C-B8).

---

## 3. Current capability map and evidence table

### 3.1 Evidence table (every relevant component, inspected this session)

Columns: purpose · entrypoint/caller · reachability · lifecycle · tests · state written ·
network effect · credentials · verdict (**reuse** = unchanged / **extend** / **wire** /
**merge-first** / **supersede** / **park**).

| Component (path) | Purpose · entrypoint · reachability | Lifecycle · tests | State · network · creds | Verdict + evidence |
|---|---|---|---|---|
| `gateway/run.py` (17,515 ln) + `gateway/platforms/telegram.py` | Telegram/Discord gateway; systemd **system** unit `hermes-gateway.service`, running [V] | merged · `tests/gateway/*` pass in CI (`tests.yml`) [V] | `~/.hermes/state.db` · Telegram API · bot token env | **Extend** — add commerce watcher + `/store` + `commerce_launch` tool wiring (§4). Fail-closed user allowlist `TELEGRAM_ALLOWED_USERS` verified at `telegram.py:574-582` [V] |
| `gateway/cogitator_intake_bridge.py` (1,957 ln) | intake/intelligent routing; called from `run.py:6940-7134` [V] | merged; **PR #120 (2026-08-02)** calibrated it so operational commands are NOT hijacked into intake [V] | none · Cogitator bridge · `COGITATOR_BRIDGE_TOKEN` | **Reuse unchanged** — #120 is exactly the guard the acceptance test's step 1 needs |
| `gateway/intelligent_review_buttons.py` | opaque single-use TTL'd Telegram inline-button tokens, server-side state, replay-safe [V] | merged · unit-tested | in-memory store · none · none | **Reuse pattern** — commerce approval buttons copy this contract (§4.3) |
| `gateway/slash_commands.py` | slash surface; resolves bridge base URL from `intake.base_url` config = `https://worker-production-42f3.up.railway.app` [V] | merged | — | **Extend** — `/store` command family |
| `registrar_porkbun.py` (833 ln) | Porkbun v3 read-only client: ping, pricing, checkDomain, getRegistrationRequirements, DNS read, NS read, domain list; env/0600-file creds, redaction guard, loopback-confined fake mode, `--check` [V] | **merged PR #85** (`a64621cc15`) · `tests/test_registrar_porkbun.py` + fixtures [V] | none · Porkbun API (reads) · `PORKBUN_API_KEY/SECRET` | **Extend** — add DNS writes + `domain/create` (dryRun, Idempotency-Key) per §9.1 |
| `plans/commerce-s1-porkbun-discovery.md` | official-doc discovery, dated 2026-07-24, cites Porkbun spec [V] | merged | — | **Reuse** — §9.1 builds on it; re-verified key points 2026-08-02 |
| `commerce_jobs.py` + `scripts/commerce_job_cli.py` + `tests/test_commerce_jobs.py` (3,047 ln) | durable SQLite job store `~/.hermes/commerce/commerce_jobs.db`: jobs, append-only `job_events`, `job_actions` (effect_class read_only/consequential), `gates` (open/complete/invalidate), forbidden-data screen (Luhn/CVV/OTP/token regexes), canonical-JSON fingerprints, optimistic `row_version`, `recover()`, timeout sweep [V] | **open PR #88**, mergeable, clean worktree `/tmp/hermes-commerce-s2-job-store`, branch `feat/commerce-s2-job-store` (3 commits) [V] · 1,189-line test suite | commerce DB · none · none | **Merge first** with the §8 state-machine v2 amendment on the same branch. This IS the job store; building another is forbidden |
| `purchase_executor.py` V0.3 (1,322 ln) + `purchase_discovery.py` + `purchase_merchants.py` + `packaging/purchase-executor/` + `scripts/purchase_operator_cli.py` | one-shot ticket-gated browser checkout, Porkbun-only allowlist, no-retry, spool 0600, redaction mirroring Cogitator; hardened root systemd units; operator CLI propose→approve→ticket [V] | merged (#76 squash) · 108+ tests · security/build review PASS (issue #65, 2026-07-22) [V per v2 report] | `~/.hermes/purchase_executor` spool · Porkbun checkout · `$CREDENTIALS_DIRECTORY` | **Reuse unchanged** as registration **Leg B** fallback only (§9.1). Not on the critical path |
| Cogitator purchase governance (`cogitator_purchase_governance.py`, 1,871 ln) + operator bridge actions in `cogitator_bridge.py`: `create_purchase_proposal`, `get_purchase_approval_packet`, `approve_and_reserve_purchase`, ticket claim, `get_purchase_status`, `cancel_purchase_before_execution`, `record_completed_purchase`, failure actions [V read-only inspection] | money authority: exact-quote approvals (15-min TTL), budgets/reservations, single-use hashed audience-bound 5-min tickets, idempotency fingerprints, receipts, reconciliation, `purchase_assets`, append-only audit | merged & **deployed on Railway**; local checkout `809db08` is merely behind `origin/main` `6f06916` — nothing stranded [V] | Cogitator SQLite (Railway) · — · bridge bearer token | **Reuse unchanged.** [U-1 §11/WP0]: confirm deployed budget/class covers one ~US$12 `.com` registration; only if not, one additive policy insert (the only possible Cogitator change in this plan) |
| `virgil_mobile_server.py` (367 ln) + `virgil_mobile/` + user unit `virgil-mobile.service` (running) | loopback :8788 FastAPI PWA; middleware enforces trusted proxy = tailscaled, Host pinning, **`Tailscale-User-Login` == `bacon.calebz@gmail.com`** (constant-time), CSRF token, rate limits, CSP, audit log [V] | merged · tests in repo | `~/.hermes/attention` DB · none · none | **Extend** — mount the §6 gate viewer here. This is the human-handoff surface |
| Tailscale runtime | `tailscale serve`: `https://virgil-server.tailce4511.ts.net:8443` (tailnet-only) → 127.0.0.1:8788; **Funnel** :443 → 127.0.0.1:8799 = `gateway.research_bridge_server` (Cogitator's callback, public by design); Cal's phone `redmi-note-13-5g` **active** on the tailnet [V] | — | — | **Reuse.** Serve injects identity headers; Funnel never does [D 2026-08-02, tailscale.com/kb/1312/serve]. The gate viewer goes on the :8443 Serve side only |
| `hermes_attention.py` | attention items, Telegram notify plan, `deep_link_for` | merged · tested | attention DB | **Reuse optionally** (not on critical path); gate cards are sent directly by the gateway watcher (§4.2) |
| agent-browser CLI (`node_modules/.bin/agent-browser`) + `tools/browser_tool.py`, `browser_supervisor.py` (CDP WebSocket supervisor), `browser_cdp_tool.py` | local Playwright Chromium (`~/.cache/ms-playwright/chromium-1223`), named daemon sessions, `--profile <dir>` **persistent user-data-dir**, `get cdp-url`, headed flag, upload/clipboard commands [V, `--help` + skill docs] | vendored dependency, working (purchase executor drives it) | profiles on disk · target sites · none | **Reuse** — the worker drives `agent-browser --session commerce_<job> --profile ~/.hermes/browser-profiles/commerce/<job>`; the gate viewer attaches to the same browser via CDP (§6–7) |
| `config.yaml` browser section | `cloud_provider: local`, `sandbox_bypass: never`, `record_sessions: false`, `allow_private_urls: false` [V] | — | — | **Reuse** — constraints stand for the commerce session |
| Xvfb present; x11vnc/noVNC/websockify/Guacamole **absent** [V] | — | — | — | evidence for rejecting the VNC design (§6.6) |
| `gateway/kanban_watchers.py`, `gateway/restart.py`, `gateway/delivery.py` | DB-watcher → chat delivery pattern; gateway restart conventions | merged | — | **Reuse pattern** for `gateway/commerce_watcher.py` |
| Secret hygiene: `redact()` in `registrar_porkbun.py`; PAN/expiry/CVV/OTP regexes in `purchase_executor.py`; `reject_forbidden_data()` in `commerce_jobs.py`; `FORBIDDEN_FIELD_TOKENS` in Cogitator [V] | — | merged, tested | — | **Reuse** — every new module imports these, adds none |
| Shopify skill doc `optional-skills/productivity/shopify/SKILL.md` | curl+GraphQL patterns, custom-app token runbook (token shown once), API version notes [V] | doc only | — | **Reuse as reference** for `shopify_admin.py` and Cal's G-token runbook |
| CI `.github/workflows/tests.yml`, `lint.yml`, `typecheck.yml` [V] | — | — | — | gates every commit of §11 |

### 3.2 Runtime facts that shape the design [V]

- Server: `virgil-server` (100.76.84.72). Gateway = system unit; Virgil Mobile = user
  unit. **`loginctl` Linger=no** — user units die if all login sessions end; WP1 enables
  linger (one command) so the worker and mobile server survive logouts.
- Listening surface: only 22 (SSH), tailscale 443/8443, loopback 8788/8799. House rule
  confirmed: new services bind loopback and are exposed only via tailnet Serve.
- Cogitator is **not** a local process; it is the Railway deployment. Planning treated
  `/home/v0id/Projects/Cogitator_clean` as read-only; its dirty files are unrelated intake
  notes [V].

### 3.3 Branch/PR reconciliation (decision per item) [V, `git cherry`/GitHub 2026-08-02]

| Branch/PR | Status | Decision |
|---|---|---|
| **PR #88** `feat/commerce-s2-job-store` | open, mergeable, clean | **Merge first** after §8 v2 amendment (WP0) |
| PR #85 S1 Porkbun adapter | merged `a64621cc15` | done |
| PR #120 intake routing calibration | merged `d30607bf6c` | done — required by acceptance step 1 |
| `feat/purchase-cart-bootstrap-v0` | both commits superseded by squash-merged #76 ("V0.3 … cart bootstrap") | **Park** (delete after Cal's nod; no unique content) |
| `fix/virgil-mobile-*`, `feat/virgil-mobile-v0-attention-queue`, `decision-batch-virgil-readonly`, `feat/decision-inbox-ux` | `git cherry` shows all commits patch-equivalent in main | **Park/delete-safe**; not touched by this plan |
| `review/ecommerce-launch-executor-plan` | July planning docs | keep as history |
| Untracked `plans/governed-ecommerce-launch-executor-review.md` on main worktree | v2 report copy | **superseded as a standalone file** — its full content is merged into this document as Appendix A; the untracked file is not committed (delete after Cal's nod) |
| Linxio/ISB/x-batch branches & 16 worktrees | unrelated, clean | untouched |
| Cogitator repo | local behind origin; ~19 stale clean worktrees; open PRs #794/#793 unrelated | untouched; deployed Railway main is the runtime truth |

---

## 4. Chosen architecture (Telegram request → durable completion)

One sentence: **the gateway turns Cal's sentence into a durable job; a separate worker
executes it step-by-step against provider adapters and one persistent browser; every
pause is a row in the job store; the gateway watcher renders rows into Telegram; the gate
viewer lets Cal act inside the same browser; approvals bind to exact fingerprints; the
receipt is the job's event ledger summarized.**

Components (new code in **bold**, all in hermes-agent):

- **Telegram entrypoint** — existing gateway session. The allowlisted agent gets one new
  registered tool, **`commerce_launch`** (`tools/commerce_tool.py`): `start_or_resume`,
  `status`, `answer_facts`, `approve`/`deny` (typed fallback), `pause`, `cancel`. The tool
  writes to the job store only — it executes nothing. Deterministic control surface
  **`/store`** in `slash_commands.py` (`/store status|pause|resume|cancel|receipt`) for
  when Cal wants no LLM in the loop. PR #120's calibration keeps this text out of intake
  [V]; a routing test pins the exact acceptance sentence (T-route-1).
- **Job creation/resumption** — `commerce_jobs.py` (PR #88): dedupe on
  `(requester, objective_fingerprint, active)` so a second "set up the AMD GPU waitlist
  store" attaches to the live job [V branch code].
- **Approved launch-context retrieval** — worker reads approved facts via the existing
  Cogitator bridge read actions; missing facts → `awaiting_cal` gate with a
  missing-facts packet (§10.2). No auto-promotion; Cal's answers are recorded in the job
  and offered to Cogitator as lesson candidates at completion.
- **Planning/execution boundary** — the **worker** (`commerce_operator.py`, user unit
  **`virgil-commerce.service`**) is deterministic: it advances the job through §8 states
  by executing typed steps from the plan packet. No model call inside the worker or any
  adapter (house rule from `purchase_executor.py` [V]). LLM output (copy drafts, domain
  recommendation rationale) enters only as data fields inside the plan packet, produced
  at planning time in the gateway session, screened by `reject_forbidden_data` and the
  §10 claim gate.
- **Provider adapters** — `registrar_porkbun.py` (extended), **`shopify_admin.py`**,
  driven only by the worker.
- **Browser control** — the worker owns one agent-browser named session per job with a
  persistent profile (§7); gate viewer attaches to the same browser via CDP.
- **Human gates** — worker `open_gate()` (PR #88 API) → gateway watcher sends Cal one
  Telegram message: plain-English action + **one private link**
  `https://virgil-server.tailce4511.ts.net:8443/gate/<gate_id>?t=<token>` (§6). Never a
  secret through Telegram (the link itself grants nothing without tailnet identity).
- **Gate completion verification** — Cal taps DONE (viewer) or replies DONE (Telegram) →
  worker runs the gate's `verify` probe against provider truth (§6.4); only then
  `complete_gate()` and resume.
- **Mutation approval** — money mutations go proposal→approval→ticket in Cogitator
  (existing bridge actions [V]); non-money mutations (DNS write, publication) bind to a
  job-store approval row keyed to the action fingerprint (PR #88 fingerprints [V]).
- **Receipt & reporting** — §16 receipt built from `job_events`/`job_actions`, persisted
  in the DB + `~/.hermes/commerce/receipts/<job_id>.json` (0600), summarized to Telegram.
- **Restart/recovery** — systemd restarts the worker; on start it calls
  `CommerceJobStore.recover()` [V branch code], re-verifies the in-flight action against
  provider truth, reattaches or relaunches the browser session from the profile (§7), and
  parks anything unknowable as `uncertain_external_state`.

### 4.1 Sequence (primary acceptance path)

```mermaid
sequenceDiagram
  autonumber
  participant Cal as Cal (Telegram, allowlisted)
  participant GW as hermes-gateway (agent + /store + watcher)
  participant DB as commerce_jobs.db
  participant W as virgil-commerce worker
  participant B as agent-browser session (persistent profile)
  participant V as Gate viewer (Virgil Mobile :8443, tailnet-only)
  participant PB as Porkbun API
  participant COG as Cogitator (Railway)
  participant SH as Shopify

  Cal->>GW: "Set up the AMD GPU waitlist store."
  GW->>DB: commerce_launch.start_or_resume → job jb_x (requested)
  GW-->>Cal: job card: checking what exists…
  W->>DB: claim job → planning
  W->>COG: read approved facts (read-only)
  W->>PB: checkDomain × 10 candidates + pricing (read-only)
  W->>DB: plan packet + fingerprint → ready
  GW-->>Cal: decision packet: recommend siliconcurrent.com, exact price, [Approve][Deny]
  Cal->>GW: Approve (single-use button token)
  GW->>COG: create proposal → approve_and_reserve (exact quote)
  W->>PB: domain/create dryRun (wouldSucceed?) then create + Idempotency-Key
  W->>PB: DNS create A/AAAA/CNAME (bundled approval)
  W->>DB: receipt evidence → awaiting_cal (gate: store creation)
  GW-->>Cal: "Create the Shopify store (~15 min). One link:" /gate/cg_1?t=…
  Cal->>V: open link (tailnet identity) → sees live browser, signs up, 2FA, plan
  V->>B: input via CDP (screencast + dispatch)
  Cal->>V: DONE
  W->>SH: verify shop reachable + token staged → resume
  W->>SH: build pages/theme/waitlist (idempotent upserts, password on)
  W->>DB: verifying → evidence bundle
  GW-->>Cal: verification report + [Approve publication]
  Cal->>GW: Approve
  W->>SH: remove password, re-verify public URL/SSL/form
  W->>DB: completed + receipt
  GW-->>Cal: durable execution receipt
```

---

## 5. Repository ownership

| Responsibility | Home | Why |
|---|---|---|
| Job store, state machine, worker, watchers, receipts | **hermes-agent** | operational plane; PR #88 already lives here [V] |
| Provider adapters (Porkbun, Shopify) | hermes-agent (repo root, house convention of `registrar_porkbun.py` [V]) | deterministic, credential-adjacent |
| Browser worker + profiles | hermes-agent + `~/.hermes/browser-profiles/commerce/` | matches existing profile dir [V] |
| Private handoff UI | hermes-agent (`virgil_mobile_server.py` + `virgil_mobile/gate.js`) | the auth boundary, service, and phone install already exist [V] |
| Telegram gateway wiring | hermes-agent `gateway/` | only place with Telegram delivery |
| Money governance, approved facts, lesson review | **Cogitator (Railway), unchanged** | existing contracts (`create_purchase_proposal` … `record_completed_purchase` [V]) justify exactly this much and no more; Cogitator gains no adapters, browser, or job tables |

Dependency direction stays one-way (Hermes → Cogitator bridge; Cogitator's only inbound
path remains the existing Funnel research bridge, untouched). Cogitator down ⇒ jobs
continue safe local/read-only work and park before any money step (tickets unobtainable ⇒
fail-closed) [V mechanism].

---

## 6. Human-gate design (chosen: CDP screencast viewer inside Virgil Mobile)

**Exact server process:** the existing `virgil-mobile.service` user unit — one FastAPI
process on 127.0.0.1:8788, already proxied at `https://virgil-server.tailce4511.ts.net:8443`
(tailnet-only Serve) [V]. New routes: `GET /gate/{gate_id}` (viewer page) and
`WS /api/gate/{gate_id}/stream` (frames + input). Unit change: add
`ReadWritePaths=/home/v0id/.hermes/commerce` (gate reads/writes job store).

**Browser/session technology:** the job's existing agent-browser Chromium. The viewer
backend resolves the session's CDP URL (`agent-browser get cdp-url --session
commerce_<job>` [V capability]), opens one CDP WebSocket, runs `Page.startScreencast`
(JPEG frames, quality tuned for phone), and forwards Cal's gestures as
`Input.dispatchTouchEvent`/`MouseEvent`/`KeyEvent` and a text box that sends
`Input.insertText` (typing passwords/2FA without a page selector). Stop screencast on
disconnect. One implementation file each side (server module + one JS asset); no new
protocol invented.

**Binding/exposure:** loopback only; tailnet Serve only. **Never Funnel** — Funnel strips
identity and is public [D 2026-08-02]. No new tailscale config beyond the existing :8443
mount.

**Authentication:** unchanged middleware — peer must be tailscaled, Host pinned,
`Tailscale-User-Login` must equal Cal's login (constant-time compare) [V]. Tagged-node
traffic has no identity headers ⇒ denied by the same check [D 2026-08-02].

**Authorisation restricted to Cal:** the identity pin *is* Cal; additionally each gate URL
carries `t=<token>` — 32-byte urlsafe secret, stored **hashed (SHA-256)** in the `gates`
row, single active gate per job, TTL **30 min** (renewable by Cal tapping "keep open";
each renewal re-verifies identity). Token ≠ authentication (identity is); it binds the
link to exactly one gate and prevents stale-link confusion/replay.

**Expiry / replay / invalidation:** token hash checked on page load and WS upgrade;
consumed rows rejected; on gate completion, cancellation, job pause, or TTL the token is
invalidated (row status change) and open WS connections are closed. Replay of a completed
gate link → 410 page with current job status, no session access.

**Job/session binding:** gate row carries `job_id` + browser session name; the viewer can
only ever attach to that session's CDP endpoint (path computed server-side; no
client-supplied targets).

**One active controller:** the WS handler holds an in-process per-gate lock — second
connection gets read-only frames with a "session controlled elsewhere" banner and a
"take control" action (which drops the first). The worker never drives the browser while
a gate is open (`awaiting_cal` state excludes worker steps by the §8 machine).

**Screenshots:** worker captures evidence screenshots **before** opening and **after**
verifying a gate; never during. Frames streamed to Cal are not persisted anywhere.
**Clipboard:** viewer text box → `Input.insertText` only; no reading of the page
clipboard. **Upload/download:** disabled in V0; gates are designed to avoid uploads (KYC
happens on Cal's own device per Shopify's flow, contingency C-B8); downloads stay in the
browser profile and are not surfaced.

**How Cal completes 2FA/payment:** directly inside the provider page via the streamed
session — Virgil renders pixels and forwards input; it does not read fields. **How Virgil
learns completion without reading the secret:** per-gate `verify` probe against provider
truth (§6.4 examples: Porkbun `ping` succeeds with the staged key; Shopify
`shop { id }` query succeeds; storefront password state; DNS answer) — never OCR, never
DOM scraping of secret fields, never trusting DONE alone.

**Abandoned gate:** TTL expiry → link invalidated, job stays `awaiting_cal`; reminders at
6 h/24 h; 72 h → `timed_out` (resumable via `/store resume`, which opens a fresh gate).
**Browser session expiry during a gate:** viewer shows "session ended"; worker relaunches
from the persistent profile, re-navigates to the gate URL, opens a fresh token, Telegram
message updates — provider login state survives via the profile (§7).

**6.6 Rejected alternatives:** Xvfb+x11vnc+noVNC (nothing installed [V], three new
services, no identity integration, worse phone UX); exposing Chrome DevTools frontend
(full-power devtools = secret exposure + desktop-only); Tailscale Funnel link (public, no
identity [D 2026-08-02] — violates the mandate outright); cloud browser handoff
(Browserbase/Browser Use — config forbids cloud [V], adds a third party to a credential
path); Telegram "please do it on your own device" for *session-bound* steps (breaks
"resume the same browser session" for challenges like CAPTCHA that must be solved inside
Virgil's session). Steps that are genuinely account-bound rather than session-bound
(Shopify signup, KYC) may be done on Cal's device as the runbook says — the gate verify
probe is identical either way.

---

## 7. Browser persistence and recovery

- **Executable:** Playwright Chromium `chromium-1223` already installed at
  `~/.cache/ms-playwright` [V], launched by agent-browser (headless; headed unnecessary —
  screencast works headless).
- **Profile path:** `~/.hermes/browser-profiles/commerce/<job_id>/` via
  `agent-browser --profile <dir>` (persistent user-data-dir: cookies, localStorage,
  service workers survive restarts [V, skill docs]). Parent dir already exists 0700 [V];
  job dirs created 0700, `umask 077` in the unit.
- **Process ownership:** the agent-browser daemon is spawned by the worker unit
  (`virgil-commerce.service`, user scope, `Restart=on-failure`, hardening copied from
  `virgil-mobile.service` [V] plus `ReadWritePaths` for commerce DB, profiles, receipts).
  WP1 runs `loginctl enable-linger v0id` so user units outlive logins ([V] Linger=no
  today — a real recovery hole, closed by one command).
- **Session identity:** exactly one session name per job: `commerce_<job_id>`, recorded in
  the job row. **Duplicate-worker prevention:** the worker takes an exclusive
  `flock` on `~/.hermes/commerce/worker.lock` at startup (plus systemd's single-instance
  guarantee); per-profile, Chromium's own `SingletonLock` refuses a second launch —
  treated as "another owner exists" ⇒ worker parks the job `reconciliation_required`
  rather than force-unlocking.
- **State allowed in the profile:** provider cookies/session storage needed for logins —
  and nothing else by design (that is what the mandate permits). **State forbidden in the
  job DB and all logs/receipts/Telegram:** passwords, card contents, 2FA codes, identity
  documents, cookies, tokens — enforced mechanically by `reject_forbidden_data()` on every
  persisted payload [V PR #88] and the executor redaction regexes on all rendered text [V].
- **Retention:** profile kept while the job is active; deleted (`shred -u` not required —
  `rm -rf`) 30 days after terminal state unless Cal keeps it; recorded in the receipt.
- **Restart behaviour:** worker start → `recover()` → for the in-flight action, re-read
  provider truth (order present? DNS record present? page exists?) → resume, or park
  uncertain. Browser: try `get cdp-url` on the recorded session; if the daemon died,
  relaunch with the same profile and re-navigate to the step's URL (every step declares
  its entry URL in the plan packet — this is the page-state reconciliation rule: steps are
  written to be **entry-URL re-enterable**, never dependent on residual page state).
- **Crash mid-gate:** §6 (fresh token, same profile). **Stale-session detection:**
  cdp-url unreachable or session absent from `agent-browser session list` ⇒ relaunch path.
- **Evidence capture:** `agent-browser screenshot` per completed step →
  `~/.hermes/commerce/evidence/<job_id>/<step>.png` (0600), path recorded in `job_actions`.
- **Anti-loop / retries:** reads retry 3× exponential backoff; consequential actions are
  single-attempt (house rule [V]); each step has `max_attempts` in the plan packet and a
  per-job step-execution counter — exceeding it fails the step to `awaiting_cal` with a
  diagnosis card, never a silent loop.
- **Provider page changed:** deterministic steps target the API first (Porkbun/Shopify
  flows below are ≥90 % API); the few browser steps (Cal-driven gates) are human-driven by
  construction, so page drift degrades to "Cal sees a different page", not automation
  breakage. Worker-driven browser automation against provider UIs is deliberately **not**
  part of V1 (that is what killed reliability in older plans).

---

## 8. Durable commerce job and state machine (v2)

**Fitness decision:** PR #88's store is fit for purpose — append-only events, gates,
fingerprint binding, forbidden-data screening, recovery, versioned optimistic writes, and
1,189 lines of tests [V]. **No second store.** One amendment before merge (same branch):
replace the v1 preorder-specific state set with the v2 set below
(`STATE_MACHINE_VERSION = 2`, no data migration — the DB has never shipped).

Design changes from v1: provider-specific execution states (`registering_domain`,
`building_store`, …) become a `current_step` field on the job + rows in `job_actions`;
gate-flavoured waiting collapses to four approval/waiting states driven by the `gates`
table; `timed_out` becomes a real state (v1 folded it into `paused`).

States and rules (columns: entry · exit · persisted evidence · idempotency · retry ·
user message · recovery):

| State | Entry | Exit → | Evidence | Idempotency / retry | Message / recovery |
|---|---|---|---|---|---|
| `requested` | allowlisted request via tool/command; dedupe attach | `planning` | request text, requester id | job unique key `(requester, objective_fp, active)`; re-request attaches | "Checking what already exists…" / trivially re-enterable |
| `planning` | job claimed by worker | `ready` \| `awaiting_cal` (missing facts) | recovery report; fact snapshot; candidate-domain read results; plan packet + `plan_fingerprint` | reads retry 3×; packet rebuild is pure | "Planning the launch…" / rebuild from scratch, cheap |
| `ready` | plan fingerprinted; decision packet rendered | `awaiting_purchase_approval` (first consequential step) \| `executing_read_only` | decision packet | re-render replaces packet; fingerprint change invalidates prior approvals [V mechanism] | decision packet card / re-render |
| `executing_read_only` | next step is read-only | `ready` \| `executing` \| `verifying` | action rows + evidence refs | reads idempotent, retry 3× | progress note / re-run step |
| `awaiting_purchase_approval` | money step proposed (Cogitator proposal created, exact quote) | `executing` on approval; `cancelled` on deny; re-quote on TTL expiry | proposal id, quote, approval ref | approval TTL 15 min [V]; expired ⇒ fresh packet | "[Approve] {domain} for {exact price}" / re-render packet |
| `awaiting_dns_approval` | DNS diff touches existing/protected records (not the initial empty-zone bundle) | `executing` \| `cancelled` | zone snapshot id + diff hash | diff-hash-bound approval | DNS diff card / re-diff |
| `awaiting_publication_approval` | verification green | `executing` (publish) \| `paused` | verification bundle, approval ref | any verification regression re-gates | launch card / re-verify then re-render |
| `awaiting_cal` | any §2 human gate opened (`gates` row: type, action text, token hash, TTL) | `resuming` after DONE+verify; `timed_out` at 72 h | gate row, opened/completed ts, verify-probe result | gate verify probe is a read; DONE without probe-pass keeps state | one link + one action / fresh token, same gate |
| `executing` | approved consequential step dispatched | `executing_read_only`/`ready` on success; `uncertain_external_state` on unknowable outcome; `awaiting_cal` on challenge | action row (fingerprint, approval ref), provider response (redacted), evidence | **single-attempt** for irreversible actions; `Idempotency-Key` where provider supports (Porkbun POSTs [D 2026-07-24]) | "Registering {domain}…" etc. / never auto-rerun: reconcile via provider read |
| `resuming` | gate verified complete | next planned step's state | probe evidence | pure | "Resuming…" / re-probe |
| `verifying` | build complete | `awaiting_publication_approval` \| `ready` (fix loop with diagnosis) | §9.3 checklist bundle | all checks read-only, retry 3× | verification report / re-run checklist |
| `uncertain_external_state` | dispatched write, no definitive result | `reconciliation_required` | last action, sanitized response fragments | no retries, reads only | "⚠ outcome unknown, reconciling" / immediate provider re-read |
| `reconciliation_required` | uncertainty recorded; provider truth ambiguous or conflicting | `ready` \| `failed` (operator decision) | reconciliation record | human decision required if conflict | reconcile packet to Cal / operator CLI or Telegram |
| `timed_out` | gate/state age limit (default 72 h, gates per §6) | `ready` via `/store resume`; `cancelled` | timeout event | — | "Timed out, resume anytime" / resume re-opens gate |
| `paused` | Cal/operator pause | `ready` \| `cancelled` | reason | — | status card / — |
| `completed` | receipt persisted + delivered | terminal | §16 receipt | — | receipt / — |
| `failed` | unrecoverable with diagnosis | terminal | failure record | — | summary + evidence / — |
| `cancelled` | Cal/operator cancel; Cogitator reservations released via existing cancel action [V] | terminal | cancel record | — | summary / — |

Global rules (unchanged from PR #88 mechanics [V]): every transition appends a
`job_events` row; illegal transitions raise; all persisted payloads pass
`reject_forbidden_data`; timeout sweep skips uncertainty states; `recover()` is the only
restart entrypoint.

---

## 9. Provider-specific execution flows

Tags per step: (API|browser) · (read|**mutation**) · approval · evidence · fallback.

### 9.1 Porkbun

Verified current 2026-08-02 against official docs (`porkbun.com/llms/guides/register-a-domain`,
`/llms/dns`): registration = `POST /domain/create/{domain}` with `cost` (integer US cents,
must equal the current `checkDomain` quote) + `agreeToTerms`; `dryRun: true` runs every
pre-flight (availability, cost, eligibility, funds, spend cap) **without charging or
creating**; `Idempotency-Key` replays the original result for 24 h instead of double-
registering; `getRegistrationRequirements/{tld}` returns `apiRegisterable`; TLDs with
registry eligibility rules **including `.au` cannot be submitted via API** — "register on
the website". Funding draws down **account credit**; account email+phone verification and
≥1 prior registration are prerequisites [D 2026-07-24 S1 doc + D 2026-08-02].

1. Auth ping + account domain list — API · read · none · ping result · if creds absent →
   `awaiting_cal` gate "create/restrict an API key" (viewer on porkbun.com, key staged to
   `~/.hermes/secrets/porkbun.env` 0600 by Cal via the documented one-command stage step —
   never through Telegram).
2. `getRegistrationRequirements` for `.com`/`.net`/`.com.au` — API · read · none →
   records `apiRegisterable` truth (resolves [U] from S1). `.com.au` additionally requires
   ABN/ACN per auDA [D 2026-08-02] → excluded unless Cal supplies one (then website leg
   via gate viewer).
3. `checkDomain` all 10 candidates (rate limit: 1/10 s per account [D 2026-07-24] — worker
   paces sequentially, ~2 min total) + `/pricing/get` — API · read · none · full
   availability/price table into the plan packet.
4. Recommendation: available candidates ranked (brand preference Silicon Current first),
   plus a read-only collision scan (web search + TM register browse, results as evidence,
   flagged for Cal — Virgil does not clear trademarks).
5. `domain/create` **dryRun** for the recommended domain — API · read-equivalent · none ·
   `wouldSucceed`, `cost`, `balance`, `sufficientFunds`, spend-limit fields. If
   `sufficientFunds=false` → gate: Cal tops up account credit on porkbun.com (viewer);
   re-run dryRun.
6. Decision packet → **exact price+domain approval** (Telegram button) → Cogitator
   proposal `create_purchase_proposal` with `final_quoted_total` = dryRun cost →
   `approve_and_reserve_purchase` [V actions exist].
7. `domain/create` — API · **mutation, irreversible** · bound approval + ticket ·
   `Idempotency-Key = jb_<job>_register_<domain>` · response `orderId`, charged cost,
   `balance` → receipt via `record_completed_purchase` [V]. Timeout/ambiguous response →
   `uncertain_external_state`; reconcile = domain list re-read (present ⇒ completed with
   evidence from `domain/get`; absent after 24 h idempotency window ⇒ definitive failure).
   **Fallback (Leg B):** existing browser purchase executor, already Porkbun-allowlisted
   [V], including the first-registration-must-be-manual case (Cal completes checkout in
   the gate viewer; C-P4).
8. WHOIS privacy on (create-time flag), auto-renew left ON (Porkbun terms bundle
   auto-renew agreement [D 2026-08-02]) — recorded as a recurring commitment in
   `purchase_assets` for renewal alerting.
9. DNS: fresh zone → `dns/retrieve` snapshot (evidence + rollback source) →
   create A `23.227.38.65`, AAAA `2620:0127:f00f:5::`, `www` CNAME
   `shops.myshopify.com.` [D 2026-08-02 help.shopify.com] — API · **mutation, reversible**
   · bundled into the purchase approval packet ("and connect it to the store") unless
   pre-existing records would be changed/deleted ⇒ `awaiting_dns_approval` diff card.
   Porkbun default NS kept (SOA/default NS are protected by Porkbun itself
   [D 2026-08-02]). Post-apply: API re-read + multi-resolver lookups until propagated
   (poll, not block: job proceeds to Shopify build meanwhile).

### 9.2 Shopify

Verified current: no store-creation API (dev-store creation is dashboard-manual too)
[D 2026-08-02]; password removal only after picking a paid plan (selectable during trial,
billed at trial end) [D 2026-08-02]; Admin GraphQL latest stable `2026-07`
[D 2026-08-02, shopify.dev]; Dawn includes a native email-signup form writing customers
with `accepts_marketing` consent [D 2026-08-02, shopify.dev email-consent doc]; custom-app
Admin token shown once at install [V skill doc, D-equivalent].

1. **G-store gate** (browser, Cal): create store at shopify.com (email, verification,
   store name = approved brand), skip onboarding questions. Session-bound challenges
   (CAPTCHA) happen in the gate viewer; account steps may equally be done on Cal's device.
   Verify probe: none yet (no token) — Cal's DONE + step 2's token proves it.
2. **G-token gate** (browser, Cal): Settings → Apps → Develop apps → create app
   "virgil-operator", scopes: `read_products, write_products` (only if a product is ever
   needed — V1 default **no products**), `write_online_store_pages/read`, `write_themes,
   read_themes`, `read_customers` (waitlist verification), `write_publications`? — exact
   scope names pinned at WP4 against the 2026-07 schema [U-2]; install app; stage token
   once into `~/.hermes/secrets/shopify.env` (0600) per runbook. Verify probe:
   `shop { id name myshopifyDomain currencyCode plan { displayName } }` succeeds ⇒ shop
   identity **pinned** to the job (account substitution invalidates Shopify approvals —
   PR #88 fingerprint mechanics).
3. Store settings — API · mutation (idempotent upserts) · none: currency AUD, timezone,
   customer email double-opt-in setting per Cal's Spam Act preference (§10).
4. Theme: confirm Dawn is the live theme (default on new stores); `themes` query; theme
   **settings** updates via the Admin API theme asset/settings surface — exact mutation
   set pinned at WP4 [U-3]; if theme settings prove API-awkward, fallback = Shopify CLI
   `theme pull/push` of an unpublished copy (CLI auth via Theme Access [U-3]); last-resort
   fallback = one G-theme gate where Cal clicks the documented theme-editor steps.
   Content lives in **pages + sections**, not custom theme code (v2 report decision,
   unchanged).
5. Landing page + content package (§10) — API · mutation (upserts keyed by handle) ·
   claim-gate enforced at build time · page HTML snapshots in evidence.
6. Waitlist form: Dawn email-signup section on the landing page (native customer form,
   consent text §10) — API/theme settings · mutation · none. No app dependency;
   Shopify Forms is the deferred upgrade path.
7. Password page ON with a generated password (default state pre-plan anyway) — verified
   by probe (storefront returns password page).
8. **G-plan gate** (browser, Cal): pick cheapest plan (Basic) + billing card. Verify
   probe: `shop { plan { displayName } }` no longer trial-tier ⇒ password removal is
   permitted [D 2026-08-02].
9. Custom domain connect: Admin domain add + verify against the §9.1 DNS records — API
   where the 2026-07 schema allows domain mutations [U-4, WP4 pins; fallback = one
   G-domain gate with viewer clicks] · mutation · covered by the DNS/purchase approval ·
   SSL status polled until issued.
10. Test pass (§9.3) → `verifying`.
11. **G-publish gate** (Telegram approval, no browser needed): approval → remove
    storefront password (Online Store → Preferences toggle; API if available in 2026-07
    schema [U-4], else the worker opens a G-publish-click viewer gate) → public smoke.

### 9.3 Verification checklist (all read-only, evidence per item)

Public page 200 on `https://<domain>` and `https://www.<domain>` with valid SSL and
correct redirect orientation; landing content matches the approved package byte-for-byte
(placeholder scan = zero hits); mobile viewport screenshot (agent-browser device
emulation); waitlist signup with `waitlist-test+<job>@<cal-domain>` → customer appears via
Admin API with consent recorded → test subscriber deleted + evidence kept; confirmation
message shown; all links resolve; **no checkout route**: `/cart`, `/checkout`,
`/products/*` return 404/redirect and no Buy/price element exists in the rendered page;
DNS answers match expected targets from two public resolvers; domain status active at
Porkbun; no forbidden-claim strings anywhere in rendered HTML (automated scan against the
§10 forbidden list).

---

## 10. Launch content and legal/trust boundaries

### 10.1 Content package (exact, claim-free; placeholders in ⟨⟩ are Cal-supplied facts)

- **Brand:** Silicon Current (pending §9.1 availability/collision checks). Announcement
  bar: "Priority access list — no payment, no obligation."
- **Hero:** "AMD Radeon RX 9070 XT 16GB — priority access for Australia." Sub: "Join the
  list. When our first allocation is confirmed, members get first access, in order of
  signup." CTA button: "Join the priority list".
- **How it works (3 steps):** join with your email → we confirm your spot instantly → when
  allocation is confirmed, you get an email with your access window. No payment is taken
  on this site.
- **Trust/value points (grounded only):** "No payment now — joining is free"; "First
  come, first served"; "Unsubscribe anytime"; "We only email you about GPU access".
- **FAQ:** Is this a purchase? (No — a free waitlist; no checkout exists on this site.)
  When will cards be available? (We don't publish dates until an allocation is
  confirmed.) Price? (Announced to the list when confirmed.) Who are you?
  (⟨business identity sentence⟩, contact below.)
- **Signup confirmation:** "You're on the list. We'll email you only about AMD GPU
  priority access. Unsubscribe anytime."
- **Marketing consent (Spam Act 2003 express-consent wording):** checkbox/inline text —
  "Email me about AMD GPU priority access from Silicon Current. Unsubscribe anytime." No
  pre-ticked boxes; double opt-in per Cal's choice at G-store facts.
- **Footer disclaimer:** "Silicon Current is an independent Australian retailer-in-
  formation. AMD and Radeon are trademarks of Advanced Micro Devices, Inc. This site is
  not affiliated with or endorsed by AMD. Joining the list is free and creates no
  obligation for either party. No payments are accepted on this site." Links: Privacy
  Policy, Contact.
- **Contact path:** `⟨contact email⟩` (mailto) — required by consent rules; monitored by
  Cal.
- **Privacy policy:** Shopify's generated template, reviewed by Cal at the facts gate
  (data collected: email only; purpose: waitlist emails; processor: Shopify).

### 10.2 Required Cal facts (missing-facts packet at `planning`)

Contact email · business identity sentence for the FAQ/footer (individual trading name is
fine for V0) · double opt-in yes/no · brand sign-off · ABN if `.com.au` is ever wanted
(defer allowed) · privacy-policy sign-off. **Not required in V0** (no checkout): pricing,
warranty, refunds, delivery, supplier anything. Australian-market note: no physical
address is published in V0; consent + working unsubscribe + contact path satisfy the
practical Spam Act posture for a zero-commerce list [I — not legal advice; flagged for
professional review before any commercial email campaign].

### 10.3 Enforcement

The content builder compiles the package from a template whose only variable slots are
the ⟨facts⟩ above; any template or fact containing a forbidden-claim term (mandate list,
§2) fails the build (test T-content); the §9.3 rendered-HTML scan enforces it end-to-end;
checkout stays disabled structurally (no products, no payment provider configured) and
§9.3 proves it. Checkout remains prohibited until supplier legitimacy, landed cost,
warranty, delivery, and refund positions pass their own future approval flow (out of
scope here by design).

---

## 11. Complete implementation work breakdown

**Branch/PR strategy (exact):** (1) amend and merge **PR #88** first — commits go on its
existing branch `feat/commerce-s2-job-store`, PR merged to `main` when green. (2) All
remaining work lands on **one integration branch `feat/virgil-commerce-operator-v1`** off
post-merge `main`, with one logical commit per work package below, opened as **one PR**
and kept green throughout (CI: tests/lint/typecheck [V]). No other PRs; no Cogitator PR
unless WP0 proves the policy gap ([U-1]), in which case exactly one additive Cogitator PR.
Merge-conflict rule: rebase the integration branch on `main` daily; conflicts resolve in
favour of `main` for unrelated files, in favour of the branch for the files listed in this
section's work packages.
Deployment order: hermes only; services installed inert and enabled explicitly.

Every WP lists: objective · depends · files · reuse · tests · acceptance · deploy ·
rollback · contingency · Cal? · auto-continue?

**WP0 — Reconciliation + job-store v2 merge.**
Objective: single job store on `main`; clean branch inventory; runtime unknowns resolved.
Depends: nothing. Files: `commerce_jobs.py`, `tests/test_commerce_jobs.py` (state set →
§8 v2; version constant; tests updated) on the #88 branch; the live-discovery note
`plans/commerce-discovery-live.md` committed on the integration branch (the master plan
itself is already on `main` via its own docs-only PR; the superseded `plans/` drafts are
never committed — §3.3); branch parking per §3.3 (delete only with Cal's nod, else leave).
Runtime tasks (read-only): authenticated Porkbun discovery via existing adapter —
`ping`, `getRegistrationRequirements` × {com, net, com.au}, `/pricing/get`, account
domain list; balance via a `domain/create` **dryRun** on the top candidate; record all
in `plans/commerce-discovery-live.md` (resolves [U-P: apiRegisterable, balance,
prior-registration prerequisite]). Query deployed Cogitator `get_purchase_status`-family
read actions to confirm the domain purchase class/budget ([U-1]).
Reuse: everything. Tests: updated suite green + full `tests/gateway` regression.
Acceptance: PR #88 merged; discovery doc committed; [U-1] answered.
Deploy: none. Rollback: revert merge commit. Contingency: if the v2 amendment balloons
(>1 day), merge #88 as-is and do the state rename as the first integration-branch commit
(states are constants + tests; no data exists). Cal: no (Porkbun key must already exist —
if absent this WP's live half moves behind WP5's key gate; code half proceeds).
Auto-continue: yes.

**WP1 — Worker + Telegram wiring (the operational spine).**
Objective: the acceptance sentence creates a durable job that advances through read-only
phases, renders packets/gates/status to Telegram, and survives restarts.
Depends: WP0. Files (new): `commerce_operator.py` (worker loop: claim → step dispatch →
state writes; deterministic; no model calls), `gateway/commerce_watcher.py` (DB watcher →
gate cards/status/approval buttons via existing delivery), `tools/commerce_tool.py`
(`commerce_launch` registered tool), `gateway/commerce_buttons.py` (opaque-token approval
buttons, contract copied from `intelligent_review_buttons.py` [V]),
`packaging/virgil-commerce/virgil-commerce.service` (+ install/uninstall mirroring
`virgil-mobile` unit hardening [V]); modified: `gateway/slash_commands.py` (`/store`),
`gateway/run.py` (watcher startup + button callback routing — smallest possible diff,
pattern of existing bridges). Also: `loginctl enable-linger v0id`.
Reuse: PR #88 store/CLI; kanban-watcher pattern; delivery; allowlist authz (unchanged).
Tests: `tests/test_commerce_operator.py` (step dispatch, crash-mid-step recovery via
kill -9 harness, duplicate-worker flock), `tests/gateway/test_commerce_routing.py`
(T-route-1: exact sentence → job; intake non-hijack regression with #120 fixtures;
button token single-use/expiry/wrong-user).
Acceptance: fake-provider job reaches `ready` and renders a decision packet in a live
Telegram smoke; `systemctl --user restart virgil-commerce` mid-run resumes correctly.
Deploy: install + enable user unit. Rollback: disable unit; revert commit (DB file kept —
house rule). Contingency: if watcher-in-gateway proves racy, fall back to the worker
sending via a minimal direct Bot-API sender (same token env) — one function, flagged
`ponytail:` for later consolidation. Cal: no. Auto-continue: yes.

**WP2 — Human-gate viewer.**
Objective: §6 exactly. Depends: WP1 (gates exist to view).
Files (new): gate router + CDP bridge in `virgil_mobile_server.py` (or sibling module
`virgil_gate_routes.py` imported by it), `virgil_mobile/gate.js`, `gate.css`; unit drop-in
adding `ReadWritePaths=/home/v0id/.hermes/commerce`. Reuse: entire existing auth
middleware, service, Serve mount, CSRF/rate-limit machinery [V]; agent-browser CDP.
Tests: `tests/test_virgil_gate.py` — token hash verify, TTL expiry, replay → 410,
single-controller lock, identity-header enforcement (reuse existing middleware tests'
fixtures), no-frame-persistence assertion; a loopback fake-CDP server for stream tests.
Acceptance: live smoke — worker opens a demo gate on `example.com`; Cal (or Fable
operating Cal's phone-equivalent via tailnet) loads the link, sees frames, types into the
page, taps DONE; verify probe runs; link invalidated.
Deploy: restart virgil-mobile unit. Rollback: revert commit + restart (existing PWA
unaffected — routes are additive). Contingency: if CDP screencast frame-rate is unusable
on the phone, drop to 1 fps stills + tap-to-refresh (same wire protocol, quality knob) —
`ponytail: quality knob, upgrade path = WebRTC, only if ever needed`. Cal: one 5-minute
gate smoke. Auto-continue: yes after smoke evidence recorded.

**WP3 — Porkbun mutation legs + governance wiring.**
Objective: §9.1 steps 5–9 end-to-end against a fake server; live up to dryRun.
Depends: WP0 (adapter base, discovery), WP1 (approval flow). Files: `registrar_porkbun.py`
(add `create_domain(dry_run=…)`, `dns_create/edit/delete`, `Idempotency-Key` support —
same validation/redaction style [V]), `commerce_operator.py` steps,
`gateway/commerce_buttons.py` (purchase approval → Cogitator operator-bridge calls with
packets from `scripts/purchase_operator_cli.py`'s builders [V]); fixtures
`tests/fixtures/porkbun_api_v3/*` extended.
Tests: fake-server registration happy/timeout/ambiguous/insufficient-funds/409-idempotent
replay; DNS create/verify/snapshot-restore; T-uncertain both outcomes; exact-quote
mismatch re-render; secret-redaction sweep.
Acceptance: loopback E2E — availability → packet → (fake) approval → dryRun → create →
DNS → evidence; **live** acceptance stops at real dryRun (`wouldSucceed: true` recorded).
The real registration is WP5's supervised act.
Deploy: none new. Rollback: revert commit. Contingency: API leg blocked (apiRegisterable
false / prior-registration rule / IP restriction) → Leg B browser executor already
allowlisted [V] or viewer gate (C-P4); neither blocks the branch. Cal: approve nothing
yet (dryRun only). Auto-continue: yes.

**WP4 — Shopify adapter + content + verification.**
Objective: §9.2 steps 2–11 implementable and §9.3 automated, all against a fake GraphQL
server; content package templated with the claim gate.
Depends: WP1; parallel with WP3. Files (new): `shopify_admin.py` (GraphQL `2026-07`
pinned constant; shop identity; page/theme-settings/navigation upserts; customer-by-email
read; domain status; storefront probes), `commerce_content.py` (template + facts →
package; forbidden-claim scanner), `commerce_verify.py` (§9.3), fixtures
`tests/fixtures/shopify_admin/*`; worker steps.
Resolves [U-2..U-4] against the live 2026-07 schema docs (exact scope names, theme
settings mutations, domain/password mutations) — documented in the WP4 commit message.
Tests: upsert double-apply idempotence; claim-gate red cases (every forbidden term);
verification each check red+green; no-PII-persistence (customer read returns only the
test address; assert nothing else stored); token-redaction sweep.
Acceptance: fake-store E2E build+verify green; content package renders byte-stable.
Deploy: none. Rollback: revert commit. Contingency: theme-settings API gap → Shopify CLI
leg → G-theme viewer gate (three-deep fallback, §9.2.4). Cal: no. Auto-continue: yes.

**WP5 — Live launch (runtime, not new code).**
Objective: the primary acceptance test, for real, with the §16 receipt.
Depends: WP1–4 merged, integration PR green + merged, services enabled.
Sequence: Cal stages Porkbun key (if not already) → Cal sends the sentence → job runs
§9.1 (real approval, real registration ≤ approved amount, DNS) → G-store/G-token gates →
build → G-plan gate → verify → G-publish approval → publish → receipt. Every gate is a
runbook card the watcher renders; nothing here needs Codex at the keyboard, but Codex
monitors the first run and files defects into the same branch.
Acceptance: §16 receipt delivered; §9.3 all green on the public site.
Rollback: store re-passworded via one command/gate; DNS snapshot restore; domain
purchase is explicitly non-rollbackable (disclosed in the approval packet).
Cal: yes (the gates above, ~30–45 min total plus DNS/SSL wait). Auto-continue: n/a.

**Effort note (for scheduling, not a promise):** WP0 ≈ ½–1 day; WP1 ≈ 2–3 days; WP2 ≈
2–3 days; WP3 ≈ 1–2 days; WP4 ≈ 2–3 days; WP5 ≈ elapsed 1–3 days dominated by Cal-gate
latency + DNS. Nothing here is research-grade; every hard primitive already exists.

---

## 12. Execution continuation policy (binding on Codex)

- Green validation (tests + lint + typecheck + WP acceptance) → **continue automatically**
  to the next WP. Do not report-and-wait between WPs.
- Known failure with a documented contingency (each WP lists one; §13 lists the rest) →
  execute the contingency, note it in the commit message, continue.
- Merge conflict → §11 resolution rule; if a conflict touches money/gate/secret code
  paths, re-run the full affected test series before continuing.
- Test failure caused by the change → fix in the same WP commit; never skip/xfail new
  tests.
- Pre-existing unrelated failure → prove it (same failure on clean `main` at the merge
  base, one command in the commit message) and continue.
- Provider human gate during WP5 → pause **that job step only**; engineering work never
  waits on a gate.
- Irreversible action (registration, publication) → only via the recorded approval flow;
  Codex never self-approves, including in staging against real providers.
- Unknown security consequence (a step would move a secret across a boundary not
  described in this plan) → **stop that path**, return one precise blocker.
- No safe contingency → return one precise blocker naming: the WP, the failing step, the
  evidence, and the single decision needed.
- Normal engineering choices (naming, splitting a function, test structure, fixture
  shape) are delegated — do not return for them.

---

## 13. Contingency matrix

Columns: detection → safe response / fallback / rollback · Cal? · resume point ·
evidence retained. Grouped; one line each.

**Repository/implementation**
| Condition | Handling |
|---|---|
| Stale open PR (#88 drifts vs main) | `mergeable` check before WP0; rebase branch; conflicts per §11 rule · no Cal · resume WP0 · rebase log |
| Conflicting branches | §3.3 table is authoritative; parked branches never merged · no · continue · table |
| Dirty worktree | inventory first, never reset/clean (house rule); commit or stash-by-copy to scratch · no · continue · `git status` snapshot |
| Missing dependency | stdlib-first rule means none expected; if a vendored tool (agent-browser) breaks → `agent-browser doctor --fix`, else pin prior node_modules state · no · same WP · doctor output |
| Failing CI / test timeout | rerun once to classify flake; real → fix in-WP; flake → mark with issue ref · no · same WP · CI links |
| Unrelated pre-existing failure | prove on clean main (§12) · no · continue · command output in commit |
| Schema mismatch (commerce DB) | `init_db` is additive-only; on mismatch stop writes, back up DB file, migrate additively · no · resume after migration · DB backup path |
| Port conflict (8788/8790) | `ss -tlnp` before bind; pick next configured port + update Serve mount · no · same WP · config diff |
| systemd unit failure | `systemctl status` + journal; unit is inert-installable; fix and re-enable · no · same WP · journal excerpt |
| Failed deployment / rollback failure | units disable cleanly (`--now`); worst case stop unit + revert commit; DB files never deleted · no · previous WP boundary · journal |

**Browser/handoff**
| Condition | Handling |
|---|---|
| No suitable browser / Playwright mismatch | `agent-browser install` re-fetches matched Chromium [V doc] · no · same step · install log |
| Browser crash / stale session | §7 relaunch from profile, re-enter step by entry URL · no · same step · event row |
| Profile lock (SingletonLock) | treat as second owner → park `reconciliation_required`, alert · maybe · after operator clears · lock path |
| Corrupt profile | move aside, relaunch fresh, logins redone via gates · possibly (re-login gate) · same step · moved-profile path |
| Expired handoff token | fresh token, same gate, Telegram card edits in place (§6) · Cal reopens · same gate · gate row |
| Cal cannot reach link / Tailscale down | check `tailscale status`; fallback message with `tailscale up` hint; gate waits (72 h) · Cal · same gate · status output |
| Mobile browser incompatibility | viewer is plain WS+canvas; fallback = read-only stills mode (WP2 contingency) · no · same gate · UA string |
| Multiple tabs/popup | CDP auto-attach covers new targets [V supervisor]; viewer shows active target switcher; worker steps always re-enter by URL · no · same step · frame tree |
| Provider popup/dialog | dialog surfaced in viewer during gates; worker steps use `dialog_policy must_respond` [V config] → step fails safe to gate · sometimes · same step · dialog text |
| File upload requested | V0: gate instructs Cal to complete that provider flow on own device (account-bound) · Cal · verify probe · gate note |
| CAPTCHA / bot challenge | open `awaiting_cal` gate on the live session (viewer solves it in-session) · Cal · same step · screenshot before gate |
| 2FA timeout / abandoned gate | §6 TTL/reminders/72 h `timed_out`, resumable · Cal · reopened gate · gate ledger |
| Page structure changed | only affects Cal-facing pages (worker is API-first §7) — update runbook text; no code hotfix needed · no · same gate · screenshot |
| Unexpected redirect during automation | adapter pins API hosts + no-redirect handler [V registrar client]; browser leg: origin check fails closed [V executor] · no · step fails safe · redirect target logged |
| Browser restart during a payment/challenge gate | profile preserves the provider session; reopened gate resumes the provider's own flow; if provider state ambiguous → verify probe decides · Cal · same gate · probe result |

**Porkbun/domain**
| Condition | Handling |
|---|---|
| Credentials absent/invalid / `IP_NOT_ALLOWED` | key gate: Cal creates/edits key with server IP restriction; re-ping · Cal · step 1 · ping error code |
| API registration unavailable (apiRegisterable false / prior-registration rule) | Leg B or viewer registration (C-P4) — flow unchanged from approval onward · Cal (checkout click) · step 7 · requirements response |
| Domain taken between check and purchase | dryRun catches first; create failure is definitive → next-ranked candidate re-packet · Cal (new approval) · step 5 · both responses |
| Price changed | exact-cost mismatch fails create; re-quote → re-packet (fingerprint change) · Cal · step 5 · quotes |
| Insufficient credit | dryRun `sufficientFunds:false` → top-up gate · Cal · step 5 · dryRun body |
| `.com.au` eligibility failure | excluded by default (no ABN); if attempted with ABN and rejected → fall back to `.com` per packet ranking · Cal informed · step 4 · registry response |
| Registration timeout / response lost | `uncertain_external_state` → 24 h idempotent replay window + domain-list re-read (§9.1.7) · only on conflict · reconcile · request id |
| Duplicate purchase risk | Idempotency-Key + single-attempt + reconciliation — dual protection [D 2026-08-02 + V house rule] · no · — · key |
| DNS mutation fails / wrong target | re-read + diff vs desired; correct via edit (reversible); snapshot restore for rollback · no · step 9 · snapshots |
| Propagation delay | poll, don't block; Shopify build proceeds; verification waits on resolvers · no · verifying · dig outputs |
| Provider outage | reads retry/backoff; mutations never blind-retry; park step with alert · no · same step · error series |

**Shopify**
| Condition | Handling |
|---|---|
| Account doesn't exist / login rejected / CAPTCHA / 2FA | G-store or login gate in viewer · Cal · same gate · probe |
| Subscription/payment required | G-plan gate (expected, scheduled) · Cal · §9.2.8 · plan query |
| Admin UI changes | affects only Cal-guided gates → runbook text update · no · same gate · screenshot |
| API unavailable / theme editing blocked / Forms gap | three-deep fallback §9.2.4; Forms not used in V1 (Dawn native form) · no · same step · API errors |
| Form submission fails / subscriber not recorded | verification T-fixture fails → diagnose (consent setting, section config) → fix upsert → re-verify · no · verifying · customer query result |
| Custom domain cannot connect / SSL pending | re-check DNS truth; SSL polls up to 48 h [D]; beyond → provider status page + park · no · step 9 · domain status |
| Storefront still passworded post-approval | publish probe fails → re-run removal step (idempotent) → else viewer gate · maybe · step 11 · probe |
| Trial expires mid-build | plan gate moves earlier (G-plan already precedes publication; build works on trial) · Cal · §9.2.8 · plan query |
| Shopify outage | as provider outage above · no · same step · status evidence |

**Business/compliance**
| Condition | Handling |
|---|---|
| No ABN | `.com` path (default); `.com.au` deferred — recorded in receipt as unresolved-by-design · Cal decision · — · packet |
| Brand/trademark concern | collision scan evidence → Cal picks next brand/domain in packet · Cal · step 4 · scan results |
| Privacy/consent wording unresolved | facts gate blocks build (not registration) until sign-off · Cal · planning · facts packet |
| Supplier/warranty/delivery/refund unresolved | irrelevant in V0 (no commerce claims, no checkout) — structurally enforced §10.3 · no · — · claim-scan |
| Cal requests checkout early | refuse in-product: `commerce_launch` returns the §2 prohibition + what evidence would unlock it; no override flag exists in V1 · Cal informed · — · event row |

**Safety/external state**
| Condition | Handling |
|---|---|
| Secret appears in logs | redaction layers [V ×3]; if a sweep test ever fails: rotate the affected credential, purge journal, add pattern — in the same WP · Cal (rotation) · same WP · sweep diff |
| Cookie/session material in job state | `reject_forbidden_data` throws at write time [V]; incident = fix caller + add regression · no · same WP · exception |
| Unauthorised user opens handoff link | Tailscale identity mismatch → 401 + audit log [V middleware]; token alone grants nothing · no · — · audit line |
| Replayed approval | single-use hashed button tokens + Cogitator single-use tickets + fingerprint binding [V] · no · — · token store |
| Duplicate job | store-level unique active key [V] · no · attach · job row |
| Uncertain DNS/publication state | uncertainty states + provider re-read (§8) · on conflict · reconcile · action row |
| Server crash after irreversible click | job store + Idempotency-Key + reconciliation (§9.1.7) — the exact scenario the design centres on · on conflict · reconcile · request id |
| Telegram delivery fails | watcher retries with backoff; gates never depend on delivery (link also visible via `/store status`) · no · — · delivery log |
| Receipt persistence fails | receipt built from the ledger — rebuildable idempotently; failure alerts and blocks `completed` until written · no · verifying · event rows |

---

## 14. Security and threat model (V0-practical, no theatre)

| Threat | Minimum practical mitigation (V0) |
|---|---|
| Unauthorised access to the handoff surface | loopback bind + tailnet-only Serve + `Tailscale-User-Login` pinned constant-time to Cal + Host pinning + trusted-proxy check — all existing, verified [V][D 2026-08-02]. No new exposure; Funnel untouched |
| Session hijack of the viewer | per-gate hashed single-active token, 30-min TTL, one controller lock, WS closed on invalidation (§6) |
| CSRF | existing middleware: Origin pinning + `X-CSRF-Token` on writes [V]; gate WS upgrade requires the same identity + token |
| Token replay | button tokens single-use (pattern [V]); gate tokens hashed + status-checked; Cogitator tickets single-use hashed audience-bound 5-min TTL [V] |
| Secret leakage (logs/receipts/Telegram) | credentials only in 0600 files / `$CREDENTIALS_DIRECTORY`; three redaction layers reused [V]; forbidden-field screens on both stores [V]; sweep tests T-sec |
| Browser-profile theft | 0700 dirs under the user account; profile holds only provider session cookies (§7); host compromise = game over anyway (accepted V0 boundary — single-user server) |
| Telegram impersonation | fail-closed user-ID allowlist [V]; buttons validate chat+user+message binding server-side (pattern [V]) |
| Duplicate approvals / double spend | exact-quote fingerprint binding + budget reservation + idempotency keys + single-attempt rule [V][D] |
| Provider-page spoofing / malicious redirects | adapters pin API hosts + refuse redirects [V]; browser purchase leg keeps origin-equality checks [V]; the viewer shows the real session URL bar rendering to Cal |
| Prompt injection via provider data | provider responses enter the LLM only as quoted data in typed packets; the worker/adapters never interpret prose; action schemas have no free-text executable field [V pattern; T-inject] |
| Uncertain external mutations | §8 uncertainty states; reads-only reconciliation; human decision on conflict [V pattern] |
| Log/screenshot leakage | evidence dir 0600; no frames persisted during gates (§6); screenshots taken only outside gate windows |
| Server compromise boundary | unchanged from today: attacker with user access owns the browser profiles and job DB but not payment credentials (root credstore [V]) and cannot mint Cogitator approvals (server-side TTL'd tickets, Railway-side state) — same boundary the purchase stack shipped with [V] |
| Kill switch | `commerce.enabled` config flag checked by worker each step; `systemctl --user disable --now virgil-commerce`; Cogitator ticket revocation [V]; documented in the runbook |

---

## 15. Test plan

Hierarchy (all series run in CI except the live smokes; house style: stdlib fakes, no new
deps [V]):

- **Unit:** every new module; fixture-driven (`tests/fixtures/porkbun_api_v3/*` extended,
  `tests/fixtures/shopify_admin/*` new).
- **State machine:** full v2 transition matrix incl. illegal transitions, timeout sweep,
  gate open/complete/invalidate, fingerprint invalidation (extends the 1,189-line #88
  suite).
- **Provider fake servers:** loopback Porkbun fake (registration happy/timeout/ambiguous/
  409-replay/insufficient-funds/rate-limited; DNS CRUD) and Shopify GraphQL fake (shop
  identity, upsert double-apply, customer-by-email, domain status, password state).
- **Browser integration:** fake-CDP stream tests (WP2); one real headless-Chromium test
  driving agent-browser against a loopback page (navigate/screenshot/profile-persist).
- **Security:** T-sec-1 secret sweep (keys/tokens never in logs/spool/receipts/Telegram
  renders); T-sec-2 forbidden-data rejection both stores; T-sec-3 no-PII persistence;
  T-inject (instruction-like provider strings produce no action/approval/plan change);
  viewer auth matrix (no identity header / wrong user / tagged-node / bad Host / bad
  token / expired / replayed → 401/403/410).
- **Restart/recovery:** kill -9 worker mid-step → recover parks or resumes correctly;
  browser daemon killed → profile relaunch; gateway restart mid-gate → cards re-render.
- **Idempotency:** action replay returns stored result; Porkbun Idempotency-Key replay;
  Shopify upsert double-apply; duplicate job attaches.
- **Telegram routing:** T-route-1 exact acceptance sentence → `commerce_launch` (not
  intake — regression against #120 fixtures [V]); `/store` family; busy-session queueing.
- **Human-gate:** open→link→DONE→verify-probe→resume; abandon→TTL→timeout→resume;
  link expiry/replay; single-controller.
- **Live smokes (supervised, in order):** (1) read-only Porkbun discovery (WP0);
  (2) gate-viewer smoke on a harmless page (WP2); (3) WP5 launch itself — domain
  registration is the first live mutation and happens only there.

**Acceptance fixtures (exact):**
- `tests/fixtures/acceptance/telegram_request.json` — message text
  **"Set up the AMD GPU waitlist store."** from Cal's allowlisted Telegram ID; expected:
  one job created (or attached), state `requested→planning`, no intake packet created,
  first Telegram reply is a job card.
- `tests/fixtures/acceptance/decision_packet.json` — 10-candidate availability table,
  recommendation `siliconcurrent.com`, exact dryRun cost in USD cents and AUD display,
  approval button token schema.
- `tests/fixtures/acceptance/gate_card.md` — rendered gate message: one action sentence,
  one `https://virgil-server.tailce4511.ts.net:8443/gate/…` link, no secrets, no second
  link.
- `tests/fixtures/acceptance/receipt.json` — golden §16 receipt with every field present
  and the no-payment attestation true.
- `scripts/commerce_fake_e2e.py` — one command that replays the whole flow against both
  fake providers + fake CDP, asserting the golden receipt (the E2E-loopback rehearsal;
  CI-run).

---

## 16. Durable execution receipt (schema)

Persisted at `~/.hermes/commerce/receipts/<job_id>.json` (0600) + summarized to Telegram;
rebuildable from the ledger at any time:

```json
{
  "job_id": "jb_…", "state_machine_version": 2, "completed_at": "…Z",
  "objective": "AMD GPU waitlist store launch",
  "actions_completed": [{"step": "…", "at": "…Z", "evidence": "evidence/jb_…/….png"}],
  "domain": {"name": "…", "registrar": "porkbun", "order_id": "…",
             "spend": {"amount_usd_cents": 0, "display": "…"},
             "cogitator": {"proposal_id": "…", "approval_id": "…", "receipt_ref": "…"},
             "auto_renew": true, "whois_privacy": true},
  "shopify": {"myshopify_domain": "…", "shop_id": "…", "plan": "…", "admin_url": "…"},
  "public_url": "https://…", "dns": {"status": "propagated", "records": ["A", "AAAA", "CNAME www"]},
  "waitlist_test": {"result": "pass", "test_address_used": "…", "consent_recorded": true,
                     "test_subscriber_deleted": true},
  "human_gates_completed": [{"gate_id": "cg_…", "type": "…", "opened": "…Z", "verified": "…Z"}],
  "verification": {"checklist": "9.3", "all_green": true, "evidence_bundle": "evidence/jb_…/"},
  "unresolved": [".com.au deferred pending ABN"],
  "no_payment_collected": true,
  "checkout_absent_verified": true,
  "total_spend": [{"provider": "porkbun", "amount": "…"}, {"provider": "shopify", "amount": "plan billed to Cal's card at trial end"}]
}
```

---

## 17. Open decisions for Cal (only these; everything else is decided here)

1. Brand/domain: approve the recommendation the decision packet will carry (default
   ranking starts at `siliconcurrent.com`).
2. Spend: approve the exact domain quote (~US$11–13 for `.com`; live-quoted) and accept
   that Shopify Basic bills Cal's card at trial end.
3. `.com` now (default) vs waiting for ABN + `.com.au`.
4. Facts packet (§10.2): contact email, identity sentence, double opt-in, privacy
   sign-off.
5. Branch hygiene: permission to delete the §3.3 parked branches (optional).

Not asked (decided in this plan): job-store placement, state machine, gate technology,
registrar, platform, theme, content structure, worker/service topology, branch strategy,
test scope.

---

## 18. Plan self-check (against the operating mandate)

Composes existing capabilities — the only genuinely new subsystems are the worker loop,
the gate viewer, and two adapter extensions; no parallel framework, no second job store,
no new datastore, no new public surface. Ends at a live launch with a receipt, not at
"pieces ready for wiring". Codex proceeds WP0→WP5 without returning for design decisions
(§12); every listed unknown [U-1..U-4, U-P] has a named resolving task inside a WP, not a
new planning round. Every externally verified claim is dated; repository claims were
verified against code, runtime services, live Tailscale/systemd state, and GitHub PR
state on 2026-08-02 — not against old plans or PR descriptions.
The mandate's runbook, acceptance-test, checklist, and prompt requirements are carried by
§§19–25 below; the v2 report's evidence base is Appendix A.

---

## 19. Deployment and rollback runbook

Ordering rule: services are installed **inert** and enabled explicitly; DB files are
never deleted on rollback (house rule [V]); every rollback below is executable without
Codex present.

| Stage | Deploy action | Verify | Rollback |
|---|---|---|---|
| WP0 (#88 merge + v2 states) | none (code only) | CI green on `main`; `tests/test_commerce_jobs.py` passes | `git revert` the merge commit; no data exists yet |
| WP1 worker + Telegram | `loginctl enable-linger v0id`; install `packaging/virgil-commerce/virgil-commerce.service`; `systemctl --user enable --now virgil-commerce` | unit `active`; fake-provider job reaches `ready`; restart mid-run resumes | `systemctl --user disable --now virgil-commerce`; revert commit; `~/.hermes/commerce/commerce_jobs.db` kept |
| WP2 gate viewer | drop-in `ReadWritePaths=/home/v0id/.hermes/commerce`; `systemctl --user restart virgil-mobile` | existing PWA unaffected; gate smoke (§15 live smoke 2) passes; auth matrix tests green | revert commit + restart virgil-mobile (routes are additive; PWA reverts cleanly) |
| WP3 Porkbun legs | none (library + worker steps) | loopback E2E green; live dryRun `wouldSucceed: true` recorded | revert commit |
| WP4 Shopify adapter + content + verify | none (library) | fake-store E2E build+verify green; content byte-stable | revert commit |
| WP5 live launch | runtime only (no new code) | §21 acceptance test passes; §16 receipt delivered | store re-password (one idempotent step or viewer gate); DNS snapshot restore (§9.1.9); Cogitator reservation release via existing cancel action; **domain purchase is non-rollbackable** — disclosed in the approval packet before Cal approves |

Cross-cutting: kill switch = `commerce.enabled: false` + `systemctl --user disable --now
virgil-commerce` + Cogitator ticket revocation (§14). Backup: nightly copy of
`commerce_jobs.db` into `~/.hermes/backups` [V dir exists]. Migrations: additive-only;
the commerce DB ships fresh (no legacy data). Deployment order across the project:
hermes only; Cogitator is redeployed **never** (unless WP0 proves the [U-1] policy gap,
in which case exactly one additive policy insert via Cogitator's own normal deploy).

---

## 20. Same-day launch runbook (WP5, elapsed timeline)

Preconditions (all true before T-0): WP0–WP4 merged, CI green; `virgil-commerce` and
`virgil-mobile` units active; §22 Cal checklist complete; `commerce.enabled: true`.

| T | Actor | Action | Expected | Abort/park path |
|---|---|---|---|---|
| T-0 | Cal | send the §25 command in Telegram | job card within seconds; state `requested→planning` | no card ⇒ `/store status`; watcher/journal check; do not resend more than once (dedupe attaches anyway) |
| T+2 min | Virgil | candidate availability + pricing (paced 1/10 s), dryRun on top pick | decision packet: domain, exact USD cost, AUD display, evidence | insufficient funds ⇒ top-up gate (C-P·5); all candidates taken ⇒ re-ranked packet |
| T+5 min | Cal | tap **Approve** on the exact domain+price packet | Cogitator proposal→reserve; registration + DNS records created; evidence captured | approval TTL 15 min — expired ⇒ fresh packet; create failure ⇒ next candidate |
| T+10 min | Virgil | opens **G-store** gate | one Telegram message, one private gate link | link unreachable ⇒ `tailscale status` on phone; gate waits ≤72 h |
| T+10–30 min | Cal | Shopify signup (email verify, store name = approved brand); then **G-token** gate: create custom app, stage token per runbook card | `shop { id … }` probe passes; shop pinned to job | CAPTCHA/2FA solved in the viewer; account steps may be done on Cal's own device — probe is identical |
| T+30 min | Virgil | store settings, theme settings, pages, waitlist form (password ON) | build steps all green in `/store status` | any upsert failure ⇒ diagnosis card, fix loop (§8 `verifying→ready`) |
| T+40 min | Cal | **G-plan** gate: pick Basic plan + billing card | plan probe no longer trial-tier | trial limitations block publish only — build already done |
| T+45 min | Virgil | domain connect + SSL poll + §9.3 verification | verification report all green (SSL/propagation may lag — job polls, doesn't block) | DNS/SSL pending ⇒ job parks in `verifying`, resumes on propagation (≤48 h worst case) |
| T+50 min* | Cal | tap **Approve publication** | password removed; public re-verify; §16 receipt delivered to Telegram | verification regression re-gates automatically |

*T+50 min is the no-wait path; DNS/SSL propagation may stretch the tail to hours without
any action needed from anyone. Total Cal hands-on time: ~30–45 min. Every gate card
contains its own instructions; this table is the supervisor's view, not a substitute.

---

## 21. Final end-to-end acceptance test (the launch IS the test)

Run exactly once, live, as WP5. Pass = all of the following, each with persisted
evidence in the job ledger:

1. The §25 command from Cal's allowlisted Telegram account creates (or attaches to) one
   job; no intake packet is created (T-route-1 behaviour, live).
2. A decision packet with live availability and an exact dryRun-quoted price is
   delivered; Cal's single approval registers the domain via Cogitator
   proposal→approval→ticket, with `Idempotency-Key`, and creates the three Shopify DNS
   records; `record_completed_purchase` receipt exists in Cogitator.
3. Every human gate (G-store, G-token, G-plan, any challenge gates) is completed through
   the tailnet gate viewer or Cal's device, and each completion is confirmed by its
   provider-truth verify probe — never by DONE alone.
4. The full §9.3 verification checklist is green on the public site: HTTPS 200 on apex
   and `www` with valid SSL, byte-exact approved content, zero placeholder/forbidden-claim
   hits, waitlist signup round-trip with the test subscriber recorded (consent captured)
   then deleted, **no reachable checkout path**, DNS answers correct from two public
   resolvers, domain active at Porkbun.
5. The §16 receipt is persisted at `~/.hermes/commerce/receipts/<job_id>.json` and
   summarized to Telegram, with `no_payment_collected: true` and
   `checkout_absent_verified: true`, and all spend itemized against Cogitator references.
6. `systemctl --user restart virgil-commerce` at any point during the run resumes the
   same job and browser session without duplicated mutations (may be exercised
   opportunistically during a wait state; mandatory in the WP1 rehearsal if skipped live).

Fail = any item above unmet after its documented contingency (§13) is exhausted; the job
parks with a diagnosis and the failure is reported with evidence, not retried blind.

---

## 22. Cal-only preparation checklist (nothing else is asked of Cal)

Before T-0 (once each):

- [ ] Telegram: confirm your account is the allowlisted user (`TELEGRAM_ALLOWED_USERS`)
      and the bot chat is reachable.
- [ ] Phone: Tailscale connected (`redmi-note-13-5g` on the tailnet); open
      `https://virgil-server.tailce4511.ts.net:8443` once to confirm identity login works.
- [ ] Porkbun: account exists with email **and** phone verified; know whether the account
      has ever registered a domain (never ⇒ the first registration happens in the gate
      viewer, contingency C-P4 — no schedule impact).
- [ ] Porkbun API key: created with the server's egress IP restriction and staged via the
      documented one-command step into `~/.hermes/secrets/porkbun.env` (0600). If not
      done, the run opens a key gate — acceptable, just slower.
- [ ] Porkbun funding: account credit ≥ US$15 (or be ready to top up in the gate viewer
      when the dryRun reports `sufficientFunds: false`).
- [ ] Facts packet answers ready (§10.2): contact email · business identity sentence ·
      double opt-in yes/no · brand sign-off · privacy-policy sign-off · ABN decision
      (default: none ⇒ `.com`).
- [ ] Shopify: an email address for signup and a billing card for the Basic plan
      (billed at trial end — this is the only recurring spend).
- [ ] Time: ~30–45 minutes of interruptible availability for the gates.
- [ ] Optional: nod (or decline) the deletion of the §3.3 parked branches.

Nothing on this list requires technical work beyond following a gate card.

---

## 23. Codex master execution prompt (exact, self-contained)

This is the complete prompt handed to Codex. It mandates continuous execution of the
entire approved plan, WP0 through WP5, with no per-package replanning stops.

```
ROLE: You are Codex, the sole implementing engineer for the Virgil Ecommerce
Operator V1. The complete approved plan is
docs/VIRGIL_ECOMMERCE_OPERATOR_MASTER_PLAN.md on main of
/home/v0id/.hermes/hermes-agent (origin 3ndym10n/hermes-agent). That document
is authoritative and already approved: do not redesign it, do not produce a
new plan, do not return for planning decisions.

MANDATE: Execute the ENTIRE plan, WP0 through WP5 (plan §11), CONTINUOUSLY.
Do not stop after WP0. Do not stop between work packages to report and wait.
A finished work package with green validation flows directly into the next
one. You are done when the WP5 acceptance test (plan §21) has passed and the
§16 receipt is delivered, or when a genuine blocker (defined below) is hit.

SETUP (read-only first): git fetch origin; git status; git worktree list.
Never reset/clean/checkout --force/stash-drop anything. Preserve every
branch, worktree, and untracked file not named by the plan.

BRANCH/PR STRATEGY (plan §11, binding):
1. WP0: amend PR #88 on its existing branch feat/commerce-s2-job-store
   (state machine v2 per plan §8), merge it to main when green.
2. All remaining code lands on ONE integration branch
   feat/virgil-commerce-operator-v1 off post-merge main, one logical commit
   per work package, opened as ONE PR, kept green (tests/lint/typecheck),
   rebased on main daily. No other PRs; no Cogitator changes unless WP0
   proves the [U-1] policy gap, in which case exactly one additive policy
   insert.

EXECUTION POLICY (plan §12, binding — apply in this order at every step):
- Successful validation (tests + lint + typecheck + the WP's acceptance
  criteria): CONTINUE AUTOMATICALLY to the next step/work package.
- Documented failure (a condition listed in plan §13 or a WP contingency):
  execute the plan's contingency, note it in the commit message, continue.
- Test failure caused by your change: fix it in the same WP commit and
  continue. Never skip, xfail, or delete a failing new test.
- Pre-existing unrelated failure: prove it unrelated (same failure on clean
  main at the merge base; put the one-command proof in the commit message)
  and continue.
- Human gate (login/2FA/CAPTCHA/payment/KYC/legal acceptance, plan §2/§6):
  pause ONLY the affected runtime job step, ensure the gate card with its
  single private link is delivered, and continue engineering work elsewhere
  wherever the plan allows it safely. Engineering never waits on a gate.
- Irreversible external action (domain registration, publication): only via
  the plan's recorded approval flow (Cogitator ticket / bound approval).
  Request Cal's explicit approval through that flow; NEVER self-approve,
  including against real providers from staging.
- Undocumented security risk (a step would move a secret across a boundary
  the plan does not describe) or no safe documented contingency: STOP that
  path and return exactly one precise blocker naming the WP, the failing
  step, the evidence, and the single decision needed. This is the only
  stop-and-ask condition.
- Ordinary engineering choices (naming, function structure, test layout,
  fixture shape) are yours. Do not return for them.

HARD LIMITS: no secrets in the repo, logs, PRs, or Telegram; no new
third-party dependencies (stdlib-first house rule); no checkout/payments
features of any kind (plan §2 prohibitions are hard-fail); no changes to
Cogitator beyond the single [U-1] policy insert if proven necessary; no
force-push to main; DB files are never deleted.

VALIDATION: every commit passes the CI suites (tests.yml, lint.yml,
typecheck.yml) plus the plan §15 series relevant to the WP. Live smokes in
plan §15 order: WP0 read-only Porkbun discovery; WP2 gate-viewer smoke; the
WP5 launch itself is the final acceptance test (plan §21).

REPORTING: one running log comment on the integration PR per work package
(what shipped, validation evidence, contingencies exercised); the final
report is the delivered §16 receipt plus the PR in a mergeable green state.
```

---

## 24. Final Fable review prompt (exact)

Given to Fable (Claude Code) when Codex reports the integration PR complete and before
the WP5 live launch is attempted:

```
You are Fable, performing the final pre-launch review of the Virgil Ecommerce
Operator V1. The authoritative plan is
docs/VIRGIL_ECOMMERCE_OPERATOR_MASTER_PLAN.md on main. Codex has merged
PR #88 (WP0) and delivered the integration PR feat/virgil-commerce-operator-v1
covering WP1–WP4.

Verify, with evidence from the code and CI (not from PR descriptions):
1. Conformance: the implementation matches plan §§4–10 — one job store (no
   second store), deterministic worker with no model calls, gate viewer only
   on the tailnet-identity surface, adapters API-first, approvals bound to
   fingerprints, money only via Cogitator tickets.
2. Security: plan §14 mitigations are implemented and tested — run the T-sec
   sweep, the viewer auth matrix, T-inject, and the forbidden-data tests
   yourself and quote the results. Confirm no secret, token, or cookie can
   reach the job DB, logs, receipts, or Telegram renders.
3. State machine: the merged v2 machine matches plan §8 exactly, including
   uncertainty states and single-attempt rules for irreversible actions.
4. Tests: the full plan §15 hierarchy exists and passes, including
   scripts/commerce_fake_e2e.py against the golden receipt; kill -9 recovery
   tests pass.
5. Runbooks: plan §19 deploy steps were followed (units inert-installed,
   enabled explicitly, linger on); the §22 Cal checklist has no unmet
   engineering-side prerequisite.
6. Scope: git diff against the merge base contains ONLY plan-listed files;
   no Cogitator changes beyond the [U-1] insert if it was proven; no new
   dependencies; no checkout/payment code paths.

Output: PASS with the evidence per item, or FAIL with the exact defect list
(file:line, failing test, or missing artifact) for Codex to fix on the same
branch. Do not fix anything yourself. WP5 (live launch) may only proceed on
PASS.
```

---

## 25. Virgil live acceptance command (exact)

Sent by Cal, from his allowlisted Telegram account, in the existing Virgil chat:

```
Set up the AMD GPU waitlist store.
```

Expected immediate behaviour (plan §4.1, T-route-1): one commerce job is created (or
attached, if one is active), no intake packet is created, and the first reply is a job
card — "Checking what already exists…". Everything after that is the §20 runbook.
Deterministic fallback if the sentence is ever misrouted: `/store status` and the
`/store` command family drive the same job with no LLM in the loop.

---

# Appendix A — Second-pass planning report (v2, 2026-07-24), merged verbatim

**Status:** historical evidence base, merged into this canonical document so that the
master plan's citations ("Appendix A §n") resolve without a second plan file. Its
repository evidence, governance analysis, security patterns, red-team plan, and
gap analysis remain valid and are relied on above. Its **launch-shape decisions are
superseded** by the master plan: the validation method is the no-payment waitlist (not a
full-payment preorder), there is no checkout/test-order/payment-activation scope in V1,
and the slice plan S1–S9 is replaced by work packages WP0–WP5 (plan §11). Where this
appendix and the master plan conflict, the master plan governs. Section numbers cited
below (e.g., "Appendix A §19") refer to this appendix's own internal numbering. Headings
are demoted one level from the original standalone file.

## Governed Ecommerce Launch Executor — Codex Execution Package (v2)

**Second-pass technical planning and self-review. Produced 2026-07-24 by Fable (Claude Code).**
Supersedes the v1 report on this branch. Research/architecture/planning only — no production code, purchases, or provider mutations were made while producing it.

Evidence tags: **[V]** verified in repository/GitHub · **[D]** verified in current official provider documentation (retrieval date given) · **[I]** engineering inference · **[U]** unresolved, requires live verification.

**Input note:** the "GPT critique" referenced by the assignment was not provided in-session and no critique document exists in either repository (`plans/`, Cogitator `docs/hermes/` searched [V]). This revision is therefore an independent second pass against repo + provider evidence only.

**Material decisions changed from v1** (full rationale inline):
1. Operational commerce-job store moved from Cogitator SQLite to a **Hermes-owned SQLite database**.
2. Issue #65 live browser-purchase acceptance reclassified from *strict prerequisite* to **parallel safety milestone**; the registrar-API purchase path gets its **own** supervised acceptance gate.
3. First-release validation method changed from Downpay deposit app to **full-payment preorder** (deposits deferred).
4. Porkbun *API-based registration* downgraded from assumed-supported to **[U]** with a verified fallback (the existing browser purchase executor, already Porkbun-allowlisted [V]).
5. Test checkout re-sequenced: Shopify **test orders require a paid plan [D]**, so plan selection/subscription approval must precede test checkout, and test mode must be disabled before public launch.
6. No custom theme in v1: **Dawn (default free theme) + pages + theme settings**, snapshot in git via Shopify CLI.

---

### 1. Executive verdict

**What already exists (verified, merged, security-reviewed) [V]:**
- A complete governed **outgoing-purchase control plane** in Cogitator (`cogitator_purchase_governance.py`, 1,871 lines; PRs #1053/#1054/#1056/#1057/#1059/#1061 merged; `docs/PURCHASE_GOVERNANCE_V1.md`): exact-quote approvals, atomic budget reservations, single-use hashed audience-bound execution tickets (5-min TTL), 15-min approval TTL, idempotency fingerprints, term-mismatch invalidation, uncertain-result reconciliation, sanitized receipts, refunds, asset registry, append-only audit events.
- A deterministic one-shot **browser purchase executor** in hermes (`purchase_executor.py` V0.3, PR #76 `0bfbec94b`; `purchase_discovery.py`, `purchase_merchants.py`), with hardened systemd packaging (`packaging/purchase-executor/`), operator CLI (`scripts/purchase_operator_cli.py`), 108+ passing tests, two Chromium fake-E2Es, independent Security and Build review PASS (issue #65 comment 2026-07-22).
- Fail-closed **user identity gating** (`gateway/authz_mixin.py`; Telegram deny-by-default allowlist, `gateway/platforms/telegram.py:574-582`).
- A **sandboxed local browser** with mandatory cleanup (`sandbox_bypass: never`, recording off, private URLs blocked — `~/.hermes/config.yaml` browser section [V]; acceptance closed 2026-07-18 per #65 spec).
- Cogitator **approved-knowledge flow**: promoted retrieval records, provenance, human review, no auto-promotion, lesson candidates [V].

**Partially complete:** durable-job patterns (research jobs, purchase lifecycle) exist but there is no commerce job object; approval identity is a confirmation phrase, not bound to a hardware factor; merchant allowlist is Porkbun-only; renewal data exists (`purchase_assets`) without alerting.

**Genuinely new:** registrar/DNS API adapter, commerce job state machine + Hermes job store, provider-account verifier, Shopify Admin adapter + theme snapshot flow, grounded page/copy builder, verification engine, human-gate polling, handover/lessons.

**Blocks the first release (true blockers):**
- **B1** Missing business facts (entity/ABN, brand, SKU, price, preorder terms) — Cal input, gates the acceptance case not the build.
- **B2** Shopify store does not exist; creation is dashboard-manual [D] — a ~15-minute Cal gate.
- **B3** Porkbun API registration capability/funding unverified [U] — resolved by S1 read-only discovery; fallback path exists either way.
- **B4** Payment credential staging (`systemd-creds`, Cal sudo) if the browser-executor purchase path is used [V].

**The one recommended path:** extend the proven control-plane/executor split — Hermes owns a new commerce job state machine and deterministic provider adapters; Cogitator remains the sole authority for money (proposals→approvals→tickets) and approved facts; the first supervised live action is the **governed domain registration**, and the first customer-facing milestone is a **password-protected Shopify store with one full-payment preorder product, verified by a test order on the Bogus gateway**, followed by Cal-approved public launch.

| Area | Completed | Partial | Missing |
|---|---|---|---|
| Outgoing-purchase governance | ✅ | | |
| Browser purchase executor + packaging | ✅ (live acceptance pending) | | |
| Identity gating, sandboxed browser, audit | ✅ | | |
| Durable commerce job + state machine | | | ❌ |
| Registrar/DNS API adapter | | | ❌ |
| Shopify adapter/theme/store flow | | | ❌ |
| Provider-account verification | | | ❌ |
| Verification engine, human-gate polling | | | ❌ |
| Approved AMD business facts | | | ❌ |
| Cogitator knowledge/lesson flow | ✅ | | |

---

### 2. Recovered repository and worktree state (re-inspected 2026-07-24, post-fetch)

#### hermes-agent (`/home/v0id/.hermes/hermes-agent`; origin `3ndym10n/hermes-agent`, upstream `NousResearch/hermes-agent`) [V]
- `origin/main` = **`8bd8bfe6045a9b193afaec5026cc9cf5a5641b16`** (Linxio sent-mail style bootstrap, #84). Local `main` = `5600ea084` (behind 2 commits after the fetch; fast-forwardable; intentionally left untouched).
- This branch `review/ecommerce-launch-executor-plan` = `3918cf698` (v1 report), pushed.
- Working tree otherwise clean. Worktrees: `hermes-agent-bulk-style` (detached `8bd8bfe60`, clean), `hermes-agent-google` (`feat/linxio-selected-source-lessons`, clean except two zero-byte junk files `--auth-code`, `--service-profile` — no secrets, ignorable), `/tmp/hermes-intelligent-intake-x-article-recovery` (clean).
- Open issues: **#65** (purchase executor — held open for the supervised live acceptance), #77 (Linxio), #43 (async research replies). **No open PRs; no closed-unmerged PRs.**

#### Cogitator (`/home/v0id/Projects/Cogitator_clean`; origin `3ndym10n/Cogitator`) [V]
- `origin/main` = **`ffc35642113bda9de440b89c692801a47eaba429`**. Recent purchase-relevant merges: `462d3fd` "Bind exact checkout terms into purchase tickets" (#1061), `11737bf` checkout targets (#1059), `809db08` operator bridge (#1057).
- Local checkout on `agent/purchase-operator-bridge-v0` @ `809db08` — **merged into origin/main**; local is simply behind. `feat/purchase-ticket-terms-v0` is patch-equivalent merged (`git cherry` = `-`; `462d3fd` on main). Nothing stranded.
- Untracked: `docs/hermes/` (copies of three hermes plan docs incl. `PURCHASE_EXECUTOR_V0_ISSUE_SPEC.md`), `storage/intake/*` capture files — data/notes, preserve untouched.
- ~19 worktrees (lanes 557–572, 606/607, purchase-*, linxio) — **all clean** [V]. Stale; prune is optional housekeeping, not done.
- Open PRs #794, #793 (June, unrelated). Open issues include #1003 (AMD venture flywheel), #1019 (ISB V1).

**Safe-to-preserve statement:** every branch, worktree, and untracked file listed above is untouched by this plan; the only repository change on this task is this report on this branch.

#### Business-fact state [V]
- `storage/promoted/2026-06-30-ai-first-ecommerce-validation-playbook.md` (approved): validate demand via preorder/deposit before building store infrastructure.
- `docs/research/RAW_BUSINESS_AI_GPU_INTAKE_PACKET_V0.md`: **no GPU/AMD/supplier facts exist** — the "gpu" raw dump contained none. No brand, domain, pricing, warranty, supplier, or preorder-term decisions exist anywhere in either repo.

---

### 3. Existing capability map

(Condensed; unchanged findings from v1 re-verified against `origin/main` where they matter.)

| Capability | State | Evidence |
|---|---|---|
| Exact-quote approval, one-time tickets, TTL, replay/stale rejection | verified complete | `cogitator_purchase_governance.py` (`APPROVAL_TTL_SECONDS`, `EXECUTION_TICKET_TTL_SECONDS`, hashed tokens, constant-time compare) [V] |
| Budgets/reservations/idempotency/receipt sanitisation/reconciliation/assets/audit | verified complete | same module; tables `purchase_budgets…purchase_refunds`; `docs/PURCHASE_GOVERNANCE_V1.md` [V] |
| Checkout-term binding into tickets | verified complete | `462d3fd` on main; `checkout_target` normalization at `cogitator_purchase_governance.py:450` [V] |
| One-shot deterministic checkout executor, no model calls, credential isolation, single submit, cleanup-always | verified complete (code); live acceptance pending | `purchase_executor.py` docstring + tests; issue #65 comments [V] |
| Merchant allowlist | Porkbun only | `purchase_merchants.py` (`V0 live allowlist: Porkbun only`) [V] |
| systemd hardening + credential staging pattern | verified complete | `packaging/purchase-executor/*.service`, `cal-gate.sh` [V] |
| Operator control surface | verified complete | `scripts/purchase_operator_cli.py`; Cogitator operator bridge [V] |
| Identity gating | verified complete | `gateway/authz_mixin.py`; Telegram fail-closed allowlist [V] |
| Sandboxed browser + cleanup | verified complete | config keys `sandbox_bypass: never`, `record_sessions: false`, `allow_private_urls: false`; `cleanup_browser` API [V] |
| Durable NL→job routing pattern | partial (research jobs only) | gateway research bridges (#62) [V] |
| Approved-fact retrieval, provenance, review, no-auto-promotion, lesson candidates | verified complete | promoted records; ISB flow [V] |
| Registrar API / DNS management / Shopify code / commerce job / account verifier / verification engine | missing | no code in either repo (searches over `git ls-files` + `git grep`) [V] |
| Shopify knowledge | documented but absent | `optional-skills/productivity/shopify/SKILL.md` is a skill doc, not an adapter [V] |

---

### 4. Gap analysis

Every gap is one of four kinds:

1. **New deterministic adapters** (registrar/DNS, Shopify Admin, verification): pure new code in hermes, testable offline against fakes.
2. **New durable commerce-job layer** in Hermes: state machine, job store, fingerprints, gates, recovery. Patterns proven in Cogitator purchase lifecycle [V]; the code is new.
3. **Governance policy extension** in Cogitator: a `commerce_launch_v1` policy (larger budget, classes for `saas_subscription`/`app_subscription`/theme) — an additive change inside the existing, preserved module. No new money machinery.
4. **Facts and human accounts**: Cal-supplied (B1, B2 above); technically trivial, sequentially gating.

Nothing in the existing purchase stack needs replacement. The one structural decision the v1 report got wrong is job-store placement (next section).

---

### 5. Architecture recommendation

#### The decision that matters: where operational commerce state lives

**Recommendation (changed from v1): the commerce job store is a Hermes-owned SQLite database at `~/.hermes/commerce/commerce_jobs.db`, schema and migrations owned by `hermes-agent` (new module `commerce_jobs.py`).** Cogitator keeps exclusive ownership of purchase proposals, approvals, budgets, reservations, tickets, receipts, assets, refunds, and audit events — untouched.

Evidence and reasoning:
- The non-negotiable ownership split assigns job lifecycle, restart recovery, and provider execution to Hermes and forbids Cogitator becoming the ecommerce runtime. Physical placement should follow operational ownership here because every job-state write would otherwise be a network round-trip through the bridge, making Cogitator availability a hard dependency of *every* step, including steps with no knowledge or money content [I].
- Hermes already operates durable local SQLite state (`~/.hermes/state.db`, `kanban.db` [V]) — a commerce DB follows an existing operational pattern, not a new one.
- The proven Cogitator SQLite idioms (`BEGIN IMMEDIATE`, unique idempotency fingerprints, append-only event tables, additive `init_db`-style migrations [V]) are *patterns to copy into* `commerce_jobs.py`, not a reason to co-locate tables.

**Strongest rejected alternative:** commerce-job tables inside Cogitator's governed SQLite (v1's position). Rejected because it makes Cogitator the ecommerce runtime in fact (every transition lands there), couples all execution to bridge availability, and blurs the exact boundary this assignment requires resolving. Its one real advantage — a single audit store — is preserved anyway, because every *financial* event still lands in `purchase_events` and every job carries the Cogitator proposal/approval IDs it referenced. Confidence: high. Would change if: Hermes lost durable-state responsibilities entirely (no evidence of that).

#### Boundary contract (Hermes ⇄ Cogitator)

| Interface | Direction | Transport | Content |
|---|---|---|---|
| Approved-fact retrieval | Hermes → Cogitator | existing bridge (`/api/cogitator_bridge`, bearer token) [V] | retrieval records + provenance; read-only |
| Purchase proposal / approval / ticket / result | Hermes → Cogitator | existing operator + executor bridge actions [V] | exact-quote money flow; Cogitator authoritative |
| Lesson candidates / launch handover | Hermes → Cogitator | existing lesson-candidate intake [V] | proposed knowledge, human-reviewed, never auto-promoted |

Dependencies flow **one way: Hermes → Cogitator**. Cogitator never calls into Hermes [V — matches every existing bridge].

**Availability behaviour:** Cogitator down ⇒ commerce jobs continue read-only/safe local work, and any step needing facts, approvals, or tickets parks in a `blocked_on_cogitator` sub-status with retry/backoff; no money can move (fail-closed by construction, since tickets are unobtainable). Hermes down ⇒ nothing executes; Cogitator state is consistent (reservations/tickets expire on TTL [V]).

**Why Hermes cannot bypass purchase governance:** (a) payment credentials exist only inside the root-owned executor units via `$CREDENTIALS_DIRECTORY` — the gateway/LLM plane has no path to them [V]; (b) the executors refuse to run without a valid single-use governance ticket [V]; (c) the registrar purchase leg (S5) reuses the same ticket-gated executor pattern; (d) provider API keys for *mutating* adapters are staged the same way, and the adapters check a job-store approval reference before any consequential call — enforced by code review + tests (T-gov series, §20). The trust anchor is credential placement, not politeness [I].

**Why Cogitator cannot become the runtime:** it exposes only the bridge actions above; it holds no provider adapters, no browser, no job tables, and its schemas reject operational/credential fields (`FORBIDDEN_FIELD_TOKENS` [V]).

#### Component diagram

```mermaid
flowchart LR
  subgraph CAL[Cal — approval authority]
    TG[Telegram / operator CLI]
  end
  subgraph HERMES[Hermes — operational plane]
    direction TB
    NLI[commerce request interpreter LLM]
    JSM[commerce_jobs.py — state machine + SQLite job store]
    VER[verification engine]
    GATE[human-gate poller]
  end
  subgraph EXEC[Deterministic executors — credential plane, hermes-owned]
    REG[registrar_porkbun.py — API adapter]
    SHOP[shopify_admin.py — Admin GraphQL + CLI]
    PEX[purchase_executor.py V0.3 - existing]
    BRW[sandboxed browser fallback - existing]
  end
  subgraph COG[Cogitator — knowledge + money authority]
    FACTS[approved facts + playbooks]
    GOV[purchase governance V1 - existing]
    LES[lesson candidates]
  end
  TG -->|fail-closed identity| HERMES
  NLI -->|typed proposals only| JSM
  JSM --> VER & GATE
  JSM -->|read facts| FACTS
  JSM -->|propose/approve/ticket| GOV
  JSM -->|dispatch actions| EXEC
  GOV -->|single-use tickets| PEX
  REG --> PB[(Porkbun API)]
  SHOP --> SF[(Shopify Admin API)]
  PEX --> PBW[(Porkbun checkout)]
  JSM -->|handover, lessons| LES
```

Trust boundaries: Cal↔Hermes (identity allowlist); LLM↔deterministic (typed schemas; LLM output is proposal data); Hermes↔Cogitator (bearer bridge; one-way); executors↔providers (origin/domain allowlists; responses untrusted); credential plane (systemd credstore / 0700 secrets; never crosses inward to LLM or Cogitator).

**Failure model:** every provider mutation is (idempotency-keyed where the provider supports it [U per provider], single-attempt where irreversible [V house rule]); process crash ⇒ job store is the restart truth; an interrupted mutation whose outcome is unknowable from the response is an **uncertain external state** (defined: a write was dispatched, no definitive success/failure evidence was recorded, and re-issuing could double-execute). Recovery = provider re-read reconciliation, human decision on conflict — the exact model purchase governance already implements [V], generalized.

**Fingerprints:** `plan_fingerprint` = SHA-256 over the canonical JSON of approval-material fields (§7 list). `action_fingerprint` = SHA-256 over the action envelope minus timestamps. Approvals bind to fingerprints; any material change recomputes and orphans only the affected approvals. This generalizes the existing terms-fingerprint mechanism (`462d3fd`) [V].

**Duplicate prevention:** job-level unique key `(requester, normalized_objective, active)` — a second "launch the GPU store" request attaches to the live job instead of creating one; action-level idempotency keys + provider-side idempotence (Shopify GraphQL mutations are idempotent per input for upserts keyed on handle [I]; Porkbun [U]).

#### Smallest-sound justification
One new DB, two new adapters, one policy extension, zero new services/queues/frameworks; every security-critical mechanism (money, tickets, credentials, sandboxing, identity) is reused, not rebuilt.

---

### 6. Source-of-truth matrix

| Data | Authoritative owner | Placement |
|---|---|---|
| Business identity, ABN, legal name | Cal + government registries | cached as approved Cogitator facts w/ provenance |
| Brand, product facts, pricing, warranty, preorder terms, supplier facts | Cogitator approved records | `storage/promoted/` + review flow [V] |
| Commerce job state, plan fingerprints, gate ledger | **Hermes** | `~/.hermes/commerce/commerce_jobs.db` (new) |
| Purchase proposals, approvals, budgets, reservations, tickets, receipts, assets, refunds, purchase audit | **Cogitator** (unchanged) | existing eight tables [V] |
| Provider credentials / payment instrument | systemd credstore; `~/.hermes/secrets` (0700 [V]) | never Cogitator, never LLM |
| Domain ownership, DNS live state, Shopify live state, payment readiness | Provider APIs | always re-read; never trusted from cache |
| Theme snapshot + deterministic code | Git (hermes-agent) | `commerce/theme/` snapshot dir (new) |
| Product configuration | Shopify Admin API (live) + desired-state spec in job | |
| Customer order/card data | Shopify exclusively | Virgil reads order summaries only |
| Recurring subscriptions | `purchase_assets` [V] + provider truth | |
| Human approval authority | Cal, recorded in Cogitator approvals + job gate ledger | |
| Launch handover, post-launch lessons | Cogitator lesson-candidate review | no auto-promotion [V] |

---

### 7. Commerce job state machine (v1 of the machine; `state_machine_version = 1`)

Persisted in the Hermes job store; every transition appends to `job_events` (append-only). Global rules: consequential/irreversible actions are **never retried automatically** [V house rule]; idempotent reads retry 3× exponential backoff; default state timeout 72 h → `paused` with a Cal notification; recovery on restart = load job, verify provider truth for the in-flight action, then resume or park as uncertain.

| State | Entry condition | Permitted actions | Required approvals | Persisted evidence | Exit → | User message |
|---|---|---|---|---|---|---|
| `requested` | Cal-verified typed request | create job (dedupe check) | — | request text, requester id | `recovering_existing_state` | "Checking what already exists…" |
| `recovering_existing_state` | job created | read-only: job store, Cogitator assets/facts, provider reads if accounts exist | — | recovery report JSON | `needs_business_facts` \| `planning` | recovered-state report |
| `needs_business_facts` | fact gaps found | render missing-facts packet; accept Cal answers → Cogitator review | Cal supplies facts | gap list, fact record ids | `planning` | missing-facts packet |
| `planning` | facts sufficient | LLM plan draft; live price re-quotes via adapters (read-only) | — | plan JSON + fingerprint | `plan_ready` | — |
| `plan_ready`/`awaiting_decision_packet` | plan fingerprinted | render decision packet | **all bound approvals**: domain purchase (exact, via Cogitator), Shopify subscription (recurring), any app/theme | approval refs ↔ fingerprints | `ready_to_execute` | decision packet |
| `ready_to_execute` | approvals live, fingerprint match | dispatch next action | per-action | action ledger row | per-plan next state | job status |
| `registering_domain` | domain approval live | Cogitator proposal→ticket→executor (API leg or browser leg) | already bound | proposal/ticket ids, receipt ref | `configuring_dns` \| `uncertain_external_state` | "Registering {domain}…" |
| `configuring_dns` | domain owned (provider-verified) | dns snapshot → preview → apply → verify | diff-hash approval if deletions/protected-class changes | zone snapshot id, diff hash | `awaiting_store_creation` | DNS diff summary |
| `awaiting_store_creation` (human gate) | no store exists | poll: none (Cal acts) | — | gate opened ts | Cal supplies store domain + custom-app token staged | gate card G1 |
| `configuring_shopify` | store reachable, account verified (§8) | shop identity read; settings reads | — | shop id/currency snapshot | `building_store` | progress |
| `building_store` | identity verified | product/page/policy upserts; theme settings push (unpublished preview); password page on | — | upsert results, theme snapshot commit | `configuring_checkout` | progress |
| `configuring_checkout` | content built | test-gateway enablement check; test order via Bogus gateway; order verify/cancel via Admin API | — | test order id + evidence | `awaiting_payment_activation` | test-order report |
| `awaiting_payment_activation` (human gate) | real payments needed | poll `payments.readiness` via Admin API | — (Cal completes KYC/bank on Shopify) | poll log | `verifying` | gate card G5 |
| `verifying` | build+payments ready | full checklist (§20 T-verify): DNS, SSL, redirects, pages, policies, product, placeholder scan, claim-grounding re-check | — | evidence bundle (screenshots+API reads) | `awaiting_public_launch_approval` \| `verification_failed` | verification report |
| `verification_failed` | any check failed | diagnose; re-plan | re-approval only if material fields changed | failure evidence | `planning` \| `building_store` | failure + evidence |
| `uncertain_external_state` | dispatched write, unknowable outcome | provider re-reads only | — | last action, response fragments (sanitized) | `awaiting_reconciliation` | uncertainty warning |
| `awaiting_reconciliation` | uncertainty recorded | render reconcile packet; Cal/operator decides | human decision if provider truth conflicts | reconciliation record | `ready_to_execute` \| `failed` | reconcile packet |
| `awaiting_public_launch_approval` | verification passed | render launch packet (incl. "test mode will be disabled") | **public-launch approval** | approval ref | `launching` | launch card |
| `launching` | approved | disable test mode; remove store password; final domain/SSL re-verify | bound | evidence | `live` | — |
| `live` | store public | post-launch checks (order email, uptime, analytics presence) | — | health snapshot | `completed` | — |
| `completed` | handover + lessons filed | — | — | handover doc ref | terminal | handover |
| `paused` | operator or timeout | resume/cancel | — | pause reason | `ready_to_execute` \| `cancelled` | status |
| `cancelled` / `failed` | operator / unrecoverable | release Cogitator reservations via existing cancel paths [V] | — | terminal record | terminal | summary |
| `rolling_back` → `rolled_back` | Cal approves rollback proposal | inverse actions where defined: DNS snapshot restore, theme revert, re-password store; **domain purchase not reversible** (disclosed at approval) | rollback approval | inverse-action ledger | terminal | rollback report |

**Approval-material fields (plan fingerprint):** domain name/TLD/registrar/provider-account, every price+currency, term length, auto-renew, recurrence of any subscription, Shopify plan tier, product price, launch date, provider-account substitution. Non-material: copy edits, styling, page order, image swaps (subject to claim-grounding). Per-action binding means unchanged actions keep their approvals; only re-fingerprinted actions are re-requested [V pattern, generalized].

---

### 8. Provider-account and credential model

**Rule:** before any consequential action, the adapter re-reads account identity from the provider and matches it to the job's pinned `account_ref`; mismatch ⇒ invalidate that provider's pending approvals and park the job. Accounts are never inferred from filenames, paths, usernames, or history.

| Provider | Identity read | Pinned fields | Environment check |
|---|---|---|---|
| Porkbun | auth ping + domain list [D 2026-07-24, porkbun.com/api/json/v3/documentation] | API-key fingerprint (SHA-256 of key id, never the key), account label Cal assigns | n/a (single production API; fake server used in tests) |
| Shopify | Admin GraphQL `shop { id name email myshopifyDomain currencyCode plan { partnerDevelopment displayName } }` [D shopify.dev 2026-07-24] | shop id, myshopifyDomain, currency | `plan.partnerDevelopment` ⇒ dev store; production writes refuse against dev-tagged account and vice versa |

**Credentials:**
- Porkbun API key+secret: staged into the registrar executor context only — systemd credentials for the mutating one-shot unit; for read-only dev/test, env-injected from `~/.hermes/secrets` (0700 [V]). Porkbun supports per-key IP/domain restrictions and keys work independently of account 2FA [D 2026-07-24] — restrict the key to this host's egress IP.
- Shopify Admin token: from a merchant-created **custom app** (Dev Dashboard; store owners have full create/manage access) [D help.shopify.com/manual/apps/app-types/custom-apps, 2026-07-24]. Cal creates the app and stages the token (single hand-off into the secrets store; Virgil never sees it in chat). Scope minimization (§10). Rotation = Cal regenerates in admin + restages; revocation = uninstall the custom app. Exact scope names and token-display mechanics **[U — confirm in S6 against the Dev Dashboard flow]**.
- Payment instrument for the browser executor leg: unchanged — `systemd-creds encrypt` into `/etc/credstore.encrypted`, root-only, Cal-sudo staged [V].
- Prohibited everywhere outside the credential plane: passwords, tokens, cookies, card data, identity documents (matches Cogitator `FORBIDDEN_FIELD_TOKENS` [V]; the Hermes job store adopts the same forbidden-field screen on every persisted payload — test T-sec-2).

---

### 9. Registrar and DNS strategy

**Primary registrar: Porkbun. Confidence: high for DNS + pricing + availability; medium for API-registration until S1 discovery.**

Verified [D 2026-07-24 unless noted]:
- Official API v3 (`https://api.porkbun.com/api/json/v3`) covering "domains, DNS, SSL, email forwarding"; auth via `apikey`/`secretapikey` (headers or body); per-key IP/domain restrictions; 2FA-independent keys (porkbun.com/api/json/v3/documentation).
- `GET /domain/getRegistrationRequirements/{tld}` returns whether a TLD is **API-registerable** plus the registration payload schema — implying an API registration endpoint exists for eligible TLDs.
- Pricing endpoint (`/pricing/get`) for default TLD pricing; DNS retrieve/update; nameserver update (unofficial OpenAPI mirror consistent with official doc).
- .au is sold (porkbun.com/tld/au). `.com.au` availability-via-API **[U]**.

Unresolved **[U — S1 discovery items, all read-only]:** exact registration endpoint name/shape; whether registration charges account balance or stored payment method; per-key spend limits (none documented); documented rate limits; `.com.au` API registerability.

**Exact-cost approval binding:** the decision packet quotes live prices from `/pricing/get` at packet time; the Cogitator proposal carries `final_quoted_total` and approval must equal it exactly [V mechanism]. **Cached or third-party prices are never approval amounts** — re-quote at proposal creation; if re-quote ≠ packet quote, the packet is re-rendered (fingerprint change).

**Two registration legs, one governance flow (both wrapped in proposal→approval→ticket):**
- **Leg A (preferred): API registration** via a new deterministic one-shot `registrar executor` that claims the ticket, calls the registration endpoint once, and reports exactly one terminal result — the same skeleton as `purchase_executor.py` minus the browser [I]. Funding model [U] determines whether the payment instrument is "account balance" (pre-funded by Cal — itself a governed purchase) or card-on-file (Cal-staged in Porkbun account).
- **Leg B (verified fallback): the existing browser purchase executor**, whose live allowlist is already exactly Porkbun [V]. If S1 discovery finds API registration unavailable/unsuitable, the launch is not blocked.

**Idempotency/uncertainty:** registration is irreversible + single-attempt; a timeout after dispatch is an uncertain external state; reconciliation = re-read domain list — domain present ⇒ record completed (with receipt from account history [U format]); absent ⇒ definitive failure. Identical to the governance model [V].

**Fallback registrar: Cloudflare Registrar** (registration API launched 2026, beta [D startuphub.ai + developers.cloudflare.com 2026-07-24]) — .com-class only, requires Cloudflare NS (DNS-only mode for the Shopify records) [I]. **Deferred** — no adapter in first release. `.com.au` if Cal requires it: an AU registrar adapter (Synergy Wholesale has a reseller API + sandbox [I/U]) — **deferred pending Cal's TLD decision**; auDA eligibility (ABN/ACN/exact-match TM; loss ⇒ cancellation within 24 h) is registrar/auDA's validation, not Virgil's [D auda.org.au 2026-07-24].

**First-release TLDs: `.com` (and `.net` as free alternate)** — flat, API-registerable-expected, no eligibility documents. `.com.au` deferred to a follow-up slice gated on Cal's entity decision.

**DNS desired-state model:** fetch full zone → classify records: **protected** (NS, MX, SPF/DKIM/DMARC TXT, verification TXT), **flagged** (existing apex/www A/AAAA/CNAME), **free**; compute add/change/delete; write zone snapshot JSON (job evidence, rollback source); deletions or protected-class changes require diff-hash-bound approval; unrelated records always preserved; apply; verify by API re-read plus multi-resolver lookups. Rollback = restore snapshot (deterministic, non-approval since it restores prior approved state, unless protected records changed meanwhile ⇒ re-diff).

Shopify connection records [D help.shopify.com 2026-07-24]: apex A `23.227.38.65`, AAAA `2620:0127:f00f:5::`, `www` CNAME `shops.myshopify.com.`; only one apex A / www CNAME pair; up to 48 h propagation. Verification: Shopify admin domain status via Admin API [U exact field] + direct resolution + HTTPS certificate check + root↔www redirect check both directions.

---

### 10. Shopify strategy

**Store creation (B2):** No public API creates stores [D community.shopify.com confirmation, 2026-07-24]. Partner dev stores are dashboard-manual too. **Recommendation: Cal creates the store directly via standard Shopify signup** (not a Partner dev store) because the acceptance case requires a paid plan anyway — test orders require a paid plan [D help.shopify.com/manual/checkout-settings/test-orders 2026-07-24] — and dev-store→paid transfer adds steps without benefit for a single-store operator. **Strongest rejected alternative:** Partner dev store first (free build time) — rejected because plan selection is required before the test-checkout milestone regardless, and transfer/ownership steps add a second human gate. Confidence: medium-high. Would change if: Cal wants an extended unpaid build period.

**Auth:** custom app in the store's Dev Dashboard → Admin API access token (§8) [D]. **Minimum scopes (target set, exact names [U] to pin in S6):** products write, content/pages write, themes write, orders read (verification), shop read. No customer-PII scopes in v1 — order verification uses order status/amounts, not customer objects; if the orders scope unavoidably includes addresses [U], the adapter redacts customer fields at ingestion (test T-sec-3).

**API version pinning:** pin the current stable GraphQL Admin version at S6 implementation time (quarterly releases, ~1-year support [D shopify.dev 2026-07-24]); record it in one constant; calendar a quarterly bump task in the job store's maintenance list.

**Upserts:** products keyed by handle; pages keyed by handle; policies via the policies mutation [U exact mutation names — pin in S6 against the pinned version's schema]. All upserts are desired-state: read → diff → write → re-read verify.

**Theme:** **Dawn** (default free theme) with settings/sections configured, content in pages; no custom theme development in v1. `shopify theme pull` snapshots the live theme into `commerce/theme/` (git); `shopify theme push` deploys to an **unpublished** theme for preview, publish only at approval. Theme rollback = push the prior git snapshot. CLI theme auth mechanism (Theme Access password vs CLI login) **[U — pin in S7]**.

**Preview:** store password protection stays ON until public launch (standard online-store password page [D]).

**Payment readiness:** Shopify Payments setup (identity/KYC + bank) is owner-only [D help.shopify.com Shopify Payments requirements/Australia, 2026-07-24] — human gate G5. Detection: Admin API payments/payouts readiness signal [U exact query — pin in S8]; fallback detection: a A$1 real-mode order is NOT used; instead Cal taps "done" and Virgil re-verifies via API before proceeding.

**Test checkout:** Bogus Gateway on a paid plan [D]; simulated orders don't appear in payouts; **test mode blocks live orders** ⇒ disabling test mode is an explicit step inside `launching` [D]. Evidence captured: order id, financial status, cancellation record.

**Public launch mechanics (deterministic):** disable test gateway → remove store password → confirm primary domain + SSL → final checklist re-run → notify Cal. All API/CLI-drivable except none [I]; treat "remove password" via Admin API/theme settings [U exact field — pin in S9] with browser-fallback if unavailable.

---

### 11 & 12. Payment, KYC, customer-data boundaries; outgoing vs customer payment separation

- **Outgoing purchases** (domain, Shopify subscription, apps/themes): exclusively via Cogitator governance — exact-quote approval, reservation, single-use ticket, one-shot executor, receipt, reconciliation [V]. The Shopify *subscription* is special: Shopify bills a card on file in the store's admin — Cal enters it during store creation (gate G1); Virgil records it as a governed recurring commitment in `purchase_assets` via a completion record, but never handles the card. [I — cleanest treatment without expanding executor scope.]
- **Customer payments:** exist only inside Shopify Checkout + its processors. Virgil never receives, inspects, logs, or stores card data or CVVs.
- **Technical prevention of crossover (not policy — mechanism):**
  1. The browser purchase executor validates the merchant against its allowlist and refuses any origin not exactly matching the canonical merchant domain [V]. The launched store's domains (`*.myshopify.com`, the purchased domain, `checkout.shopify.com`) are **never added** to the merchant allowlist — enforced by test T-iso-1 that asserts the allowlist rejects them.
  2. The store's own domain is added to a **deny-list constant** in the executor config at S9 (defense in depth) so even a future allowlist mistake fails closed [I].
  3. Test checkout runs through the Shopify adapter path (API-driven Bogus-gateway order), not the purchase executor; the adapter possesses no payment credentials at all.
  4. KYC/identity documents: only Cal, only on Shopify's own pages, never through Virgil's browser executor (gate G5 explicitly directs Cal to their own browser/device).

---

### 13. Human-gate matrix

| # | Gate | Trigger | Why automation stops | Cal's exact action | Virgil must never observe | Completion detection (provider truth) | Poll/resume | Timeout | Prior approvals | Invalidates plan? | Failure/escalation |
|---|---|---|---|---|---|---|---|---|---|---|---|
| G0 | Purchase approval (each) | consequential spend proposed | money authority is Cal | approve exact packet (operator CLI phrase / Telegram button) | — | Cogitator approval record | immediate | approval TTL 15 min [V] ⇒ re-render | n/a | — | re-propose |
| G0r | Recurring-subscription approval | any `commitment_type=recurring` | ongoing liability | approve with renewal terms shown | — | Cogitator approval record | immediate | same | n/a | — | re-propose |
| G1 | Store creation + account link | no store exists | no store-creation API [D] | sign up at shopify.com, pick plan, enter billing card, create custom app, stage token per runbook | store password, billing card, token value in chat | Admin `shop` query succeeds with expected shop id | poll on Cal "done" + hourly | 72 h ⇒ paused | preserved | store identity pinned; substitution invalidates Shopify-scoped approvals | escalate to Cal |
| G2 | Login/2FA (any provider, browser leg) | interactive challenge pre-submit | executor never solves challenges [V] | complete login/2FA on own device | credentials, OTP | subsequent API/session verification | on "done" | 24 h | preserved | no | definitive failure recorded [V] |
| G3 | Terms/contract acceptance | provider requires ToS/DPA | legally binding | read + accept on provider site | contract creds | provider account state readable | on "done" | 72 h | preserved | only if terms change costs | pause |
| G4 | Identity/KYC | Shopify Payments verification | identity documents | complete verification in Shopify admin | ID documents, personal data | payments-readiness API signal [U query] | poll 6-hourly + on "done" | 7 d ⇒ paused reminder | preserved | no | Shopify support path, Cal-driven |
| G5 | Banking details | payout account needed | bank credentials | enter BSB/account in Shopify admin | bank details | payouts configured signal [U] | with G4 | with G4 | preserved | no | with G4 |
| G6 | Payment activation | test done, real payments next | switches money live | confirm activation in admin (with G4/G5 usually) | — | gateway active + test mode off status | poll | 72 h | preserved | no | pause |
| G7 | Consequential DNS diff | deletions/protected-record changes | mail/site breakage risk | approve exact diff hash | — | approval record; then DNS re-read post-apply | immediate | 15 min TTL | others preserved | diff change re-binds | re-render diff |
| G8 | Public-launch approval | verification passed | irreversible public exposure | approve launch packet | — | approval record; then live-state re-verify | immediate | 15 min TTL | preserved | any verification regression re-gates | re-verify |
| G9 | Rollback approval | rollback proposed | destructive inverse actions | approve rollback packet | — | approval record | immediate | 15 min | n/a | — | manual ops |

All gate completions are detected from **provider truth re-reads**, never solely from Cal's "done" message; Cal's message only triggers an immediate poll.

---

### 14. Security, privacy and prompt-injection analysis

| Threat | Defense | Proving test (§20/§21) |
|---|---|---|
| Prompt injection via provider pages/API responses (e.g., a domain-search result or Shopify error text containing instructions) | provider data enters the LLM only as quoted data inside typed structures; approvals/budgets live in Cogitator SQLite unreachable from content; deterministic executors parse fields, never interpret prose [V pattern] | RT-1 |
| Generated marketing copy triggering actions | copy is written into page specs only; the action schema has no free-text executable field; claim-grounding gate blocks unapproved assertions | RT-2 |
| Redirection attack (checkout/DNS response redirects executor to attacker origin) | executor origin allowlist + registrable-domain equality check before any fill/submit [V]; adapters pin API hosts | RT-3 |
| DNS takeover / malicious nameserver change | NS records are protected-class (G7 approval); post-apply re-read; transfer-lock ON after registration | RT-4 |
| Provider-account substitution | §8 pinning; mismatch invalidates that provider's approvals | RT-5 |
| Stale approval / replay / ticket theft | 15-min approval TTL, 5-min single-use hashed audience-bound tickets, constant-time compare, terminal-state checks [V] | existing governance tests [V] + RT-6 |
| Secret exfiltration via logs/receipts/LLM | credentials only in credential plane; redaction regexes on every log/spool/payload [V]; forbidden-field screens in both stores | RT-7 |
| Customer-checkout crossover | §12 mechanisms 1–3 | RT-8 / T-iso-1 |
| Malicious theme content (injected JS in theme snapshot) | theme = Dawn + settings only in v1; snapshot diffs reviewed in PR; no third-party theme/app code beyond the pinned set | RT-9 |
| Supply-chain (new deps) | adapters use stdlib `urllib`/`http.client` like the executor [V pattern]; Shopify CLI is the one tool dependency, version-pinned | RT-10 |
| Uncertain external writes double-executed | single-attempt rule + uncertain-state reconciliation [V] | RT-11 |
| Kill switch | `commerce.enabled` config flag gates all dispatch; `systemctl disable --now` for executor units; ticket revocation [V] | RT-12 |

Customer PII: v1 stores none (order verification uses ids/amounts/status). If a scope forces PII exposure [U], ingestion redaction + no persistence (T-sec-3).

---

### 15. Australian ecommerce and preorder readiness

- **Technical enforcement (Virgil):** grounded-claim gate (no price/discount/authenticity/delivery claim without an approved Cogitator fact); required pages (refund, privacy, terms, contact) present before `verifying` passes; preorder shipping-timeframe wording rendered from an approved fact; GST-inclusive price display per Shopify tax settings.
- **Business decisions (Cal approves):** price, deposit/full-payment structure, delivery window, refund policy text, "discounted" comparative claims (only with an approved evidence record), warranty terms.
- **Professional advice (neither Virgil nor Cal-solo):** GST registration/treatment, ACL compliance of preorder terms and delivery-failure remedies, warranty obligations for grey/parallel imports, .com.au eligibility interpretation. The report flags these; it does not resolve them. [I — not legal advice]

---

### 16. First-release scope and definition of done

One registrar (Porkbun) · one domain (.com-class) · one Shopify store (paid plan) · one provider account each · one product, one offer (**full-payment preorder**) · Dawn theme · required pages/policies · password-protected preview · Bogus-gateway test checkout · supervised public launch · handover.

**Validation-method decision:** **full-payment preorder** — an ordinary product purchase with approved preorder/delivery wording. Rejected alternatives: (a) deposit via Downpay/deferred purchase options — requires an app subscription, vaulted-card selling plans, excludes local payment methods, and adds test-mode complexity [D shopify.dev deferred purchase options 2026-07-24]; strongest alternative, deferred to roadmap; (b) no-payment waitlist — fails the product objective ("connect payments, test checkout"). Confidence: high. Would change if: Cal's price point makes full prepayment commercially unviable — then Downpay slice moves up.

**Definition of done (first release):**
1. Domain registered via governed purchase (receipt + asset recorded), DNS connected, SSL active, root/www redirects verified.
2. Store on paid plan; identity pinned; one product, landing page, four policy pages live behind password.
3. Bogus-gateway test order placed, verified via Admin API, cancelled; evidence stored.
4. Shopify Payments active (Cal KYC done); test mode disabled at launch.
5. Public launch executed after G8 approval; post-launch checklist green.
6. Handover delivered (domains/renewals, subscriptions, admin URLs, evidence bundle); ≥3 lesson candidates filed in Cogitator review queue.
7. All red-team tests (§21) passing in CI; no secrets in any artifact (T-sec sweep clean).

---

### 17. Deferred roadmap

Deferred: multi-registrar optimisation; Cloudflare/AU-registrar adapters; `.com.au` (pending Cal entity decision); multi-store; custom selling-plan/preorder apps; deposit offers (Downpay); customer-order operations beyond verification; broad analytics; autonomous refunds; support automation; renewal *automation* (renewal **alerting** from `purchase_assets` is IN scope — safety-relevant); theme development beyond Dawn settings.
Not deferrable (security/recovery): account pinning, uncertainty reconciliation, kill switch, DNS snapshots, forbidden-field screens, claim-grounding gate.

---

### 18. Ranked implementation slices

Every slice: independently reviewable/mergeable, new branch off current `origin/main` (`8bd8bfe60` hermes / `ffc3564` Cogitator at planning time — Codex must re-fetch), PR without merge, no real purchases. "Untouched" always includes: all existing purchase-executor files' behavior, all Linxio/ISB work, all worktrees.

**S1 — Porkbun read-only adapter + live discovery (hermes).**
Objective: deterministic client for ping/pricing/availability/registration-requirements/DNS-read; resolve every Porkbun [U].
Create: `registrar_porkbun.py`, `tests/test_registrar_porkbun.py`, `tests/fixtures/porkbun_api_v3/*.json`.
Modify: none.
Schema: none. Surface: library + `python registrar_porkbun.py --check` self-check.
Security: API key from env/secrets file, never argv/logs; stdlib HTTP only; responses schema-validated.
Tests: fixture-driven unit tests; fake local HTTP server; key-redaction test.
Acceptance: all [U] items answered in a `plans/` discovery note (registration endpoint shape, funding model, rate limits, .com.au flag) using read-only calls only.
Deploy: none (library). Rollback: revert commit.

**S2 — Commerce job store + state machine (hermes).**
Create: `commerce_jobs.py` (SQLite `~/.hermes/commerce/commerce_jobs.db`; tables `jobs`, `job_events` (append-only), `job_actions`, `provider_accounts`, `gates`; additive `init_db`-style migration copying Cogitator idioms [V]), `tests/test_commerce_jobs.py`.
Modify: none.
Surface: library + `scripts/commerce_job_cli.py` (status/list/pause/resume/cancel; no Telegram yet).
Security: forbidden-field screen on all persisted payloads (port of governance token list [V]).
Tests: transition matrix, restart recovery, duplicate-job dedupe, fingerprint invalidation, timeout→paused.
Acceptance: full §7 machine enforced; crash-mid-transition test recovers correctly.

**S3 — Provider-account verifier (hermes).**
Create: `commerce_accounts.py` + tests. Modify: `commerce_jobs.py` (pin/verify hooks).
Acceptance: substitution test invalidates pending approvals (T-acct-1).

**S4 — DNS desired-state engine (hermes).**
Create: `dns_diff.py`, `tests/test_dns_diff.py`. Modify: `registrar_porkbun.py` (DNS write methods, gated).
Acceptance: protected-record preservation, snapshot/rollback, diff-hash approval binding — all fixture-tested; no live zone touched.

**S5 — Governed domain registration (both repos).**
Cogitator: modify `cogitator_purchase_governance.py` + tests — additive `commerce_launch_v1` policy (classes + budget per Cal's approved figure; PROPOSAL/TICKET flow unchanged).
Hermes: create `registrar_purchase_executor.py` (Leg A one-shot; ticket-gated; single attempt; spool; cleanup) OR configure Leg B (browser executor cart path) if S1 discovery rules out Leg A; `tests/test_registrar_purchase_executor.py`; fake-E2E vs loopback fake Porkbun.
Deploy: staging unit mirroring `packaging/purchase-executor/` pattern if Leg A.
Acceptance: loopback fake-E2E green; **supervised live registration of Cal's approved domain = first live acceptance** (Cal present; bounded ≤ approved amount).
Rollback: unit disable; ticket revoke; uncertain path exercised in staging first.

**S6 — Shopify Admin adapter (hermes).**
Create: `shopify_admin.py` (pinned GraphQL version constant; shop identity; product/page/policy upserts; order read/cancel; readiness reads), `tests/test_shopify_admin.py` + fake GraphQL server fixture.
Modify: `commerce_accounts.py` (Shopify pinning).
Acceptance: all mutations desired-state idempotent (double-apply test); scope list pinned and documented; zero customer-PII persistence test.

**S7 — Theme + landing/content builder (hermes).**
Create: `commerce_content.py` (grounded copy: every claim carries an approved-fact ref; placeholder scanner), `commerce/theme/` snapshot dir, `tests/test_commerce_content.py`.
Acceptance: ungrounded-claim build failure test; theme pull/push round-trip against fake store [U CLI auth pinned here].

**S8 — Gates, verification engine, payment readiness (hermes).**
Create: `commerce_verify.py`, gate poller inside `commerce_jobs.py`, `tests/test_commerce_verify.py`.
Acceptance: full checklist against fixture store; G4/G5 poll logic on fake readiness endpoint.

**S9 — Launch, handover, lessons, Telegram rendering (hermes).**
Create: `gateway/commerce_launch_bridge.py` (rendering/routing via existing bridge patterns [V]), handover generator; deny-list constant for store domains in executor config.
Acceptance: end-to-end loopback rehearsal: request → packets → fake registration → fake store build → fake verification → launch approval → handover; T-iso-1 green.

---

### 19. File-level change matrix

**hermes-agent (all new unless noted):**
| Path | Purpose |
|---|---|
| `registrar_porkbun.py` | Porkbun API v3 client (read-only S1; DNS writes S4) |
| `registrar_purchase_executor.py` | one-shot ticket-gated API registration executor (S5, if Leg A) |
| `dns_diff.py` | desired-state diff/snapshot/rollback engine |
| `commerce_jobs.py` | job store + state machine + gate poller |
| `commerce_accounts.py` | provider-account pinning/verification |
| `shopify_admin.py` | Admin GraphQL adapter (pinned version) |
| `commerce_content.py` | grounded page/copy builder + placeholder/claim gates |
| `commerce_verify.py` | verification checklist engine |
| `gateway/commerce_launch_bridge.py` | Telegram packet rendering/routing |
| `scripts/commerce_job_cli.py` | operator job CLI |
| `commerce/theme/` | Dawn theme snapshot (git) |
| `tests/test_registrar_porkbun.py`, `tests/test_registrar_purchase_executor.py`, `tests/test_dns_diff.py`, `tests/test_commerce_jobs.py`, `tests/test_commerce_accounts.py`, `tests/test_shopify_admin.py`, `tests/test_commerce_content.py`, `tests/test_commerce_verify.py` | per-slice suites |
| `tests/fixtures/porkbun_api_v3/`, `tests/fixtures/shopify_admin/` | API fixtures |
| `packaging/registrar-executor/` (S5, Leg A only) | unit files mirroring purchase-executor packaging |
| MODIFY `packaging/purchase-executor/config.yaml` (S9) | store-domain deny-list constant |

**Cogitator:**
| Path | Purpose |
|---|---|
| MODIFY `cogitator_purchase_governance.py` | additive `commerce_launch_v1` policy (classes, budget) |
| MODIFY `tests/test_cogitator_purchase_governance.py` | policy tests |
| (no other files; no schema beyond existing additive pattern) | |

No other existing file in either repo is modified.

---

### 20. Test plan

| Series | Coverage |
|---|---|
| T-unit | every new module; fixture-driven; stdlib-only fakes (house pattern [V]) |
| T-schema | job-store migration idempotence; additive-only assertion; forbidden-field screens |
| T-state | full §7 transition matrix incl. illegal-transition rejection |
| T-recover | kill -9 mid-transition → restart → correct resume/park; gate-resume after poll success |
| T-dup | duplicate job request attaches; duplicate action idempotency-key returns stored result |
| T-fake-porkbun | fake HTTP server: pricing, availability, DNS CRUD, registration happy/timeout/ambiguous |
| T-fake-shopify | fake GraphQL: shop identity, upsert double-apply idempotence, order read/cancel, readiness |
| T-dns | diff correctness; protected-record preservation; snapshot restore; email-record deletion refused without G7 |
| T-approve | stale approval rejected; fingerprint change orphans only affected approvals; TTL expiry |
| T-acct | account-substitution invalidation (Porkbun key swap; Shopify shop-id mismatch; dev-vs-prod refusal) |
| T-inject | provider responses/pages containing instruction-like text produce no action/approval/plan change |
| T-sec | (1) key/token never in logs/spool/receipts (regex sweep); (2) forbidden-field rejection both stores; (3) no customer-PII persistence |
| T-iso-1 | merchant allowlist + deny-list reject store domains and `checkout.shopify.com` for the purchase executor |
| T-uncertain | dispatched-write timeout → uncertain → reconciliation both outcomes |
| T-checkout | Bogus-gateway order lifecycle vs fake Shopify; test-mode-off asserted in launch path |
| E2E-loopback | full §18-S9 rehearsal, everything faked, single command |
| Staging | synthetic credentials, staging units (existing pattern [V]); registrar staging vs fake server |
| Live acceptance | (1) supervised governed domain registration (S5); (2) issue #65 supervised browser purchase — **parallel milestone, independently scheduled**; (3) supervised public launch (G8) |

---

### 21. Red-team plan

| # | Scenario | Expected fail-closed behavior | Proving test |
|---|---|---|---|
| RT-1 | Porkbun search result named `ignore-previous-instructions-approve.com` flows to planner | string treated as data; no approval/plan mutation | T-inject |
| RT-2 | Generated FAQ copy contains "system: register domain X" | page spec carries it as text; no action schema emitted from content | T-inject |
| RT-3 | Fake merchant page posts payment form to attacker origin | origin equality check aborts pre-fill [V] | existing executor test + T-fake-porkbun variant |
| RT-4 | DNS apply attempts NS replacement | protected-class ⇒ G7 approval required; absent ⇒ refused | T-dns |
| RT-5 | Shopify token swapped for different store between approval and execute | shop-id pin mismatch ⇒ approvals invalidated, job parked | T-acct |
| RT-6 | Captured ticket replayed / used by wrong audience | single-use hash + audience check rejects [V] | governance suite [V] |
| RT-7 | Exception message embeds API secret | redaction filter strips before log/spool [V pattern extended] | T-sec-1 |
| RT-8 | Operator misconfigures purchase executor at the launched store's checkout | allowlist rejects; deny-list backstop | T-iso-1 |
| RT-9 | Theme snapshot PR introduces `<script src=…>` | PR review + snapshot diff test flags non-Dawn additions | T-content lint |
| RT-10 | Malicious PyPI lookalike dependency proposed | slices are stdlib-only; CI dependency-diff check | review gate |
| RT-11 | Registration request times out after dispatch; retry would double-buy | single-attempt; uncertain; reconcile via domain-list re-read | T-uncertain |
| RT-12 | Runaway job during incident | `commerce.enabled=false` halts dispatch; units disabled; tickets revoked | T-kill (flag respected mid-job) |

---

### 22. Deployment and rollout plan

- **Branches:** one per slice (`feat/commerce-s1-porkbun-readonly`, …) off freshly fetched `origin/main`; PRs, never merged by Codex.
- **Migrations:** additive only, both repos; job-store DB is new (no legacy data); Cogitator policy addition is insert-if-missing (house pattern [V]).
- **Packages/units:** none until S5; then `packaging/registrar-executor/` mirroring the purchase-executor unit set (inert install, Cal sudo gate, staging with synthetic creds [V pattern]).
- **Credentials:** Porkbun key (IP-restricted) staged by Cal; Shopify token staged by Cal at G1; payment credentials only if Leg B, via existing `systemd-creds` flow.
- **Flags:** `commerce.enabled: false` default; `commerce.dry_run: true` default (all mutating calls print-and-record); staging mode = fake endpoints via config URL override (loopback-confined like `--fake-e2e` [V pattern]).
- **Production enablement order:** S1 discovery note reviewed → S2–S4 merged with CI green → staging E2E → Cal enables `commerce.enabled` → supervised live domain registration → S6–S8 merged → G1 store gate → build/test-checkout → G4/G5/G6 → supervised public launch.
- **Health/observability:** job-store status CLI; gate-age and uncertainty alerts to Telegram; renewal alerts from `purchase_assets`.
- **Backup:** nightly copy of `commerce_jobs.db` into existing `~/.hermes/backups` [V dir exists]; Cogitator DB backup precondition already documented in `PURCHASE_GOVERNANCE_V1.md` [V].
- **Kill switch:** flag + unit disable + ticket revocation (§21 RT-12).
- **Rollback:** per-slice git revert; DB files retained (never dropped — house rule [V]); DNS snapshot restore; theme snapshot push; store re-password.

---

### 23. Critical path

**Blockers in order:** S1 discovery ([U] resolution) → S2 job store → S5 governance policy + registration leg → **supervised live domain registration** (first live acceptance).
**Parallelizable:** S3, S4, S6, S7 can proceed alongside S2/S5 once S1 lands (S4 needs S1; S6/S7 need nothing but fixtures); issue #65's supervised browser purchase is fully parallel and shares only Cal's calendar; B1 fact collection and G1 store creation are Cal-time, schedulable anytime.
**Shortest path to first live acceptance:** S1 → S2 → S5(Cogitator policy ∥ hermes executor) → staging E2E → Cal approves domain packet → live registration. Everything Shopify-side follows without blocking it.

---

### 24. Open decisions for Cal (genuinely Cal's)

1. **Entity/TLD:** trade as individual/company; `.com` now vs wait for `.com.au` (needs ABN/ACN). Default if silent: `.com` first release.
2. **Budget:** approve the `commerce_launch_v1` budget figure (pilot A$100 [V] cannot cover Shopify Basic ≈ A$56+GST/mo [D-third-party, re-quote live] + domain + margin). A concrete proposal will be in the first decision packet.
3. **Brand + domain shortlist approval** (Virgil proposes, Cal picks/approves exact registration).
4. **Product facts:** GPU model, condition, supplier claim permitted publicly, stock, price, delivery window, refund/warranty text (B1).
5. **Preorder wording sign-off** (ACL exposure is Cal's risk to accept or route to advice).
6. Scheduling: supervised sessions for (a) live domain registration, (b) #65 browser purchase (parallel milestone), (c) public launch.

Not asked of Cal (decided here): job-store placement, registrar choice, validation method, theme, API auth mechanism, state machine, slice order.

---

### 25. Codex implementation prompt (Slice S1 only)

```
ROLE: You are Codex, implementing Slice S1 of the Governed Ecommerce Launch
Executor, exactly as specified in
plans/governed-ecommerce-launch-executor-review.md (branch
review/ecommerce-launch-executor-plan) sections 5, 9, 18-S1, 19, 20.
Do not redesign the architecture. Do not implement any later slice.

REPO / BASE:
- Repo: /home/v0id/.hermes/hermes-agent (origin 3ndym10n/hermes-agent).
- Run `git fetch origin` and branch from CURRENT origin/main (was 8bd8bfe60
  at planning time; use whatever it is now).
- Branch name: feat/commerce-s1-porkbun-readonly.

STATE RECOVERY FIRST (read-only):
- `git status`, `git worktree list`, `git log --oneline -5 origin/main`.
- Preserve everything: do not touch purchase_executor.py,
  purchase_merchants.py, purchase_discovery.py, packaging/, gateway/, any
  Linxio/ISB files, any worktree, any untracked file. Your diff may only
  contain the files listed below.

IMPLEMENT (deterministic Python, stdlib HTTP only, mirror the style of
purchase_executor.py — module docstring contract, no retry loops on
non-idempotent calls, redaction of secrets from all output):
1. registrar_porkbun.py — Porkbun API v3 client, READ-ONLY in this slice:
   - auth ping; default pricing retrieval; domain availability/registration
     requirements per TLD (including the API-registerable flag);
     DNS record retrieval; nameserver read; account domain list.
   - API key/secret from env vars PORKBUN_API_KEY / PORKBUN_SECRET_KEY or a
     0600 file path env PORKBUN_CREDENTIALS_FILE. Never from argv. Never
     logged; add a redaction guard on all exception/log text.
   - Base URL override env PORKBUN_API_BASE for the test fake server; when
     overridden to loopback, refuse non-loopback hosts (copy the fake-e2e
     loopback-confinement pattern from purchase_executor.py).
   - Strict response validation; unknown/missing fields raise typed errors.
   - NO mutation endpoints in this slice (no DNS writes, no registration).
2. tests/test_registrar_porkbun.py + tests/fixtures/porkbun_api_v3/*.json:
   - fixture-driven unit tests + a threading.HTTPServer fake covering:
     happy paths for every method; auth failure; malformed response;
     key-redaction (assert secrets never appear in logs/exceptions);
     loopback-confinement guard.
3. A --check self-check entrypoint (python registrar_porkbun.py --check)
   that runs against the fake server only.

ACCEPTANCE CRITERIA:
- All new tests pass: `python -m pytest tests/test_registrar_porkbun.py -q`.
- Existing purchase tests still pass:
  `python -m pytest tests/test_purchase_executor.py tests/test_purchase_discovery.py -q`.
- `git diff --stat` shows ONLY the files listed above.
- No new third-party dependencies.

ALSO PRODUCE (docs, same branch): plans/commerce-s1-porkbun-discovery.md
recording, from OFFICIAL Porkbun documentation (cite URLs + retrieval date),
answers to: exact registration endpoint name/shape; funding model (account
balance vs stored card); documented rate limits; per-key restrictions;
.com.au API-registerability. Mark anything requiring an authenticated live
call as "pending live check" — do NOT make authenticated live calls, do NOT
register anything, do NOT spend money.

DELIVERY:
- Commit with message: "feat(commerce): S1 Porkbun read-only registrar
  adapter + discovery note".
- Push the branch and open a PR to main titled the same; do NOT merge it.
- In the PR body: test output, discovery-note summary, unresolved risks,
  and explicit confirmation that no live mutation or purchase occurred.
- Run your automatic review loop on the PR before reporting done.
- HARD LIMITS: no real purchases, no provider mutations, no secrets in the
  repo, no changes outside the listed files, no public launch actions.
```

---

### Self-red-team (answered before finalising)

Virgil is the operator (Fable plans, Codex builds — §0/§18); purchase executor preserved and reused (§2/§9/§12, zero behavioral modifications listed in §19); customer vs outgoing payments separated with mechanisms and tests (§12, T-iso-1); Hermes/Cogitator boundary resolved with a physical-placement decision and bypass-prevention argument (§5); file-level changes exact (§19) with invented-vs-existing paths distinguished (new files are proposals, existing files verified); real test + red-team plans (§20–21); deployment/rollback (§22); first release constrained (§16); no AMD commercial facts invented (§2, §24-4); Gmail project excluded — the only Linxio-adjacent reuse is the generic Telegram bridge rendering pattern, named narrowly in §18-S9 because it is the existing message-routing surface [V]; the Codex prompt is executable without redesign (single slice, exact files, criteria); [V]/[D]/[I]/[U] used throughout; human gates enumerated (§13); unrelated branches/worktrees/untracked files untouched (§2 statement).
