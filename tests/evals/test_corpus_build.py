"""Corpus build with injected fetcher + mocked extraction — no network."""
from __future__ import annotations

import json

from evals.corpus.build import build_corpus

MANIFEST = """\
name: public-v1
domain_note: 2026 AI-tooling release notes — post-cutoff by construction.
sources:
  - url: https://example.com/release-a
    note: project A 2026.07 release notes
"""


async def test_build_corpus_persists_and_locks(tmp_path, store, monkeypatch):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(MANIFEST)

    async def fake_fetch(url: str) -> str:
        return "Project A 2026.07 adds a context-eval harness with three arms."

    async def fake_extract(content, extraction_prompt, source_url, source_query, cfg):
        from delapan.core.exploration.models import ExtractionResult

        return ExtractionResult(
            findings=[{"title": "Project A 2026.07 eval harness",
                       "content": {"fact": "three ablation arms"},
                       "category": "release", "confidence": 0.8}],
            source_url=source_url, source_query=source_query, model_used="fake",
        )

    async def fake_embed_batch(texts):
        return [[1.0] + [0.0] * 1535 for _ in texts]

    monkeypatch.setattr("evals.corpus.build.extract_findings", fake_extract)
    monkeypatch.setattr("delapan.core.memory.persist.embed_batch", fake_embed_batch)
    monkeypatch.setattr(
        "evals.corpus.build.LOCKFILE", tmp_path / "lockfile.json"
    )

    lock = await build_corpus(manifest, project="evals-corpus-t", kb="v1", fetcher=fake_fetch)

    assert lock["sources"][0]["finding_count"] == 1
    assert len(lock["finding_ids"]) == 1
    assert json.loads((tmp_path / "lockfile.json").read_text()) == lock
    # findings actually landed in the store
    kb_row = store.resolve_kb(*store.resolve_project("evals-corpus-t", create=False), "v1",
                              create=False)
    assert store.count_findings(kb_row) == 1
