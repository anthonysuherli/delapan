"""Shared tenancy resolution for the local HTTP routes.

    {project}/{kb} path params ──► resolve_tenant(create=False) ──► (ctx, store)
                                          │ missing
                                          └──► HTTPException 404

Every KB-scoped route goes through `resolve_kb_or_404` so the surface has one
not-found behaviour: names never create tenants over HTTP (unlike the MCP
explore tool) — the control panel reads/mutates existing KBs only.
"""

from __future__ import annotations

from fastapi import HTTPException

from delapan.core.agent.state import TenantContext
from delapan.mcp.tenancy import resolve_tenant
from delapan.store import Store, get_store


def resolve_kb_or_404(project: str, kb: str) -> tuple[TenantContext, Store]:
    """Resolve `{project}/{kb}` by name (never creating) → (ctx, scoped store)."""
    try:
        ctx = resolve_tenant(project, kb, create=False)
    except Exception as exc:  # noqa: BLE001 — any resolution failure → 404
        raise HTTPException(
            status_code=404, detail=f"project/KB not found: {project}/{kb}"
        ) from exc
    return ctx, get_store(ctx.access_token, org_id=ctx.org_id)
