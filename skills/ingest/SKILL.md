---
name: ingest
description: Research a topic using your own web search and reading, then persist the findings to the KB via delapan_add_findings. Use INSTEAD of /delapan:explore when you can search the web yourself — novel findings cost no LLM call, only near-duplicates do. Falls back to /delapan:explore for unattended or deployed runs.
---

# Delapan Ingest (agent-driven)

Research with your own tools; delapan only stores and dedupes. Genuinely novel
findings cost no LLM call (embedding only); near-duplicates route through a
resolution call to merge/refine them. Either way it's dramatically cheaper than
`delapan_explore`, which runs planning, per-page extraction, and evaluation LLM
calls throughout.

## When to use

Use this when you (the agent) have web search and fetch available. Use
`/delapan:explore` instead when the research must run unattended, on a schedule,
or on a deployed surface with no agent in the loop.

## Target resolution

```bash
git rev-parse --show-toplevel | xargs basename   # → project
git branch --show-current                         # → kb
```

## Workflow

1. **Decompose** the topic into 3-5 genuinely distinct sub-questions. Do this
   explicitly — the paid pipeline has a known defect where it collapses onto one
   subtopic, and you avoid it only by deliberately covering different ground.
2. **Search and read** for each sub-question with your own tools. Prefer primary
   sources; read the page rather than trusting the search snippet.
3. **Extract findings.** One claim per finding. A finding is a specific, checkable
   statement — not a summary of a page. Put the claim in `title` and the
   supporting structure in `content`.
4. **Cite everything.** Every finding needs `provenance` with the URL you actually
   read. Findings without provenance are rejected.
5. **Persist** with `delapan_add_findings(project, kb, findings)`.
6. **Confirm** with `delapan_resume` to see refreshed coverage.

## Finding shape

```json
{
  "title": "Vercel AI Gateway charges zero markup over provider list price",
  "category": "fact",
  "content": {"markup": "0%", "applies_to": "including BYOK"},
  "provenance": [{"url": "https://vercel.com/docs/ai-gateway/pricing"}],
  "confidence": 0.8,
  "tags": ["pricing"]
}
```

## Quality bar

- Set `confidence` honestly: ~0.9 for a primary-source number, ~0.5 for a single
  secondary blog, lower for anything inferred.
- Do not submit a finding you could not point at a specific line of a source for.
- Re-submitting overlapping material is safe — the resolver refines rather than
  duplicates.
