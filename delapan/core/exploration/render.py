"""Render a finding's content for persistence.

Shared by the explore call sites (`mcp/server.py`, `api/routes_explore.py`) and
the memory persist path. Previously duplicated verbatim in both call sites.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def render_content(content: Any) -> str:
    """Render a finding's free-form ``content`` dict to a markdown body.

    A single-key dict with a string value renders to that string; otherwise each
    key becomes a ``**Label**: value`` line (lists/dicts as fenced JSON). Strings
    pass through; non-dicts are stringified."""
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return str(content)
    if not content:
        return ""

    if len(content) == 1:
        only = next(iter(content.values()))
        if isinstance(only, str):
            return only

    lines: list[str] = []
    for key, value in content.items():
        label = key.replace("_", " ").title()
        if isinstance(value, (list, dict)):
            lines.append(f"**{label}**:")
            lines.append("```json")
            lines.append(json.dumps(value, indent=2))
            lines.append("```")
        else:
            lines.append(f"**{label}**: {value}")
    return "\n".join(lines)


def normalize_provenance(provenance: Any) -> list[dict]:
    """Findings carry ``[{url, query}]``; keep that shape, stamp ``accessed_at``."""
    if not provenance:
        return []
    out: list[dict] = []
    for p in provenance:
        if isinstance(p, dict):
            entry = dict(p)
            entry.setdefault("accessed_at", _now_iso())
            out.append(entry)
    return out
