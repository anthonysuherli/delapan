"""External-service clients for the engine.

Open-core build: only the non-cloud clients live here.

  * ``ai_gateway`` — Vercel AI Gateway (OpenAI-compatible) structured/text
    completions for the exploration + KG pipeline.
  * ``anthropic`` — ChatAnthropic wrapper for the chat agent / ReAct loop.
  * ``embeddings`` — OpenAI embedding client.
  * ``tavily`` — web search + page extraction.

The Supabase and PostHog clients are intentionally absent — persistence goes
through the ``delapan.store`` seam (local SQLite by default), and analytics are
not part of the open-core surface.
"""

from __future__ import annotations
