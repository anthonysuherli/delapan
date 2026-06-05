"""Shared types for the agent graph + tool dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict


class MessagePart(TypedDict, total=False):
    type: Literal["text", "tool-call"]
    text: str
    toolCallId: str
    toolName: str
    args: dict[str, Any]
    status: Literal["running", "success", "error"]
    phase: str
    result: Any
    error: str


class Message(TypedDict, total=False):
    id: str
    role: Literal["user", "assistant", "system", "tool"]
    parts: list[MessagePart]
    meta: dict[str, Any]


@dataclass
class TenantContext:
    """Resolved tenancy for the current request."""

    user_id: str
    org_id: str
    project_id: str
    kb_id: str
    thread_id: str
    access_token: str


@dataclass
class StreamEvent:
    """SSE event emitted to the frontend."""

    type: Literal["phase", "tool_call", "tool_result", "text_delta", "error", "done", "narration"]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SlashInvocation:
    """Parsed slash command from the user."""

    cmd: str
    args: dict[str, Any]
