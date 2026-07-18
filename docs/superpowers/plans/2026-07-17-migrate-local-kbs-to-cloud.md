# Migrate remaining local KBs to cloud Supabase — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the two local-only projects (`demo/main`, `qwen-hackathon/main`) to cloud Supabase, generalizing the existing `actuary`-only migration script so it works for any local project.

**Architecture:** Generalize `scripts/port_actuary_to_cloud.py` (rename to `scripts/port_project_to_cloud.py`) by replacing its hardcoded `PROJECT_NAME`/`KBS` constants with a `--project` CLI argument and dynamic KB discovery against the local SQLite db. Everything else — dry-run-by-default, `--execute`, `--rollback`, the 32-hex→UUID transform, the PostgREST write path — is reused unchanged. Then run it for both remaining projects and extend the existing live smoke test to cover them.

**Tech Stack:** Python 3.12, `sqlite_vec`, `urllib` (no new dependencies), pytest.

**Spec:** `backend/docs/superpowers/specs/2026-07-17-cloud-remote-mcp-design.md` §7

## Global Constraints

- **All commands run from `backend/`** (the delapan git repo). Use `.venv/bin/python` / `.venv/bin/pytest`.
- **`SUPABASE_SECRET_KEY`** env var must be set (service-role secret) before running the script for real — never hardcode it, never commit it. It is separate from `SUPABASE_SERVICE_ROLE_KEY` used elsewhere in the app; the script reads its own env var by design (see its `_key()` function) — don't conflate the two.
- **Dry-run is the default.** Every real run of the script must be dry-run first, `--execute` second — this behavior must not change.
- **Local ids are 32-char hex** (`uuid.uuid4().hex`); cloud requires canonical dashed UUIDs. The existing `dash()` transform handles this — do not change it.
- **`ORG = "1a7d0aa5-587f-4420-985b-bafcf03bf04f"`** stays a module constant — every project ported by this script belongs to the same owner/org, matching how `actuary` was ported.
- **Local db path:** `~/.delapan/delapan.db` (the module-level `DB` constant, read only by `main()`, not by `build()`).

---

### Task 1: Generalize the migration script

**Files:**
- Rename + modify: `scripts/port_actuary_to_cloud.py` → `scripts/port_project_to_cloud.py`
- Test: `tests/test_port_project_to_cloud.py`

**Interfaces:**
- Produces: `build(conn: sqlite3.Connection, project_name: str) -> tuple[dict, list[dict], list[dict], list[dict], list[dict]]` — `(project_row, kb_rows, findings, nodes, edges)`, same shape as before, now parametrized by name instead of hardcoded to `"actuary"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_port_project_to_cloud.py`:

```python
from __future__ import annotations

import asyncio
import sqlite3

import pytest
import sqlite_vec


def _seed(tmp_path, monkeypatch, project_name: str, kb_name: str):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "t.db"))
    from delapan.core import config as cfg

    cfg.get_settings.cache_clear()
    from delapan.store import get_store

    store = get_store()
    org_id, project_id = store.resolve_project(project_name, create=True)
    kb_id = store.resolve_kb(org_id, project_id, kb_name, create=True)
    asyncio.run(
        store.insert_findings(
            [
                {
                    "kb_id": kb_id,
                    "title": "T1",
                    "content": "C1",
                    "category": "cat",
                    "confidence": 0.9,
                    "tags": ["a"],
                    "provenance": [],
                    "embedding": [0.1] * 1536,
                }
            ]
        )
    )
    return tmp_path / "t.db"


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    return conn


def test_build_discovers_kbs_for_named_project(tmp_path, monkeypatch):
    db_path = _seed(tmp_path, monkeypatch, "demo", "main")
    from scripts.port_project_to_cloud import build

    conn = _connect(db_path)
    project_row, kb_rows, findings, nodes, edges = build(conn, "demo")

    assert project_row["name"] == "demo"
    assert [kb["name"] for kb in kb_rows] == ["main"]
    assert len(findings) == 1
    assert findings[0]["title"] == "T1"
    assert len(findings[0]["id"]) == 36  # canonical dashed UUID
    assert findings[0]["embedding"].startswith("[")
    assert findings[0]["embedding"].count(",") == 1535  # 1536 floats
    assert nodes == []
    assert edges == []


def test_build_unknown_project_exits(tmp_path, monkeypatch):
    db_path = _seed(tmp_path, monkeypatch, "demo", "main")
    from scripts.port_project_to_cloud import build

    conn = _connect(db_path)
    with pytest.raises(SystemExit):
        build(conn, "does-not-exist")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_port_project_to_cloud.py -v`
