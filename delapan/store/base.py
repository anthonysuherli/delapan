"""The Store protocol — the engine's single seam over persistence.

    engine ──► Store ──► {Supabase (cloud), SQLite (local)}

delapan is split into a free/local tier (SQLite + sqlite-vec, single user, no
auth) and a paid/cloud tier (Supabase: Postgres + pgvector + GoTrue + RLS). The
engine talks to this protocol instead of any one backend so the two tiers share
one engine. Return shapes are plain dicts / lists of dicts matching today's
Supabase rows; no ORM, no backend-specific objects cross this boundary.

This file defines the contract ONLY. `supabase.py` is the cloud implementation;
the engine call sites are flipped to `get_store(...)` in a later task.
"""

from __future__ import annotations

from typing import Protocol


class Store(Protocol):
    """Persistence surface the engine depends on. Implementations are tier-specific."""

    # --- findings — hot path -------------------------------------------------

    async def match_findings(
        self,
        kb_id: str | None,
        query_embedding: list[float],
        match_count: int,
        min_similarity: float,
        categories: list[str] | None = None,
    ) -> list[dict]:
        """Vector-search findings; rows carry a `similarity` field.

        `kb_id` scopes to one KB; `kb_id=None` searches every KB in the store's
        org (the load-bearing org filter — SQLite's synthetic ``"local"`` or the
        SupabaseStore's injected ``org_id``). `categories`, when given, restricts
        results to those `category` values.
        """
        ...

    async def insert_findings(self, rows: list[dict]) -> list[str]:
        """Insert already-embedded finding rows; return the new ids in order."""
        ...

    async def update_finding(
        self,
        kb_id: str,
        finding_id: str,
        *,
        content=None,
        confidence=None,
        provenance=None,
        embedding=None,
        title: str | None = None,
    ) -> None:
        """Partial in-place update, keeping the id STABLE (so KG `grounded_in`
        references stay valid). Every field is optional; ``None`` means KEEP the
        current value. Re-indexes the embedding when one is given."""
        ...

    async def invalidate_finding(
        self, kb_id: str, finding_id: str, *, superseded_by: str | None = None
    ) -> None:
        """Retire a finding in place (no insert): stamp `invalidated_at` and an
        optional forward pointer. It disappears from match/list/count but stays
        readable via `get_finding`. Findings are never deleted to dedup them."""
        ...

    async def supersede_finding(self, kb_id: str, target_id: str, new_row: dict) -> str:
        """Insert `new_row` and retire `target_id` pointing at it, atomically.
        Returns the new finding id. Raises if the target is absent or already
        retired — leaving the KB unchanged."""
        ...

    def get_finding(self, kb_id: str, finding_id: str) -> dict:
        """One finding scoped to `kb_id`. Raises if not found."""
        ...

    def get_finding_global(self, finding_id: str) -> dict:
        """One finding by its global id, ignoring KB scope. Raises if not found.

        ``findings.id`` is globally unique, so this resolves cross-KB
        ``grounded_in`` citations (e.g. a unified graph whose nodes cite findings
        owned by the source KBs). Org isolation still applies on the cloud tier
        via RLS; on the local tier the single synthetic org makes it a no-op.
        """
        ...

    def list_findings(
        self,
        kb_id: str,
        category: str | None = None,
        limit: int | None = None,
        include_invalidated: bool = False,
    ) -> dict:
        """Most-recent findings in `kb_id`. Returns {"count", "total", "findings"}.

        Live rows only unless `include_invalidated` — retired rows stay
        reachable for history/audit, never for retrieval. ``count`` is rows
        returned (bounded by `limit`); ``total`` is rows matching `kb_id` +
        `category` + the live-only filter, regardless of `limit`."""
        ...

    def delete_finding(self, kb_id: str, finding_id: str) -> dict:
        """Delete one finding from `kb_id`. Returns {"deleted": finding_id}."""
        ...

    def count_findings(self, kb_id: str) -> int:
        """Exact number of LIVE findings in `kb_id` (uncapped, unlike list_findings)."""
        ...

    # --- synopsis spine ------------------------------------------------------

    def load_synopsis(self, kb_id: str) -> dict | None:
        """Current synopsis row for `kb_id`, or None."""
        ...

    def upsert_synopsis(
        self, kb_id: str, content: list[dict], finding_count: int, model: str
    ) -> None:
        """Write the KB's synopsis spine (one current row per KB)."""
        ...

    # --- exploration row lifecycle -------------------------------------------

    def create_exploration(self, org_id: str, kb_id: str, prompt: str) -> str:
        """Insert a pending exploration row; return its id."""
        ...

    def update_exploration(self, exploration_id: str, **patch) -> None:
        """Patch an exploration row (status / completed_at / finding_ids / error)."""
        ...

    def get_exploration(self, exploration_id: str) -> dict | None:
        """Read an exploration row, or None if missing."""
        ...

    # --- tenancy — find-or-create by name ------------------------------------

    def resolve_project(self, name: str, *, create: bool) -> tuple[str, str]:
        """Resolve the named project → (org_id, project_id)."""
        ...

    def resolve_kb(self, org_id: str, project_id: str, name: str, *, create: bool) -> str:
        """Resolve the named KB within (org_id, project_id) → kb_id."""
        ...

    def list_projects(self, *, include_archived: bool = False) -> list[dict]:
        """All of the caller's projects with their KBs, for client discovery.

        Returns ``[{project, project_id, archived_at, kbs: [{kb, kb_id,
        finding_count, last_finding_at, archived_at}]}]``. ``finding_count`` and
        ``last_finding_at`` cover live findings only (``invalidated_at IS NULL``).

        ``include_archived`` is a single switch over both tiers: False (default)
        omits archived projects *and* archived KBs of active projects; True
        returns everything, each row carrying its ``archived_at`` so the caller
        can tell them apart. Cloud scopes to the authenticated user's org."""
        ...

    def set_archived(
        self, *, project_id: str, kb_id: str | None = None, archived: bool
    ) -> dict:
        """Archive or unarchive a project (``kb_id=None``) or a single KB.

        Returns ``{"project_id", "kb_id", "archived_at", "finding_count"}``.
        ``finding_count`` counts live findings (``invalidated_at IS NULL``) — for
        the whole project when ``kb_id`` is None. Idempotent: archiving an
        already-archived target returns the existing stamp. Raises if the target
        does not exist — this never creates on demand.
        """
        ...

    # --- activity knowledge graph --------------------------------------------
    # Nodes/edges live in their own per-KB namespace (`kb_id` = the reserved
    # activity KB). Dedupe is by exact ``(type, normalized label)`` so a repo or
    # file resolves to one stable node; a stored label `embedding` is used only
    # for semantic subgraph seeding, never for dedupe.

    async def upsert_kg_nodes(self, kb_id: str, nodes: list[dict]) -> list[str]:
        """Insert-or-merge nodes; return their ids in input order.

        Each row carries ``org_id, type, label, properties, grounded_in,
        embedding``. A row whose ``(type, normalized label)`` already exists in
        ``kb_id`` reuses that node (merging ``properties`` + ``grounded_in``);
        duplicates within the batch resolve to the same id."""
        ...

    async def upsert_kg_edges(self, kb_id: str, edges: list[dict]) -> int:
        """Insert edges, skipping duplicates; return the number newly inserted.

        Each row carries ``org_id, source_node_id, target_node_id, relation,
        properties, grounded_in``. An edge equal to an existing one on
        ``(source, target, relation)`` is skipped (idempotent re-capture)."""
        ...

    async def update_kg_node(
        self,
        kb_id: str,
        node_id: str,
        *,
        properties: dict,
        grounded_in: list[str] | None = None,
        embedding: list[float] | None = None,
        label: str | None = None,
        type: str | None = None,
    ) -> None:
        """Overwrite a node's payload (unlike upsert_kg_nodes, which merges with
        existing-wins). `properties` replaces wholesale; `grounded_in` replaces when
        given; `embedding` re-indexes the vector when given; `label`/`type` rename
        the node when given (the dedupe key changes — re-embedding is optional).
        Used to re-distill a concept's body/confidence/version in place."""
        ...

    def delete_kg_node(self, kb_id: str, node_id: str) -> dict:
        """Delete one node from `kb_id` plus its vector row and every incident
        edge. Returns ``{"deleted": bool, "removed_edge_ids": [...]}`` — deleted
        is False (with no edge ids) when the node is absent."""
        ...

    def delete_kg_edge(self, kb_id: str, edge_id: str) -> dict:
        """Delete one edge from `kb_id`. Returns ``{"deleted": bool}``."""
        ...

    async def match_kg_nodes(
        self,
        kb_id: str,
        query_embedding: list[float],
        match_count: int,
        min_similarity: float,
    ) -> list[dict]:
        """Semantic node search in `kb_id`; rows carry a `similarity` field."""
        ...

    def get_kg_subgraph(
        self,
        kb_id: str,
        *,
        seed_node_ids: list[str] | None = None,
        node_cap: int = 200,
        edge_cap: int = 600,
        depth: int = 1,
    ) -> dict:
        """Return ``{"nodes", "edges"}`` for `kb_id`.

        With ``seed_node_ids`` → BFS from those nodes up to ``depth`` hops,
        capped by ``node_cap``/``edge_cap``. Without → the whole graph, capped.
        ``depth=1`` is the original one-hop behaviour."""
        ...

    def list_kg_nodes(
        self, kb_id: str, *, type: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """Most-recent nodes in `kb_id` (optionally one type). Rows carry
        ``id, type, label, properties, created_at``."""
        ...

    def get_kg_node(self, kb_id: str, node_id: str) -> dict | None:
        """One node by id within `kb_id`, or None. Row carries the full decoded
        ``id, type, label, properties, grounded_in, created_at`` — the authoritative
        read for re-distilling a concept in place (versus a capped, recency-windowed
        list)."""
        ...

    def kg_stats(self, kb_id: str) -> dict:
        """Graph totals + breakdowns: ``node_count, edge_count, by_type,
        by_relation``."""
        ...

    def clear_kg(self, kb_id: str) -> None:
        """Delete all nodes and edges for `kb_id` (edges first — FK constraint).

        Used by ``build_graph(rebuild=True)`` before a full rebuild. Scoped
        strictly to ``kb_id`` so other KBs in the same org are never touched."""
        ...

    # --- KG intent schema (versioned, approved target ontology) ---------------
    # Stored in `kg_schemas` (one row per version, newest = active). `set`
    # inserts the next version; `get` reads the highest. The KG builder reads
    # `get` to steer extraction; a view pairs INTENT with EMERGENT ontology.

    def get_kg_intent(self, kb_id: str) -> dict | None:
        """The KB's highest-version approved KG intent schema, or None if never set.

        Returns a dict with at least ``{version, schema}``."""
        ...

    def set_kg_intent(self, org_id: str, kb_id: str, schema: dict) -> dict:
        """Persist an approved schema as the next version (never overwrites history).

        Version is ``max(existing version for kb_id) + 1`` (first set = 1).
        Returns a dict with at least ``{version, schema}``."""
        ...

    # --- first-run offer-once stamp ------------------------------------------

    def get_init_offered(self, kb_id: str) -> bool:
        """Return True iff the KG schema wizard has already been offered for `kb_id`.

        Reads ``init_offered_at``; returns False when the column is absent or
        null (pre-migration, or wizard not yet offered)."""
        ...

    def mark_init_offered(self, kb_id: str) -> None:
        """Stamp `kb_id` with the time the KG schema wizard was offered.

        Called once — by ``delapan_mark_init_offered`` — after the first-run
        schema offer is surfaced.  Prevents re-offering on subsequent sessions.
        No-op if the column is absent (local tier before migration 0007)."""
        ...

    # --- schema-drift offer debounce -----------------------------------------
    # The residual node count stamped at the last drift offer. Powers the re-arm
    # gate in `drift.assess_drift`: a declined drift offer only re-surfaces once
    # residual grows past this baseline by `rearm_delta`.

    def get_drift_marker(self, kb_id: str) -> int:
        """Residual count stamped at the last drift offer for `kb_id` (0 if never).

        Best-effort: returns 0 when the backing column is absent."""
        ...

    def set_drift_marker(self, kb_id: str, count: int) -> None:
        """Stamp the residual count at which a drift offer was surfaced for `kb_id`.

        Called once per offer so the next session doesn't re-nag until drift
        intensifies. No-op if the backing column is absent."""
        ...

    # --- monitoring — best-effort --------------------------------------------

    async def record_access(
        self,
        *,
        org_id: str,
        kb_id: str,
        surface: str,
        targets: list,
        query_text: str | None = None,
        coverage: str | None = None,
        band_counts: dict | None = None,
    ) -> None:
        """Append one access event. Best-effort by contract: **must never raise**.

        `coverage` is the rich/sparse/gap verdict for `query_text`, `band_counts`
        the per-band hit counts — the curation flywheel's ground truth. `targets`
        is accepted for Protocol compatibility and is not persisted by the
        query-level row; per-target fan-out rows can be added later without a
        schema change."""
        ...

    # --- curation flywheel ---------------------------------------------------
    # Backlog of gap/sparse queries, materialized at write time. All CRUD-dumb:
    # every transition rule lives in `core/curation/recorder.py`, once, not per
    # tier. All async — the cloud tier's postgrest calls go through to_thread so
    # a background recording never blocks the event loop.

    async def match_curation_topics(
        self,
        kb_id: str,
        query_embedding: list[float],
        match_count: int,
        min_similarity: float,
    ) -> list[dict]:
        """Cosine KNN over this KB's topics; rows carry `similarity`.

        Row shape: `id, query_text, query_norm, coverage, recurrence, first_seen,
        last_seen, consumed_at, resolved_at, similarity`."""
        ...

    async def upsert_curation_topic(self, row: dict) -> str:
        """Insert a topic, or increment the existing one on `(kb_id, query_norm)`.

        Atomic: the conflict target is a unique index and the increment happens in
        SQL, so concurrent recordings of the same query can never double-insert or
        lose an update. On conflict: `recurrence += 1`, `last_seen`/`coverage`
        refreshed, `consumed_at`/`resolved_at` cleared. `row` carries `org_id,
        kb_id, query_text, query_norm, embedding, coverage, seen_at`. Returns the
        topic id."""
        ...

    async def bump_curation_topic(
        self, kb_id: str, topic_id: str, *, coverage: str, seen_at: str
    ) -> None:
        """Increment `recurrence` in SQL and refresh `last_seen`/`coverage`,
        clearing `consumed_at`/`resolved_at` — the vector-hit path's counterpart to
        `upsert_curation_topic`'s conflict arm."""
        ...

    async def update_curation_topic(self, kb_id: str, topic_id: str, **patch) -> None:
        """Patch stamp columns (`consumed_at`, `resolved_at`). Values are written
        verbatim; `None` clears."""
        ...

    async def list_curation_topics(
        self, kb_id: str, *, include_closed: bool = False, limit: int | None = None
    ) -> list[dict]:
        """Topics for `kb_id`. Open-only by default (`consumed_at IS NULL AND
        resolved_at IS NULL`); ranking is the caller's job (`rank_backlog`)."""
        ...

    async def prune_access_events(self, kb_id: str, older_than_iso: str) -> None:
        """Delete this KB's access events older than `older_than_iso`.
        Best-effort: never raises."""
        ...

    # --- resolution event log (memory write decisions) -----------------------
    # Append-only observability for the mem0-style resolver: one row per applied
    # decision (ADD/UPDATE/NOOP/SUPERSEDE). Never load-bearing for retrieval.

    async def insert_resolution_events(self, kb_id: str, events: list[dict]) -> None:
        """Append resolution decision rows. Best-effort by contract.

        Each row carries ``op, candidate_title, target_finding_id, new_finding_id,
        details, reason``; ``op`` is one of ADD/UPDATE/NOOP/SUPERSEDE. ``details``
        is an op-specific JSON blob. No-op on an empty list."""
        ...

    def list_resolution_events(self, kb_id: str, limit: int | None = None) -> list[dict]:
        """Most-recent resolution events in ``kb_id`` (newest first). Rows carry
        ``id, op, candidate_title, target_finding_id, new_finding_id, details,
        reason, created_at``. ``limit`` defaults to 50; hard-capped at 500."""
        ...
