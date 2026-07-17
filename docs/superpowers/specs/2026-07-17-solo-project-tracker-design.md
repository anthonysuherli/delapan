# Solo project tracker — markdown source of truth, Supabase sync, private dashboard route

*2026-07-17 · Status: approved design, pre-implementation · Line: backend master + frontend main · Scope: thin tracking layer for a solo deployed engineer developing delapan end to end.*

## Problem

As a solo engineer shipping delapan across backend and frontend, two things get lost:

1. **Initiative state** — what's active, blocked, done, or drifted (e.g. the storage End Goal in `vision.md` is stale while write-path-dedup shipped on both tiers). Status lives in heads, scattered branches, and amendment logs.
2. **Prioritized backlog** — next work is scattered across docs, branches, and chat. There is no single ordered list of what to do next.

Existing `vision.md`, dated specs, and plans already carry the *detail*. What's missing is a thin operational layer on top: status, blockers, links, and an ordered backlog — readable in git, syncable to a private view on delapan.ai.

## Decisions (approved 2026-07-17)

1. **Thin layer, not a rewrite.** `docs/tracking/` indexes initiatives and backlog; vision/specs/plans stay untouched as the detail.
2. **Markdown is source of truth.** One file per initiative (`docs/tracking/initiatives/<slug>.md`) with YAML frontmatter + prose body; one ordered `docs/tracking/backlog.md`. Lives in the **backend** repo (where `docs/` already lives); may reference frontend work.
3. **Hybrid delivery.** A sync script parses markdown → Supabase tables; the existing sigma.js frontend adds a private `/tracking` route that reads those tables.
4. **Auth = Supabase GoTrue.** Authenticated users only (RLS); no anon read; no public sign-up UI. Writes to tracking tables are service-role only (the sync script).
5. **Updates = agent skill.** Session-end skill updates markdown and runs the sync script. CI sync is out of scope for v1.
6. **Read-only dashboard in v1.** Edits happen only in markdown — one write path.
7. **Out of scope:** br8n, task-level tickets, GitHub Issues as source of truth, build-time JSON bundling, backend `/v1` tracking API, CI-on-push sync.

## Architecture

```
docs/tracking/initiatives/*.md  ──┐
docs/tracking/backlog.md        ──┤──► tracking_sync.py ──► Supabase tables
                                  │         (service role)
                                  │
                                  └──► git (human + agent readable)

Supabase GoTrue login ──► frontend /tracking ──► SELECT (RLS: authenticated)
```

**Units:**

| Unit | Purpose | Interface | Depends on |
|---|---|---|---|
| Markdown tracking folder | Source of truth | Files on disk under `docs/tracking/` | None |
| Parser + validator | Frontmatter/body → row dicts; hard-fail on bad data | Pure functions | Markdown files |
| Sync script | Mirror folder → tables (upsert + delete orphans + rewrite backlog) | CLI `scripts/tracking_sync.py` | Parser, Supabase service role |
| Supabase schema | Persist synced rows; enforce auth | Migration + RLS | Existing Supabase project |
| Frontend `/tracking` | Private status view | Path gate + GoTrue session + table reads | Supabase anon key + auth |
| Agent skill | Keep markdown current; run sync at session end | Skill under plugin `skills/` | Sync script, git |

## Components

### C1 · Markdown layout

```
backend/docs/tracking/
  backlog.md
  initiatives/
    <slug>.md
```

**Initiative frontmatter (required fields):**

| Field | Type | Values / notes |
|---|---|---|
| `title` | string | Display name |
| `status` | enum | `proposed` \| `active` \| `blocked` \| `paused` \| `done` \| `dropped` |
| `repo` | enum | `backend` \| `frontend` \| `both` |
| `blocked_by` | list of slugs | Must resolve to existing initiative files (or empty) |
| `spec` | path, URL, or null | Backend-repo-relative path (validated on disk) **or** absolute `https://` URL (not validated on disk); null if none |
| `plan` | path, URL, or null | Same rules as `spec` |
| `branch` | string or null | Git branch name, if any |
| `updated` | date | `YYYY-MM-DD` |

