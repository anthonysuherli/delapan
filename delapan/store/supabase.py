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
import logging
import uuid
from datetime import datetime, timezone

from delapan.core.clients.supabase import user_client

logger = logging.getLogger(__name__)

_MAX_GROUNDED = 50
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 1000


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

    def list_projects(self, *, include_archived: bool = False) -> list[dict]:
        """Projects + KBs with live-finding activity, via one RPC round-trip."""
        rows = self._c.rpc(
            "list_projects_with_activity",
            {"p_org_id": self._org_id, "p_include_archived": include_archived},
        ).execute().data or []

        out: list[dict] = []
        by_pid: dict[str, dict] = {}
        for r in rows:
            pid = r["project_id"]
            proj = by_pid.get(pid)
            if proj is None:
                proj = {
                    "project": r["project_name"],
                    "project_id": pid,
                    "archived_at": r["project_archived_at"],
                    "kbs": [],
                }
                by_pid[pid] = proj
                out.append(proj)
            if r["kb_id"] is None:
                continue  # project with no KBs — LEFT JOIN filler row
            proj["kbs"].append(
                {
                    "kb": r["kb_name"],
                    "kb_id": r["kb_id"],
                    "finding_count": int(r["finding_count"]),
                    "last_finding_at": r["last_finding_at"],
                    "archived_at": r["kb_archived_at"],
                }
            )
        return out

    def set_archived(
        self, *, project_id: str, kb_id: str | None = None, archived: bool
    ) -> dict:
        """Stamp/clear ``archived_at`` on a KB (or the project when kb_id is None)."""
        # The KB lookup is scoped by project_id too — same contract as the SQLite
        # tier: a (project, kb) pair that doesn't belong together must raise.
        table, row_id = ("kbs", kb_id) if kb_id else ("projects", project_id)
        q = (
            self._c.table(table).select("archived_at")
            .eq("id", row_id).eq("org_id", self._org_id)
        )
        if kb_id:
            q = q.eq("project_id", project_id)
        cur = q.limit(1).execute().data
        if not cur:
            raise RuntimeError(f"{table} {row_id!r} not found")

        current = cur[0].get("archived_at")
        if archived and current is not None:
            stamp = current  # idempotent — don't move the timestamp
        else:
            stamp = _now_iso() if archived else None
            res = (
                self._c.table(table).update({"archived_at": stamp})
                .eq("id", row_id).eq("org_id", self._org_id).execute()
            )
            # Prefer the value Postgres echoes back. A timestamptz round-trips
            # through PostgREST in its own format, so returning the Python-side
            # string here and the DB-side string on the idempotent path below
            # would make two archives of the same KB disagree.
            if res.data and archived:
                stamp = res.data[0].get("archived_at", stamp)

        return {
            "project_id": project_id,
            "kb_id": kb_id,
            "archived_at": stamp,
            "finding_count": self._live_finding_count(project_id, kb_id),
        }

    def _live_finding_count(self, project_id: str, kb_id: str | None) -> int:
        """Live (non-invalidated) findings in one KB, or across a project's KBs."""
        if kb_id:
            kb_ids = [kb_id]
        else:
            kb_ids = [
                r["id"]
                for r in (
                    self._c.table("kbs").select("id")
                    .eq("org_id", self._org_id).eq("project_id", project_id)
                    .execute().data
                )
            ]
        if not kb_ids:
            return 0
        res = (
            self._c.table("findings").select("id", count="exact")
            .in_("kb_id", kb_ids).is_("invalidated_at", "null")
            .limit(1).execute()
        )
        return res.count or 0

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

    def _row_payload(self, r: dict) -> dict:
        """The insert_findings enrichment, shared with supersede_finding: stamps
        org_id/status/created_at/valid_from and encodes embedding as vector text."""
        row = {
            "id": r.get("id") or uuid.uuid4().hex, "org_id": self._org_id,
            "kb_id": r.get("kb_id"), "title": r.get("title"), "content": r.get("content"),
            "category": r.get("category"), "confidence": r.get("confidence"),
            "tags": list(r.get("tags") or []), "provenance": list(r.get("provenance") or []),
            "status": "approved", "created_at": r.get("created_at") or _now_iso(),
            "valid_from": r.get("valid_from") or _now_iso(),
        }
        emb = r.get("embedding")
        if emb is not None:
            row["embedding"] = self._vec(list(emb))
        return row

    async def insert_findings(self, rows: list[dict]) -> list[str]:
        if not rows:
            return []
        payload = [self._row_payload(r) for r in rows]
        await asyncio.to_thread(lambda: self._c.table("findings").insert(payload).execute())
        return [row["id"] for row in payload]

    async def update_finding(
        self, kb_id: str, finding_id: str, *, content=None, confidence=None,
        provenance=None, embedding=None, title: str | None = None,
    ) -> None:
        """Partial in-place update; ``None`` fields are left out of the payload."""
        patch: dict = {}
        if content is not None:
            patch["content"] = content
        if confidence is not None:
            patch["confidence"] = confidence
        if provenance is not None:
            patch["provenance"] = list(provenance)
        if title is not None:
            patch["title"] = title
        if embedding is not None:
            patch["embedding"] = self._vec(list(embedding))
        if not patch:
            return
        await asyncio.to_thread(
            lambda: self._c.table("findings").update(patch)
            .eq("kb_id", kb_id).eq("id", finding_id).execute()
        )

    async def invalidate_finding(
        self, kb_id: str, finding_id: str, *, superseded_by: str | None = None
    ) -> None:
        """Retire a row in place — it leaves every read path but stays readable."""
        await asyncio.to_thread(
            lambda: self._c.table("findings")
            .update({"invalidated_at": _now_iso(), "superseded_by": superseded_by})
            .eq("kb_id", kb_id).eq("id", finding_id).execute()
        )

    async def supersede_finding(self, kb_id: str, target_id: str, new_row: dict) -> str:
        """Insert + retire in one server-side transaction. Returns the new id."""
        params = {
            "p_kb_id": kb_id, "p_target_id": target_id, "p_row": self._row_payload(new_row)
        }
        data = await asyncio.to_thread(
            lambda: self._c.rpc("supersede_finding", params).execute().data
        )
        return data if isinstance(data, str) else data[0]

    @staticmethod
    def _finding(row: dict) -> dict:
        return {
            "id": row["id"], "title": row["title"], "content": row["content"],
            "category": row["category"], "confidence": row["confidence"],
            "tags": row.get("tags") or [], "provenance": row.get("provenance") or [],
            "created_at": row["created_at"],
            "valid_from": row.get("valid_from"),
            "invalidated_at": row.get("invalidated_at"),
            "superseded_by": row.get("superseded_by"),
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

    def list_findings(self, kb_id, category=None, limit=None,
                      include_invalidated: bool = False) -> dict:
        n = min(limit or LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT)
        q = (self._c.table("findings")
             .select("id,title,category,confidence,tags,created_at", count="exact")
             .eq("kb_id", kb_id))
        if category:
            q = q.eq("category", category)
        if not include_invalidated:
            q = q.is_("invalidated_at", "null")
        res = q.order("created_at", desc=True).limit(n).execute()
        findings = [{"id": r["id"], "title": r["title"], "category": r["category"],
                     "confidence": r["confidence"], "tags": r.get("tags") or [],
                     "created_at": r["created_at"]} for r in res.data]
        total = int(res.count or 0)
        return {"count": len(findings), "total": total, "findings": findings}

    def count_findings(self, kb_id: str) -> int:
        """Exact LIVE finding count for `kb_id` (invalidated_at IS NULL)."""
        res = (self._c.table("findings").select("id", count="exact")
               .eq("kb_id", kb_id).is_("invalidated_at", "null").execute())
        return int(res.count or 0)

    def delete_finding(self, kb_id: str, finding_id: str) -> dict:
        self._c.table("findings").delete().eq("kb_id", kb_id).eq("id", finding_id).execute()
        return {"deleted": finding_id}

    # --- resolution event log -------------------------------------------------

    async def insert_resolution_events(self, kb_id: str, events: list[dict]) -> None:
        """Append resolution decision rows. Best-effort: an audit-log write must
        never break the caller, so failures are logged and swallowed."""
        if not events:
            return
        payload = [
            {
                "org_id": self._org_id, "kb_id": kb_id, "op": e.get("op"),
                "candidate_title": e.get("candidate_title"),
                "target_finding_id": e.get("target_finding_id"),
                "new_finding_id": e.get("new_finding_id"),
                "details": e.get("details"), "reason": e.get("reason") or "",
                "created_at": _now_iso(),
            }
            for e in events
        ]
        try:
            await asyncio.to_thread(
                lambda: self._c.table("resolution_events").insert(payload).execute()
            )
        except Exception:  # noqa: BLE001 — audit log is best-effort
            logger.warning("failed to write resolution_events for kb=%s", kb_id, exc_info=True)

    def list_resolution_events(self, kb_id: str, limit: int | None = None) -> list[dict]:
        """Most-recent resolution events for `kb_id`, newest first (cap 500)."""
        n = min(limit or 50, 500)
        return (
            self._c.table("resolution_events").select("*")
            .eq("kb_id", kb_id).order("created_at", desc=True).limit(n).execute().data
        ) or []

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
                seen.add(r["id"])
                out.append(r)
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
                        seen_e.add(er["id"])
                        all_edges.append(er)
                    new_nodes.add(er["source_node_id"])
                    new_nodes.add(er["target_node_id"])
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
        # Use count="exact" so totals are accurate even when PostgREST's default
        # row cap truncates the payload.  by_type/by_relation are aggregated over
        # the fetched page — for very large KBs a dedicated kg_stats RPC would be
        # the exact path, but that is deferred.
        node_res = (self._c.table("kg_nodes").select("type", count="exact")
                    .eq("kb_id", kb_id).execute())
        edge_res = (self._c.table("kg_edges").select("relation", count="exact")
                    .eq("kb_id", kb_id).execute())
        by_type: dict[str, int] = {}
        for r in node_res.data:
            key = r.get("type") or "unknown"
            by_type[key] = by_type.get(key, 0) + 1
        by_relation: dict[str, int] = {}
        for r in edge_res.data:
            key = r.get("relation") or "unknown"
            by_relation[key] = by_relation.get(key, 0) + 1
        return {"node_count": int(node_res.count or 0),
                "edge_count": int(edge_res.count or 0),
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
                self._merge_node(batch[key], props, grounded)
                ids.append(batch[key])
                continue
            existing = (self._c.table("kg_nodes").select("id")
                        .eq("kb_id", kb_id).eq("type", typ).eq("label", label)
                        .limit(1).execute().data)
            if existing:
                nid = existing[0]["id"]
                self._merge_node(nid, props, grounded)
            else:
                nid = uuid.uuid4().hex
                row: dict = {"id": nid, "org_id": self._org_id, "kb_id": kb_id, "type": typ,
                             "label": label, "properties": props,
                             "grounded_in": grounded[-_MAX_GROUNDED:], "aliases": [],
                             "merge_history": [], "created_at": _now_iso()}
                emb = nd.get("embedding")
                if emb is not None:
                    row["embedding"] = self._vec(list(emb))
                self._c.table("kg_nodes").insert(row).execute()
            batch[key] = nid
            ids.append(nid)
        return ids

    def _merge_node(self, node_id: str, props: dict, grounded: list[str]) -> None:
        rows = (self._c.table("kg_nodes").select("properties,grounded_in")
                .eq("id", node_id).limit(1).execute().data)
        if not rows:
            return
        ex_props = rows[0].get("properties") or {}
        ex_grounded = rows[0].get("grounded_in") or []
        merged_props = {**props, **ex_props}
        merged_grounded = list(dict.fromkeys([*ex_grounded, *grounded]))[-_MAX_GROUNDED:]
        (self._c.table("kg_nodes").update(
            {"properties": merged_props, "grounded_in": merged_grounded})
         .eq("id", node_id).execute())

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

    async def update_kg_node(self, kb_id: str, node_id: str, *, properties: dict,
                             grounded_in: list[str] | None = None,
                             embedding: list[float] | None = None,
                             label: str | None = None,
                             type: str | None = None) -> None:
        patch: dict = {"properties": properties}
        if grounded_in is not None:
            patch["grounded_in"] = list(grounded_in)[-_MAX_GROUNDED:]
        if label is not None:
            patch["label"] = label
        if type is not None:
            patch["type"] = type
        if embedding is not None:
            patch["embedding"] = self._vec(list(embedding))

        def _run() -> None:
            (self._c.table("kg_nodes").update(patch)
             .eq("id", node_id).eq("kb_id", kb_id).execute())
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

    def upsert_synopsis(self, kb_id: str, content: list, finding_count: int, model: str) -> None:
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
            rows = (self._c.table("kbs").select("init_offered_at")
                    .eq("id", kb_id).limit(1).execute().data)
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
            rows = (self._c.table("kbs").select("drift_offered_count")
                    .eq("id", kb_id).limit(1).execute().data)
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

    async def record_access(self, *, org_id: str, kb_id: str, surface: str,
                            targets: list, query_text: str | None = None) -> None:
        def _run() -> None:
            try:
                self._c.table("access_events").insert(
                    {"org_id": self._org_id, "kb_id": kb_id, "surface": surface,
                     "targets": list(targets), "query_text": query_text,
                     "created_at": _now_iso()}).execute()
            except Exception:  # noqa: BLE001 — monitoring must never break the caller
                pass
        await asyncio.to_thread(_run)
