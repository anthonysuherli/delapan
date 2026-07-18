"""Hard validation for tracking rows — never skip; collect all errors then raise.

    InitiativeRow[] + BacklogItem[] ──► validate_tracking ──► ok | TrackingValidationError
"""

from __future__ import annotations

from pathlib import Path

from delapan.tracking.models import BacklogItem, InitiativeRow

STATUSES = frozenset({"proposed", "active", "blocked", "paused", "done", "dropped"})
REPOS = frozenset({"backend", "frontend", "both"})


class TrackingValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _is_url(value: str) -> bool:
    return value.startswith("https://")


def _check_local_path(
    slug: str,
    field: str,
    value: str,
    repo_root: Path,
    errors: list[str],
) -> None:
    root = repo_root.resolve()
    path = Path(value)
    if path.is_absolute():
        errors.append(f"{slug}: {field} path must be relative: {value}")
        return
    resolved = (root / value).resolve()
    if not resolved.is_relative_to(root):
        errors.append(f"{slug}: {field} path outside repo: {value}")
        return
    if not resolved.is_file():
        errors.append(f"{slug}: {field} path missing: {value}")


def validate_tracking(
    initiatives: list[InitiativeRow],
    backlog: list[BacklogItem],
    *,
    repo_root: Path,
) -> None:
    errors: list[str] = []
    slugs = [i.slug for i in initiatives]
    if len(slugs) != len(set(slugs)):
        errors.append("duplicate initiative slug")
    slug_set = set(slugs)

    for i in initiatives:
        if not i.title.strip():
            errors.append(f"{i.slug}: missing title")
        if i.status not in STATUSES:
            errors.append(f"{i.slug}: unknown status {i.status!r}")
        if i.repo not in REPOS:
            errors.append(f"{i.slug}: unknown repo {i.repo!r}")
        if not i.updated.strip():
            errors.append(f"{i.slug}: missing updated")
        for b in i.blocked_by:
            if b not in slug_set:
                errors.append(f"{i.slug}: blocked_by unknown slug {b!r}")
        for field, value in (("spec", i.spec), ("plan", i.plan)):
            if value is None:
                continue
            if _is_url(value):
                continue
            _check_local_path(i.slug, field, value, repo_root, errors)

    for item in backlog:
        if item.repo not in REPOS:
            errors.append(f"backlog#{item.position}: unknown repo {item.repo!r}")
        if item.initiative_slug and item.initiative_slug not in slug_set:
            errors.append(
                f"backlog#{item.position}: unknown initiative {item.initiative_slug!r}"
            )

    if errors:
        raise TrackingValidationError(errors)
