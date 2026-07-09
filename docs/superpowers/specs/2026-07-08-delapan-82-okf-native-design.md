# delapan-82 — OKF-native fork design

**Date:** 2026-07-08
**Status:** Approved in brainstorming; awaiting implementation plan
**Target:** new repo at `8star/delapan-82` (sibling of `delapan-ai/`, `br8n/`)

## Premise

delapan-82 is a hard fork of the delapan KB engine in which Google's **Open
Knowledge Format (OKF) v0.1** — directories of markdown files with YAML
frontmatter, published by Google Cloud on 2026-06-12
([spec](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md))
— replaces JSON-dict/XML contracts as the **native interface for all context
operations**. The OKF bundle *is* the knowledge base; everything else (vectors,
caches, wire payloads) is derived.

Grounding fact from the pre-design code map: delapan is already halfway there.
Finding bodies are stored as rendered markdown (`_render_content` output is
exactly what gets embedded), the sidecar columns (`title, category, confidence,
tags, provenance, created_at`) are frontmatter-shaped, and a feature named
"OKF" (`OKFConfig`, `core/agent/concept_doc.py`, `grounded_hash`) already
synthesizes per-entity markdown docs. The fork is an inversion — the file
becomes the source of truth instead of a render of it — not an invention.

## Decisions (settled during brainstorming)

| # | Decision | Choice |
|---|---|---|
| 1 | Core goals | All three: agent-direct file access, git-native knowledge, ecosystem interop → **files canonical** |
| 2 | KB home | **In-repo bundle; git is the cloud.** No Supabase in the core path. This deliberately reverses the 2026-06-13 vision amendment ("retire SQLite, unify on Supabase") for this fork; a cloud index service may return later as an optional derived layer. |
| 3 | Frontend | **Deferred — engine first.** No sigma.js port, no `/api/*` wire surface in v1. OKF's reference static HTML visualizer covers interim browsing. |
| 4 | Agent writes | **Agents are full producers.** The Write tool is the ingestion API; the engine adopts agent-authored files via reindex. |
| 5 | Architecture | **B — Bundle-native engine.** The `Store` protocol dissolves into a `Bundle` abstraction + derived index sidecar (vs. rejected A: OKFStore adapter under the existing dict-row seam; C: minimal file-first with curation dropped). |

## Fork mechanics

- Single new git repo: engine + plugin shell (skills/, mcp.json). No frontend.
- br8n precedent applies verbatim: copy engine files, rename package
  `delapan.*` → `delapan82.*`, fully standalone, **never** a cross-repo import;
  engine fixes sync by hand.
- Config split preserved: secrets in `.env` (pydantic `Settings`), knobs in
  `config.yaml` (`AppConfig`); env override prefix becomes `D82_<SECTION>__<FIELD>`.
- The existing concept-doc "OKF" feature is **absorbed, not renamed**: concept
  docs become real OKF files (name collision resolved by convergence).

## Bundle anatomy

A KB is an OKF v0.1-conformant directory inside the repo it describes.
Default `knowledge/` (visible, so knowledge diffs ride along in PRs);
configurable via `bundle.dir`. `kb = branch` is structural: switching branches
switches knowledge.

```
knowledge/
  index.md          # OKF reserved: progressive-disclosure listing; okf_version: "0.1"
  log.md            # OKF reserved: chronological journal (explore runs, captures)
  synopsis.md       # type: synopsis — topic/gloss spine; frontmatter:
                    #   finding_count_at_build, built_at, model
  findings/<slug>.md   # type: finding
  concepts/<slug>.md   # type: concept
  references/          # optional citations, per OKF convention
  .index/              # DERIVED, gitignored: sqlite-vec vectors + id→path map
                       #   + content hashes. Never source of truth, never committed.
```

**Finding frontmatter:** `id` (immutable uuid hex), `type: finding`, `title`,
`category`, `confidence`, `tags`, `provenance`, `created_at`, plus the fields
today's persist silently drops — `quality`, `source_count`, `exploration_id`,
`extraction_model` — kept deliberately. Body = markdown, byte-identical to
today's `content` column.

**Concept frontmatter:** `id`, `type: concept`, `label`, `entity_type`,
`aliases`, `grounded_hash` (existing FNV-1a pattern, its original
grounding-staleness job). Body = concept doc; an `# Evidence` section links to
grounding findings as bundle-relative links (git-diffable provenance replacing
`grounded_in` id lists).

**Identity:** filename = human slug from title; identity = frontmatter `id`.
The derived index maintains id→path, so renames re-associate and links repair.
Links are bundle-relative paths per OKF idiom; broken links are tolerated per
spec.

## Components

Package layout `delapan82/{bundle, index, core, mcp}`:

- **`bundle/`** — replaces the `Store` protocol. `Bundle`: root discovery, doc
  read/write/list, frontmatter parse + validation, link resolution,
  `index.md`/`log.md` maintenance. `identity`: slugging + id allocation.
  `writer`: atomic temp+rename writes → index update → log append. This is the
  **single** persist boundary (kills the `_render_content` /
  `_normalize_provenance` duplication between `mcp/server.py` and
  `routes_explore.py`).
- **`index/`** — derived sidecar: sqlite-vec embeddings, id→path map, content
  hashes. Lazy revalidation on every engine read entry point: mtime scan →
  hash check → re-embed changed, drop deleted, adopt new files. Content hashes
  live **only in the index** (frontmatter hashes would be git-diff noise).
