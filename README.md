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

# or a loopback HTTP API on 127.0.0.1 (currently /health)
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
> persistence), the `Store` seam, and the MCP server all run on SQLite today.
> The capture/resume/explore HTTP routes are the next surface — see
> [Roadmap](#status--roadmap).

## What's inside

| Capability | Module |
|---|---|
| **Coverage-banded grounding** — score how well the KB covers a query | `core/agent/` |
| **Gap-fill exploration** — plan → search → crawl → extract → merge | `core/exploration/` |
| **Knowledge graph** — entities + relations over findings | `core/knowledge_graph/` |
| **KB / findings / projects** persistence | `core/{findings,kbs,projects}/` |
| **Pluggable storage** — `Store` protocol; ships SQLite | `store/` |
| **MCP server** | `mcp/` |

## Architecture — the storage seam

The engine **never** imports a storage client directly. It calls `get_store()`, which
returns a backend selected by `DELAPAN_BACKEND` (`local` | `cloud`, auto-detected from
creds when unset). Ship a new backend by implementing `store/base.py::Store`.

```python
from delapan.store import get_store

store = get_store()          # SQLiteStore on the local tier
findings = store.match_findings(kb_id, embedding, limit=10)
```

The open-core distribution ships the **SQLite** backend. The hosted/cloud backend is a
separate, closed implementation behind the same protocol.

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
- The engine core — `agent` (preamble/synopsis/resume), `exploration`, `findings`, `kbs`, `projects`, `knowledge_graph` models.
- The tenancy gateway — `resolve_tenant()` resolves a local tenant through the store.
- The MCP server — `delapan_resume` / `delapan_search` / `delapan_explore` / `delapan_projects` (whole package imports; all 4 tools register and run).
- `python -m delapan.api.main` → `/health`.

**Next:**
- The capture / resume / explore HTTP routes (mirror the MCP tools over FastAPI).
- A test suite (port the SQLite store tests).
- Concepts, drift, deepen, bridges, monitoring, user-profile, research reports, and the broader MCP tool surface.
- Store-route or gate the remaining cloud-coupled surfaces (`userprofile`, generic `knowledge_graph/builder`) — currently `[cloud]`-gated at call-time.

LLM-backed features (synopsis rebuild, exploration) need `ANTHROPIC_API_KEY` /
`AI_GATEWAY_API_KEY` / `OPENAI_API_KEY`; browse/tenant/persistence work without them.

## License

[AGPL-3.0-or-later](./LICENSE). Self-host freely; network-deployed modifications must
be shared under the same license. For commercial / non-AGPL licensing, contact the
maintainer.
