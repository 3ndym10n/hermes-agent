# Context Rotation — Harness-Side V0 Plan

**Status:** Planning only (docs). No runtime code, tests, config, or secrets are
touched by this document.
**Owner area:** Hermes/Virgil runtime (the live agent loop), not Cogitator.
**Related:** Cogitator PR #831 (merged). The Cogitator "Context Window
Management" plan places the *live* detect → rotate → inject → continue loop in
the Hermes/Virgil harness. Cogitator V0 will later supply the **checkpoint
builder**, the **bridge**, and **hard usage-limit classification**. This
document designs only the harness seam that those Cogitator pieces plug into,
plus a harness-local fallback so V0 can ship before Cogitator's builder lands.

---

## 1. Problem statement

A long-lived conversation (gateway DM, cron job, Virgil repo task) can grow past
the point where in-place compression keeps it healthy. Today the harness has two
behaviors at the edges:

1. **In-place lossy compression** (`ContextCompressor`) — summarizes mid-history
   when prompt tokens cross ~75% of the context window. Good for steady growth.
2. **Hard auto-reset** (`gateway/run.py`) — when compression is *exhausted*
   (`compression_exhausted`), the session is reset and **all context is
   dropped**, with a "🔄 Session auto-reset" notice.

The gap between them is **Context Rotation**: instead of dropping everything when
a conversation becomes oversized, build a *compact checkpoint* of what matters,
start a *fresh internal conversation/thread*, *inject* the checkpoint, and
*continue* — and, separately, **stop retrying immediately** when the provider
says `usage_limit_reached` (a scheduled plan cap, not a transient blip).

Context Rotation V0 is the controlled middle path between "compress in place" and
"throw it all away."

---

## 2. Goal (the six steps this plan designs)

The harness runtime should be able to:

1. **Detect** an oversized conversation.
2. **Request or build** a compact checkpoint (Cogitator builder when available;
   harness-local fallback otherwise).
3. **Start** a fresh internal conversation/thread.
4. **Inject** the checkpoint into that fresh thread.
5. **Continue** safely from the injected state.
6. **Stop retrying immediately** on `usage_limit_reached`.

All of this ships **behind a feature flag, default off** (Section 9).

---

## 3. Where the relevant machinery already lives

These are the exact files/functions a V0 implementation would touch or extend.
Line numbers are approximate anchors at time of writing — verify before editing.

### 3.1 Context engine (detection + compaction abstraction)

- **`agent/context_engine.py`** — `ContextEngine` ABC. The seam for any context
  strategy. Key members:
  - `should_compress(prompt_tokens=None) -> bool`
  - `should_compress_preflight(messages) -> bool`
  - `compress(messages, current_tokens=None, focus_topic=None) -> list`
  - `update_from_response(usage)` — fed real token usage after every API call.
  - `threshold_tokens`, `threshold_percent` (default `0.75`), `context_length`,
    `last_prompt_tokens`, `compression_count`.
  - `on_session_start`, `on_session_end`, `on_session_reset`.
  - `get_tool_schemas` / `handle_tool_call` — engines may expose their own tools.
  - This is the natural home for a new **`should_rotate(...)`** /
    **`build_checkpoint(...)`** capability (see Section 6), kept *optional* with a
    default implementation so third-party engines don't break.

- **`agent/context_compressor.py`** — `ContextCompressor(ContextEngine)`, the
  default engine.
  - `should_compress` (~line 815): fires when `tokens >= self.threshold_tokens`;
    has an "ineffective compression" circuit breaker (`_ineffective_compression_count >= 2`).
  - `update_from_response` (~line 771): records `last_prompt_tokens` /
    `last_real_prompt_tokens`.
  - `threshold_tokens = max(int(context_length * threshold_percent), …)`
    (~line 712).
  - Summary construction (`_serialize_for_summary`, `_build_static_fallback_summary`,
    `_compute_summary_budget`) is the **reusable basis for the harness-local
    fallback checkpoint** in V0.
  - `COMPRESSED_SUMMARY_METADATA_KEY = "_compressed_summary"` — existing
    convention for marking a synthesized summary message so it never reaches the
    wire as a special role. A rotation checkpoint message should reuse the same
    metadata-tagging discipline.

