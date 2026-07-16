from __future__ import annotations

from delapan.core.agent.preamble import assess_coverage, band_findings, render_preamble
from delapan.core.config import TiersConfig


def _rows(*sims):
    return [{"id": f"f{i}", "title": f"T{i}", "content": "body", "category": "fact",
             "similarity": s} for i, s in enumerate(sims)]


def test_bands_split_on_configured_thresholds():
    cfg = TiersConfig()
    bands = band_findings(_rows(0.9, cfg.band1_min, 0.5, cfg.band2_min, 0.3, cfg.band3_min), cfg)
    assert len(bands[1]) == 2      # >= band1_min
    assert len(bands[2]) == 2      # >= band2_min
    assert len(bands[3]) == 2      # >= band3_min


def test_rows_below_band3_are_dropped():
    cfg = TiersConfig()
    bands = band_findings(_rows(cfg.band3_min - 0.01, 0.0), cfg)
    assert bands == {1: [], 2: [], 3: []}


def test_bands_stay_similarity_desc():
    cfg = TiersConfig()
    bands = band_findings(_rows(0.6, 0.95, 0.7), cfg)
    sims = [r["similarity"] for r in bands[1]]
    assert sims == sorted(sims, reverse=True)


def test_coverage_rich_needs_enough_band1_hits():
    cfg = TiersConfig()
    n = cfg.rich_hit_count
    assert assess_coverage(band_findings(_rows(*([0.9] * n)), cfg), cfg) == "rich"
    assert assess_coverage(band_findings(_rows(*([0.9] * (n - 1))), cfg), cfg) == "sparse"


def test_coverage_gap_when_nothing_bands():
    cfg = TiersConfig()
    assert assess_coverage(band_findings(_rows(0.01), cfg), cfg) == "gap"
    assert assess_coverage(band_findings([], cfg), cfg) == "gap"


def test_preamble_respects_char_budget_dropping_weakest_first():
    # render_preamble's real signature is (synopsis, bands, *, depth, cfg) —
    # the brief guessed (bands, synopsis=None, cfg=, depth=), which is reversed
    # positionally and would raise "multiple values for argument 'synopsis'".
    cfg = TiersConfig(preamble_char_budget=900)
    rows = [
        {"id": "keep", "title": "Strong", "content": "x" * 400, "category": "fact",
         "similarity": 0.95},
        {"id": "drop", "title": "Weak", "content": "y" * 400, "category": "fact",
         "similarity": 0.56},
    ]
    out = render_preamble(None, band_findings(rows, cfg), depth="normal", cfg=cfg)
    assert len(out) <= cfg.preamble_char_budget
    assert "Strong" in out and "Weak" not in out


def test_preamble_escapes_xml_in_titles():
    cfg = TiersConfig()
    rows = [{"id": "a", "title": 'A & B <script>', "content": "body", "category": "fact",
             "similarity": 0.9}]
    out = render_preamble(None, band_findings(rows, cfg), depth="normal", cfg=cfg)
    assert "<script>" not in out
    assert "&amp;" in out or "&#" in out
