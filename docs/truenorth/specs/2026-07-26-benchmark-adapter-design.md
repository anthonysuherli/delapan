# Benchmark question-source adapters — phase-2 design (addendum)

**Date:** 2026-07-26
**Status:** draft (extends the approved 2026-07-26 context-eval-pipeline spec, which scoped
phase 2 as "question sources are an interface; arms and scoring never know where questions
came from")
**Vision goals served:** *Grounding preserved end to end* (external benchmarks exercise the
same provenance-carrying injection path); operationalizes external comparability for the
eval harness without touching engine behavior.

## Problem

The v1 harness proved the ablation on our own pinned-changelog corpus. To get discipline
diversity and externally comparable numbers, we need to run public benchmark datasets
(corpus + grounded QA + evidence ids) through the *same* runner and scorers. Survey and
shortlist: `docs/research/2026-07-26-benchmark-corpus-candidates.md`. First targets:

1. **watsonxDocsQA** (`ibm-research/watsonxDocsQA`, 1,144 docs / 75 QA, gold doc labels) —
   the small clean smoke corpus the adapter is built against.
2. **MultiHop-RAG** (`yixuantt/MultiHopRAG`, ~600 news articles / 2,556 queries, doc-id
   gold sets, includes `null` insufficient-evidence queries) — the first real
   cross-discipline numbers.

## Design decisions (each forced by something we know)

1. **Direct insertion, not the extraction pipeline.** Benchmark docs are inserted as
   findings via `store.insert_findings` — no LLM extraction, no `resolve_and_persist`.
   Two reasons: the write-time resolver would merge near-duplicate benchmark docs and
   corrupt the gold mapping, and extraction cost/lossiness adds nothing when the eval
   needs the corpus verbatim. (`memory.enabled` stays untouched; we simply don't go
   through the resolver for benchmark KBs.)
2. **Chunking to the renderer's truncation limit.** `_render_finding` truncates content
   at 1,200 chars, so docs are split into ~1,000-char chunks (paragraph-boundary
   preferred, no mid-word cuts); one finding per chunk. `title` = `"{doc_title} [i/n]"`,
   `provenance` = `[{"url": <dataset doc id>, "query": <dataset name>}]`.
3. **Gold mapping via provenance, evidence-chunk-precise where possible.** Baseline: a
   question's `gold_finding_ids` = every finding whose provenance doc id is in the
   dataset's gold evidence set. When the dataset provides evidence TEXT per gold doc
   (MultiHop-RAG does), gold narrows to the chunk(s) containing that evidence
   (normalized substring match, spanning-boundary tolerant), falling back to all of the
   doc's chunks when no chunk matches — the fallback is counted in the lockfile
   (`"gold_fallback_questions"`). The adapter also writes a sidecar lockfile
   `evals/adapters/<name>.lock.json`:
   `{"dataset", "revision", "doc_to_finding_ids": {...}, "kb": ..., "counts": ...}` so
   doc-level analysis (did we hit the right *document*) is derivable from records later.
4. **Unanswerable mapping.** MultiHop-RAG `null` queries (insufficient evidence) map to
   our `type: unanswerable` with empty `gold_finding_ids` — the abstention row works
   unchanged. watsonxDocsQA has no such rows; its set is answerable-only (the report
   already renders that correctly).
5. **No synopsis for benchmark KBs.** Synopsis is an LLM artifact of the ingest pipeline;
   benchmark preambles render findings-only. Noted in run reports via the manifest's
   config snapshot (no code change needed — `render_preamble` already handles empty
   synopsis).
6. **Subsampling is explicit and seeded.** MultiHop-RAG's 2,556 queries would cost ~10×
   the v1 run. The adapter takes `--sample N --seed S` and stratifies by the dataset's
   question type (inference/comparison/temporal/null), recording sample parameters in
   the emitted set yaml header and lockfile. Default N=150.
7. **Data acquisition via the `datasets` library**, behind a new optional extra
   `evals-adapters = ["datasets>=3.0"]`. Dataset revision (commit hash) is pinned in the
   lockfile at build time — same discipline as the corpus manifest and HHEM pin.
8. **Same tiers as v1.** Adapter KBs build on the local SQLite tier
   (`DELAPAN_BACKEND=local`), embeddings via the existing `embed_batch`.
9. **Oracle budget override (the chunking consequence).** With chunked docs, a
   question's gold set can exceed the default 7,000-char preamble budget, which would
   silently clip the oracle arm and break its ceiling semantics. `build_context` gains
   `oracle_budget: int | None = None` (None keeps today's behavior byte-identical) and
   `run_eval`/the CLI gain `--oracle-budget`; the value is recorded in the run manifest.
   Adapter-emitted set yamls note the recommended value (e.g. 24,000 for MultiHop-RAG).
   This changes only `evals/` code, never the engine.

## Components

```
evals/adapters/
  __init__.py
  common.py             # chunk_doc(), insert_chunks() (embed_batch + insert_findings),
                        # write_set_yaml(), write_lockfile() — pure/IO split
  watsonx_docsqa.py     # load dataset -> chunks -> KB delapan-evals/watsonx-docsqa
                        #   -> evals/sets/watsonx-docsqa-v1.yaml (75 q)
  multihop_rag.py       # load dataset -> chunks -> KB delapan-evals/multihop-rag
                        #   -> evals/sets/multihop-rag-v1.yaml (stratified sample)
tests/evals/
  test_adapter_common.py    # chunking bounds/boundaries, gold-mapping, yaml validity
                            # (loads back through load_question_set), all hermetic with
                            # in-memory fake dataset rows — `datasets` NOT required
  test_adapter_smoke_live.py  # opt-in (RUN_EVALS_ADAPTER_SMOKE=1): watsonxDocsQA
                              # end-to-end build + 3-question run
```

CLI: module mains — `python -m evals.adapters.watsonx_docsqa [--kb ...]`,
`python -m evals.adapters.multihop_rag [--sample 150 --seed 0]`. (Not added to
`evals/__main__.py`; adapters are build-time tools, runs stay `python -m evals run`.)

## Error handling

- Missing `datasets` dep → ImportError naming the `evals-adapters` extra (same pattern
  as HHEM).
- A gold doc id absent from the corpus (dataset inconsistency) → the question is DROPPED
  and counted in the lockfile (`"dropped_questions"`), never silently kept with partial
  gold — the mem0/Zep lesson applied to adapters.
- Chunk-embedding failures → build aborts (a partially embedded corpus would silently
  skew retrieval); the KB is created only after all embeddings succeed.

## Testing

- Hermetic: chunker (boundary, unicode, short-doc passthrough), gold mapping incl. the
  dropped-question path, emitted yaml round-trips through `load_question_set`, lockfile
  shape. Fake rows, no network, no `datasets` import.
- Opt-in live smoke as above.

## Success criteria (phase-2a done when)

1. `python -m evals.adapters.watsonx_docsqa` builds the KB + set from a pinned revision,
   and `python -m evals run --set evals/sets/watsonx-docsqa-v1.yaml ...` completes all
   three arms with an artifact whose manifest carries the set sha.
2. `python -m evals.adapters.multihop_rag --sample 150 --seed 0` does the same, with the
   `null`-query rows appearing in the report's unanswerable section.
3. Hermetic suite still green offline without `datasets` installed.
4. Reports for both runs committed under `evals/reports/`.

## Non-goals

- No LongMemEval adapter yet (session-structured; separate follow-up as originally scoped).
- No doc-level retrieval metrics in the report itself (derivable from records + lockfile;
  revisit if the finding-level numbers prove misleading).
- No RAGBench/FinanceBench/QASPER/LegalBench-RAG adapters in this slice — same seam,
  added on demand once the two above prove the shape.
