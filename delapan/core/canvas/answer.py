"""Grounded streaming answer for the canvas search thread.

    preamble (KB) + candidates (fresh web) ─► system prompt
    history + prompt ─────────────────────► messages
                └─► stream_text_completion ─► text deltas

The KB preamble grounds the reply in what the user already keeps; candidates
ground it in what this search just surfaced. Persistence is elsewhere (/keep).
"""

from __future__ import annotations

from typing import AsyncIterator

from delapan.core.clients.ai_gateway import stream_text_completion
from delapan.core.config import CanvasConfig
from delapan.core.exploration.models import Finding
from delapan.core.exploration.render import render_content

_SYSTEM_TEMPLATE = (
    "You are the research companion on a knowledge-canvas. Answer the user's "
    "question concisely from the grounding below. Prefer candidate findings for "
    "fresh facts and the KB preamble for what the user already knows; name "
    "candidate titles when you draw on them. If the grounding is insufficient, "
    "say so plainly.\n\n"
    "## KB preamble\n{preamble}\n\n## Candidate findings (this search)\n{candidates}"
)


def _candidate_digest(candidates: list[Finding], *, char_budget: int = 700) -> str:
    blocks = []
    for i, f in enumerate(candidates):
        body = render_content(f.content)[:char_budget]
        blocks.append(f"[{i}] {f.title} ({f.category})\n{body}")
    return "\n\n".join(blocks) if blocks else "(none)"


def build_answer_messages(prompt: str, history: list[dict], cfg: CanvasConfig) -> list[dict]:
    """Thread messages for synthesis: clamped history then the new user turn."""
    clamped = [
        {"role": m.get("role", "user"), "content": str(m.get("content", ""))}
        for m in history[-cfg.max_history_turns :]
    ]
    return [*clamped, {"role": "user", "content": prompt}]


async def stream_answer(
    prompt: str,
    *,
    preamble_xml: str,
    candidates: list[Finding],
    history: list[dict],
    cfg: CanvasConfig,
) -> AsyncIterator[str]:
    """Yield answer text deltas grounded in the KB preamble + candidates."""
    system = _SYSTEM_TEMPLATE.format(
        preamble=preamble_xml, candidates=_candidate_digest(candidates)
    )
    async for delta in stream_text_completion(
        model=cfg.answer_model,
        system=system,
        messages=build_answer_messages(prompt, history, cfg),
        max_tokens=cfg.answer_max_tokens,
    ):
        yield delta
