"""KG extractor — one structured LLM pass over a KB's findings.

    findings (title + category + content) ──► AI Gateway ──► KGExtraction

Mirrors `exploration/extractor.py`: routes through the gateway, instructs the
schema in the prompt (`use_json_schema=False`, for the free-form `properties`
dicts), and never raises — a failed pass yields an empty graph rather than
poisoning the build.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from delapan.core.clients.ai_gateway import structured_completion
from delapan.core.config import KnowledgeGraphConfig
from delapan.core.knowledge_graph.models import KGExtraction

if TYPE_CHECKING:
    from delapan.core.knowledge_graph.schema import KGSchema

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are building a knowledge graph from a knowledge base's findings.\n"
    "Extract the salient ENTITIES (nodes) and the RELATIONSHIPS (directed edges) "
    "between them.\n\n"
    "RULES:\n"
    "1. Use a single canonical `label` per entity — merge obvious aliases "
    "(e.g. 'OpenAI' and 'OpenAI Inc.' are one node labelled 'OpenAI').\n"
    "2. `type` is a short lowercase ontology class (company, person, technology, "
    "concept, product, place, event, metric, ...). Reuse types across entities.\n"
    "3. Every edge's `source` and `target` MUST exactly match a node `label` you "
    "also return. `relation` is a short verb phrase (acquired, founded_by, "
    "competes_with, part_of, uses).\n"
    "4. Extract only what the findings support — no outside knowledge, no "
    "placeholders. Prefer fewer, well-supported nodes over speculative ones.\n"
    "5. Put supporting attributes in `properties` (free-form key/value).\n"
    "6. GROUND every node and every edge: set `grounded_in` to the finding id(s) that "
    "evidence it. Each finding is prefixed with its id as `(finding_id: <id>)`. Use those "
    "exact ids; give every node and edge at least one."
)


def _schema_block(schema: KGSchema) -> str:
    """Render the approved ontology as a SOFT-guidance addendum to `_SYSTEM`.

    Lists the preferred node/relation types (with descriptions), the relation-
    validity constraints, and the competency questions the graph must answer.
    Soft mode: out-of-schema signal is KEPT (typed 'other'), never dropped — so a
    too-narrow schema degrades gracefully instead of losing findings."""

    def _node_line(nt) -> str:  # noqa: ANN001 — local formatter over schema.NodeType
        head = f"{nt.name} — {nt.description}" if nt.description else nt.name
        if nt.layer:
            head += f" [layer: {nt.layer}]"
        if nt.attributes:
            attrs = ", ".join(
                f"{a.name}:{a.type}{'*' if a.required else ''}" for a in nt.attributes
            )
            head += f" {{attributes: {attrs}}}"
        return head

    nodes = "; ".join(_node_line(nt) for nt in schema.node_types)
    rels = "; ".join(
        f"{rt.name} — {rt.description}" if rt.description else rt.name
        for rt in schema.relation_types
    )
    validity = "; ".join(
        f"{rel} ({', '.join(pairs)})" for rel, pairs in schema.relation_validity.items() if pairs
    )
    cqs = " ".join(f"- {q}" for q in schema.competency_questions)
    has_attrs = any(nt.attributes for nt in schema.node_types)
    has_layers = any(nt.layer for nt in schema.node_types)
    lines = [
        "\n\nTARGET ONTOLOGY (prefer these classes; do not force-fit):",
        f"  node types: {nodes or '(none)'}",
        f"  relation types: {rels or '(none)'}",
    ]
    if validity:
        lines.append(f"  legal relations: {validity}")
    if cqs:
        lines.append(f"This graph must be able to answer: {cqs}")
    if has_attrs:
        lines.append(
            "For each node, fill `properties` with the declared attributes for its type (key = "
            "attribute name); use the attribute's value type, and include every attribute marked "
            "* (required) when the findings supply it — omit a key rather than inventing a value."
        )
    if has_layers:
        lines.append("Also set `properties.layer` to the node type's declared layer.")
    lines.append(
        "If a salient entity or relation does not fit the ontology, KEEP it: use the "
        'closest class, or `type`/`relation` "other", and add a `properties.note`. '
        "Never drop supported signal to satisfy the schema."
    )
    return "\n".join(lines)


def _catalogue(findings: list[dict], max_finding_chars: int) -> str:
    lines: list[str] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        title = f.get("title", "")
        category = f.get("category", "")
        content = str(f.get("content", ""))[:max_finding_chars]
        fid = f.get("id")
        marker = f" (finding_id: {fid})" if fid else ""
        lines.append(f"### {title} [{category}]{marker}\n{content}")
    return "\n\n".join(lines)


# Entity names too generic to safely text-match against finding bodies (a bare
# "person" or "system" would match almost anything). Mirrors delapan's guard.
_GENERIC_NAMES = frozenset(
    {
        "entity",
        "relationship",
        "person",
        "system",
        "framework",
        "algorithm",
        "organization",
        "standard",
        "concept",
        "technology",
        "company",
        "product",
        "model",
        "method",
        "tool",
        "service",
        "data",
        "api",
        "other",
    }
)


def _is_specific(name: str) -> bool:
    return len(name.strip()) >= 3 and name.strip().lower() not in _GENERIC_NAMES


def backfill_grounding(extraction: KGExtraction, findings: list[dict]) -> int:
    """Deterministic repair: fill empty `grounded_in` by case-insensitive substring
    match of an entity's label (or an edge's endpoint labels) against finding text.

    Ported from delapan. The generic-name guard prevents a vague label from matching
    everything. Returns the count of nodes+edges still ungrounded after the pass."""
    texts = [
        (str(f.get("id")), f"{f.get('title', '')} {f.get('content', '')}".lower())
        for f in findings
        if isinstance(f, dict) and f.get("id")
    ]
    label_by_norm = {n.label.strip().lower(): n.label for n in extraction.nodes}

    def _match(name: str) -> list[str]:
        if not _is_specific(name):
            return []
        needle = name.strip().lower()
        return [fid for fid, text in texts if needle in text]

    unresolved = 0
    for node in extraction.nodes:
        if node.grounded_in:
            continue
        node.grounded_in = _match(node.label)
        if not node.grounded_in:
            unresolved += 1

    for edge in extraction.edges:
        if edge.grounded_in:
            continue
        matched: set[str] = set()
        for endpoint in (edge.source, edge.target):
            # use the canonical node label when the endpoint resolves to a node
            label = label_by_norm.get(endpoint.strip().lower(), endpoint)
            matched.update(_match(label))
        edge.grounded_in = sorted(matched)
        if not edge.grounded_in:
            unresolved += 1

    return unresolved


async def extract_graph(
    findings: list[dict], cfg: KnowledgeGraphConfig, schema: KGSchema | None = None
) -> KGExtraction:
    """Extract a graph from the KB's findings. Returns an empty graph on failure.

    When `schema` is given, its approved ontology is appended to the system prompt
    as SOFT guidance (prefer those types; keep out-of-schema signal as 'other').
    When `schema is None`, the prompt is byte-identical to the free-form default."""
    if not findings:
        return KGExtraction()
    system = _SYSTEM + (_schema_block(schema) if schema else "")
    user = _catalogue(findings, cfg.max_finding_chars)
    try:
        extraction = await structured_completion(
            model=cfg.extraction_model,
            response_format=KGExtraction,
            system=system,
            user=user,
            temperature=cfg.temperature,
            fallback_model=cfg.extraction_fallback_model,
            reasoning_effort=cfg.reasoning_effort,
            # Free-form `properties` dicts — strict json_schema would empty them.
            use_json_schema=False,
        )
    except Exception as exc:  # noqa: BLE001 — extraction is best-effort
        logger.warning("KG extraction failed: %s", exc)
        return KGExtraction()
    # Deterministic provenance repair: fill any grounded_in the model left empty.
    unresolved = backfill_grounding(extraction, findings)
    if unresolved:
        logger.info("KG extraction: %d node(s)/edge(s) left ungrounded after backfill", unresolved)
    return extraction
