# GPT Researcher venv — dependency & security inventory (Phase 1, Cogitator #1012)

Isolated venv for the `POST /research_gather` provider. **Never** installed
into the gateway's main venv or Railway; created only by
`gateway/scripts/setup_gptr_venv.sh` at `~/.hermes/gptr-venv`.

## Pinned surface

- Lock: `gateway/gptr-requirements.lock` — exact `uv pip freeze` of the venv
  that passed the Phase 0 B3 proof-of-fit (2026-07-10).
- Anchor pins: `gpt-researcher==0.15.1`, `ddgs==9.14.4`, Python 3.11.15.
- 185 packages; dominated by the langchain stack (`langchain`,
  `langchain-community`, `langchain-core`, `langchain-openai`, …), scraping
  (`beautifulsoup4`, `lxml`, `playwright`-free config), and HTTP clients.

## Measured footprint (this host, 2026-07-10/11)

| Metric | Value |
|---|---|
| Disk | 902 MB venv |
| Install time | ~8 s with uv (warm wheel cache) |
| Cold import (`import gpt_researcher`) | 1.2–1.6 s |
| Import-only peak RSS | 154 MB |
| One discovery run (worker, live smoke) | 12.8 s, $0.0075 est., 5 sources |
| Full B3-style run peak RSS (Phase 0) | ~333 MB |

## Vulnerability scan

`pip-audit` result against the lock is recorded in the PR conversation; the
repo's existing `osv-scanner` CI workflow also picks up this lock file on
every subsequent change.

## Isolation & trust model

- One-shot **subprocess** per request (`gateway/gptr_gather_worker.py`),
  spawned by the bridge with: scratch `TemporaryDirectory` cwd (auto-removed),
  scratch-scoped `HOME`/`XDG_*`, `PATH=/usr/bin:/bin`, and `OPENROUTER_API_KEY` only,
  150 s wall-clock timeout, stdout-JSON protocol.
- The worker pins its own model/retriever env unconditionally, so host env
  drift can never silently change the proven Phase 0 configuration.
- The bridge rebuilds the response **field-by-field from a whitelist**
  (url/title/snippet + count/latency/cost): verdicts, promotion
  recommendations, ledgers, or agent instructions can never cross the bridge
  even from a compromised worker.
- Residual risk (accepted, documented): the subprocess runs as the same Unix
  user — filesystem isolation is by convention (scratch cwd), not by
  sandboxing. Upgrade path if this ever matters: systemd `DynamicUser=` or a
  container. Neither repo nor `~/cogitator-brain` is passed to the worker.
- Cogitator-side containment (counterpart PR): URLs are leads only — every
  retained source is independently re-fetched through Cogitator's SSRF guard,
  and provider snippets are never evidence.
