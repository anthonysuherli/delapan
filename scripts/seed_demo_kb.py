"""Seed a demo KB ("demo"/"main") for the KG control panel — no external APIs.

    python scripts/seed_demo_kb.py
        │
        ├─► findings (28, fake-embedded)      ──► findings + vec_findings
        ├─► intent schema (set_kg_intent v1)  ──► kg_schemas
        ├─► kg nodes (40) + edges (~70)       ──► kg_nodes/vec_kg_nodes/kg_edges
        └─► synopsis (6 entries)              ──► kb_synopsis

Topic: agentic AI memory systems. Embeddings are deterministic fakes (per-item
seeded RNG, 1536-dim, L2-normalized) so seeding needs no OPENAI_API_KEY and
re-runs are stable. Idempotent: findings are guarded by their stable ids, the
intent schema is only set once, nodes/edges dedupe in the store, the synopsis
upserts. Forces the local SQLite backend (honors DELAPAN_DB_PATH).
"""

from __future__ import annotations

import asyncio
import math
import os
import random

os.environ.setdefault("DELAPAN_BACKEND", "local")

DIM = 1536

# --- findings: (id-suffix, title, summary, category, confidence, url) --------

FINDINGS: list[tuple[str, str, str, str, float, str]] = [
    (
        "001",
        "Episodic vs semantic memory in LLM agents",
        "Agent memory work borrows the cognitive split between episodic memory (specific "
        "interaction traces) and semantic memory (distilled facts). Most production systems "
        "store both but retrieve them through different indexes.",
        "concept",
        0.9,
        "https://arxiv.org/abs/2404.13501",
    ),
    (
        "002",
        "MemGPT introduces OS-inspired memory paging",
        "MemGPT treats the context window as main memory and external storage as disk, paging "
        "context in and out via self-issued function calls. The agent manages its own memory "
        "hierarchy instead of relying on a fixed retriever.",
        "architecture",
        0.95,
        "https://arxiv.org/abs/2310.08560",
    ),
    (
        "003",
        "Vector stores as long-term memory backends",
        "Embedding-indexed stores (pgvector, sqlite-vec, FAISS) remain the default long-term "
        "memory substrate: cheap writes, semantic reads, no schema. Weak at temporal and "
        "relational queries compared to graph-backed memory.",
        "technique",
        0.85,
        "https://www.pinecone.io/learn/vector-database/",
    ),
    (
        "004",
        "Reflexion: verbal reinforcement via episodic memory",
        "Reflexion agents store self-generated critiques of failed attempts in an episodic "
        "buffer and condition retries on them — memory as a learning signal without weight "
        "updates.",
        "technique",
        0.9,
        "https://arxiv.org/abs/2303.11366",
    ),
    (
        "005",
        "Generative Agents' memory stream and retrieval scoring",
        "The Stanford Generative Agents paper logs every observation to an append-only memory "
        "stream and retrieves by a weighted sum of recency, importance, and relevance — the "
        "scoring recipe most agent frameworks copied.",
        "architecture",
        0.95,
        "https://arxiv.org/abs/2304.03442",
    ),
    (
        "006",
        "Recency, importance, relevance scoring for retrieval",
        "Triple-factor retrieval scoring (exponential recency decay x LLM-rated importance x "
        "cosine relevance) outperforms pure similarity for agent memory because it favors "
        "fresh, salient context.",
        "technique",
        0.8,
        "https://arxiv.org/abs/2304.03442",
    ),
    (
        "007",
        "Letta productionizes MemGPT memory blocks",
        "Letta (the MemGPT successor) ships named, size-bounded memory blocks (persona, human, "
        "task) that the agent edits with tools; blocks persist across sessions and are visible "
        "in every prompt.",
        "tool",
        0.85,
        "https://docs.letta.com/concepts/memgpt",
    ),
    (
        "008",
        "LangMem SDK for agent memory",
        "LangChain's LangMem provides hot-path and background memory managers that extract, "
        "consolidate, and retrieve memories from conversations, exposing semantic, episodic, "
        "and procedural memory types.",
        "tool",
        0.8,
        "https://blog.langchain.dev/langmem-sdk-launch/",
    ),
    (
        "009",
        "Zep: temporal knowledge graph memory",
        "Zep stores conversational memory as a temporal knowledge graph (Graphiti): facts carry "
        "valid-from/valid-to intervals, so the agent can answer what was true when — a query "
        "class flat vector stores cannot serve.",
        "tool",
        0.85,
        "https://arxiv.org/abs/2501.13956",
    ),
    (
        "010",
        "Mem0 layered memory architecture",
        "Mem0 layers working, episodic, and factual memory with an extraction pipeline that "
        "decides what to remember after each exchange, claiming large token savings versus "
        "full-history prompting on LOCOMO.",
        "tool",
        0.8,
        "https://arxiv.org/abs/2504.19413",
    ),
    (
        "011",
        "Context window limits drive external memory",
        "Even million-token contexts degrade (lost-in-the-middle, cost, latency), so external "
        "memory is an architectural necessity, not a stopgap: agents must select context, not "
        "accumulate it.",
        "challenge",
        0.9,
        "https://arxiv.org/abs/2307.03172",
    ),
    (
        "012",
        "Catastrophic forgetting in long-horizon agents",
        "Naive summarize-and-truncate loops lose task-critical details over long horizons; "
        "agents need explicit write policies and consolidation instead of lossy rolling "
        "summaries.",
        "challenge",
        0.85,
        "https://arxiv.org/abs/2402.18540",
    ),
    (
        "013",
        "Memory consolidation via periodic summarization",
        "Consolidation jobs compress raw episodic traces into semantic summaries on a schedule "
        "(end of session, token threshold), mirroring sleep-phase consolidation in biological "
        "memory.",
        "technique",
        0.8,
        "https://arxiv.org/abs/2308.15022",
    ),
    (
        "014",
        "HippoRAG applies hippocampal indexing theory to RAG",
        "HippoRAG builds an open knowledge graph over the corpus and runs personalized PageRank "
        "from query entities — modeling the hippocampal index over neocortical traces — beating "
        "iterative RAG on multi-hop QA.",
        "architecture",
        0.9,
        "https://arxiv.org/abs/2405.14831",
    ),
    (
        "015",
        "Knowledge-graph memory vs flat vector memory",
        "Graph-backed memory wins on multi-hop, temporal, and aggregation queries; vector "
        "memory wins on write simplicity and fuzzy recall. Hybrid systems (graph + embeddings "
        "per node) are converging as the default.",
        "concept",
        0.85,
        "https://neo4j.com/blog/genai/knowledge-graph-vs-vector-rag/",
    ),
    (
        "016",
        "LOCOMO benchmarks long-term conversational memory",
        "LOCOMO evaluates very long conversations (300+ turns) with QA over single-hop, "
        "multi-hop, temporal, and adversarial questions — the de facto benchmark memory vendors "
        "report against.",
        "evaluation",
        0.9,
        "https://arxiv.org/abs/2402.17753",
    ),
    (
        "017",
        "GoodAI LTM benchmark stresses continual recall",
        "GoodAI's LTM benchmark interleaves distractor tasks between fact insertion and recall, "
        "testing integration and update of memories rather than single-prompt retrieval.",
        "evaluation",
        0.75,
        "https://github.com/GoodAI/goodai-ltm-benchmark",
    ),
    (
        "018",
        "Working memory as scratchpad in ReAct loops",
        "The ReAct pattern's thought-action-observation trace is a working memory: it holds "
        "intermediate state for the current task and is discarded after, distinct from "
        "persistent stores.",
        "concept",
        0.8,
        "https://arxiv.org/abs/2210.03629",
    ),
    (
        "019",
        "Procedural memory stores learned skills",
        "Procedural memory persists how-to knowledge — tool-use routines, code snippets, "
        "prompts that worked — so agents improve at recurring tasks; Voyager's skill library is "
        "the canonical example.",
        "concept",
        0.8,
        "https://arxiv.org/abs/2305.16291",
    ),
    (
        "020",
        "Memory write policies: deciding what to persist",
        "Write-time gating (LLM judges salience before persisting) beats store-everything: it "
        "cuts retrieval noise and storage cost, at the risk of dropping facts that only later "
        "become relevant.",
        "technique",
        0.75,
        "https://arxiv.org/abs/2502.12110",
    ),
    (
        "021",
        "Forgetting curves and memory decay schedules",
        "Decay schedules (exponential down-weighting, TTL eviction, Ebbinghaus-style "
        "reinforcement on access) keep memory stores bounded and bias retrieval toward "
        "still-relevant facts.",
        "technique",
        0.7,
        "https://arxiv.org/abs/2404.00573",
    ),
    (
        "022",
        "Privacy risks of persistent agent memory",
        "Persistent memory accumulates PII across sessions; leakage through retrieval into "
        "unrelated contexts and the difficulty of honoring deletion requests are open "
        "compliance problems.",
        "challenge",
        0.8,
        "https://arxiv.org/abs/2409.00729",
    ),
    (
        "023",
        "Shared memory coordinates multi-agent systems",
        "Multi-agent frameworks use shared memory (blackboards, shared vector stores, common "
        "KGs) for coordination; consistency and write contention mirror classic distributed-"
        "systems problems.",
        "concept",
        0.75,
        "https://arxiv.org/abs/2402.01680",
    ),
    (
        "024",
        "A-Mem links memories into a Zettelkasten graph",
        "A-Mem stores each memory as an atomic note, auto-generates links to related notes, and "
        "evolves old notes when new ones arrive — a dynamic, self-organizing memory graph "
        "without a fixed schema.",
        "architecture",
        0.8,
        "https://arxiv.org/abs/2502.12110",
    ),
    (
        "025",
        "RAG is the substrate most agent memory builds on",
        "Most agent memory systems are specialized RAG: write path (extract, embed, store) plus "
        "read path (retrieve, rerank, inject). Innovations differ mainly in what gets written "
        "and how retrieval is scored.",
        "concept",
        0.85,
        "https://arxiv.org/abs/2312.10997",
    ),
    (
        "026",
        "Memory poisoning attacks on agent stores",
        "Adversarial content can plant persistent false memories that later steer agent "
        "behavior (e.g. exfiltration instructions recalled as trusted context); write-time "
        "provenance checks are the main defense.",
        "challenge",
        0.75,
        "https://arxiv.org/abs/2407.12784",
    ),
    (
        "027",
        "Needle-in-a-haystack tests overstate memory ability",
        "High needle-in-a-haystack scores don't transfer to realistic memory use: retrieval "
        "from clean planted text ignores integration, updating, and conflicting-fact "
        "resolution that real agent memory requires.",
        "evaluation",
        0.7,
        "https://arxiv.org/abs/2407.01437",
    ),
    (
        "028",
        "Sleep-time compute reorganizes memory offline",
        "Letta's sleep-time compute runs background agents that re-derive and reorganize memory "
        "between sessions — trading idle compute for better-organized context at interaction "
        "time.",
        "technique",
        0.7,
        "https://arxiv.org/abs/2504.13171",
    ),
]

