# Virgil Linxio Sales Assistant — 30-Day Product Proof

Canonical source of truth for the current proving focus. One document, one issue.
This is a **temporary 30-day proving focus**, not a permanent decision that Linxio
is the only future use of Virgil or Cogitator, and not a cancellation of anything
else. Nothing listed as paused or preserved is closed or deleted.

Every claim here was verified against the machine, the repositories and the live
runtime on 2026-07-29. A merged PR is not proof of deployment; a running service
is not proof of useful product behaviour.

## Product objective

New external Linxio email arrives → Virgil detects it promptly → reads only that
thread → decides whether a reply is needed → retrieves only trustworthy, relevant
Linxio facts → retrieves Cal's approved writing guidance → keeps customer facts,
business facts and style guidance separate → prepares a Gmail reply draft →
**never sends** → creates one organised Virgil Mobile item → alerts Cal only when
useful → Cal reviews and sends → outcome and corrections are measured → reusable
corrections may become reviewed Cogitator lessons.

Automatic sending is out of scope for this proof.

## Identities

| Name | What it is |
|---|---|
| **Virgil** | Cal's assistant, operator and product identity. Cal should experience one assistant, not a set of subsystems. |
| **Hermes** | The agent runtime: models, tools, connected services, workers, execution state, Telegram gateway, Virgil Mobile backend. Infrastructure, not the product identity. |
| **Cogitator** | The durable evidence, memory, policy and retrieval layer: approved facts, reviewed lessons, provenance, promoted guidance. Not the live task dashboard, and never a replacement for source systems. |
| **Virgil Mobile** | Cal's primary operational surface — what needs Cal, what Virgil prepared, the next action, upcoming sales activity, important failures. Not a sysadmin dashboard. |
| **Telegram** | Voice and quick capture, concise urgent alerts, simple approvals, links into Virgil Mobile. Not the only interface and not the permanent history. |
| **Obsidian** | Optional deep-review surface over durable Markdown. Not the assumed daily interface. |

## Active workflow

```
Gmail history poll (60s timer, shadow mode)
  → deterministic exclusions (internal, automated, bulk, calendar, not-inbox…)
  → thread read (only the triggering thread)
  → bounded classifier: reply needed? category? confidence?
  → approved business facts   ← deterministic Linxio eligibility gate
  → writing guidance          ← promoted Linxio rules only
  → evidence separation check
  → draft generated in memory, grounding validated
  → SHADOW: nothing written to Gmail
  → Attention item + alert only when useful
```

## Current architecture

- **Worker**: `skills/productivity/google-workspace/scripts/incoming_autodraft.py`,
  one-shot under `linxio-incoming-autodraft.timer` (60s), hardened unit
  (`ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges`).
- **State**: private SQLite at `secrets/google/incoming-autodraft/state.db`.
  Backups in `backups/incoming-autodraft/`.
- **Attention Queue**: `attention/attention.db`, served by `virgil-mobile.service`
  on `127.0.0.1:8788`, exposed tailnet-only via Tailscale Serve on `:8443`.
- **Source sync**: `virgil-operational-sources.timer` (60s) refreshes gmail,
  calendar, cogitator, github, ecommerce, system source status.
- **Cogitator bridge**: Railway, `worker-production-42f3.up.railway.app`,
  bearer-token, action-contract validated.

## Current state by capability

Classified as: production-proven / deployed-not-proven / merged-not-deployed /
local-only / broken / paused / missing / unknown.

### Virgil / Hermes

| Capability | State |
|---|---|
| Gmail worker (shadow) | **Production-proven** — running, watermark advancing, 0 drafts, 0 sends |
| Attention Queue | **Production-proven** — 108 items, 7 sources |
| Virgil Mobile PWA | **Deployed, not fully proven** — service healthy; product usefulness unmeasured |
| Telegram gateway | **Deployed, not fully proven** — fallback path only for Gmail alerts |
| Calendar integration | **Deployed, not fully proven** — read-only, 2 events in window |
| Source synchronisation | **Production-proven** — all sources reporting |
| GitHub integration | **Production-proven** — 4 open PRs tracked |
| System monitoring | **Production-proven** — health findings resolve correctly |
| Approved-fact retrieval | **Deployed** — gate live and failing closed (no facts exist to retrieve) |
| Writing-guidance retrieval | **Production-proven** — 5 global rules across all safe categories |
| Model/provider routing | **Unknown** — not assessed in this sprint |

### Cogitator

