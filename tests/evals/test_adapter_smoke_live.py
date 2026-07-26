"""Opt-in: real HF download (max_docs-limited) + 3-question run with real keys.

Run: RUN_EVALS_ADAPTER_SMOKE=1 pytest tests/evals/test_adapter_smoke_live.py -v
Needs: evals-adapters extra installed, AI gateway key. Skipped otherwise."""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_EVALS_ADAPTER_SMOKE"),
    reason="adapter live smoke is opt-in (RUN_EVALS_ADAPTER_SMOKE=1)",
)


async def test_watsonx_smoke(store, tmp_path, monkeypatch):
    import evals.adapters.watsonx_docsqa as wx
    from evals.runner import run_eval

    monkeypatch.setattr(wx, "SET_PATH", tmp_path / "set.yaml")
    monkeypatch.setattr(wx, "LOCK_PATH", tmp_path / "lock.json")
    lock = await wx.build(project="wx-smoke", kb="v1", max_docs=25)
    assert lock["counts"]["chunks"] > 0
    if lock["counts"]["questions"] == 0:
        pytest.skip("no question survived the 25-doc corpus slice")

    trimmed = tmp_path / "set3.yaml"
    text = (tmp_path / "set.yaml").read_text()
    # run only the first question: rewrite the set with one entry
    import yaml as _yaml

    spec = _yaml.safe_load(text)
    spec["questions"] = spec["questions"][:1]
    trimmed.write_text(_yaml.safe_dump(spec, sort_keys=False))

    from delapan.core.config import get_config

    model = get_config().canvas.answer_model
    run_dir = await run_eval(
        set_path=trimmed, project="wx-smoke", kb="v1",
        arms=["closed_book", "production", "oracle"],
        answer_model=model, judge_model=model, out_dir=tmp_path / "runs",
        oracle_budget=24_000,
    )
    records = json.loads((run_dir / "records.json").read_text())
    assert len(records) == 3 and all(not r["unscored"] for r in records)
