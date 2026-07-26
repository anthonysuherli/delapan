---
name: backlog
description: Show the KB's curation backlog — gap/sparse queries it was asked and couldn't answer, ranked by demand. Use when deciding what to research next, or before running explore without a topic.
---

# Delapan Backlog

What the KB has been asked and failed to answer, ranked by recurrence × severity × recency.

## Target resolution

```bash
git rev-parse --show-toplevel | xargs basename   # → project
git branch --show-current                         # → kb
```

## Workflow

1. Call **`delapan_backlog`** with `project` and `kb`.
2. Present the ranked topics: `query_text`, `coverage`, `recurrence`, `score`.
3. To fill the top gap, call **`delapan_explore`** with **no `prompt`** — it consumes the top topic itself. To fill a different one, pass that topic's `query_text` as the prompt.

## Notes

- An empty backlog is normal for a new KB: topics only appear once `resume`/`search` records a gap or sparse verdict.
- A topic leaves the backlog when it is consumed by an explore, or when a later query on it reads `rich` (the gap closed).
- Recording is best-effort and never blocks: a KB with curation disabled always returns an empty backlog.
