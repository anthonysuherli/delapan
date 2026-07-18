# Solo Project Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a solo engineer a markdown source-of-truth for initiatives + backlog, synced to Supabase, and a private `delapan.ai/tracking` dashboard behind GoTrue.

**Architecture:** Parse `docs/tracking/` (one initiative file + ordered backlog) into validated row dicts; `scripts/tracking_sync.py` mirrors them into RLS-locked Supabase tables via the service-role client; the existing Vite/React frontend path-gates `/tracking` to a read-only view that signs in with GoTrue and SELECTs those tables. Agents keep markdown current via a session-end skill.

**Tech Stack:** Python 3.11+, PyYAML (already in backend), pytest, supabase-py `service_client()`, Vite/React 18, `@supabase/supabase-js`, Vitest, Supabase Postgres + GoTrue RLS.

**Spec:** `docs/superpowers/specs/2026-07-17-solo-project-tracker-design.md` — read it before Task 1.

## Global Constraints

- **Backend repo root:** `8star/delapan-ai/backend` (git remote `delapan-be`). Branch for this work: `docs/solo-project-tracker` (already holds the design spec) or continue on it / merge to `master` as directed. All backend paths below are relative to that root.
- **Frontend repo root:** `8star/delapan-ai/frontend` (git remote `delapan-fe`). Frontend tasks commit there on a matching branch `feat/tracking-route`.
- **Plugin skills:** `8star/delapan-ai/skills/` (sibling of `backend/`, not inside the backend git repo — skill file lands in the unversioned plugin shell; also copy/install into `~/.cursor/skills/delapan/tracking/` if that is how other delapan skills are exposed).
- House style (backend): `from __future__ import annotations`, type hints, terse module docstring with ASCII flow, ruff line-length 100.
- Run backend tests: `uv run pytest <path> -v`. Lint: `uv run ruff check delapan tests scripts`.
- Run frontend tests: `npm test` (vitest). Build: `npm run build`.
- **Markdown is the only write path.** Dashboard is read-only. Sync failures must not silently skip bad rows.
- **Do not** invent CI sync, dashboard edit UI, br8n tracking, or `/v1` tracking API.
- Commit after every task with the message in that task's final step. Backend and frontend commits stay in their own repos.

## File structure

| Path | Responsibility |
|---|---|
| `docs/tracking/backlog.md` | Ordered backlog source of truth |
| `docs/tracking/initiatives/<slug>.md` | One initiative per file (frontmatter + body) |
| `delapan/tracking/__init__.py` | Package exports |
| `delapan/tracking/models.py` | `InitiativeRow`, `BacklogItem` dataclasses |
| `delapan/tracking/parse.py` | Load + parse markdown → rows (no I/O to Supabase) |
| `delapan/tracking/validate.py` | Hard validation; raises `TrackingValidationError` |
| `scripts/tracking_sync.py` | CLI: parse → validate → upsert/delete/rewrite |
| `migrations/2026-07-17-tracking.sql` | Tables + RLS |
| `tests/fixtures/tracking/` | Fixture markdown for unit/dry-run tests |
| `tests/test_tracking_parse.py` | Parser tests |
| `tests/test_tracking_validate.py` | Validation tests |
| `tests/test_tracking_sync.py` | Dry-run + mirror logic with fake client |
| Frontend `src/tracking/viewModel.ts` | Pure: next-step extract, group, links |
| Frontend `src/tracking/viewModel.test.ts` | Vitest |
| Frontend `src/tracking/supabaseClient.ts` | Browser supabase client from Vite env |
| Frontend `src/tracking/TrackingApp.tsx` | Auth gate + data fetch + layout |
| Frontend `src/tracking/SignInForm.tsx` | Email/password only |
| Frontend `src/main.tsx` | Path gate `/tracking` |
| Frontend `vercel.json` | SPA rewrite for `/tracking` |
| `../skills/tracking/SKILL.md` | Agent session-end workflow |

---

### Task 1: Seed markdown + models + parser (TDD)

**Files:**
- Create: `docs/tracking/backlog.md`
- Create: `docs/tracking/initiatives/supabase-storage-unification.md`
- Create: `docs/tracking/initiatives/write-path-dedup.md`
- Create: `docs/tracking/initiatives/live-delta-stream.md`
- Create: `docs/tracking/initiatives/hitl-consequence-preview.md`
- Create: `docs/tracking/initiatives/canvas-v1.md`
- Create: `docs/tracking/initiatives/pluggable-retrieval.md`
- Create: `delapan/tracking/__init__.py`
- Create: `delapan/tracking/models.py`
- Create: `delapan/tracking/parse.py`
- Create: `tests/fixtures/tracking/initiatives/alpha.md`
- Create: `tests/fixtures/tracking/initiatives/beta.md`
- Create: `tests/fixtures/tracking/backlog.md`
- Test: `tests/test_tracking_parse.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `@dataclass InitiativeRow`: `slug: str`, `title: str`, `status: str`, `repo: str`, `blocked_by: list[str]`, `spec: str | None`, `plan: str | None`, `branch: str | None`, `updated: str`, `body_md: str`
  - `@dataclass BacklogItem`: `position: int`, `text: str`, `repo: str`, `initiative_slug: str | None`
  - `parse_initiative_file(path: Path) -> InitiativeRow`
  - `parse_backlog_file(path: Path) -> list[BacklogItem]`
  - `load_tracking_dir(root: Path) -> tuple[list[InitiativeRow], list[BacklogItem]]` where `root` contains `initiatives/` and `backlog.md`

- [ ] **Step 1: Write the failing parser tests**

Create `tests/test_tracking_parse.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from delapan.tracking.parse import (
    load_tracking_dir,
    parse_backlog_file,
    parse_initiative_file,
)

