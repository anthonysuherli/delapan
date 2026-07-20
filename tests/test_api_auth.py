"""Auth-layer tests: forged HS256 JWTs against a test secret — hermetic."""
from __future__ import annotations

import time

import jwt
import pytest
from fastapi import HTTPException

SECRET = "test-jwt-secret"


def _token(sub: str = "user-a", *, aud: str = "authenticated", exp_delta: int = 3600,
           secret: str = SECRET) -> str:
    return jwt.encode(
        {"sub": sub, "aud": aud, "exp": int(time.time()) + exp_delta}, secret, algorithm="HS256"
    )


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    from delapan.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_verify_bearer_valid_returns_sub():
    from delapan.api.auth import verify_bearer

    assert verify_bearer(f"Bearer {_token('user-9')}") == "user-9"


@pytest.mark.parametrize(
    "header",
    [None, "", "Token abc", f"Bearer {jwt.encode({'sub': 'x'}, 'wrong', algorithm='HS256')}"],
)
def test_verify_bearer_missing_or_invalid_401(header):
    from delapan.api.auth import verify_bearer

    with pytest.raises(HTTPException) as exc:
        verify_bearer(header)
    assert exc.value.status_code == 401


def test_verify_bearer_expired_401():
    from delapan.api.auth import verify_bearer

    with pytest.raises(HTTPException) as exc:
        verify_bearer(f"Bearer {_token(exp_delta=-10)}")
    assert exc.value.status_code == 401


def test_verify_bearer_wrong_audience_401():
    from delapan.api.auth import verify_bearer

    with pytest.raises(HTTPException) as exc:
        verify_bearer(f"Bearer {_token(aud='not-authenticated')}")
    assert exc.value.status_code == 401


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_):  # chainable PostgREST fake
        return self

    def eq(self, *_):
        return self

    def limit(self, *_):
        return self

    def execute(self):
        import types

        return types.SimpleNamespace(data=self._rows)


class _FakeService:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "beta_members"
        return _FakeTable(self._rows)


def test_require_beta_member_passes(monkeypatch):
    import delapan.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_service_client", lambda: _FakeService([{"user_id": "u1"}]))
    from delapan.api.auth import require_beta

    require_beta("u1")  # no raise


def test_require_beta_non_member_403(monkeypatch):
    import delapan.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_service_client", lambda: _FakeService([]))
    from delapan.api.auth import require_beta

    with pytest.raises(HTTPException) as exc:
        require_beta("u2")
    assert exc.value.status_code == 403


def test_request_tenancy_auth_none_delegates(monkeypatch):
    """With api.auth=none the dependency is exactly resolve_kb_or_404 — parity."""
    import delapan.api.auth as auth_mod

    sentinel = ("ctx", "store")
    monkeypatch.setattr(auth_mod, "resolve_kb_or_404", lambda p, k: sentinel)
    monkeypatch.delenv("DLP_API__AUTH", raising=False)
    from delapan.core.config import get_config

    get_config.cache_clear()
    from delapan.api.auth import request_tenancy

    class _Req:  # request is unused on the auth-none path
        headers: dict = {}

    assert request_tenancy("p", "k", _Req()) == sentinel
    get_config.cache_clear()
