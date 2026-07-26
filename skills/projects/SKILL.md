---
name: delapan-projects
description: List cross-repo projects and branches with activity metadata. Use when the user asks what repos/KBs exist or wants to switch context across projects.
---

# Delapan Projects

Discovery surface for all projects and branch-KBs the store knows about.

## Workflow

1. Call **`delapan_projects`** (no project/kb required). Pass
   `include_archived: true` to also see archived entries.
2. Present repos and branches using `finding_count` and `last_finding_at` —
   these count live findings only, so they reflect real activity.
3. If the user picks a target, note `project` + `kb` for subsequent
   `delapan_resume` / `delapan_search` calls.
4. To put a stale KB away, call **`delapan_archive`** with `project` (and `kb`
   for a single KB). It is reversible — `archived: false` restores it — and
   never deletes anything. Running `delapan_explore` against an archived KB
   unarchives it automatically.

## When to use

- "What repos do I have in delapan?"
- "Which branches have findings?"
- Before resuming work in a different repo than the current cwd
