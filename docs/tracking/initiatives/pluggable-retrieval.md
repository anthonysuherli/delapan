---
title: Pluggable retrieval
status: proposed
repo: backend
blocked_by: []
spec: null
plan: null
branch: null
updated: 2026-07-17
---

pgvector (Supabase) canonical; Elasticsearch selectable by config with no engine
call-site changes — both satisfy the same `Store` contract (vision End Goal 2).

**Next step**

Inventory current vector search call sites and confirm what an Elasticsearch
adapter would need behind the existing `Store` seam.
