"""PerfStudio: perfboard layout design, verification and a soldering guide.

The package root stays deliberately thin -- importing ``perfstudio`` must not drag in
Qt, VTK, the router or anything else with a cost, because the MCP server, the CLI and
the test suite all import narrow pieces of the engine and pay for whatever the root
touches. Only the version number lives here, and it is re-exported from
``perfstudio.version`` so that ``perfstudio.__version__`` works the way every other
Python package makes people expect it to.
"""

from __future__ import annotations

from perfstudio.version import __version__

__all__ = ["__version__"]
