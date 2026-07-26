"""MultiHop-RAG -> eval KB + question set (evidence-narrowed gold, null->abstain).

    corpus(url,title,body) ─► chunk_doc ─► insert_chunks ─► KB
    queries ─► stratified seeded sample ─► gold_chunk_ids(evidence facts)
            ─► sets/multihop-rag-v1.yaml (+ null_query -> unanswerable)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import random
import time
from collections import defaultdict
from pathlib import Path

from delapan.mcp.tenancy import resolve_tenant
from delapan.store import get_store
from evals.adapters.common import (
    Chunk,
    chunk_doc,
    gold_chunk_ids,
    insert_chunks,
    load_hf,
    write_lockfile,
    write_set_yaml,
)

DATASET = "yixuantt/MultiHopRAG"
SET_PATH = Path(__file__).parent.parent / "sets" / "multihop-rag-v1.yaml"
LOCK_PATH = Path(__file__).parent / "multihop-rag.lock.json"
RECOMMENDED_ORACLE_BUDGET = 24_000


def _qid(query: str) -> str:
    """SHA1(query)[:12] — deterministic question identifier."""
    return f"mh-{hashlib.sha1(query.encode()).hexdigest()[:12]}"


def _our_type(question_type: str) -> str:
    """Map MultiHopRAG question_type to eval type."""
    t = question_type.lower()
    if "null" in t:
        return "unanswerable"
    if "temporal" in t:
        return "temporal"
    return "multi-hop"


def _stratified_sample(rows: list[dict], n: int, seed: int) -> list[dict]:
    """Deterministic proportional sample by raw question_type, >=1 per group.
    When more non-empty groups exist than n, keeps first n (by sorted type
    name) at quota=1, zeros the rest. Never decreases quota below 1 while
    another group can still contribute."""
    if n >= len(rows):
        return sorted(rows, key=lambda r: _qid(r["query"]))
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[str(r["question_type"])].append(r)
    for g in groups.values():
        g.sort(key=lambda r: _qid(r["query"]))
    names = sorted(groups)
    rng = random.Random(seed)
    quota = {k: max(1, round(n * len(groups[k]) / len(rows))) for k in names}
    while sum(quota.values()) > n:  # trim largest first (but never below 1)
        candidates = [x for x in names if quota[x] > 1]
        if not candidates:  # more non-empty groups than n
            for i, k in enumerate(names):
                quota[k] = 1 if i < n else 0
            break
        k = max(candidates, key=lambda x: quota[x])
        quota[k] -= 1
    while sum(quota.values()) < n:
        k = max(names, key=lambda x: len(groups[x]) - quota[x])
        quota[k] += 1
    picked: list[dict] = []
    for k in names:
        picked.extend(rng.sample(groups[k], min(quota[k], len(groups[k]))))
    return sorted(picked, key=lambda r: _qid(r["query"]))


async def build(
    *,
    project: str = "delapan-evals",
    kb: str = "multihop-rag",
    revision: str | None = None,
    sample: int = 150,
    seed: int = 0,
    max_docs: int | None = None,
) -> dict:
    """Load corpus + queries, chunk, sample, narrow gold, build KB + sets."""
    docs, rev = load_hf(DATASET, "corpus", "train", revision=revision)
    docs = sorted(docs, key=lambda d: str(d["url"]))[: max_docs or len(docs)]
    queries, _ = load_hf(DATASET, "MultiHopRAG", "train", revision=rev)

    chunks: list[Chunk] = []
    for d in docs:
        chunks.extend(chunk_doc(
            str(d["url"]), str(d["title"]), str(d["body"]),
            id_namespace=f"{project}/{kb}"
        ))

    ctx = resolve_tenant(project, kb, create=True)
    store = get_store()
    if store.count_findings(ctx.kb_id) > 0:
        raise RuntimeError(
            f"KB {project}/{kb} already has findings — use a fresh --kb "
            "(re-runs would collide on deterministic ids)"
        )
    n_chunks = await insert_chunks(
        store, org_id=ctx.org_id, kb_id=ctx.kb_id, chunks=chunks, dataset=DATASET
    )

    sampled = _stratified_sample(queries, sample, seed)
    questions: list[dict] = []
    dropped: list[str] = []
    fallbacks: list[str] = []
    chunks_by_doc = {c.doc_id for c in chunks}

    for row in sampled:
        qid = _qid(str(row["query"]))
        our_type = _our_type(str(row["question_type"]))
        if our_type == "unanswerable":
            questions.append(
                {"id": qid, "question": str(row["query"]), "reference_answer": "",
                 "gold_finding_ids": [], "type": "unanswerable"}
            )
            continue
        evidence = row.get("evidence_list") or []
        gold_docs = sorted({str(e["url"]) for e in evidence})
        facts = [str(e.get("fact", "")) for e in evidence]
        ids, fallback = gold_chunk_ids(chunks, gold_docs, evidence_texts=facts)
        if not ids or not all(d in chunks_by_doc for d in gold_docs):
            dropped.append(qid)
            continue
        if fallback:
            fallbacks.append(qid)
        questions.append(
            {"id": qid, "question": str(row["query"]),
             "reference_answer": str(row["answer"]),
             "gold_finding_ids": ids, "type": our_type}
        )

    header = (
        f"# Generated by evals.adapters.multihop_rag — dataset {DATASET}@{rev}\n"
        f"# sample={sample} seed={seed}. Gold narrowed to evidence chunks.\n"
        f"# Recommended run flag: --oracle-budget {RECOMMENDED_ORACLE_BUDGET}\n"
    )
    write_set_yaml(SET_PATH, "multihop-rag-v1", questions, header)

    doc_to_ids: dict[str, list[str]] = {}
    for c in chunks:
        doc_to_ids.setdefault(c.doc_id, []).append(c.finding_id)
    lock = {
        "dataset": DATASET, "revision": rev,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kb": f"{project}/{kb}", "max_docs": max_docs,
        "sample": sample, "seed": seed,
        "sampled_question_ids": [q["id"] for q in questions],  # post-drop: includes only kept Qs
        "counts": {"docs": len(docs), "chunks": n_chunks, "questions": len(questions)},
        "dropped_questions": dropped, "gold_fallback_questions": fallbacks,
        "doc_to_finding_ids": doc_to_ids,
    }
    write_lockfile(LOCK_PATH, lock)
    return lock


def main() -> int:
    """CLI entry point."""
    p = argparse.ArgumentParser(prog="evals.adapters.multihop_rag")
    p.add_argument("--project", default="delapan-evals")
    p.add_argument("--kb", default="multihop-rag")
    p.add_argument("--revision", default=None)
    p.add_argument("--sample", type=int, default=150)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-docs", type=int, default=None)
    a = p.parse_args()
    lock = asyncio.run(
        build(project=a.project, kb=a.kb, revision=a.revision,
              sample=a.sample, seed=a.seed, max_docs=a.max_docs)
    )
    print(f"{lock['kb']}: {lock['counts']} dropped={len(lock['dropped_questions'])} "
          f"fallback={len(lock['gold_fallback_questions'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
