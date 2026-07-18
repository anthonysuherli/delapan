"""Plan and apply a markdown → Supabase mirror.

    local files ──► validate ──► SyncPlan ──► upsert / delete / rewrite backlog
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, cast

from delapan.tracking.models import BacklogItem, InitiativeRow


@dataclass
class SyncPlan:
    upserts: list[InitiativeRow]
    delete_slugs: list[str]
    backlog: list[BacklogItem]


class TrackingTableClient(Protocol):
    def table(self, table_name: str, /) -> Any: ...


def plan_sync(
    local: list[InitiativeRow],
    remote_slugs: set[str],
    backlog: list[BacklogItem],
) -> SyncPlan:
    """Plan a full local-to-remote mirror without performing I/O."""
    local_slugs = {initiative.slug for initiative in local}
    return SyncPlan(
        upserts=list(local),
        delete_slugs=sorted(remote_slugs - local_slugs),
        backlog=list(backlog),
    )


def _initiative_payload(row: InitiativeRow, synced_at: str) -> dict[str, Any]:
    return {
        "slug": row.slug,
        "title": row.title,
        "status": row.status,
        "repo": row.repo,
        "blocked_by": row.blocked_by,
        "spec": row.spec,
        "plan": row.plan,
        "branch": row.branch,
        "body_md": row.body_md,
        "updated": row.updated,
        "synced_at": synced_at,
    }


def _backlog_payload(row: BacklogItem, synced_at: str) -> dict[str, Any]:
    return {
        "position": row.position,
        "text": row.text,
        "repo": row.repo,
        "initiative_slug": row.initiative_slug,
        "synced_at": synced_at,
    }


def apply_sync(client: TrackingTableClient, plan: SyncPlan, *, dry_run: bool) -> None:
    """Apply a plan, or print its write counts without touching the client."""
    if dry_run:
        print(f"dry-run: upsert {len(plan.upserts)} initiatives")
        print(f"dry-run: delete {plan.delete_slugs}")
        print(f"dry-run: rewrite backlog ({len(plan.backlog)} items)")
        return

    synced_at = datetime.now(timezone.utc).isoformat()
    if plan.upserts:
        client.table("tracking_initiatives").upsert(
            [_initiative_payload(row, synced_at) for row in plan.upserts]
        ).execute()

    for slug in plan.delete_slugs:
        client.table("tracking_initiatives").delete().eq("slug", slug).execute()

    response = client.table("tracking_backlog").select("position").execute()
    backlog_rows = cast(list[dict[str, Any]], response.data or [])
    for row in backlog_rows:
        client.table("tracking_backlog").delete().eq("position", row["position"]).execute()

    if plan.backlog:
        client.table("tracking_backlog").insert(
            [_backlog_payload(row, synced_at) for row in plan.backlog]
        ).execute()