### 3.2 The live agent loop (where detect/continue happen)

- **`agent/conversation_loop.py`** — `run_conversation(...)` (~line 469). This is
  the per-turn retry loop (`while retry_count < max_retries`, ~line 920) where:
  - `classify_api_error(...)` is called (~line 2208) and the result
    (`classified.retryable`, `classified.reason`, `classified.should_compress`)
    drives recovery.
  - Compression is triggered mid-loop and signals exhaustion via
    `restart_with_compressed_messages` and `compression_exhausted` result keys
    (~lines 2738, 2912, 2946, 2996; `max_compression_attempts = 3` at ~line 912).
  - Rate-limit / billing eager-fallback logic (~lines 2760–2854).
  - This loop is where **"continue safely"** (step 5) and **"stop retrying on
    usage_limit_reached"** (step 6) must be enforced.

- **`agent/turn_retry_state.py`** (`TurnRetryState`) and **`agent/retry_utils.py`**
  (`jittered_backoff`) — retry bookkeeping and backoff. A `usage_limit_reached`
  outcome must short-circuit *both* (no further attempts, no backoff sleep).

- **`run_agent.py`**:
  - `_transition_context_engine_session(...)` (~line 535) — drives the engine
    lifecycle (`on_session_end` / `on_session_reset` / `on_session_start`) when a
    session rotates. The rotation flow must call through this so engine state
    (compression counters, token tracking) resets cleanly on the fresh thread.
  - Session-rotation hook (~line 3004): "Called when session_id rotates (e.g.
    /new, context compression)." This is the existing internal-rotation entry
    point Context Rotation should ride on rather than inventing a new one.
  - Preflight compression check (calls `should_compress` / `should_compress_preflight`).

### 3.3 Error classification (usage-limit detection)

- **`agent/error_classifier.py`**:
  - `classify_api_error(...)` (~line 441) → `ClassifiedError` with
    `retryable: bool` (~line 82).
  - `_classify_402(...)` (~line 902) — the key disambiguation: a 402/usage
    message with a *transient* signal ("try again", "resets at", "window") is
    `rate_limit` (retryable); a confirmed cap is `billing` (**`retryable=False`**).
  - `_USAGE_LIMIT_PATTERNS` (~line 137) and `_USAGE_LIMIT_TRANSIENT_SIGNALS`
    (~line 145) — the pattern tables that already encode "is this a hard cap?"
  - **Cogitator will later own "hard usage-limit classification."** Until then,
    `_classify_402` + the `usage_limit_reached` checks below are the harness's
    authority.

- **`agent/agent_runtime_helpers.py`** (~lines 695–717) — already detects
  `usage_limit_reached` from `error_context` (`"usage_limit_reached"`,
  `"gousagelimit"`, `"usage limit reached"`, `"usage limit has been reached"`) to
  decide credential-pool rotation. This is the canonical harness-side
  `usage_limit_reached` detector and should be the single source of truth that
  Section 8 wires into the retry stop.

- **`gateway/run.py`** (~line 9570) — surfaces the `usage_limit_reached`
  user-facing message (`type == "usage_limit_reached"`, with `resets_in_seconds`
  → "resets in ~Nh"). The rotation path must not retry *past* this; it should
  surface this same message and stop.

### 3.4 Session rotation primitives (start fresh + inject)

- **`gateway/session.py`** — `reset_session(session_key, display_name=None)`
  (~line 1163): allocates a new `session_id`, swaps in a clean `SessionEntry`
  with `is_fresh_reset=True`, ends the old DB session. This is the **"start a
  fresh internal conversation/thread"** primitive (step 3).

