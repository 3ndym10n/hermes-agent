# Porkbun discovery exhausted its attempts — RCA and recovery

Job `cj_46091c0f_557a_46eb_b21b_fbc70f333a2c` paused after three
`porkbun_discover` actions on step `s01_porkbun_discovery_5` failed with the
generic `provider_step_failed` code. No purchase and no DNS mutation occurred:
the failing step is `read_only`, and the only consequential step in the plan
(`s02_porkbun_register`) is never dispatched without a bound purchase approval.

## What the code proves

* `provider_step_failed` is produced in exactly one place —
  `CommerceOperator._execute`, when a non-`ProviderStepError` escapes a
  non-consequential handler.
* Every failure path *inside* the discovery handler's provider block already
  mapped to a `porkbun_*` code, so the recorded generic code proves the
  exception escaped **after** that block: the evidence write, the expanded-plan
  build, or the plan replacement in `_finish_handler_result`.
* It also proves the escape happened **before** `finish_action(succeeded)`. Had
  it come later, the compensating `finish_action(failed)` would itself have
  raised `action_not_terminalizable` and the actions would still read
  `succeeded`; all three read `failed`.
* Discovery checked all ten candidates on every attempt with a 10 s sleep
  between each — 10 live availability calls and ~90 s of blocked worker per
  attempt, 30 calls across the three attempts, against the same per-key rate
  limit the purchase leg needs.
* On a rate limit the worker returned the job to `ready`, so attempts two and
  three were dispatched immediately inside the same window.

## What remains unproven

Which of the four post-provider statements raised. Distinguishing them needs
the persisted result rows of the live job, and this change deliberately does
not read or mutate the commerce database. The stage codes added here
(`porkbun_read_failed`, `porkbun_rate_limited`, `porkbun_response_invalid`,
`porkbun_plan_replacement_failed`, `evidence_payload_forbidden`,
`evidence_write_failed`) make the next occurrence self-identifying.

The live read-only probe that returned `RATE_LIMIT_EXCEEDED` on its second
availability request was a separate diagnostic session, not one of this job's
three attempts. It is real evidence about the provider limit; it is not
evidence about which statement failed here.

## Approved post-deployment recovery

Run only after this change is deployed and the worker restarted. Nothing below
touches Porkbun before the bound purchase approval.

1. Cancel the exhausted job:

       /store cancel cj_46091c0f_557a_46eb_b21b_fbc70f333a2c

   Cancellation is refused while a consequential action is unresolved, so a
   clean cancel is itself confirmation that no purchase is in flight.

2. Create one new job with the exact launch sentence. Do not paraphrase — the
   objective fingerprint is what attaches to the existing launch record.

3. Launch facts: the five approved facts (`contact_email`,
   `business_identity_sentence`, `double_opt_in`, `brand_signoff`,
   `privacy_signoff`) are re-read from Cogitator during planning. If the facts
   gate opens, supply them once through the gate; do not re-approve facts that
   already resolve.

4. Discovery now makes a single availability call while `warpsupply.com` is
   available. If Porkbun rate-limits it, the job pauses with
   `porkbun_rate_limited` and a `retry_after` timestamp on the action result.
   `/store resume` before that time re-pauses without spending an attempt;
   after it, the retry proceeds normally.

5. The registration step still opens the purchase approval gate and still
   re-quotes against live Porkbun immediately before the only real mutation.
   No provider write happens until that approval is bound.

## Known remaining ceiling

The 10 s spacing between availability checks is still a blocking `time.sleep`
in the worker. On the normal launch it is never reached, because the first
candidate is available and the scan stops there. It is only reached while
walking past unavailable candidates, and in the worst case (all ten taken) it
still blocks the worker for ~90 s. Making that scan resumable across ticks is a
redesign, not a hotfix; it is worth doing if the preferred domain is ever lost.
