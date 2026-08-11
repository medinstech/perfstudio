"""Binding the board session to MCP. This file is protocol and nothing else.

Every tool below is two lines: a docstring the model reads to decide whether to call it,
and a call into ``BoardSession``. The behaviour lives there, is plain Python and is
tested without a client -- because a test that needs a live MCP session is testing the
transport, and the interesting failures are never in the transport.

THE STDOUT TRAP (PLAN.md Sec 9.1). On stdio, stdout IS the protocol. One stray print
corrupts the stream and the client reports something baffling and unrelated. So this
module configures logging to stderr before anything else, and nothing anywhere under
``perfstudio.mcp`` may print. The engine has no prints; the Qt and VTK imports the
render tools pull in are the real risk, which is another reason they are imported lazily
inside the tools rather than at module scope.

THIRTY-ONE TOOLS, against PLAN.md Sec 2's "~25, deliberately narrow", and the overage is
stated rather than hidden. Each tool is a verb an agent cannot compose from the others,
and the surface was trimmed rather than grown: the history listing folded into
``get_status``, and "solder bridge" is not a separate tool from ``add_solder_trace``
because a bridge is a two-pad trace and one concept should not have two names.
``get_board_info`` stayed separate from ``get_status`` for a reason worth knowing --
status runs DRC, LVS and the ratsnest, and an agent that only wants the pitch should not
pay for that.

The two the plan singles out as critical are here and are worth every byte of their
schema: ``render_2d_view``/``render_3d_view``, because an agent editing a board it
cannot see is working blind, and ``snapshot``/``restore``, because it has to be able to
try something drastic and get back out.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from perfstudio.mcp.session import BoardSession, SessionError, new_board
from perfstudio.version import __version__

# Before anything else, and to stderr. See the module docstring.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="perfstudio-mcp: %(message)s")
log = logging.getLogger("perfstudio.mcp")

INSTRUCTIONS = f"""\
PerfStudio {__version__} — design a circuit on pad-per-hole perfboard, verify it, and
produce a soldering guide.

Holes are addressed like a spreadsheet: column letters then a 1-based row — A1, C7, AC12.
Every tool speaks that language; there are no raw coordinates in this API.

A perfboard connection is not one thing, and choosing between them is most of the craft:
  solder-trace   adjacent pads joined with solder. Cheap, but orthogonal steps only and
                 it cannot cross anything.
  bare-wire      tinned wire on the solder side. Cannot cross other copper.
  insulated-wire may cross anything, at the cost of stripping and preparing it.
  top-jumper     insulated, over the component side.
Prefer traces, then bare wire, then insulated wire. `autoroute` already knows this.

A good working order: get_status → import_netlist or place_component →
optimize_placement → autoroute → run_drc / run_lvs → generate_guide. Take a snapshot
before anything drastic; every edit is also undoable one step at a time.

