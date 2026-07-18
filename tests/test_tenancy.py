from __future__ import annotations


def test_resolve_tenant_local_creates_by_name(monkeypatch, tmp_path):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "t.db"))
    from delapan.core import config as cfg

    cfg.get_settings.cache_clear()
    from delapan.mcp.tenancy import resolve_tenant

    ctx = resolve_tenant("myrepo", "main", create=True)
    assert ctx.org_id == "local"
    assert ctx.kb_id
    assert ctx.access_token == ""


def test_resolve_tenant_for_token_skips_login(monkeypatch):
    from delapan.mcp import tenancy

    monkeypatch.setattr(tenancy, "_org_for", lambda user_id: "org-123")

    calls = {}

    class FakeStore:
        def resolve_project(self, name, *, create):
            calls["resolve_project"] = (name, create)
            return "org-123", "proj-1"

        def resolve_kb(self, org_id, project_id, name, *, create):
            calls["resolve_kb"] = (org_id, project_id, name, create)
            return "kb-1"

    def fake_get_store(token, *, org_id):
        calls["get_store"] = (token, org_id)
        return FakeStore()

    monkeypatch.setattr("delapan.store.get_store", fake_get_store)

    ctx = tenancy.resolve_tenant_for_token("user-9", "tok-abc", "demo", "main", create=False)

    assert ctx.user_id == "user-9"
    assert ctx.org_id == "org-123"
    assert ctx.project_id == "proj-1"
    assert ctx.kb_id == "kb-1"
    assert ctx.access_token == "tok-abc"
    assert calls["get_store"] == ("tok-abc", "org-123")
    assert calls["resolve_project"] == ("demo", False)
    assert calls["resolve_kb"] == ("org-123", "proj-1", "main", False)


def test_resolve_store_for_token_skips_login(monkeypatch):
    from delapan.mcp import tenancy

    monkeypatch.setattr(tenancy, "_org_for", lambda user_id: "org-123")
    sentinel = object()
    monkeypatch.setattr(
        "delapan.store.get_store",
        lambda token, *, org_id: sentinel if (token, org_id) == ("tok-abc", "org-123") else None,
    )

    store = tenancy.resolve_store_for_token("user-9", "tok-abc")
    assert store is sentinel
