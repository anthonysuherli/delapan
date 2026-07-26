"""Shared benchmark-adapter machinery: chunk docs into findings, map gold.

    dataset rows ──► chunk_doc ──► Chunk(finding_id=sha1(doc#i)) ──► insert_chunks
    gold doc ids + evidence text ──► gold_chunk_ids ──► gold_finding_ids
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from delapan.core.clients.embeddings import embed_batch
from delapan.store import Store


@dataclass(frozen=True)
class Chunk:
    """A portion of a document, identified deterministically by doc_id + index."""

    doc_id: str
    doc_title: str
    index: int
    total: int
    text: str
    finding_id: str


def _finding_id(doc_id: str, index: int) -> str:
    """SHA1(doc_id#index)[:32] — deterministic chunk identifier."""
    return hashlib.sha1(f"{doc_id}#{index}".encode()).hexdigest()[:32]


def chunk_doc(
    doc_id: str, title: str, text: str, max_chars: int = 1000
) -> list[Chunk]:
    """Split on paragraph boundaries first, then whitespace — never mid-word.
    Empty or whitespace-only text yields no chunks. Single tokens >max_chars
    hard-split (no boundary available)."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    pieces: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = f"{buf}\n\n{p}" if buf else p
            continue
        if buf:
            pieces.append(buf)
            buf = ""
        while len(p) > max_chars:  # oversized para: split on whitespace
            cut = p.rfind(" ", 0, max_chars)
            # a single token >max_chars has no boundary — hard split only option
            cut = cut if cut > 0 else max_chars
            pieces.append(p[:cut].rstrip())
            p = p[cut:].lstrip()
        buf = p
    if buf:
        pieces.append(buf)
    if not pieces:
        return []
    total = len(pieces)
    return [
        Chunk(
            doc_id=doc_id,
            doc_title=title,
            index=i,
            total=total,
            text=t,
            finding_id=_finding_id(doc_id, i),
        )
        for i, t in enumerate(pieces)
    ]


def _norm(s: str) -> str:
    """Normalize whitespace and case for evidence matching."""
    return re.sub(r"\s+", " ", s).strip().lower()


def gold_chunk_ids(
    chunks: list[Chunk],
    gold_doc_ids: list[str],
    evidence_texts: list[str] | None = None,
) -> tuple[list[str], bool]:
    """Finding ids for a question's gold docs; narrowed to evidence-bearing chunks
    when evidence text is given. Returns (ids, used_fallback). Empty ids = gold doc
    absent."""
    gold_set = set(gold_doc_ids)
    doc_chunks = [c for c in chunks if c.doc_id in gold_set]
    if not doc_chunks:
        return [], False
    if evidence_texts:
        hits = [
            c
            for c in doc_chunks
            if any(_norm(e) in _norm(c.text) for e in evidence_texts if e.strip())
        ]
        if hits:
            return [c.finding_id for c in hits], False
        return [c.finding_id for c in doc_chunks], True  # counted fallback
    return [c.finding_id for c in doc_chunks], False


def load_hf(
    name: str, config: str, split: str, revision: str | None = None
) -> tuple[list[dict], str]:
    """Load a HF dataset split at a pinned revision. Returns (rows, revision_sha)."""
    try:
        from datasets import load_dataset
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise ImportError(
            'adapters need the optional extra: uv pip install -e ".[evals-adapters]"'
        ) from exc
    sha = revision or HfApi().dataset_info(name).sha
    ds = load_dataset(name, config, split=split, revision=sha)
    return list(ds), sha


async def insert_chunks(
    store: Store,
    *,
    org_id: str,
    kb_id: str,
    chunks: list[Chunk],
    dataset: str,
    batch_size: int = 128,
) -> int:
    """Embed EVERYTHING first, insert only after all embeddings succeed —
    a partially embedded corpus would silently skew retrieval."""
    embeddings: list[list[float]] = []
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        embeddings.extend(
            await embed_batch([f"{c.doc_title}\n\n{c.text}" for c in batch])
        )
    rows = [
        {
            "id": c.finding_id,
            "org_id": org_id,
            "kb_id": kb_id,
            "title": f"{c.doc_title} [{c.index + 1}/{c.total}]",
            "content": c.text,
            "category": "benchmark-doc",
            "confidence": 0.5,
            "tags": [],
            "provenance": [{"url": c.doc_id, "query": dataset}],
            "embedding": e,
        }
        for c, e in zip(chunks, embeddings)
    ]
    await store.insert_findings(rows)
    return len(rows)


def write_set_yaml(
    path: Path, name: str, questions: list[dict], header: str
) -> None:
    """Write question set YAML with header."""
    body = yaml.safe_dump(
        {"name": name, "questions": questions},
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    Path(path).write_text(header + body)


def write_lockfile(path: Path, lock: dict) -> None:
    """Write lock dict as JSON with indent=2, sort_keys=True."""
    Path(path).write_text(json.dumps(lock, indent=2, sort_keys=True))
