"""Parse docs/tracking markdown into row dataclasses.

    initiatives/*.md ──► InitiativeRow
    backlog.md       ──► list[BacklogItem]  (position = list order)
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from delapan.tracking.models import BacklogItem, InitiativeRow

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_REPO_TAG = re.compile(r"\[(backend|frontend|both)\]")
_INIT_TAG = re.compile(r"\[initiative:([a-z0-9-]+)\]")


def _nullish(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "null", "none"}:
        return None
    return str(value)


def parse_initiative_file(path: Path) -> InitiativeRow:
    raw = path.read_text(encoding="utf-8")
    m = _FRONTMATTER.match(raw)
    if not m:
        raise ValueError(f"{path}: missing YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        # malformed file content is a value problem, not a caller type bug
        raise ValueError(f"{path}: frontmatter must be a mapping")  # noqa: TRY004
    blocked = meta.get("blocked_by") or []
    if not isinstance(blocked, list):
        raise ValueError(f"{path}: blocked_by must be a list")  # noqa: TRY004
    return InitiativeRow(
        slug=path.stem,
        title=str(meta.get("title") or ""),
        status=str(meta.get("status") or ""),
        repo=str(meta.get("repo") or ""),
        blocked_by=[str(s) for s in blocked],
        spec=_nullish(meta.get("spec")),
        plan=_nullish(meta.get("plan")),
        branch=_nullish(meta.get("branch")),
        updated=str(meta.get("updated") or ""),
        body_md=m.group(2).strip() + ("\n" if m.group(2).strip() else ""),
    )


def parse_backlog_file(path: Path) -> list[BacklogItem]:
    items: list[BacklogItem] = []
    position = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        position += 1
        payload = stripped[2:].strip()
        repo_m = _REPO_TAG.search(payload)
        init_m = _INIT_TAG.search(payload)
        text = _REPO_TAG.sub("", payload)
        text = _INIT_TAG.sub("", text).strip()
        text = re.sub(r"\s+", " ", text).strip()
        items.append(
            BacklogItem(
                position=position,
                text=text,
                repo=repo_m.group(1) if repo_m else "both",
                initiative_slug=init_m.group(1) if init_m else None,
            )
        )
    return items


def load_tracking_dir(root: Path) -> tuple[list[InitiativeRow], list[BacklogItem]]:
    init_dir = root / "initiatives"
    initiatives = [
        parse_initiative_file(p)
        for p in sorted(init_dir.glob("*.md"))
        if p.is_file()
    ]
    backlog = parse_backlog_file(root / "backlog.md")
    return initiatives, backlog
