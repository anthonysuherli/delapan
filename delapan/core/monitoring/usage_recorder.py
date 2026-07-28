"""Write-time cost metering. One usage_events row per LLM/embedding/search call.

Called from the leaf clients (``clients/ai_gateway``, ``clients/embeddings``,
``clients/tavily``) so every call is metered regardless of entry path — HTTP
API, MCP tool, or a direct fresh-process invocation. Fire-and-forget: monitoring
must never break the request path, so every failure is swallowed.

**Open-core notes.** ``usage_events`` is a cloud-tier table; a local-only
install (no Supabase creds) skips the write entirely rather than raising per
call. The local tier's synthetic ``org_id="local"``/``user_id="local"`` are not
UUIDs, so they are dropped to NULL — the columns are ``uuid`` and would
otherwise reject the whole row, silently losing the event.
"""

from __future__ import annotations

import asyncio
import uuid
from functools import lru_cache
from typing import Any

from delapan.core.config import get_config, get_settings
from delapan.core.monitoring.metering_context import current_metering
from delapan.core.monitoring.pricing import compute_cost


@lru_cache(maxsize=1)
def _meter_client() -> Any:
    """Memoized service-role client. Cached because record_usage runs once per
    LLM call — building a fresh client each time would dominate its cost."""
    from delapan.core.clients.supabase import service_client  # lazy: cloud-only

    return service_client()


def _as_uuid(value: str | None) -> str | None:
    """Keep real UUIDs, drop anything else (e.g. the local tier's "local")."""
    if not value:
        return None
    try:
        uuid.UUID(value)
    except ValueError:
        return None
    return value


async def record_usage(
    *,
    model: str,
    in_tokens: int = 0,
    out_tokens: int = 0,
    units: int = 0,
    meta: dict[str, Any] | None = None,
    sb: Any | None = None,
) -> None:
    """Append one costed, attributed usage event. Best-effort; never raises."""
    try:
        if sb is None:
            s = get_settings()
            if not (s.supabase_url and s.supabase_service_role_key):
                return  # local-only install — nowhere to write
        cfg = get_config().metering
        if model == "tavily":
            cost = units * cfg.tavily_search_usd
        else:
            cost = compute_cost(model, in_tokens, out_tokens, cfg.price_table)

        scope = current_metering()
        row = {
            "org_id": _as_uuid(scope.org_id) if scope else None,
            "user_id": _as_uuid(scope.user_id) if scope else None,
            "operation": scope.operation if scope else "other",
            "model": model,
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
            "units": units,
            "cost_usd": round(cost, 6),
            "meta": meta or {},
        }
        client = sb or _meter_client()
        # supabase-py's execute() is blocking; off-thread it so metering never
        # stalls the pipeline's concurrent gateway calls.
        await asyncio.to_thread(lambda: client.table("usage_events").insert(row).execute())
    except Exception:  # noqa: BLE001, S110 — deliberate: metering is fire-and-forget
        pass  # monitoring must not break the request path
