"""CLI: python -m evals {run,report,sweep}.

    run    ──► run_eval ──► evals/runs/<ts>/ (manifest, records, report.md)
    report ──► re-render report.md from an artifact (byte-reproducible)
    sweep  ──► run per (budget × depth) grid point via DLP_ env + cache_clear
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from delapan.core.config import get_config
from evals.artifact import load_run
from evals.report import render_report
from evals.runner import run_eval
from evals.scoring.correctness import is_correct
from evals.scoring.efficiency import efficiency_per_1k

DEFAULT_ARMS = ["closed_book", "production", "oracle"]


def _model_default() -> str:
    return get_config().canvas.answer_model


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evals")
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("--set", required=True, type=Path)
    run_p.add_argument("--project", required=True)
    run_p.add_argument("--kb", required=True)
    run_p.add_argument("--arms", default=",".join(DEFAULT_ARMS))
    run_p.add_argument("--answer-model", default=None)
    run_p.add_argument("--judge-model", default=None)
    run_p.add_argument("--depth", default="normal", choices=["shallow", "normal", "deep"])
    run_p.add_argument("--out", type=Path, default=Path("evals/runs"))
    run_p.add_argument("--hhem", action="store_true")

    rep_p = sub.add_parser("report")
    rep_p.add_argument("run_dir", type=Path)

    sw_p = sub.add_parser("sweep")
    sw_p.add_argument("--set", required=True, type=Path)
    sw_p.add_argument("--project", required=True)
    sw_p.add_argument("--kb", required=True)
    sw_p.add_argument("--budgets", default="3000,7000,12000")
    sw_p.add_argument("--depths", default="shallow,deep")
    sw_p.add_argument("--answer-model", default=None)
    sw_p.add_argument("--judge-model", default=None)
    sw_p.add_argument("--out", type=Path, default=Path("evals/runs"))

    args = p.parse_args(argv)

    if args.cmd == "report":
        manifest, records = load_run(args.run_dir)
        print(render_report(manifest, records))
        return 0

    answer_model = args.answer_model or _model_default()
    judge_model = args.judge_model or _model_default()

    if args.cmd == "run":
        run_dir = asyncio.run(
            run_eval(
                set_path=args.set, project=args.project, kb=args.kb,
                arms=args.arms.split(","), answer_model=answer_model,
                judge_model=judge_model, out_dir=args.out, depth=args.depth,
                use_hhem=args.hhem,
            )
        )
        print(run_dir)
        return 0

    # sweep: production arm only — the other arms don't read tiers config.
    prev = os.environ.get("DLP_TIERS__PREAMBLE_CHAR_BUDGET")
    points: list[tuple[str, str, Path]] = []
    try:
        for budget in args.budgets.split(","):
            for depth in args.depths.split(","):
                os.environ["DLP_TIERS__PREAMBLE_CHAR_BUDGET"] = budget
                get_config.cache_clear()  # lru_cache — without this every point measures point 1
                run_dir = asyncio.run(
                    run_eval(
                        set_path=args.set, project=args.project, kb=args.kb,
                        arms=["closed_book", "production"], answer_model=answer_model,
                        judge_model=judge_model,
                        out_dir=args.out / f"sweep-b{budget}-{depth}", depth=depth,
                    )
                )
                print(f"budget={budget} depth={depth} -> {run_dir}")
                points.append((budget, depth, run_dir))
    finally:
        if prev is not None:
            os.environ["DLP_TIERS__PREAMBLE_CHAR_BUDGET"] = prev
        else:
            os.environ.pop("DLP_TIERS__PREAMBLE_CHAR_BUDGET", None)
        get_config.cache_clear()

    rows = ["| budget | depth | arm | n | accuracy | mean_tokens | eff_per_1k |",
            "|---|---|---|---|---|---|---|"]
    for budget, depth, run_dir in points:
        _, records = load_run(run_dir)
        by_arm: dict[str, list[dict]] = {}
        for r in records:
            if not r["unscored"] and r["question_type"] != "unanswerable":
                by_arm.setdefault(r["arm"], []).append(r)
        closed = by_arm.get("closed_book", [])
        closed_acc = (
            sum(is_correct(r["question_type"], r["verdict"]) for r in closed) / len(closed)
            if closed else None
        )
        for arm in sorted(by_arm):
            ans = by_arm[arm]
            acc = sum(is_correct(r["question_type"], r["verdict"]) for r in ans) / len(ans)
            mean_tok = sum(r["tokens_injected"] for r in ans) / len(ans)
            eff = f"{efficiency_per_1k(acc, closed_acc, mean_tok):.3f}" if closed_acc is not None else "n/a"
            rows.append(f"| {budget} | {depth} | {arm} | {len(ans)} | {acc:.3f} | {mean_tok:.0f} | {eff} |")
    table = "\n".join(rows) + "\n"
    print(table)
    (args.out / "sweep-summary.md").write_text(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
