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
