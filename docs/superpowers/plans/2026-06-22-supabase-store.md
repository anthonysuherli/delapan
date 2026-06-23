# SupabaseStore — cloud-tier Store — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `SupabaseStore` so the delapan engine + frontend serve cloud KBs (starting with the ported `actuary` project) from Supabase, with no engine call-site changes.

**Architecture:** A single `delapan/store/supabase.py` implements the full `Store` protocol via the synchronous `supabase-py` client bound to a user JWT (RLS does org isolation); vector search calls the already-deployed `match_findings`/`match_kg_nodes` RPCs; embeddings are inline `vector(1536)` columns written as bracketed strings. A `delapan/core/clients/supabase.py` factory builds service-role and user-scoped clients; `scripts/seed_dev.py` provisions the MCP user. Async protocol methods wrap the sync client in `asyncio.to_thread`.

**Tech Stack:** Python 3.12, `supabase-py` (PostgREST + GoTrue), pytest, pytest-asyncio.

**Spec:** `backend/docs/superpowers/specs/2026-06-22-supabase-store-cloud-tier-design.md`

## Global Constraints

- **All commands run from `backend/`** (the delapan git repo, branch `feat/supabase-store`). Use `.venv/bin/python` / `.venv/bin/pytest` / `.venv/bin/ruff`.
- **Embeddings** are sent to Postgres as a bracketed string `"[f1,…,f1536]"` — exactly 1536 floats. Never a Python list, never a JSON array, never the SQLite binary blob. Helper: `_vec(list) -> str`.
- **`org_id` is `NOT NULL`** on every table — every INSERT the store issues must include `self._org_id`. Reads never add an explicit `org_id` filter (the user JWT + RLS scope to the org); they filter by `kb_id` only.
- **Vector RPCs** are called as `client.rpc("match_findings"/"match_kg_nodes", {"query_embedding": _vec(emb), "match_kb_id": kb_id, "match_count": n, "min_similarity": s})`. The kb param is **`match_kb_id`** (not `kb_id`). RPCs return a trimmed projection — `match_findings` → `{id,title,content,category,confidence,tags,provenance,similarity}`; `match_kg_nodes` → `{id,type,label,properties,similarity}`.
- **JSON/array columns:** `tags`/`aliases` are `text[]` (send Python lists); `provenance`/`properties`/`grounded_in`/`merge_history` are `jsonb` (send dicts/lists). Do **not** `json.dumps` — PostgREST coerces.
- **Cloud-only required columns filled on write:** `findings.status="approved"`; `kg_nodes.aliases=[]`, `kg_nodes.merge_history=[]`.
- **Return-shape parity is load-bearing:** every method returns the exact same dict/list shape `SQLiteStore` returns (same keys, JSON already decoded by PostgREST) so the engine can't tell the tiers apart. Mirror `delapan/store/sqlite.py`.
- **Not-found contract:** `get_finding`/`get_finding_global` raise `RuntimeError("finding not found")` on empty; `get_kg_node`/`load_synopsis`/`get_exploration`/`get_kg_intent` return `None`.
- **`record_access` is best-effort — must never raise.**
- **`supabase` is a lazy, cloud-only import** (module-level inside `core/clients/supabase.py`, never imported by the local tier). Style: `from __future__ import annotations`, type hints, ruff line-length 100.
- **`_MAX_GROUNDED = 50`** (grounded_in cap, mirrors SQLite). **`LIST_DEFAULT_LIMIT = 20`, `LIST_MAX_LIMIT = 100`** (findings); node list default 50 / max 500.
- **`DELAPAN_BACKEND` stays `local`** until the final task verifies end-to-end.

---

### Task 1: RLS verification gate (no app code)

The whole user-JWT design assumes RLS is enabled with `org_id`-scoped policies that an org member satisfies. Verify before building. **If this fails, stop and escalate** — the auth model (or the policies) must be fixed first.

**Files:** none (investigation + a committed findings note).

- [ ] **Step 1: Run the RLS posture query**

