# delapan

**The grounding engine behind context-aware AI tooling.** Capture intent, ground
every answer in a maintained knowledge base, and fill gaps from the web on demand —
**ground → grow → answer**.

delapan runs fully local (SQLite + `sqlite-vec`, no cloud, no account) or behind your
own storage via a small `Store` protocol. It ships as an MCP server, so any MCP client
(Claude Code, etc.) can use it out of the box.

## Quickstart — local, no credentials

```bash
pip install "delapan[local]"

# MCP server for Claude Code / any MCP client (resume, search, explore, projects)
python -m delapan.mcp.server

# or a loopback HTTP API on 127.0.0.1 (health, projects, KG read/write,
# findings, synopsis, resume, explore-over-SSE under /api/*)
python -m delapan.api.main
```

MCP tools: **`delapan_resume`** (tap a KB → resume card), **`delapan_search`**
(semantic recall over findings), **`delapan_explore`** (gap-fill from the web,
needs LLM + Tavily keys), **`delapan_projects`** (cross-repo discovery).

```python
# the engine, on SQLite, with no cloud creds:
from delapan.store import get_store
from delapan.mcp.tenancy import resolve_tenant

ctx = resolve_tenant("my-repo", "main", create=True)   # tenant on the local store
store = get_store()
print(store.count_findings(ctx.kb_id))
```

The local tier stores everything in `~/.delapan/delapan.db` (override with
`DELAPAN_DB_PATH`). No Supabase, no API key, loopback-only.

> **Status:** the engine core (grounding, exploration, findings, KB/project
> persistence), the `Store` seam, the MCP server, and the local HTTP API
> (`/api/*` — mirrors the MCP surface plus KG read/write for a control-panel
> frontend) all run on SQLite today — see [Roadmap](#status--roadmap).

## What's inside

| Capability | Module |
|---|---|
| **Coverage-banded grounding** — score how well the KB covers a query | `core/agent/` |
| **Gap-fill exploration** — plan → search → crawl → extract → merge | `core/exploration/` |
| **Write-time resolution** — ADD/UPDATE/NOOP/SUPERSEDE a candidate finding against its KB before persisting; nothing is ever deleted, only retired (bi-temporal `valid_from`/`invalidated_at`/`superseded_by`) | `core/memory/` |
| **Knowledge graph** — entities + relations over findings | `core/knowledge_graph/` |
| **Pluggable storage** — `Store` protocol; ships SQLite, plus a Supabase/pgvector backend | `store/` |
| **MCP server** | `mcp/` |

## Project tracking

Solo initiative status and prioritized backlog live in [`docs/tracking/`](docs/tracking/)
(markdown source of truth). See the [design spec](docs/superpowers/specs/2026-07-17-solo-project-tracker-design.md).

**Automatic sync**
- Local: `.git/hooks/post-commit` (installed from `.githooks/post-commit`) runs
  `scripts/tracking_sync.py` after commits that touch `docs/tracking/`.
- CI: GitHub Action `tracking-sync` mirrors on push (needs secrets
  `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`).

Manual:

```bash
uv run python scripts/tracking_sync.py --dry-run
uv run python scripts/tracking_sync.py
```

Findings, KBs, and projects are not separate submodules — that persistence lives
inside the `Store` implementations themselves (`store/sqlite.py`, `store/supabase.py`),
behind the one `Store` protocol below.

## Architecture — the storage seam

The engine **never** imports a storage client directly. It calls `get_store()`, which
returns a backend selected by `DELAPAN_BACKEND` (`local` | `cloud`, auto-detected from
creds when unset). Ship a new backend by implementing `store/base.py::Store`.

```python
from delapan.store import get_store

store = get_store()          # SQLiteStore on the local tier
findings = store.match_findings(kb_id, embedding, limit=10)
```

Every write to `findings` goes through `core/memory/persist.py::resolve_and_persist`,
not straight to `insert_findings` — a resolver decides per candidate whether it's
genuinely new, refines an existing finding, merely corroborates one, or contradicts
one, and applies that via the `Store`'s `update_finding`/`invalidate_finding`/
`supersede_finding` primitives. Set `memory.enabled: false` in `config.yaml` to fall
back to plain append-only ADD. `scripts/dedup_backfill.py` retires duplicates
already sitting in an existing KB (dry-run by default); `scripts/calibrate_bands.py`
recalibrates the coverage-band thresholds above for whichever embedding model is
active. Schema changes for this land in `migrations/` (cloud tier only — SQLite
migrates itself in-process).

The open-core distribution ships the **SQLite** backend, at parity with the cloud
Supabase/pgvector backend for both retrieval and the write-resolution path above.

## Configuration

Copy `.env.example` and fill the **local** block (the cloud block is optional
and only needed for a self-hosted multi-tenant deployment):

```bash
cp .env.example .env
```

## Development

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev,local]"
pytest && ruff check .
```

## Status & roadmap

**Working today (verified on SQLite, no cloud deps):**
- The `Store` seam — `get_store()` → `SQLiteStore`; tenancy, project listing, findings, synopsis, KG.
- The engine core — `agent` (preamble/synopsis/resume), `exploration`, `memory` (resolver + persist), `knowledge_graph` models.
- The tenancy gateway — `resolve_tenant()` resolves a local tenant through the store.
- The MCP server — `delapan_resume` / `delapan_search` / `delapan_explore` / `delapan_projects` (whole package imports; all 4 tools register and run).
- `python -m delapan.api.main` → `/health` plus the `/api/*` surface: projects,
  per-KB graph read/write (nodes/edges CRUD, stats, schema), findings
  list/get/delete, synopsis, resume, and explore over SSE. CORS allows the
  control-panel dev origins (`:5173`); `scripts/seed_demo_kb.py` seeds a
  credential-free demo KB to point a frontend at.

**Next:**
- The capture HTTP route (mirror the remaining MCP-adjacent surface over FastAPI).
- Concepts, drift, deepen, bridges, monitoring, user-profile, research reports, and the broader MCP tool surface.
- Store-route or gate the remaining cloud-coupled surfaces (`userprofile`, generic `knowledge_graph/builder`) — currently `[cloud]`-gated at call-time.

LLM-backed features (synopsis rebuild, exploration) need `ANTHROPIC_API_KEY` /
`AI_GATEWAY_API_KEY` / `OPENAI_API_KEY`; browse/tenant/persistence work without them.

## License

[AGPL-3.0-or-later](./LICENSE). Self-host freely; network-deployed modifications must
be shared under the same license. For commercial / non-AGPL licensing, contact the
maintainer.
