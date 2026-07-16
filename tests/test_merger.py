from __future__ import annotations

from delapan.core.exploration.merger import FindingMerger, confidence_from_sources
from delapan.core.exploration.models import Finding


def _finding(title: str, category: str = "fact", url: str = "http://a") -> Finding:
    # merge_findings takes Finding objects, not raw dicts (the brief's `_raw`
    # dict helper was a guess); Finding requires exploration_id/project_id.
    return Finding(
        exploration_id="exp1",
        project_id="proj1",
        category=category,
        title=title,
        content={"k": "v"},
        provenance=[{"url": url}],
    )


def test_confidence_rises_with_source_count_and_saturates():
    assert confidence_from_sources(1) < confidence_from_sources(2) < confidence_from_sources(5)
    assert confidence_from_sources(50) <= 1.0


def test_similar_titles_in_same_category_merge_and_union_provenance():
    # FindingMerger.__init__ has no defaults for fuzzy_threshold/min_confidence
    # (the brief's `FindingMerger()` would raise TypeError) — use the same
    # values ExplorationConfig defaults to (fuzzy_match_threshold=0.80,
    # min_confidence_threshold=0.2).
    m = FindingMerger(fuzzy_threshold=0.8, min_confidence=0.2)
    out = m.merge_findings([
        _finding("Tavily pricing tiers", url="http://a"),
        _finding("Tavily pricing tier", url="http://b"),  # fuzzy-equal title
    ])
    assert len(out) == 1
    assert {p["url"] for p in out[0].provenance} == {"http://a", "http://b"}
    assert out[0].confidence > confidence_from_sources(1) - 0.001


def test_same_title_in_different_categories_does_not_merge():
    m = FindingMerger(fuzzy_threshold=0.8, min_confidence=0.2)
    out = m.merge_findings([_finding("Pricing", category="fact"),
                            _finding("Pricing", category="decision")])
    assert len(out) == 2


def test_unrelated_titles_do_not_merge():
    m = FindingMerger(fuzzy_threshold=0.8, min_confidence=0.2)
    out = m.merge_findings([_finding("Tavily pricing"), _finding("Postgres vector index")])
    assert len(out) == 2


def test_findings_below_min_confidence_are_dropped():
    m = FindingMerger(fuzzy_threshold=0.8, min_confidence=0.99)
    assert m.merge_findings([_finding("Only one source")]) == []
