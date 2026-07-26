# Eval report — multihop-rag-v1

- git: `300690a`  answerer: `anthropic/claude-sonnet-4.6`  judge: `anthropic/claude-sonnet-4.6`  created: 2026-07-26T18:27:15Z
- records: 450 (0 unscored)

## Accuracy (answerable questions)

| arm | n | accuracy | 95% CI | mean tokens | eff/1k tok | faithfulness |
|---|---|---|---|---|---|---|
| closed_book | 132 | 0.364 | [0.280, 0.447] | 0 | 0.000 | n/a |
| oracle | 132 | 0.848 | [0.788, 0.909] | 695 | 0.698 | n/a |
| production | 132 | 0.659 | [0.576, 0.742] | 1489 | 0.198 | n/a |

## Unanswerable questions (abstention rate)

| arm | n | abstained |
|---|---|---|
| closed_book | 18 | 0.833 |
| oracle | 18 | 0.889 |
| production | 18 | 0.778 |

## McNemar production vs closed_book: p = 0.000 (prod-only correct: 41, closed-only correct: 2, paired n = 132)

## Post-run retrieval diagnostics (production arm, answerable n=132)

| type | n | accuracy | all gold injected | any gold injected |
|---|---|---|---|---|
| multi-hop | 98 | 0.69 | 0.09 | 0.77 |
| temporal | 34 | 0.56 | 0.18 | 0.68 |

- Coverage verdicts: 122 rich / 10 sparse — the verdict is over-confident on
  multi-hop: single-query embedding retrieval finds SOME evidence (77% any-gold)
  but completes the full gold set only 11% of the time.
- Accuracy when all gold injected: 13/15 (0.87). When incomplete: 74/117 (0.63).
  The production-oracle gap (0.659 vs 0.848) is a RETRIEVAL loss, not a
  generation loss — the first corpus where the ablation localizes the bottleneck.
- Obvious lever: multi-query retrieval (decompose the question, retrieve per
  hop) or query expansion before banding.
