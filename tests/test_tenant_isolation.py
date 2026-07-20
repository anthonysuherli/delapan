"""Isolation acceptance test (spec §H): two users, two orgs, zero bleed.

Enforcement line under test = the engine's explicit org scoping through the
Store: ``resolve_project``/``resolve_kb`` filter by ``org_id``, so the same
project/KB *names* resolve to distinct ``org_id``/``kb_id`` values per user,
and every finding read/write below is then scoped to the resolved ``kb_id``.

This runs entirely against ``fake_supabase``, which implements no Postgres
row-level security at all — it doesn't matter whether the real client would be
the RLS-scoped anon+JWT client or the RLS-bypassing service-role client, the
fake enforces neither. A pass here proves the engine's own tenant-scoping
logic (org-filtered resolution + kb_id-scoped queries) doesn't leak; it proves
nothing about live Postgres RLS policy correctness. RLS is separately audited
by ``scripts/rls_audit.py`` — a live run found and fixed 14 real gaps. Don't
over-trust this test as a stand-in for that audit.
"""

from __future__ import annotations

import pytest

import delapan.mcp.tenancy as tenancy_mod
from delapan.store import get_store
from tests.fake_supabase import FakeSupabase

ORGS = {"user-a": "org-a", "user-b": "org-b"}


def _finding_row(kb_id: str, title: str) -> dict:
    return {"kb_id": kb_id, "title": title, "content": "c", "category": "note", "confidence": 0.9}


@pytest.fixture()
def two_org_cloud(monkeypatch):
    """Cloud backend over one shared fake_supabase instance, `_org_for`
    answering from ORGS. Store wiring copied from tests/test_supabase_misc.py's
    `make_store` (the harness tests/test_curation_store_supabase.py imports):
    monkeypatch `delapan.store.supabase.user_client` to hand back the fake
    regardless of the token it's called with, so every `get_store(token, ...)`
    call — for either user — shares the same backing tables. That's the only
    way a cross-tenant bleed could actually show up in these assertions."""
    monkeypatch.setenv("DELAPAN_BACKEND", "cloud")
    fake = FakeSupabase()
    monkeypatch.setattr("delapan.store.supabase.user_client", lambda _token: fake)
    monkeypatch.setattr(tenancy_mod, "_org_for", lambda user_id: ORGS[user_id])
    return fake


@pytest.mark.asyncio
async def test_user_b_cannot_read_user_a_findings(two_org_cloud):
    ctx_a = tenancy_mod.resolve_tenant_for_token("user-a", "tok-a", "proj", "kb", create=True)
    store_a = get_store("tok-a", org_id=ctx_a.org_id)
    await store_a.insert_findings([_finding_row(ctx_a.kb_id, "A's secret")])

    ctx_b = tenancy_mod.resolve_tenant_for_token("user-b", "tok-b", "proj", "kb", create=True)
    store_b = get_store("tok-b", org_id=ctx_b.org_id)
    assert store_b.list_findings(ctx_b.kb_id)["findings"] == []  # no read bleed
    assert ctx_a.org_id != ctx_b.org_id  # same names, distinct tenants
    assert ctx_a.kb_id != ctx_b.kb_id


@pytest.mark.asyncio
async def test_user_b_cannot_write_into_user_a_kb(two_org_cloud):
    ctx_a = tenancy_mod.resolve_tenant_for_token("user-a", "tok-a", "proj", "kb", create=True)
    ctx_b = tenancy_mod.resolve_tenant_for_token("user-b", "tok-b", "proj", "kb", create=True)

    store_b = get_store("tok-b", org_id=ctx_b.org_id)
    await store_b.insert_findings([_finding_row(ctx_b.kb_id, "B's finding")])  # via B's own kb

    store_a = get_store("tok-a", org_id=ctx_a.org_id)
    assert store_a.list_findings(ctx_a.kb_id)["findings"] == []  # A's KB untouched
