"""Mirror docs/tracking → Supabase tracking_* tables.

    markdown ──► parse ──► validate ──► read remote slugs ──► plan ──► apply

    uv run python scripts/tracking_sync.py
    uv run python scripts/tracking_sync.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from delapan.core.clients.supabase import service_client
from delapan.tracking.parse import load_tracking_dir
from delapan.tracking.sync import apply_sync, plan_sync
from delapan.tracking.validate import TrackingValidationError, validate_tracking


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("docs/tracking"))
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    initiatives, backlog = load_tracking_dir(args.root)
    try:
        validate_tracking(initiatives, backlog, repo_root=args.repo_root)
    except TrackingValidationError as error:
        for message in error.errors:
            print(f"error: {message}", file=sys.stderr)
        return 1

    client = service_client()
    response = client.table("tracking_initiatives").select("slug").execute()
    remote_slugs = {row["slug"] for row in (response.data or [])}
    plan = plan_sync(initiatives, remote_slugs, backlog)
    apply_sync(client, plan, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
