"""Text-format importers: turning other tools' file formats into PerfStudio's model.

Each parser here is pure (string in, data out; no filesystem access) so it can be unit
tested without touching disk and reused identically from the desktop app, the CLI, and
the MCP server. Ported from packages/parsers/src/*.ts.
"""

from __future__ import annotations

from .kicad import ImportedComponent, KicadNetlistImport, infer_net_class, parse_kicad_netlist
from .sexpr import SExpr, SExprSyntaxError, parse_sexpr

__all__ = [
    "ImportedComponent",
    "KicadNetlistImport",
    "SExpr",
    "SExprSyntaxError",
    "infer_net_class",
    "parse_kicad_netlist",
    "parse_sexpr",
]
