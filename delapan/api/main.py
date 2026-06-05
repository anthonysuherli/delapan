"""FastAPI app for the open-core engine — loopback /health only.

    uvicorn delapan.api.main:app  ──►  GET /health

The cloud tier's full HTTP surface (/agent, /v1/*, /internal/*) lives behind the
``[cloud]`` extra; open-core exposes just the health loopback so the process is
observable. The MCP stdio server (``python -m delapan.mcp.server``) is the
primary open-core entry path.
"""

from __future__ import annotations

from fastapi import FastAPI

from delapan.api.health import router as health_router

app = FastAPI(title="delapan (open-core)")
app.include_router(health_router)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001)


if __name__ == "__main__":
    main()
