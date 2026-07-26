# Benchmark corpus candidates for the evals harness (survey 2026-07-26)

Cross-discipline corpora with grounded QA + evidence ids, for plugging into the
closed-book/production/oracle ablation (`evals/`). Full survey below the shortlist.
Harness fit note: our gold unit is `gold_finding_ids` (extracted findings), so an
adapter maps each dataset's gold DOC/PASSAGE id -> the findings whose provenance
cites that document. Doc-level evidence is the easiest mapping; span-level also
works (span's parent doc -> findings).

## Shortlist (best corpus + QA + evidence-id packages)

| # | Dataset | Discipline | Evidence granularity | License | Contamination |
|---|---|---|---|---|---|
| 1 | PatronusAI/financebench (+ PDFs on GitHub) | finance (10-K/Q filings) | page+excerpt strings | Apache-2.0 | moderate (public filings, pre-cutoff) |
| 2 | LegalBench-RAG (github zeroentropy-ai) | law (714 contracts) | CHAR-SPAN gold (best in survey); public P@K baselines to compare against | mixed (CUAD CC BY 4.0; ContractNLI CC BY-NC) | moderate |
| 3 | allenai/qasper | science (NLP papers) | paragraph/sentence spans | CC BY 4.0 | moderate |
| 4 | yixuantt/MultiHopRAG | news (Sep-Dec 2023) | doc-id sets, 2-4 docs/question, multi-hop | research/non-commercial | LOW (3-month window) |
| 5 | galileo-ai/ragbench | 12-subset aggregate (finqa, tatqa, cuad, pubmedqa, techqa, hotpotqa, covidqa, ...) | passage-level relevance (TRACe) | Apache-2.0 top-level, verify per subset | mixed |

Also noteworthy: ibm-research/watsonxDocsQA (1,144 docs / 75 QA, gold doc labels — small+clean
smoke corpus), databricks/officeqa (Treasury Bulletins 1939-2025, tables+narrative),
bigbio/bioasq_task_b (snippet-level gold + PMIDs, needs PubMed pull), LongMemEval
(xiaowu0162/longmemeval-cleaned — the planned phase-2 adapter target; session-level oracle evidence).

## Fit ranking for OUR harness

1. **MultiHop-RAG first**: bundled corpus, doc-level gold (cleanest -> gold_finding_ids mapping),
   multi-hop question types the current v1 set lacks, lowest contamination. Non-commercial license
   fine for internal eval.
2. **watsonxDocsQA** as the tiny adapter smoke test before wiring anything big.
3. **FinanceBench + QASPER** for discipline spread (finance narrative + scientific).
4. **LegalBench-RAG** when we want externally comparable retrieval numbers (its paper publishes P@K).
5. **RAGBench** if/when we want breadth in one schema instead of per-dataset adapters.

## Caveats carried from the survey

- Nothing here is truly post-cutoff except living benchmarks (FreshQA/RealTimeQA — no fixed corpus,
  poor fit). Our own pinned-changelog corpus stays the low-contamination arm; these add discipline
  diversity and external comparability, not contamination immunity.
- CC BY-NC components (ContractNLI, parts of LegalBench) block any commercial redistribution of
  results artifacts — fine internally.
- QASA, HaluBench, FaithBench, CLAPNQ had no stable canonical HF org at survey time — re-verify ids.
- CRAG: realistic/hard but no per-answer gold doc ids -> skip until we want an unlabeled stress test.

## Per-discipline candidates (condensed)

**Finance** — FinanceBench (150 open QA, ~360 filings, PDFs on GitHub, Apache-2.0);
FinQA `ibm-research/finqa` + `DEXTER-CQA/FinQA-Corpus` (8.2k QA, numerical programs);
ConvFinQA `AdaptLLM/ConvFinQA` (multi-turn); TAT-QA `next-tat/TAT-QA` + DEXTER corpus
(16.5k QA, table+text, CC BY-SA); T2-RAGBench `G4KMU/t2-ragbench` (23k QA aggregate, 2025);
FiQA `BeIR/fiqa` (forum text, retrieval-only).

**Medicine** — BioASQ Task B `bigbio/bioasq_task_b` (snippet spans + gold PMIDs, CC BY 2.5,
corpus = PubMed pulled separately); PubMedQA `bigbio/pubmed_qa` (yes/no/maybe, MIT);
CovidQA via `galileo-ai/ragbench` covidqa config (CORD-19 passages bundled); MedQA
`cogbuji/medqa_corpus_en` + `GBaker/MedQA-USMLE-4-options` (MCQ, weak for RAG).

**Law** — LegalBench-RAG (6,858 QA / 714 docs, char-span gold, mixed licenses);
CUAD `theatticusproject/cuad-qa` (clause spans, CC BY 4.0); ContractNLI (NLI format,
CC BY-NC); LegalBench `nguha/legalbench` (162 tasks, only QA subtasks corpus-grounded);
pile-of-law (raw corpus only, ingestion stress test).

**Science** — QASPER `allenai/qasper` (5k QA / 1,585 papers, evidence fields, CC BY 4.0);
SciFact `allenai/scifact` (claim verification, sentence rationales, CC BY-NC);
QASA (GitHub-first, verify mirror).

**News** — MultiHop-RAG `yixuantt/MultiHopRAG` (2,556 queries, doc-id gold, 600+ articles
Sep-Dec 2023); CRAG `facebookresearch/CRAG` + `crag-mm-2025/*` (hard, no gold doc ids);
FreshQA / RealTimeQA (living, no fixed corpus).

**Wikipedia (sanity-check tier only — heavy pretraining contamination)** — KILT
`facebook/kilt_tasks` + `kilt_wikipedia` (char-offset provenance, best tooling);
HotpotQA (sentence-level supporting facts); NQ; TriviaQA.

**Hallucination-annotated (faithfulness-judge validation, not retrieval)** — RAGTruth
`wandb/RAGTruth-processed` (18k responses, char-span hallucination labels);
RAGBench `galileo-ai/ragbench` (100k, 12 subsets, TRACe passage annotations, Apache-2.0);
FaithBench (github vectara, span labels); HaluBench (composite, id unstable); CLAPNQ
(primeqa GitHub, passage gold, NQ-derived contamination).

**Memory / phase 2** — LongMemEval `xiaowu0162/longmemeval-cleaned` (500 Q, session-level
oracle evidence, ICLR 2025; the planned adapter target); LoCoMo (skip as primary — known
gameability + label problems; third-party HF mirrors only).

**Other** — TechQA `rojagtap/tech-qa` + IBM Technotes corpus (enterprise support,
non-commercial); `nvidia/TechQA-RAG-Eval`; `ibm-research/watsonxDocsQA` (1,144 docs /
75 QA, gold doc labels — clean small smoke corpus); `databricks/officeqa` (Treasury
Bulletins 1939-2025); DoD policy QA `CDAO/dod-policy-qa`; ImmigrationQA + EU-AI-Act
bench (arXiv-first, mirrors unverified).

