"""Always-on KB preamble: synopsis spine + dynamic similarity bands.

Flow (per turn):
    embed(query) ─► match_findings ─► band_findings ─► assess_coverage
                                          │
        load synopsis ───────────────────┴─► render_preamble ─► <preamble/>

The pure core (band_findings / assess_coverage / render_preamble) has no IO and
is the single source of the coverage heuristic — the explore tool's `auto`
branch reuses it. `select_preamble` (IO) wraps it with synopsis load + embed,
both via the Store seam.
"""

from __future__ import annotations

from typing import Literal
from xml.sax.saxutils import escape

from delapan.core.agent.synopsis import load_synopsis
from delapan.core.clients.embeddings import embed_text
from delapan.core.config import TiersConfig, get_config
from delapan.store import Store

Coverage = Literal["rich", "sparse", "gap"]
Depth = Literal["shallow", "normal", "deep"]

_DEPTH_BANDS: dict[str, tuple[int, ...]] = {
    "shallow": (1,),
    "normal": (1, 2),
    "deep": (1, 2, 3),
}

# `escape` covers &, <, > but NOT " — pass this for values that land inside
# double-quoted attributes (topic=, category=) to keep the XML well-formed.
_ATTR_QUOTE = {'"': "&quot;"}


def band_findings(rows: list[dict], cfg: TiersConfig) -> dict[int, list[dict]]:
    """Bucket match_findings rows by their `similarity` into bands 1/2/3.
    Rows below band3_min are dropped. Each band stays similarity-desc."""
    bands: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    for r in sorted(rows, key=lambda r: r.get("similarity", 0.0), reverse=True):
        s = r.get("similarity", 0.0)
        if s >= cfg.band1_min:
            bands[1].append(r)
        elif s >= cfg.band2_min:
            bands[2].append(r)
        elif s >= cfg.band3_min:
            bands[3].append(r)
    return bands


def assess_coverage(bands: dict[int, list[dict]], cfg: TiersConfig) -> Coverage:
    """rich = enough band-1 hits; gap = nothing banded; else sparse."""
    if len(bands[1]) >= cfg.rich_hit_count:
        return "rich"
    if not (bands[1] or bands[2] or bands[3]):
        return "gap"
    return "sparse"


def render_preamble(
    synopsis: list[dict],
    bands: dict[int, list[dict]],
    *,
    depth: Depth = "normal",
    cfg: TiersConfig | None = None,
) -> str:
    """Assemble the <preamble>: synopsis (always) + depth-selected bands,
    bounded by cfg.preamble_char_budget (lowest-similarity findings drop first)."""
    cfg = cfg or get_config().tiers
    parts: list[str] = ["<preamble>"]

    if synopsis:
        parts.append("  <synopsis>")
        for e in synopsis:
            topic = escape(str(e.get("topic", "")), _ATTR_QUOTE)  # attribute value
            gloss = escape(str(e.get("gloss", "")))  # element text
            parts.append(f'    <entry topic="{topic}">{gloss}</entry>')
        parts.append("  </synopsis>")

    selected: list[dict] = []
    for b in _DEPTH_BANDS.get(depth, _DEPTH_BANDS["normal"]):
        selected.extend(bands.get(b, []))
    selected.sort(key=lambda r: r.get("similarity", 0.0), reverse=True)

    # Track the projected length of the final "\n".join(...). Every part costs
    # its own length plus one join newline for every part after the first. We
    # seed `projected` with the parts already committed (synopsis + the trailing
    # </preamble> we know we'll add) so the budget is a hard ceiling, not a soft
    # one. The <findings>/</findings> wrapper is reserved up front: it's only
    # paid for once at least one finding is admitted, and refunded if none are.
    _FINDINGS_OPEN = "  <findings>"
    _FINDINGS_CLOSE = "  </findings>"
    _CLOSE = "</preamble>"

    def _joined_len(committed: list[str]) -> int:
        # len of "\n".join(committed + [_CLOSE]): every part's chars (incl.
        # _CLOSE itself) + one newline between each of the (count) parts.
        final = [*committed, _CLOSE]
        return sum(len(p) for p in final) + (len(final) - 1)

    base = _joined_len(parts)  # parts + </preamble>, no findings yet
    # Reserve the wrapper cost: two extra parts => two extra lines, plus their
    # own text, plus two extra join newlines.
    wrapper_cost = len(_FINDINGS_OPEN) + len(_FINDINGS_CLOSE) + 2

    rendered: list[str] = []
    projected = base
    for f in selected:
        block = _render_finding(f)
        # Admitting this block adds: the wrapper (only on the first admit), the
        # block's text, and one join newline for the block itself.
        added = len(block) + 1 + (wrapper_cost if not rendered else 0)
        if projected + added > cfg.preamble_char_budget:
            break
        rendered.append(block)
        projected += added

    if rendered:
        parts.append(_FINDINGS_OPEN)
        parts.extend(rendered)
        parts.append(_FINDINGS_CLOSE)
    elif not synopsis:
        parts.append("  <empty/>")

    parts.append(_CLOSE)
    return "\n".join(parts)


async def select_preamble(
    query: str | None,
    *,
    store: Store,
    kb_id: str,
    depth: Depth = "normal",
) -> tuple[str, Coverage]:
    """IO entry: load synopsis + (optional) band query matches → (xml, coverage)."""
    cfg = get_config().tiers
    syn_row = load_synopsis(store, kb_id)
    synopsis = (syn_row or {}).get("content") or []

    bands: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    coverage: Coverage = "gap"
    if query:
        qvec = await embed_text(query)
        rows = await store.match_findings(
            kb_id,
            qvec,
            get_config().search.max_limit,
            # floor at the weakest band; band_findings drops anything below it
            cfg.band3_min,
        )
        bands = band_findings(rows or [], cfg)
        coverage = assess_coverage(bands, cfg)

    return render_preamble(synopsis, bands, depth=depth, cfg=cfg), coverage


def _render_finding(f: dict) -> str:
    title = escape(f.get("title") or "")  # element text
    category = escape(f.get("category") or "", _ATTR_QUOTE)  # attribute value
    content = escape((f.get("content") or "")[:1200])  # element text
    return (
        f'    <finding id="{f.get("id", "")}" category="{category}">\n'
        f"      <title>{title}</title>\n"
        f"      <content>{content}</content>\n"
        "    </finding>"
    )
