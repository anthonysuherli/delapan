"""Hard validation for tracking rows — never skip; collect all errors then raise."""

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
    return value.startswith("https://") or value.startswith("http://")


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
            if not (repo_root / value).is_file():
                errors.append(f"{i.slug}: {field} path missing: {value}")

    for item in backlog:
        if item.repo not in REPOS:
            errors.append(f"backlog#{item.position}: unknown repo {item.repo!r}")
        if item.initiative_slug and item.initiative_slug not in slug_set:
            errors.append(
                f"backlog#{item.position}: unknown initiative {item.initiative_slug!r}"
            )

    if errors:
        raise TrackingValidationError(errors)