FIX = Path(__file__).parent / "fixtures" / "tracking"


def test_parse_initiative_frontmatter_and_body():
    row = parse_initiative_file(FIX / "initiatives" / "alpha.md")
    assert row.slug == "alpha"
    assert row.title == "Alpha"
    assert row.status == "active"
    assert row.repo == "backend"
    assert row.blocked_by == ["beta"]
    assert row.spec == "docs/superpowers/specs/example.md"
    assert row.plan is None
    assert row.branch == "feat/alpha"
    assert row.updated == "2026-07-17"
    assert "**Next step**" in row.body_md
    assert "Ship parser" in row.body_md


def test_parse_backlog_tags_and_positions():
    items = parse_backlog_file(FIX / "backlog.md")
    assert len(items) == 2
    assert items[0].position == 1
    assert items[0].text == "Do the first thing"
    assert items[0].repo == "backend"
    assert items[0].initiative_slug == "alpha"
    assert items[1].position == 2
    assert items[1].text == "Do the second thing"
    assert items[1].repo == "both"
    assert items[1].initiative_slug is None


def test_load_tracking_dir_collects_all():
    inits, backlog = load_tracking_dir(FIX)
    slugs = sorted(i.slug for i in inits)
    assert slugs == ["alpha", "beta"]
    assert len(backlog) == 2
```

- [ ] **Step 2: Write fixtures**

`tests/fixtures/tracking/initiatives/alpha.md`:

```markdown
---
title: Alpha
status: active
repo: backend
blocked_by: [beta]
spec: docs/superpowers/specs/example.md
plan: null
branch: feat/alpha
updated: 2026-07-17
---

Context for alpha.

**Next step**

Ship parser.
```

`tests/fixtures/tracking/initiatives/beta.md`:

```markdown
---
title: Beta
status: blocked
repo: both
blocked_by: []
spec: null
plan: null
branch: null
updated: 2026-07-16
---

Waiting on Alpha.
```

`tests/fixtures/tracking/backlog.md`:

```markdown
# Backlog

