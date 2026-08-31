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

THIRTY-NINE TOOLS, against PLAN.md Sec 2's "~25, deliberately narrow", and the overage is
stated rather than hidden. Each tool is a verb an agent cannot compose from the others,
and the surface was trimmed rather than grown: the history listing folded into
``get_status``, and "solder bridge" is not a separate tool from ``add_solder_trace``
because a bridge is a two-pad trace and one concept should not have two names.
``get_board_info`` stayed separate from ``get_status`` for a reason worth knowing --
status runs DRC, LVS and the ratsnest, and an agent that only wants the pitch should not
pay for that.

The most recent two were argued the same way. ``check_heights`` is named in PLAN.md
Sec 9.2 and answers a question no other tool can: how tall the build stands, which is
what decides the enclosure, and which is invisible in both render tools because a part
too tall looks exactly like one that is not. ``set_height_limit`` exists because without
it that limit can only be typed into the GUI, leaving one DRC rule permanently silent
for an agent -- and folding the limit into ``check_heights`` as an argument would make a
read tool mutate the document, off the command bus and outside the undo stack.

THE MOST RECENT FIVE are the netlist group -- ``create_net``, ``connect_pins``,
``disconnect_pins``, ``update_net``, ``delete_net`` -- and they are the largest single
addition this file has taken, so the case for them is worth stating. Until they existed
``import_netlist`` was the only way a net could enter a document, which meant an agent
could place parts, draw copper and route, but could not produce the INTENT all three are
measured against: on a board that never had a KiCad schematic there was no ratsnest, so
nothing for ``autoroute`` to route and nothing for ``run_lvs`` to check. None of the five
composes from the others -- declaring a net, adding a pin, removing one, renaming and
forgetting are five different verbs -- and ``update_net`` carries the only route in the
whole API to ``current_a`` and ``voltage_v``, which two DRC rules and the guide's wire
gauge are silent without.

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
    "dip", "resistor", "electrolytic", or leave it empty for all of them.

    A part that is NOT in this list can still be used: ask for one by its
    measurements instead of its name. Placing an id nothing recognises returns the
    grammar for that, which is where it is spelled out rather than here — it is a
    page long and only matters once."""
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
def render_schematic(px_per_mm: int = 8) -> Image:
    """A picture of the CIRCUIT: symbols, wires, junctions, ground and power glyphs.

    What the board is a way of BUILDING, drawn from the nets themselves. Use it after
    wiring to check that what you connected is the circuit you meant — the board renders
    show copper, which is what the circuit was turned into, not what it is.

    The sheet is generated from the document every time; nothing about it is stored, so
    there is no layout to keep in step and no way for it to disagree with the netlist.
    """
    png, meta = session.render_schematic(px_per_mm=px_per_mm)
    log.info(
        "rendered schematic: %s part(s), %s rail(s), %sx%s",
        meta["parts"], meta["rails"], meta["width_px"], meta["height_px"],
    )
    for note in meta["notes"]:
        log.info("schematic note: %s", note)
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


# ---------------------------------------------------------------------------
# The board itself
#
# An agent could place parts on a board and could not change the board: the only route
# to a different size, a different material or a stripboard was new_document, which
# throws the work away. Mounting holes and edge connectors could be READ back and not
# added, which reads as a broken tool rather than a missing one.
# ---------------------------------------------------------------------------


@mcp.tool()
def set_board(
    cols: int | None = None,
    rows: int | None = None,
    material: str | None = None,
    board_type: str | None = None,
    strip_axis: str | None = None,
    pitch: float | None = None,
    single_sided: bool | None = None,
) -> dict[str, Any]:
    """Change the board under the design, keeping everything on it. Anything left out is
    left alone. board_type is "pad-per-hole" (every hole its own island) or "stripboard"
    (whole rows already joined — you cut the track to separate them, and strip_axis says
    which way they run). Shrinking a board that still has a part hanging off the new edge
    is refused, naming the part."""
    return session.set_board(cols, rows, material, board_type, strip_axis, pitch, single_sided)


@mcp.tool()
def add_mounting_hole(
    hole: str, diameter: float = 3.2, head_diameter: float = 6.0
) -> dict[str, Any]:
    """Drill a screw hole at a hole address. It removes the copper from the pads it lands
    on — including the orthogonal neighbours of a bore this size — and a pin left standing
    on one is a DRC error, because there is nothing there to solder to."""
    return session.add_mounting_hole(hole, diameter, head_diameter)


@mcp.tool()
def add_edge_connector(
    edge: str, start: int, count: int, finger_width: float = 2.0, face: str = "bottom"
) -> dict[str, Any]:
    """Replace a run of grid pads along one edge ("top", "bottom", "left", "right") with
    connector fingers, starting at index `start` and `count` wide. A finger IS the pad
    there, rather than something over it."""
    return session.add_edge_connector(edge, start, count, finger_width, face)


@mcp.tool()
def cut_track(hole: str) -> dict[str, Any]:
    """Break a stripboard track at a hole. The cut is drilled through the pad, so that
    hole has nothing to solder to afterwards — which is what separates the two nets that
    were sharing the strip. Refused on a board that has no tracks."""
    return session.cut_track(hole)


@mcp.tool()
def remove_board_feature(id: str) -> dict[str, Any]:
    """Take back a mounting hole, an edge connector or a track cut, by the id
    get_board_info gave you."""
    return session.remove_board_feature(id)


@mcp.tool()
def import_netlist(path: str) -> dict[str, Any]:
    """Import a KiCad netlist — one way the circuit's intent gets onto the board, and what
    makes LVS and the guide's continuity checks possible. REPLACES the whole netlist;
    create_net builds one up instead. Reports any components the netlist names that the
    board does not have yet."""
    return session.import_netlist(path)


# ---------------------------------------------------------------------------
# The design, before the board
#
# The order every other EDA tool works in, and the one an agent could not follow here:
# every route a part had into a document ended in place_component, which needs a hole. So
# "design a 555 astable" meant inventing a layout before the circuit was finished.
# ---------------------------------------------------------------------------


@mcp.tool()
def add_part(ref: str, footprint_id: str, value: str = "") -> dict[str, Any]:
    """Put a part in the DESIGN without saying where on the board it goes. Draw the whole
    circuit this way, wire it with create_net / connect_pins, then place_parts and
    optimize_placement. The footprint is asked for now because everything else derives
    from it: the schematic symbol, the pins, the 3D body and the bill of materials."""
    return session.add_part(ref, footprint_id, value)


@mcp.tool()
def update_part(
    ref: str,
    new_ref: str | None = None,
    value: str | None = None,
    footprint_id: str | None = None,
) -> dict[str, Any]:
    """Rename a part in the design or change what it is; omitted fields are left alone. A
    rename CARRIES ITS WIRING, because a reference is the only name a net has for a part —
    and is refused if that would put one pin on two nets."""
    return session.update_part(ref, new_ref, value, footprint_id)


@mcp.tool()
def delete_part(ref: str) -> dict[str, Any]:
    """Take a part out of the design ALONG WITH its connections. Different from
    delete_component, which takes a part off the board and leaves the schematic still
    asking for it (an LVS open)."""
    return session.delete_part(ref)


@mcp.tool()
def list_parts() -> dict[str, Any]:
    """Every part in the design that is not on the board yet, with its pins."""
    return session.list_parts()


@mcp.tool()
def place_parts(placements: list[dict[str, Any]]) -> dict[str, Any]:
    """Move parts from the design onto the board, all as ONE undo step. Each placement is
    {"ref": "R1", "at": "C7", "rotation": 90}; rotation is optional. Laying out a drawn
    circuit is one decision, and one command per part means one undo press per part, each
    leaving a board half laid out. optimize_placement arranges them afterwards."""
    return session.place_parts(placements)


@mcp.tool()
def unplace_component(ref: str) -> dict[str, Any]:
    """Take a part off the board and keep it in the design, wiring intact. What to reach
    for instead of delete_component when the part belongs in the circuit but not in that
    hole."""
    return session.unplace_component(ref)


# ---------------------------------------------------------------------------
# The netlist, without KiCad
#
# The intent everything else is measured against. Without a net there is no ratsnest,
# and so nothing for autoroute to route or for LVS to check — which used to mean a board
# that never had a schematic could not be routed at all.
# ---------------------------------------------------------------------------


@mcp.tool()
def create_net(
    name: str,
    net_class: str = "signal",
    pins: list[str] | None = None,
    current_a: float | None = None,
    voltage_v: float | None = None,
) -> dict[str, Any]:
    """Declare a net: a name, a class ("signal", "ground" or "power"), and optionally the
    pins on it as "U1.8" addresses. Ground and power are routed first and get a rail, so
    the class is a routing decision rather than a label. current_a and voltage_v are what
    wake DRC's capacity and creepage rules — no netlist format carries them."""
    return session.create_net(name, net_class, pins, current_a, voltage_v)