| Capability | State |
|---|---|
| Bridge | **Production-proven** |
| Writing guidance | **Production-proven** — 5 global + category-specific, deduplicated |
| Approved Linxio business facts | **Missing** — zero records exist (Gate A) |
| Promotion workflow | **Production-proven** — incl. new `promote_global` re-promotion action |
| Retrieval (promoted store) | **Production-proven** |
| Research / Decision Inbox / operating memory | **Paused** for this proof |
| Outcome and lesson loop | **Deployed, not fully proven** |

### Development operations

| Item | State |
|---|---|
| Worktrees | 14 Hermes, 24 Cogitator — all preserved |
| Open PRs | Hermes #88 (draft, commerce); Cogitator #793, #794 |
| CI | Green on both repos; one flaky live-model-catalogue test in Hermes |
| Deployment | Hermes: `git pull` into the stable checkout the timer executes. Cogitator: Railway on merge to main |
| Routine relay dependence on Cal | Reduced — Opus now drives issue → PR → CI → merge → deploy → verify without relaying |

## Current trust blocker

**Resolved for retrieval. Blocked on source material.**

Production shadow testing found the approved-fact path returning approved records
that were not relevant Linxio business facts — a sponsored X article on AI-agent
teams and a Southeast University Knowledge Graph course README were offered as
Linxio business facts for product, pricing, scheduling and acknowledgement
questions alike. The drafting model refused to cite them. That refusal was acting
as the *relevance boundary* rather than the last safety net.

Root cause: `_approved_facts` checked only approved/promoted lifecycle. The email
category was free text in the retrieval prompt with no filtering effect.

Fixed in #115: a record enters the bucket only when every deterministic check
passes — lifecycle, `linxio_business_fact` record type, explicit Linxio scope,
compatible fact category, non-empty provenance, not superseded. Missing metadata
fails rather than passes. Checks run before any semantic score.

The writing-guidance gap had a separate root cause: every rule was promoted once
per category carrying both `linxio-email-writing` and `email-message-category:`,
while the global lookup requires the category trigger to be *absent* — so
`global_guidance_count` was zero for every kind and six categories retrieved
nothing. Resolved by Cal-approved global promotion (Gate B).

**The remaining blocker is Gate A: Cogitator holds no approved Linxio business
facts at all.** Every commercial question correctly returns
`missing_approved_fact`. This is a source-access problem, not a retrieval problem.

## Evidence buckets

Three closed, independently validated buckets. No record silently crosses.

1. **Customer facts** — only the active Gmail thread. Never auto-promoted.
2. **Approved business facts** — only eligible, relevant, approved Linxio records,
   carrying record reference, fact category, provenance, approval evidence, scope
   and supersession status.
3. **Writing guidance** — only promoted Linxio writing records, carrying record
   reference, global/category scope, provenance and applied category.

Any numeric, pricing, commercial, contractual, warranty, delivery, payment or
installation statement in a draft must trace to the current customer thread or to
one eligible approved Linxio business fact.

Writing guidance may affect tone, structure, length, clarity and call to action
only. It may never supply or override product capabilities, prices, GST, payment
terms, contract terms, warranty, installation, delivery, stock, cancellation or
refunds.

## Rollout stages

| Stage | State |
|---|---|
| 0. Worker runs without global halts | **Passed** — #113 |
| 1. Retrieval trust: no irrelevant record reaches drafting | **Passed** — #115 |
| 2. Writing guidance resolves for every safe category | **Passed** — Gate B |
| 3. Bounded production shadow verification | **Passed** |
| 4. Gate A — approved Linxio business fact set | **Blocked** — no authoritative source |
| 5. Draft-only standing policy approval | Not started |
| 6. Supervised 20-email draft-only pilot | Not started |

## Allowed autonomy and human gates

Virgil may, without asking: poll Gmail read-only, read a triggering thread,
classify, retrieve evidence, generate a draft in memory, create Attention items,
send alerts, and record sanitized aggregates.

Virgil may **never**, in this proof: send email, create a Gmail draft without an
approved standing policy, mark read, archive, label, move or delete, promote a
fact or widen a rule's scope, or widen an OAuth grant.

**Human gates:**

- **Gate A** — approve a minimal Linxio business-fact set. Blocked on source material.
- **Gate B** — approve global writing-rule scope. **Approved 2026-07-29.**
- **Gate C** — approve the exact draft-only standing policy before any Gmail draft.
- **Gate D** — approve any move beyond draft-only. Automatic sending stays out of scope.

## Success metrics and definitions

Measured over the first 20 eligible draft-only cases, sanitized aggregates only,
reusing existing worker and Attention state. No new database for metrics.