# --- intent schema ------------------------------------------------------------

INTENT_SCHEMA: dict = {
    "description": "Map the landscape of memory systems for agentic AI.",
    "node_types": ["Concept", "System", "Technique", "Benchmark", "Challenge"],
    "relation_types": [
        "implements",
        "addresses",
        "evaluates",
        "uses",
        "extends",
        "contrasts_with",
    ],
    "competency_questions": [
        "Which systems implement which memory techniques?",
        "Which challenges does each technique address?",
        "Which benchmarks evaluate which systems?",
        "How do graph-backed and vector-backed memory differ?",
    ],
}

# --- KG nodes: (type, label, gloss, [finding id-suffixes]) --------------------

NODES: list[tuple[str, str, str, list[str]]] = [
    ("System", "MemGPT", "OS-inspired self-managed memory hierarchy", ["002"]),
    ("System", "Letta", "MemGPT successor with persistent memory blocks", ["007", "028"]),
    ("System", "Zep", "Temporal knowledge-graph memory service", ["009"]),
    ("System", "Mem0", "Layered memory with extraction pipeline", ["010"]),
    ("System", "LangMem", "LangChain memory SDK", ["008"]),
    ("System", "Generative Agents", "Memory-stream simulacra architecture", ["005"]),
    ("System", "HippoRAG", "Hippocampal-index-inspired graph RAG", ["014"]),
    ("System", "A-Mem", "Zettelkasten-style self-linking memory", ["024"]),
    ("System", "Reflexion", "Self-critique episodic learning loop", ["004"]),
    ("System", "Voyager", "Skill-library agent for open-ended tasks", ["019"]),
    ("Concept", "Episodic memory", "Stored traces of specific interactions", ["001", "004"]),
    ("Concept", "Semantic memory", "Distilled facts independent of episodes", ["001"]),
    ("Concept", "Procedural memory", "Persisted skills and routines", ["019"]),
    ("Concept", "Working memory", "Transient in-context task state", ["018"]),
    ("Concept", "Long-term memory", "Persistence beyond a single session", ["003", "016"]),
    ("Concept", "Memory stream", "Append-only observation log", ["005"]),
    ("Concept", "Context window", "The model's bounded attention budget", ["011"]),
    ("Concept", "RAG", "Retrieve-then-generate memory substrate", ["025"]),
    ("Concept", "Knowledge-graph memory", "Entity-relation structured store", ["015", "009"]),
    ("Concept", "Shared memory", "Cross-agent coordination store", ["023"]),
    ("Technique", "Memory paging", "Swap context between window and store", ["002"]),
    ("Technique", "Retrieval scoring", "Recency x importance x relevance", ["006", "005"]),
    ("Technique", "Periodic summarization", "Scheduled episodic-to-semantic compression", ["013"]),
    ("Technique", "Memory decay", "Down-weight or evict stale memories", ["021"]),
    ("Technique", "Write policies", "Gate what gets persisted", ["020"]),
    ("Technique", "Note linking", "Auto-link atomic memory notes", ["024"]),
    ("Technique", "Temporal knowledge graphs", "Facts with validity intervals", ["009"]),
    ("Technique", "Vector search", "Embedding-similarity recall", ["003"]),
    ("Technique", "Hippocampal indexing", "Graph index over content traces", ["014"]),
    ("Technique", "Sleep-time compute", "Offline memory reorganization", ["028"]),
    ("Benchmark", "LOCOMO", "Very-long-conversation memory QA", ["016"]),
    ("Benchmark", "GoodAI LTM", "Continual recall with distractors", ["017"]),
    ("Benchmark", "Needle-in-a-haystack", "Planted-fact retrieval probe", ["027"]),
    ("Benchmark", "MemBench", "Holistic agent-memory evaluation", ["027", "016"]),
    ("Challenge", "Catastrophic forgetting", "Loss of critical details over horizons", ["012"]),
    ("Challenge", "Context window limits", "Cost and degradation of long contexts", ["011"]),
    ("Challenge", "Memory poisoning", "Adversarial persistent false memories", ["026"]),
    ("Challenge", "Privacy leakage", "PII recalled into wrong contexts", ["022"]),
    ("Challenge", "Retrieval noise", "Irrelevant memories crowding context", ["020", "006"]),
    ("Challenge", "Consolidation cost", "Compute/latency of memory maintenance", ["013", "028"]),
]