@mcp.tool()
def connect_pins(net: str, pins: list[str]) -> dict[str, Any]:
    """Put pins on a net, as one undo step. Pins are "U1.8" addresses. A pin belongs to
    exactly one net, so one already on another net is refused rather than moved."""
    return session.connect_pins(net, pins)


@mcp.tool()
def disconnect_pins(net: str, pins: list[str]) -> dict[str, Any]:
    """Take pins off a net, as one undo step."""
    return session.disconnect_pins(net, pins)


@mcp.tool()
def update_net(
    name: str,
    new_name: str | None = None,
    net_class: str | None = None,
    current_a: float | None = None,
    voltage_v: float | None = None,
    clear_current: bool = False,
    clear_voltage: bool = False,
) -> dict[str, Any]:
    """Rename a net, reclassify it, or state the current and voltage it carries. Anything
    left out is left alone — passing null never erases a value; clear_current and
    clear_voltage do that explicitly."""
    return session.update_net(
        name, new_name, net_class, current_a, voltage_v, clear_current, clear_voltage
    )


@mcp.tool()
def delete_net(name: str) -> dict[str, Any]:
    """Forget a net. Copper already laid for it stays on the board and stops being
    anything reroute or remove_stale_conductors will touch."""
    return session.delete_net(name)


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
      "best"       route with all four, measure what each would cost to BUILD, keep the
                   best. Costs roughly two ordinary routes. Prefer this when the user has
                   not stated a preference -- it answers by measuring instead of guessing.
    On the NE555 fixture "balanced" gives 4 traces and 10 wires; "solder" gives 17 traces
    and 6 wires but 27 holes at bridging risk.

    Under "best" the result carries `comparison`: every style's traces, wires, wire length,
    risk holes and effort score, cheapest first. Report that trade rather than only the
    winner -- fewer wires against more bridging risk is the user's call to overrule.

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


@mcp.tool()
def check_heights() -> dict[str, Any]:
    """How tall the finished board stands, part by part, tallest first — and which parts
    are over the declared height limit, if one is set. The question a 2D view cannot
    answer and the one that decides which enclosure the board fits."""
    return session.check_heights()


@mcp.tool()
def set_height_limit(height_limit_mm: float | None = None) -> dict[str, Any]:
    """Declare the clear height available above the component side, in mm — the inside
    of the case the board has to fit. DRC then reports any part taller than this. Pass
    nothing (or null) to remove the limit."""
    return session.set_height_limit(height_limit_mm)


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
