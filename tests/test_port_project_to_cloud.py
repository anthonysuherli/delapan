from __future__ import annotations

import asyncio
import sqlite3

import pytest
import sqlite_vec


def _seed(tmp_path, monkeypatch, project_name: str, kb_name: str):
    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "t.db"))
    from delapan.core import config as cfg

    cfg.get_settings.cache_clear()
    from delapan.store import get_store

    store = get_store()
    org_id, project_id = store.resolve_project(project_name, create=True)
    kb_id = store.resolve_kb(org_id, project_id, kb_name, create=True)
    asyncio.run(
        store.insert_findings(
            [
                {
                    "kb_id": kb_id,
                    "title": "T1",
                    "content": "C1",
                    "category": "cat",
                    "confidence": 0.9,
                    "tags": ["a"],
                    "provenance": [],
                    "embedding": [0.1] * 1536,
                }
            ]
        )
    )
    return tmp_path / "t.db"


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    return conn


def test_build_discovers_kbs_for_named_project(tmp_path, monkeypatch):
    db_path = _seed(tmp_path, monkeypatch, "demo", "main")
    from scripts.port_project_to_cloud import build

    conn = _connect(db_path)
    project_row, kb_rows, findings, nodes, edges = build(conn, "demo")

    assert project_row["name"] == "demo"
    assert [kb["name"] for kb in kb_rows] == ["main"]
    assert len(findings) == 1
    assert findings[0]["title"] == "T1"
    assert len(findings[0]["id"]) == 36  # canonical dashed UUID
    assert findings[0]["embedding"].startswith("[")
    assert findings[0]["embedding"].count(",") == 1535  # 1536 floats
    assert nodes == []
    assert edges == []


def test_build_unknown_project_exits(tmp_path, monkeypatch):
    db_path = _seed(tmp_path, monkeypatch, "demo", "main")
    from scripts.port_project_to_cloud import build

    conn = _connect(db_path)
    with pytest.raises(SystemExit):
        build(conn, "does-not-exist")


def test_dash_slug_ids_map_to_consistent_uuids(tmp_path, monkeypatch):
    """Slug finding ids like 'demo-finding-001' must map to stable UUID5s,
    and every reference (in grounded_in) must resolve to the same UUID."""
    from scripts.port_project_to_cloud import build
    import uuid
    import json
    from datetime import datetime, timezone

    monkeypatch.setenv("DELAPAN_BACKEND", "local")
    monkeypatch.setenv("DELAPAN_DB_PATH", str(tmp_path / "t.db"))
    from delapan.core import config as cfg

    cfg.get_settings.cache_clear()
    from delapan.store import get_store

    store = get_store()
    org_id, project_id = store.resolve_project("demo", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)

    # Seed a finding with a slug id instead of a UUID
    slug_finding_id = "demo-finding-001"
    asyncio.run(
        store.insert_findings(
            [
                {
                    "id": slug_finding_id,  # Pass explicit slug id
                    "kb_id": kb_id,
                    "title": "SlugTitle",
                    "content": "SlugContent",
                    "category": "cat",
                    "confidence": 0.9,
                    "tags": ["slug"],
                    "provenance": [],
                    "embedding": [0.1] * 1536,
                }
            ]
        )
    )

    # Now insert a kg_node with grounded_in referencing the slug id
    conn = _connect(tmp_path / "t.db")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO kg_nodes
           (id, org_id, kb_id, type, label, properties, grounded_in, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "node-001",
            "local",
            kb_id,
            "concept",
            "TestNode",
            json.dumps({}),
            json.dumps([slug_finding_id]),  # grounded_in references the slug
            now,
        ),
    )
    conn.commit()

    # Now call build() and verify the transformation
    project_row, kb_rows, findings, nodes, edges = build(conn, "demo")

    # Find the finding that was inserted with the slug id
    slug_findings = [f for f in findings if f["title"] == "SlugTitle"]
    assert len(slug_findings) == 1
    transformed_finding_id = slug_findings[0]["id"]

    # Verify it's a valid 36-character UUID string
    assert len(transformed_finding_id) == 36
    parsed = uuid.UUID(transformed_finding_id)  # Should not raise
    assert str(parsed) == transformed_finding_id

    # Find the node
    nodes_for_kb = [n for n in nodes if n["label"] == "TestNode"]
    assert len(nodes_for_kb) == 1
    node = nodes_for_kb[0]

    # Verify the node's grounded_in references the same transformed UUID
    assert node["grounded_in"] == [transformed_finding_id]


def test_dash_preserves_already_valid_uuids():
    """UUIDs that are already valid must pass through unchanged."""
    from scripts.port_project_to_cloud import dash

    valid_uuid = "feb75aad-fcf5-4502-9b92-12508ca3f237"
    result = dash(valid_uuid)
    assert result == valid_uuid

    # Also test uppercase (canonical form is lowercase)
    upper_uuid = "FEB75AAD-FCF5-4502-9B92-12508CA3F237"
    result_upper = dash(upper_uuid)
    # Should pass through as-is (not re-lowercased)
    assert result_upper == upper_uuid


def test_dash_hex_to_uuid():
    """32-char hex strings must be dashed into canonical UUID format."""
    from scripts.port_project_to_cloud import dash

    hex_32 = "feb75aadcf54502e9b9212508ca3f237"
    result = dash(hex_32)
    assert result == "feb75aad-cf54-502e-9b92-12508ca3f237"
