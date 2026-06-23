"""Test that the cloud tier of resolve_tenant and resolve_store pass org_id to get_store.

C1 fix: tenancy.py must call _org_for(user_id) and forward the result to
get_store(token, org_id=...) on the cloud path.  Without this, SupabaseStore._org_id
is None, reads return nothing, and writes violate NOT NULL.

active_backend and get_store are imported locally inside resolve_tenant /
resolve_store as `from delapan.store import active_backend, get_store`, so we
must monkeypatch at the delapan.store module level, not on tenancy.
"""

from __future__ import annotations

from delapan.mcp import tenancy


class _FakeStore:
    """Minimal store stub: resolve_project and resolve_kb just return fixed ids."""

    def resolve_project(self, name, *, create):
        return ("org-xyz", "proj1")

    def resolve_kb(self, org_id, project_id, name, *, create):
        return "kb1"


def test_cloud_resolve_tenant_passes_org_id(monkeypatch):
    """resolve_tenant (cloud path) must forward org_id to get_store."""
    captured: dict = {}

    import delapan.store as store_mod

    monkeypatch.setattr(store_mod, "active_backend", lambda: "cloud")
    monkeypatch.setattr(tenancy, "_login", lambda: ("user-1", "jwt"))
    monkeypatch.setattr(tenancy, "_org_for", lambda uid: "org-xyz")

    def fake_get_store(token, org_id=None):
        captured["token"] = token
        captured["org_id"] = org_id
        return _FakeStore()

    monkeypatch.setattr(store_mod, "get_store", fake_get_store)

    ctx = tenancy.resolve_tenant("p", "k", create=False)

    assert captured["token"] == "jwt", f"expected token='jwt', got {captured['token']!r}"
    assert captured["org_id"] == "org-xyz", (
        f"org_id not passed to get_store — got {captured['org_id']!r}"
    )
    assert ctx.org_id == "org-xyz"
    assert ctx.kb_id == "kb1"


def test_cloud_resolve_store_passes_org_id(monkeypatch):
    """resolve_store (cloud path) must forward org_id to get_store."""
    captured: dict = {}

    import delapan.store as store_mod

    monkeypatch.setattr(store_mod, "active_backend", lambda: "cloud")
    monkeypatch.setattr(tenancy, "_login", lambda: ("user-2", "jwt2"))
    monkeypatch.setattr(tenancy, "_org_for", lambda uid: "org-abc")

    def fake_get_store(token, org_id=None):
        captured["token"] = token
        captured["org_id"] = org_id
        return _FakeStore()

    monkeypatch.setattr(store_mod, "get_store", fake_get_store)

    tenancy.resolve_store()

    assert captured["token"] == "jwt2"
    assert captured["org_id"] == "org-abc", (
        f"org_id not passed to get_store — got {captured['org_id']!r}"
    )
