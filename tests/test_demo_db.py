"""The committed demo artifact must stay loadable and populated (hermetic read)."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "data" / "demo.db"


def test_demo_artifact_is_populated():
    from delapan.store.sqlite import SQLiteStore

    assert DEMO.exists(), "data/demo.db must be committed (scripts/build_demo_db.py)"
    store = SQLiteStore(str(DEMO))
    projects = store.list_projects(include_archived=True)
    demo = next(p for p in projects if p["project"] == "delapan")
    [kb] = demo["kbs"]  # the demo project has exactly one KB
    assert kb["finding_count"] >= 8

    syn = store.load_synopsis(kb["kb_id"])
    assert syn and len(syn["content"]) >= 4
    assert all({"topic", "gloss"} <= set(e) for e in syn["content"])