| Metric | Definition |
|---|---|
| Incoming messages examined | Inbox messages the worker evaluated |
| External-human candidates | Not internal, automated, bulk, calendar or receipt |
| Eligible | Safe category, sufficient confidence, evidence available |
| Drafts created | Gmail drafts written (0 until Gate C) |
| Decision required | Stopped for Cal, by reason code |
| Ignored by reason | Deterministic exclusions, by reason |
| Duplicates suppressed | Repeat events for a message already handled |
| Missing facts | `missing_approved_fact` |
| Conflicts | `conflicting_facts` |
| Unsupported claims prevented | Draft rejected by grounding validation |
| Detection-to-draft time | Message arrival → prepared reply |
| Sent unchanged / minor edit / major edit / discarded | Cal's one-tap outcome |
| Factual corrections | Cal corrected a fact in the draft |
| Worker failures | Terminal processing failures |
| Missed important emails | Cal flags an email Virgil should have caught |
| Estimated time saved | Cal's estimate per handled email |

**Thresholds:** at least 80% usable with no or minor edits; **zero** unsupported
commercial claims; zero cross-customer leaks; zero unintended Gmail mutations.

## Next product gate

> The trusted shadow workflow has passed. Retrieval trust, evidence separation and
> Gmail safety are proven in production. The remaining blocker is that Cogitator
> holds no approved Linxio business facts, so commercial drafting stays blocked.
> Supply the authoritative source material, approve the minimal fact set, then
> approve the exact draft-only standing policy to begin the supervised 20-email
> pilot.

Draft creation is not enabled without that explicit approval.

---

# Freeze registry

Temporary, for the 30-day proof. **Nothing here is closed, deleted or cancelled.**

## Active

- Virgil Linxio Sales Assistant proof (this document)
- Gmail incoming shadow worker
- Approved-fact eligibility and writing-guidance retrieval trust
- LINXIO FACT bounded document-ingestion channel

## Supporting

- Virgil Mobile PWA and Attention Queue
- Cogitator bridge, promoted store and promotion workflow
- Operational source synchronisation
- Telegram gateway (alerts, approvals, quick capture)
- Google Calendar read-only integration

## Paused — preserved, not closed

New generic Cogitator domains · new knowledge record types · generic autonomous
research expansion · trading agents · personal-life integrations · new dashboards
· native Android development · Discord migration · new model-routing frameworks ·
new vector databases · new operational databases · automatic email sending ·
speculative HubSpot or softphone adapters without real credentials · new ecommerce
executor slices beyond already-active work · new broad self-improvement or
dream-cycle schedulers.

## Preserved

- Active and unfinished ecommerce work, commerce job state (PR #88, `feat/commerce-s2-job-store`)
- Purchase-executor work (issue #65 and its branches)
- All Forge work
- Historical Sent-style analysis state
- Gmail checkpoint, baseline and worker state
- Attention Queue data
- Promoted Cogitator records
- Google credentials and tokens; Railway variables; current backups
- Untracked project plans (`plans/governed-ecommerce-launch-executor-review.md`)
- All 14 Hermes and 24 Cogitator worktrees

## Awaiting separate review

- **Forge** — separate Lovable-style AI app-builder project; preserved; awaiting
  grounded Forge-specific review. **Its repository is not grounded**: no `forge`
  repository exists under `3ndym10n`, and only local working directories were
  found (`Projects/forge-delta-smoke`, `Projects/forge-spikes`, plus Android build
  artifacts under `apk-share/`). Forge is **not** a commerce executor, purchase
  engine, deterministic ticket runner, or a subsystem of Virgil or Cogitator, and
  its architecture must not be inferred from the Hermes purchase executor or
  ecommerce work. Not part of this proof.
- Ecommerce / purchase executor — active work preserved, no new slices
- Intelligent Second Brain V1 (Cogitator #1019)
- Cogitator PRs #793, #794

## Deprecation candidate

None proposed. No project is recommended for deprecation from this sprint.

---

## Change log

| Date | Change |
|---|---|
| 2026-07-29 | Worker global-halt fix (#112 / #113, `e85c9c47a`) |
| 2026-07-29 | Approved-fact eligibility gate + `missing_writing_guidance` (#114 / #115, `40ab1faae`) |
| 2026-07-29 | Guidance scope reporting (Cogitator #1073, `5aec10e3a`) |
| 2026-07-29 | `promote_global` re-promotion action (Cogitator #1074, `f952950d7`) |
| 2026-07-29 | Guidance dedup + email-review HTTP status (Cogitator #1075, `eb54d4c50`) |
| 2026-07-29 | Gate B approved; 5 global writing rules promoted |
