"""Auth-layer tests: forged HS256 JWTs against a test secret — hermetic."""
from __future__ import annotations

import importlib
import logging
import sys
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


@pytest.fixture()
def supabase_mode_client(monkeypatch, tmp_path):
    """TestClient with api.auth=supabase — requests without/with tokens hit the gate.
    Local SQLite backend keeps it hermetic; tenancy resolution is never reached
    for the 401/403 assertions."""
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("DLP_API__AUTH", "supabase")
    from delapan.core.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    from fastapi.testclient import TestClient

    from delapan.api.main import app
    from delapan.api.ratelimit import limiter as _limiter

    # In-memory limiter storage is a module-level singleton shared across the
    # whole test session — reset it so buckets from one test never leak into
    # the next. The no-op fallback (no slowapi) has no .reset(); skip it then.
    if hasattr(_limiter, "reset"):
        _limiter.reset()
    yield TestClient(app)
    if hasattr(_limiter, "reset"):
        _limiter.reset()
    get_settings.cache_clear()
    get_config.cache_clear()


def test_routes_require_token_in_supabase_mode(supabase_mode_client):
    for path in ("/api/projects", "/api/projects/p/kbs/k/findings"):
        r = supabase_mode_client.get(path)
        assert r.status_code == 401, path


def test_routes_403_without_beta_membership(supabase_mode_client, monkeypatch):
    import delapan.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_service_client", lambda: _FakeService([]))
    r = supabase_mode_client.get(
        "/api/projects", headers={"Authorization": f"Bearer {_token('u-no-beta')}"}
    )
    assert r.status_code == 403


def test_rate_limit_pipeline_429(supabase_mode_client, monkeypatch):
    import delapan.api.auth as auth_mod
    import delapan.api.routes_explore as explore_mod
    import delapan.mcp.tenancy as tenancy_mod

    monkeypatch.setattr(auth_mod, "_service_client", lambda: _FakeService([{"user_id": "u1"}]))
    # supabase_mode_client runs the local SQLite store (hermetic), but tenancy
    # resolution for api.auth=="supabase" always takes the cloud-token path
    # (resolve_tenant_for_token), which looks up org membership via a real
    # Supabase service client. Stub the org lookup only — org_id is discarded
    # by the local backend's get_store() anyway.
    monkeypatch.setattr(tenancy_mod, "_org_for", lambda user_id: "org-test")
    # Force the pipeline to short-circuit before touching real search/LLM
    # providers — this test only cares that the SECOND request is rate
    # limited, not that exploration succeeds.
    monkeypatch.setattr(explore_mod, "missing_pipeline_keys", lambda: ["AI_GATEWAY_API_KEY"])
    monkeypatch.setenv("DLP_API__RATE_LIMIT_PIPELINE", "1/hour")
    from delapan.core.config import get_config

    get_config.cache_clear()
    headers = {"Authorization": f"Bearer {_token('u1')}"}
    # Two POSTs: the second must be limited regardless of what the first returns.
    supabase_mode_client.post("/api/projects/p/kbs/k/explore", json={"prompt": "x"}, headers=headers)
    r = supabase_mode_client.post(
        "/api/projects/p/kbs/k/explore", json={"prompt": "x"}, headers=headers
    )
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}
    get_config.cache_clear()


def test_rate_limit_default_also_applies_to_explore(supabase_mode_client, monkeypatch):
    """`@limiter.limit(pipeline_limit, override_defaults=False)` must not let
    /explore escape the app-wide default — both limits apply, and the tighter
    one (the default here) is the one that binds."""
    import delapan.api.auth as auth_mod
    import delapan.api.routes_explore as explore_mod
    import delapan.mcp.tenancy as tenancy_mod

    monkeypatch.setattr(auth_mod, "_service_client", lambda: _FakeService([{"user_id": "u1"}]))
    monkeypatch.setattr(tenancy_mod, "_org_for", lambda user_id: "org-test")
    monkeypatch.setattr(explore_mod, "missing_pipeline_keys", lambda: ["AI_GATEWAY_API_KEY"])
    monkeypatch.setenv("DLP_API__RATE_LIMIT_DEFAULT", "1/hour")
    monkeypatch.setenv("DLP_API__RATE_LIMIT_PIPELINE", "100/hour")
    from delapan.core.config import get_config

    get_config.cache_clear()
    headers = {"Authorization": f"Bearer {_token('u1')}"}
    supabase_mode_client.post("/api/projects/p/kbs/k/explore", json={"prompt": "x"}, headers=headers)
    r = supabase_mode_client.post(
        "/api/projects/p/kbs/k/explore", json={"prompt": "x"}, headers=headers
    )
    assert r.status_code == 429
    # The tight default (1/hour), not the generous pipeline limit (100/hour),
    # must be the one that fired — proves override_defaults=False wires both
    # checks in rather than the pipeline decorator silently dropping the default.
    assert "1 per 1 hour" in r.json()["error"]
    get_config.cache_clear()


