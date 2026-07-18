---
title: Supabase storage unification
status: blocked
repo: backend
blocked_by: []
spec: docs/superpowers/specs/2026-06-22-supabase-store-cloud-tier-design.md
plan: docs/superpowers/plans/2026-06-22-supabase-store.md
branch: null
updated: 2026-07-16
---

Vision End Goal 0 (retire SQLite, single Supabase `Store`) is **stale**.
Amendment log 2026-07-16: write-path dedup shipped on both tiers; the codebase
has moved away from End Goal 0 twice. SQLite remains the local tier; cloud uses
Supabase. Unification is blocked until vision is revisited — do not treat End
Goal 0 as still in effect.

**Next step**

Revisit `docs/truenorth/vision.md` End Goal 0 and Planned Detour "Supabase
unification": decide whether to ratify dual-tier storage or resume SQLite
retirement before resuming this initiative.
