# Context-injection eval pipeline — design

**Date:** 2026-07-26
**Status:** approved (brainstorming session 2026-07-26)
**Vision goals served:** *Grounding preserved end to end* (faithfulness is measured against the injected, provenance-carrying findings); *Self-correcting memory writes* (the `memory.enabled` ablation quantifies dedup's effect on context efficiency); operationalizes the "resolution observably dedupes" acceptance criterion. The hermetic tier honors the offline-SQLite invariant.

## Problem

Delapan's product claim is that injecting curated KB context (the `<preamble>`) makes LLM sessions more grounded and cheaper than the alternatives. Nothing measures that today. The repo has seeds — golden query sets (`tests/test_golden_sets.py`), band calibration (`scripts/calibrate_bands.py`), per-query `access_events` telemetry — but no measurement of the *generated answer*, no token accounting, and no ablation harness. One failure mode is already documented and reproducible (`tests/golden/delapan_engine.yaml`: a false-`rich` verdict injecting off-topic findings) but only its retrieval half is tested.

This pipeline measures three things, repeatably and cheaply:

1. **Hallucination reduction** — how much does the preamble reduce unsupported/incorrect claims vs. no context?
2. **Context efficiency** — answer quality per token injected; where does the budget/depth curve flatten?
3. **Retrieval quality** — do the bands and coverage verdicts deliver the right findings, ranked right?

## Research grounding (why this shape)

Decisions below follow a 2026-07-26 research pass (24 findings in the `delapan/context-eval` KB + a deep-research report; sources cited there):

- **Paired three-arm ablation** (closed-book / production / oracle) is the credible proof shape for "context helps" (Google DeepMind "Sufficient Context", arXiv:2411.06037). The production-vs-oracle gap isolates retrieval loss from generation loss.
- **Public memory benchmarks are not a foundation.** LoCoMo has wrong gold labels, a trivial filesystem baseline matches specialized memory systems (Letta, 2025-08), and the mem0-vs-Zep dispute showed its scores aren't reproducible across vendors. LongMemEval (ICLR 2025) is the rigorous option — deferred to phase 2.
- **Reproducibility discipline is the mem0/Zep lesson**: pin model/API versions and prompts, publish raw per-question predictions, report variance, count unanswerable questions explicitly.
- **Cheap local faithfulness scoring works**: Vectara HHEM-2.1-Open (<600MB, CPU, beats GPT-4 on hallucination classification) for volume; frontier judge panels only for periodic validation. Reference-based judging beats reference-free by ~21 points (GroUSE ablation).
- **Known trap**: reference-free faithfulness scores go near-perfect on *empty* context — a `gap`-verdict or closed-book record must never receive a faithfulness score.
- **Small-n statistics**: paired-by-question McNemar + bootstrap CIs make a 50–150 question set defensible; that size is also the field's recommendation for regression gates.
- **Contamination**: eval corpus is public data (ratified in session), so the closed-book arm can leak pretraining knowledge. Mitigations: recent post-cutoff sources, per-question closed-book results kept visible for filtering, leakage-filtered question generation.

## Architecture

New top-level `evals/` package in this repo. Two tiers:

| Tier | Trigger | Network | What runs |
|---|---|---|---|
| **Hermetic** | pytest (with the normal suite) | none | retrieval + verdict metrics against committed embedding sidecars — extends the `tests/golden/` pattern |
| **Full** | manual/periodic CLI: `python -m evals run` | LLM + embeddings | ablation arms, answer generation, scoring, stats, run artifact |

```
question set (yaml) ──► arm runner ──► per-question records ──► scorers ──► stats ──► run artifact + report
        ▲                   │
  corpus manifest      select_preamble / render_preamble (real code paths)
```

### Components

**1. Corpus builder (`evals/corpus/`).** A committed manifest of pinned public source URLs/documents — recent, post-knowledge-cutoff content in one domain (default: 2026 AI-tooling release notes and announcements — fast-moving, verifiable, structurally post-cutoff; final source list picked at implementation against that criterion) — plus a rebuild script that ingests them through the real pipeline (dedup resolver, synopsis) into a dedicated eval KB on the local SQLite tier. Output: KB + a lockfile of resulting finding IDs/hashes so drift is detectable.

**2. Question set (`evals/sets/*.yaml`).** 50–150 questions, frozen and versioned. Each entry: `question`, `reference_answer`, `gold_finding_ids`, `type` ∈ {single-hop, multi-hop, temporal, unanswerable}. Unanswerable questions are first-class and always reported as their own row — never mixed into the headline accuracy (the exact numerator/denominator trick that inflated Zep's LoCoMo score by ~25 points). Generation: LLM-drafted from findings → lexical-leakage filter (paraphrase away from source wording) → one human curation pass → frozen.

**3. Arm runner (`evals/runner.py`).** Per question, paired arms:

| Arm | Context handed to the answerer |
|---|---|
| `closed_book` | none — parametric floor |
| `production` | real `select_preamble(query, …)` output |
| `oracle` | gold findings rendered via `render_preamble` directly (retrieval bypassed) — ceiling |
| `full_context` (optional flag) | all live findings up to a cap — tests banding vs. "inject everything" |

Answerer: one pinned model via the AI Gateway, minimal system template mirroring `core/canvas/answer.py` (`_SYSTEM_TEMPLATE`). Per-record capture: answer text, exact preamble XML, chars injected, tokens injected (one pinned tokenizer for all runs, e.g. tiktoken `o200k_base` — an approximation is fine since the metric is only compared across arms/runs; the engine budget itself is chars), coverage verdict, band counts, latency, cost estimate.

Config sweeps (band thresholds, `preamble_char_budget`, `depth`, `memory.enabled`) use the existing `DLP_<SECTION>__<FIELD>` env mechanism; the runner calls `get_config.cache_clear()` between sweep points (known lru_cache gotcha).

**4. Scorers (`evals/scoring/`).**

- *Correctness*: reference-based LLM judge — rubric + chain-of-thought, structured JSON verdict, pinned judge model. For `unanswerable` questions, correct = abstention.
- *Faithfulness*: HHEM-2.1-Open locally (optional `[evals]` extra) scoring answer vs. injected context. Guard: records with no injected context (closed-book arm, `gap` verdicts that injected nothing) are exempt from faithfulness — hallucination reduction is instead the unsupported/incorrect-claim rate vs. the reference, compared across arms. A 2–3-model judge panel validates a sample of HHEM scores periodically (not every run).
- *Retrieval*: Precision@K, Recall@K, MRR, NDCG against `gold_finding_ids`; **coverage-verdict calibration** — P(gold findings actually injected | verdict=`rich`) and its converse. The false-`rich` failure mode becomes a permanently tracked number.
- *Context efficiency*: (production accuracy − closed-book accuracy) per 1k injected tokens; budget-sweep curves (quality vs. `preamble_char_budget` × `depth`).
- Scorer failure marks the record `unscored` — never a default score. (Deliberate contrast with `core/exploration/evaluator.py`, which defaults failures to quality 1.0; acceptable there, not in an eval.)

**5. Stats (`evals/stats.py`).** Pure functions: McNemar's test on paired arm-vs-arm correctness; bootstrap CIs (10k resamples) on every reported rate. No point estimate is reported without an interval.

**6. Run artifacts (`evals/runs/<timestamp>/`).** Each run writes: git SHA, full resolved config snapshot, answerer/judge model IDs and prompts, question-set version, corpus lockfile hash, and **raw per-question records** (all arms, all scores). A `report` command renders a markdown summary (headline table, per-type breakdown, unanswerable row, efficiency curve data) from the artifact alone — reports are reproducible from artifacts without re-running.

### Error handling

- Missing API keys → the full tier exits up front naming the variable (matching the explore skill convention); the hermetic tier is unaffected.
- Answerer/judge call failures → retry once, then record `unscored` with the error; the run completes and the report shows the unscored count. A run with >10% unscored records is flagged non-comparable.
- Fire-and-forget engine telemetry (`_BG_TASKS`) is drained before reading `access_events`-derived numbers.

### Testing

- Scorers and stats are pure-function tested with fixed fixtures (known answers → known metric values; McNemar/bootstrap against hand-computed cases).
- The hermetic tier itself runs in the normal offline pytest suite against the SQLite `store` fixture, seeded from committed sidecars — no network, no cloud.
- One end-to-end smoke (marked, opt-in) runs 3 questions × 2 arms against live keys.

## Phase 2 (scoped now, built later): LongMemEval adapter

Question sources are an interface: arms and scorers never know where questions came from. Phase 2 adds an adapter that ingests LongMemEval chat histories into KBs and runs its 500 questions (including its abstention set) through the same runner + scorers, producing externally comparable numbers under the same artifact discipline. LoCoMo is explicitly skipped (gameable, bad labels, unreproducible vendor scores).

## Non-goals

- No dashboard/UI; markdown reports from artifacts only.
- No CI wiring (the repo has no test workflow today); the hermetic tier simply joins the local pytest run.
- No LoCoMo. No leaderboard chasing.
- No per-run frontier judge panels — panel validation is periodic and sampled.
- No changes to engine behavior: the harness consumes `select_preamble`/`render_preamble`/`Store` as-is. Knob changes motivated by eval results are separate, later changes.

## Success criteria (v1 done when)

1. `python -m evals run --arms closed_book,production,oracle` completes on the seeded public-data KB and writes a full artifact.
2. The report shows headline deltas with bootstrap CIs and a McNemar p-value for production vs. closed-book, with unanswerable questions reported separately.
3. Coverage-verdict calibration and retrieval metrics reproduce the known false-`rich` case as a measured (not anecdotal) number on the hermetic tier.
4. A budget sweep (≥3 budgets × 2 depths) produces a quality-vs-tokens curve from one command.
5. Re-running `report` on an existing artifact reproduces the same numbers byte-for-byte.
6. Full pytest suite (including the new hermetic tier and scorer/stats units) passes offline.
