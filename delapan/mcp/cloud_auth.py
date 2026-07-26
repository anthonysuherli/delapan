"""Resource-server token verification for the cloud MCP server.

    claude.ai bearer token ──► SupabaseTokenVerifier.verify_token ──► AccessToken
                                        │ auth.get_user(token) against Supabase

Supabase Auth is the OAuth 2.1 authorization server (see docs/superpowers/specs/
2026-07-17-cloud-remote-mcp-design.md §6); this only validates the token it
issued and extracts the user id TenantContext resolution needs downstream.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier


def _default_client(_token: str) -> Any:
    from delapan.core.clients.supabase import create_client
    from delapan.core.config import get_settings

    s = get_settings()
    assert s.supabase_url and s.supabase_anon_key, (
        "SUPABASE_URL and SUPABASE_ANON_KEY required for the cloud tier."
    )
    return create_client(s.supabase_url, s.supabase_anon_key)


class SupabaseTokenVerifier(TokenVerifier):
    """Validates a bearer token by asking Supabase who it belongs to."""

    def __init__(self, client_factory: Callable[[str], Any] = _default_client) -> None:
        self._client_factory = client_factory

    async def verify_token(self, token: str) -> AccessToken | None:
        client = self._client_factory(token)
        try:
            res = client.auth.get_user(token)
        except Exception:  # noqa: BLE001 — any auth failure means an invalid token
            return None
        if not res or not res.user:
            return None
        return AccessToken(token=token, client_id="claude.ai", scopes=[], subject=res.user.id)
