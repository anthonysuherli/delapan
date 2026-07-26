"""Stranger round-trip: cold-start the wrapper in a hermetic env, zero keys.

    tmp HOME ──► scripts/mcp-server.sh ──► seed demo ──► projects/resume over stdio
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "mcp-server.sh"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_stranger_round_trip(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),  # → ~/.delapan lands in tmp; seeder must fill it
        "CLAUDE_PLUGIN_ROOT": str(REPO),
        "DELAPAN_BACKEND": "local",
        # a stranger has no keys; force-empty so the maintainer's .env can't leak in
        "AI_GATEWAY_API_KEY": "",
        "OPENAI_API_KEY": "",
        "TAVILY_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "SUPABASE_URL": "",
        "SUPABASE_SERVICE_ROLE_KEY": "",
        # reuse the real uv cache so the cold start doesn't re-download the world
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache" / "uv")),
        "UV_PROJECT_ENVIRONMENT": str(REPO / ".venv"),
    }
    params = StdioServerParameters(command=str(WRAPPER), env=env)
    async with asyncio.timeout(300):  # bound cold uv resolve + handshake; hang → fail, not wedge
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            projects = _payload(await session.call_tool("delapan_projects", {}))
            assert any(p["project"] == "delapan" for p in projects["projects"]), (
                "demo KB must be seeded on first start"
            )

            resume = _payload(
                await session.call_tool(
                    "delapan_resume", {"project": "delapan", "kb": "demo"}
                )
            )
            assert "<synopsis>" in resume["preamble"], "zero-key resume must render the demo"

            card = _payload(
                await session.call_tool(
                    "delapan_resume", {"project": "ghost", "kb": "ghost"}
                )
            )
            assert "onboarding" in card and "try_demo" in card