# --- KG edges: (source label, relation, target label) -------------------------

EDGES: list[tuple[str, str, str]] = [
    # systems implement techniques
    ("MemGPT", "implements", "Memory paging"),
    ("MemGPT", "implements", "Vector search"),
    ("Letta", "implements", "Memory paging"),
    ("Letta", "implements", "Sleep-time compute"),
    ("Letta", "implements", "Write policies"),
    ("Zep", "implements", "Temporal knowledge graphs"),
    ("Zep", "implements", "Vector search"),
    ("Mem0", "implements", "Write policies"),
    ("Mem0", "implements", "Periodic summarization"),
    ("Mem0", "implements", "Memory decay"),
    ("LangMem", "implements", "Periodic summarization"),
    ("LangMem", "implements", "Write policies"),
    ("Generative Agents", "implements", "Retrieval scoring"),
    ("Generative Agents", "implements", "Periodic summarization"),
    ("HippoRAG", "implements", "Hippocampal indexing"),
    ("HippoRAG", "implements", "Vector search"),
    ("A-Mem", "implements", "Note linking"),
    ("A-Mem", "implements", "Retrieval scoring"),
    ("Reflexion", "implements", "Write policies"),
    ("Voyager", "implements", "Vector search"),
    # systems use concepts
    ("MemGPT", "uses", "Context window"),
    ("MemGPT", "uses", "Long-term memory"),
    ("Letta", "uses", "Long-term memory"),
    ("Letta", "uses", "Working memory"),
    ("Zep", "uses", "Knowledge-graph memory"),
    ("Zep", "uses", "Episodic memory"),
    ("Mem0", "uses", "Episodic memory"),
    ("Mem0", "uses", "Semantic memory"),
    ("Mem0", "uses", "Working memory"),
    ("LangMem", "uses", "Semantic memory"),
    ("LangMem", "uses", "Procedural memory"),
    ("Generative Agents", "uses", "Memory stream"),
    ("Generative Agents", "uses", "Episodic memory"),
    ("HippoRAG", "uses", "Knowledge-graph memory"),
    ("HippoRAG", "uses", "RAG"),
    ("A-Mem", "uses", "Episodic memory"),
    ("A-Mem", "uses", "Semantic memory"),
    ("Reflexion", "uses", "Episodic memory"),
    ("Reflexion", "uses", "Working memory"),
    ("Voyager", "uses", "Procedural memory"),
    # techniques/systems address challenges
    ("Memory paging", "addresses", "Context window limits"),
    ("Periodic summarization", "addresses", "Context window limits"),
    ("Periodic summarization", "addresses", "Catastrophic forgetting"),
    ("Write policies", "addresses", "Retrieval noise"),
    ("Write policies", "addresses", "Memory poisoning"),
    ("Memory decay", "addresses", "Retrieval noise"),
    ("Memory decay", "addresses", "Privacy leakage"),
    ("Retrieval scoring", "addresses", "Retrieval noise"),
    ("Sleep-time compute", "addresses", "Consolidation cost"),
    ("Note linking", "addresses", "Catastrophic forgetting"),
    ("Temporal knowledge graphs", "addresses", "Catastrophic forgetting"),
    ("MemGPT", "addresses", "Context window limits"),
    ("Zep", "addresses", "Catastrophic forgetting"),
    ("Mem0", "addresses", "Context window limits"),
    # benchmarks evaluate systems/concepts
    ("LOCOMO", "evaluates", "Mem0"),
    ("LOCOMO", "evaluates", "Zep"),
    ("LOCOMO", "evaluates", "Long-term memory"),
    ("GoodAI LTM", "evaluates", "Letta"),
    ("GoodAI LTM", "evaluates", "Long-term memory"),
    ("Needle-in-a-haystack", "evaluates", "Context window"),
    ("MemBench", "evaluates", "MemGPT"),
    ("MemBench", "evaluates", "A-Mem"),
    # lineage / extension
    ("Letta", "extends", "MemGPT"),
    ("A-Mem", "extends", "Generative Agents"),
    ("HippoRAG", "extends", "RAG"),
    ("Mem0", "extends", "RAG"),
    # contrasts
    ("Knowledge-graph memory", "contrasts_with", "RAG"),
    ("Episodic memory", "contrasts_with", "Semantic memory"),
    ("Working memory", "contrasts_with", "Long-term memory"),
    ("Needle-in-a-haystack", "contrasts_with", "LOCOMO"),
]

