"""Record embeddings for a golden set — findings AND queries — into its sidecar.

    <name>.yaml ──► embed_batch(findings + queries) ──► <name>.vectors.json

Run once per set (needs AI_GATEWAY_API_KEY); the committed vectors keep the
test suite offline. Re-run only when deliberately re-recording against a new
embedding model — then update `embedding_model:` in the yaml too.

    uv run python scripts/gen_golden_embeddings.py tests/golden/delapan_engine.yaml
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import yaml

from delapan.core.clients.embeddings import embed_batch


async def main(path_str: str) -> None:
    path = Path(path_str)
    spec = yaml.safe_load(path.read_text())
    findings = spec["findings"]
    queries = [q["query"] for q in spec["queries"]]

    texts = [f"{f['title']}\n\n{f['content']}" for f in findings] + queries
    vecs = await embed_batch(texts)

    out: dict[str, list[float]] = {}
    for f, v in zip(findings, vecs[: len(findings)]):
        out[f["id"]] = [round(x, 6) for x in v]
    for q, v in zip(queries, vecs[len(findings) :]):
        out[f"q:{q}"] = [round(x, 6) for x in v]

    sidecar = path.with_suffix(".vectors.json")
    sidecar.write_text(json.dumps(out))
    print(f"wrote {sidecar} ({len(out)} vectors)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
