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
from delapan.core.config import get_settings
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


def missing_pipeline_keys() -> list[str]:
    """Credentials the research pipeline needs end-to-end (search + LLM + embeddings).

    The AI Gateway carries both the LLM calls and the embeddings, so it is the
    only provider credential required; OPENAI_API_KEY is just the embeddings
    fallback when no gateway key is set, never a requirement.
    """
    s = get_settings()
    required = (
        ("TAVILY_API_KEY", s.tavily_api_key),
        ("AI_GATEWAY_API_KEY", s.ai_gateway_api_key),
    )
    return [name for name, value in required if not value]