# --- synopsis ------------------------------------------------------------------

SYNOPSIS: list[dict] = [
    {
        "topic": "Memory architectures",
        "gloss": "MemGPT/Letta-style paged hierarchies, memory streams, and Zettelkasten "
        "graphs are the dominant agent memory designs.",
    },
    {
        "topic": "Memory types",
        "gloss": "Episodic, semantic, procedural, and working memory map onto distinct "
        "stores and retrieval paths in agent systems.",
    },
    {
        "topic": "Retrieval and scoring",
        "gloss": "Recency x importance x relevance scoring beats pure cosine similarity for "
        "selecting agent context.",
    },
    {
        "topic": "Graph vs vector memory",
        "gloss": "Temporal knowledge graphs (Zep, HippoRAG) serve multi-hop and temporal "
        "queries flat vector stores cannot.",
    },
    {
        "topic": "Maintenance",
        "gloss": "Consolidation, decay schedules, write gating, and sleep-time compute keep "
        "stores bounded and relevant.",
    },
    {
        "topic": "Risks and evaluation",
        "gloss": "LOCOMO and LTM benchmarks expose gaps needle-in-a-haystack hides; memory "
        "poisoning and privacy leakage are open problems.",
    },
]


def fake_embedding(key: str) -> list[float]:
    """Deterministic 1536-dim unit vector seeded by `key` (no API calls)."""
    rng = random.Random(f"seed-demo-kb::{key}")
    v = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