Body: free prose. Convention: end with a paragraph whose first line is `**Next step**` (or a `## Next step` heading) so the dashboard can extract it. Extraction rule (explicit): take the text under that marker; if neither marker exists, use the first non-empty paragraph of the body.

**Backlog format:** ordered markdown list; position = priority (1-based). Optional tags in brackets:

```markdown
# Backlog

- Fix coverage band drift on gemini embeddings [backend] [initiative:write-path-dedup]
- Stand up OrbStack local Supabase for hermetic tests [backend]
```

Tags: `[backend]` / `[frontend]` / `[both]` for `repo`; `[initiative:<slug>]` to link an item to an initiative when relevant. Untagged `repo` defaults to `both`.

### C2 · Seeding (implementation step)

Derive initial initiative files from current reality (not invent a greenfield backlog):

- Vision End Goals / Planned Detours + amendment log (storage unification stale/blocked; write-path-dedup delivered; live delta / HITL preview still open).
- Recent branches and in-flight docs (e.g. canvas v1 design on `docs/canvas-v1-design`).
- Frontend work visible from `delapan-fe` (findings view, etc.) only when it is a real initiative, not every commit.

Human corrects seed content before treating it as authoritative.

### C3 · Supabase schema

New migration `migrations/YYYY-MM-DD-tracking.sql` (date = implementation day):

**`tracking_initiatives`**

| Column | Type | Notes |
|---|---|---|
| `slug` | text PK | Filename stem |
| `title` | text | |
| `status` | text | Constrained to the enum above |
| `repo` | text | `backend` \| `frontend` \| `both` |
| `blocked_by` | text[] | Slugs |
| `spec` | text null | |
| `plan` | text null | |
| `branch` | text null | |
| `body_md` | text | Full markdown body |
| `updated` | date | From frontmatter |
| `synced_at` | timestamptz | Set by sync script |

**`tracking_backlog`**

| Column | Type | Notes |
|---|---|---|
| `position` | int PK | 1-based priority |
| `text` | text | Item text without tags |
| `repo` | text | Default `both` |
| `initiative_slug` | text null | From `[initiative:…]` |
| `synced_at` | timestamptz | |

**RLS:** `SELECT` for `authenticated` only. No policies for anon. No INSERT/UPDATE/DELETE for authenticated clients — writes use the service role (bypasses RLS) from the sync script only.

Reuse the existing Supabase project (same as engine cloud tier). Local OrbStack stack gets the same migration so local sync/dev stays hermetic.

### C4 · Sync script

`backend/scripts/tracking_sync.py`:

1. Load all `docs/tracking/initiatives/*.md` and `docs/tracking/backlog.md`.
2. Parse + validate (hard fail, non-zero exit, clear message — never skip bad rows):
   - Unknown `status` / `repo`
   - Missing required frontmatter
   - Dangling `blocked_by` slug
   - `spec` / `plan` is a non-URL relative path but the file is missing on disk
   - Duplicate slugs / non-list `blocked_by`
3. Mirror to Supabase:
   - Upsert all current initiatives by `slug`
   - Delete initiative rows whose files are gone
   - Replace backlog: delete all rows, insert current ordered list (simplest correct rewrite)
4. `--dry-run` prints the planned upserts/deletes without writing.
5. Creds from `.env` / Settings (service role URL + key). No hardcoded knobs. Follow existing script patterns (`scripts/dedup_backfill.py` style).

Idempotent: re-running with unchanged files is a no-op aside from `synced_at`.

### C5 · Frontend private route

The app has no router today (`App.tsx` is a single shell with a graph/findings toggle). Keep it that simple:

