from __future__ import annotations


def test_render_content_single_string_value():
    from delapan.core.exploration.render import render_content

    assert render_content({"summary": "hello"}) == "hello"


def test_render_content_multi_key_markdown():
    from delapan.core.exploration.render import render_content

    out = render_content({"plan": "free", "limit": 1000})
    assert "**Plan**: free" in out
    assert "**Limit**: 1000" in out


def test_render_content_passthrough_and_empty():
    from delapan.core.exploration.render import render_content

    assert render_content("already text") == "already text"
    assert render_content({}) == ""


def test_normalize_provenance_stamps_accessed_at():
    from delapan.core.exploration.render import normalize_provenance

    out = normalize_provenance([{"url": "http://x"}])
    assert out[0]["url"] == "http://x"
    assert "accessed_at" in out[0]
    assert normalize_provenance([]) == []
