"""GPT Researcher gather worker — runs ONLY inside the isolated, pinned
``~/.hermes/gptr-venv`` (see ``gateway/gptr-requirements.lock`` and
``gateway/scripts/setup_gptr_venv.sh``), never in the gateway's main venv.

One-shot subprocess protocol (spawned by ``research_bridge_server``):
  stdin:  {"query": str, "max_sources": int}
  stdout: {"status": "ok", "sources": [{"url", "title", "snippet"}, ...],
           "count": int, "latency_s": float, "cost_usd_estimate": float}
          or {"status": "error", "error": "<sanitized fixed string>"}

Gathering material ONLY — no verdicts, no recommendations, no ledger, no
agent instructions ever cross this boundary (Phase 1 doctrine, Cogitator
issue #1012). Cogitator independently re-fetches every URL through its SSRF
guard; snippets are ranking hints, never evidence.

Pinned Phase-0 B3 discovery configuration (the run that passed the bar):
  gpt-researcher==0.15.1, RETRIEVER=duckduckgo,
  FAST/SMART/STRATEGIC_LLM = openrouter:openai/gpt-4o-mini,
  EMBEDDING = openrouter:openai/text-embedding-3-small.
The judge model is NOT here — judging is Cogitator-side and safety-critical.

OPENROUTER_API_KEY arrives via the subprocess environment (set by the bridge
from ~/.hermes/.env); it is never printed, logged, or echoed back.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

MAX_SOURCES_CAP = 20
SNIPPET_CHARS = 300

# Pinned discovery config — set unconditionally so a stray host env var can
# never silently change the proven configuration.
PINNED_ENV = {
    "RETRIEVER": "duckduckgo",
    "FAST_LLM": "openrouter:openai/gpt-4o-mini",
    "SMART_LLM": "openrouter:openai/gpt-4o-mini",
    "STRATEGIC_LLM": "openrouter:openai/gpt-4o-mini",
    "EMBEDDING": "openrouter:openai/text-embedding-3-small",
}


def _fail(reason: str) -> None:
    print(json.dumps({"status": "error", "error": reason}))
    sys.exit(0)  # protocol errors travel in JSON, not exit codes


async def _gather(query: str, max_sources: int) -> dict:
    from gpt_researcher import GPTResearcher

    t0 = time.time()
    researcher = GPTResearcher(query=query, report_type="research_report")
    await researcher.conduct_research()
    sources, seen = [], set()
    for s in researcher.get_research_sources() or []:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        raw = str(s.get("raw_content") or s.get("content") or "")
        sources.append({
            "url": url,
            "title": str(s.get("title") or "")[:200],
            "snippet": " ".join(raw.split())[:SNIPPET_CHARS],
        })
        if len(sources) >= max_sources:
            break
    try:
        cost = float(researcher.get_costs() or 0.0)
    except Exception:
        cost = 0.0
    return {
        "status": "ok",
        "sources": sources,
        "count": len(sources),
        "latency_s": round(time.time() - t0, 1),
        "cost_usd_estimate": round(cost, 4),
    }


def main() -> None:
    os.environ.update(PINNED_ENV)
    if not os.environ.get("OPENROUTER_API_KEY"):
        _fail("provider key unavailable")
    try:
        request = json.loads(sys.stdin.read() or "{}")
        query = str(request.get("query") or "").strip()
        max_sources = max(1, min(int(request.get("max_sources") or 10), MAX_SOURCES_CAP))
    except Exception:
        _fail("invalid request")
        return
    if not query:
        _fail("invalid request")
        return
    try:
        result = asyncio.run(_gather(query, max_sources))
    except Exception:
        # Details stay in stderr for local diagnosis; only a fixed string
        # crosses the protocol boundary.
        import traceback
        traceback.print_exc(file=sys.stderr)
        _fail("gather failed")
        return
    print(json.dumps(result))


if __name__ == "__main__":
    main()
