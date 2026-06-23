"""Supabase client factories for the cloud tier.

    get_settings() ──► service_client()  (service-role, bypasses RLS)
                  └──► user_client(jwt)   (anon key + user JWT, RLS-scoped)

`supabase` is imported lazily inside the factories so a local-only install
never needs the ``[cloud]`` extra. tenancy.py imports ``service_client`` here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from delapan.core.config import get_settings

if TYPE_CHECKING:
    from supabase import Client


def create_client(url: str, key: str) -> "Client":
    """Thin seam over supabase.create_client (so tests can monkeypatch it)."""
    from supabase import create_client as _create

    return _create(url, key)


def service_client() -> "Client":
    """Service-role client — bypasses RLS. For admin/tenancy lookups."""
    s = get_settings()
    assert s.supabase_url and s.supabase_service_role_key, (
        "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY required for the cloud tier."
    )
    return create_client(s.supabase_url, s.supabase_service_role_key)


def user_client(access_token: str) -> "Client":
    """Anon-key client carrying the user JWT — every call is RLS-scoped."""
    s = get_settings()
    assert s.supabase_url and s.supabase_anon_key, (
        "SUPABASE_URL and SUPABASE_ANON_KEY required for the cloud tier."
    )
    client = create_client(s.supabase_url, s.supabase_anon_key)
    client.postgrest.auth(access_token)
    return client
