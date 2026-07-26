"""Minimal eval answerer — mirrors core/canvas/answer.py's grounding shape.

    question + (context xml | None) ──► system prompt ──► text_completion ──► answer
"""

from __future__ import annotations

from delapan.core.clients.ai_gateway import text_completion

ABSTAIN_MARKER = "I cannot answer this from the available information."

GROUNDED_SYSTEM = (
    "Answer the user's question concisely using the knowledge-base context below. "
    "If the context and your own knowledge are insufficient to answer reliably, "
    f'reply exactly: "{ABSTAIN_MARKER}"\n\n'
    "## KB context\n{context}"
)

CLOSED_BOOK_SYSTEM = (
    "Answer the user's question concisely from your own knowledge. "
    "If you cannot answer reliably, "
    f'reply exactly: "{ABSTAIN_MARKER}"'
)


async def answer_question(
    question: str, context_xml: str | None, *, model: str, max_tokens: int = 800
) -> str:
    system = (
        GROUNDED_SYSTEM.format(context=context_xml)
        if context_xml is not None
        else CLOSED_BOOK_SYSTEM
    )
    return await text_completion(
        model=model, system=system, user=question, max_tokens=max_tokens
    )