In the Supabase SQL editor (https://supabase.com/dashboard/project/gunqbyddzuwzpncfigro/sql/new) — or via the Supabase MCP `execute_sql` if reconnected — run:

```sql
select relname, relrowsecurity, relforcerowsecurity
from pg_class
where relname in ('findings','kg_nodes','kg_edges','kbs','projects','kb_synopsis');

select tablename, policyname, cmd, qual, with_check
from pg_policies
where tablename in ('findings','kg_nodes','kg_edges','kbs','projects','kb_synopsis')
order by tablename, policyname;
```

Expected: `relrowsecurity = true` for each table, and at least one `SELECT` policy per table whose `qual` scopes by `org_id` (typically `org_id in (select org_id from org_members where user_id = auth.uid())`).

- [ ] **Step 2: Confirm the actuary org has the policy path**

```sql
select om.user_id, om.role
from org_members om
where om.org_id = '1a7d0aa5-587f-4420-985b-bafcf03bf04f';
```

Expected: at least one member (the build's MCP user must become one of these, or be added — Task 8).

- [ ] **Step 3: Record the verdict**

Write `backend/docs/superpowers/plans/rls-verification.md` with the query outputs and a one-line verdict: **RLS-OK** (enabled + org-scoped policies present) or **RLS-BLOCKED** (with what's missing). Commit it.

```bash
git add docs/superpowers/plans/rls-verification.md
git commit -m "chore(store): record cloud RLS verification verdict"
```

Expected: verdict is **RLS-OK** before proceeding. If **RLS-BLOCKED**, escalate to the human — do not continue.

---

### Task 2: `[cloud]` extra + client factory

**Files:**
- Modify: `backend/` venv (install the extra)
- Create: `backend/delapan/core/clients/supabase.py`
- Test: `backend/tests/test_clients_supabase.py`

**Interfaces:**
- Produces:
  - `service_client() -> Client` — service-role client (bypasses RLS), reads `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` from `get_settings()`.
  - `user_client(access_token: str) -> Client` — anon-key client with the user JWT applied (RLS-scoped), reads `SUPABASE_URL` + `SUPABASE_ANON_KEY`.

- [ ] **Step 1: Install the cloud extra**

Run: `(cd backend && .venv/bin/pip install -e ".[cloud]")`
Expected: installs `supabase`, `asyncpg`, `pyjwt`, `argon2-cffi`. Verify: `.venv/bin/python -c "import supabase; print(supabase.__version__)"` prints a version.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_clients_supabase.py`:

```python
from types import SimpleNamespace

from delapan.core.clients import supabase as sb


def test_service_client_uses_service_role_key(monkeypatch):
    captured = {}

    def fake_create_client(url, key):
        captured["url"], captured["key"] = url, key
        return SimpleNamespace(url=url, key=key)

    monkeypatch.setattr(sb, "create_client", fake_create_client)
    monkeypatch.setattr(
        sb, "get_settings",
        lambda: SimpleNamespace(
            supabase_url="https://x.supabase.co",
            supabase_service_role_key="svc",
            supabase_anon_key="anon",
        ),
    )
    c = sb.service_client()
    assert captured == {"url": "https://x.supabase.co", "key": "svc"}
    assert c.url == "https://x.supabase.co"


def test_user_client_applies_token(monkeypatch):
    applied = {}

    class FakeClient:
        def __init__(self):
            self.postgrest = SimpleNamespace(auth=lambda t: applied.__setitem__("token", t))

    monkeypatch.setattr(sb, "create_client", lambda url, key: FakeClient())
    monkeypatch.setattr(
        sb, "get_settings",
        lambda: SimpleNamespace(
            supabase_url="https://x.supabase.co",
            supabase_service_role_key="svc",
            supabase_anon_key="anon",
        ),
    )
    sb.user_client("jwt-123")
    assert applied["token"] == "jwt-123"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `(cd backend && .venv/bin/pytest tests/test_clients_supabase.py -v)`
Expected: FAIL — `ModuleNotFoundError: No module named 'delapan.core.clients.supabase'`.

- [ ] **Step 4: Write the implementation**

Create `backend/delapan/core/clients/supabase.py`:

```python
"""Supabase client factories for the cloud tier.

    get_settings() ──► service_client()  (service-role, bypasses RLS)
                  └──► user_client(jwt)   (anon key + user JWT, RLS-scoped)

`supabase` is imported lazily inside the factories so a local-only install
never needs the ``[cloud]`` extra. tenancy.py imports ``service_client`` here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from delapan.core.config import get_settings

if TYPE_CHECKING:
    from supabase import Client


def create_client(url: str, key: str) -> "Client":
    """Thin seam over supabase.create_client (so tests can monkeypatch it)."""
    from supabase import create_client as _create

    return _create(url, key)


def service_client() -> "Client":
    """Service-role client — bypasses RLS. For admin/tenancy lookups."""
    s = get_settings()
    assert s.supabase_url and s.supabase_service_role_key, (
        "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY required for the cloud tier."
    )
    return create_client(s.supabase_url, s.supabase_service_role_key)


def user_client(access_token: str) -> "Client":
    """Anon-key client carrying the user JWT — every call is RLS-scoped."""
    s = get_settings()
    assert s.supabase_url and s.supabase_anon_key, (
        "SUPABASE_URL and SUPABASE_ANON_KEY required for the cloud tier."
    )
    client = create_client(s.supabase_url, s.supabase_anon_key)
    client.postgrest.auth(access_token)
    return client
```

- [ ] **Step 5: Run test to verify it passes**

Run: `(cd backend && .venv/bin/pytest tests/test_clients_supabase.py -v && .venv/bin/ruff check delapan/core/clients/supabase.py)`
Expected: 2 passed, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add delapan/core/clients/supabase.py tests/test_clients_supabase.py pyproject.toml
git commit -m "feat(store): supabase client factories (service-role + user-JWT)"
```

---

### Task 3: SupabaseStore skeleton + fake-client harness + tenancy

Establishes the class, the shared helpers, the in-memory fake used by every later test, and the first three methods.

**Files:**
- Create: `backend/delapan/store/supabase.py`
- Create: `backend/tests/fake_supabase.py` (test harness, shared)
- Create: `backend/tests/test_supabase_tenancy.py`

**Interfaces:**
- Produces:
  - `SupabaseStore(access_token: str, *, org_id: str)` — `self._c` is the user-scoped client (via `user_client`), `self._org_id = org_id`.
  - `_vec(emb: list[float]) -> str` — `"[" + ",".join(repr(float(x)) for x in emb) + "]"`.
  - `resolve_project(name, *, create) -> tuple[str, str]`, `resolve_kb(org_id, project_id, name, *, create) -> str`, `list_projects() -> list[dict]`.
  - `tests/fake_supabase.py`: `FakeSupabase` — a stateful in-memory PostgREST stand-in with `.table(name)`, `.rpc(name, params)`, `.register_rpc(name, fn)`, and `.tables` dict for seeding/asserting.

- [ ] **Step 1: Write the fake-client harness**

Create `backend/tests/fake_supabase.py`:

```python
"""Minimal stateful stand-in for the supabase-py client used in unit tests.

Supports the call shapes SupabaseStore uses: table CRUD with eq/neq/in_/order/
limit/count filters, and rpc(name, params) dispatched to registered callables.
Filters/rows are plain dicts; enough PostgREST behavior to test the store's
transforms and control flow without a network.
"""

from __future__ import annotations

import uuid


class _Resp:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, table, op, payload=None):
        self._t = table
        self._op = op           # "select" | "insert" | "update" | "delete" | "upsert"
        self._payload = payload
        self._filters = []       # list[(kind, col, val)]
        self._order = None
        self._limit = None
        self._count = None
        self._on_conflict = None

    # filter builders (return self for chaining)
    def eq(self, col, val): self._filters.append(("eq", col, val)); return self
    def neq(self, col, val): self._filters.append(("neq", col, val)); return self
    def in_(self, col, vals): self._filters.append(("in", col, list(vals))); return self
    def order(self, col, desc=False): self._order = (col, desc); return self
    def limit(self, n): self._limit = n; return self

    def _match(self, row):
        for kind, col, val in self._filters:
            if kind == "eq" and row.get(col) != val: return False
            if kind == "neq" and row.get(col) == val: return False
            if kind == "in" and row.get(col) not in val: return False
        return True

    def execute(self):
        rows = self._t._rows
        if self._op == "select":
            sel = [dict(r) for r in rows if self._match(r)]
            if self._order:
                col, desc = self._order
                sel.sort(key=lambda r: (r.get(col) is not None, r.get(col)), reverse=desc)
            total = len(sel)
            if self._limit is not None:
                sel = sel[: self._limit]
            return _Resp(sel, count=total if self._count == "exact" else None)
        if self._op in ("insert", "upsert"):
            payload = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for r in payload:
                r = dict(r)
                r.setdefault("id", uuid.uuid4().hex)
                if self._op == "upsert" and self._on_conflict:
                    key = self._on_conflict
                    existing = next((x for x in rows if x.get(key) == r.get(key)), None)
                    if existing:
                        existing.update(r)
                        out.append(dict(existing))
                        continue
                rows.append(r)
                out.append(dict(r))
            return _Resp(out)
        if self._op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(self._payload)
            return _Resp([dict(r) for r in hit])
        if self._op == "delete":
            keep, removed = [], []
            for r in rows:
                (removed if self._match(r) else keep).append(r)
            self._t._rows[:] = keep
            return _Resp([dict(r) for r in removed])
        raise AssertionError(self._op)


class _Table:
    def __init__(self, rows): self._rows = rows
    def select(self, *_cols, count=None):
        q = _Query(self, "select"); q._count = count; return q
    def insert(self, payload): return _Query(self, "insert", payload)
    def upsert(self, payload, on_conflict=None):
        q = _Query(self, "upsert", payload); q._on_conflict = on_conflict; return q
    def update(self, payload): return _Query(self, "update", payload)
    def delete(self): return _Query(self, "delete")


class FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self._rpcs = {}

    def table(self, name):
        return _Table(self.tables.setdefault(name, []))

    def register_rpc(self, name, fn):
        self._rpcs[name] = fn

    def rpc(self, name, params):
        fn = self._rpcs[name]
        return _Query.__new__(_Query) if False else _RpcResp(fn(params))


class _RpcResp:
    def __init__(self, data): self._data = data
    def execute(self): return _Resp(self._data)
```

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_supabase_tenancy.py`:

```python
import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(
        "delapan.store.supabase.user_client", lambda _t: fake
    )
    store = SupabaseStore("jwt", org_id="org1")
    return store, fake


def test_vec_encoding():
    assert SupabaseStore._vec([0.5, -1.0]) == "[0.5,-1.0]"


def test_resolve_project_find_or_create(monkeypatch):
    store, fake = make_store(monkeypatch)
    org, pid = store.resolve_project("repoA", create=True)
    assert org == "org1" and pid
    # second resolve finds the same row
    org2, pid2 = store.resolve_project("repoA", create=False)
    assert (org2, pid2) == (org, pid)
    assert fake.tables["projects"][0]["org_id"] == "org1"


def test_resolve_kb_not_found_raises(monkeypatch):
    store, _ = make_store(monkeypatch)
    with pytest.raises(RuntimeError):
        store.resolve_kb("org1", "p1", "missing", create=False)


def test_list_projects_shape(monkeypatch):
    store, fake = make_store(monkeypatch)
    _, pid = store.resolve_project("repoA", create=True)
    kid = store.resolve_kb("org1", pid, "main", create=True)
    out = store.list_projects()
    assert out == [{"project": "repoA", "project_id": pid,
                    "kbs": [{"kb": "main", "kb_id": kid,
                             "snapshot_count": 0, "last_activity": None}]}]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `(cd backend && .venv/bin/pytest tests/test_supabase_tenancy.py -v)`
Expected: FAIL — `ModuleNotFoundError: No module named 'delapan.store.supabase'`.

- [ ] **Step 4: Write the skeleton + tenancy**

Create `backend/delapan/store/supabase.py`:

```python
"""SupabaseStore — the cloud-tier Store over Supabase (Postgres + pgvector + RLS).

    SupabaseStore(token, org_id) ──► user-scoped supabase-py client ──► PostgREST
                                                                  └──► match_* RPCs

The paid/cloud counterpart to SQLiteStore. Every method returns the same dict/
list shape SQLiteStore returns so the engine is tier-agnostic. RLS scopes reads
to the user's org via the JWT; writes set ``org_id`` explicitly. Embeddings are
inline ``vector(1536)`` columns written as bracketed strings. Async protocol
methods run the sync client under ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from delapan.core.clients.supabase import user_client

_MAX_GROUNDED = 50
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseStore:
    def __init__(self, access_token: str, *, org_id: str) -> None:
        self._c = user_client(access_token)
        self._org_id = org_id

    @staticmethod
    def _vec(emb: list[float]) -> str:
        return "[" + ",".join(repr(float(x)) for x in emb) + "]"

    # --- tenancy -------------------------------------------------------------

    def resolve_project(self, name: str, *, create: bool) -> tuple[str, str]:
        rows = (
            self._c.table("projects").select("id")
            .eq("org_id", self._org_id).eq("name", name).limit(1).execute().data
        )
        if rows:
            return self._org_id, rows[0]["id"]
        if not create:
            raise RuntimeError(f"project {name!r} not found")
        pid = uuid.uuid4().hex
        self._c.table("projects").insert(
            {"id": pid, "org_id": self._org_id, "name": name, "created_at": _now_iso()}
        ).execute()
        return self._org_id, pid

    def resolve_kb(self, org_id: str, project_id: str, name: str, *, create: bool) -> str:
        rows = (
            self._c.table("kbs").select("id")
            .eq("org_id", org_id).eq("project_id", project_id).eq("name", name)
            .limit(1).execute().data
        )
        if rows:
            return rows[0]["id"]
        if not create:
            raise RuntimeError(f"kb {name!r} not found")
        kid = uuid.uuid4().hex
        self._c.table("kbs").insert(
            {"id": kid, "org_id": org_id, "project_id": project_id, "name": name,
             "published": False, "retrieval_miss_streak": 0, "created_at": _now_iso()}
        ).execute()
        return kid

    def list_projects(self) -> list[dict]:
        prows = (
            self._c.table("projects").select("id,name")
            .eq("org_id", self._org_id).neq("name", "__journal__")
            .order("created_at").execute().data
        )
        out: list[dict] = []
        for p in prows:
            krows = (
                self._c.table("kbs").select("id,name")
                .eq("org_id", self._org_id).eq("project_id", p["id"])
                .order("created_at").execute().data
            )
            kbs = []
            for k in krows:
                snaps = (
                    self._c.table("findings").select("created_at", count="exact")
                    .eq("kb_id", k["id"]).eq("category", "snapshot")
                    .order("created_at", desc=True).limit(1).execute()
                )
                last = snaps.data[0]["created_at"] if snaps.data else None
                kbs.append({"kb": k["name"], "kb_id": k["id"],
                            "snapshot_count": snaps.count or 0, "last_activity": last})
            out.append({"project": p["name"], "project_id": p["id"], "kbs": kbs})
        return out
```

- [ ] **Step 5: Run test to verify it passes**

Run: `(cd backend && .venv/bin/pytest tests/test_supabase_tenancy.py -v)`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add delapan/store/supabase.py tests/fake_supabase.py tests/test_supabase_tenancy.py
git commit -m "feat(store): SupabaseStore skeleton + tenancy + fake-client harness"
```

---

### Task 4: Findings

**Files:**
- Modify: `backend/delapan/store/supabase.py` (append the findings methods)
- Test: `backend/tests/test_supabase_findings.py`

**Interfaces:**
- Consumes: `SupabaseStore`, `FakeSupabase` (Task 3); `_vec`, `_now_iso`, `_MAX_GROUNDED`, `LIST_*`.
- Produces: `async match_findings(kb_id, query_embedding, match_count, min_similarity, categories=None)`, `async insert_findings(rows) -> list[str]`, `get_finding(kb_id, fid) -> dict` (raises), `get_finding_global(fid) -> dict` (raises), `list_findings(kb_id, category=None, limit=None) -> dict`, `count_findings(kb_id) -> int`, `delete_finding(kb_id, fid) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_supabase_findings.py`:

```python
import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1"), fake


@pytest.mark.asyncio
async def test_insert_sets_org_and_status_and_vec(monkeypatch):
    store, fake = make_store(monkeypatch)
    ids = await store.insert_findings([
        {"id": "f1", "kb_id": "kb1", "title": "T", "content": "body",
         "category": "c", "confidence": 0.9, "tags": ["a"],
         "provenance": [{"url": "u"}], "embedding": [0.1, 0.2]},
    ])
    assert ids == ["f1"]
    row = fake.tables["findings"][0]
    assert row["org_id"] == "org1" and row["status"] == "approved"
    assert row["embedding"] == "[0.1,0.2]"
    assert row["tags"] == ["a"] and row["provenance"] == [{"url": "u"}]


def test_get_finding_raises_when_absent(monkeypatch):
    store, _ = make_store(monkeypatch)
    with pytest.raises(RuntimeError):
        store.get_finding("kb1", "nope")


def test_get_finding_global_ignores_kb(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["findings"] = [{"id": "f1", "org_id": "org1", "kb_id": "other",
                                "title": "T", "content": "b", "category": "c",
                                "confidence": 0.5, "tags": [], "provenance": [],
                                "created_at": "t"}]
    got = store.get_finding_global("f1")
    assert got["id"] == "f1" and got["title"] == "T" and "created_at" in got


def test_list_and_count(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["findings"] = [
        {"id": f"f{i}", "org_id": "org1", "kb_id": "kb1", "title": f"T{i}",
         "category": "c", "confidence": 0.5, "tags": [], "created_at": f"t{i}"}
        for i in range(3)
    ]
    out = store.list_findings("kb1")
    assert out["count"] == 3 and out["findings"][0]["id"].startswith("f")
    assert store.count_findings("kb1") == 3


@pytest.mark.asyncio
async def test_match_findings_calls_rpc_with_match_kb_id(monkeypatch):
    store, fake = make_store(monkeypatch)
    seen = {}
    fake.register_rpc("match_findings", lambda p: (seen.update(p) or [
        {"id": "f1", "title": "T", "content": "b", "category": "c",
         "confidence": 0.9, "tags": [], "provenance": [], "similarity": 0.8}]))
    hits = await store.match_findings("kb1", [0.1, 0.2], match_count=5, min_similarity=0.0)
    assert seen["match_kb_id"] == "kb1" and seen["query_embedding"] == "[0.1,0.2]"
    assert hits[0]["similarity"] == 0.8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `(cd backend && .venv/bin/pytest tests/test_supabase_findings.py -v)`
Expected: FAIL — `AttributeError: 'SupabaseStore' object has no attribute 'insert_findings'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/delapan/store/supabase.py`:

```python
    # --- findings ------------------------------------------------------------

    async def match_findings(self, kb_id, query_embedding, match_count,
                             min_similarity, categories=None):
        params = {
            "query_embedding": self._vec(query_embedding),
            "match_kb_id": kb_id,
            "match_count": match_count,
            "min_similarity": min_similarity,
        }
        if categories:
            params["categories"] = list(categories)
        data = await asyncio.to_thread(
            lambda: self._c.rpc("match_findings", params).execute().data
        )
        return data or []

    async def insert_findings(self, rows: list[dict]) -> list[str]:
        if not rows:
            return []
        payload, ids = [], []
        for r in rows:
            fid = r.get("id") or uuid.uuid4().hex
            ids.append(fid)
            row = {
                "id": fid, "org_id": self._org_id, "kb_id": r.get("kb_id"),
                "title": r.get("title"), "content": r.get("content"),
                "category": r.get("category"), "confidence": r.get("confidence"),
                "tags": list(r.get("tags") or []),
                "provenance": list(r.get("provenance") or []),
                "status": "approved", "created_at": r.get("created_at") or _now_iso(),
            }
            emb = r.get("embedding")
            if emb is not None:
                row["embedding"] = self._vec(list(emb))
            payload.append(row)
        await asyncio.to_thread(lambda: self._c.table("findings").insert(payload).execute())
        return ids

    @staticmethod
    def _finding(row: dict) -> dict:
        return {
            "id": row["id"], "title": row["title"], "content": row["content"],
            "category": row["category"], "confidence": row["confidence"],
            "tags": row.get("tags") or [], "provenance": row.get("provenance") or [],
            "created_at": row["created_at"],
        }

    def get_finding(self, kb_id: str, finding_id: str) -> dict:
        rows = (self._c.table("findings").select("*")
                .eq("kb_id", kb_id).eq("id", finding_id).limit(1).execute().data)
        if not rows:
            raise RuntimeError("finding not found")
        return self._finding(rows[0])

    def get_finding_global(self, finding_id: str) -> dict:
        rows = (self._c.table("findings").select("*")
                .eq("id", finding_id).limit(1).execute().data)
        if not rows:
            raise RuntimeError("finding not found")
        return self._finding(rows[0])

    def list_findings(self, kb_id, category=None, limit=None) -> dict:
        n = min(limit or LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT)
        q = (self._c.table("findings")
             .select("id,title,category,confidence,tags,created_at").eq("kb_id", kb_id))
        if category:
            q = q.eq("category", category)
        rows = q.order("created_at", desc=True).limit(n).execute().data
        findings = [{"id": r["id"], "title": r["title"], "category": r["category"],
                     "confidence": r["confidence"], "tags": r.get("tags") or [],
                     "created_at": r["created_at"]} for r in rows]
        return {"count": len(findings), "findings": findings}

    def count_findings(self, kb_id: str) -> int:
        res = (self._c.table("findings").select("id", count="exact")
               .eq("kb_id", kb_id).execute())
        return int(res.count or 0)

    def delete_finding(self, kb_id: str, finding_id: str) -> dict:
        self._c.table("findings").delete().eq("kb_id", kb_id).eq("id", finding_id).execute()
        return {"deleted": finding_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `(cd backend && .venv/bin/pytest tests/test_supabase_findings.py -v)`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add delapan/store/supabase.py tests/test_supabase_findings.py
git commit -m "feat(store): SupabaseStore findings (match/insert/get/list/count/delete)"
```

---

### Task 5: KG read

**Files:**
- Modify: `backend/delapan/store/supabase.py` (append KG read methods)
- Test: `backend/tests/test_supabase_kg_read.py`

**Interfaces:**
- Produces: `get_kg_subgraph(kb_id, *, seed_node_ids=None, node_cap=200, edge_cap=600, depth=1) -> dict`, `kg_stats(kb_id) -> dict`, `list_kg_nodes(kb_id, *, type=None, limit=None) -> list[dict]`, `get_kg_node(kb_id, node_id) -> dict | None`, `async match_kg_nodes(kb_id, query_embedding, match_count, min_similarity) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_supabase_kg_read.py`:

```python
import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1"), fake


def _node(i, kb="kb1"):
    return {"id": i, "org_id": "org1", "kb_id": kb, "type": "concept", "label": i,
            "properties": {}, "grounded_in": [], "created_at": "t"}


def _edge(eid, s, t, kb="kb1"):
    return {"id": eid, "org_id": "org1", "kb_id": kb, "source_node_id": s,
            "target_node_id": t, "relation": "r", "properties": {},
            "grounded_in": [], "created_at": "t"}


def test_full_subgraph(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["kg_nodes"] = [_node("a"), _node("b")]
    fake.tables["kg_edges"] = [_edge("e1", "a", "b")]
    g = store.get_kg_subgraph("kb1")
    assert {n["id"] for n in g["nodes"]} == {"a", "b"}
    assert g["edges"][0]["source_node_id"] == "a"


def test_bfs_subgraph_one_hop(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["kg_nodes"] = [_node("a"), _node("b"), _node("c")]
    fake.tables["kg_edges"] = [_edge("e1", "a", "b"), _edge("e2", "b", "c")]
    g = store.get_kg_subgraph("kb1", seed_node_ids=["a"], depth=1)
    assert "a" in {n["id"] for n in g["nodes"]}
    assert "b" in {n["id"] for n in g["nodes"]}  # one hop reaches b


def test_kg_stats(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["kg_nodes"] = [_node("a"), {**_node("b"), "type": "tech"}]
    fake.tables["kg_edges"] = [_edge("e1", "a", "b")]
    s = store.kg_stats("kb1")
    assert s["node_count"] == 2 and s["edge_count"] == 1
    assert s["by_type"] == {"concept": 1, "tech": 1}


def test_get_kg_node_none_when_absent(monkeypatch):
    store, _ = make_store(monkeypatch)
    assert store.get_kg_node("kb1", "nope") is None


@pytest.mark.asyncio
async def test_match_kg_nodes_rpc(monkeypatch):
    store, fake = make_store(monkeypatch)
    seen = {}
    fake.register_rpc("match_kg_nodes", lambda p: (seen.update(p) or [
        {"id": "a", "type": "concept", "label": "A", "properties": {}, "similarity": 1.0}]))
    hits = await store.match_kg_nodes("kb1", [0.1], match_count=3, min_similarity=0.0)
    assert seen["match_kb_id"] == "kb1" and hits[0]["similarity"] == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `(cd backend && .venv/bin/pytest tests/test_supabase_kg_read.py -v)`
Expected: FAIL — `AttributeError: ... 'get_kg_subgraph'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/delapan/store/supabase.py`:

```python
    # --- KG read -------------------------------------------------------------

    @staticmethod
    def _node(r: dict) -> dict:
        return {"id": r["id"], "type": r["type"], "label": r["label"],
                "properties": r.get("properties") or {},
                "grounded_in": r.get("grounded_in") or [], "created_at": r["created_at"]}

    @staticmethod
    def _edge(r: dict) -> dict:
        return {"id": r["id"], "source_node_id": r["source_node_id"],
                "target_node_id": r["target_node_id"], "relation": r["relation"],
                "properties": r.get("properties") or {},
                "grounded_in": r.get("grounded_in") or [], "created_at": r["created_at"]}

    def _incident_edges(self, kb_id: str, frontier: list[str], edge_cap: int) -> list[dict]:
        # PostgREST .or_() is brittle; fetch source- and target-incident edges
        # separately and union (mirrors the SQLite OR query).
        src = (self._c.table("kg_edges").select("*").eq("kb_id", kb_id)
               .in_("source_node_id", frontier).limit(edge_cap).execute().data)
        tgt = (self._c.table("kg_edges").select("*").eq("kb_id", kb_id)
               .in_("target_node_id", frontier).limit(edge_cap).execute().data)
        seen, out = set(), []
        for r in [*src, *tgt]:
            if r["id"] not in seen:
                seen.add(r["id"]); out.append(r)
        return out

    def get_kg_subgraph(self, kb_id, *, seed_node_ids=None,
                        node_cap=200, edge_cap=600, depth=1) -> dict:
        if seed_node_ids:
            frontier = list(dict.fromkeys(seed_node_ids))
            all_node_ids: set[str] = set(frontier)
            all_edges: list[dict] = []
            seen_e: set[str] = set()
            visited: set[str] = set()
            for _ in range(max(depth, 1)):
                to_expand = [n for n in frontier if n not in visited]
                if not to_expand:
                    break
                visited.update(to_expand)
                hop = self._incident_edges(kb_id, to_expand, edge_cap)
                new_nodes: set[str] = set()
                for er in hop:
                    if er["id"] not in seen_e:
                        seen_e.add(er["id"]); all_edges.append(er)
                    new_nodes.add(er["source_node_id"]); new_nodes.add(er["target_node_id"])
                all_node_ids.update(new_nodes)
                if len(all_node_ids) >= node_cap:
                    break
                frontier = [n for n in new_nodes if n not in visited]
                if not frontier:
                    break
            wanted = list(all_node_ids)[:node_cap]
            node_rows = (self._c.table("kg_nodes").select("*").eq("kb_id", kb_id)
                         .in_("id", wanted).execute().data) if wanted else []
            edge_rows = all_edges[:edge_cap]
        else:
            node_rows = (self._c.table("kg_nodes").select("*")
                         .eq("kb_id", kb_id).limit(node_cap).execute().data)
            edge_rows = (self._c.table("kg_edges").select("*")
                         .eq("kb_id", kb_id).limit(edge_cap).execute().data)
        return {"nodes": [self._node(r) for r in node_rows],
                "edges": [self._edge(r) for r in edge_rows]}

    def kg_stats(self, kb_id: str) -> dict:
        node_rows = (self._c.table("kg_nodes").select("type").eq("kb_id", kb_id).execute().data)
        edge_rows = (self._c.table("kg_edges").select("relation").eq("kb_id", kb_id).execute().data)
        by_type: dict[str, int] = {}
        for r in node_rows:
            by_type[r.get("type") or "unknown"] = by_type.get(r.get("type") or "unknown", 0) + 1
        by_relation: dict[str, int] = {}
        for r in edge_rows:
            key = r.get("relation") or "unknown"
            by_relation[key] = by_relation.get(key, 0) + 1
        return {"node_count": len(node_rows), "edge_count": len(edge_rows),
                "by_type": by_type, "by_relation": by_relation}

    def list_kg_nodes(self, kb_id, *, type=None, limit=None) -> list[dict]:
        n = min(limit or 50, 500)
        q = self._c.table("kg_nodes").select("*").eq("kb_id", kb_id)
        if type:
            q = q.eq("type", type)
        rows = q.order("created_at", desc=True).limit(n).execute().data
        return [self._node(r) for r in rows]

    def get_kg_node(self, kb_id: str, node_id: str) -> dict | None:
        rows = (self._c.table("kg_nodes").select("*")
                .eq("id", node_id).eq("kb_id", kb_id).limit(1).execute().data)
        return self._node(rows[0]) if rows else None

    async def match_kg_nodes(self, kb_id, query_embedding, match_count, min_similarity):
        params = {"query_embedding": self._vec(query_embedding), "match_kb_id": kb_id,
                  "match_count": match_count, "min_similarity": min_similarity}
        data = await asyncio.to_thread(
            lambda: self._c.rpc("match_kg_nodes", params).execute().data)
        return data or []
```

Note: `kg_stats` aggregates client-side because PostgREST has no `GROUP BY`. At cloud scale the per-KB node/edge sets are bounded (hundreds); if a KB ever grows past a few thousand nodes, replace with a deployed `kg_stats` RPC (deferred per spec §7).

- [ ] **Step 4: Run test to verify it passes**

Run: `(cd backend && .venv/bin/pytest tests/test_supabase_kg_read.py -v)`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add delapan/store/supabase.py tests/test_supabase_kg_read.py
git commit -m "feat(store): SupabaseStore KG reads (subgraph/stats/list/get/match)"
```

---

### Task 6: KG write

**Files:**
- Modify: `backend/delapan/store/supabase.py` (append KG write methods)
- Test: `backend/tests/test_supabase_kg_write.py`

**Interfaces:**
- Produces: `async upsert_kg_nodes(kb_id, nodes) -> list[str]`, `async upsert_kg_edges(kb_id, edges) -> int`, `async update_kg_node(kb_id, node_id, *, properties, grounded_in=None, embedding=None, label=None, type=None) -> None`, `delete_kg_node(kb_id, node_id) -> dict`, `delete_kg_edge(kb_id, edge_id) -> dict`, `clear_kg(kb_id) -> None`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_supabase_kg_write.py`:

```python
import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1"), fake


@pytest.mark.asyncio
async def test_upsert_nodes_inserts_with_required_cols(monkeypatch):
    store, fake = make_store(monkeypatch)
    ids = await store.upsert_kg_nodes("kb1", [
        {"type": "concept", "label": "A", "properties": {"x": 1},
         "grounded_in": ["f1"], "embedding": [0.1]},
    ])
    assert len(ids) == 1
    row = fake.tables["kg_nodes"][0]
    assert row["org_id"] == "org1" and row["aliases"] == [] and row["merge_history"] == []
    assert row["embedding"] == "[0.1]"


@pytest.mark.asyncio
async def test_upsert_nodes_merges_existing(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["kg_nodes"] = [{"id": "n1", "org_id": "org1", "kb_id": "kb1",
                                "type": "concept", "label": "A",
                                "properties": {"keep": 1}, "grounded_in": ["f1"],
                                "aliases": [], "merge_history": [], "created_at": "t"}]
    ids = await store.upsert_kg_nodes("kb1", [
        {"type": "concept", "label": "A", "properties": {"keep": 9, "new": 2},
         "grounded_in": ["f2"]}])
    assert ids == ["n1"]  # reused
    row = fake.tables["kg_nodes"][0]
    assert row["properties"]["keep"] == 1  # existing wins
    assert row["grounded_in"] == ["f1", "f2"]  # unioned


@pytest.mark.asyncio
async def test_upsert_edges_skips_dupes_and_selfloops(monkeypatch):
    store, fake = make_store(monkeypatch)
    n = await store.upsert_kg_edges("kb1", [
        {"source_node_id": "a", "target_node_id": "b", "relation": "r"},
        {"source_node_id": "a", "target_node_id": "b", "relation": "r"},  # dupe
        {"source_node_id": "a", "target_node_id": "a", "relation": "r"},  # self
    ])
    assert n == 1


def test_delete_node_removes_incident_edges(monkeypatch):
    store, fake = make_store(monkeypatch)
    fake.tables["kg_nodes"] = [{"id": "a", "org_id": "org1", "kb_id": "kb1",
                                "type": "c", "label": "A", "properties": {},
                                "grounded_in": [], "created_at": "t"}]
    fake.tables["kg_edges"] = [{"id": "e1", "org_id": "org1", "kb_id": "kb1",
                                "source_node_id": "a", "target_node_id": "b",
                                "relation": "r", "properties": {}, "grounded_in": [],
                                "created_at": "t"}]
    res = store.delete_kg_node("kb1", "a")
    assert res["deleted"] is True and res["removed_edge_ids"] == ["e1"]
    assert fake.tables["kg_nodes"] == [] and fake.tables["kg_edges"] == []


def test_delete_missing_node(monkeypatch):
    store, _ = make_store(monkeypatch)
    assert store.delete_kg_node("kb1", "nope") == {"deleted": False, "removed_edge_ids": []}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `(cd backend && .venv/bin/pytest tests/test_supabase_kg_write.py -v)`
Expected: FAIL — `AttributeError: ... 'upsert_kg_nodes'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/delapan/store/supabase.py`:

```python
    # --- KG write ------------------------------------------------------------

    async def upsert_kg_nodes(self, kb_id: str, nodes: list[dict]) -> list[str]:
        if not nodes:
            return []
        return await asyncio.to_thread(self._upsert_kg_nodes_sync, kb_id, nodes)

    def _upsert_kg_nodes_sync(self, kb_id: str, nodes: list[dict]) -> list[str]:
        ids: list[str] = []
        batch: dict[tuple[str, str], str] = {}
        for nd in nodes:
            typ, label = nd.get("type") or "", nd.get("label") or ""
            props = dict(nd.get("properties") or {})
            grounded = list(nd.get("grounded_in") or [])
            key = (typ, label)
            if key in batch:
                self._merge_node(batch[key], props, grounded); ids.append(batch[key]); continue
            existing = (self._c.table("kg_nodes").select("id")
                        .eq("kb_id", kb_id).eq("type", typ).eq("label", label)
                        .limit(1).execute().data)
            if existing:
                nid = existing[0]["id"]
                self._merge_node(nid, props, grounded)
            else:
                nid = uuid.uuid4().hex
                row = {"id": nid, "org_id": self._org_id, "kb_id": kb_id, "type": typ,
                       "label": label, "properties": props,
                       "grounded_in": grounded[-_MAX_GROUNDED:], "aliases": [],
                       "merge_history": [], "created_at": _now_iso()}
                emb = nd.get("embedding")
                if emb is not None:
                    row["embedding"] = self._vec(list(emb))
                self._c.table("kg_nodes").insert(row).execute()
            batch[key] = nid; ids.append(nid)
        return ids

    def _merge_node(self, node_id: str, props: dict, grounded: list[str]) -> None:
        rows = self._c.table("kg_nodes").select("properties,grounded_in").eq("id", node_id).limit(1).execute().data
        if not rows:
            return
        ex_props = rows[0].get("properties") or {}
        ex_grounded = rows[0].get("grounded_in") or []
        merged_props = {**props, **ex_props}
        merged_grounded = list(dict.fromkeys([*ex_grounded, *grounded]))[-_MAX_GROUNDED:]
        self._c.table("kg_nodes").update(
            {"properties": merged_props, "grounded_in": merged_grounded}).eq("id", node_id).execute()

    async def upsert_kg_edges(self, kb_id: str, edges: list[dict]) -> int:
        if not edges:
            return 0
        return await asyncio.to_thread(self._upsert_kg_edges_sync, kb_id, edges)

    def _upsert_kg_edges_sync(self, kb_id: str, edges: list[dict]) -> int:
        inserted = 0
        for e in edges:
            sid, tid = e.get("source_node_id"), e.get("target_node_id")
            rel = e.get("relation") or ""
            if not sid or not tid or sid == tid:
                continue
            dupe = (self._c.table("kg_edges").select("id").eq("kb_id", kb_id)
                    .eq("source_node_id", sid).eq("target_node_id", tid)
                    .eq("relation", rel).limit(1).execute().data)
            if dupe:
                continue
            self._c.table("kg_edges").insert(
                {"id": uuid.uuid4().hex, "org_id": self._org_id, "kb_id": kb_id,
                 "source_node_id": sid, "target_node_id": tid, "relation": rel,
                 "properties": dict(e.get("properties") or {}),
                 "grounded_in": list(e.get("grounded_in") or []),
                 "created_at": _now_iso()}).execute()
            inserted += 1
        return inserted

    async def update_kg_node(self, kb_id, node_id, *, properties, grounded_in=None,
                             embedding=None, label=None, type=None) -> None:
        patch: dict = {"properties": properties}
        if grounded_in is not None:
            patch["grounded_in"] = list(grounded_in)[-_MAX_GROUNDED:]
        if label is not None:
            patch["label"] = label
        if type is not None:
            patch["type"] = type
        if embedding is not None:
            patch["embedding"] = self._vec(list(embedding))

        def _run():
            self._c.table("kg_nodes").update(patch).eq("id", node_id).eq("kb_id", kb_id).execute()
        await asyncio.to_thread(_run)

    def delete_kg_node(self, kb_id: str, node_id: str) -> dict:
        exists = (self._c.table("kg_nodes").select("id")
                  .eq("id", node_id).eq("kb_id", kb_id).limit(1).execute().data)
        if not exists:
            return {"deleted": False, "removed_edge_ids": []}
        src = (self._c.table("kg_edges").select("id").eq("kb_id", kb_id)
               .eq("source_node_id", node_id).execute().data)
        tgt = (self._c.table("kg_edges").select("id").eq("kb_id", kb_id)
               .eq("target_node_id", node_id).execute().data)
        edge_ids = list(dict.fromkeys([r["id"] for r in [*src, *tgt]]))
        for eid in edge_ids:
            self._c.table("kg_edges").delete().eq("id", eid).execute()
        self._c.table("kg_nodes").delete().eq("id", node_id).eq("kb_id", kb_id).execute()
        return {"deleted": True, "removed_edge_ids": edge_ids}

    def delete_kg_edge(self, kb_id: str, edge_id: str) -> dict:
        removed = (self._c.table("kg_edges").delete()
                   .eq("id", edge_id).eq("kb_id", kb_id).execute().data)
        return {"deleted": bool(removed)}

    def clear_kg(self, kb_id: str) -> None:
        self._c.table("kg_edges").delete().eq("kb_id", kb_id).execute()
        self._c.table("kg_nodes").delete().eq("kb_id", kb_id).execute()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `(cd backend && .venv/bin/pytest tests/test_supabase_kg_write.py -v)`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add delapan/store/supabase.py tests/test_supabase_kg_write.py
git commit -m "feat(store): SupabaseStore KG writes (upsert/update/delete/clear)"
```

---

### Task 7: synopsis · exploration · intent · stamps · monitoring

**Files:**
- Modify: `backend/delapan/store/supabase.py` (append the remaining methods)
- Test: `backend/tests/test_supabase_misc.py`

**Interfaces:**
- Produces: `load_synopsis(kb_id) -> dict | None`, `upsert_synopsis(kb_id, content, finding_count, model) -> None`, `create_exploration(org_id, kb_id, prompt) -> str`, `update_exploration(exploration_id, **patch) -> None`, `get_exploration(exploration_id) -> dict | None`, `get_kg_intent(kb_id) -> dict | None`, `set_kg_intent(org_id, kb_id, schema) -> dict`, `get_init_offered(kb_id) -> bool`, `mark_init_offered(kb_id) -> None`, `get_drift_marker(kb_id) -> int`, `set_drift_marker(kb_id, count) -> None`, `async record_access(*, org_id, kb_id, surface, targets, query_text=None) -> None`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_supabase_misc.py`:

```python
import pytest

from delapan.store.supabase import SupabaseStore
from tests.fake_supabase import FakeSupabase


def make_store(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _t: fake)
    return SupabaseStore("jwt", org_id="org1"), fake


def test_synopsis_roundtrip(monkeypatch):
    store, _ = make_store(monkeypatch)
    store.upsert_synopsis("kb1", content=[{"h": "x"}], finding_count=3, model="m")
    syn = store.load_synopsis("kb1")
    assert syn["content"] == [{"h": "x"}] and syn["finding_count_at_build"] == 3


def test_load_synopsis_none(monkeypatch):
    store, _ = make_store(monkeypatch)
    assert store.load_synopsis("kb1") is None


def test_exploration_lifecycle(monkeypatch):
    store, _ = make_store(monkeypatch)
    eid = store.create_exploration("org1", "kb1", "find things")
    store.update_exploration(eid, status="done", finding_ids=["f1"], junk="ignored")
    got = store.get_exploration(eid)
    assert got["status"] == "done" and got["finding_ids"] == ["f1"]


def test_kg_intent_versions(monkeypatch):
    store, _ = make_store(monkeypatch)
    assert store.get_kg_intent("kb1") is None
    r1 = store.set_kg_intent("org1", "kb1", {"v": 1})
    r2 = store.set_kg_intent("org1", "kb1", {"v": 2})
    assert r1["version"] == 1 and r2["version"] == 2
    assert store.get_kg_intent("kb1")["version"] == 2


@pytest.mark.asyncio
async def test_record_access_never_raises(monkeypatch):
    store, _ = make_store(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(store._c, "table", boom)
    await store.record_access(org_id="org1", kb_id="kb1", surface="s", targets=[])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `(cd backend && .venv/bin/pytest tests/test_supabase_misc.py -v)`
Expected: FAIL — `AttributeError: ... 'upsert_synopsis'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/delapan/store/supabase.py`:

```python
    # --- synopsis ------------------------------------------------------------

    def load_synopsis(self, kb_id: str) -> dict | None:
        rows = (self._c.table("kb_synopsis")
                .select("content,finding_count_at_build,built_at,model")
                .eq("kb_id", kb_id).limit(1).execute().data)
        if not rows:
            return None
        r = rows[0]
        return {"content": r.get("content") or [],
                "finding_count_at_build": r.get("finding_count_at_build"),
                "built_at": r.get("built_at"), "model": r.get("model")}

    def upsert_synopsis(self, kb_id, content, finding_count, model) -> None:
        self._c.table("kb_synopsis").upsert(
            {"kb_id": kb_id, "org_id": self._org_id, "content": content,
             "finding_count_at_build": finding_count, "model": model,
             "built_at": _now_iso()}, on_conflict="kb_id").execute()

    # --- exploration ---------------------------------------------------------

    def create_exploration(self, org_id: str, kb_id: str, prompt: str) -> str:
        eid = uuid.uuid4().hex
        self._c.table("explorations").insert(
            {"id": eid, "org_id": self._org_id, "kb_id": kb_id, "prompt": prompt,
             "status": "pending", "started_at": _now_iso(),
             "created_at": _now_iso()}).execute()
        return eid

    def update_exploration(self, exploration_id: str, **patch) -> None:
        allowed = {"status", "error", "finding_ids", "started_at", "completed_at", "prompt"}
        clean = {k: v for k, v in patch.items() if k in allowed}
        if not clean:
            return
        self._c.table("explorations").update(clean).eq("id", exploration_id).execute()

    def get_exploration(self, exploration_id: str) -> dict | None:
        rows = (self._c.table("explorations")
                .select("id,status,finding_ids,completed_at,error")
                .eq("id", exploration_id).limit(1).execute().data)
        if not rows:
            return None
        r = rows[0]
        return {"id": r["id"], "status": r["status"],
                "finding_ids": r.get("finding_ids") or [],
                "completed_at": r.get("completed_at"), "error": r.get("error")}

    # --- KG intent schema ----------------------------------------------------

    def get_kg_intent(self, kb_id: str) -> dict | None:
        rows = (self._c.table("kg_schemas").select("version,schema")
                .eq("kb_id", kb_id).order("version", desc=True).limit(1).execute().data)
        if not rows:
            return None
        return {"version": rows[0]["version"], "schema": rows[0].get("schema") or {}}

    def set_kg_intent(self, org_id: str, kb_id: str, schema: dict) -> dict:
        cur = (self._c.table("kg_schemas").select("version")
               .eq("kb_id", kb_id).order("version", desc=True).limit(1).execute().data)
        next_version = (cur[0]["version"] if cur else 0) + 1
        self._c.table("kg_schemas").insert(
            {"id": uuid.uuid4().hex, "org_id": org_id, "kb_id": kb_id,
             "version": next_version, "schema": schema,
             "created_at": _now_iso()}).execute()
        return {"version": next_version, "schema": schema}

    # --- offer/drift stamps (best-effort) ------------------------------------

    def get_init_offered(self, kb_id: str) -> bool:
        try:
            rows = self._c.table("kbs").select("init_offered_at").eq("id", kb_id).limit(1).execute().data
            return bool(rows and rows[0].get("init_offered_at"))
        except Exception:  # noqa: BLE001 — column absent
            return False

    def mark_init_offered(self, kb_id: str) -> None:
        try:
            self._c.table("kbs").update({"init_offered_at": _now_iso()}).eq("id", kb_id).execute()
        except Exception:  # noqa: BLE001
            pass

    def get_drift_marker(self, kb_id: str) -> int:
        try:
            rows = self._c.table("kbs").select("drift_offered_count").eq("id", kb_id).limit(1).execute().data
            v = rows[0].get("drift_offered_count") if rows else None
            return int(v) if v is not None else 0
        except Exception:  # noqa: BLE001
            return 0

    def set_drift_marker(self, kb_id: str, count: int) -> None:
        try:
            self._c.table("kbs").update({"drift_offered_count": int(count)}).eq("id", kb_id).execute()
        except Exception:  # noqa: BLE001
            pass

    # --- monitoring (best-effort, never raises) ------------------------------

    async def record_access(self, *, org_id, kb_id, surface, targets, query_text=None) -> None:
        def _run():
            try:
                self._c.table("access_events").insert(
                    {"org_id": self._org_id, "kb_id": kb_id, "surface": surface,
                     "targets": list(targets), "query_text": query_text,
                     "created_at": _now_iso()}).execute()
            except Exception:  # noqa: BLE001 — monitoring must never break the caller
                pass
        await asyncio.to_thread(_run)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `(cd backend && .venv/bin/pytest tests/test_supabase_misc.py -v && .venv/bin/ruff check delapan/store/supabase.py)`
Expected: 5 passed, ruff clean.

- [ ] **Step 5: Full unit gate**

Run: `(cd backend && .venv/bin/pytest tests/test_supabase_*.py tests/test_clients_supabase.py -v)`
Expected: all SupabaseStore unit suites pass.

- [ ] **Step 6: Commit**

```bash
git add delapan/store/supabase.py tests/test_supabase_misc.py
git commit -m "feat(store): SupabaseStore synopsis/exploration/intent/stamps/monitoring"
```

---

### Task 8: `seed_dev.py` — MCP user provisioner

**Files:**
- Create: `backend/scripts/seed_dev.py`

**Interfaces:**
- Consumes: `service_client` (Task 2); `get_settings()` (`mcp_user_email`, `mcp_user_password`).
- Produces: a runnable script that ensures the MCP GoTrue user exists and belongs to the actuary org.

- [ ] **Step 1: Write the script**

Create `backend/scripts/seed_dev.py`:

```python
"""Ensure the MCP user exists in GoTrue and belongs to the target org.

    .venv/bin/python scripts/seed_dev.py [--org <uuid>]

Idempotent. Uses the service-role client (admin). Reads the MCP user creds from
settings (DLP_MCP_USER_EMAIL / DLP_MCP_USER_PASSWORD). Default org is the ported
actuary org. Prints the resolved user_id.
"""

from __future__ import annotations

import argparse
import sys

from delapan.core.clients.supabase import service_client
from delapan.core.config import get_settings

ACTUARY_ORG = "1a7d0aa5-587f-4420-985b-bafcf03bf04f"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default=ACTUARY_ORG)
    args = ap.parse_args()
    s = get_settings()
    sb = service_client()

    # 1. find-or-create the auth user (admin API)
    email, password = s.mcp_user_email, s.mcp_user_password
    existing = sb.auth.admin.list_users()
    user = next((u for u in existing if getattr(u, "email", None) == email), None)
    if user is None:
        res = sb.auth.admin.create_user(
            {"email": email, "password": password, "email_confirm": True})
        user = res.user
        print(f"created auth user {email}")
    else:
        print(f"auth user {email} already exists")
    user_id = user.id

    # 2. ensure org membership
    member = (sb.table("org_members").select("user_id")
              .eq("org_id", args.org).eq("user_id", user_id).limit(1).execute().data)
    if not member:
        sb.table("org_members").insert(
            {"org_id": args.org, "user_id": user_id, "role": "member"}).execute()
        print(f"added {email} to org {args.org}")
    else:
        print(f"{email} already a member of {args.org}")
    print(f"user_id={user_id}")


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it (requires creds in `.env`)**

Run: `(cd backend && .venv/bin/python scripts/seed_dev.py)`
Expected: prints `user_id=…`, and on a second run prints "already exists"/"already a member" (idempotent). If `org_members` columns differ from `{org_id,user_id,role}`, adjust the insert to match the live schema (verify with `\d org_members` in the SQL editor).

- [ ] **Step 3: Commit**

```bash
git add scripts/seed_dev.py
git commit -m "feat(store): seed_dev.py — provision the MCP user + org membership"
```

---

### Task 9: live smoke tests + end-to-end cutover

**Files:**
- Create: `backend/tests/test_supabase_live.py`
- Modify: `backend/.env` (creds; not committed)

**Interfaces:**
- Consumes: the full `SupabaseStore` (Tasks 2–7), `resolve_tenant` (cloud branch, exists), `seed_dev.py` (Task 8).

- [ ] **Step 1: Write the env-gated live smoke test**

Create `backend/tests/test_supabase_live.py`:

```python
"""Opt-in live smoke tests against the real cloud actuary KB.

Run with: RUN_CLOUD_TESTS=1 .venv/bin/pytest tests/test_supabase_live.py -v
Skipped by default so the unit suite stays hermetic.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_CLOUD_TESTS") != "1", reason="set RUN_CLOUD_TESTS=1")


def _store():
    from delapan.mcp.tenancy import resolve_tenant
    from delapan.store import get_store
    ctx = resolve_tenant("actuary", "methodologies", create=False)
    return get_store(ctx.access_token, org_id=ctx.org_id), ctx


def test_live_reads_actuary():
    store, ctx = _store()
    stats = store.kg_stats(ctx.kb_id)
    assert stats["node_count"] == 69 and stats["edge_count"] == 56
    findings = store.list_findings(ctx.kb_id)
    assert findings["count"] > 0
    g = store.get_kg_subgraph(ctx.kb_id, node_cap=10)
    assert g["nodes"]
```

- [ ] **Step 2: Confirm `.env` creds + seeded user, run the live smoke**

Ensure `backend/.env` has `DELAPAN_BACKEND=cloud`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `DLP_MCP_USER_EMAIL`, `DLP_MCP_USER_PASSWORD`, then:

Run: `(cd backend && RUN_CLOUD_TESTS=1 .venv/bin/pytest tests/test_supabase_live.py -v)`
Expected: PASS — the MCP user reads the actuary org's rows under RLS (proves the auth model end-to-end). If it returns 0 rows, RLS policy or org membership is wrong → revisit Task 1/Task 8.

- [ ] **Step 3: Manual end-to-end (the acceptance gate)**

With `DELAPAN_BACKEND=cloud`, start the backend and frontend:
```bash
(cd backend && .venv/bin/python -m uvicorn delapan.api.main:app --host 127.0.0.1 --port 8001) &
(cd frontend && npm run dev)
```
In the browser (scope = `actuary` / `methodologies`): the graph loads from cloud; select a node, press `R` — the OKF reader shows frontmatter, real finding bodies, related concepts, sources; `✨ synthesize` round-trips. This is the goal state.

- [ ] **Step 4: Commit**

```bash
git add tests/test_supabase_live.py
git commit -m "test(store): env-gated live smoke against cloud actuary KB"
```

- [ ] **Step 5: Leave `DELAPAN_BACKEND` per preference**

`.env` is not committed. Decide whether to keep `DELAPAN_BACKEND=cloud` (serve cloud by default) or revert to `local`. Document the choice in the PR description.

---

## Self-Review

**Spec coverage:**
- §2 missing modules → Tasks 2 (client), 3 (store), 8 (seed); `[cloud]` install → Task 2.
- §3 full protocol → Tasks 3–7 cover all ~30 methods (tenancy, findings, KG read, KG write, synopsis/explore/intent/stamps/monitoring).
- §4 decisions (supabase-py, user-JWT+RLS, inline embeddings, async-via-to_thread, seed_dev) → Tasks 2,3,8 + the async wrappers in 4–7.
- §5 file structure → exact paths in every task; no engine call-site changes.
- §6 cross-cutting rules → Global Constraints, enforced per method.
- §7 verified RPC signatures + accepted non-atomicity → match_* in Tasks 4/5; non-atomic delete/merge/find-or-create implemented as specified.
- §8 data flow → Task 9 live test exercises resolve_tenant→get_store→store.
- §9 error handling → not-found raise vs None (Tasks 4,5,7); record_access never raises (Task 7).
- §10 testing → unit (fake client, Tasks 3–7), live smoke (Task 9), manual E2E (Task 9).
- §11 RLS gate → Task 1 (first); creds/seed prerequisites → Tasks 8,9; rotate-secret noted in Task 1/PR.

**Placeholder scan:** every code step shows complete code; every test step shows real assertions; commands have expected output. No TBD/TODO.

**Type consistency:** `SupabaseStore(access_token, *, org_id)`, `_vec`, `_now_iso`, `_finding`/`_node`/`_edge` helpers, `_MAX_GROUNDED`/`LIST_*` constants, and `user_client`/`service_client` names are used identically across Tasks 2–9. Method signatures match `store/base.py` exactly. `match_kb_id` RPC param and `org_id` injection are consistent throughout. The `FakeSupabase` API (`.table`, `.rpc`, `.register_rpc`, `.tables`) defined in Task 3 is used unchanged in Tasks 4–7.
