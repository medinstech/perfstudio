"""The MCP server: PerfStudio as something an agent can drive (PLAN.md Sec 9, M6).

``session.py`` is the whole of it -- one board, its command bus, and every operation as
a method returning plain data. ``server.py`` binds those methods to the protocol and
nothing else, and is the only file here that imports ``mcp``, so the session is
importable and testable on an install without it.

Run it with::

    python -m perfstudio.mcp

or register it with a client (see docs/MCP.md).
"""

from __future__ import annotations

from perfstudio.mcp.session import BoardSession, SessionError, new_board

__all__ = ["BoardSession", "SessionError", "new_board"]
