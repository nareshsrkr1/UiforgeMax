"""Tool handlers.

Handlers are framework-independent functions so they can be unit tested without
a running MCP transport. ``server.py`` adapts them to MCP tools. Every handler
that mutates pipeline state calls :func:`uiforgemax.state.gates.assert_gate`
first.
"""

from uiforgemax.tools.context import ToolContext

__all__ = ["ToolContext"]
