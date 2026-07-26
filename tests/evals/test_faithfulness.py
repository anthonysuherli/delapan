"""Empty-context exemption guard; predictor injection; missing-dep message."""
from __future__ import annotations

import pytest

from evals.scoring.faithfulness import score_faithfulness

FAKE = lambda premise, hypothesis: 0.42  # — injected predictor


def test_none_context_exempt():
    assert score_faithfulness(None, "answer", predictor=FAKE) is None


def test_empty_preamble_exempt():
    assert score_faithfulness("<preamble>\n  <empty/>\n</preamble>", "a", predictor=FAKE) is None


def test_synopsis_only_preamble_exempt():
    xml = "<preamble>\n  <synopsis>\n    <entry topic=\"t\">g</entry>\n  </synopsis>\n</preamble>"
    assert score_faithfulness(xml, "a", predictor=FAKE) is None


def test_findings_present_scores():
    xml = (
        "<preamble>\n  <findings>\n"
        '    <finding id="f1" category="fact">\n      <title>T</title>\n'
        "      <content>C</content>\n    </finding>\n  </findings>\n</preamble>"
    )
    assert score_faithfulness(xml, "a", predictor=FAKE) == 0.42


def test_missing_hhem_dep_is_actionable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_transformers(name, *a, **kw):
        if name.startswith("transformers"):
            raise ImportError("nope")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_transformers)
    from evals.scoring.faithfulness import load_hhem

    with pytest.raises(ImportError, match="evals-hhem"):
        load_hhem()
