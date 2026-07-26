---
name: delapan-search
description: Semantic recall over findings in the current KB. Use when the user asks a question that should be answered from ingested knowledge, not from guessing or generic web search.
---

# Delapan Search

Grounded semantic search over the current branch's findings.

## Target resolution

```bash
git rev-parse --show-toplevel | xargs basename   # → project
git branch --show-current                         # → kb
```

## Workflow

1. If no preamble this session, run **`delapan_resume`** first (see delapan-resume skill — it will emit the observability banner).
2. Call **`delapan_search`** with `project`, `kb`, and the user's query.
3. Answer from returned findings. Cite finding titles/snippets; do not invent facts not in results.
4. If results are thin and coverage was `gap`, suggest `/delapan:explore` to fill gaps.

## Query

Use the user's question verbatim unless it needs disambiguation for retrieval.

## Failure modes

- Result contains `error` mentioning a credential: tell the user to copy
  `.env.example` to `.env` in the plugin root and set the named variable.
