---
title: Live delta stream
status: proposed
repo: both
blocked_by: []
spec: null
plan: null
branch: null
updated: 2026-07-17
---

SSE or websocket from engine → frontend so new nodes/edges appear on the sigma
canvas during explore/ingest without manual refresh (vision End Goal 3).

**Next step**

Draft a short design spec: event schema, transport choice (SSE vs WS), and
which explore/KG write paths emit deltas.
