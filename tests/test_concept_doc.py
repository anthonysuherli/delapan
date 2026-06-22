import pytest

from delapan.core.agent import concept_doc


def test_grounded_hash_known_vectors():
    assert concept_doc.grounded_hash(["f01", "f25"]) == "f6fd8219"
    assert concept_doc.grounded_hash(["f25", "f01"]) == "f6fd8219"  # order-independent
    assert concept_doc.grounded_hash(["a", "b", "c"]) == "7a8f5e87"
    assert concept_doc.grounded_hash([]) == "811c9dc5"


class _FakeStore:
    def get_kg_node(self, kb_id, node_id):
        if node_id == "missing":
            return None
        return {"id": node_id, "type": "concept", "label": "CSM",
                "properties": {}, "grounded_in": ["f1"], "created_at": "2026-06-18T00:00:00Z"}

    def get_finding(self, kb_id, fid):
        return {"id": fid, "title": "CSM is unearned profit",
                "content": {"fact": "deferred to P&L"}, "confidence": 0.9}


@pytest.mark.asyncio
async def test_synthesize_shape(monkeypatch):
    captured_user = {}

    async def fake_completion(**kwargs):
        captured_user["prompt"] = kwargs.get("user", "")
        return "The unearned profit under IFRS 17.\n---\n## Overview\nThe CSM defers gains."

    monkeypatch.setattr(concept_doc, "text_completion", fake_completion)
    monkeypatch.setattr(concept_doc, "read_graph", lambda *a, **k: {"nodes": [], "edges": []})

    doc = await concept_doc.synthesize_concept_doc(_FakeStore(), "kb", "n1")
    assert doc["description"] == "The unearned profit under IFRS 17."
    assert doc["body_markdown"].startswith("## Overview")
    assert doc["grounded_hash"] == concept_doc.grounded_hash(["f1"])
    assert doc["model"]  # came from config
    assert doc["built_at"]
    # content dict must appear as readable text, not Python repr
    assert "deferred to P&L" in captured_user["prompt"]
    assert "{'fact'" not in captured_user["prompt"]


@pytest.mark.asyncio
async def test_synthesize_missing_node(monkeypatch):
    monkeypatch.setattr(concept_doc, "read_graph", lambda *a, **k: {"nodes": [], "edges": []})
    with pytest.raises(LookupError):
        await concept_doc.synthesize_concept_doc(_FakeStore(), "kb", "missing")
