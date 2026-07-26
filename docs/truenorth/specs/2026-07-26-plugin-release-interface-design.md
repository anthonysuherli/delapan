# Claude Code plugin release interface — portable shell + first-run-aware engine

**Date:** 2026-07-26
**Status:** draft — pending ratification
**Vision goals served:** the vision's core framing (engine "tapped by Claude Code");
the open-core local tier as the public free story (Non-Goals, billing note); the
on-ramp to End Goal "Hosted public tier with account isolation" (the plugin is the
free door, the cloud connector the upgrade path). Invariants honored: local tier
stays auth-less; offline suite stays hermetic on SQLite; all new knobs are config,
never hardcoded; `Store` parity untouched (no `Store` method changes).

## Problem

Delapan's richest user interface — the Claude Code plugin (six skills over a
10-tool stdio MCP server) — is its least release-ready surface. The shell lives
unversioned in the `delapan-ai/` working folder, `mcp.json` hardcodes an absolute
path to a local venv, `plugin.json` and the skills on disk have drifted, and
first-run behavior assumes the author's machine: multi-key `.env`, existing KBs,
and two known landmines (a quota-capped explore reports "completed" with zero
findings; a stale `OPENAI_API_KEY` gate in `api/deps.py` contradicts the gateway
setup). A stranger cannot install it, and if they could, their first ten minutes
would be tracebacks and false successes.

Meanwhile the claude.ai cloud connector is live (Fly, Supabase OAuth) and codex8
is a pre-build scaffold. This release anchors on the **plugin**; the design must
not preclude the other two but does not change them.

## Decisions (ratified in brainstorming, 2026-07-26)

1. **Anchor:** Claude Code plugin, public-marketplace install, open-core
   auth-less SQLite tier. Cloud connector and codex8 out of scope.
2. **Home:** the plugin shell moves into this repo (the public open-core engine
   repo). One repo = engine + plugin; no shell/engine drift possible.
3. **Bootstrap:** a `uv run` wrapper script; no venv paths in `mcp.json`.
4. **First run:** one required key (AI Gateway) + a pre-seeded demo KB so
   recall demonstrates value with zero keys.
5. **Skill lineup:** resume, search, explore, projects, backlog, model.
   `tracking` stays private (personal Supabase sync).
6. **Approach:** portable shell **plus** engine-side first-run awareness
   (onboarding card, actionable key errors, landmine fixes) — chosen over
   packaging-only because the known failure modes are exactly what a stranger
   hits first, and fixes at the MCP layer are inherited by every client.

## Design

### 1. Packaging & distribution

Repo-root additions to this repo:

```
.claude-plugin/
  plugin.json          # name "delapan", six skills, mcpServers → ./mcp.json
  marketplace.json     # enables: claude plugin marketplace add anthonysuherli/delapan
skills/                # resume, search, explore, projects, backlog, model
mcp.json               # stdio server via wrapper below
scripts/mcp-server.sh  # exec uv run --project "${CLAUDE_PLUGIN_ROOT}" python -m delapan.mcp.server
data/demo.db           # pre-built demo KB (see §2)
.env.example           # AI_GATEWAY_API_KEY=…  TAVILY_API_KEY=… (optional)
```

- **Wrapper is the only launch path.** Claude Code sets `${CLAUDE_PLUGIN_ROOT}`
  to the cloned plugin dir; the wrapper `exec`s `uv run` there, with the plugin
  root as cwd so `.env` and `config.yaml` resolve. uv materializes the env from
  `uv.lock` on first launch. If `uv` is missing, the wrapper exits non-zero with
  a single stderr line giving the install command.
- **Install story:** `claude plugin marketplace add anthonysuherli/delapan` →
  install `delapan` → done. During development, the marketplace is added from
  the local repo path; the old unversioned shell in `delapan-ai/` is retired.
- **Versioning:** `plugin.json` version is bumped alongside `pyproject.toml` in
  the release checklist (single commit; no automation for now).
