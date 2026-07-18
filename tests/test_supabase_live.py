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


def test_live_reads_demo():
    from delapan.mcp.tenancy import resolve_tenant
    from delapan.store import get_store

    ctx = resolve_tenant("demo", "main", create=False)
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    assert store.count_findings(ctx.kb_id) == 28


def test_live_reads_qwen_hackathon():
    from delapan.mcp.tenancy import resolve_tenant
    from delapan.store import get_store

    ctx = resolve_tenant("qwen-hackathon", "main", create=False)
    store = get_store(ctx.access_token, org_id=ctx.org_id)
    assert store.count_findings(ctx.kb_id) == 24
