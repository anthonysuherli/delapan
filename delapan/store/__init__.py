"""Store: the engine's persistence seam + backend selection.

`Store` is the protocol; `SupabaseStore` (cloud) and `SQLiteStore` (free/local)
are the two implementations. `get_store()` is the factory the engine calls.

Backend selection (in order):
  1. ``DELAPAN_BACKEND`` env var — ``"local"`` or ``"cloud"`` (explicit override);
  2. otherwise ``"cloud"`` iff the Supabase creds are present, else ``"local"``.

Caching: the local SQLite store holds a long-lived connection, so it is cached
as a module-level singleton keyed by db_path (one connection reused across
calls; a different ``DELAPAN_DB_PATH`` — as tests use — gets its own store). The
cloud store is built fresh every call: it carries a per-request ``access_token``
(RLS scope), so caching it would be wrong across users. This mirrors today's
behaviour where ``user_client(token)`` is created per request.

Open-core boundary: the cloud `SupabaseStore` is imported lazily (inside the
cloud branch of `get_store`) so a local-only install — which has neither the
``[cloud]`` extra nor a populated ``delapan.core.config`` — imports this module
cleanly. The creds sniff is likewise resilient: a missing ``delapan.core.config``
(it is created by a later task) or any settings error is treated as "no cloud
creds" → the factory defaults to the local tier.
"""

from __future__ import annotations

import os

from delapan.store.base import Store
from delapan.store.sqlite import SQLiteStore, _default_db_path

__all__ = ["Store", "SQLiteStore", "get_store", "active_backend"]

# Local stores cached by db_path so the SQLite connection is reused.
_local_stores: dict[str, SQLiteStore] = {}


def _has_cloud_creds() -> bool:
    """True iff the Supabase env vars needed for the cloud tier are present.

    Resilient by design: ``delapan.core.config`` is provided by a later task, so
    an ImportError (module absent) or any settings-construction error is treated
    as "no cloud creds" and the factory falls back to the local tier. The local
    tier must work without ``delapan.core.config`` present.

    Note: when present, `get_settings()` is `@lru_cache`d, so creds are sniffed
    once per process. Set ``DELAPAN_BACKEND`` explicitly to skip sniffing.
    """
    try:
        from delapan.core.config import get_settings
    except ImportError:
        return False
    try:
        s = get_settings()
    except Exception:  # noqa: BLE001 — no/invalid settings → local tier
        return False
    return bool(getattr(s, "supabase_url", None) and getattr(s, "supabase_service_role_key", None))


def active_backend() -> str:
    """The selected backend name — ``"local"`` or ``"cloud"``.

    Single source of truth for selection, shared by `get_store` and the tenancy
    fork (local skips GoTrue login). ``DELAPAN_BACKEND`` overrides creds-sniffing.
    """
    backend = os.getenv("DELAPAN_BACKEND")
    if not backend:
        backend = "cloud" if _has_cloud_creds() else "local"
    return backend


def get_store(access_token: str | None = None, *, org_id: str | None = None) -> Store:
    """Return the active Store for this request.

    Local: a cached SQLiteStore (connection reused). Cloud: a fresh
    SupabaseStore bound to ``access_token`` for RLS-scoped finding ops and an
    optional ``org_id`` (sourced from the verified request JWT) that skips the
    store's internal MCP-path login. The cloud store is imported lazily so a
    local-only install never needs the ``[cloud]`` extra.
    """
    if active_backend() == "local":
        path = _default_db_path()
        store = _local_stores.get(path)
        if store is None:
            store = SQLiteStore(path)
            _local_stores[path] = store
        return store
    from delapan.store.supabase import SupabaseStore

    return SupabaseStore(access_token, org_id=org_id)