- If `window.location.pathname === "/tracking"` (and optionally trailing slash), render `TrackingApp` instead of the main control panel.
- `TrackingApp`:
  1. If no GoTrue session → email/password sign-in form (no sign-up).
  2. If session → fetch `tracking_initiatives` + `tracking_backlog` via `supabase-js` (anon key + user JWT).
  3. Render: initiatives grouped by status (`active` → `blocked` → `proposed` → `paused` → `done`; hide `dropped` unless toggled), each card showing title, repo, blockers, **Next step** (extraction rule in C1), and links: `spec`/`plan` used as-is when absolute URL, else `https://github.com/anthonysuherli/delapan-be/blob/master/<path>` (relative paths are always backend-repo paths — frontend docs use absolute URLs); `branch` links to `delapan-be` when `repo` is `backend` or `both`, and to `delapan-fe` when `repo` is `frontend`.
  4. Ordered backlog below.
- Path gate only — do **not** boot the engine store when serving `/tracking` (no dependency on the local API for this view).
- Hosting: same deploy as delapan.ai (existing frontend). Ensure the host serves SPA fallback for `/tracking` (or a static rewrite) so deep links work.
- Env: `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` (document in `.env.example`). Tracking tables are never readable without a valid session.

Read-only in v1 — no edit UI.

### C6 · Agent skill

Add `delapan-ai/skills/tracking/SKILL.md` in the plugin shell (same tree as the other delapan skills), installable into Cursor's skills path like explore/resume/backlog:

**When:** end of a coding session that touched product work, or when the user asks to update project status.

**Steps:**

1. Update frontmatter (`status`, `blocked_by`, `branch`, `updated`) and **Next step** prose for any initiative touched.
2. Add/strike/reorder lines in `backlog.md` as needed.
3. Run `python scripts/tracking_sync.py` from the backend repo root.
4. Commit markdown changes with the session (sync failure is **reported**, not swallowed — markdown still commits so git never lags the truth).

Skill does not invent initiatives from thin air; it updates what the session actually moved.

## Data flow

1. Engineer or agent edits `docs/tracking/**`.
2. Sync script validates and upserts to Supabase.
3. User opens `https://delapan.ai/tracking`, signs in with Supabase auth.
4. Frontend SELECTs rows under RLS; dashboard reflects markdown state.

Failure modes:

| Failure | Behavior |
|---|---|
| Invalid markdown | Sync exits non-zero; tables unchanged; agent reports error |
| Sync network/auth failure | Same; markdown commit may still land |
| Logged-out `/tracking` | Sign-in UI only; no data leak |
| Anon key alone | RLS denies SELECT |

## Error handling & testing

**Backend**

- Unit tests for parser → row dicts (initiatives + backlog tags).
- Unit tests for each validation failure mode.
- Dry-run contract test against fixture files under `tests/fixtures/tracking/`.

**Frontend**

- Vitest for row → view-model mapping (status grouping, next-step extraction, link building).
- Auth gate + live rendering verified manually against local or cloud Supabase.

**Acceptance criteria**

1. Edit an initiative's `status` in markdown → run sync → after login, `/tracking` shows the new status.
2. Logged-out visit to `/tracking` shows only sign-in; network tab shows no successful SELECT of tracking rows.
3. Sync with a dangling `blocked_by` exits non-zero and does not partially write.
4. Seeded initiatives + backlog cover current vision detours and in-flight canvas work; human can correct in one pass.

## Rollout

1. Land markdown layout + seed files + parser/validator + tests (backend).
2. Land migration; apply to local Supabase then cloud.
3. Land sync script; dry-run against seed; live sync once.
4. Create the solo GoTrue user (or use existing); verify RLS with anon vs authenticated.
5. Land frontend `/tracking` + env vars; deploy; verify SPA path.
6. Land agent skill; run one end-to-end session update.

## Non-goals (v1)

- Editing initiatives from the dashboard
- CI-triggered sync
- Fine-grained task tickets
- Public or org-wide multi-user project management
- Syncing br8n work
- Replacing TrueNorth vision/specs/plans
