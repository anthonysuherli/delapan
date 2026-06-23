"""Resolve a TenantContext for the in-process MCP entry path.

    local tier ──► get_store() ──► resolve_project(name) ──► resolve_kb(name)
    cloud tier ──► GoTrue login ──► org (membership) ──► project/kb (by name)

Resolution is by *name* for delapan-style ergonomics; missing project/KB are
created on demand (toggle with `create=False`).

**Open-core fork.** The local tier is single-user and auth-less: `org_id` is the
synthetic ``"local"``, the access token is empty, and there is no GoTrue login —
`get_store()` ignores token/org args entirely. The cloud helpers (`_login`,
`_org_for`, etc.) stay in this module but lazily import ``supabase`` *inside* the
function body, so a local-only install (no ``[cloud]`` extra) never imports it.

**Why the service client on the cloud path, not the user client (a deliberate
convention deviation).** The cloud paths use the service client and an explicit
``.eq("org_id", ...)`` filter on every query: (1) the org lookup hits
``org_members``, whose RLS has bitten this repo before — the service client
sidesteps that; (2) find-or-create by name is simpler without RLS in the loop.
**The load-bearing invariant is that every query stays scoped to the ``org_id``
derived from the authenticated user's own membership.**
"""

from __future__ import annotations

import uuid

from delapan.core.agent.state import TenantContext
from delapan.core.config import get_settings


def _login() -> tuple[str, str]:
    """GoTrue login for the configured MCP user (cloud tier only).

    Lazily imports ``supabase`` so a local-only install never pulls the cloud
    extra. Returns ``(user_id, access_token)``."""
    from supabase import create_client  # lazy: cloud-only dependency

    s = get_settings()
    assert s.supabase_url and s.supabase_anon_key, (
        "SUPABASE_URL and SUPABASE_ANON_KEY must be set for cloud-tier MCP login."
    )
    anon = create_client(s.supabase_url, s.supabase_anon_key)
    res = anon.auth.sign_in_with_password(
        {"email": s.mcp_user_email, "password": s.mcp_user_password}
    )
    if not res.session or not res.user:
        raise RuntimeError(
            "MCP user login failed — check the MCP user creds and that the user "
            "exists (run scripts/seed_dev.py)."
        )
    return res.user.id, res.session.access_token


class _NoOrgError(RuntimeError):
    """The user has no org membership yet (distinct from real DB errors)."""


def _service_client():
    """The Supabase service-role client (cloud tier only). Lazily imported."""
    from delapan.core.clients.supabase import service_client  # lazy: cloud-only

    return service_client()


def _org_for(user_id: str) -> str:
    sb = _service_client()
    om = sb.table("org_members").select("org_id").eq("user_id", user_id).limit(1).execute()
    if not om.data:
        raise _NoOrgError("no org for user — did the handle_new_user trigger run?")
    return om.data[0]["org_id"]


def resolve_tenant(project: str, kb: str, *, create: bool = True) -> TenantContext:
    """Resolve a TenantContext by name, creating project/KB on demand.

    Identity fork:
      * local tier — single user, no auth (org_id="local", token=""); the Store
        owns find-or-create by name.
      * cloud tier — the configured MCP user logs in; tenancy is resolved through
        the user-scoped Store. `TenantContext`'s shape is identical on both paths.
    """
    from delapan.store import active_backend, get_store

    if active_backend() == "local":
        store = get_store()
        org_id, project_id = store.resolve_project(project, create=create)
        kb_id = store.resolve_kb(org_id, project_id, kb, create=create)
        return TenantContext(
            user_id="local",
            org_id=org_id,
            project_id=project_id,
            kb_id=kb_id,
            thread_id=str(uuid.uuid4()),
            access_token="",
        )

    # Cloud tier: configured MCP user login → RLS-scoped store.
    user_id, token = _login()
    org_id = _org_for(user_id)
    store = get_store(token, org_id=org_id)
    org_id, project_id = store.resolve_project(project, create=create)
    kb_id = store.resolve_kb(org_id, project_id, kb, create=create)
    return TenantContext(
        user_id=user_id,
        org_id=org_id,
        project_id=project_id,
        kb_id=kb_id,
        thread_id=str(uuid.uuid4()),
        access_token=token,
    )


def resolve_store():
    """Org-scoped Store with no project/kb binding — for cross-repo reads.

    Same identity fork as ``resolve_tenant`` minus project/kb resolution: local
    tier returns the single local store; cloud tier logs the configured MCP user
    in for an RLS-scoped token. Used by the cross-repo ``delapan_projects`` tool.
    """
    from delapan.store import active_backend, get_store

    if active_backend() == "local":
        return get_store()
    user_id, token = _login()
    return get_store(token, org_id=_org_for(user_id))