Nothing here writes to disk unless you name a path.
"""

mcp: FastMCP[Any] = FastMCP("perfstudio", instructions=INSTRUCTIONS)

#: One board per server process. See BoardSession for why it is not a workspace of them.
session = BoardSession()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@mcp.tool()
def get_status() -> dict[str, Any]:
    """Where this board stands: part and net counts, DRC and LVS summaries, how many
    connections are still unrouted, and how deep the undo stack is. Call this first, and
    again after anything that surprises you."""
    return session.get_status()


@mcp.tool()
def get_board_info() -> dict[str, Any]:
    """The board itself: grid size, pitch, thickness, material, and the hole addresses of
    its corners."""
    return session.get_board_info()


@mcp.tool()
def list_components() -> list[dict[str, Any]]:
    """Every part on the board with its reference, value, footprint, anchor hole,
    rotation and whether it is locked."""
    return session.list_components()


@mcp.tool()
def get_component(ref: str) -> dict[str, Any]:
    """One part in full, including which hole each of its pins sits in."""
    return session.get_component(ref)


@mcp.tool()
def get_nets() -> list[dict[str, Any]]:
    """The schematic's nets — what the circuit is SUPPOSED to connect. What the board
    actually connects is a different question; ask get_net_connections."""
    return session.get_nets()


@mcp.tool()
def get_net_connections(name: str) -> dict[str, Any]:
    """What the board actually joins for one net, which pins are still in separate
    islands, and the connections that remain to be made."""
    return session.get_net_connections(name)


@mcp.tool()
def list_footprints(search: str = "") -> list[dict[str, Any]]:
    """The parts library. Free text matches the id, the name or the body type — try
    "dip", "resistor", "electrolytic", or leave it empty for all of them."""
    return session.list_footprints(search)


# ---------------------------------------------------------------------------
# Seeing
# ---------------------------------------------------------------------------


@mcp.tool()
def render_2d_view(side: str = "top", px_per_mm: int = 12) -> Image:
    """A picture of the board as the editor draws it.

    side="top" is the component side; side="bottom" is the solder side as you would see
    it holding the board up — mirrored, showing pads, cut lead ends and the copper,
    with no component bodies, because on a real board the parts are on the far face.
    """
    png, meta = session.render_2d(side=side, px_per_mm=px_per_mm)
    log.info("rendered 2D %s at %sx%s", meta["side"], meta["width_px"], meta["height_px"])
    return Image(data=png, format="png")


@mcp.tool()
def render_3d_view(flipped: bool = False) -> Image:
    """A 3D picture of the assembled board, component side or turned over. Use it to
    check that parts look right and that nothing is somewhere absurd."""
    png, meta = session.render_3d(flipped=flipped)
    log.info("rendered 3D (flipped=%s, %s actors)", meta["flipped"], meta.get("actors"))
    return Image(data=png, format="png")


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


@mcp.tool()
def open_document(path: str) -> dict[str, Any]:
    """Open a .perf board file, replacing whatever this session was working on."""
    return session.open_document(path)


@mcp.tool()
def save_document(path: str | None = None) -> dict[str, Any]:
    """Write the board to disk. Without a path it saves over the file it came from."""
    return session.save_document(path)


@mcp.tool()
def new_document(cols: int = 30, rows: int = 20, material: str = "FR4") -> dict[str, Any]:
    """Start a blank board. material is FR4, or FR2/FR1 for the cheaper phenolic kind —
    which matters: phenolic pads lift under heat, and the build guide derates the iron
    temperature and dwell time for them."""
    global session
    session = BoardSession(document=new_board(cols=cols, rows=rows, material=material))
    return session.get_status()


@mcp.tool()
def import_netlist(path: str) -> dict[str, Any]:
    """Import a KiCad netlist — this is how the circuit's intent gets onto the board at
    all, and what makes LVS and the guide's continuity checks possible. Reports any
    components the netlist names that the board does not have yet."""
    return session.import_netlist(path)


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


@mcp.tool()
def place_component(
    ref: str, footprint_id: str, hole: str, value: str = "", rotation: int = 0
) -> dict[str, Any]:
    """Put a part on the board. `hole` is where pin 1 goes ("C7"); rotation is 0, 90,
    180 or 270. Use list_footprints to find a footprint_id."""
    return session.place_component(
        ref=ref, footprint_id=footprint_id, hole=hole, value=value, rotation=rotation
    )


@mcp.tool()
def move_component(ref: str, hole: str) -> dict[str, Any]:
    """Move a part so its pin 1 sits in a different hole."""
    return session.move_component(ref, hole)


@mcp.tool()
def rotate_component(ref: str, rotation: int) -> dict[str, Any]:
    """Turn a part to an absolute rotation of 0, 90, 180 or 270 degrees."""
    return session.rotate_component(ref, rotation)


@mcp.tool()
def set_component_locked(ref: str, locked: bool = True) -> dict[str, Any]:
    """Pin a part where it is. Locked parts are never moved by optimize_placement, which
    is how you keep a connector at the edge where it has to be."""
    return session.set_component_locked(ref, locked)


@mcp.tool()
def delete_component(ref: str) -> dict[str, Any]:
    """Remove a part. Its lead bends go with it; wires and traces are left alone,
    because they may still be wanted and DRC will point at whatever dangles."""
    return session.delete_component(ref)


@mcp.tool()
def add_wire(
    from_hole: str, to_hole: str, kind: str = "insulated-wire", net: str | None = None
) -> dict[str, Any]:
    """Run a wire between two holes. "insulated-wire" may cross other copper, "bare-wire"
    may not, "top-jumper" goes over the component side. Naming the net lets LVS and the
    guide know what it is for."""
    return session.add_wire(from_hole, to_hole, kind=kind, net=net)


@mcp.tool()
def add_solder_trace(
    holes: list[str], spine_gauge_mm: float | None = None, net: str | None = None
) -> dict[str, Any]:
    """Join a chain of adjacent pads with solder — the cheapest connection there is.

    The holes must be orthogonally adjacent, in order: solder spans the 0.6 mm gap
    between neighbouring pads and does not reliably span the 1.7 mm diagonal one. Give
    spine_gauge_mm (0.6 is typical) for a run over a tinned-wire spine, which drops the
    resistance by roughly a factor of ten and is what you want for a power or ground
    rail longer than five or six pads. A two-hole trace is a solder bridge.
    """
    return session.add_solder_trace(holes, spine_gauge_mm=spine_gauge_mm, net=net)


@mcp.tool()
def remove_stale_conductors() -> dict[str, Any]:
    """Delete copper that no longer reaches the net it claims — what a moved part leaves
    behind. Run it after moving things and before routing again."""
    return session.remove_stale_conductors()


# ---------------------------------------------------------------------------
# The planners
# ---------------------------------------------------------------------------


@mcp.tool()
def autoroute(nets: list[str] | None = None, style: str = "balanced") -> dict[str, Any]:
    """Route the board, or just the named nets, and commit it as one undoable step.

    `style` chooses which primitive to reach for first, which is a judgement about the
    person building the board rather than about the board:
      "balanced"   weigh each primitive on its own cost (the default)
      "solder"     solder trace wherever solder reaches, jumper only over crossings
      "wire"       for anyone who would rather cut wire than drag solder along a row
      "lead-bend"  fold a component's own leg where it reaches, then solder, then wire
    On the NE555 fixture "balanced" gives 4 traces and 10 wires; "solder" gives 14 traces
    and no wire at all.

    Reports every connection it could NOT make, with the router's reason. Do not treat a
    partial result as a finished board: unroutable connections usually mean the parts
    are in the wrong places, and optimize_placement is the answer rather than more
    routing.
    """
    return session.autoroute(nets, style)


@mcp.tool()
def reroute(nets: list[str] | None = None, style: str = "balanced") -> dict[str, Any]:
    """Rip up the existing routing and plan it again from nothing.

    Use this after moving parts. `autoroute` only ADDS: the copper laid out for a part's
    old position still joins the right pins, so nothing flags it, and routing again just
    puts more copper beside it — the board grows every time. This throws that away and
    re-plans. Conductors with no net assigned are left alone.
    """
    return session.reroute(nets, style)


@mcp.tool()
def optimize_placement(seed: int = 0, apply: bool = True) -> dict[str, Any]:
    """Rearrange the unlocked parts to shorten the connections and make more of them
    solderable as traces rather than wires.

    Simulated annealing, deterministic for a given seed, and it judges candidates by
    actually routing them. Use apply=False to see what it would do first. Existing
    routing is not moved with the parts — remove_stale_conductors then autoroute after
    accepting one.
    """
    return session.optimize_placement(seed=seed, apply=apply)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@mcp.tool()
def run_drc() -> dict[str, Any]:
    """Check the board against the design rules: overlapping parts, crossing conductors,
    pins sharing a hole, and — the one that matters most on perfboard — solder traces
    running within a pad gap of another net, which is the commonest way a build fails."""
    return session.run_drc()


@mcp.tool()
def run_lvs() -> dict[str, Any]:
    """Compare what the board connects against what the schematic asked for. Opens,
    shorts and floating copper, named down to the pin. This is the machine answer to
    "is it actually right"."""
    return session.run_lvs()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@mcp.tool()
def generate_guide(directory: str | None = None) -> dict[str, Any]:
    """Produce the step-by-step soldering guide: phases in build order, hole addresses,
    orientations, a wire cut list, and measurement checkpoints derived from the netlist
    and from DRC's own risk list.

    Without a directory it returns the summary and anything the guide could not cover —
    which is the useful form of "is this board buildable yet". With one it writes
    guide.html (self-contained, offline), guide.json, cut_list.csv and bom.csv.
    """
    return session.generate_guide(directory)


@mcp.tool()
def export_pdf(directory: str | None = None) -> dict[str, Any]:
    """Write the 1:1 printable sheets — component side, and the mirrored solder side —
    which are meant to be printed at exactly 100% and held against the real board."""
    return session.export_pdf(directory)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@mcp.tool()
def snapshot(label: str = "") -> dict[str, Any]:
    """Remember the board as it is now, under a name you can restore later. Cheap. Take
    one before anything drastic."""
    return session.snapshot(label)


@mcp.tool()
def restore(label: str) -> dict[str, Any]:
    """Go back to a snapshot. This clears the undo stack — but the state being replaced
    is saved first as "before-restore", so the way back is another restore."""
    return session.restore(label)


@mcp.tool()
def undo() -> dict[str, Any]:
    """Undo the last change. Batched operations — autoroute, optimize_placement — undo
    as one step, not one conductor at a time."""
    return session.undo()


@mcp.tool()
def redo() -> dict[str, Any]:
    """Redo the change that was last undone."""
    return session.redo()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the server. stdio by default (PLAN.md Sec 9.1's primary transport)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        print(f"perfstudio-mcp {__version__}", file=sys.stderr)
        return 0

    transport = "stdio"
    if "--http" in args:
        # Streamable HTTP, for attaching to an already-open GUI. Never SSE: that
        # transport is deprecated and writing a new server against it would be
        # building something already on its way out.
        transport = "streamable-http"

    if args and not args[0].startswith("--"):
        result = session.open_document(args[0])
        if not result.get("ok"):
            log.error("could not open %s: %s", args[0], result.get("message"))
            return 1
        log.info("opened %s", args[0])

    log.info("PerfStudio %s MCP server on %s", __version__, transport)
    mcp.run(transport=transport)  # type: ignore[arg-type]
    return 0


#: Re-exported so a client that wraps this module can catch bad-input errors by name.
__all__ = ["INSTRUCTIONS", "SessionError", "main", "mcp", "session"]
