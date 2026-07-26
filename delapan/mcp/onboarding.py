"""First-run affordances for the MCP surface.

    KB not found ──► kb_not_found_card() ──► gap verdict + guidance (+ demo offer)
    server start ──► seed_demo_if_absent() ──► copy bundled data/demo.db (Task 6)

Shared by the local stdio server and the cloud connector: the demo offer
self-suppresses wherever no demo project exists (cloud never seeds one).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from delapan.store import Store

DEMO_PROJECT = "delapan"
DEMO_KB = "demo"

# onboarding.py lives at <root>/delapan/mcp/onboarding.py → <root>/data/demo.db
BUNDLED_DEMO_DB = Path(__file__).resolve().parents[2] / "data" / "demo.db"


def _demo_available(store: Store | None) -> bool:
    if store is None:
        return False
    try:
        projects = store.list_projects(include_archived=True)
    except Exception:  # noqa: BLE001 — the card must never raise
        return False
    return any(p.get("project") == DEMO_PROJECT for p in projects)


def kb_not_found_card(
    project: str, kb: str, exc: Exception, store: Store | None = None
) -> dict:
    """KB-not-found result with onboarding guidance instead of a bare error."""
    guidance = (
        f"No KB exists yet for {project}/{kb}. Run /delapan:explore (the "
        "delapan_explore tool) with a prompt to research and seed it"
    )
    from delapan.store import active_backend

    if active_backend() == "local":
        guidance += (
            " — needs AI_GATEWAY_API_KEY and TAVILY_API_KEY in the plugin root's .env."
        )
    else:
        guidance += "."

    card = {
        "error": f"KB not found ({project}/{kb}): {exc}",
        "coverage": "gap",
        "onboarding": guidance,
    }
    if _demo_available(store):
        card["try_demo"] = (
            f'delapan_resume(project="{DEMO_PROJECT}", kb="{DEMO_KB}") — bundled demo '
            "KB about delapan itself; its resume card works with no keys configured."
        )
    return card


def seed_demo_if_absent() -> None:
    """First run on the local tier: copy the bundled demo KB into place.

    No-op whenever the target DB already exists, the bundled artifact is
    missing, or the active backend is the cloud tier."""
    from delapan.store import active_backend
    from delapan.store.sqlite import _default_db_path

    if active_backend() != "local":
        return
    target = Path(_default_db_path())
    if target.exists() or not BUNDLED_DEMO_DB.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BUNDLED_DEMO_DB, target)
