---
title: Write-path dedup
status: done
repo: backend
blocked_by: []
spec: docs/superpowers/specs/2026-07-16-write-path-dedup-design.md
plan: docs/superpowers/plans/2026-07-16-write-path-dedup.md
branch: null
updated: 2026-07-16
---

Findings now resolve ADD/UPDATE/NOOP/SUPERSEDE at write time via `core/memory/`.
Shipped on both SQLite and Supabase tiers — not Supabase-only as the original
vision assumed.

**Next step**

None — delivered. Keep this initiative as historical anchor; revisit only if
resolution semantics need extension (e.g. batch ingest, cross-KB dedup).