- **`core/exploration/`** — ports unchanged (plan → search → crawl → extract →
  evaluate → merge already returns unpersisted findings). Persist call becomes
  `bundle.writer`. **Cross-run dedup (new; fixes a current flaw where
  re-running an exploration duplicates rows):** before writing, query the index
  for similar existing findings; above `dedup.threshold` (config knob), merge
  into the existing file — provenance-URL union + the existing
  `(1 − 0.6^sources) × quality` confidence formula — instead of duplicating.
- **`core/agent/`** — `band_findings` / `assess_coverage` (thresholds
  .55/.40/.25 in `TiersConfig`) untouched. `render_preamble` re-targeted from
  the bespoke XML dialect to a concatenated stream of OKF excerpts (frontmatter
  summary + body, same 7000-char budget and per-doc caps). Synopsis rebuild
  gates (+15 findings / 168h, never-raises discipline) port as-is; output →
  `synopsis.md`.
- **LLM-facing JSON stays JSON** — `structured_completion`, `ExplorationPlan`,
  `FindingBatch`, embeddings, Tavily clients are provider plumbing, not
  knowledge. The `use_json_schema=False` free-form-dict gotcha is preserved.

## Data flow

**Read path (the inversion):** query → embed → KNN over sidecar → resolve hits
to files → freshness check → render from files. Similarity scores are
per-query ephemera: they appear in tool-result envelopes, never in files.

**Write path:** pipeline output, agent Write-tool output, human edits, and git
merges all mutate files; the next engine read revalidates and re-indexes. A
skill-documented frontmatter template tells agents what to emit.

## Interface surface

Three MCP tools (down from four — `delapan_projects` drops; git is project
management now). None of them take `project`/`kb` arguments anymore: the
bundle is discovered from the working repo (root discovery in `bundle/`), and
the branch supplies KB identity. JSON envelopes remain (MCP transport); every
knowledge payload inside them is OKF markdown.

| Tool | Returns |
|---|---|
| `delapan_resume(query?, depth)` | `{coverage, bundle_path, preamble}` — curated markdown render **plus** directory pointer, so agents start curated and continue with Read/Grep natively |
| `delapan_search(query, limit?)` | hits as `{path, frontmatter, excerpt, similarity}` — pointers into files, not row dumps |
| `delapan_explore(prompt, max_findings?)` | paths of files written/merged; appends a `log.md` entry |

Skills mirror the current shell: `/d82:resume`, `/d82:explore`, `/d82:search`,
plus a capture-convention skill documenting the agent-authored-finding
template.

## Integrity & error handling

- **Conformance guarantees (per OKF spec):** unknown `type` tolerated, unknown
  frontmatter keys preserved on round-trip, broken links tolerated, missing
  optional fields accepted. Any OKF consumer — including Google's reference
  visualizer — can read a delapan-82 bundle raw.
- **Malformed frontmatter:** file excluded from index; warning via monitoring
  seam + `log.md`; the engine never rejects a bundle and never crashes on one
  bad file.
- **Renames** survive via frontmatter `id` re-association; **deletes** drop
  vectors; reindex is idempotent; writes are atomic (temp+rename).
- **No embeddings key:** graceful degradation — search/banding unavailable,
  resume returns synopsis + bundle pointer with coverage `unknown`; file
  reading never blocks on a provider.

## Testing

- Golden-bundle fixtures: write→read round-trip byte-identical; unknown
  frontmatter keys preserved.
- Conformance test: every engine-written bundle passes OKF v0.1 rules
  (parseable frontmatter, non-empty `type`, reserved-file structure).
- Invalidation properties: edit/delete/rename → index converges on next read.
- Dedup regression: running the same exploration twice yields no duplicate
  files.
- Ported pipeline tests run against a temp bundle.

## v1 success criteria

1. In any repo, `/d82:explore` produces a committed, reviewable `knowledge/` diff.
2. Fresh-session `/d82:resume` returns preamble + pointer with correct coverage banding.
3. A human edit to a finding is reflected in the next search with no manual step.
4. An agent-written finding file is adopted by the index on next read.
5. The bundle opens unmodified in Google's reference HTML visualizer.

## Explicitly out of v1

- Frontend and the `/api/*` loopback wire surface (deferred, not dropped).
- Supabase/cloud tier and multi-tenant serving.
- Ghost/dead surfaces flagged by the code map — `/v1/*`, `/agent`,
  `run_deepen`, `Store.record_access`, `search_mode`,
  `SearchConfig.max_finding_chunks` — are **not ported**.
- LLM-mediated content merge for near-duplicate findings (v1 merges
  provenance/confidence only; content merge is a follow-on).

## Known risks

- **Index/file disagreement** is the new failure class (file edited, vector
  stale; file deleted, vector dangling). Mitigation: lazy revalidation on
  every read entry point + idempotent rebuild; a full `reindex --force` escape
  hatch.
- **Git merges can create semantic duplicates** no pipeline ever sees. v1
  accepts this (dedup catches high-similarity pairs at next persist;
  retrieval-time dedup is a follow-on).
- **OKF is v0.1 and will move.** The engine reads/writes through one
  `bundle/` module, so spec bumps are localized; `okf_version` is declared in
  root `index.md`.
