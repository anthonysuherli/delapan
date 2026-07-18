---
title: Cloud remote MCP server
status: done
repo: backend
blocked_by: []
spec: null
plan: null
branch: null
updated: 2026-07-18
---

Spec and plan live on `master` (not on this tracking branch): `docs/superpowers/
specs/2026-07-17-cloud-remote-mcp-design.md` and `docs/superpowers/plans/
2026-07-17-cloud-remote-mcp-server.md`.

delapan's four MCP tools (resume/search/explore/projects) are now reachable
from claude.ai over `streamable-http`, authenticated via Supabase Auth's own
OAuth 2.1 server (this codebase is a resource server only — no bespoke
authorization server). New module `delapan/mcp/cloud_server.py` reuses the
same tool implementations as the local stdio server
(`delapan/mcp/server.py`) via shared `_*_impl` functions; only tenancy
resolution differs (caller's own OAuth token vs. a fixed configured user).

Live at `https://delapan-cloud-mcp-799946348577.us-west1.run.app` on Google
Cloud Run (`delapan-475102` project, `us-west1`), running `stateless_http`
mode, scale-to-zero. Originally deployed to Fly.io first (`delapan-cloud-mcp`
app) to validate the design end-to-end; migrated to Cloud Run once
`stateless_http` was verified compatible, and the Fly app was destroyed.
Secrets live in GCP Secret Manager, readable only by a dedicated
`delapan-cloud-mcp` service account.

Companion initiative (not tracked separately — small, already merged): local
KBs `demo` and `qwen-hackathon` were ported to cloud Supabase alongside the
existing `actuary`, generalizing the one-off actuary port script
(`scripts/port_project_to_cloud.py`) to take any project name.

Deferred, not built: no rate-limiting/cost cap on `delapan_explore` for cloud
callers, and some Supabase calls in the tenancy/auth path run synchronously
inside async tool handlers (blocks the event loop under concurrent load).
Both fine at current single-user scale; see backlog.
