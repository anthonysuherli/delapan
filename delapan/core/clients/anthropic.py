"""Anthropic client wrapper for the LangGraph agent."""

from __future__ import annotations

from functools import lru_cache

from langchain_anthropic import ChatAnthropic

from delapan.core.config import get_config, get_settings


@lru_cache(maxsize=2)
def chat_model(model: str | None = None) -> ChatAnthropic:
    """Memoized ChatAnthropic instance. Defaults to the configured agent model.

    When ``agent.thinking_budget > 0``, extended thinking is enabled. Anthropic
    rejects thinking unless ``temperature == 1`` and requires the budget to be
    strictly below ``max_tokens``, so we force both here rather than trusting the
    config to stay consistent.
    """
    settings = get_settings()
    agent = get_config().agent

    kwargs: dict = dict(
        model=model or agent.model,
        anthropic_api_key=settings.anthropic_api_key,
        max_tokens=agent.max_tokens,
        streaming=True,
    )
    if agent.thinking_budget > 0:
        kwargs["temperature"] = 1.0  # required by the API when thinking is enabled
        kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": min(agent.thinking_budget, agent.max_tokens - 1),
        }
    else:
        kwargs["temperature"] = agent.temperature

    return ChatAnthropic(**kwargs)  # type: ignore[arg-type]


def text_of(content: object) -> str:
    """Extract assistant text from a LangChain response.

    ChatAnthropic returns `content` as a plain str for simple replies, but as a
    list of content blocks (`[{"type": "text", "text": ...}, ...]`) once tools
    are bound. Handle both, else the ReAct loop silently drops the answer.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""
