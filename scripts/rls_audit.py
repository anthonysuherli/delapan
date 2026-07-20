"""RLS audit — every tenant table must be org/user-scoped, by test not convention.

    pg_tables + pg_policies ──► evaluate() ──► gap list (empty = clean)

Catches the kg_schemas class of gap mechanically (spec §H). Read-only catalog
queries — safe against production. Usage: uv run python scripts/rls_audit.py
(requires DATABASE_URL).
"""

from __future__ import annotations

import asyncio
import sys

TENANT_TABLES = {
    "projects",
    "kbs",
    "findings",
    "kg_nodes",
    "kg_edges",
    "kg_schemas",
    "resolution_events",
    "explorations",
    "beta_members",
    "tracking_initiatives",
    "tracking_backlog",
    # org/user identity — 2026-07-20 widen (public schema has 29 tables, not 11)
    "orgs",
    "org_members",
    # api keys
    "api_keys",
    "api_key_usage",
    # chat
    "chat_threads",
    "chat_messages",
    # uploads
    "uploads",
    # kb feature tables
    "kb_synopsis",
    "kb_concepts",
    "drift_baselines",
    "bridges",
    "deepen_runs",
    "user_kb_relevance",
    # access audit
    "access_events",
    "access_rollup_daily",
    "access_requests",
    # the blind spot that started this widening: RLS was fully disabled
    "kg_communities",
    # duet
    "duet_reports",
}
_SCOPE_MARKERS = ("org_id", "auth.uid()")


def evaluate(tables: list[dict], policies: list[dict]) -> list[str]:
    gaps: list[str] = []
    by_name = {t["tablename"]: t for t in tables}
    for name in sorted(TENANT_TABLES):
        table = by_name.get(name)
        if table is None:
            gaps.append(f"{name}: table not found in public schema")
            continue
        if not table["rowsecurity"]:
            gaps.append(f"{name}: rowsecurity disabled")
        pols = [p for p in policies if p["tablename"] == name]
        if not any(p["cmd"] in ("SELECT", "ALL", "*") for p in pols):
            gaps.append(f"{name}: no SELECT policy")
        for p in pols:
            if p["cmd"] in ("INSERT", "UPDATE", "ALL", "*") and not p.get("with_check"):
                gaps.append(f"{name}: {p['cmd']} policy missing WITH CHECK")
            scope_text = " ".join(filter(None, (p.get("qual"), p.get("with_check"))))
            if scope_text and not any(m in scope_text for m in _SCOPE_MARKERS):
                gaps.append(f"{name}: {p['cmd']} policy not org/user scoped: {scope_text!r}")
    return gaps


async def _fetch() -> tuple[list[dict], list[dict]]:
    import asyncpg  # [cloud] extra

    from delapan.core.config import get_settings

    url = get_settings().database_url
    assert url, "DATABASE_URL required for the RLS audit"
    conn = await asyncpg.connect(url)
    try:
        tables = await conn.fetch(
            "select tablename, rowsecurity from pg_tables where schemaname = 'public'"
        )
        policies = await conn.fetch(
            "select tablename, cmd, qual, with_check from pg_policies where schemaname = 'public'"
        )
    finally:
        await conn.close()
    return [dict(r) for r in tables], [dict(r) for r in policies]


def main() -> None:
    tables, policies = asyncio.run(_fetch())
    gaps = evaluate(tables, policies)
    for gap in gaps:
        print(f"GAP  {gap}")
    print(f"{len(gaps)} gap(s) across {len(TENANT_TABLES)} tenant tables")
    sys.exit(1 if gaps else 0)


if __name__ == "__main__":
    main()