- **License:** `plugin.json` must match this repo's `LICENSE` before publishing
  (current shell says AGPL-3.0 — verify at implementation).

### 2. First-run UX

- **Demo KB.** `data/demo.db` is a small SQLite KB about delapan itself
  (architecture, findings/preamble model, how explore works). On server start,
  if the default DB is absent, the server copies `data/demo.db` into place
  (path resolution unchanged; `DELAPAN_DB_PATH` still overrides). First
  `delapan_projects` already lists the demo project; `delapan_search` returns
  grounded findings with zero keys. The copy is local-tier-only by design —
  cloud onboarding is signup — and involves no `Store` method, so the parity
  invariant is untouched.
- **Onboarding card.** `delapan_resume` for a repo/branch with no KB returns
  the `gap` verdict plus onboarding guidance: run `/delapan:explore` to seed
  (states the key requirement), and — only when the demo project exists in the
  store — meanwhile try `/delapan:search` on the demo KB. Implemented in the
  MCP tool layer so the cloud connector (and a future codex8 port) inherit it;
  the demo mention self-suppresses on cloud, where no demo KB is seeded.
- **One-key story.** Required: the AI Gateway key (LLM + embeddings).
  Optional: `TAVILY_API_KEY` — without it, explore refuses up front with one
  clear line; resume/search still work. Any missing/invalid credential produces
  a single actionable message naming the variable and `.env` location — never a
  traceback. `.env` lives at the plugin root, untracked (survives plugin
  updates); `.env.example` ships.
- **Landmine fixes.**
  - Explore runs that write zero findings (Tavily quota, no results) return an
    explicit `empty` status with the reason; the explore skill renders it
    verbatim. No zero-finding run may report plain "completed".
  - The stale `OPENAI_API_KEY` gate in `api/deps.py` is removed/corrected to
    match the gateway-based credential model.

### 3. Skills polish

Sanitization pass over the six shipped skills: no personal paths, KB names, or
Supabase references; each skill states its failure modes (empty KB → explore;
missing key → `.env`). README gains a stranger-facing quickstart: two install
commands → first resume (demo card) → add key → first explore on their repo.

## Error handling (summary)

| Condition | Behavior |
|---|---|
| `uv` not installed | wrapper exits non-zero, one stderr line with install command |
| No KB for current repo | resume returns gap verdict + onboarding card |
| No/invalid AI Gateway key | one actionable message naming variable + `.env` path |
| No Tavily key, explore invoked | explore refuses up front, one clear line |
| Explore writes zero findings | status `empty` + reason (quota / no results), rendered by skill |

## Testing

- **Hermetic unit tests (SQLite tier, offline):** onboarding card on empty KB;
  key-gate messages; explore `empty` status; demo-KB first-run copy (present →
  no copy; absent → copied, demo project listed).
- **Stranger round-trip e2e (scripted):** temp `HOME` → install plugin from
  local marketplace path → wrapper cold-starts via `uv run` → `delapan_resume`
  yields onboarding card → `delapan_search` on demo KB yields findings. No
  cloud access anywhere in the suite.

## Acceptance criteria

1. Fresh machine with uv: marketplace add + install → `/delapan:resume` returns
   the onboarding/demo card — zero keys configured, zero tracebacks.
2. `/delapan:search` on the demo KB returns grounded findings with zero keys.
3. With one key set, `/delapan:explore` on a real repo produces findings;
   empty/quota runs report `empty` + reason, never silent success.
4. No personal references in any shipped skill; `tracking` absent from the
   shipped plugin.
5. Offline test suite green on the SQLite tier with no cloud access.

## Non-goals

- claude.ai connector changes (already live; inherits MCP-layer onboarding).
- codex8 build (own spec/track in `~/Repositories/8star/codex8`).
- PyPI publishing / `uvx` distribution.
- Windows-native support (documented as WSL for now).
- Any `Store` protocol change; any cloud-tier onboarding change.
