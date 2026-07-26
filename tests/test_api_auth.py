"""Auth-layer tests: HS256 fallback (forged, test secret) and ES256/JWKS (real
in-test EC keypairs, fake injected JWKS source) — both hermetic, no network."""

from __future__ import annotations

import importlib
import inspect
import logging
import sys
import time
from typing import ClassVar

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException

from delapan.api.ratelimit import _HAVE_SLOWAPI

SECRET = "test-jwt-secret"


def _token(
    sub: str = "user-a", *, aud: str = "authenticated", exp_delta: int = 3600, secret: str = SECRET
) -> str:
    return jwt.encode(
        {"sub": sub, "aud": aud, "exp": int(time.time()) + exp_delta}, secret, algorithm="HS256"
    )


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    """HS256 secret configured, ES256/JWKS deliberately unconfigured (empty
    `SUPABASE_URL` — a real repo `.env` may set it, and `monkeypatch.delenv`
    can't shadow a dotenv-sourced default, only `setenv("", ...)` can) so
    `verify_bearer` takes the HS256 fallback path by default. Individual
    ES256 tests below override `SUPABASE_URL` (and inject a fake JWKS client)
    to exercise the primary path instead."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUPABASE_URL", "")
    from delapan.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _es256_keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key, private_key.public_key()


def _es256_token(
    private_key,
    sub: str = "user-es",
    *,
    aud: str = "authenticated",
    exp_delta: int = 3600,
    kid: str = "test-kid",
) -> str:
    return jwt.encode(
        {"sub": sub, "aud": aud, "exp": int(time.time()) + exp_delta},
        private_key,
        algorithm="ES256",
        headers={"kid": kid},
    )


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


class _FakeJWKClient:
    """Injected in place of `auth._jwk_client()` — never touches the network.
    Returns a fixed signing key, or raises the given exception, regardless of
    the token's actual `kid` (the tests control the scenario directly)."""

    def __init__(self, public_key=None, *, raises: Exception | None = None):
        self._public_key = public_key
        self._raises = raises

    def get_signing_key_from_jwt(self, token):
        if self._raises is not None:
            raise self._raises
        return _FakeSigningKey(self._public_key)


def _use_jwks(monkeypatch, jwk_client) -> None:
    """Point `verify_bearer` at ES256/JWKS: real `SUPABASE_URL` config plus
    the fake client seam, mirroring `_service_client()`'s injection below."""
    monkeypatch.setenv("SUPABASE_URL", "https://fake-project.supabase.co")
    from delapan.core.config import get_settings

    get_settings.cache_clear()
    import delapan.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_jwk_client", lambda: jwk_client)


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


