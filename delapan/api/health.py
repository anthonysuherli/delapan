"""Loopback /health — a one-route router reporting the active store backend.

    GET /health ──► {"status": "ok", "backend": active_backend()}

The open-core engine ships no cloud HTTP API; this is the single always-on
endpoint a local operator (or an orchestrator) can hit to confirm the process is
up and which tier (local SQLite / cloud Supabase) the Store seam selected.
"""

from __future__ import annotations

from fastapi import APIRouter

from delapan.store import active_backend

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "backend": active_backend()}
