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
