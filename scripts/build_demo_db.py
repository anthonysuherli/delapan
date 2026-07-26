"""Build data/demo.db — the bundled zero-key demo KB (maintainer-run).

    demo_findings.yaml ──► embed_batch ──► SQLiteStore(data/demo.db) ──► commit artifact

Usage: uv run --extra local python scripts/build_demo_db.py
Needs AI_GATEWAY_API_KEY (or OPENAI_API_KEY) in .env at build time only.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "data" / "demo.db"
SRC = REPO / "scripts" / "demo_findings.yaml"


async def main() -> None:
    from delapan.core.clients.embeddings import embed_batch
    from delapan.store.sqlite import SQLiteStore

    data = yaml.safe_load(SRC.read_text(encoding="utf-8"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()  # fully derived artifact — always rebuild from scratch

    store = SQLiteStore(str(OUT))
    org_id, project_id = store.resolve_project("delapan", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "demo", create=True)

    texts = [f"{f['title']}. {f['summary']}" for f in data["findings"]]
    embeddings = await embed_batch(texts)
    rows = [
        {
            "id": uuid.uuid4().hex[:8],
            "org_id": org_id,
            "kb_id": kb_id,
            "title": f["title"],
            "content": {"summary": f["summary"]},
            "category": f.get("category", "fact"),
            "confidence": 0.95,
            "tags": f.get("tags", []),
            "provenance": [],
            "embedding": emb,
        }
        for f, emb in zip(data["findings"], embeddings, strict=True)
    ]
    ids = await store.insert_findings(rows)
    store.upsert_synopsis(
        kb_id, data["synopsis"], finding_count=len(ids), model="handcrafted"
    )
    print(f"demo.db built: {len(ids)} findings, {len(data['synopsis'])} synopsis entries → {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
