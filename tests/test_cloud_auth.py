from __future__ import annotations

from types import SimpleNamespace

import pytest

from delapan.mcp.cloud_auth import SupabaseTokenVerifier, _default_client


class _FakeAuth:
    def __init__(self, user_id: str | None):
        self._user_id = user_id

    def get_user(self, token):
        if self._user_id is None:
            raise RuntimeError("invalid token")
        return SimpleNamespace(user=SimpleNamespace(id=self._user_id))


@pytest.mark.asyncio
async def test_verify_token_valid_returns_access_token():
    verifier = SupabaseTokenVerifier(
        client_factory=lambda token: SimpleNamespace(auth=_FakeAuth("user-9"))
    )
    result = await verifier.verify_token("tok-abc")
    assert result is not None
    assert result.subject == "user-9"
    assert result.token == "tok-abc"


@pytest.mark.asyncio
async def test_verify_token_invalid_returns_none():
    verifier = SupabaseTokenVerifier(
        client_factory=lambda token: SimpleNamespace(auth=_FakeAuth(None))
    )
    result = await verifier.verify_token("bad-token")
    assert result is None


def test_default_client_guards_unset_settings(monkeypatch):
    import delapan.core.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(supabase_url=None, supabase_anon_key=None),
    )
    with pytest.raises(AssertionError):
        _default_client("tok-abc")
