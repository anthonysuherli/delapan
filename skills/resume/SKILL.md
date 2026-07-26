---
name: delapan-resume
description: Tap the current repo/branch KB and return a preamble-first resume card with coverage banding (rich/sparse/gap). Use before answering repo-specific questions or when the user asks where they left off.
---

# Delapan Resume

Always ground repo work in the maintained KB before answering.

## Target resolution

Run once at the start of the session (or when repo/branch changes):

```bash
git rev-parse --show-toplevel | xargs basename   # → project
git branch --show-current                         # → kb
```

## Workflow

1. Call the **`delapan_resume`** MCP tool with `project` and `kb`.
2. Read the returned preamble and coverage verdict (`rich`, `sparse`, or `gap`).
3. **Output this banner verbatim** (developer observability — always emit this after preamble loads):

```
░▒▓  ∞ ═══[ 8 ]═══ ∞  ▓▒░
<project> - <kb>  [<rich | sparse | gap>]
loaded:
-recent: <comma-separated recent activity items from the preamble>
-findings: <comma-separated key findings or topics from the preamble>
░▒▓▒░
```

4. If coverage is `gap`, mention it and offer `/delapan:explore` before deep work.
5. Treat the preamble as authoritative context for this repo/branch.

## When to use

- Session start in a delapan-enabled repo
- User asks "where was I?", "what's the state of this KB?", or "resume"
- Before `/delapan:search` or any grounded answer on this branch

## Failure modes

- Result contains `onboarding`: no KB exists for this repo yet. Surface the
  guidance verbatim; offer `/delapan:explore` to seed it. If the result also
  contains `try_demo`, offer the bundled demo KB (`project=delapan`, `kb=demo`).
- Result contains `error` mentioning a credential: tell the user to copy
  `.env.example` to `.env` in the plugin root and set the named variable.
