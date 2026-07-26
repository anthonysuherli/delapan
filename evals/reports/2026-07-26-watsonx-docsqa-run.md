# Eval report — watsonx-docsqa-v1

- git: `8c6b1d1`  answerer: `anthropic/claude-sonnet-4.6`  judge: `anthropic/claude-sonnet-4.6`  created: 2026-07-26T17:23:58Z
- records: 225 (0 unscored)

## Accuracy (answerable questions)

| arm | n | accuracy | 95% CI | mean tokens | eff/1k tok | faithfulness |
|---|---|---|---|---|---|---|
| closed_book | 75 | 0.693 | [0.587, 0.787] | 0 | 0.000 | n/a |
| oracle | 75 | 0.893 | [0.813, 0.960] | 2576 | 0.078 | n/a |
| production | 75 | 0.987 | [0.960, 1.000] | 1456 | 0.201 | n/a |

## Unanswerable questions (abstention rate)

| arm | n | abstained |
|---|---|---|

## McNemar production vs closed_book: p = 0.000 (prod-only correct: 23, closed-only correct: 1, paired n = 75)
