"""OKF concept-doc synthesis: an entity's grounded findings + relations → a
readable prose document, via one AI-Gateway pass.

    node + grounded findings + 1-hop relations ─► text_completion ─► {description, body}

`grounded_hash` is the staleness key shared verbatim with the frontend
(`src/okf/conceptDoc.ts`): FNV-1a 32-bit over the sorted, comma-joined grounded
finding ids. The reader recomputes it to tell whether a cached doc still matches
the node's evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from delapan.core.clients.ai_gateway import text_completion
from delapan.core.config import get_config
from delapan.core.knowledge_graph.service import read_graph
from delapan.store import Store

_SYSTEM = """\
You write encyclopedia-style concept documents for a knowledge base. Given an \
entity, its known facts (findings), and its relationships, write a clear, neutral \
article. Ground every statement ONLY in the supplied findings — invent nothing and \
cite no source that was not given. Output exactly two parts separated by a line \
containing only '---':
1) a single-sentence description (plain text, no markdown),
2) the article body in markdown (use ## headings and short paragraphs; no \
frontmatter, no title)."""


def grounded_hash(ids: list[str]) -> str:
    """FNV-1a 32-bit hex over the sorted, comma-joined ids. MUST match the TS
    `groundedHash` in delapan-fe."""
    key = ",".join(sorted(ids)).encode("utf-8")
    h = 0x811C9DC5
    for b in key:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def _finding_brief(f: dict) -> str:
    content = f.get("content", "")
    if isinstance(content, dict):
        content = "; ".join(str(v) for v in content.values() if v)
    return f"- {f.get('title', '')}: {content}"


async def synthesize_concept_doc(store: Store, kb_id: str, node_id: str) -> dict:
    """One gateway pass over a node's grounded findings + 1-hop relations.

    Returns {description, body_markdown, model, built_at, grounded_hash}.
    Raises LookupError(node_id) when the node is absent. The caller gates on the
    LLM key and maps exceptions to HTTP status."""
    node = store.get_kg_node(kb_id, node_id)
    if node is None:
        raise LookupError(node_id)

    grounded = node.get("grounded_in") or []
    findings: list[dict] = []
    for fid in grounded:
        try:
            findings.append(store.get_finding(kb_id, fid))
        except Exception:  # noqa: BLE001 — a missing finding simply isn't briefed
            continue

    graph = read_graph(store, kb_id, focus=node_id, depth=1)
    label_by_id = {n.get("id"): n.get("label") for n in graph.get("nodes") or []}
    rels: list[str] = []
    for e in graph.get("edges") or []:
        src, tgt, rel = e.get("source_node_id"), e.get("target_node_id"), e.get("relation")
        if src == node_id and tgt in label_by_id:
            rels.append(f"{node.get('label')} {rel} {label_by_id[tgt]}")
        elif tgt == node_id and src in label_by_id:
            rels.append(f"{label_by_id[src]} {rel} {node.get('label')}")

    user = (
        f"Entity: {node.get('label')} (type: {node.get('type')})\n\n"
        "Findings:\n"
        + ("\n".join(_finding_brief(f) for f in findings) or "(none)")
        + "\n\nRelationships:\n"
        + ("\n".join(f"- {r}" for r in rels) or "(none)")
    )

    cfg = get_config().okf
    raw = await text_completion(
        model=cfg.model,
        system=_SYSTEM,
        user=user,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
    )
    description, sep, body = raw.partition("\n---\n")
    if not sep:  # model skipped the separator — treat the whole reply as body
        description, body = "", raw
    return {
        "description": description.strip(),
        "body_markdown": body.strip(),
        "model": cfg.model,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "grounded_hash": grounded_hash(grounded),
    }