def test_verify_bearer_neither_configured_500(monkeypatch):
    """No SUPABASE_URL and no SUPABASE_JWT_SECRET — production misconfiguration,
    not a bad token; the old error was 'SUPABASE_JWT_SECRET not configured'."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "")
    from delapan.core.config import get_settings

    get_settings.cache_clear()
    from delapan.api.auth import verify_bearer

    with pytest.raises(HTTPException) as exc:
        verify_bearer(f"Bearer {_token()}")
    assert exc.value.status_code == 500


def test_verify_bearer_es256_valid_returns_sub(monkeypatch):
    """The production case: a real EC P-256 keypair signs the token, its
    public half is served through the injected JWKS seam — no network."""
    private_key, public_key = _es256_keypair()
    _use_jwks(monkeypatch, _FakeJWKClient(public_key))
    from delapan.api.auth import verify_bearer

    token = _es256_token(private_key, "user-es-1")
    assert verify_bearer(f"Bearer {token}") == "user-es-1"


def test_verify_bearer_es256_wrong_key_401(monkeypatch):
    """Token signed by one EC key, JWKS serves the signing key of a different
    (unrelated) one — a matching `kid` but a real signature mismatch, so this
    must 401 outright rather than being retried against HS256."""
    signing_key, _ = _es256_keypair()
    _other_signing_key, other_public_key = _es256_keypair()
    _use_jwks(monkeypatch, _FakeJWKClient(other_public_key))
    from delapan.api.auth import verify_bearer

    token = _es256_token(signing_key)
    with pytest.raises(HTTPException) as exc:
        verify_bearer(f"Bearer {token}")
    assert exc.value.status_code == 401


def test_verify_bearer_es256_expired_401(monkeypatch):
    private_key, public_key = _es256_keypair()
    _use_jwks(monkeypatch, _FakeJWKClient(public_key))
    from delapan.api.auth import verify_bearer

    token = _es256_token(private_key, exp_delta=-10)
    with pytest.raises(HTTPException) as exc:
        verify_bearer(f"Bearer {token}")
    assert exc.value.status_code == 401


def test_verify_bearer_es256_wrong_audience_401(monkeypatch):
    private_key, public_key = _es256_keypair()
    _use_jwks(monkeypatch, _FakeJWKClient(public_key))
    from delapan.api.auth import verify_bearer

    token = _es256_token(private_key, aud="not-authenticated")
    with pytest.raises(HTTPException) as exc:
        verify_bearer(f"Bearer {token}")
    assert exc.value.status_code == 401


def test_verify_bearer_es256_no_jwks_match_falls_back_to_hs256(monkeypatch):
    """Unknown/absent `kid` (no JWKS match, e.g. `PyJWKClientError`) — unlike
    a matched-but-invalid key, this is exactly the case that should still try
    the legacy HS256 secret: a genuinely valid HS256 token must be accepted."""
    _use_jwks(monkeypatch, _FakeJWKClient(raises=jwt.PyJWKClientError("no matching kid")))
    from delapan.api.auth import verify_bearer

    assert verify_bearer(f"Bearer {_token('user-legacy')}") == "user-legacy"


def test_verify_bearer_jwks_unreachable_503(monkeypatch):
    """A JWKS fetch that fails on the network must not read as a forged
    token — even with a valid HS256 secret configured, an outage on the
    primary (ES256) path is surfaced as 503, not silently swallowed."""
    _use_jwks(
        monkeypatch, _FakeJWKClient(raises=jwt.PyJWKClientConnectionError("connection refused"))
    )
    from delapan.api.auth import verify_bearer

    with pytest.raises(HTTPException) as exc:
        verify_bearer(f"Bearer {_token()}")
    assert exc.value.status_code == 503


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
        headers: ClassVar[dict] = {}

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


@pytest.fixture()
def local_client(monkeypatch, tmp_path):
    """TestClient with api.auth=none (the default) — isolates default-limit
    enforcement from the beta-gate auth path, which 401s/403s before the
    limiter would ever run and would mask what this fixture is for."""
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "api.db"))
    from delapan.core.config import get_config, get_settings

    get_settings.cache_clear()
    get_config.cache_clear()
    from fastapi.testclient import TestClient

    from delapan.api.main import app
    from delapan.api.ratelimit import limiter as _limiter

    if hasattr(_limiter, "reset"):
        _limiter.reset()
    yield TestClient(app)
    if hasattr(_limiter, "reset"):
        _limiter.reset()
    get_settings.cache_clear()
    get_config.cache_clear()


def test_rate_limit_default_not_enforced_in_none_mode(local_client, monkeypatch):
    """`enforce_default_limit` is a no-op unless `api.auth == "supabase"` — the
    plan's binding constraint is byte-identical local (`api.auth == "none"`)
    behavior, including no rate ceiling, even when the `[cloud]` extra (and
    therefore slowapi) happens to be installed."""
    monkeypatch.setenv("DLP_API__RATE_LIMIT_DEFAULT", "1/hour")
    from delapan.core.config import get_config

    get_config.cache_clear()
    local_client.get("/api/projects")
    r = local_client.get("/api/projects")
    assert r.status_code == 200
    get_config.cache_clear()


def test_rate_limit_default_enforced_in_supabase_mode(supabase_mode_client, monkeypatch):
    """Counterpart: with `api.auth == "supabase"`, GET /api/projects — which
    carries no `@limiter.limit(...)` decorator of its own — is still bound by
    `api.rate_limit_default`. This is the app-wide guarantee `SlowAPIMiddleware`
    was supposed to provide (see ratelimit.py's module docstring and main.py)
    but can't, because FastAPI 0.139's `include_router` no longer flattens
    child routes into `app.routes`."""
    import delapan.api.auth as auth_mod
    import delapan.mcp.tenancy as tenancy_mod

    monkeypatch.setattr(auth_mod, "_service_client", lambda: _FakeService([{"user_id": "u1"}]))
    monkeypatch.setattr(tenancy_mod, "_org_for", lambda user_id: "org-test")
    monkeypatch.setenv("DLP_API__RATE_LIMIT_DEFAULT", "1/hour")
    from delapan.core.config import get_config

    get_config.cache_clear()
    headers = {"Authorization": f"Bearer {_token('u1')}"}
    supabase_mode_client.get("/api/projects", headers=headers)
    r = supabase_mode_client.get("/api/projects", headers=headers)
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}
    get_config.cache_clear()


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
    supabase_mode_client.post(
        "/api/projects/p/kbs/k/explore", json={"prompt": "x"}, headers=headers
    )
    r = supabase_mode_client.post(
        "/api/projects/p/kbs/k/explore", json={"prompt": "x"}, headers=headers
    )
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}
    get_config.cache_clear()


def test_rate_limit_pipeline_429_canvas_search(supabase_mode_client, monkeypatch):
    """Canvas routes run the same spend path as /explore (embeddings → Tavily
    → LLM) but previously carried only the generous app-wide default —
    mirrors test_rate_limit_pipeline_429 for POST /canvas/search."""
    import delapan.api.auth as auth_mod
    import delapan.api.routes_canvas as canvas_mod
    import delapan.mcp.tenancy as tenancy_mod
    from delapan.store import get_store

    monkeypatch.setattr(auth_mod, "_service_client", lambda: _FakeService([{"user_id": "u1"}]))
    monkeypatch.setattr(tenancy_mod, "_org_for", lambda user_id: "org-test")
    monkeypatch.setattr(canvas_mod, "missing_pipeline_keys", lambda: ["AI_GATEWAY_API_KEY"])
    monkeypatch.setenv("DLP_API__RATE_LIMIT_PIPELINE", "1/hour")
    from delapan.core.config import get_config

    get_config.cache_clear()
    # canvas/search is non-creating (request_tenancy, not the *_creating
    # variant /explore uses) — the KB must already exist or every call 404s
    # before ever reaching the rate limiter.
    store = get_store()
    org_id, project_id = store.resolve_project("p", create=True)
    store.resolve_kb(org_id, project_id, "k", create=True)
    headers = {"Authorization": f"Bearer {_token('u1')}"}
    supabase_mode_client.post(
        "/api/projects/p/kbs/k/canvas/search", json={"prompt": "x"}, headers=headers
    )
    r = supabase_mode_client.post(
        "/api/projects/p/kbs/k/canvas/search", json={"prompt": "x"}, headers=headers
    )
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}
    get_config.cache_clear()


def test_rate_limit_pipeline_not_enforced_in_none_mode_canvas_search(local_client, monkeypatch):
    """Counterpart to test_rate_limit_pipeline_429_canvas_search: the
    ``@limiter.limit(pipeline_limit, ...)`` decorator on POST /canvas/search
    is a separate enforcement path from ``enforce_default_limit`` and never
    consulted ``api.auth`` on its own — so a local install (``api.auth ==
    "none"``) with a tight ``rate_limit_pipeline`` used to 429 anyway,
    breaking local-tier parity. ``exempt_when=_local_tier_exempt`` on the
    decorator fixes that; this proves repeated calls stay 200 in local mode
    even with a 1/hour pipeline limit."""
    import delapan.api.routes_canvas as canvas_mod
    from delapan.store import get_store

    monkeypatch.setattr(canvas_mod, "missing_pipeline_keys", lambda: ["AI_GATEWAY_API_KEY"])
    monkeypatch.setenv("DLP_API__RATE_LIMIT_PIPELINE", "1/hour")
    from delapan.core.config import get_config

    get_config.cache_clear()
    # canvas/search is non-creating (request_tenancy, not the *_creating
    # variant /explore uses) — the KB must already exist or every call 404s
    # before ever reaching the rate limiter.
    store = get_store()
    org_id, project_id = store.resolve_project("p", create=True)
    store.resolve_kb(org_id, project_id, "k", create=True)
    r1 = local_client.post("/api/projects/p/kbs/k/canvas/search", json={"prompt": "x"})
    r2 = local_client.post("/api/projects/p/kbs/k/canvas/search", json={"prompt": "x"})
    assert r1.status_code != 429
    assert r2.status_code != 429
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
    supabase_mode_client.post(
        "/api/projects/p/kbs/k/explore", json={"prompt": "x"}, headers=headers
    )
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
    the "slowapi mechanics" note in task-6-report.md). Undecorated routes are
    checked instead by the `enforce_default_limit` dependency (also in
    ratelimit.py), since `SlowAPIMiddleware` can't do it: this FastAPI version
    (0.139) no longer flattens `include_router`-added routes into `app.routes`,
    so slowapi's `_find_route_handler` can't find them and `_should_exempt`
    treats every included route as exempt. But those undecorated routes have
    no auth dependency ahead of the limiter the way /explore does, so a forged
    token reaching one of them would hit `_key` for real — this test still
    isolates `_key`'s behavior at the unit level rather than relying on a
    specific route's dependency ordering."""
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


@pytest.mark.skipif(not _HAVE_SLOWAPI, reason="slowapi not installed")
def test_ratelimit_private_api_tripwire():
    """`enforce_default_limit` (ratelimit.py) reaches into slowapi's private
    surface — `Limiter._check_request_limit`, `_route_limits`,
    `_dynamic_route_limits`, `_exempt_routes` — none of which are public API,
    and slowapi carries no version ceiling in pyproject.toml (task-6b review
    finding). If a dependency bump renames or drops one of these, the module
    would either crash somewhere deep in slowapi with a confusing traceback,
    or (per `_check_request_limit`'s `in_middleware=True` semantics, see
    `enforce_default_limit`'s docstring) silently degrade to a no-op with no
    other test signal until someone read the diff by hand. This fails loudly,
    at the source, the moment any of them moves.

    (The fifth private dependency this module has — Starlette setting
    `scope["endpoint"]` during route dispatch — isn't hasattr-checkable the
    same way; its disappearance already fails loudly via
    `test_rate_limit_default_enforced_on_undecorated_route`, since
    `enforce_default_limit` reads `request.scope.get("endpoint")` and quietly
    returns on `None`, which would flip that test's 429 assertion.)"""
    from delapan.api.ratelimit import limiter

    for attr in (
        "_exempt_routes",
        "_route_limits",
        "_dynamic_route_limits",
        "_check_request_limit",
    ):
        assert hasattr(limiter, attr), f"slowapi Limiter lost private attribute {attr!r}"

    # enforce_default_limit calls limiter._check_request_limit(request, handler, True)
    # positionally — pin the parameter names/order that call shape assumes.
    params = list(inspect.signature(limiter._check_request_limit).parameters)
    assert params[:3] == ["request", "endpoint_func", "in_middleware"], (
        f"Limiter._check_request_limit signature changed: {params!r}"
    )


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
            "slowapi not installed" in r.message and "DISABLED" in r.message for r in caplog.records
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
