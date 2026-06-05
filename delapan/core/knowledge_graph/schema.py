"""KG intent schema — the user-approved TARGET ONTOLOGY for a KB's graph.

    findings ─► propose_schema (LLM, grounded) ─► KGSchema ─► user approves ─► persist
                                                      │
                              extract_graph(schema=…) reads it as SOFT guidance

Pure module — no Supabase, no FastAPI (mirrors the `exploration/` boundary). The
proposer mines the KB's findings for candidate entity/relation classes and the
competency questions the graph should answer; the user edits/approves the result;
`build_graph(use_schema=True)` then feeds it to the extractor. `regime` is "soft"
in v1: the schema steers extraction but never forces out-of-schema signal to be
dropped (the extractor keeps it as type "other"). Authoring never raises — a model
hiccup falls back to a deterministic proposal, like `projects/description.py`.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from delapan.core.clients.ai_gateway import structured_completion
from delapan.core.config import KnowledgeGraphConfig
from delapan.core.knowledge_graph.extractor import _catalogue

logger = logging.getLogger(__name__)

# Fallback ontology when the KB is empty and there's no emergent graph to mine —
# the extractor's own example classes, so a sparse KB still gets a sane default.
_DEFAULT_NODE_TYPES = ["company", "person", "technology", "concept", "product"]
_DEFAULT_RELATIONS = ["uses", "part_of", "competes_with", "founded_by", "acquired"]

# Value types a typed attribute may declare (ported from delapan's ontology model).
_ATTR_TYPES = ("text", "number", "date", "url", "list", "bool")


class Attribute(BaseModel):
    """A typed property a node class carries (e.g. company.founded_year : number).

    Ported from delapan: gives node classes a property schema, so extraction fills
    structured `properties` instead of arbitrary free-form keys."""

    name: str = Field(description="Short snake_case property name, e.g. 'founded_year'")
    type: str = Field(default="text", description=f"Value type — one of {', '.join(_ATTR_TYPES)}")
    required: bool = Field(default=False, description="Whether every instance should carry it")
    description: str = Field(default="", description="One-line meaning of the property")


class NodeType(BaseModel):
    """One entity class in the target ontology."""

    name: str = Field(description="Short lowercase class name, e.g. 'company'")
    description: str = Field(default="", description="One-line meaning of this class")
    examples: list[str] = Field(
        default_factory=list, description="2-3 real entity names drawn from the findings"
    )
    attributes: list[Attribute] = Field(
        default_factory=list,
        description="2-4 typed properties instances of this class should carry",
    )
    layer: str = Field(
        default="",
        description=(
            "Optional grouping/plane this class sits in (e.g. for a codebase: "
            "'orchestration', 'interface', 'data', 'infrastructure'; for a research KB: "
            "a domain grouping). Free-form; used to cluster the graph by tier."
        ),
    )


class RelationType(BaseModel):
    """One directed relation class in the target ontology."""

    name: str = Field(description="Short snake_case verb phrase, e.g. 'acquired'")
    description: str = Field(default="", description="One-line meaning of this relation")


class KGSchema(BaseModel):
    """A KB's approved KG intent — the artifact persisted to `kg_schemas`."""

    node_types: list[NodeType] = Field(default_factory=list)
    relation_types: list[RelationType] = Field(default_factory=list)
    # relation name → legal "source_type->target_type" pairs (typed by node_type names).
    relation_validity: dict[str, list[str]] = Field(default_factory=dict)
    competency_questions: list[str] = Field(default_factory=list)
    regime: Literal["soft"] = "soft"
    version: int = 1


class _SchemaProposal(BaseModel):
    """LLM-facing shape — same fields minus the persistence-only `regime`/`version`."""

    node_types: list[NodeType] = Field(default_factory=list)
    relation_types: list[RelationType] = Field(default_factory=list)
    relation_validity: dict[str, list[str]] = Field(default_factory=dict)
    competency_questions: list[str] = Field(default_factory=list)


