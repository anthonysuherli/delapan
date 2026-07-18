from __future__ import annotations

from pathlib import Path

from delapan.tracking.parse import (
    load_tracking_dir,
    parse_backlog_file,
    parse_initiative_file,
)

FIX = Path(__file__).parent / "fixtures" / "tracking"


def test_parse_initiative_frontmatter_and_body():
    row = parse_initiative_file(FIX / "initiatives" / "alpha.md")
    assert row.slug == "alpha"
    assert row.title == "Alpha"
    assert row.status == "active"
    assert row.repo == "backend"
    assert row.blocked_by == ["beta"]
    assert row.spec == "docs/superpowers/specs/example.md"
    assert row.plan is None
    assert row.branch == "feat/alpha"
    assert row.updated == "2026-07-17"
    assert "**Next step**" in row.body_md
    assert "Ship parser" in row.body_md


def test_parse_backlog_tags_and_positions():
    items = parse_backlog_file(FIX / "backlog.md")
    assert len(items) == 2
    assert items[0].position == 1
    assert items[0].text == "Do the first thing"
    assert items[0].repo == "backend"
    assert items[0].initiative_slug == "alpha"
    assert items[1].position == 2
    assert items[1].text == "Do the second thing"
    assert items[1].repo == "both"
    assert items[1].initiative_slug is None


def test_load_tracking_dir_collects_all():
    inits, backlog = load_tracking_dir(FIX)
    slugs = sorted(i.slug for i in inits)
    assert slugs == ["alpha", "beta"]
    assert len(backlog) == 2