- Do the first thing [backend] [initiative:alpha]
- Do the second thing
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /Users/anthonysuherli/Repositories/8star/delapan-ai/backend
uv run pytest tests/test_tracking_parse.py -v
```

Expected: FAIL with `ModuleNotFoundError` or `ImportError` for `delapan.tracking`.

- [ ] **Step 4: Implement models + parser**

`delapan/tracking/models.py`:

```python
"""Tracking row shapes — mirror of docs/tracking markdown + Supabase tables."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InitiativeRow:
    slug: str
    title: str
    status: str
    repo: str
    blocked_by: list[str] = field(default_factory=list)
    spec: str | None = None
    plan: str | None = None
    branch: str | None = None
    updated: str = ""
    body_md: str = ""


@dataclass
class BacklogItem:
    position: int
    text: str
    repo: str = "both"
    initiative_slug: str | None = None
```

`delapan/tracking/parse.py`:

```python
"""Parse docs/tracking markdown into row dataclasses.

    initiatives/*.md ──► InitiativeRow
    backlog.md       ──► list[BacklogItem]  (position = list order)
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from delapan.tracking.models import BacklogItem, InitiativeRow

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_REPO_TAG = re.compile(r"\[(backend|frontend|both)\]")
_INIT_TAG = re.compile(r"\[initiative:([a-z0-9-]+)\]")


def _nullish(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "null", "none"}:
        return None
    return str(value)


def parse_initiative_file(path: Path) -> InitiativeRow:
    raw = path.read_text(encoding="utf-8")
    m = _FRONTMATTER.match(raw)
    if not m:
        raise ValueError(f"{path}: missing YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: frontmatter must be a mapping")
    blocked = meta.get("blocked_by") or []
    if not isinstance(blocked, list):
        raise ValueError(f"{path}: blocked_by must be a list")
    return InitiativeRow(
        slug=path.stem,
        title=str(meta.get("title") or ""),
        status=str(meta.get("status") or ""),
        repo=str(meta.get("repo") or ""),
        blocked_by=[str(s) for s in blocked],
        spec=_nullish(meta.get("spec")),
        plan=_nullish(meta.get("plan")),
        branch=_nullish(meta.get("branch")),
        updated=str(meta.get("updated") or ""),
        body_md=m.group(2).strip() + ("\n" if m.group(2).strip() else ""),
    )


def parse_backlog_file(path: Path) -> list[BacklogItem]:
    items: list[BacklogItem] = []
    position = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        position += 1
        payload = stripped[2:].strip()
        repo_m = _REPO_TAG.search(payload)
        init_m = _INIT_TAG.search(payload)
        text = _REPO_TAG.sub("", payload)
        text = _INIT_TAG.sub("", text).strip()
        text = re.sub(r"\s+", " ", text).strip()
        items.append(
            BacklogItem(
                position=position,
                text=text,
                repo=repo_m.group(1) if repo_m else "both",
                initiative_slug=init_m.group(1) if init_m else None,
            )
        )
    return items


def load_tracking_dir(root: Path) -> tuple[list[InitiativeRow], list[BacklogItem]]:
    init_dir = root / "initiatives"
    initiatives = [
        parse_initiative_file(p)
        for p in sorted(init_dir.glob("*.md"))
        if p.is_file()
    ]
    backlog = parse_backlog_file(root / "backlog.md")
    return initiatives, backlog
```

`delapan/tracking/__init__.py`:

```python
"""Markdown project-tracking layer (initiatives + backlog)."""

from delapan.tracking.models import BacklogItem, InitiativeRow
from delapan.tracking.parse import load_tracking_dir, parse_backlog_file, parse_initiative_file

__all__ = [
    "BacklogItem",
    "InitiativeRow",
    "load_tracking_dir",
    "parse_backlog_file",
    "parse_initiative_file",
]
```

- [ ] **Step 5: Run parser tests — expect PASS**

```bash
uv run pytest tests/test_tracking_parse.py -v
```

Expected: all PASS.

- [ ] **Step 6: Seed real `docs/tracking/` from current vision**

Create these initiative files (adjust prose if reality drifted; keep frontmatter exact):

`docs/tracking/initiatives/write-path-dedup.md` — `status: done`, `repo: backend`, links to `docs/superpowers/specs/2026-07-16-write-path-dedup-design.md` and matching plan.

`docs/tracking/initiatives/supabase-storage-unification.md` — `status: blocked`, note vision amendment (SQLite kept; End Goal 0 stale).

`docs/tracking/initiatives/canvas-v1.md` — `status: active`, `repo: both`, link plan/design under `docs/plans/` or `docs/superpowers/` as they exist on the branch; `branch: docs/canvas-v1-design` if still accurate.

`docs/tracking/initiatives/live-delta-stream.md` — `status: proposed`, `repo: both`.

`docs/tracking/initiatives/hitl-consequence-preview.md` — `status: proposed`, `repo: frontend`, `blocked_by: [live-delta-stream]`.

`docs/tracking/initiatives/pluggable-retrieval.md` — `status: proposed`, `repo: backend`.

`docs/tracking/backlog.md` — at least 3 ordered items tagged sensibly (e.g. revisit storage End Goal, OrbStack local Supabase, canvas phase-1).

Every seed file must include a `**Next step**` section.

- [ ] **Step 7: Commit (backend)**

```bash
git add docs/tracking delapan/tracking tests/test_tracking_parse.py tests/fixtures/tracking
git commit -m "$(cat <<'EOF'
feat(tracking): seed markdown SoT + parse initiatives/backlog

EOF
)"
```

---

### Task 2: Hard validation (TDD)

**Files:**
- Create: `delapan/tracking/validate.py`
- Test: `tests/test_tracking_validate.py`
- Modify: `delapan/tracking/__init__.py` (export validator)

**Interfaces:**
- Consumes: `InitiativeRow`, `BacklogItem`, `load_tracking_dir`
- Produces:
  - `class TrackingValidationError(ValueError)` with `.errors: list[str]`
  - `validate_tracking(initiatives: list[InitiativeRow], backlog: list[BacklogItem], *, repo_root: Path) -> None` — raises `TrackingValidationError` if any issue
  - Allowed statuses: `proposed|active|blocked|paused|done|dropped`
  - Allowed repos: `backend|frontend|both`
  - Relative `spec`/`plan` (not starting with `https://`) must exist under `repo_root`
  - Every `blocked_by` slug must exist in the initiative set
  - Duplicate slugs → error
  - Missing required fields (`title`, `status`, `repo`, `updated`) → error

- [ ] **Step 1: Write failing validation tests**

```python
from __future__ import annotations

from pathlib import Path

import pytest

from delapan.tracking.models import BacklogItem, InitiativeRow
from delapan.tracking.validate import TrackingValidationError, validate_tracking

ROOT = Path(__file__).resolve().parents[1]  # backend repo root


def _init(**kwargs) -> InitiativeRow:
    base = dict(
        slug="alpha",
        title="Alpha",
        status="active",
        repo="backend",
        blocked_by=[],
        spec=None,
        plan=None,
        branch=None,
        updated="2026-07-17",
        body_md="x",
    )
    base.update(kwargs)
    return InitiativeRow(**base)


def test_ok_with_empty_backlog():
    validate_tracking([_init()], [], repo_root=ROOT)


def test_unknown_status_fails():
    with pytest.raises(TrackingValidationError) as ei:
        validate_tracking([_init(status="shipping")], [], repo_root=ROOT)
    assert any("status" in e for e in ei.value.errors)


def test_dangling_blocked_by_fails():
    with pytest.raises(TrackingValidationError) as ei:
        validate_tracking([_init(blocked_by=["nope"])], [], repo_root=ROOT)
    assert any("blocked_by" in e for e in ei.value.errors)


def test_missing_relative_spec_fails():
    with pytest.raises(TrackingValidationError) as ei:
        validate_tracking(
            [_init(spec="docs/does-not-exist.md")],
            [],
            repo_root=ROOT,
        )
    assert any("spec" in e for e in ei.value.errors)


def test_https_spec_skips_disk_check():
    validate_tracking(
        [_init(spec="https://example.com/spec.md")],
        [],
        repo_root=ROOT,
    )


def test_duplicate_slugs_fail():
    with pytest.raises(TrackingValidationError) as ei:
        validate_tracking([_init(), _init()], [], repo_root=ROOT)
    assert any("duplicate" in e.lower() for e in ei.value.errors)
```

- [ ] **Step 2: Run — expect FAIL**

```bash
uv run pytest tests/test_tracking_validate.py -v
```

Expected: `ImportError` for `delapan.tracking.validate`.

- [ ] **Step 3: Implement `validate.py`**

```python
"""Hard validation for tracking rows — never skip; collect all errors then raise."""

from __future__ import annotations

from pathlib import Path

from delapan.tracking.models import BacklogItem, InitiativeRow

STATUSES = frozenset({"proposed", "active", "blocked", "paused", "done", "dropped"})
REPOS = frozenset({"backend", "frontend", "both"})


class TrackingValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _is_url(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")


def validate_tracking(
    initiatives: list[InitiativeRow],
    backlog: list[BacklogItem],
    *,
    repo_root: Path,
) -> None:
    errors: list[str] = []
    slugs = [i.slug for i in initiatives]
    if len(slugs) != len(set(slugs)):
        errors.append("duplicate initiative slug")
    slug_set = set(slugs)

    for i in initiatives:
        if not i.title.strip():
            errors.append(f"{i.slug}: missing title")
        if i.status not in STATUSES:
            errors.append(f"{i.slug}: unknown status {i.status!r}")
        if i.repo not in REPOS:
            errors.append(f"{i.slug}: unknown repo {i.repo!r}")
        if not i.updated.strip():
            errors.append(f"{i.slug}: missing updated")
        for b in i.blocked_by:
            if b not in slug_set:
                errors.append(f"{i.slug}: blocked_by unknown slug {b!r}")
        for field, value in (("spec", i.spec), ("plan", i.plan)):
            if value is None:
                continue
            if _is_url(value):
                continue
            if not (repo_root / value).is_file():
                errors.append(f"{i.slug}: {field} path missing: {value}")

    for item in backlog:
        if item.repo not in REPOS:
            errors.append(f"backlog#{item.position}: unknown repo {item.repo!r}")
        if item.initiative_slug and item.initiative_slug not in slug_set:
            errors.append(
                f"backlog#{item.position}: unknown initiative {item.initiative_slug!r}"
            )

    if errors:
        raise TrackingValidationError(errors)
```

Export from `__init__.py`.

- [ ] **Step 4: Run — expect PASS**

```bash
uv run pytest tests/test_tracking_validate.py tests/test_tracking_parse.py -v
```

- [ ] **Step 5: Validate the real seed directory**

```bash
uv run python -c "
from pathlib import Path
from delapan.tracking import load_tracking_dir
from delapan.tracking.validate import validate_tracking
root = Path('docs/tracking')
repo = Path('.')
i, b = load_tracking_dir(root)
validate_tracking(i, b, repo_root=repo)
print(f'ok: {len(i)} initiatives, {len(b)} backlog')
"
```

Expected: `ok: …`. If seed paths are wrong, fix the markdown (not the validator).

- [ ] **Step 6: Commit**

```bash
git add delapan/tracking/validate.py delapan/tracking/__init__.py tests/test_tracking_validate.py docs/tracking
git commit -m "$(cat <<'EOF'
feat(tracking): hard-validate initiatives and backlog before sync

EOF
)"
```

---

### Task 3: Supabase migration + RLS

**Files:**
- Create: `migrations/2026-07-17-tracking.sql`

**Interfaces:**
- Consumes: nothing in Python.
- Produces: tables `tracking_initiatives`, `tracking_backlog` with RLS SELECT for `authenticated` only.

- [ ] **Step 1: Write the migration**

`migrations/2026-07-17-tracking.sql`:

```sql
-- Solo project tracker: markdown-synced initiatives + backlog.
-- Spec: docs/superpowers/specs/2026-07-17-solo-project-tracker-design.md (§C3)
-- Writes: service role only (bypasses RLS). Reads: authenticated SELECT.

create table if not exists tracking_initiatives (
  slug text primary key,
  title text not null,
  status text not null check (status in ('proposed','active','blocked','paused','done','dropped')),
  repo text not null check (repo in ('backend','frontend','both')),
  blocked_by text[] not null default '{}',
  spec text,
  plan text,
  branch text,
  body_md text not null default '',
  updated date not null,
  synced_at timestamptz not null default now()
);

create table if not exists tracking_backlog (
  position int primary key,
  text text not null,
  repo text not null check (repo in ('backend','frontend','both')),
  initiative_slug text,
  synced_at timestamptz not null default now()
);

alter table tracking_initiatives enable row level security;
alter table tracking_backlog enable row level security;

-- Drop-if-exists so re-apply is idempotent in local stacks.
drop policy if exists tracking_initiatives_select_authenticated on tracking_initiatives;
create policy tracking_initiatives_select_authenticated
  on tracking_initiatives for select
  to authenticated
  using (true);

drop policy if exists tracking_backlog_select_authenticated on tracking_backlog;
create policy tracking_backlog_select_authenticated
  on tracking_backlog for select
  to authenticated
  using (true);

-- Intentionally no insert/update/delete policies for authenticated/anon.
```

- [ ] **Step 2: Apply to local Supabase (OrbStack) if available**

```bash
# If supabase CLI is linked to local:
supabase db query --local < migrations/2026-07-17-tracking.sql
# Or paste into Studio SQL editor for local + cloud projects.
```

If local stack is not running, still commit the migration file; applying to cloud is a manual gate before Task 5 live demo. Record which project(s) were applied in the commit message body if applied.

- [ ] **Step 3: Commit**

```bash
git add migrations/2026-07-17-tracking.sql
git commit -m "$(cat <<'EOF'
feat(migrations): tracking_initiatives + tracking_backlog with authenticated RLS

EOF
)"
```

---

### Task 4: Sync script (TDD + dry-run)

**Files:**
- Create: `scripts/tracking_sync.py`
- Create: `delapan/tracking/sync.py` (pure mirror plan + apply helpers — keeps script thin)
- Test: `tests/test_tracking_sync.py`

**Interfaces:**
- Consumes: `load_tracking_dir`, `validate_tracking`, `service_client()`
- Produces:
  - `@dataclass SyncPlan`: `upserts: list[InitiativeRow]`, `delete_slugs: list[str]`, `backlog: list[BacklogItem]`
  - `plan_sync(local: list[InitiativeRow], remote_slugs: set[str], backlog: list[BacklogItem]) -> SyncPlan`
  - `apply_sync(client, plan: SyncPlan, *, dry_run: bool) -> None` — uses `client.table(...)`
  - CLI: `uv run python scripts/tracking_sync.py [--dry-run] [--root docs/tracking]`

- [ ] **Step 1: Write failing sync-plan tests**

```python
from __future__ import annotations

from delapan.tracking.models import BacklogItem, InitiativeRow
from delapan.tracking.sync import SyncPlan, plan_sync


def _i(slug: str) -> InitiativeRow:
    return InitiativeRow(
        slug=slug, title=slug, status="active", repo="backend",
        updated="2026-07-17", body_md="x",
    )


def test_plan_upserts_all_and_deletes_orphans():
    plan = plan_sync(
        local=[_i("a"), _i("b")],
        remote_slugs={"b", "c"},
        backlog=[BacklogItem(1, "x", "both", None)],
    )
    assert {i.slug for i in plan.upserts} == {"a", "b"}
    assert plan.delete_slugs == ["c"]
    assert len(plan.backlog) == 1
```

- [ ] **Step 2: Run — expect FAIL**

```bash
uv run pytest tests/test_tracking_sync.py -v
```

- [ ] **Step 3: Implement `delapan/tracking/sync.py`**

```python
"""Plan and apply a markdown → Supabase mirror.

    local files ──► validate ──► SyncPlan ──► upsert / delete / rewrite backlog
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from delapan.tracking.models import BacklogItem, InitiativeRow


@dataclass
class SyncPlan:
    upserts: list[InitiativeRow]
    delete_slugs: list[str]
    backlog: list[BacklogItem]


class TrackingTableClient(Protocol):
    def table(self, name: str) -> Any: ...


def plan_sync(
    local: list[InitiativeRow],
    remote_slugs: set[str],
    backlog: list[BacklogItem],
) -> SyncPlan:
    local_slugs = {i.slug for i in local}
    return SyncPlan(
        upserts=list(local),
        delete_slugs=sorted(remote_slugs - local_slugs),
        backlog=list(backlog),
    )


def _initiative_payload(row: InitiativeRow) -> dict[str, Any]:
    return {
        "slug": row.slug,
        "title": row.title,
        "status": row.status,
        "repo": row.repo,
        "blocked_by": row.blocked_by,
        "spec": row.spec,
        "plan": row.plan,
        "branch": row.branch,
        "body_md": row.body_md,
        "updated": row.updated,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def apply_sync(client: TrackingTableClient, plan: SyncPlan, *, dry_run: bool) -> None:
    if dry_run:
        print(f"dry-run: upsert {len(plan.upserts)} initiatives")
        print(f"dry-run: delete {plan.delete_slugs}")
        print(f"dry-run: rewrite backlog ({len(plan.backlog)} items)")
        return

    if plan.upserts:
        client.table("tracking_initiatives").upsert(
            [_initiative_payload(r) for r in plan.upserts]
        ).execute()
    for slug in plan.delete_slugs:
        client.table("tracking_initiatives").delete().eq("slug", slug).execute()

    # Rewrite backlog: delete every row, then insert current list.
    client.table("tracking_backlog").delete().gte("position", 1).execute()
    if plan.backlog:
        now = datetime.now(timezone.utc).isoformat()
        client.table("tracking_backlog").insert(
            [
                {
                    "position": b.position,
                    "text": b.text,
                    "repo": b.repo,
                    "initiative_slug": b.initiative_slug,
                    "synced_at": now,
                }
                for b in plan.backlog
            ]
        ).execute()
```

Note: verify the delete-all idiom against supabase-py in this repo; if needed, select all positions then delete by list. Prefer whatever works with zero leftover rows.

- [ ] **Step 4: Implement CLI `scripts/tracking_sync.py`**

Follow `scripts/dedup_backfill.py` style (argparse, module docstring). Outline:

```python
"""Mirror docs/tracking → Supabase tracking_* tables.

    uv run python scripts/tracking_sync.py           # apply
    uv run python scripts/tracking_sync.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from delapan.core.clients.supabase import service_client
from delapan.tracking.parse import load_tracking_dir
from delapan.tracking.sync import apply_sync, plan_sync
from delapan.tracking.validate import TrackingValidationError, validate_tracking


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("docs/tracking"))
    p.add_argument("--repo-root", type=Path, default=Path("."))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    initiatives, backlog = load_tracking_dir(args.root)
    try:
        validate_tracking(initiatives, backlog, repo_root=args.repo_root)
    except TrackingValidationError as e:
        for err in e.errors:
            print(f"error: {err}", file=sys.stderr)
        return 1

    client = service_client()
    remote = client.table("tracking_initiatives").select("slug").execute()
    remote_slugs = {row["slug"] for row in (remote.data or [])}
    plan = plan_sync(initiatives, remote_slugs, backlog)
    apply_sync(client, plan, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

For unit tests of `apply_sync` dry-run, no network. Optionally add a fake client test that records upsert/delete/insert calls.

- [ ] **Step 5: Run unit tests**

```bash
uv run pytest tests/test_tracking_sync.py tests/test_tracking_validate.py -v
uv run ruff check delapan/tracking scripts/tracking_sync.py
```

- [ ] **Step 6: Dry-run against real seed (needs env)**

```bash
# Requires SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in backend/.env
uv run python scripts/tracking_sync.py --dry-run
```

Expected: prints upsert/delete/backlog counts; exit 0. Then without `--dry-run` once migration is applied.

- [ ] **Step 7: Commit**

```bash
git add delapan/tracking/sync.py scripts/tracking_sync.py tests/test_tracking_sync.py
git commit -m "$(cat <<'EOF'
feat(tracking): sync script mirrors markdown into Supabase tables

EOF
)"
```

---

### Task 5: Frontend view-model + `/tracking` route (frontend repo)

**Files (all under `8star/delapan-ai/frontend`):**
- Create: `src/tracking/types.ts`
- Create: `src/tracking/viewModel.ts`
- Create: `src/tracking/viewModel.test.ts`
- Create: `src/tracking/supabaseClient.ts`
- Create: `src/tracking/SignInForm.tsx`
- Create: `src/tracking/TrackingApp.tsx`
- Create: `src/styles/tracking.css`
- Create: `vercel.json`
- Modify: `src/main.tsx`
- Modify: `.env.example`
- Modify: `package.json` (add `@supabase/supabase-js`)

**Interfaces:**
- Consumes: Supabase rows shaped like Task 3 columns.
- Produces:
  - `extractNextStep(bodyMd: string): string`
  - `groupInitiatives(rows, { showDropped: boolean }): { status: string; items: ViewInitiative[] }[]`
  - `linkForSpecOrPlan(value: string | null): string | null`
  - `branchUrl(repo: string, branch: string | null): string | null`
  - Status order: `active`, `blocked`, `proposed`, `paused`, `done` (+ `dropped` if toggled)

- [ ] **Step 1: Branch + install dependency**

```bash
cd /Users/anthonysuherli/Repositories/8star/delapan-ai/frontend
git checkout main && git pull
git checkout -b feat/tracking-route
npm install @supabase/supabase-js
```

- [ ] **Step 2: Write failing view-model tests**

`src/tracking/viewModel.test.ts`:

```typescript
import { describe, expect, it } from "vitest";
import {
  branchUrl,
  extractNextStep,
  groupInitiatives,
  linkForSpecOrPlan,
} from "./viewModel";
import type { InitiativeRow } from "./types";

const row = (partial: Partial<InitiativeRow> & Pick<InitiativeRow, "slug" | "status">): InitiativeRow => ({
  title: partial.title ?? partial.slug,
  repo: partial.repo ?? "backend",
  blocked_by: partial.blocked_by ?? [],
  spec: partial.spec ?? null,
  plan: partial.plan ?? null,
  branch: partial.branch ?? null,
  body_md: partial.body_md ?? "",
  updated: partial.updated ?? "2026-07-17",
  ...partial,
});

describe("extractNextStep", () => {
  it("reads **Next step** block", () => {
    const body = "Intro.\n\n**Next step**\n\nDo the thing.\n";
    expect(extractNextStep(body)).toBe("Do the thing.");
  });

  it("falls back to first paragraph", () => {
    expect(extractNextStep("Only para.\n\nSecond.")).toBe("Only para.");
  });
});

describe("linkForSpecOrPlan", () => {
  it("passes through https URLs", () => {
    expect(linkForSpecOrPlan("https://example.com/a")).toBe("https://example.com/a");
  });

  it("maps relative paths to delapan-be", () => {
    expect(linkForSpecOrPlan("docs/x.md")).toBe(
      "https://github.com/anthonysuherli/delapan-be/blob/master/docs/x.md",
    );
  });
});

describe("branchUrl", () => {
  it("uses fe repo for frontend", () => {
    expect(branchUrl("frontend", "feat/x")).toBe(
      "https://github.com/anthonysuherli/delapan-fe/tree/feat/x",
    );
  });

  it("uses be repo for backend/both", () => {
    expect(branchUrl("both", "feat/x")).toContain("delapan-be");
  });
});

describe("groupInitiatives", () => {
  it("orders statuses and hides dropped by default", () => {
    const groups = groupInitiatives(
      [
        row({ slug: "d", status: "done" }),
        row({ slug: "a", status: "active" }),
        row({ slug: "x", status: "dropped" }),
        row({ slug: "b", status: "blocked" }),
      ],
      { showDropped: false },
    );
    expect(groups.map((g) => g.status)).toEqual(["active", "blocked", "done"]);
    expect(groups.flatMap((g) => g.items.map((i) => i.slug))).not.toContain("x");
  });
});
```

- [ ] **Step 3: Run — expect FAIL**

```bash
npm test -- src/tracking/viewModel.test.ts
```

- [ ] **Step 4: Implement `types.ts` + `viewModel.ts`**

Implement `extractNextStep` to match the spec: prefer `**Next step**` or `## Next step`, else first non-empty paragraph.

Implement grouping with fixed order `["active","blocked","proposed","paused","done","dropped"]`.

- [ ] **Step 5: Run — expect PASS**

```bash
npm test -- src/tracking/viewModel.test.ts
```

- [ ] **Step 6: Supabase client + SignIn + TrackingApp**

`src/tracking/supabaseClient.ts` — `createClient(import.meta.env.VITE_SUPABASE_URL, import.meta.env.VITE_SUPABASE_ANON_KEY)`.

`SignInForm.tsx` — email/password → `supabase.auth.signInWithPassword`; no sign-up link.

`TrackingApp.tsx`:
1. `onAuthStateChange` / `getSession`
2. If no session → `SignInForm`
3. Else fetch both tables ordered (`updated` desc / `position` asc)
4. Render grouped initiatives + backlog using view-model helpers
5. Sign-out button

`src/styles/tracking.css` — minimal layout using existing tokens from `styles/tokens.css` (no new purple/glow aesthetic; match control panel neutrals).

- [ ] **Step 7: Path gate in `main.tsx`**

Replace the root render with:

```tsx
import { createRoot } from "react-dom/client";
import App from "./App";
import { TrackingApp } from "./tracking/TrackingApp";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/layout.css";
import "./styles/panels.css";
import "./styles/canvas.css";
import "./styles/motion.css";
import "./styles/tracking.css";

const path = window.location.pathname.replace(/\/$/, "") || "/";
const root = createRoot(document.getElementById("root")!);
root.render(path === "/tracking" ? <TrackingApp /> : <App />);
```

Critical: `/tracking` must **not** call `useStore().boot()` — `TrackingApp` is a separate tree.

- [ ] **Step 8: Env + SPA rewrite**

`.env.example` append:

```
# Private /tracking route (Supabase GoTrue + tracking_* tables)
VITE_SUPABASE_URL=
VITE_SUPABASE_ANON_KEY=
```

`vercel.json`:

```json
{
  "rewrites": [{ "source": "/tracking", "destination": "/index.html" }]
}
```

If hosting is not Vercel, still land this file as the documented SPA fallback; adjust for the real host when deploying.

- [ ] **Step 9: Build + test**

```bash
npm test
npm run build
```

Expected: pass + clean build.

- [ ] **Step 10: Commit (frontend)**

```bash
git add package.json package-lock.json src/tracking src/main.tsx src/styles/tracking.css .env.example vercel.json
git commit -m "$(cat <<'EOF'
feat(tracking): private /tracking route with GoTrue and read-only dashboard

EOF
)"
```

- [ ] **Step 11: Manual acceptance (after migration + sync applied)**

1. Set `VITE_SUPABASE_*` in `.env.local`; `npm run dev`; open `/tracking` logged out → only sign-in; confirm Network has no successful SELECT of `tracking_*`.
2. Sign in with the solo GoTrue user → initiatives + backlog render.
3. Flip an initiative `status` in markdown → `uv run python scripts/tracking_sync.py` in backend → refresh → UI updates.

---

### Task 6: Agent skill + README pointer

**Files:**
- Create: `../skills/tracking/SKILL.md` (path relative to backend: `delapan-ai/skills/tracking/SKILL.md`)
- Optionally install copy: `~/.cursor/skills/delapan/tracking/SKILL.md`
- Modify: `README.md` (backend) — one row in "What's inside" or a short "Project tracking" note linking the spec + sync command

**Interfaces:**
- Consumes: markdown layout + `scripts/tracking_sync.py`
- Produces: skill workflow agents follow at session end

- [ ] **Step 1: Write the skill**

`delapan-ai/skills/tracking/SKILL.md`:

```markdown
---
name: delapan-tracking
description: Update the solo project tracker (docs/tracking initiatives + backlog) at session end and sync to Supabase for delapan.ai/tracking. Use when finishing product work or when the user asks for project status.
---

# Delapan Tracking

Markdown in `backend/docs/tracking/` is the source of truth for initiatives and the prioritized backlog. The private dashboard at `/tracking` reads the Supabase mirror.

## Target

- Repo: `delapan-ai/backend` (delapan-be)
- Files: `docs/tracking/initiatives/<slug>.md`, `docs/tracking/backlog.md`

## Workflow

1. Identify which initiatives this session actually moved (do not invent new ones).
2. Update frontmatter: `status`, `blocked_by`, `branch`, `updated` (today's date).
3. Update the `**Next step**` section to the concrete next action.
4. Add, remove, or reorder lines in `docs/tracking/backlog.md` as needed.
5. From the backend repo root run:

```bash
uv run python scripts/tracking_sync.py
```

6. If sync fails, report the errors; still commit the markdown so git stays ahead of the mirror.
7. Commit tracking markdown with the session's other changes when the user wants a commit.

## Rules

- One write path: markdown only (no dashboard edits).
- Hard validation: fix dangling `blocked_by` / missing spec paths before re-running sync.
- Do not track br8n or fine-grained tickets here.
```

Copy into `~/.cursor/skills/delapan/tracking/SKILL.md` so Cursor discovers it like the other delapan skills.

- [ ] **Step 2: README note (backend)**

Add a short subsection or table row pointing at `docs/tracking/`, the sync command, and the design spec. Do not rewrite the README.

- [ ] **Step 3: Commit skill is outside backend git** — commit only the README change in backend:

```bash
cd /Users/anthonysuherli/Repositories/8star/delapan-ai/backend
git add README.md
git commit -m "$(cat <<'EOF'
docs: point at docs/tracking solo project tracker

EOF
)"
```

Note in the PR/summary that `skills/tracking/SKILL.md` lives in the plugin shell and may need separate versioning if/when that shell is git-tracked.

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|---|---|
| C1 markdown layout + frontmatter | Task 1 |
| C2 seeding | Task 1 Step 6 |
| C3 migration + RLS | Task 3 |
| C4 sync script + dry-run + hard fail | Task 2 + 4 |
| C5 `/tracking` + GoTrue + no engine boot | Task 5 |
| C6 agent skill | Task 6 |
| Parser/validator/dry-run tests | Tasks 1, 2, 4 |
| Frontend view-model vitest | Task 5 |
| Acceptance: sync→UI, logged-out leak, dangling blocked_by | Tasks 2, 4, 5 Step 11 |
| Non-goals respected | Global Constraints |

No TBD placeholders remain. Types (`InitiativeRow`, `BacklogItem`, `SyncPlan`) are consistent across tasks.
