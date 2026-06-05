"""MCP surface for the open-core delapan engine.

A third entry path alongside the (cloud-only) HTTP API: an in-process FastMCP
stdio server that resolves tenancy by name and drains the same engine through the
Store seam. Exposes a deliberately small four-tool surface — resume / search /
explore / projects.
"""

from __future__ import annotations
