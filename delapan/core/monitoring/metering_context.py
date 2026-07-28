"""Ambient tenancy + operation for cost metering.

Leaf clients (ai_gateway/embeddings/tavily) have no TenantContext, so the entry
point (MCP tool, HTTP route) sets this scope from its ctx; record_usage() reads
it. Set the scope BEFORE any asyncio.create_task so the copied context carries
it into detached tasks (e.g. the background KG update).
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class MeteringScope:
    org_id: str | None
    user_id: str | None
    operation: str  # 'explore' | 'ingest' | 'deepen' | 'chat' | 'search' | 'other'


_active: ContextVar[MeteringScope | None] = ContextVar("delapan_metering", default=None)


def current_metering() -> MeteringScope | None:
    return _active.get()


@contextlib.contextmanager
def metering_scope(
    *, org_id: str | None, user_id: str | None, operation: str
) -> Generator[MeteringScope, None, None]:
    scope = MeteringScope(org_id=org_id, user_id=user_id, operation=operation)
    token = _active.set(scope)
    try:
        yield scope
    finally:
        _active.reset(token)
