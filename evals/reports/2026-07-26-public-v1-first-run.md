# Eval report — public-v1

- git: `410a7a3`  answerer: `anthropic/claude-sonnet-4.6`  judge: `anthropic/claude-sonnet-4.6`  created: 2026-07-26T14:59:03Z
- records: 177 (0 unscored)

## Accuracy (answerable questions)

| arm | n | accuracy | 95% CI | mean tokens | eff/1k tok | faithfulness |
|---|---|---|---|---|---|---|
| closed_book | 54 | 0.111 | [0.037, 0.204] | 0 | 0.000 | n/a |
| oracle | 54 | 1.000 | [1.000, 1.000] | 127 | 7.000 | n/a |
| production | 54 | 1.000 | [1.000, 1.000] | 1959 | 0.454 | n/a |

## Unanswerable questions (abstention rate)

| arm | n | abstained |
|---|---|---|
| closed_book | 5 | 0.800 |
| oracle | 5 | 1.000 |
| production | 5 | 0.800 |

## McNemar production vs closed_book: p = 0.000 (prod-only correct: 48, closed-only correct: 0, paired n = 54)
