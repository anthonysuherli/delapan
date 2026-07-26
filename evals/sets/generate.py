"""Draft eval questions from findings; reject lexical leakage; human curates.

    findings ──► structured_completion (drafts) ──► has_leakage gate ──► draft yaml
    (a human reviews/edits the draft, then freezes it as sets/v1.yaml)
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from delapan.core.clients.ai_gateway import structured_completion
from delapan.store import Store

_DRAFT_SYSTEM = (
    "Write one question a user could ask that this finding answers, plus the "
    "reference answer, USING YOUR OWN WORDS — never copy phrases from the finding. "
    "Prefer questions requiring the finding's specific facts (versions, numbers, dates)."
)


class DraftQuestion(BaseModel):
    question: str
    reference_answer: str
    type: Literal["single-hop", "multi-hop", "temporal"] = "single-hop"


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def has_leakage(question: str, source_text: str, max_span: int = 8) -> bool:
    """True when the question contains a contiguous run of >= max_span tokens
    that also appears contiguously in the source — the RAGAS-testset failure
    mode where retrieval scores are inflated because the question echoes the chunk."""
    q, s = _tokens(question), _tokens(source_text)
    if len(q) < max_span:
        return False
    spans = {tuple(s[i : i + max_span]) for i in range(len(s) - max_span + 1)}
    return any(tuple(q[i : i + max_span]) in spans for i in range(len(q) - max_span + 1))


async def draft_questions(
    store: Store, kb_id: str, *, model: str, per_finding: int = 1
) -> list[dict]:
    """Draft evaluation questions from KB findings, filtering lexical leakage.

    Args:
        store: Store instance for finding retrieval.
        kb_id: Knowledge base ID.
        model: LLM model for drafting questions.
        per_finding: Number of drafts per finding (default 1).

    Returns:
        List of dicts with keys: id, question, reference_answer,
        gold_finding_ids, type. Ready for yaml output and manual curation.
    """
    listed = store.list_findings(kb_id, limit=1000)["findings"]
    drafts: list[dict] = []
    for row in listed:
        full = store.get_finding(kb_id, row["id"])
        source = f"{full.get('title', '')}\n{full.get('content', '')}"
        for _ in range(per_finding):
            d = await structured_completion(
                model=model,
                response_format=DraftQuestion,
                system=_DRAFT_SYSTEM,
                user=source,
            )
            if has_leakage(d.question, source):
                continue  # drop, don't rephrase — the human curator adds coverage
            drafts.append(
                {
                    "id": f"q-{row['id']}",
                    "question": d.question,
                    "reference_answer": d.reference_answer,
                    "gold_finding_ids": [row["id"]],
                    "type": d.type,
                }
            )
    return drafts
