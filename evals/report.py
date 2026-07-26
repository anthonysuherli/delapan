"""Markdown report from an artifact — deterministic, CIs on every rate.

    (manifest, records) ──► per-arm tables + McNemar + efficiency ──► markdown
"""

from __future__ import annotations

from collections import defaultdict

from evals.scoring.correctness import is_correct
from evals.scoring.efficiency import efficiency_per_1k
from evals.stats import bootstrap_ci, mcnemar_exact, paired_table


def _fmt(x: float) -> str:
    return f"{x:.3f}"


def render_report(manifest: dict, records: list[dict]) -> str:
    scored = [r for r in records if not r["unscored"]]
    unscored_n = len(records) - len(scored)
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for r in scored:
        by_arm[r["arm"]].append(r)

    lines = [
        f"# Eval report — {manifest['set_name']}",
        "",
        (f"- git: `{manifest['git_sha']}`  answerer: `{manifest['answer_model']}`  "
         f"judge: `{manifest['judge_model']}`  created: {manifest['created_at']}"),
        f"- records: {len(records)} ({unscored_n} unscored)",
    ]
    if records and unscored_n / len(records) > 0.10:
        lines.append("- **NON-COMPARABLE: more than 10% of records are unscored.**")

    lines += ["", "## Accuracy (answerable questions)", "",
              "| arm | n | accuracy | 95% CI | mean tokens | eff/1k tok | faithfulness |",
              "|---|---|---|---|---|---|---|"]

    closed_ans = [r for r in by_arm.get("closed_book", []) if r["question_type"] != "unanswerable"]
    closed_acc: float | None = (
        sum(1.0 for r in closed_ans if is_correct(r["question_type"], r["verdict"])) / len(closed_ans)
        if closed_ans else None
    )

    for arm in sorted(by_arm):
        ans = [r for r in by_arm[arm] if r["question_type"] != "unanswerable"]
        if not ans:
            continue
        outcomes = [1.0 if is_correct(r["question_type"], r["verdict"]) else 0.0 for r in ans]
        acc = sum(outcomes) / len(outcomes)
        lo, hi = bootstrap_ci(outcomes)
        mean_tok = sum(r["tokens_injected"] for r in ans) / len(ans)
        faith = [r["faithfulness"] for r in ans if r["faithfulness"] is not None]
        eff = (
            _fmt(efficiency_per_1k(acc, closed_acc, mean_tok))
            if closed_acc is not None else "n/a"
        )
        lines.append(
            f"| {arm} | {len(ans)} | {_fmt(acc)} | [{_fmt(lo)}, {_fmt(hi)}] "
            f"| {mean_tok:.0f} | {eff} "
            f"| {_fmt(sum(faith) / len(faith)) if faith else 'n/a'} |"
        )

    lines += ["", "## Unanswerable questions (abstention rate)", "",
              "| arm | n | abstained |", "|---|---|---|"]
    for arm in sorted(by_arm):
        una = [r for r in by_arm[arm] if r["question_type"] == "unanswerable"]
        if not una:
            continue
        rate = sum(1 for r in una if r["verdict"] == "abstained") / len(una)
        lines.append(f"| {arm} | {len(una)} | {_fmt(rate)} |")

    if {"closed_book", "production"} <= set(by_arm):
        pairs: dict[str, dict[str, bool]] = defaultdict(dict)
        for r in scored:
            if r["arm"] in {"closed_book", "production"} and r["question_type"] != "unanswerable":
                pairs[r["question_id"]][r["arm"]] = is_correct(r["question_type"], r["verdict"])
        complete = [p for p in pairs.values() if len(p) == 2]
        if complete:
            a = [p["production"] for p in complete]
            b = [p["closed_book"] for p in complete]
            b_n, c_n = paired_table(a, b)
            lines += ["", (f"## McNemar production vs closed_book: "
                           f"p = {_fmt(mcnemar_exact(b_n, c_n))} "
                           f"(prod-only correct: {b_n}, closed-only correct: {c_n}, "
                           f"paired n = {len(complete)})")]

    return "\n".join(lines) + "\n"
