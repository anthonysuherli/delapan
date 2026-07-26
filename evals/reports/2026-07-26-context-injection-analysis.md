# Context injection: three-corpus ablation analysis

**Date:** 2026-07-26 · **Engine:** delapan open-core (master, `a0ca5ed`) · **Answerer & judge:** `anthropic/claude-sonnet-4.6` · **Harness:** `evals/` (spec: `docs/truenorth/specs/2026-07-26-context-eval-pipeline-design.md`)

## Executive summary

We ran delapan's preamble injection through a paired closed-book / production / oracle ablation on three corpora spanning three regimes: a post-cutoff private-ish corpus (2026 tooling changelogs), an enterprise documentation benchmark (watsonxDocsQA), and a multi-hop news benchmark (MultiHop-RAG). 852 records, zero unscored, every comparison paired by question.

Three conclusions, each carried by a different corpus:

1. **Injection eliminates or massively reduces hallucination on every corpus** — +89, +29, and +30 accuracy points over closed-book, McNemar p ≈ 0.000 throughout.
2. **Banded chunk retrieval beats handing the model whole correct documents.** On watsonxDocsQA, production (98.7%, 1,456 tokens) outscored the doc-level oracle (89.3%, 2,576 tokens). Every oracle failure was an *abstention* on a bloated injection — precision beats volume.
3. **Multi-hop retrieval completeness is the engine's one measured weakness.** On MultiHop-RAG, production trails oracle by 19 points, and the diagnostics localize it exactly: the full gold evidence set was injected for only 11% of questions, while the coverage verdict claimed `rich` 92% of the time.

## Method (one paragraph)

Each question runs through up to three arms with the same answerer model: **closed-book** (no context — the parametric floor), **production** (the real `select_preamble` path: embed → band → budgeted XML preamble), and **oracle** (gold findings rendered directly, bypassing retrieval — the ceiling). Answers are graded by a reference-based LLM judge (rubric + chain-of-thought, structured verdict); unanswerable questions score correct only on abstention and are always reported separately. Statistics are paired: McNemar's exact test on discordant pairs, bootstrap 95% CIs (10k resamples, seeded). Every run pins git SHA, model IDs, prompts, config, dataset revisions, and raw per-question records. Benchmark corpora are chunked (~1,000 chars) and inserted verbatim (no LLM extraction, no dedup resolver) with deterministic ids, so gold evidence maps exactly onto findings.

## Headline results

| corpus | regime | n (ans.) | closed-book | production | oracle | McNemar prod vs closed |
|---|---|---|---|---|---|---|
| changelogs `public-v1` | post-cutoff, single-hop | 54 | 0.111 [0.04, 0.20] | **1.000** | 1.000 (127 tok) | p≈0.000 (48/0) |
| watsonxDocsQA | enterprise docs, single-doc | 75 | 0.693 [0.59, 0.79] | **0.987** [0.96, 1.00] | 0.893 [0.81, 0.96] | p≈0.000 (23/1) |
| MultiHop-RAG (sample 150, seed 0) | news, 2–4-doc hops | 132 | 0.364 [0.28, 0.45] | 0.659 [0.58, 0.74] | **0.848** [0.79, 0.91] | p≈0.000 (41/2) |

Abstention on unanswerable questions (changelogs n=5, MultiHop `null` n=18): oracle 1.00/0.89, production 0.80/0.78, closed-book 0.80/0.83. All arms abstain most of the time; production's misses include one judge-labeling artifact (below).

## Finding 1 — Injection works everywhere, and the closed-book arm doubles as a contamination meter

The closed-book gradient is exactly what corpus recency predicts: **11%** on post-cutoff changelogs → **36%** on Q4-2023 news → **69%** on long-public IBM docs. This is the designed contamination-visibility mechanism working: on public corpora, part of the "closed-book" score is pretraining leakage, which *shrinks the apparent value of injection without shrinking its real value*. The changelog corpus — content structurally absent from pretraining — is therefore the cleanest measure of injection's ceiling: 11% → 100%.

The discordant pairs make the hallucination-reduction claim precise: across the three corpora, production fixed 48+23+41 = 112 questions closed-book got wrong, while closed-book beat production on just 0+1+2 = 3. The harness also caught closed-book confabulating outright (e.g., inventing a nonexistent "Pydantic pure-Python build variant" with a confident version number) — the failure mode grounding exists to prevent, observed directly.

## Finding 2 — Precision beats volume: production outscored the whole-document oracle

On watsonxDocsQA the "ceiling" arm lost to production, 89.3% vs 98.7%. The per-record diagnosis removes the mystery:

- **All 8 oracle failures are abstentions, not wrong answers.** Each occurred on a gold document that chunked into 23+ pieces (~5,400 tokens injected — well under the 24k oracle budget, so nothing was clipped). These are glossary/definition questions: the answer is one line buried in a wall of sibling chunks, and the model declined rather than dig it out.
- **Production found the needle instead of the haystack**: on those same questions it injected 6–7 high-similarity chunks (the definition chunk among them) and answered correctly 7 of 8.
- Production injected chunks from *non-gold* documents in 70/75 records and had the complete doc-level gold set in only 21/75 — yet scored 98.7%. Doc-level gold overstates what's actually needed; similarity-ranked chunks across documents are the better context.

This is "context rot" observed in our own harness: more context degraded behavior even at modest lengths, and the band/budget machinery — the thing this pipeline was built to evaluate — is demonstrably earning its keep on single-hop questions. It also means doc-granularity oracle arms *underestimate* the ceiling; evidence-chunk gold (as used for MultiHop-RAG) is the more honest ceiling construction.

