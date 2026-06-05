"""Vercel AI Gateway client — OpenAI-compatible structured-output calls.

The exploration pipeline's LLM calls (query planning, finding extraction) route
through Vercel AI Gateway using the `openai` SDK pointed at the Gateway base URL.
Models are provider/model strings, e.g. ``anthropic/claude-sonnet-4-6`` or
``openai/gpt-4o-mini``. Pydantic models are the structured-output schema.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import TYPE_CHECKING, TypeVar, cast

from openai import AsyncOpenAI, omit
from pydantic import BaseModel

if TYPE_CHECKING:
    from openai.types.shared.reasoning_effort import ReasoningEffort

from delapan.core.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@lru_cache(maxsize=1)
def gateway_client() -> AsyncOpenAI:
    """Memoized AsyncOpenAI pointed at the AI Gateway base URL."""
    s = get_settings()
    return AsyncOpenAI(api_key=s.ai_gateway_api_key, base_url=s.ai_gateway_base_url)


async def text_completion(
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> str:
    """One plain-text completion routed through AI Gateway — no structured output.

    Mirrors ``structured_completion``'s client/gateway setup but omits any
    ``response_format`` and returns the assistant message text verbatim (``""``
    if the model returns no content). Used for cheap, best-effort prose such as
    per-phase narration.
    """
    completion = await gateway_client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens if max_tokens is not None else omit,
    )
    return completion.choices[0].message.content or ""


async def structured_completion(
    *,
    model: str,
    response_format: type[T],
    system: str,
    user: str,
    temperature: float = 0.0,
    fallback_model: str | None = None,
    use_json_schema: bool = True,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> T:
    """One structured-output completion parsed into ``response_format``.

    Routes through AI Gateway. On any failure (transport error, refusal, or
    parse failure) retries once with ``fallback_model`` when provided. Raises
    the last exception if every model fails.

    ``use_json_schema`` controls the mechanism. ``True`` (default) uses the
    gateway's strict ``json_schema`` response format — correct for fully-typed
    schemas. Set ``False`` for schemas with free-form ``dict[str, Any]`` fields
    (e.g. the extractor's ``content``): strict mode forces those to ``{}``, so
    we instead instruct the schema in the prompt and validate the reply.

    ``max_tokens`` caps the completion's output budget; ``None`` lets the
    provider default apply. Raise it for long structured outputs (e.g. a full
    research report) the ~4k default would otherwise truncate mid-JSON.
    """
    models = [model]
    if fallback_model and fallback_model != model:
        models.append(fallback_model)

    last_exc: Exception | None = None
    for m in models:
        try:
            return await _attempt(
                m,
                response_format,
                system,
                user,
                temperature,
                use_json_schema,
                max_tokens,
                reasoning_effort,
            )
        except Exception as exc:  # noqa: BLE001 — provider/transport/parse failures
            last_exc = exc
            logger.warning("structured_completion failed on model=%s: %s", m, exc)

    assert last_exc is not None  # models is non-empty
    raise last_exc


async def _attempt(
    model: str,
    response_format: type[T],
    system: str,
    user: str,
    temperature: float,
    use_json_schema: bool,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> T:
    """Get structured output. With ``use_json_schema`` try the gateway's strict
    ``json_schema`` format first and fall back to prompt-instructed JSON;
    otherwise go straight to prompt-instructed JSON."""
    if not use_json_schema:
        return await _parse_prompt_json(
            model, response_format, system, user, temperature, max_tokens, reasoning_effort
        )
    try:
        return await _parse_json_schema(
            model, response_format, system, user, temperature, max_tokens, reasoning_effort
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("json_schema failed for %s (%s); retrying prompt-JSON mode", model, exc)
        return await _parse_prompt_json(
            model, response_format, system, user, temperature, max_tokens, reasoning_effort
        )


async def _parse_json_schema(
    model: str,
    response_format: type[T],
    system: str,
    user: str,
    temperature: float,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> T:
    """AI Gateway structured output via `response_format: {type: json_schema}`.

    The gateway returns the JSON as a *string* in `message.content` (not the
    OpenAI SDK's `.parsed` field), so we validate it against the model ourselves.
    """
    completion = await gateway_client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": response_format.__name__,
                "schema": response_format.model_json_schema(),
            },
        },
        temperature=temperature,
        max_tokens=max_tokens if max_tokens is not None else omit,
        reasoning_effort=cast("ReasoningEffort", reasoning_effort)
        if reasoning_effort is not None
        else omit,
    )
    return response_format.model_validate_json(completion.choices[0].message.content or "")


async def _parse_prompt_json(
    model: str,
    response_format: type[T],
    system: str,
    user: str,
    temperature: float,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> T:
    """Last resort: no `response_format` — instruct JSON in the prompt and
    validate. Covers providers/models that reject the json_schema parameter."""
    schema = json.dumps(response_format.model_json_schema())
    completion = await gateway_client().chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    f"{system}\n\nReturn ONLY a single JSON object matching this JSON "
                    f"Schema. No prose, no markdown code fences:\n{schema}"
                ),
            },
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens if max_tokens is not None else omit,
        reasoning_effort=cast("ReasoningEffort", reasoning_effort)
        if reasoning_effort is not None
        else omit,
    )
    content = completion.choices[0].message.content or ""
    return response_format.model_validate_json(_strip_fences(content))


def _strip_fences(text: str) -> str:
    """Strip ```json ... ``` fences a model may wrap its JSON in."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()
