"""Web-research exploration pipeline (plan → search → crawl → extract → merge).

Pure of Supabase, SSE, and FastAPI: `run_exploration` takes a prompt + config
and a progress callback, and returns a list of `Finding` objects. `tools/explore.py`
owns persistence. LLM calls route through Vercel AI Gateway; web search/extract
via Tavily.
"""

from delapan.core.exploration.engine import ingest_pages, run_exploration
from delapan.core.exploration.models import (
    ExplorationPlan,
    Finding,
    RawFinding,
    SearchQuery,
    Source,
)

__all__ = [
    "ingest_pages",
    "run_exploration",
    "ExplorationPlan",
    "Finding",
    "RawFinding",
    "SearchQuery",
    "Source",
]
