"""Tests for the generated schematic (src/perfstudio/schematic.py).

THE THREE PROPERTIES THE MODULE IS BUILT ON, AND WHAT WOULD BREAK THEM.

- **No wire crosses a symbol.** Not "usually", and not as something a nudge factor keeps
  true: wires live in the channels between the column/row cells and symbols live in the
  cells, so the property holds by construction. ``test_no_wire_ever_crosses_a_symbol``
  measures it on every fixture in the repository, because the day the layout gains a
  special case for something is the day the construction argument stops being an argument.
- **The same document draws the same sheet.** The layout is a graph-drawing heuristic with
  BFS layering, barycentre sweeps and left-edge track packing in it, and every one of those
  has a tie to break. A tie broken by set iteration order would give a sheet that rearranged
  itself between runs -- and the two golden dumps below would be unblessable.
- **Polarity is read from the pin NAMES.** An LED has pin 1 as its anode and a diode has
  pin 1 as its cathode. A single convention keyed on pin 1 therefore draws one of the two
  backwards, which is a wrong sheet rather than an ugly one; ``guide._polarity_note`` makes
  the same distinction for the same reason and the two must not drift apart.

The two golden dumps (``tests/schematic_golden/``) exist for the reason
``test_guide_golden`` exists: everything else in this file asserts something somebody
thought to name, and a symbol that quietly moved two columns, a net that stopped being
drawn, a rail that turned back into a wire is exactly what nobody names. They are OUR
output -- the TypeScript engine never drew a schematic -- so they live here rather than in
``tools/diffcheck/golden/``. Re-bless with ``PERFSTUDIO_BLESS_SCHEMATIC=1`` AFTER READING
THE DIFF; coordinates are printed to two decimals, which is finer than anything a person
would call a change and coarse enough to absorb a last-ULP disagreement between platforms.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import get_args

import pytest

from perfstudio import persist, schematic_export
from perfstudio.footprints import footprint_lookup, standard_footprints
from perfstudio.model import (
    Board,
    BodyArchetype,
    BodySpec,
    ComponentInstance,
    DocumentMeta,
    Footprint,
    FootprintPin,
    HoleCoord,
    Net,
    NetNode,
    PerfDocument,
    Point2,
    SchematicPart,
)
from perfstudio.schematic import (
    _KIND_BY_ARCHETYPE,
    _SYMBOL_BUILDERS,
    LEAD_MM,
    NET_LABEL_ADVANCE,
    NET_LABEL_CLEARANCE_MM,
    NET_LABEL_MM,
    NO_CONNECT_MM,
    PIN_PITCH_MM,
    RAIL_GLYPH_DEPTH_MM,
    RAIL_GLYPH_MM,
    TRACK_PITCH_MM,
    Label,
    SchematicDrawing,
    SchematicOptions,
    Symbol,
    SymbolKind,
    _split_tall_layers,
    build_schematic,
    symbol_kind_for,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = ROOT / "tools" / "diffcheck" / "golden"
EXAMPLES_DIR = ROOT / "examples"
EXPECTED_DIR = Path(__file__).resolve().parent / "schematic_golden"
REGISTRY = footprint_lookup()

#: Every board in the repository, both the differential fixtures and the four worked
#: examples. The structural properties below are asserted against all of them rather than
#: against a chosen one, because "holds by construction" is a claim about all inputs.
ALL_BOARDS = sorted(GOLDEN_DIR.glob("*.perf")) + sorted(EXAMPLES_DIR.glob("*.perf"))

#: How many lines of a golden diff to print. Enough to see what moved.
DIFF_LINES = 40


def load(path: Path) -> PerfDocument:
    result = persist.deserialize_document(path.read_text(encoding="utf-8"))
    assert result.ok, result.message
    return result.document


def drawing_for(path: Path) -> SchematicDrawing:
    return build_schematic(load(path), REGISTRY)


def boxes(drawing: SchematicDrawing) -> list[tuple[float, float, float, float, str]]:
    return [
        (symbol.at.x, symbol.at.y, symbol.at.x + symbol.width, symbol.at.y + symbol.height, symbol.ref)
        for symbol in drawing.symbols
    ]


def segments(drawing: SchematicDrawing) -> list[tuple[Point2, Point2, str]]:
    """Every drawn run, wires and rail stubs alike, tagged with what it belongs to."""
    found: list[tuple[Point2, Point2, str]] = []
    for wire in drawing.wires:
        for start, end in zip(wire.path, wire.path[1:], strict=False):
            found.append((start, end, f"wire {wire.net_name}"))
    for rail in drawing.rails:
        for start, end in zip(rail.path, rail.path[1:], strict=False):
            found.append((start, end, f"rail {rail.net_name}"))
    return found


# ---------------------------------------------------------------------------
# The properties that hold by construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda path: path.stem)
def test_every_run_is_orthogonal(path: Path) -> None:
    """No diagonals anywhere. A schematic drawn with sloped wires is a schematic nobody
    can follow a net through, and the channel model has no way to produce one."""
    for start, end, what in segments(drawing_for(path)):
        assert abs(start.x - end.x) < 1e-9 or abs(start.y - end.y) < 1e-9, (
            f"{what} runs diagonally from {start} to {end}"
        )


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda path: path.stem)
def test_no_wire_ever_crosses_a_symbol(path: Path) -> None:
    """THE property. Touching a symbol at its own pin is fine and is why the test uses a
    strict overlap; passing THROUGH one is what must never happen."""
    drawing = drawing_for(path)
    for start, end, what in segments(drawing):
        low_x, high_x = min(start.x, end.x), max(start.x, end.x)
        low_y, high_y = min(start.y, end.y), max(start.y, end.y)
        for x0, y0, x1, y1, ref in boxes(drawing):
            crossing = high_x > x0 + 1e-9 and low_x < x1 - 1e-9 and high_y > y0 + 1e-9 and low_y < y1 - 1e-9
            assert not crossing, f"{what} passes through {ref}"


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda path: path.stem)
def test_no_two_symbols_share_space(path: Path) -> None:
    """One symbol per grid cell, and cells do not overlap -- so this can only fail if the
    cell assignment gave two parts the same (column, row)."""
    placed = boxes(drawing_for(path))
    for first in range(len(placed)):
        for second in range(first + 1, len(placed)):
            a, b = placed[first], placed[second]
            overlapping = (
                a[0] < b[2] - 1e-9 and b[0] < a[2] - 1e-9 and a[1] < b[3] - 1e-9 and b[1] < a[3] - 1e-9
            )
            assert not overlapping, f"{a[4]} and {b[4]} overlap"


def net_label_box(label: Label) -> tuple[float, float, float, float]:
    """The rectangle a net name occupies. ``Label.at`` IS its baseline, and it sits above.

    The same arithmetic ``schematic._label_box`` uses, written out again rather than
    imported: a test that asks the code under test where it put something can only ever
    agree with it.
    """
    width = len(label.text) * NET_LABEL_MM * NET_LABEL_ADVANCE
    return label.at.x, label.at.y - NET_LABEL_MM, label.at.x + width, label.at.y


def box_meets_segment(
    box: tuple[float, float, float, float], start: Point2, end: Point2
) -> bool:
    x0, y0, x1, y1 = box
    if max(start.x, end.x) < x0 or min(start.x, end.x) > x1:
        return False
    return not (max(start.y, end.y) < y0 or min(start.y, end.y) > y1)


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda path: path.stem)
def test_a_net_name_is_never_drawn_along_a_horizontal_run(path: Path) -> None:
    """THE property, and it holds by construction rather than by search.

    A net name used to be placed AT its own trunk -- ``Label.at`` was the wire's y and the
    wire's left end -- so the line ran through every descender and the branch dropping into
    that end ran up through the first letter. All 41 net names across the boards in this
    repository were drawn on a wire.

    The name now sits in a band above the run, and the band is what cannot overlap. It is
    shorter than ``TRACK_PITCH_MM``, so it cannot reach the lane above; its own trunk is
    below it by ``NET_LABEL_CLEARANCE_MM``. Between them those two facts mean NO horizontal
    run can be in it -- which is the reading that matters, because text and a horizontal
    wire lie along each other rather than crossing, and a line along a word is the one
    overlap that makes it unreadable. What is left is vertical branches, and the search in
    ``_net_label_at`` is about those.
    """
    drawing = drawing_for(path)
    for label in drawing.labels:
        if label.kind != "net":
            continue
        box = net_label_box(label)
        for start, end, what in segments(drawing):
            if abs(start.y - end.y) > 1e-9:
                continue
            assert not box_meets_segment(box, start, end), (
                f"{path.stem}: the name {label.text} is drawn along {what}"
            )


def test_a_net_name_is_almost_never_crossed_by_another_net() -> None:
    """Sliding along the trunk clears the vertical branches too, and the number is the point.

    ``_net_label_at`` starts at the left-hand end of the run -- where a reader looks for the
    name -- and only moves right, in half grid squares, if the band there is occupied.
    Across every board in the repository that takes 41 of 41 names off a wire down to
    three, and 29 of them do not move at all.

    Counted against OTHER nets only. A name crossed by a branch of the net it names is not
    ambiguous about anything: it is the same wire, and the reader loses nothing. Two of the
    41 are that, and chasing them would move names away from the end a reader looks at.

    A bound rather than zero, deliberately. The remaining three have branches crossing the
    band at every step along the whole trunk, and every alternative is worse: moving the
    name off its own run makes it ambiguous, and widening the channel rearranges a sheet
    that is otherwise fine. What the bound protects is the regression -- a change that puts
    the names back on the wires fails here loudly.
    """
    crossed = 0
    total = 0
    for path in ALL_BOARDS:
        drawing = drawing_for(path)
        for label in drawing.labels:
            if label.kind != "net":
                continue
            total += 1
            box = net_label_box(label)
            if any(
                box_meets_segment(box, start, end)
                for start, end, what in segments(drawing)
                if not what.endswith(f" {label.text}")
            ):
                crossed += 1
    assert total >= 40, "the boards stopped producing net names; the measurement is empty"
    assert crossed <= total * 0.1, (
        f"{crossed} of {total} net names are crossed by another net, over the 10% bound"
    )


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda path: path.stem)
def test_no_sheet_comes_out_taller_than_it_is_wide(path: Path) -> None:
    """A layer is a hint about distance, not a constraint, and it used to be treated as one.

    Everything one hop from the root went in one column. On `lm317-supply` ten of the
    eleven parts hang directly off U1, so the sheet came out 129 x 239 mm -- nearly twice
    as tall as it was wide, on a circuit that fits across a page with room to spare. There
    was nothing to respect in that arrangement: a schematic has no precedence order, so a
    part moved one column further out only makes its own wire span one more channel.

    `_split_tall_layers` caps a layer at about the side of a square and chunks the rest
    into columns of their own. It fires only where a layer was genuinely over-full: the
    fourteen random fixtures are untouched, and the five real circuits went from a worst
    aspect of 1.84 to 0.92 -- lm317 to 185 x 132, and the NE555 from 4 columns by 7 rows to
    5 by 4, which reads left to right the way a schematic should.

    The bound has headroom on purpose. What it is here to catch is the 1.84, and a sheet
    that is a little taller than square is a fair drawing of a circuit that is genuinely
    deep rather than wide.
    """
    drawing = drawing_for(path)
    if not drawing.symbols:
        return
    aspect = drawing.height / drawing.width
    assert aspect <= 1.1, (
        f"{path.stem} is {drawing.width:.0f} x {drawing.height:.0f} mm, "
        f"{aspect:.2f} times taller than wide"
    )


def test_a_layer_is_split_rather_than_drawn_as_one_tall_column() -> None:
    """The cap is what the test above rests on, measured directly rather than through a
    sheet's millimetres.

    A root with nine parts hanging off it is one BFS layer of nine. Nine in a column is the
    shape that made the LM317 sheet unreadable; `max(3, ceil(sqrt(10)))` is 4, so it comes
    out as three columns of at most four instead.
    """
    layers = [["U1"], [f"R{n}" for n in range(1, 10)]]
    split = _split_tall_layers(layers, 10)
    assert [len(column) for column in split] == [1, 4, 4, 1]
    assert [ref for column in split for ref in column] == layers[0] + layers[1], (
        "the reference order inside a layer is kept, so R1 stays beside R2"
    )


def test_a_layer_small_enough_to_read_is_left_alone() -> None:
    """The cap never fires below three, so a small circuit is not spread into a strip."""
    layers = [["U1"], ["R1", "R2", "R3"]]
    assert _split_tall_layers(layers, 4) == layers


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda path: path.stem)
def test_a_pin_is_marked_unconnected_exactly_when_no_net_reaches_it(path: Path) -> None:
    """A lead ending in space is ambiguous, and the cross is what resolves it.

    Without a marker an unwired pin is drawn as a plain lead stopping in mid-air -- which
    is also exactly what a pin whose wire the sheet failed to draw would look like, and a
    reader cannot tell those two apart. Ten of the eleven parts on `arduino-io-shield` have
    one; the LM317 and the booster have none, because every pin on those is in the netlist.

    Both directions here. A pin a net reaches must NEVER be crossed -- that would be the
    sheet contradicting its own wire -- and a pin no net reaches must always be, or the
    marker is decoration rather than a statement.
    """
    drawing = drawing_for(path)
    marked = {(mark.ref, mark.pin) for mark in drawing.no_connects}
    document = load(path)
    reachable = {(node.component_ref, node.pin) for net in document.nets for node in net.nodes}
    for symbol in drawing.symbols:
        for pin in symbol.pins:
            key = (symbol.ref, pin.number)
            if key in reachable:
                assert key not in marked, f"{key} is in a net and still crossed"
            else:
                assert key in marked, f"{key} is in no net and not crossed"


def test_the_cross_sits_on_the_pin_it_marks() -> None:
    """``NoConnect.at`` is the point a WIRE would attach to, not the body edge, so the mark
    lands where the connection is missing rather than beside it."""
    drawing = drawing_for(EXAMPLES_DIR / "arduino-io-shield.perf")
    assert drawing.no_connects, "this fixture is chosen for having unconnected pins"
    anchors = {
        (symbol.ref, pin.number): (symbol.at.x + pin.at.x, symbol.at.y + pin.at.y)
        for symbol in drawing.symbols
        for pin in symbol.pins
    }
    for mark in drawing.no_connects:
        assert (mark.at.x, mark.at.y) == anchors[(mark.ref, mark.pin)]


def test_an_unconnected_pin_is_not_reported_as_a_defect() -> None:
    """Unused pins on a header are the ordinary case, not a hole in the design.

    ``notes`` is for things that are wrong -- a pin the netlist names and the footprint
    does not have, a part nothing defines, a net with one node. A sheet that added ten
    notes for the ten open pins of a connector would bury the one note that matters, and
    LVS is where a connection that was SUPPOSED to exist gets reported.
    """
    drawing = drawing_for(EXAMPLES_DIR / "arduino-io-shield.perf")
    assert drawing.no_connects
    assert drawing.notes == ()


def test_the_cross_stays_inside_the_lead_it_marks() -> None:
    """Small enough not to touch the body or the pin next door.

    Pins down a DIP are ``PIN_PITCH_MM`` apart and the lead is ``LEAD_MM`` long, so a mark
    reaching half a pitch would meet its neighbour and one reaching the lead's length would
    meet the body. Both would read as part of the symbol rather than as a note about it.
    """
    assert 2 * NO_CONNECT_MM < PIN_PITCH_MM
    assert 2 * NO_CONNECT_MM < LEAD_MM


def test_a_net_name_cannot_reach_the_neighbouring_track() -> None:
    """The band a name occupies has to fit between two runs the allocator separated.

    Same rule as ``RAIL_GLYPH_MM``'s, and cheap for the same reason: two runs given
    different tracks are a whole ``TRACK_PITCH_MM`` apart, so anything shorter than a pitch
    cannot reach out of its own lane. This is what makes the test above a property and not
    a coincidence: a taller band would put names on horizontal wires that no amount of
    searching along the trunk could ever move them off.
    """
    assert NET_LABEL_CLEARANCE_MM + NET_LABEL_MM < TRACK_PITCH_MM


def test_the_exported_sheet_draws_a_net_name_at_the_size_the_layout_reserved() -> None:
    """One fact, two consumers. The layout keeps a box of ``NET_LABEL_MM`` clear above each
    run; a sheet that drew the name at some other size would be filling a gap that was
    measured for a different piece of text."""
    assert schematic_export.PAPER.net_mm == NET_LABEL_MM


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda path: path.stem)
def test_nothing_is_drawn_through_a_rail_glyph(path: Path) -> None:
    """A ground symbol is three horizontal bars, and a wire lying across one reads as part
    of it.

    Unlike an ordinary crossing -- which every schematic has, and which the absence of a
    junction dot already settles -- there is no convention that rescues a line drawn
    THROUGH the glyph. So rail anchors are allocated out of the same track pool the trunks
    come from, and the vertical stub is padded past its anchor by the glyph's own depth.
    This measures the box that leaves clear; the renderer draws inside it.
    """
    drawing = drawing_for(path)
    anchors = {(round(rail.at.x, 6), round(rail.at.y, 6)) for rail in drawing.rails}
    assert len(anchors) == len(drawing.rails), "two rail glyphs are drawn on top of each other"

    for rail in drawing.rails:
        deep = RAIL_GLYPH_DEPTH_MM if rail.direction == "down" else -RAIL_GLYPH_DEPTH_MM
        box = (
            rail.at.x - RAIL_GLYPH_MM,
            min(rail.at.y, rail.at.y + deep),
            rail.at.x + RAIL_GLYPH_MM,
            max(rail.at.y, rail.at.y + deep),
        )
        for other in [*drawing.wires, *drawing.rails]:
            if other is rail:
                continue  # its own stem runs into the glyph, which is the stem
            for start, end in zip(other.path, other.path[1:], strict=False):
                low_x, high_x = min(start.x, end.x), max(start.x, end.x)
                low_y, high_y = min(start.y, end.y), max(start.y, end.y)
                through = (
                    high_x > box[0] + 1e-9
                    and low_x < box[2] - 1e-9
                    and high_y > box[1] + 1e-9
                    and low_y < box[3] - 1e-9
                )
                assert not through, (
                    f"{other.net_name} is drawn through the {rail.net_name} glyph "
                    f"at {rail.at.x:.2f},{rail.at.y:.2f}"
                )


def test_the_glyph_can_never_reach_the_lane_beside_it() -> None:
    """The whole guarantee above rests on this, so it is asserted rather than assumed.

    Two runs the left-edge sweep separated are one track pitch apart. A glyph smaller than
    a pitch in both directions therefore cannot reach one; a glyph larger than a pitch
    would make the clearance depend on which lane happened to be free.
    """
    assert RAIL_GLYPH_MM < TRACK_PITCH_MM
    assert RAIL_GLYPH_DEPTH_MM < TRACK_PITCH_MM


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda path: path.stem)
def test_the_same_document_draws_the_same_sheet(path: Path) -> None:
    """Every tie in the layout is broken by reference or net id, never by iteration order.

    Equality over the whole dataclass, not over a summary: a heuristic that reordered two
    symbols of equal barycentre would keep the same counts and the same bounding box.
    """
    document = load(path)
    assert build_schematic(document, REGISTRY) == build_schematic(document, REGISTRY)


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda path: path.stem)
def test_everything_drawn_stays_on_the_sheet(path: Path) -> None:
    """``width``/``height`` are the sheet, so a rail that hung off the bottom of it would
    be clipped by any renderer that trusted them -- and both of ours do."""
    drawing = drawing_for(path)
    points = [point for start, end, _ in segments(drawing) for point in (start, end)]
    points += [rail.at for rail in drawing.rails]
    points += [symbol.at for symbol in drawing.symbols]
    points += [
        Point2(x=symbol.at.x + symbol.width, y=symbol.at.y + symbol.height)
        for symbol in drawing.symbols
    ]
    for point in points:
        assert -1e-9 <= point.x <= drawing.width + 1e-9
        assert -1e-9 <= point.y <= drawing.height + 1e-9


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda path: path.stem)
def test_every_pin_the_netlist_names_is_reached(path: Path) -> None:
    """A node that is resolvable and silently undrawn would be the worst possible failure
    here: the sheet would show a circuit that is missing a connection the board has."""
    document = load(path)
    drawing = build_schematic(document, REGISTRY)
    anchors: dict[tuple[str, str], Point2] = {}
    for symbol in drawing.symbols:
        for pin in symbol.pins:
            anchors[(symbol.ref, pin.number)] = Point2(
                x=symbol.at.x + pin.at.x, y=symbol.at.y + pin.at.y
            )
    touched = {
        (round(run.path[0].x, 6), round(run.path[0].y, 6))
        for run in [*drawing.wires, *drawing.rails]
    }
    for net in document.nets:
        for node in net.nodes:
            anchor = anchors.get((node.component_ref, node.pin))
            if anchor is None:
                continue  # reported in notes; test_a_pin_that_does_not_exist_is_reported
            assert (round(anchor.x, 6), round(anchor.y, 6)) in touched, (
                f"{net.name}: {node.component_ref} pin {node.pin} has no run leaving it"
            )


# ---------------------------------------------------------------------------
# Rails
# ---------------------------------------------------------------------------


def test_ground_and_power_become_rails_and_signals_do_not() -> None:
    drawing = drawing_for(EXAMPLES_DIR / "ne555-astable.perf")
    assert {rail.net_class for rail in drawing.rails} <= {"ground", "power"}
    assert all(wire.net_class == "signal" for wire in drawing.wires)
    assert drawing.rails, "the fixture has a GND net; something should have railed"


def test_turning_rails_off_puts_the_ground_net_back_on_the_sheet_as_wire() -> None:
    """The option is the escape hatch for a four-part circuit where the glyphs are more
    ceremony than the sheet needs, and it has to actually change the drawing."""
    document = load(EXAMPLES_DIR / "ne555-astable.perf")
    plain = build_schematic(document, REGISTRY, SchematicOptions(rail_classes=frozenset()))
    assert not plain.rails
    assert any(wire.net_class == "ground" for wire in plain.wires)


def test_a_ground_rail_points_down_and_a_power_rail_points_up() -> None:
    drawing = drawing_for(EXAMPLES_DIR / "ne555-astable.perf")
    for rail in drawing.rails:
        expected = "up" if rail.net_class == "power" else "down"
        assert rail.direction == expected
        if expected == "down":
            assert rail.at.y > rail.path[0].y
        else:
            assert rail.at.y < rail.path[0].y


def test_a_rail_never_makes_two_parts_neighbours() -> None:
    """Rails are kept out of the layering graph on purpose.

    A ground net touching every part would otherwise make every part adjacent to every
    other, collapse the breadth-first layering into two columns and produce the exact
    hairball the glyphs exist to prevent -- arriving by the back door.
    """
    board = Board(
        type="pad-per-hole",
        cols=40,
        rows=40,
        pitch=2.54,
        thickness=1.6,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=0.8,
    )
    parts = tuple(
        ComponentInstance(
            id=f"c{index}",
            ref=f"R{index}",
            value="1k",
            footprint_id="r-axial-3",
            anchor=HoleCoord(col=index * 4, row=0),
        )
        for index in range(1, 7)
    )
    everything_to_ground = Net(
        id="net-gnd",
        name="GND",
        net_class="ground",
        nodes=tuple(NetNode(component_ref=f"R{index}", pin="2") for index in range(1, 7)),
    )
    document = PerfDocument(
        meta=DocumentMeta(name="rails", created="", modified=""),
        board=board,
        components=parts,
        nets=(everything_to_ground,),
    )
    drawing = build_schematic(document, REGISTRY)
    # Six parts joined by nothing but ground: they get packed into a block, not strung out
    # in one column and not collapsed into one.
    columns = {round(symbol.at.x, 3) for symbol in drawing.symbols}
    rows = {round(symbol.at.y, 3) for symbol in drawing.symbols}
    assert len(columns) > 1 and len(rows) > 1
    assert len(drawing.rails) == 6
    assert not drawing.wires


# ---------------------------------------------------------------------------
# Symbols: what gets a shape, and which way round
# ---------------------------------------------------------------------------


def two_lead(footprint_id: str, *, polarized: bool, names: tuple[str | None, str | None]) -> Footprint:
    return Footprint(
        id=footprint_id,
        name=footprint_id,
        pins=(
            FootprintPin(number="1", d_col=0, d_row=0, name=names[0]),
            FootprintPin(number="2", d_col=2, d_row=0, name=names[1]),
        ),
        body_outline=(),
        body_height=2.0,
        body=BodySpec(archetype="axial-cylinder"),
        lead_diameter=0.6,
        polarized=polarized,
    )


def one_part_document(footprint_id: str) -> PerfDocument:
    board = Board(
        type="pad-per-hole",
        cols=20,
        rows=20,
        pitch=2.54,
        thickness=1.6,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=0.8,
    )
    return PerfDocument(
        meta=DocumentMeta(name="one", created="", modified=""),
        board=board,
        components=(
            ComponentInstance(
                id="c1", ref="D1", value="", footprint_id=footprint_id, anchor=HoleCoord(col=1, row=1)
            ),
        ),
    )


def only_symbol(document: PerfDocument, lookup_footprint: Footprint) -> Symbol:
    drawing = build_schematic(
        document, lambda fp_id: lookup_footprint if fp_id == lookup_footprint.id else None
    )
    assert len(drawing.symbols) == 1
    return drawing.symbols[0]


def test_an_unnamed_polarised_two_lead_part_draws_pin_1_as_the_cathode() -> None:
    """The DO-41 convention, and the same one ``guide._polarity_note`` states in words.

    Pin 1 ends up on the LEFT, which is where the diode symbol puts its bar.
    """
    footprint = two_lead("d-do41", polarized=True, names=(None, None))
    symbol = only_symbol(one_part_document("d-do41"), footprint)
    assert symbol.kind == "diode"
    left = next(pin for pin in symbol.pins if pin.side == "left")
    assert left.number == "1"


def test_an_led_draws_its_cathode_on_the_bar_even_though_pin_1_is_the_anode() -> None:
    """The case a pin-1 convention gets wrong, and the reason the names are read first.

    ``led-5mm`` names its pins ``A`` and ``K``; the K is pin 2, so pin 2 is what has to end
    up on the left where the bar is drawn. A rule that trusted pin 1 would light the LED
    backwards on the bench and look perfectly fine on the screen.
    """
    real = standard_footprints()["led-5mm"]
    assert [pin.name for pin in real.pins] == ["A", "K"]
    symbol = only_symbol(one_part_document("led-5mm"), real)
    assert symbol.kind == "led"
    left = next(pin for pin in symbol.pins if pin.side == "left")
    assert left.number == "2", "the cathode (K) belongs on the barred end"


def test_an_electrolytic_draws_its_positive_lead_against_the_straight_plate() -> None:
    real = standard_footprints()["c-elec-d5-p2"]
    assert [pin.name for pin in real.pins] == ["+", "-"]
    symbol = only_symbol(one_part_document("c-elec-d5-p2"), real)
    assert symbol.kind == "polarised-capacitor"
    left = next(pin for pin in symbol.pins if pin.side == "left")
    assert left.name == "+"


def test_a_named_cathode_on_pin_1_still_lands_on_the_left() -> None:
    """Names beat the convention in both directions, not just the interesting one."""
    footprint = two_lead("d-named", polarized=True, names=("K", "A"))
    symbol = only_symbol(one_part_document("d-named"), footprint)
    left = next(pin for pin in symbol.pins if pin.side == "left")
    assert left.number == "1"


def test_a_to92_is_a_box_and_that_is_the_decision_not_an_omission() -> None:
    """The registry records no E/B/C for a TO-92, so nothing here may draw one.

    This is a regression guard on a deliberate refusal: the transistor symbol is the
    obvious thing to add, and adding it means asserting a lead assignment that no part of
    this codebase holds. If the registry ever gains pin names, this test is the place the
    change gets noticed.
    """
    real = standard_footprints()["to92"]
    assert all(pin.name is None for pin in real.pins)
    assert symbol_kind_for(real, len(real.pins)) == "box"


def test_a_two_terminal_archetype_with_the_wrong_pin_count_falls_back_to_a_box() -> None:
    """A resistor shape has exactly two places to attach a wire. A third pin would have
    nowhere to go, and dropping it silently is the failure mode this guard exists for."""
    resistor = standard_footprints()["r-axial-3"]
    assert symbol_kind_for(resistor, 2) == "resistor"
    assert symbol_kind_for(resistor, 3) == "box"
    assert symbol_kind_for(None, 2) == "box"


def test_a_dip_numbers_down_the_left_and_up_the_right() -> None:
    """DIP order is not a style choice: it is how the package is numbered, so it is the
    only ordering that lets a pin read off this sheet be found on the part."""
    real = standard_footprints()["dip-8"]
    symbol = only_symbol(one_part_document("dip-8"), real)
    assert symbol.kind == "ic"
    left = [pin.number for pin in symbol.pins if pin.side == "left"]
    right = [pin.number for pin in symbol.pins if pin.side == "right"]
    assert left == ["1", "2", "3", "4"]
    assert right == ["8", "7", "6", "5"]
    top_right = min((pin for pin in symbol.pins if pin.side == "right"), key=lambda pin: pin.at.y)
    assert top_right.number == "8"


def test_a_header_keeps_all_its_pins_on_one_side() -> None:
    """An eight-way strip split across two sides of a box is drawn as two four-ways."""
    real = standard_footprints()["hdr-1x8"]
    symbol = only_symbol(one_part_document("hdr-1x8"), real)
    assert symbol.kind == "connector"
    assert {pin.side for pin in symbol.pins} == {"left"}


def test_a_potentiometer_puts_the_wiper_on_pin_2() -> None:
    """The one assumption in the module the registry does not back, pinned so that it is a
    decision on the record rather than something a reader has to infer from the geometry."""
    real = standard_footprints()["pot-3"]
    symbol = only_symbol(one_part_document("pot-3"), real)
    assert symbol.kind == "potentiometer"
    wiper = next(pin for pin in symbol.pins if pin.number == "2")
    ends = [pin for pin in symbol.pins if pin.number != "2"]
    assert wiper.at.y < min(pin.at.y for pin in ends), "the wiper enters above the body"


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------


def test_every_body_archetype_maps_to_a_symbol() -> None:
    """Adding a body to the registry must fail here rather than quietly drawing the new
    part as a box -- which is what a ``dict.get`` with a default would have done."""
    assert set(get_args(BodyArchetype)) == set(_KIND_BY_ARCHETYPE)


def test_every_symbol_kind_has_a_builder() -> None:
    assert set(get_args(SymbolKind)) == set(_SYMBOL_BUILDERS)


def test_every_symbol_kind_is_reachable_from_the_registry_or_deliberately_is_not() -> None:
    """Every kind either something in the 61 footprints asks for, or is the fallback.

    Catches a kind that was written, given a builder, and then never wired to an
    archetype -- code that looks tested because the builder has a test and is dead.
    """
    reachable = {symbol_kind_for(fp, len(fp.pins)) for fp in standard_footprints().values()}
    unreachable = set(get_args(SymbolKind)) - reachable
    assert unreachable == set(), f"no footprint in the registry ever draws {sorted(unreachable)}"


# ---------------------------------------------------------------------------
# What cannot be drawn is said, not dropped
# ---------------------------------------------------------------------------


def test_a_part_nothing_defines_is_drawn_and_reported() -> None:
    board = Board(
        type="pad-per-hole",
        cols=20,
        rows=20,
        pitch=2.54,
        thickness=1.6,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=0.8,
    )
    document = PerfDocument(
        meta=DocumentMeta(name="missing", created="", modified=""),
        board=board,
        nets=(
            Net(
                id="net-1",
                name="OUT",
                nodes=(NetNode(component_ref="U9", pin="1"), NetNode(component_ref="R4", pin="2")),
            ),
        ),
    )
    drawing = build_schematic(document, REGISTRY)
    assert {symbol.ref for symbol in drawing.symbols} == {"U9", "R4"}
    assert all(symbol.unplaced and symbol.undefined for symbol in drawing.symbols)
    assert any("neither on the board nor in the design" in note for note in drawing.notes)


def test_a_part_in_the_design_is_drawn_as_itself_and_is_not_a_complaint() -> None:
    """The schematic-first case, and the state every circuit is in while it is drawn.

    A part in ``doc.parts`` has a footprint, so it gets its own symbol and its own pins
    rather than a two-pin box built out of whatever the netlist mentioned. And it produces
    NO note: on a sheet being captured every part is unplaced, and a note apiece would
    bury the ones that mean something.
    """
    board = Board(
        type="pad-per-hole",
        cols=20,
        rows=20,
        pitch=2.54,
        thickness=1.6,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=0.8,
    )
    document = PerfDocument(
        meta=DocumentMeta(name="design", created="", modified=""),
        board=board,
        parts=(
            SchematicPart(id="part-1", ref="C4", value="100nF", footprint_id="c-disc-p2"),
            SchematicPart(id="part-2", ref="U2", value="NE555", footprint_id="dip-8"),
        ),
        nets=(
            Net(
                id="net-1",
                name="TRIG",
                nodes=(NetNode(component_ref="C4", pin="1"), NetNode(component_ref="U2", pin="2")),
            ),
        ),
    )
    drawing = build_schematic(document, REGISTRY)
    by_ref = {symbol.ref: symbol for symbol in drawing.symbols}

    assert by_ref["C4"].kind == "capacitor"
    assert by_ref["C4"].value == "100nF"
    assert by_ref["U2"].kind == "ic"
    # Eight, from the footprint -- not the one pin the net happens to name.
    assert len(by_ref["U2"].pins) == 8
    assert all(symbol.unplaced and not symbol.undefined for symbol in drawing.symbols)
    assert drawing.notes == ()
    assert drawing.wires, "the two parts are wired to each other"


def test_placing_a_part_changes_nothing_about_the_circuit_it_draws() -> None:
    """The sheet is a picture of the DESIGN, so moving a part onto the board must not
    change which symbol it is, what it is called or what it is wired to -- only the flag
    that says where it lives."""
    board = Board(
        type="pad-per-hole",
        cols=20,
        rows=20,
        pitch=2.54,
        thickness=1.6,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=0.8,
    )
    part = SchematicPart(id="part-1", ref="R5", value="220", footprint_id="r-axial-3")
    design = PerfDocument(
        meta=DocumentMeta(name="either way", created="", modified=""),
        board=board,
        parts=(part,),
    )
    laid_out = PerfDocument(
        meta=design.meta,
        board=board,
        components=(
            ComponentInstance(
                id=part.id,
                ref=part.ref,
                value=part.value,
                footprint_id=part.footprint_id,
                anchor=HoleCoord(col=3, row=3),
            ),
        ),
    )
    before = build_schematic(design, REGISTRY).symbols[0]
    after = build_schematic(laid_out, REGISTRY).symbols[0]

    assert (before.ref, before.value, before.kind) == (after.ref, after.value, after.kind)
    assert before.pins == after.pins
    assert before.unplaced and not after.unplaced


def test_a_pin_that_the_footprint_does_not_have_is_reported() -> None:
    board = Board(
        type="pad-per-hole",
        cols=20,
        rows=20,
        pitch=2.54,
        thickness=1.6,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=0.8,
    )
    document = PerfDocument(
        meta=DocumentMeta(name="extra", created="", modified=""),
        board=board,
        components=(
            ComponentInstance(
                id="c1", ref="R1", value="", footprint_id="r-axial-3", anchor=HoleCoord(col=1, row=1)
            ),
        ),
        nets=(
            Net(id="net-1", name="OUT", nodes=(NetNode(component_ref="R1", pin="7"),)),
        ),
    )
    drawing = build_schematic(document, REGISTRY)
    assert any("does not have" in note for note in drawing.notes)
    assert any("is not on the sheet" in note for note in drawing.notes)


def test_an_empty_document_draws_nothing_and_says_so() -> None:
    board = Board(
        type="pad-per-hole",
        cols=10,
        rows=10,
        pitch=2.54,
        thickness=1.6,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=0.8,
    )
    drawing = build_schematic(
        PerfDocument(meta=DocumentMeta(name="", created="", modified=""), board=board), REGISTRY
    )
    assert drawing.symbols == ()
    assert drawing.notes and "Nothing to draw" in drawing.notes[0]


def test_a_junction_only_appears_in_the_middle_of_a_trunk() -> None:
    """A dot at the end of a trunk would claim a join where the wire only turns a corner."""
    drawing = drawing_for(EXAMPLES_DIR / "ne555-astable.perf")
    trunks = {
        wire.net_id: wire
        for wire in drawing.wires
        if len(wire.path) == 2 and abs(wire.path[0].y - wire.path[1].y) < 1e-9
        and abs(wire.path[0].x - wire.path[1].x) > 1e-9
    }
    for junction in drawing.junctions:
        trunk = trunks[junction.net_id]
        low, high = sorted((trunk.path[0].x, trunk.path[1].x))
        assert abs(junction.at.y - trunk.path[0].y) < 1e-9
        assert low < junction.at.x < high


# ---------------------------------------------------------------------------
# The whole sheet, frozen
# ---------------------------------------------------------------------------

#: Two boards, chosen to cover between them nine of the ten symbol kinds and all three net
#: classes: the NE555 brings the DIP and a power rail, the LM317 supply brings the diode,
#: the electrolytic, the potentiometer and a part drawn as a box.
FROZEN = ("ne555-astable", "lm317-supply")


def dump(drawing: SchematicDrawing) -> str:
    """The sheet as readable text: what is on it, where, in the order it was built.

    Two decimals is finer than a change anybody would make on purpose and coarse enough
    that a platform whose libm rounds the last bit differently still agrees.
    """
    lines = [f"sheet {drawing.width:.2f} x {drawing.height:.2f}"]
    for symbol in drawing.symbols:
        flag = " UNDEFINED" if symbol.undefined else (" UNPLACED" if symbol.unplaced else "")
        lines.append(
            f"symbol {symbol.ref} {symbol.kind} {symbol.footprint_id} "
            f"at {symbol.at.x:.2f},{symbol.at.y:.2f} "
            f"size {symbol.width:.2f}x{symbol.height:.2f} value={symbol.value!r}{flag}"
        )
        for pin in symbol.pins:
            lines.append(
                f"    pin {pin.number} name={pin.name!r} {pin.side} "
                f"{pin.at.x:.2f},{pin.at.y:.2f}"
            )
    for wire in drawing.wires:
        path = " -> ".join(f"{point.x:.2f},{point.y:.2f}" for point in wire.path)
        lines.append(f"wire {wire.net_name} [{wire.net_class}] {path}")
    for rail in drawing.rails:
        path = " -> ".join(f"{point.x:.2f},{point.y:.2f}" for point in rail.path)
        lines.append(f"rail {rail.net_name} [{rail.net_class}] {rail.direction} {path}")
    for junction in drawing.junctions:
        lines.append(f"junction {junction.net_id} {junction.at.x:.2f},{junction.at.y:.2f}")
    for mark in drawing.no_connects:
        lines.append(f"no-connect {mark.ref}.{mark.pin} {mark.at.x:.2f},{mark.at.y:.2f}")
    for label in drawing.labels:
        lines.append(
            f"label [{label.kind}/{label.anchor}] {label.at.x:.2f},{label.at.y:.2f} {label.text}"
        )
    for note in drawing.notes:
        lines.append(f"note {note}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("stem", FROZEN)
def test_the_sheet_is_the_sheet_that_was_blessed(stem: str) -> None:
    produced = dump(drawing_for(EXAMPLES_DIR / f"{stem}.perf"))
    expected_path = EXPECTED_DIR / f"{stem}.txt"

    if os.environ.get("PERFSTUDIO_BLESS_SCHEMATIC"):
        EXPECTED_DIR.mkdir(exist_ok=True)
        # An explicit LF, the way `test_guide_golden` already writes its own. The
        # repository is LF everywhere, and a bless run on Windows would otherwise put
        # CRLF into a file every other platform leaves alone.
        expected_path.write_text(produced, encoding="utf-8", newline="\n")
        pytest.skip(f"blessed {expected_path.name}")

    assert expected_path.exists(), (
        f"{expected_path} is missing. Run with PERFSTUDIO_BLESS_SCHEMATIC=1 to create it."
    )
    expected = expected_path.read_text(encoding="utf-8")
    if produced != expected:
        diff = list(
            difflib.unified_diff(
                expected.splitlines(), produced.splitlines(), "blessed", "produced", lineterm=""
            )
        )
        shown = "\n".join(diff[:DIFF_LINES])
        more = f"\n... and {len(diff) - DIFF_LINES} more lines" if len(diff) > DIFF_LINES else ""
        pytest.fail(f"{expected_path.name} changed:\n{shown}{more}")


def test_the_frozen_boards_still_cover_what_they_were_chosen_for() -> None:
    """The two dumps were picked to exercise nine kinds between them. A footprint change
    that quietly reduced that would leave the goldens passing and covering less."""
    kinds: set[SymbolKind] = set()
    classes: set[str] = set()
    for stem in FROZEN:
        drawing = drawing_for(EXAMPLES_DIR / f"{stem}.perf")
        kinds |= {symbol.kind for symbol in drawing.symbols}
        classes |= {rail.net_class for rail in drawing.rails}
        classes |= {wire.net_class for wire in drawing.wires}
    assert len(kinds) >= 9, sorted(kinds)
    assert classes == {"ground", "power", "signal"}