## Finding 3 — Multi-hop retrieval completeness is the measured bottleneck

MultiHop-RAG is the first corpus where production trails oracle (0.659 vs 0.848), and the records pinpoint why:

| production arm (answerable n=132) | multi-hop (n=98) | temporal (n=34) |
|---|---|---|
| accuracy | 0.69 | 0.56 |
| ALL gold evidence chunks injected | 0.09 | 0.18 |
| ANY gold evidence chunk injected | 0.77 | 0.68 |

- Accuracy when the full gold set was injected: **0.87** (13/15). When incomplete: **0.63** (74/117). The production–oracle gap is retrieval loss, not generation loss — oracle proves the generator handles these questions fine when the evidence arrives (and at only 695 mean tokens).
- **The coverage verdict is over-confident here**: 122/132 records said `rich`. A single query embedding lands plenty of *similar* news chunks (band 1 fills up → `rich`), but similarity to the whole question ≠ coverage of each hop. The verdict measures the former; multi-hop needs the latter. This is the same shape as the historical false-`rich` band bug, recurring at the reasoning level rather than the vocabulary level.
- Partial evidence still helps (0.364 → 0.659 overall) — the system degrades to "answer from the hops you found," which the judge scores correct roughly when the found hops suffice.

**The lever this points at:** decompose multi-hop questions into per-hop sub-queries (or generate query expansions), retrieve per sub-query, and merge before banding. The eval gives a precise target: move all-gold-injected from 11% toward oracle, and expect production accuracy to track from 0.66 toward ~0.85. A cheaper complement: make the coverage verdict hop-aware (e.g., verdict per decomposed sub-query) so `rich` stops over-promising on compound questions.

## Finding 4 — Instrument notes (what to trust, and how far)

- **Judge quality:** verdicts were consistent and the reference-based rubric held up; but one abstention phrased as prose ("the knowledge base does not contain pricing for…") was labeled `correct` instead of `abstained`, costing production an abstention point on the changelog run. The rubric should map "declines because information is absent" to `abstained` explicitly. Same-family answerer and judge (both sonnet-4.6) is disclosed; reference-based grading limits self-preference, but a periodic cross-family judge pass remains the right validation.
- **Question provenance:** changelog questions were drafted from the same findings retrieval searches (leakage-filtered, but retrieval remains intentionally easy); benchmark questions are independent, which is exactly why MultiHop-RAG could expose the retrieval gap the self-generated set could not.
- **Faithfulness column is n/a** in all runs — HHEM is behind the optional `evals-hhem` extra (torch) and wasn't installed; hallucination reduction here rests on reference-based correctness deltas, which is the stronger claim anyway.
- **Determinism/reproducibility:** all three runs are reconstructible — pinned dataset revisions + chunk-id lockfiles + frozen sets (sha-pinned in manifests) + raw records. Re-rendering any report from its artifact is byte-identical.

## Threats to validity

1. Single answerer/judge model and single run per corpus (no seed variance on the LLM side); CIs cover question sampling, not model stochasticity — temperature 0 mitigates but does not eliminate.
2. Doc-level gold (watsonx) makes the oracle arm a lower bound on the true ceiling (Finding 2); comparisons *between* arms remain valid, absolute "ceiling" language should reference the evidence-chunk-gold corpora.
3. MultiHop-RAG is one 150-question stratified sample (seed 0) of 2,556; per-type splits (esp. temporal, n=34) carry wide implicit intervals.
4. Public-corpus closed-book scores include pretraining leakage (quantified, not eliminated — see Finding 1).
5. `rich`/`sparse` calibration conclusions are specific to the current band thresholds tuned for gemini-embedding-001.

## Recommendations, ranked

1. **Multi-query retrieval for compound questions** (engine work): decompose → retrieve per hop → merge → band. Measured by re-running `multihop-rag-v1` unchanged; target all-gold-injected ≥ 50% and production ≥ 0.80.
2. **Budget sweep on `public-v1`** (zero engine work, one command): the changelog run showed ~15× token headroom (oracle: 127 tokens for the same 100%); find the knee of the quality-vs-budget curve and consider lowering the 7,000-char default.
3. **Hop-aware coverage verdict**: verdict-per-sub-query, or at minimum flag compound questions; kills the 92%-`rich`-while-89%-incomplete over-confidence.
4. **Judge rubric patch**: map prose-abstentions to `abstained` (one-line prompt change; re-run affected sets to confirm no drift).
5. **Adopt evidence-span gold for future adapters** (LegalBench-RAG next — char-span gold, published P@K baselines for external comparability).

## Appendix — runs

| run | artifact | report |
|---|---|---|
| changelogs public-v1 | `evals/runs/20260726-145903/` | `evals/reports/2026-07-26-public-v1-first-run.md` |
| watsonxDocsQA | `evals/runs/20260726-172358/` | `evals/reports/2026-07-26-watsonx-docsqa-run.md` |
| MultiHop-RAG | `evals/runs/20260726-182715/` | `evals/reports/2026-07-26-multihop-rag-run.md` |

Corpus/set provenance: `evals/corpus/manifest.yaml` + `lockfile.json` (changelogs); `evals/adapters/*.lock.json` (pinned HF revisions, chunk maps); frozen sets under `evals/sets/`. Benchmark survey: `docs/research/2026-07-26-benchmark-corpus-candidates.md`.