- **`gateway/run.py`** (~line 9288) — the existing `compression_exhausted`
  auto-reset block: calls `reset_session`, evicts the cached agent, clears model
  / reasoning overrides, re-syncs the Telegram topic binding (#35809 guard), and
  appends the "🔄 Session auto-reset" notice. **This is the exact integration
  point for Context Rotation.** Today it *drops* context; V0 inserts a
  checkpoint-build-and-inject step *before* the drop so the fresh session is
  seeded instead of empty. The #35809 topic-binding re-sync and agent-cache
  eviction must be preserved verbatim.

- **`gateway/slash_commands.py`** — `_handle_reset_command(...)` (~line 64)
  handles **both `/new` and `/reset`**: discards `/queue` overflow, clears
  approval state, fires the plugin `on_session_reset` hook
  (`reason="new_session"`), and (for Telegram topics) re-binds via
  `_sync_telegram_topic_binding`. Section 7 defines how automatic rotation must
  reuse — not duplicate — this path.

### 3.5 Virgil pattern to mirror (fail-closed, builder-loaded, metered)

- **`gateway/virgil_preflight_gate.py`** — the precedent for how Hermes calls
  into Cogitator-built logic from inside the gateway:
  - Loads a Cogitator builder module by path (`_BUILDER_FILE =
    "cogitator_virgil_preflight.py"`), versions packets
    (`_PACKET_TYPE = "virgil_preflight_v0"`).
  - **Fail-closed**: any failure halts before model/tool activity and delivers a
    deterministic notice; detection is deterministic and local (no LLM
    classifier).
  - Records metrics to `storage/metrics/virgil_preflight_events.jsonl`.
  - Context Rotation V0 should mirror this shape: deterministic local detection,
    a versioned packet type (`context_checkpoint_v0`), a pluggable
    Cogitator-built builder discovered by path, and JSONL metrics. The key
    *difference* in posture: rotation is **fail-open toward the existing hard
    reset** — if checkpoint build/inject fails, fall back to today's
    drop-everything auto-reset rather than blocking the user.

- **`gateway/config.py`** — `SessionResetPolicy` (~line 274) is the existing
  config model for "when sessions reset." The rotation flag belongs adjacent to
  it, following the established `enabled: bool = False` + `_coerce_bool(...,
  False)` default-off convention used by other gateway dataclasses.

---

## 4. Where message / token counting happens

Authoritative count sources, in order of trust:

1. **Real prompt tokens** — `ContextEngine.last_prompt_tokens`, set by
   `update_from_response(usage)` from the provider's actual usage payload after
   each API call. This is the number `should_compress` trusts and the number
   rotation detection should trust.
2. **Rough pre-call estimate** — `estimate_messages_tokens_rough(...)` (imported
   in `agent/context_compressor.py`) plus a `CHARS_PER_TOKEN` heuristic and a
   flat per-image token cost. Used by `should_compress_preflight` *before* a call
   when no real usage exists yet.
3. **Message-count heuristics** — `len(history) > 50` is used in `gateway/run.py`
   as a coarse "this is a big session" guard alongside 400/413 errors. Useful
   only as a secondary safety net, never as the primary rotation trigger.

`context_length` (the model's window) is resolved by `get_model_context_length`
and recalculated on model switch via `ContextEngine.update_model(...)`. Rotation
thresholds must be expressed as **fractions of `context_length`** so they track
model switches automatically (same as `threshold_percent`).

**V0 decision:** detection uses **real `last_prompt_tokens` against
`context_length`** as primary, with the rough preflight estimate as the
pre-call early-warning. Do not introduce a new tokenizer.

---

## 5. Recommended thresholds

Rotation sits *above* compression on the same axis. Express everything relative
to `context_length`.

| Watermark | Fraction | Source signal | Action |
|---|---|---|---|
| Compress | `0.75` (existing `threshold_percent`) | `should_compress()` real tokens | In-place lossy summarization (unchanged) |
| **Rotate (soft)** | **`0.90`** | real `last_prompt_tokens / context_length` after a turn, *and* compression already ran ≥1× this session (`compression_count >= 1`) or `_ineffective_compression_count >= 1` | Build checkpoint → fresh thread → inject → continue |
| **Rotate (hard)** | n/a | `compression_exhausted` result key, or context-overflow error class (`context_overflow`, `payload_too_large`, `long_context_tier`) | Same rotation flow; this replaces today's drop-everything reset at `gateway/run.py:~9288` |
| Stop | n/a | `usage_limit_reached` (Section 8) | No rotation, no retry — surface reset-time message and stop |

Rationale:
- **0.90, not lower:** compression already owns 0.75–0.90. Rotating earlier would
  fight compression and churn sessions. Rotation should only fire when
  compression is failing to hold the line (hence the `compression_count >= 1` /
  ineffective-compression gate) or when the window is genuinely near full.
- **Hard rotate on exhaustion** turns today's lossy hard-reset into a
  checkpointed rotation — strictly better than dropping everything.
- All fractions are **config-overridable** (Section 9) with these as defaults.

A small **rotation cooldown** (e.g. no more than one rotation per N turns, and a
per-session rotation cap, e.g. 3) prevents a rotation→still-too-big→rotation
loop. If the rotation cap is hit, fall back to the existing hard reset.

---

## 6. How checkpoint build + injection should work

### 6.1 Build (step 2)

A **checkpoint** is a single compact, self-contained message that lets the agent
keep working without the prior transcript. V0 uses a two-source strategy:

1. **Cogitator builder (preferred, when present):** discover a builder module by
   path exactly like `virgil_preflight_gate.py` does (versioned
   `context_checkpoint_v0` packet). Cogitator owns "checkpoint builder / bridge"
   per #831. The harness calls it with the live message list + metadata and
   expects back a structured checkpoint packet (text + optional `relevant_files`
   / open-task list).
2. **Harness-local fallback (always available):** reuse
   `ContextCompressor._serialize_for_summary` + `_build_static_fallback_summary`
   (and `_compute_summary_budget`) to produce a summary. This is the same
   machinery compression already trusts, so V0 ships even before Cogitator's
   builder exists.

The checkpoint message must:
- Carry a metadata marker analogous to `COMPRESSED_SUMMARY_METADATA_KEY`
  (e.g. `_rotation_checkpoint`) so frontends and the wire-sanitizer treat it as
  ordinary content, never a special role (this is exactly the leak class #38788
  guards against).
- Preserve the **protected tail** (`protect_last_n`, default 6) verbatim where
  feasible, so the most recent turns survive the rotation un-summarized.
- Include the user's *current* pending request (the turn that triggered
  rotation) so "continue" has something to act on.

### 6.2 Start fresh + inject (steps 3–4)

- **Gateway path:** call `session_store.reset_session(session_key)` to get a
  clean `SessionEntry` (`is_fresh_reset=True`), then seed the new session's
  transcript with `[system_prompt?] + [checkpoint message] + [pending user
  message]` instead of leaving it empty. Preserve the existing post-reset chores
  from `gateway/run.py:~9288`: `_evict_cached_agent`, clear model/reasoning
  overrides, `_sync_telegram_topic_binding` (#35809).
- **Engine path:** route the session swap through
  `run_agent._transition_context_engine_session(...)` so the engine fires
  `on_session_end` (old) → `on_session_reset` → `on_session_start` (new) and
  zeroes its token/compression counters. A rotated session must *not* inherit the
  old `last_prompt_tokens`.
- The injected checkpoint becomes the new session's head; subsequent compression
  treats it as protected head content.

### 6.3 Continue safely (step 5)

- After injection, **re-enter the same turn** (the loop's existing
  `restart_with_compressed_messages` mechanism is the model: rebuild
  `api_messages` from the new transcript and continue the `while retry_count <
  max_retries` loop) rather than asking the user to resend.
- Reset `retry_count`, `compression_attempts`, and `_retry.primary_recovery_attempted`
  on a successful rotation, exactly as the eager-fallback path does
  (`agent/conversation_loop.py:~2786`).
- If the *first* call on the fresh+injected session **still** overflows, do not
  rotate again immediately — decrement the rotation budget and, when exhausted,
  fall back to today's hard reset with the existing notice.

---

## 7. How `/new` relates to automatic rotation

`/new` (and `/reset`) is the **user-initiated, context-discarding** boundary;
automatic rotation is the **system-initiated, context-preserving** boundary. They
must share plumbing but differ in payload:

| | `/new` (manual) | Auto-rotation (system) |
|---|---|---|
| Trigger | User command via `_handle_reset_command` | Threshold / exhaustion (Section 5) |
| Context carried | **None** — clean slate is the point | **Checkpoint injected** |
| Session swap | `reset_session` | `reset_session` (same call) |
| Engine lifecycle | `on_session_reset` fires | `on_session_reset` fires (via `_transition_context_engine_session`) |
| Plugin hook | `on_session_reset` (`reason="new_session"`) | same hook, **`reason="auto_rotation"`** |
| Topic re-bind | `_sync_telegram_topic_binding` | same |
| User notice | "/new" confirmation | "🔄 rotated, continuing" notice |

Rules:
- Automatic rotation **must reuse** `reset_session` + the `on_session_reset`
  plugin hook + topic re-bind, not reimplement them. The only addition is the
  checkpoint injection step and a distinct `reason` value so plugins/metrics can
  tell manual resets from auto-rotations.
- `/new` must **always win**: a user `/new` that arrives mid-rotation cancels any
  pending rotation and produces a truly empty session (no checkpoint).
- `/new` remains a hard boundary even when rotation is enabled — rotation never
  changes `/new` semantics.

---

## 8. How `usage_limit_reached` stops retries

`usage_limit_reached` is a **scheduled plan cap** (resets on a clock), not a
transient rate limit. Retrying it burns nothing useful and, on metered plans,
can deepen the hole. The rule:

> On a confirmed `usage_limit_reached`, **stop immediately**: no further retry
> iterations, no backoff sleep, no rotation, no fallback churn beyond a single
> credential-pool rotation attempt if one is configured.

Wiring (no new detector — reuse existing authorities):
1. **Detect** with the existing logic in `agent/agent_runtime_helpers.py:~695`
   (`usage_limit_reached` from `error_context.reason` / `.message`) and the
   `_classify_402` confirmed-billing branch
   (`agent/error_classifier.py:~922`, `retryable=False`). Treat
   `type == "usage_limit_reached"` (`gateway/run.py:~9570`) as the canonical
   provider signal.
2. **In the retry loop** (`agent/conversation_loop.py`, ~line 920): when the
   classified error is a confirmed hard usage cap, set `retry_count =
   max_retries` (terminate the `while`) and **skip `jittered_backoff`**. Do not
   route to compression or rotation — neither helps a plan cap.
3. **Surface** the existing reset-time message
   (`"Your plan's usage limit has been reached. It resets in ~Nh."`) and return a
   result flagged so the gateway does not auto-retry or re-enqueue.
4. **Distinguish from transient 429/402:** the `_USAGE_LIMIT_TRANSIENT_SIGNALS`
   path (`try again`, `resets at`, `window`, `requests remaining`) stays
   `retryable=True` and is untouched — only the *confirmed hard cap* short-circuits.
5. **Cogitator handoff:** when Cogitator's "hard usage-limit classification"
   lands, it replaces the harness heuristic as the authority for step 1; the
   stop-behavior (steps 2–3) stays in the harness.

This behavior should be active **independent of the rotation feature flag** — it
is a correctness fix, not a rotation feature — but its enablement can be gated by
the same flag in V0 to keep the blast radius of the initial change contained, then
graduated to always-on once proven.

---

## 9. Feature flag — default OFF

Rotation ships dark. Recommended config under the gateway/context namespace,
following the `enabled: bool = False` + `_coerce_bool(..., False)` convention
already used in `gateway/config.py`:

```yaml
# gateway config (illustrative — not applied by this doc)
context_rotation:
  enabled: false            # MASTER SWITCH — default off
  soft_watermark: 0.90      # fraction of context_length
  rotate_on_exhaustion: true
  max_rotations_per_session: 3
  cooldown_turns: 2
  stop_on_usage_limit: true # hard-cap retry short-circuit (Section 8)
  builder: "auto"           # "auto" = Cogitator builder if present else local fallback
  notify: true
```

- **`enabled: false`** is the single master gate. With it off, behavior is
  byte-for-byte today's: compression + hard auto-reset, no checkpoint.
- Every threshold is overridable; defaults match Section 5.
- The flag is read once per session/turn; no live-reload requirement for V0.

---

## 10. Tests required

All new tests; no existing tests modified by V0 except additive cases. Mirror the
existing test layout (`tests/agent/`, `tests/gateway/`).

**Detection / thresholds**
- Rotation does **not** fire below soft watermark.
- Rotation fires at soft watermark **only** when the compression-already-ran gate
  is satisfied (no rotation when compression hasn't been tried).
- Rotation fires on `compression_exhausted` and on context-overflow error classes.
- Thresholds track `context_length` across `update_model` (200K → 32K).

**Checkpoint build**
- Harness-local fallback produces a non-empty checkpoint from a known transcript.
- Cogitator-builder path is invoked when a builder module resolves; falls back to
  local builder when it is missing or raises (fail-open).
- Checkpoint message carries the `_rotation_checkpoint` metadata marker and never
  serializes a non-standard role onto the wire (regression guard, cf. #38788).

**Rotation flow (gateway)**
- `reset_session` is called and its return value is **captured** (regression of
  #35809 — see `tests/gateway/test_35809_auto_reset_clean_context.py`).
- Telegram topic binding is re-synced after rotation; agent cache evicted; model/
  reasoning overrides cleared.
- Fresh session transcript = `[checkpoint] + [pending user message]`, not empty.
- `on_session_reset` plugin hook fires with `reason="auto_rotation"`.

**Continue**
- After rotation, the turn re-enters the loop and completes without the user
  resending; `retry_count`/`compression_attempts` reset.
- Second consecutive overflow decrements rotation budget and, when exhausted,
  falls back to hard reset.

**usage_limit_reached**
- Confirmed hard cap → loop terminates with **zero** extra retries and **no**
  backoff sleep; reset-time message surfaced.
- Transient 402/429 (`try again` / `resets at`) is unaffected and still retries.
- `/new` cancels a pending rotation and yields an empty session.

**Flag**
- With `enabled: false`, no rotation path executes and behavior matches today.

---

## 11. Rollout plan

1. **Ship dark.** Land the flag (default off), detection, harness-local
   checkpoint builder, and the `usage_limit_reached` stop — all gated. No behavior
   change for any user.
2. **Shadow metrics.** When off, still record *would-rotate* events to
   `storage/metrics/context_rotation_events.jsonl` (mirror
   `virgil_preflight_events.jsonl`) to size frequency and validate thresholds
   against real traffic.
3. **Dogfood.** Enable on a single non-production session/operator (Cal's
   Telegram, or a cron lane) via config override. Watch rotation count, post-
   rotation success rate, and any context-loss complaints.
4. **Cogitator builder swap.** When Cogitator's checkpoint builder + hard
   usage-limit classifier land, switch `builder: auto` to prefer them; keep the
   local fallback as the safety net.
5. **Graduate `usage_limit_reached` stop to always-on** once proven (it is a
   correctness fix, not a feature).
6. **Default-on consideration** only after shadow + dogfood data show rotation
   strictly beats hard reset, and never below the documented watermarks.

## 12. Rollback boundary

- **Single switch:** `context_rotation.enabled: false` returns to today's exact
  behavior (compression + hard auto-reset). No data migration, no schema change,
  no session format change — a rotated session is just a normal session with a
  checkpoint message at its head.
- **No persisted state depends on rotation.** Disabling mid-flight is safe; an
  already-rotated session continues to work (its checkpoint is ordinary content).
- **The `usage_limit_reached` stop** can be rolled back independently via
  `stop_on_usage_limit: false` without disabling rotation.
- **Builder isolation:** a broken Cogitator builder cannot break the harness —
  the builder path is fail-open to the local fallback, which is fail-open to the
  hard reset.

## 13. Forbidden surfaces (do not touch in V0)

- **Provider/model config and credentials** — no changes to provider routing,
  API keys, `.env`, or secret sources.
- **`/new` and `/reset` semantics** — rotation reuses their plumbing but must not
  change their user-visible meaning (still a clean slate).
- **Compression internals** beyond *reading* `_serialize_for_summary` /
  `_build_static_fallback_summary` for the fallback checkpoint. Do not change the
  0.75 compression watermark or the summary budget math.
- **Wire-format / message roles** — the checkpoint must be ordinary content with
  metadata, never a new role (#38788 leak class).
- **The #35809 topic-binding re-sync and agent-cache eviction** in the
  auto-reset block — preserve verbatim; rotation inserts *before* the reset, it
  does not replace those guards.
- **Cogitator-side responsibilities** — the harness does not implement the
  checkpoint builder, the bridge, or the hard usage-limit classifier; it only
  defines and calls their seam.
- **Nous rate-limit cross-session breaker** (`agent/nous_rate_guard.py`) — out of
  scope; usage-limit stop must not interfere with it.

## 14. Implementation risk class

**Medium-high.** Justification:
- Touches the **core retry loop** (`conversation_loop.py`) and **session
  lifecycle** (`gateway/run.py`, `gateway/session.py`, `run_agent.py`) — the most
  regression-sensitive areas (#9893, #10063, #35809, #38788 all live here).
- Mitigated by: default-off flag, fail-open posture (every failure degrades to
  today's behavior), reuse of existing primitives (`reset_session`,
  `_transition_context_engine_session`, `restart_with_compressed_messages`,
  existing `usage_limit_reached` detector), and shadow-metrics before enablement.
- The **`usage_limit_reached` stop** alone is low risk (narrow, well-bounded) and
  could land as a separate smaller PR ahead of rotation.

---

## 15. Recommended implementation prompt

> Implement Context Rotation V0 in the Hermes/Virgil harness behind a
> default-off flag, per `docs/CONTEXT_ROTATION_HARNESS_V0_PLAN.md`.
>
> Land in three reviewable PRs:
>
> **PR 1 — usage_limit_reached stop (low risk).** In
> `agent/conversation_loop.py`'s retry loop, when the error classifies as a
> *confirmed* hard usage cap (reuse the detector in
> `agent/agent_runtime_helpers.py:~695` and `_classify_402`'s billing branch),
> terminate the loop with no further retries and no `jittered_backoff`, and
> surface the existing reset-time message. Leave transient 402/429 retry behavior
> untouched. Gate behind `context_rotation.stop_on_usage_limit` (default true).
> Add the tests in Section 10 under "usage_limit_reached".
>
> **PR 2 — detection + harness-local checkpoint + rotation flow (default off).**
> Add `context_rotation` config to `gateway/config.py` (default `enabled:
> false`, watermarks per Section 5). Add an optional `should_rotate` /
> `build_checkpoint` capability to `ContextEngine` with a default implementation
> on `ContextCompressor` that reuses `_serialize_for_summary` /
> `_build_static_fallback_summary`. At the `compression_exhausted` auto-reset
> block in `gateway/run.py` (~9288), when the flag is on, build a checkpoint and
> seed the fresh `reset_session` transcript with `[checkpoint] + [pending user
> message]` before the existing reset chores (preserve the #35809 re-sync and
> agent-cache eviction). Route the swap through
> `run_agent._transition_context_engine_session`. Fire `on_session_reset` with
> `reason="auto_rotation"`. Re-enter the turn via the existing
> `restart_with_compressed_messages` mechanism. Record would-rotate/did-rotate
> events to `storage/metrics/context_rotation_events.jsonl`. Add all remaining
> Section 10 tests.
>
> **PR 3 — Cogitator builder seam.** Add a fail-open builder loader modeled on
> `gateway/virgil_preflight_gate.py` (versioned `context_checkpoint_v0` packet,
> discovered by path) that prefers a Cogitator-built checkpoint builder and falls
> back to the harness-local builder when absent or on any error.
>
> Constraints: do not change `/new`/`/reset` semantics, the 0.75 compression
> watermark, provider/credential config, `.env`, or message wire-format. Do not
> enable rotation by default. Run the full `tests/agent` and `tests/gateway`
> suites; ensure existing #9893/#10063/#35809/#38788 regression tests still pass.

---

## 16. Validation of *this* (docs-only) change

```bash
git diff --check                       # no whitespace/conflict errors
git status --short                     # expect exactly one line:
#   ?? docs/CONTEXT_ROTATION_HARNESS_V0_PLAN.md
```

Only `docs/CONTEXT_ROTATION_HARNESS_V0_PLAN.md` is added. No runtime code, tests,
provider config, secrets, or services are modified by this document.
