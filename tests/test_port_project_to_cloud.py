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