_SYSTEM = (
    "You are designing the SCHEMA (intent) of a knowledge graph that will be built "
    "from a knowledge base's findings. Propose a TARGET ONTOLOGY a person can review "
    "and approve.\n\n"
    "Return:\n"
    "- node_types: 4-10 entity classes. Each has a short lowercase `name`, a one-line "
    "`description`, and 2-3 `examples` — REAL entity names taken from the findings. Also give "
    "each class:\n"
    "    • `attributes`: 2-4 typed properties instances of the class should carry, each with a "
    "snake_case `name`, a `type` (one of: text, number, date, url, list, bool), `required`, and a "
    "one-line `description`. Draw them from properties the findings actually report.\n"
    "    • `layer`: an optional short grouping/plane the class sits in (cluster related classes — "
    "e.g. for a codebase 'orchestration'/'interface'/'data'; for a research KB a domain grouping). "
    "Leave '' if no natural grouping.\n"
    "- relation_types: 4-12 directed relation classes. Each has a short snake_case "
    "verb-phrase `name` (e.g. founded_by, competes_with, part_of) and a `description`.\n"
    "- relation_validity: for each relation name, the legal `source_type->target_type` "
    "pairs, using your own node_type names (e.g. 'uses': ['company->technology']).\n"
    "- competency_questions: 3-6 questions the finished graph should be able to answer "
    "— what someone working with this KB most likely wants to know.\n\n"
    "Ground every class AND attribute in what the findings ACTUALLY contain — no speculative types "
    "or properties. Prefer a small, coherent ontology over an exhaustive one."
)


def _emergent_hint(emergent: dict | None) -> str:
    """A short prompt addendum biasing reuse of an already-built graph's ontology."""
    if not emergent:
        return ""
    types = ", ".join(emergent.get("node_types") or []) or "(none)"
    rels = ", ".join(emergent.get("relations") or []) or "(none)"
    return (
        "\n\nAn earlier graph build used these classes — reuse them where they fit, "
        f"rather than inventing near-duplicates:\nnode types: {types}\nrelations: {rels}"
    )


def _fallback_schema(emergent: dict | None) -> KGSchema:
    """Deterministic proposal — reuse the emergent ontology if a graph exists, else
    the extractor's default classes. Never raises; gives the user something to edit."""
    node_names = (emergent or {}).get("node_types") or _DEFAULT_NODE_TYPES
    rel_names = (emergent or {}).get("relations") or _DEFAULT_RELATIONS
    return KGSchema(
        node_types=[NodeType(name=n) for n in node_names],
        relation_types=[RelationType(name=r) for r in rel_names],
        relation_validity={},
        competency_questions=[
            "What are the key entities and how are they connected?",
            "Which entities are most central to this knowledge base?",
        ],
    )


async def propose_schema(
    findings: list[dict], cfg: KnowledgeGraphConfig, *, emergent: dict | None = None
) -> KGSchema:
    """Draft a target ontology from the KB's findings. Falls back to a deterministic
    proposal on empty input or model failure — never raises."""
    if not findings:
        return _fallback_schema(emergent)
    user = _catalogue(findings, cfg.max_finding_chars) + _emergent_hint(emergent)
    try:
        proposal = await structured_completion(
            model=cfg.extraction_model,
            response_format=_SchemaProposal,
            system=_SYSTEM,
            user=user,
            temperature=cfg.temperature,
            fallback_model=cfg.extraction_fallback_model,
            reasoning_effort=cfg.reasoning_effort,
            use_json_schema=False,  # free-form relation_validity dict
        )
    except Exception as exc:  # noqa: BLE001 — proposal is best-effort
        logger.warning("KG schema proposal failed: %s", exc)
        return _fallback_schema(emergent)
    if not proposal.node_types:
        return _fallback_schema(emergent)
    return KGSchema(**proposal.model_dump())


def validate_schema(schema: KGSchema) -> list[str]:
    """Structural checks. Returns a list of human-readable errors ([] = valid).

    Catches the mistakes a hand-edited schema makes: no node types, or a
    relation-validity entry that references a relation or node type that wasn't
    declared (which would silently never match at extraction time)."""
    errors: list[str] = []
    node_names = {nt.name for nt in schema.node_types}
    rel_names = {rt.name for rt in schema.relation_types}

    if not node_names:
        errors.append("schema has no node_types")

    # Typed attributes: each must declare a known value type, and names must be unique
    # within a class (a dupe would silently overwrite at extraction time).
    for nt in schema.node_types:
        seen: set[str] = set()
        for attr in nt.attributes:
            if attr.type not in _ATTR_TYPES:
                errors.append(
                    f"node type '{nt.name}' attribute '{attr.name}' has unknown type "
                    f"'{attr.type}' (expected one of {', '.join(_ATTR_TYPES)})"
                )
            if attr.name in seen:
                errors.append(f"node type '{nt.name}' declares attribute '{attr.name}' twice")
            seen.add(attr.name)

    for rel, pairs in schema.relation_validity.items():
        if rel not in rel_names:
            errors.append(f"relation_validity references undeclared relation '{rel}'")
        for pair in pairs:
            if "->" not in pair:
                errors.append(f"relation_validity['{rel}'] entry '{pair}' is not 'a->b'")
                continue
            src, _, tgt = pair.partition("->")
            for side in (src.strip(), tgt.strip()):
                if side and side not in node_names:
                    errors.append(
                        f"relation_validity['{rel}'] references undeclared node type '{side}'"
                    )
    return errors