def _rl_request(token: str):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("1.2.3.4", 12345),
    }
    return Request(scope)


def test_key_forged_sub_falls_back_to_ip_bucket():
    """`_key` must verify the JWT before trusting its `sub`. Two garbage-signed
    tokens (wrong secret, different subs) must both fall back to the same
    client-IP key — a forged sub can no longer mint its own bucket.

    Unit-level on purpose: an HTTP round trip can't observe this on any current
    route. /explore is the only `@limiter.limit(...)`-decorated route, and
    slowapi's decorator wraps the endpoint function itself, which FastAPI only
    calls after `Depends(request_tenancy_creating)` resolves — so a
    garbage-signed token always 401s at real auth (Task 2's verify_bearer)
    before the rate limiter ever runs (see ratelimit.py's module docstring /
    the "slowapi mechanics" note in task-6-report.md). Undecorated routes would
    otherwise hit the check earlier via SlowAPIMiddleware, but this FastAPI
    version (0.139) no longer flattens `include_router`-added routes into
    `app.routes`, so slowapi's `_find_route_handler` can't find them and
    `_should_exempt` treats every included route as exempt — a pre-existing
    slowapi/FastAPI incompatibility, unrelated to this task, that leaves
    `rate_limit_default` unenforced except on routes explicitly decorated."""
    from delapan.api.ratelimit import _key

    tok_a = _token("forged-sub-a", secret="wrong-secret")
    tok_b = _token("forged-sub-b", secret="wrong-secret")
    assert _key(_rl_request(tok_a)) == _key(_rl_request(tok_b)) == "1.2.3.4"


def test_key_valid_token_buckets_by_sub():
    """Sanity counterpart: a genuinely valid token still buckets by its
    verified sub, not IP — the fallback above is specific to verification
    failure, not a blanket regression to IP-only bucketing."""
    from delapan.api.ratelimit import _key

    assert _key(_rl_request(_token("user-42"))) == "user-42"


def test_ratelimit_noop_when_slowapi_missing(monkeypatch, caplog):
    """Local-only installs (no [cloud] extra) must still import cleanly with
    rate limiting disabled — and log a warning about it, not fail silently."""
    import delapan.api.main as main_mod
    import delapan.api.ratelimit as ratelimit_mod
    import delapan.api.routes_explore as routes_explore_mod

    monkeypatch.setitem(sys.modules, "slowapi", None)
    try:
        with caplog.at_level(logging.WARNING):
            importlib.reload(ratelimit_mod)
            importlib.reload(routes_explore_mod)
            importlib.reload(main_mod)

        assert ratelimit_mod._HAVE_SLOWAPI is False
        assert isinstance(ratelimit_mod.limiter, ratelimit_mod._NoOpLimiter)
        assert main_mod.app is not None
        assert not hasattr(main_mod.app.state, "limiter")
        assert any(
            "slowapi not installed" in r.message and "DISABLED" in r.message
            for r in caplog.records
        )
    finally:
        # sys.modules["slowapi"] reverts to the real module on undo; reload the
        # dependent modules again so their `limiter`/decorator bindings — and
        # every other test in the session — see the real Limiter again.
        monkeypatch.undo()
        importlib.reload(ratelimit_mod)
        importlib.reload(routes_explore_mod)
        importlib.reload(main_mod)
        assert ratelimit_mod._HAVE_SLOWAPI is True