Expected: `ModuleNotFoundError: No module named 'scripts.port_project_to_cloud'` (the file doesn't exist under this name yet).

- [ ] **Step 3: Rename and rewrite the script**

```bash
git mv scripts/port_actuary_to_cloud.py scripts/port_project_to_cloud.py
```

Replace the full contents of `scripts/port_project_to_cloud.py` with:

```python
#!/usr/bin/env python3
"""One-off migration: a local SQLite project (all its KBs) → cloud Supabase.

    ~/.delapan/delapan.db  ──(read + transform)──►  Supabase REST (PostgREST)

Dry-run by default (no writes). Pass --execute to write. The Supabase secret
key is read from env SUPABASE_SECRET_KEY (never hardcoded). On any write error,
prints the new project_id so the partial insert can be rolled back with
--rollback <project_uuid>.

Transforms:
  * 32-hex ids → canonical UUID (dashes), applied consistently to every id and
    every reference (grounded_in, edge source/target) so integrity holds.
  * org_id → the cloud org; project_id/kb_id → fresh cloud UUIDs.
  * cloud-required fields filled: findings.status, kg_nodes.aliases/merge_history,
    kbs.published/retrieval_miss_streak.
  * embeddings read from vec_findings/vec_kg_nodes, written as pgvector text.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
import sys
import urllib.error
import urllib.request
import uuid as uuidlib

import sqlite_vec

DB = os.path.expanduser("~/.delapan/delapan.db")
BASE = "https://gunqbyddzuwzpncfigro.supabase.co/rest/v1"
ORG = "1a7d0aa5-587f-4420-985b-bafcf03bf04f"
BATCH = 25


def dash(h: str) -> str:
    """32-char hex → canonical 8-4-4-4-12 UUID. Pass through anything else."""
    h = (h or "").strip()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}" if len(h) == 32 else h


def vec_to_str(blob: bytes | None) -> str | None:
    if not blob:
        return None
    floats = struct.unpack(f"<{len(blob) // 4}f", blob)
    return "[" + ",".join(repr(x) for x in floats) + "]"


def jloads(v, default):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return v  # raw string content


def _key() -> str:
    k = os.environ.get("SUPABASE_SECRET_KEY")
    if not k:
        sys.exit("set SUPABASE_SECRET_KEY env var")
    return k


def _req(path: str, *, method: str, key: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}/{path}",
        data=data,
        method=method,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    with urllib.request.urlopen(req) as r:
        return r.status


def post(table: str, rows: list[dict], key: str, execute: bool) -> int:
    if not rows or not execute:
        return len(rows)
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        try:
            _req(table, method="POST", key=key, body=chunk)
            done += len(chunk)
        except urllib.error.HTTPError as e:
            print(f"  ERROR {table} [{i}:{i+len(chunk)}]: {e.code} {e.read().decode()[:400]}",
                  file=sys.stderr)
            raise
    return done


def rollback(project_uuid: str, key: str) -> None:
    """Delete everything tied to a ported project (children first)."""
    kb_rows = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                f"{BASE}/kbs?select=id&project_id=eq.{project_uuid}",
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
            )
        ).read()
    )
    kb_ids = [r["id"] for r in kb_rows]
    for kb in kb_ids:
        for t in ("kg_edges", "kg_nodes", "findings", "kb_synopsis"):
            _req(f"{t}?kb_id=eq.{kb}", method="DELETE", key=key)
    _req(f"kbs?project_id=eq.{project_uuid}", method="DELETE", key=key)
    _req(f"projects?id=eq.{project_uuid}", method="DELETE", key=key)
    print(f"rolled back project {project_uuid} ({len(kb_ids)} kbs)")


def build(conn: sqlite3.Connection, project_name: str):
    conn.row_factory = sqlite3.Row
    proj_uuid = str(uuidlib.uuid4())
    local_proj = conn.execute(
        "SELECT id, created_at FROM projects WHERE name=?", (project_name,)
    ).fetchone()
    if not local_proj:
        sys.exit(f"no local project named {project_name!r}")

    kb_names = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM kbs WHERE project_id=? ORDER BY name", (local_proj["id"],)
        )
    ]
    if not kb_names:
        sys.exit(f"local project {project_name!r} has no KBs")

    kb_map: dict[str, str] = {}  # local kb_id -> new cloud uuid
    kb_rows: list[dict] = []
    for name in kb_names:
        row = conn.execute(
            "SELECT k.id, k.created_at FROM kbs k WHERE k.project_id=? AND k.name=?",
            (local_proj["id"], name),
        ).fetchone()
        new_id = str(uuidlib.uuid4())
        kb_map[row["id"]] = new_id
        kb_rows.append({
            "id": new_id, "org_id": ORG, "project_id": proj_uuid, "name": name,
            "published": False, "created_at": row["created_at"],
            "retrieval_miss_streak": 0,
        })

    # default_kb_id FKs into kbs, which FK back into projects — circular, so the
    # project goes in with a null default and is patched after the kbs exist.
    project_row = {
        "id": proj_uuid, "org_id": ORG, "name": project_name, "default_kb_id": None,
        "created_at": local_proj["created_at"],
    }

    findings, nodes, edges = [], [], []
    for local_kb, cloud_kb in kb_map.items():
        for f in conn.execute("SELECT * FROM findings WHERE kb_id=?", (local_kb,)):
            emb = conn.execute(
                "SELECT embedding FROM vec_findings WHERE finding_id=?", (f["id"],)
            ).fetchone()
            findings.append({
                "id": dash(f["id"]), "org_id": ORG, "kb_id": cloud_kb,
                "title": f["title"] or "", "content": f["content"] or "",
                "category": f["category"], "confidence": f["confidence"],
                "tags": jloads(f["tags"], []), "provenance": jloads(f["provenance"], []),
                "embedding": vec_to_str(emb["embedding"]) if emb else None,
                "created_at": f["created_at"], "status": "approved",
            })
        for n in conn.execute("SELECT * FROM kg_nodes WHERE kb_id=?", (local_kb,)):
            emb = conn.execute(
                "SELECT embedding FROM vec_kg_nodes WHERE node_id=?", (n["id"],)
            ).fetchone()
            nodes.append({
                "id": dash(n["id"]), "org_id": ORG, "kb_id": cloud_kb,
                "type": n["type"] or "concept", "label": n["label"] or "",
                "properties": jloads(n["properties"], {}),
                "grounded_in": [dash(x) for x in jloads(n["grounded_in"], [])],
                "embedding": vec_to_str(emb["embedding"]) if emb else None,
                "created_at": n["created_at"], "aliases": [], "merge_history": [],
            })
        for e in conn.execute("SELECT * FROM kg_edges WHERE kb_id=?", (local_kb,)):
            edges.append({
                "id": dash(e["id"]), "org_id": ORG, "kb_id": cloud_kb,
                "source_node_id": dash(e["source_node_id"]),
                "target_node_id": dash(e["target_node_id"]),
                "relation": e["relation"] or "related_to",
                "properties": jloads(e["properties"], {}),
                "grounded_in": [dash(x) for x in jloads(e["grounded_in"], [])],
                "created_at": e["created_at"],
            })
    return project_row, kb_rows, findings, nodes, edges


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="local project name to port")
    ap.add_argument("--execute", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--rollback", metavar="PROJECT_UUID", help="delete a ported project")
    args = ap.parse_args()
    key = _key()

    if args.rollback:
        rollback(args.rollback, key)
        return
    if not args.project:
        sys.exit("--project is required (unless using --rollback)")

    conn = sqlite3.connect(DB)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    project_row, kb_rows, findings, nodes, edges = build(conn, args.project)

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"=== {mode}: {args.project} → cloud ===")
    print(f"project {project_row['id']}  org {ORG}")
    for kb in kb_rows:
        nf = sum(1 for f in findings if f["kb_id"] == kb["id"])
        nn = sum(1 for n in nodes if n["kb_id"] == kb["id"])
        ne = sum(1 for e in edges if e["kb_id"] == kb["id"])
        print(f"  kb {kb['name']:14} {kb['id']}  f={nf} n={nn} e={ne}")
    fe = sum(1 for f in findings if f["embedding"])
    ne_ = sum(1 for n in nodes if n["embedding"])
    print(f"totals: findings={len(findings)} (emb {fe})  nodes={len(nodes)} (emb {ne_})  "
          f"edges={len(edges)}")

    # sanity: every node/edge reference resolves to a ported finding/node
    fids = {f["id"] for f in findings}
    nids = {n["id"] for n in nodes}
    dangling_g = sum(1 for n in nodes for g in n["grounded_in"] if g not in fids)
    dangling_e = sum(1 for e in edges if e["source_node_id"] not in nids
                     or e["target_node_id"] not in nids)
    print(f"refs: grounded_in→missing-finding={dangling_g}  edge→missing-node={dangling_e}")

    if not args.execute:
        sample = findings[0] if findings else {}
        print("\nsample finding (transformed):")
        print(json.dumps({k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80
                              else (f"[vec {v.count(',')+1}]" if k == 'embedding' and v else v))
                          for k, v in sample.items()}, indent=2)[:900])
        print("\n(dry-run — nothing written; re-run with --execute)")
        return

    try:
        print("\nwriting…")
        print("  project:", post("projects", [project_row], key, True))
        print("  kbs:", post("kbs", kb_rows, key, True))
        if kb_rows:  # now that kbs exist, point the project's default at the first
            _req(f"projects?id=eq.{project_row['id']}", method="PATCH", key=key,
                 body={"default_kb_id": kb_rows[0]["id"]})
            print("  project.default_kb_id ->", kb_rows[0]["name"])
        print("  findings:", post("findings", findings, key, True))
        print("  kg_nodes:", post("kg_nodes", nodes, key, True))
        print("  kg_edges:", post("kg_edges", edges, key, True))
        print(f"\nDONE. project_id={project_row['id']}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAILED: {exc}", file=sys.stderr)
        print(f"roll back with: --rollback {project_row['id']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

Changes from the original: `PROJECT_NAME`/`KBS` constants removed; `build()` takes `project_name` and discovers that project's KBs by querying `kbs` directly instead of a hardcoded list (so the "warn: local kb not found" branch — dead once KBs are discovered rather than guessed — is gone); `main()` gains a required `--project` argument and uses it in place of the old hardcoded string in every print statement.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_port_project_to_cloud.py -v`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/port_project_to_cloud.py tests/test_port_project_to_cloud.py
git rm --cached scripts/port_actuary_to_cloud.py 2>/dev/null  # no-op if git mv already staged it
git commit -m "refactor: generalize the actuary cloud-port script to any local project"
```

---

### Task 2: Port `demo` and `qwen-hackathon` to cloud

**Files:** none (operational — runs the script from Task 1 against the real local db and the real Supabase project).
**Prerequisite:** `SUPABASE_SECRET_KEY` set in your shell (the service-role secret; check `backend/.env` for `SUPABASE_SERVICE_ROLE_KEY` and export it as `SUPABASE_SECRET_KEY` if you don't already have this set separately).

- [ ] **Step 1: Dry-run for `demo`**

Run: `SUPABASE_SECRET_KEY=<your key> .venv/bin/python scripts/port_project_to_cloud.py --project demo`
Expected output shape:
```
=== DRY-RUN: demo → cloud ===
project <uuid>  org 1a7d0aa5-587f-4420-985b-bafcf03bf04f
  kb main            <uuid>  f=28 n=0 e=0
totals: findings=28 (emb 28)  nodes=0 (emb 0)  edges=0
refs: grounded_in→missing-finding=0  edge→missing-node=0

sample finding (transformed):
{...}

(dry-run — nothing written; re-run with --execute)
```
Confirm `findings=28`, `dangling_g=0`, `dangling_e=0` before proceeding — a nonzero dangling count means a data-integrity problem to investigate before writing.

- [ ] **Step 2: Execute for `demo`**

Run: `SUPABASE_SECRET_KEY=<your key> .venv/bin/python scripts/port_project_to_cloud.py --project demo --execute`
Expected: ends with `DONE. project_id=<uuid>` — save that uuid in case a rollback is needed (`--rollback <uuid>`).

- [ ] **Step 3: Dry-run for `qwen-hackathon`**

Run: `SUPABASE_SECRET_KEY=<your key> .venv/bin/python scripts/port_project_to_cloud.py --project qwen-hackathon`
Expected: same shape as Step 1, with `findings=24`.

- [ ] **Step 4: Execute for `qwen-hackathon`**

Run: `SUPABASE_SECRET_KEY=<your key> .venv/bin/python scripts/port_project_to_cloud.py --project qwen-hackathon --execute`
Expected: ends with `DONE. project_id=<uuid>`.

- [ ] **Step 5: Extend the live smoke test**

Read `tests/test_supabase_live.py` (existing `test_live_reads_actuary`). Add:

```python
def test_live_reads_demo():
    from delapan.mcp.tenancy import resolve_tenant
    from delapan.store import get_store

    ctx = resolve_tenant("demo", "main", create=False)
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    findings = store.list_findings(ctx.kb_id)
    assert findings["count"] == 28


def test_live_reads_qwen_hackathon():
    from delapan.mcp.tenancy import resolve_tenant
    from delapan.store import get_store

    ctx = resolve_tenant("qwen-hackathon", "main", create=False)
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    findings = store.list_findings(ctx.kb_id)
    assert findings["count"] == 24
```

- [ ] **Step 6: Run the live smoke test**

Run: `RUN_CLOUD_TESTS=1 .venv/bin/pytest tests/test_supabase_live.py -v`
Expected: `3 passed` (the pre-existing actuary test plus the two new ones).

- [ ] **Step 7: Commit**

```bash
git add tests/test_supabase_live.py
git commit -m "test: verify demo and qwen-hackathon KBs are live on cloud Supabase"
```