async def seed() -> None:
    from delapan.store import get_store

    store = get_store()
    org_id, project_id = store.resolve_project("demo", create=True)
    kb_id = store.resolve_kb(org_id, project_id, "main", create=True)

    # Findings — guarded by their stable ids (insert is not an upsert).
    def fid(suffix: str) -> str:
        return f"demo-finding-{suffix}"

    new_rows: list[dict] = []
    for suffix, title, summary, category, confidence, url in FINDINGS:
        try:
            store.get_finding(kb_id, fid(suffix))
            continue  # already seeded
        except Exception:  # noqa: BLE001 — store raises on a missing finding
            pass
        domain = url.split("/")[2]
        new_rows.append(
            {
                "id": fid(suffix),
                "org_id": org_id,
                "kb_id": kb_id,
                "title": title,
                # Dict content — the shape the local store round-trips (a plain
                # string would decode to {} through `_json_load` on read).
                "content": {"summary": summary},
                "category": category,
                "confidence": confidence,
                "tags": ["agent-memory", category],
                "provenance": [
                    {"url": url, "domain": domain, "query": "agentic AI memory systems"}
                ],
                "embedding": fake_embedding(f"finding::{title}"),
            }
        )
    inserted_findings = len(await store.insert_findings(new_rows))

    # Intent schema — versioned; only set on first run.
    intent = store.get_kg_intent(kb_id)
    if intent is None:
        intent = store.set_kg_intent(org_id, kb_id, INTENT_SCHEMA)

    # Nodes — upsert dedupes on (type, label), so re-runs are stable.
    node_rows = [
        {
            "org_id": org_id,
            "type": typ,
            "label": label,
            "properties": {"gloss": gloss},
            "grounded_in": [fid(s) for s in grounded],
            "embedding": fake_embedding(f"node::{typ}::{label}"),
        }
        for typ, label, gloss, grounded in NODES
    ]
    node_ids = await store.upsert_kg_nodes(kb_id, node_rows)
    id_by_label = {label: nid for (_, label, _, _), nid in zip(NODES, node_ids)}

    # Edges — upsert skips existing (source, target, relation) triples.
    edge_rows = [
        {
            "org_id": org_id,
            "source_node_id": id_by_label[src],
            "target_node_id": id_by_label[dst],
            "relation": rel,
            "properties": {},
            "grounded_in": [],
        }
        for src, rel, dst in EDGES
    ]
    inserted_edges = await store.upsert_kg_edges(kb_id, edge_rows)

    # Synopsis — one current row per KB; upsert is idempotent.
    store.upsert_synopsis(
        kb_id, content=SYNOPSIS, finding_count=store.count_findings(kb_id), model="seed-demo"
    )

    stats = store.kg_stats(kb_id)
    print("Seeded demo/main:")
    print(f"  kb_id            {kb_id}")
    print(f"  findings         {store.count_findings(kb_id)} (+{inserted_findings} this run)")
    print(f"  intent schema    v{intent['version']}")
    print(f"  kg nodes         {stats['node_count']} ({stats['by_type']})")
    print(f"  kg edges         {stats['edge_count']} (+{inserted_edges} this run)")
    print(f"  synopsis entries {len(SYNOPSIS)}")


if __name__ == "__main__":
    asyncio.run(seed())
