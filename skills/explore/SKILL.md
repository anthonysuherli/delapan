---
name: delapan-explore
description: Run the gap-fill exploration pipeline (plan → search → crawl → extract → merge) when KB coverage is sparse or gap. Blocks ~1–3 minutes. Requires AI_GATEWAY_API_KEY and TAVILY_API_KEY in the plugin's .env.
---

# Delapan Explore

Fill knowledge gaps from the web and merge new findings into the KB.

## Target resolution

```bash
git rev-parse --show-toplevel | xargs basename   # → project
git branch --show-current                         # → kb
```

## Workflow

1. Confirm explore is appropriate — coverage `gap`/`sparse`, or user explicitly asked to research/fill gaps.
2. Call **`delapan_explore`** with `project`, `kb`, and an optional focus prompt (user's topic).
3. **No topic in mind?** Call `delapan_explore` with **no `prompt`** to consume the top item of the curation backlog (see the `backlog` skill). An empty backlog returns an error and creates nothing.
4. Wait for completion (synchronous; may take 1–3 minutes).
5. Re-run **`delapan_resume`** to refresh preamble/coverage, then answer from updated findings.

## Prerequisites

- `AI_GATEWAY_API_KEY` (and `TAVILY_API_KEY`) in the plugin root's `.env` — copy `.env.example`

If keys are missing, tell the user which env vars to set — do not silently skip.

## Failure modes

- Result contains `error` mentioning a credential: tell the user to copy
  `.env.example` to `.env` in the plugin root and set the named variable.
- Explore result has `status: "empty"`: no findings were produced — relay the
  `reason` field verbatim (never report an empty run as success).
