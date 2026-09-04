"""The netlist, drawn: a schematic generated from the document, never stored in it.

The board answers "where does this go"; nothing in this application has ever answered
"what am I building". LVS says ``net VOUT is open`` and the ratsnest draws a line across
the copper, and both of those are statements about a net the user has no way to LOOK at.
This module is that view: it takes ``doc.nets`` -- the schematic's intent, the thing every
other module derives from -- and produces a drawing of it.

**It is generated, not stored, and that is the decision footprints already made.** A
``.perf`` file carries no symbol positions and neither does a KiCad netlist, so there is
nothing to read; and adding symbol coordinates to the document would reopen the
byte-for-byte format (``test_persist.py::test_golden_round_trip_byte_identical``) for
something no user edits. Same document in, same drawing out -- pure, no clock, no RNG, no
filesystem, ties broken by reference and net id -- which is also what lets the layout be
compared against a golden file rather than looked at.

**THE CIRCUIT IS EDITABLE AND THE SHEET IS NOT, and PLAN.md D3 is why.** The design
itself — which parts exist (``doc.parts``), what they are, what is wired to what
(``doc.nets``) — is edited through the command bus like everything else, and the panel
over this module is where a circuit gets drawn before a board is laid out. What cannot be
edited is the DRAWING: nothing here moves a symbol, chooses a corner for a wire or
remembers a sheet position, because that would be state, state would be a document field,
and a document field would be the geometric schematic editor D3 declined to write — a
year of work whose output this tool already accepts from KiCad. Every sheet is derived
afresh, so there is nothing to keep in step with the netlist and nothing to lay out by
hand.

THREE DECISIONS CARRY THE LEGIBILITY, AND EACH IS THE ONE THAT KEEPS IT FROM BEING A
HAIRBALL.

- **Ground and power become rail glyphs, not wires** (``SchematicOptions.rail_classes``).
  A GND net touching eleven pins drawn as wires is eleven lines crossing everything on the
  sheet; every schematic ever drawn hangs a ground symbol off the pin instead. The classes
  are already in the document -- ``Net.net_class``, which ``parsers.kicad.infer_net_class``
  fills in on import -- so this costs nothing and is the single largest difference between
  a readable sheet and an unreadable one.
- **A symbol is drawn only where the registry knows what every lead IS.** A resistor, a
  capacitor, a diode, an LED and a crystal get their real shapes, and polarity comes from
  the registry's own pin names (a plus, a ``K``, an ``A``) with pin 1 as the cathode for a
  polarised part that has none -- exactly the rule ``guide._polarity_note`` follows, and
  for the same reason: an LED's pin 1 is its anode and a diode's is its cathode, so a
  convention keyed on pin 1 alone draws one of the two backwards. Where the registry does
  not know -- a TO-92 has no E/B/C in it, a tactile switch no pole -- the part is a
  labelled box with numbered pins. Drawing a transistor symbol would assert a lead
  assignment nothing in this codebase holds.
- **Symbols sit in a column/row grid and wires run only in the channels between them**, so
  no wire can cross a symbol -- not as a tuning parameter, as a consequence of where the
  tracks are allowed to be. Verticals live in the channel between two columns, horizontal
  trunks in the channel between two rows, and each channel widens to fit the tracks
  assigned to it by a left-edge sweep. Wires still cross each other, which is what junction
  dots are for; that is a schematic, not a defect.

Everything is millimetres on a 2.54 mm grid, which is the grid schematics are drawn on and
the same scene unit ``ui/view2d.py`` already uses, so the renderer needs no scale factor.
Coordinates grow right and down, matching the rest of the application.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict, deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .connectivity import FootprintLookup
from .model import (
    BodyArchetype,
    Footprint,
    Mm,
    Net,
    NetClass,
    NetId,
    PerfDocument,
    Point2,
)

# ---------------------------------------------------------------------------
# The sheet's units
# ---------------------------------------------------------------------------

#: 0.1 inch. Schematics are drawn on this grid, KiCad's default is this grid, and every
#: coordinate this module emits is a multiple of it or a half of one.
GRID_MM: Mm = 2.54

#: One routing track. A channel carrying n of them is n of these wide, plus a margin.
TRACK_PITCH_MM: Mm = GRID_MM

#: The narrowest a channel gets, whatever it has to carry. Wide enough for a rail glyph to
#: drop into and for two symbols not to touch.
MIN_CHANNEL_MM: Mm = 4 * GRID_MM

#: The room a rail glyph occupies at ``Rail.at``: half-width across the stub, and depth
#: along it past the anchor.
#:
#: ONE FACT, TWO CONSUMERS -- the shape this codebase uses for ``heat-proximity`` and for
#: ``stripboard.MIN_SEPARABLE_GAP``. The renderer draws the bars inside this box and the
#: layout keeps every other run out of it, so a renderer that drew a wider glyph would be
#: putting bars through wires the layout believed it had cleared.
#: ``test_nothing_is_drawn_through_a_rail_glyph`` measures the layout half.
#:
#: BOTH MUST STAY UNDER ``TRACK_PITCH_MM``, and that is what makes the guarantee cheap
#: rather than another allocation pass: two runs that were given different tracks are a
#: whole pitch apart, so a glyph smaller than a pitch cannot reach the neighbouring lane in
#: either direction. Everything on the SAME track already has a disjoint interval.
RAIL_GLYPH_MM: Mm = 0.9 * GRID_MM
RAIL_GLYPH_DEPTH_MM: Mm = 0.9 * GRID_MM

#: Blank border around the whole sheet.
MARGIN_MM: Mm = 4 * GRID_MM

#: Lead length between a symbol's body and the point a wire attaches to.
LEAD_MM: Mm = 2 * GRID_MM

#: Pin-to-pin spacing down the side of a multi-pin body.
PIN_PITCH_MM: Mm = 2 * GRID_MM

#: The height a net name is drawn at, in millimetres of sheet, and the average advance of
#: one character as a fraction of it.
#:
#: ONE FACT, TWO CONSUMERS again. The LAYOUT has to know how much room a net name takes in
#: order to keep a wire out of it, and only a renderer knows the real size -- so the
#: exported sheet takes its size from here (``SheetInk.net_mm``) rather than naming its
#: own, and the panel, which draws text at a fixed PIXEL size on purpose, treats this as
#: the nominal it was always laid out against. The advance is an average over a
#: sans-serif's alphabet; it only has to be close, because the search below steps in half
#: grid squares and a net name is six characters.
NET_LABEL_MM: Mm = 1.3
NET_LABEL_ADVANCE: float = 0.55

#: How far a net name sits clear of the run it names, and how far past a branch it starts.
#:
#: The sheet used to place a net label AT its trunk, left-anchored: the baseline WAS the
#: wire's y and the x WAS the leftmost branch, so the line ran through every descender and
#: the branch ran up through the first letter. All 41 net labels across the fixtures in
#: this repository landed on a wire, which is 100% of them.
#:
#: ``NET_LABEL_CLEARANCE_MM + NET_LABEL_MM`` MUST STAY UNDER ``TRACK_PITCH_MM``, for the
#: reason ``RAIL_GLYPH_MM`` must: a label that reached into the neighbouring lane would be
#: sitting on a run the track allocator believed it had separated, and no amount of
#: searching along the trunk can move it out of a band that is too tall to fit.
#: ``test_a_net_label_cannot_reach_the_neighbouring_track`` is the measurement.
NET_LABEL_CLEARANCE_MM: Mm = 0.35 * GRID_MM
NET_LABEL_INSET_MM: Mm = 0.3 * GRID_MM

#: Half the diagonal of the cross drawn on a pin no net reaches. Small enough that a row
#: of them down an unused header reads as a row of marks rather than as hatching, and it
#: stays inside the lead so it cannot touch the body or a neighbouring pin.
NO_CONNECT_MM: Mm = 0.3 * GRID_MM


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

#: Spelled with ``TypeAlias`` rather than PEP 695's ``type``, and it has to be: this name
#: is READ AT RUN TIME by ``get_args`` in ``test_schematic.py``, which asserts every kind
#: has a builder. A PEP 695 alias returns an empty tuple there, and the test would then
#: assert that an empty set equals an empty set -- the trap ``model.py`` documents five
#: times over.
SymbolKind: TypeAlias = Literal[  # noqa: UP040
    "resistor",
    "capacitor",
    "polarised-capacitor",
    "diode",
    "led",
    "crystal",
    "potentiometer",
    "ic",
    "connector",
    "box",
]


@dataclass(frozen=True, slots=True)
class SymbolShape:
    """One primitive of a symbol body, in symbol-local millimetres.

    Three kinds and no more, because a schematic symbol is lines, closed shapes and
    circles -- and a renderer that has to handle a fourth is a renderer with an untested
    branch in it.
    """

    kind: Literal["polyline", "polygon", "circle"]
    #: ``polyline``/``polygon``: the vertices. ``circle``: one point, the centre.
    points: tuple[Point2, ...]
    #: ``circle`` only.
    radius: Mm = 0.0
    filled: bool = False


@dataclass(frozen=True, slots=True)
class SymbolPin:
    """One lead of a symbol. ``at`` is where a WIRE attaches, not where the body ends."""

    number: str
    name: str | None
    at: Point2
    side: Literal["left", "right"]


@dataclass(frozen=True, slots=True)
class Symbol:
    """One part on the sheet.

    ``at`` is the top-left of the symbol's own coordinate system in sheet millimetres, and
    every ``SymbolShape`` and ``SymbolPin`` inside it is relative to that -- so a renderer
    translates once and draws.
    """

    ref: str
    value: str
    kind: SymbolKind
    footprint_id: str | None
    at: Point2
    shapes: tuple[SymbolShape, ...]
    pins: tuple[SymbolPin, ...]
    width: Mm
    height: Mm
    #: In the design, not yet on the board. The ordinary state of every part on a
    #: schematic that has not been laid out, so it is counted rather than complained
    #: about -- see ``SchematicDrawing.notes``, which deliberately says nothing about it.
    unplaced: bool = False
    #: NOTHING defines this part -- neither the board nor the design. A net names it, so
    #: it is drawn, but its pins are whatever the netlist happened to mention and its
    #: footprint is a guess. That is a real hole in the design, unlike ``unplaced``, and
    #: it is what the dashed outline and the note are for.
    undefined: bool = False


@dataclass(frozen=True, slots=True)
class Wire:
    """One orthogonal run of a signal net, in sheet millimetres.

    A net becomes one trunk plus one of these per pin rather than a single path, so every
    segment is independently checkable for orthogonality and a renderer can highlight a
    whole net by ``net_id`` without walking a tree.
    """

    net_id: NetId
    net_name: str
    net_class: NetClass
    path: tuple[Point2, ...]


@dataclass(frozen=True, slots=True)
class Junction:
    """A solid dot: three or more segments of one net meeting at a point.

    Only where a pin's vertical lands in the MIDDLE of its trunk. At either end the trunk
    simply turns the corner, and a dot there would claim a join that is really a bend.
    """

    net_id: NetId
    at: Point2


@dataclass(frozen=True, slots=True)
class Rail:
    """A ground or power glyph hanging off one pin, standing in for wires to every other.

    ``path`` is the stub from the pin to ``at``; the glyph is drawn at ``at`` pointing
    ``direction``. ``net_class`` picks the glyph: three shrinking bars for ground, a bar on
    a stem for power.

    ``at`` sits on a reserved track of a horizontal channel -- the same allocation the
    trunks come out of -- so no trunk can ever run along the line the bars are drawn on. A
    wire may still CROSS the stub, which is an ordinary schematic crossing and carries no
    dot; a wire lying along the glyph reads as part of it, which is not.
    """

    net_id: NetId
    net_name: str
    net_class: NetClass
    path: tuple[Point2, ...]
    at: Point2
    direction: Literal["down", "up"]


@dataclass(frozen=True, slots=True)
class NoConnect:
    """A cross on a pin that no net in the document reaches.

    A STATEMENT OF FACT, NOT OF INTENT, and that is the difference from KiCad's marker of
    the same shape. There, somebody places one to say "I meant to leave this open"; here
    nothing is placed by hand, so what this can honestly say is only that the netlist does
    not mention the pin. That is worth saying: without it an unwired pin is drawn as a
    plain lead ending in space, which is also what a pin whose wire the sheet failed to
    draw would look like, and a reader cannot tell those apart.

    It is not a defect and produces no note. Unused pins on a header are the ordinary case
    -- ten of the eleven parts on `arduino-io-shield` have one -- and LVS is where a
    connection that was supposed to exist gets reported.
    """

    ref: str
    pin: str
    at: Point2


@dataclass(frozen=True, slots=True)
class Label:
    """A piece of text with a place and a job.

    ``kind`` exists so the renderer can style text without parsing it: a reference reads
    bold, a value dim, a net name small, a pin number smaller still.
    """

    text: str
    at: Point2
    kind: Literal["ref", "value", "net", "pin"]
    anchor: Literal["left", "centre", "right"] = "left"


@dataclass(frozen=True, slots=True)
class SchematicOptions:
    """What to draw, kept separate from how.

    ``rail_classes`` is the one worth changing: empty it and every ground pin gets a wire,
    which is occasionally what you want on a four-part circuit and never what you want on
    a thirty-part one.
    """

    rail_classes: frozenset[NetClass] = frozenset({"ground", "power"})
    #: A power net with fewer nodes than this stays a wire. Two is the convention -- a
    #: schematic uses the glyph however few pins the rail reaches -- and it is a number
    #: rather than a flag so a two-pin +5V can be made a visible wire when that is clearer.
    rail_min_nodes: int = 2
    show_values: bool = True
    show_pin_numbers: bool = True


DEFAULT_SCHEMATIC_OPTIONS = SchematicOptions()


@dataclass(frozen=True, slots=True)
class SchematicDrawing:
    """Everything on the sheet, plus what could not be put on it.

    ``notes`` is not decoration. A pin the netlist names and the footprint does not have,
    a part that is not on the board, a net with a single node -- each is a real defect that
    LVS also reports, and a drawing that silently omitted them would be a picture of a
    circuit nobody has.
    """

    symbols: tuple[Symbol, ...] = ()
    wires: tuple[Wire, ...] = ()
    rails: tuple[Rail, ...] = ()
    junctions: tuple[Junction, ...] = ()
    no_connects: tuple[NoConnect, ...] = ()
    labels: tuple[Label, ...] = ()
    width: Mm = 0.0
    height: Mm = 0.0
    notes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# What a renderer has to be told
# ---------------------------------------------------------------------------

#: The bars of a ground glyph: how wide each is as a fraction of ``RAIL_GLYPH_MM``, and how
#: far along the stub it sits as a fraction of ``RAIL_GLYPH_DEPTH_MM``. Three shrinking bars
#: is the convention, and the shrinking is what makes the glyph read as a ground rather than
#: as three wires that happen to be stacked.
_GROUND_BARS: tuple[tuple[float, float], ...] = ((1.0, 0.0), (0.6, 0.35), (0.25, 0.70))


def no_connect_arms(mark: NoConnect) -> tuple[tuple[Point2, Point2], tuple[Point2, Point2]]:
    """The two strokes of the cross, so the panel and the exported sheet cannot disagree.

    The same arrangement as ``rail_glyph_bars``: the shape lives here, beside the layout
    that made room for it, and a renderer asks rather than deciding.
    """
    reach = NO_CONNECT_MM
    return (
        (
            Point2(x=mark.at.x - reach, y=mark.at.y - reach),
            Point2(x=mark.at.x + reach, y=mark.at.y + reach),
        ),
        (
            Point2(x=mark.at.x - reach, y=mark.at.y + reach),
            Point2(x=mark.at.x + reach, y=mark.at.y - reach),
        ),
    )


def rail_glyph_bars(rail: Rail) -> tuple[tuple[Point2, Point2], ...]:
    """The bars to draw at ``rail.at``: one for power, three shrinking ones for ground.

    ONE FACT, TWO RENDERERS. ``ui/viewsch.py`` paints these on screen and
    ``schematic_export.py`` writes them into an SVG that becomes the printed sheet. A glyph
    drawn one way in the panel and another on paper would be two answers to "which rail is
    this", which is the one question the glyph exists to answer.

    The bars stay inside the box the LAYOUT cleared -- ``RAIL_GLYPH_MM`` across the stub and
    ``RAIL_GLYPH_DEPTH_MM`` along it -- because that box is the only reason no wire is lying
    where they go; ``test_nothing_is_drawn_through_a_rail_glyph`` measures the layout half of
    that bargain. They use 0.7 of the depth rather than all of it: the next lane is one track
    pitch away and a bar reaching for it reads as touching.

    ``direction`` is honoured rather than assumed. Every ground rail this module emits points
    down and every power rail up, because ``rail_channel`` picks the channel from the net
    class -- so this is the reading that stays correct if that ever stops being true, not a
    case anybody has seen.
    """
    x, y = rail.at.x, rail.at.y
    if rail.net_class == "power":
        return ((Point2(x=x - RAIL_GLYPH_MM, y=y), Point2(x=x + RAIL_GLYPH_MM, y=y)),)
    sign = 1.0 if rail.direction == "down" else -1.0
    return tuple(
        (
            Point2(x=x - RAIL_GLYPH_MM * shrink, y=y + sign * RAIL_GLYPH_DEPTH_MM * step),
            Point2(x=x + RAIL_GLYPH_MM * shrink, y=y + sign * RAIL_GLYPH_DEPTH_MM * step),
        )
        for shrink, step in _GROUND_BARS
    )


# ---------------------------------------------------------------------------
# Symbols, generated from the footprint registry
# ---------------------------------------------------------------------------
#
# The rule this section enforces, stated once: a symbol gets its real electrical shape only
# where the registry knows what each of its leads IS. Two leads plus a body archetype is
# enough to tell a resistor from a ceramic capacitor; a plus, a K or an A in a pin name is
# enough to know which way round a polarised part goes. Three leads on a TO-92 is not
# enough to know which one is the base, so a TO-92 is a box with numbered pins -- which is
# less pretty and does not lie.


@dataclass(frozen=True, slots=True)
class _PinSpec:
    number: str
    name: str | None


@dataclass(frozen=True, slots=True)
class _SymbolBody:
    shapes: tuple[SymbolShape, ...]
    pins: tuple[SymbolPin, ...]
    width: Mm
    height: Mm


def _p(x: Mm, y: Mm) -> Point2:
    return Point2(x=x, y=y)


def _pin_sort_key(number: str) -> tuple[int, float, str]:
    """Pin "10" after pin "9", and a lettered pin after every numbered one.

    A netlist may name a pin anything. Sorting the numbers as text puts pin 10 between 1
    and 2 and silently reorders half a DIP-16.
    """
    try:
        return (0, float(number), "")
    except ValueError:
        return (1, 0.0, number)


def _other_pin(pins: tuple[_PinSpec, ...], number: str) -> str | None:
    for pin in pins:
        if pin.number != number:
            return pin.number
    return None


def _cathode_pin_number(pins: tuple[_PinSpec, ...], polarized: bool) -> str | None:
    """Which lead is the cathode, by the rule ``guide._polarity_note`` already follows.

    Names first, convention second, and in that order deliberately: an LED has pin 1 as its
    ANODE and a diode has pin 1 as its cathode, so a rule that reads pin 1 without looking
    at the names draws one of the two backwards -- on the screen, and then on the bench.
    """
    for pin in pins:
        if pin.name == "K":
            return pin.number
    for pin in pins:
        if pin.name == "A":
            return _other_pin(pins, pin.number)
    if polarized and len(pins) == 2:
        # Unnamed but polarised: a diode, where pin 1 is the cathode by the convention this
        # registry and KiCad DO-41 both follow.
        return pins[0].number
    return None


def _positive_pin_number(pins: tuple[_PinSpec, ...], polarized: bool) -> str | None:
    """Which lead of an electrolytic is the positive one. Names first, as above."""
    for pin in pins:
        if pin.name == "+":
            return pin.number
    for pin in pins:
        if pin.name == "-":
            return _other_pin(pins, pin.number)
    if polarized and len(pins) == 2:
        return pins[0].number
    return None


# -- two-terminal geometry ---------------------------------------------------
#
# Every two-lead symbol is the same size and sits on the same axis, so a row of them lines
# up without the layout having to know what any of them is.

_TWO_W: Mm = 8 * GRID_MM
_TWO_H: Mm = 4 * GRID_MM
_AXIS: Mm = _TWO_H / 2


def _two_terminal_pins(
    pins: tuple[_PinSpec, ...], left_number: str | None
) -> tuple[SymbolPin, ...]:
    """Pin 1 on the left unless ``left_number`` says otherwise.

    The polarity helpers return a pin NUMBER rather than a side, so this is where a part
    whose cathode is pin 2 gets DRAWN the other way round instead of being relabelled.
    """
    ordered = list(pins)
    if left_number is not None and len(ordered) == 2 and ordered[0].number != left_number:
        ordered.reverse()
    return (
        SymbolPin(number=ordered[0].number, name=ordered[0].name, at=_p(0.0, _AXIS), side="left"),
        SymbolPin(
            number=ordered[1].number, name=ordered[1].name, at=_p(_TWO_W, _AXIS), side="right"
        ),
    )


def _leads(x_left_end: Mm, x_right_start: Mm) -> tuple[SymbolShape, ...]:
    return (
        SymbolShape(kind="polyline", points=(_p(0.0, _AXIS), _p(x_left_end, _AXIS))),
        SymbolShape(kind="polyline", points=(_p(x_right_start, _AXIS), _p(_TWO_W, _AXIS))),
    )


def _resistor_body(pins: tuple[_PinSpec, ...], footprint: Footprint | None) -> _SymbolBody:
    """The IEC rectangle rather than the zig-zag.

    Both are correct and the rectangle stays legible at the size a whole sheet is looked
    at, which is the size this one is looked at.
    """
    half = 0.75 * GRID_MM
    box = SymbolShape(
        kind="polygon",
        points=(
            _p(2 * GRID_MM, _AXIS - half),
            _p(6 * GRID_MM, _AXIS - half),
            _p(6 * GRID_MM, _AXIS + half),
            _p(2 * GRID_MM, _AXIS + half),
        ),
    )
    return _SymbolBody(
        shapes=(*_leads(2 * GRID_MM, 6 * GRID_MM), box),
        pins=_two_terminal_pins(pins, None),
        width=_TWO_W,
        height=_TWO_H,
    )


def _capacitor_body(pins: tuple[_PinSpec, ...], footprint: Footprint | None) -> _SymbolBody:
    half = 1.3 * GRID_MM
    left_x, right_x = 3.6 * GRID_MM, 4.4 * GRID_MM
    plates = (
        SymbolShape(kind="polyline", points=(_p(left_x, _AXIS - half), _p(left_x, _AXIS + half))),
        SymbolShape(kind="polyline", points=(_p(right_x, _AXIS - half), _p(right_x, _AXIS + half))),
    )
    return _SymbolBody(
        shapes=(*_leads(left_x, right_x), *plates),
        pins=_two_terminal_pins(pins, None),
        width=_TWO_W,
        height=_TWO_H,
    )


def _polarised_capacitor_body(
    pins: tuple[_PinSpec, ...], footprint: Footprint | None
) -> _SymbolBody:
    """Straight plate positive, curved plate negative, and a plus over the positive lead.

    The plus is the mark actually printed on the can, so the sheet and the part in the hand
    agree about which end is which.
    """
    half = 1.3 * GRID_MM
    straight_x, curve_x = 3.5 * GRID_MM, 4.6 * GRID_MM
    positive = _positive_pin_number(pins, footprint.polarized if footprint else True)
    curve_points: list[Point2] = []
    steps = 8
    for index in range(steps + 1):
        t = -1.0 + 2.0 * index / steps
        curve_points.append(_p(curve_x + 0.5 * GRID_MM * (1.0 - t * t), _AXIS + t * half))
    plus_x, plus_y = 2.4 * GRID_MM, _AXIS - 1.9 * GRID_MM
    arm = 0.35 * GRID_MM
    shapes = (
        *_leads(straight_x, curve_x),
        SymbolShape(
            kind="polyline", points=(_p(straight_x, _AXIS - half), _p(straight_x, _AXIS + half))
        ),
        SymbolShape(kind="polyline", points=tuple(curve_points)),
        SymbolShape(kind="polyline", points=(_p(plus_x - arm, plus_y), _p(plus_x + arm, plus_y))),
        SymbolShape(kind="polyline", points=(_p(plus_x, plus_y - arm), _p(plus_x, plus_y + arm))),
    )
    return _SymbolBody(
        shapes=shapes, pins=_two_terminal_pins(pins, positive), width=_TWO_W, height=_TWO_H
    )


def _diode_shapes(*, cathode_left: bool) -> tuple[SymbolShape, ...]:
    half = 1.3 * GRID_MM
    near, far = 3.6 * GRID_MM, 5.2 * GRID_MM
    bar_x = near if cathode_left else far
    tip_x = near if cathode_left else far
    base_x = far if cathode_left else near
    return (
        *_leads(near, far),
        SymbolShape(kind="polyline", points=(_p(bar_x, _AXIS - half), _p(bar_x, _AXIS + half))),
        SymbolShape(
            kind="polygon",
            points=(_p(base_x, _AXIS - half), _p(base_x, _AXIS + half), _p(tip_x, _AXIS)),
        ),
    )


def _diode_body(pins: tuple[_PinSpec, ...], footprint: Footprint | None) -> _SymbolBody:
    cathode = _cathode_pin_number(pins, footprint.polarized if footprint else True)
    return _SymbolBody(
        shapes=_diode_shapes(cathode_left=True),
        pins=_two_terminal_pins(pins, cathode),
        width=_TWO_W,
        height=_TWO_H,
    )


def _led_body(pins: tuple[_PinSpec, ...], footprint: Footprint | None) -> _SymbolBody:
    """A diode with two arrows leaving it.

    The arrows are the whole difference between "this is a diode" and "this is the part
    that lights up", so they are drawn rather than implied by a colour.
    """
    cathode = _cathode_pin_number(pins, footprint.polarized if footprint else True)
    arrows: list[SymbolShape] = []
    # Kept inside the symbol's own box on purpose: the reference designator is drawn just
    # above that box, and an arrowhead poking out of it lands underneath the text.
    for offset in (0.0, 0.9 * GRID_MM):
        start = _p(4.0 * GRID_MM + offset, _AXIS - 1.1 * GRID_MM)
        end = _p(start.x + 0.6 * GRID_MM, start.y - 0.6 * GRID_MM)
        head = 0.3 * GRID_MM
        arrows.append(SymbolShape(kind="polyline", points=(start, end)))
        arrows.append(
            SymbolShape(
                kind="polygon",
                points=(
                    end,
                    _p(end.x - head * 2, end.y + head * 0.6),
                    _p(end.x - head * 0.6, end.y + head * 2),
                ),
                filled=True,
            )
        )
    return _SymbolBody(
        shapes=(*_diode_shapes(cathode_left=True), *arrows),
        pins=_two_terminal_pins(pins, cathode),
        width=_TWO_W,
        height=_TWO_H,
    )


def _crystal_body(pins: tuple[_PinSpec, ...], footprint: Footprint | None) -> _SymbolBody:
    half = 1.3 * GRID_MM
    left_x, right_x = 3.2 * GRID_MM, 5.6 * GRID_MM
    slab = 0.9 * GRID_MM
    shapes = (
        *_leads(left_x, right_x),
        SymbolShape(kind="polyline", points=(_p(left_x, _AXIS - half), _p(left_x, _AXIS + half))),
        SymbolShape(kind="polyline", points=(_p(right_x, _AXIS - half), _p(right_x, _AXIS + half))),
        SymbolShape(
            kind="polygon",
            points=(
                _p(3.8 * GRID_MM, _AXIS - slab),
                _p(5.0 * GRID_MM, _AXIS - slab),
                _p(5.0 * GRID_MM, _AXIS + slab),
                _p(3.8 * GRID_MM, _AXIS + slab),
            ),
        ),
    )
    return _SymbolBody(
        shapes=shapes, pins=_two_terminal_pins(pins, None), width=_TWO_W, height=_TWO_H
    )


def _potentiometer_body(pins: tuple[_PinSpec, ...], footprint: Footprint | None) -> _SymbolBody:
    """A resistor with an arrow into it, pin 2 the wiper.

    THE ONE ASSUMPTION IN THIS FILE THE REGISTRY DOES NOT BACK. ``pot-3`` carries no pin
    names, and on a three-lead inline potentiometer the middle lead is the wiper --
    universally, on every part this tool models. It is written down here rather than left
    to be discovered, because it is the same shape of claim the module refuses to make
    about a TO-92 base; the difference is that this one has no counterexample.
    """
    height = 6 * GRID_MM
    axis = 4 * GRID_MM
    half = 0.75 * GRID_MM
    wiper_y = 1 * GRID_MM
    wiper_x = 4 * GRID_MM
    head = 0.45 * GRID_MM
    shapes = (
        SymbolShape(kind="polyline", points=(_p(0.0, axis), _p(2 * GRID_MM, axis))),
        SymbolShape(kind="polyline", points=(_p(6 * GRID_MM, axis), _p(_TWO_W, axis))),
        SymbolShape(
            kind="polygon",
            points=(
                _p(2 * GRID_MM, axis - half),
                _p(6 * GRID_MM, axis - half),
                _p(6 * GRID_MM, axis + half),
                _p(2 * GRID_MM, axis + half),
            ),
        ),
        SymbolShape(
            kind="polyline",
            points=(
                _p(_TWO_W, wiper_y),
                _p(wiper_x, wiper_y),
                _p(wiper_x, axis - half - head * 2),
            ),
        ),
        SymbolShape(
            kind="polygon",
            points=(
                _p(wiper_x - head, axis - half - head * 2),
                _p(wiper_x + head, axis - half - head * 2),
                _p(wiper_x, axis - half),
            ),
            filled=True,
        ),
    )
    by_number = {pin.number: pin for pin in pins}
    ends = [pin for pin in pins if pin.number != "2"]
    wiper = by_number.get("2", pins[-1])
    if len(ends) < 2:  # pragma: no cover - a 3-pin footprint always has two non-wiper pins
        ends = list(pins)
    drawn = (
        SymbolPin(number=ends[0].number, name=ends[0].name, at=_p(0.0, axis), side="left"),
        SymbolPin(number=wiper.number, name=wiper.name, at=_p(_TWO_W, wiper_y), side="right"),
        SymbolPin(number=ends[-1].number, name=ends[-1].name, at=_p(_TWO_W, axis), side="right"),
    )
    return _SymbolBody(shapes=shapes, pins=drawn, width=_TWO_W, height=height)


# -- everything else is a box, and the box says what it knows -----------------


def _boxy_body(
    pins: tuple[_PinSpec, ...], *, body_width: Mm, split: bool, dip_order: bool, notch: bool
) -> _SymbolBody:
    """A rectangle with numbered pins down one or both sides.

    ``dip_order`` is the DIP numbering and not a style: pins 1..n/2 run down the left and
    the rest run UP the right, which is how the package is numbered and therefore the only
    ordering that lets someone read a pin off this sheet and find it on the part.
    """
    count = len(pins)
    if split:
        per_side = (count + 1) // 2
        left = list(pins[:per_side])
        right = list(pins[per_side:])
        if dip_order:
            right.reverse()
    else:
        left, right = list(pins), []

    rows = max(len(left), len(right), 1)
    height = (rows + 1) * PIN_PITCH_MM
    width = LEAD_MM + body_width + (LEAD_MM if right else 0.0)
    body_left = LEAD_MM
    body_right = LEAD_MM + body_width
    top = PIN_PITCH_MM / 2
    bottom = height - PIN_PITCH_MM / 2

    shapes: list[SymbolShape] = [
        SymbolShape(
            kind="polygon",
            points=(
                _p(body_left, top),
                _p(body_right, top),
                _p(body_right, bottom),
                _p(body_left, bottom),
            ),
        )
    ]
    drawn: list[SymbolPin] = []
    for index, pin in enumerate(left):
        y = (index + 1) * PIN_PITCH_MM
        shapes.append(SymbolShape(kind="polyline", points=(_p(0.0, y), _p(body_left, y))))
        drawn.append(SymbolPin(number=pin.number, name=pin.name, at=_p(0.0, y), side="left"))
    for index, pin in enumerate(right):
        y = (index + 1) * PIN_PITCH_MM
        shapes.append(SymbolShape(kind="polyline", points=(_p(body_right, y), _p(width, y))))
        drawn.append(SymbolPin(number=pin.number, name=pin.name, at=_p(width, y), side="right"))
    if notch:
        # The pin-1 mark, in the place the package carries it.
        shapes.append(
            SymbolShape(
                kind="circle",
                points=(_p((body_left + body_right) / 2, top),),
                radius=0.5 * GRID_MM,
            )
        )
    return _SymbolBody(
        shapes=tuple(shapes), pins=tuple(drawn), width=width, height=height
    )


def _ic_body(pins: tuple[_PinSpec, ...], footprint: Footprint | None) -> _SymbolBody:
    return _boxy_body(pins, body_width=8 * GRID_MM, split=True, dip_order=True, notch=True)


def _connector_body(pins: tuple[_PinSpec, ...], footprint: Footprint | None) -> _SymbolBody:
    """Pins down one side however many there are.

    A header is a place wires leave the board, and splitting one across two sides of a box
    would draw the eight-way strip in your hand as two four-ways.
    """
    body = _boxy_body(pins, body_width=6 * GRID_MM, split=False, dip_order=False, notch=False)
    shroud = SymbolShape(
        kind="polyline",
        points=(
            _p(LEAD_MM + 1.4 * GRID_MM, PIN_PITCH_MM / 2),
            _p(LEAD_MM + 1.4 * GRID_MM, body.height - PIN_PITCH_MM / 2),
        ),
    )
    return _SymbolBody(
        shapes=(*body.shapes, shroud), pins=body.pins, width=body.width, height=body.height
    )


def _box_body(pins: tuple[_PinSpec, ...], footprint: Footprint | None) -> _SymbolBody:
    """The honest fallback: a TO-92, a tactile switch, a relay, an unplaced part.

    Five pins or fewer go down one side, because a three-lead part with one pin on the left
    and two on the right invites the reader to see a transistor -- which is precisely the
    reading this box exists to avoid.
    """
    split = len(pins) > 5
    return _boxy_body(pins, body_width=6 * GRID_MM, split=split, dip_order=False, notch=False)


_SYMBOL_BUILDERS: dict[
    SymbolKind, Callable[[tuple[_PinSpec, ...], Footprint | None], _SymbolBody]
] = {
    "resistor": _resistor_body,
    "capacitor": _capacitor_body,
    "polarised-capacitor": _polarised_capacitor_body,
    "diode": _diode_body,
    "led": _led_body,
    "crystal": _crystal_body,
    "potentiometer": _potentiometer_body,
    "ic": _ic_body,
    "connector": _connector_body,
    "box": _box_body,
}

#: Which symbol an archetype asks for. ``test_schematic`` asserts this covers every member
#: of ``BodyArchetype``, so adding a body to the registry fails here rather than silently
#: drawing the new part as a box.
_KIND_BY_ARCHETYPE: dict[BodyArchetype, SymbolKind] = {
    "axial-cylinder": "resistor",  # ...or a diode, when the footprint says it is polarised
    "radial-electrolytic": "polarised-capacitor",
    "disc-ceramic": "capacitor",
    "box-film": "capacitor",
    "dip": "ic",
    "to92": "box",  # no E/B/C in the registry, so no transistor symbol
    "to220": "box",  # likewise, and the tab tells you nothing about the pinout either
    "led-round": "led",
    "pin-header": "connector",
    "screw-terminal": "connector",
    "potentiometer": "potentiometer",
    "tactile-switch": "box",  # which pins are the same pole is not recorded anywhere
    "crystal-hc49": "crystal",
    "relay-box": "box",
    "generic-box": "box",
}

_TWO_TERMINAL_KINDS: frozenset[SymbolKind] = frozenset(
    {"resistor", "capacitor", "polarised-capacitor", "diode", "led", "crystal"}
)


def symbol_kind_for(footprint: Footprint | None, pin_count: int) -> SymbolKind:
    """Which symbol this part gets drawn as.

    The pin-count guards are not defensive noise. A two-terminal shape has exactly two
    places to attach a wire, so a footprint that grew a third pin must fall back to a box
    rather than have the extra lead silently dropped off the sheet.
    """
    if footprint is None:
        return "box"
    kind = _KIND_BY_ARCHETYPE[footprint.body.archetype]
    if kind == "resistor" and footprint.polarized:
        kind = "diode"
    if kind in _TWO_TERMINAL_KINDS and pin_count != 2:
        return "box"
    if kind == "potentiometer" and pin_count != 3:
        return "box"
    return kind


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
#
# Columns and rows for the symbols, channels between them for the wires, and nothing
# anywhere else. The whole reason a generated schematic can be read at all is that the two
# never share space:
#
#     channel 0   column 0   channel 1   column 1   channel 2
#   +-----------+----------+-----------+----------+-----------+
#   |           |   [R1]   |           |   [U1]   |           |  row 0
#   +-----------+----------+-----------+----------+-----------+  <- horizontal channel 1
#   |           |   [C1]   |           |   [R2]   |           |  row 1
#   +-----------+----------+-----------+----------+-----------+
#
# A pin leaves its symbol horizontally into the vertical channel beside its column; the
# wire runs down that channel to a horizontal trunk sitting in the channel between two
# rows; the trunk runs across to the other pins. Every segment is in a channel, so no
# segment can cross a symbol. Both kinds of channel widen to fit however many tracks a
# left-edge sweep says they need, which is why the sheet is sized last and not guessed.


def _ref_sort_key(ref: str) -> tuple[str, int, str]:
    """R2 before R10, and R before U. Sorting references as plain text does neither."""
    prefix_length = 0
    while prefix_length < len(ref) and ref[prefix_length].isalpha():
        prefix_length += 1
    prefix = ref[:prefix_length]
    digits = ""
    for character in ref[prefix_length:]:
        if not character.isdigit():
            break
        digits += character
    return (prefix.upper(), int(digits) if digits else -1, ref)


@dataclass(slots=True)
class _Placed:
    ref: str
    value: str
    kind: SymbolKind
    footprint_id: str | None
    body: _SymbolBody
    unplaced: bool
    undefined: bool
    col: int = 0
    row: int = 0
    x: Mm = 0.0
    y: Mm = 0.0

    def anchor_of(self, pin: SymbolPin) -> Point2:
        return Point2(x=self.x + pin.at.x, y=self.y + pin.at.y)


def _assign_tracks(intervals: Sequence[tuple[str, float, float]]) -> tuple[dict[str, int], int]:
    """The fewest parallel lanes a set of intervals needs, by the left-edge sweep.

    Two runs share a lane only when one finishes strictly before the other starts. Allowing
    them to touch would put two wires end to end on the same line, which reads as one wire
    and is the one mistake a schematic must not make.
    """
    ordered = sorted(intervals, key=lambda item: (item[1], item[2], item[0]))
    last_end: list[float] = []
    tracks: dict[str, int] = {}
    for key, low, high in ordered:
        for index, end in enumerate(last_end):
            if end < low:
                last_end[index] = high
                tracks[key] = index
                break
        else:
            tracks[key] = len(last_end)
            last_end.append(high)
    return tracks, len(last_end)


def _channel_size(track_count: int) -> Mm:
    return max(MIN_CHANNEL_MM, 2 * GRID_MM + max(0, track_count - 1) * TRACK_PITCH_MM)


def _is_rail(net: Net, options: SchematicOptions) -> bool:
    return net.net_class in options.rail_classes and len(net.nodes) >= options.rail_min_nodes


def _collect_symbols(
    doc: PerfDocument, lookup: FootprintLookup, notes: list[str]
) -> dict[str, _Placed]:
    """One symbol per reference the design defines OR the board carries OR a net names.

    All three, and each for its own reason. A part on the board is obvious. A part in
    ``doc.parts`` is the schematic-first case -- drawn and wired before anything has been
    laid out, which is the only state a circuit is in while it is being captured. A ref
    that only a net names is neither, and it is the one that is actually wrong: something
    is wired to a part nothing in the document defines. Drawing only the intersection
    would hide two of the three.

    A part's own definition -- its footprint and value -- comes from wherever it lives,
    so an unplaced resistor is drawn as a resistor rather than as a box with two pins.
    """
    placed_by_ref = {component.ref: component for component in doc.components}
    designed_by_ref = {part.ref: part for part in doc.parts}
    pins_named: dict[str, set[str]] = defaultdict(set)
    for net in doc.nets:
        for node in net.nodes:
            pins_named[node.component_ref].add(node.pin)

    refs = sorted(
        set(pins_named) | set(placed_by_ref) | set(designed_by_ref), key=_ref_sort_key
    )
    symbols: dict[str, _Placed] = {}
    for ref in refs:
        component = placed_by_ref.get(ref)
        part = designed_by_ref.get(ref)
        value = component.value if component is not None else (part.value if part else "")
        footprint_id = (
            component.footprint_id
            if component is not None
            else (part.footprint_id if part is not None else None)
        )
        footprint = lookup(footprint_id) if footprint_id is not None else None
        if footprint_id is not None and footprint is None:
            notes.append(f"{ref}: footprint {footprint_id!r} is not in the registry")
        if footprint is not None:
            specs = tuple(_PinSpec(number=pin.number, name=pin.name) for pin in footprint.pins)
            missing = sorted(
                pins_named[ref] - {pin.number for pin in footprint.pins}, key=_pin_sort_key
            )
            if missing:
                notes.append(
                    f"{ref}: the netlist names pin(s) {', '.join(missing)}, "
                    f"which {footprint.id} does not have"
                )
        else:
            # No footprint to ask, so the netlist is the only account of what pins exist.
            specs = tuple(
                _PinSpec(number=number, name=None)
                for number in sorted(pins_named[ref], key=_pin_sort_key)
            )
        if not specs:
            specs = (_PinSpec(number="1", name=None),)
        kind = symbol_kind_for(footprint, len(specs))
        undefined = component is None and part is None
        symbols[ref] = _Placed(
            ref=ref,
            value=value,
            kind=kind,
            footprint_id=footprint_id,
            body=_SYMBOL_BUILDERS[kind](specs, footprint),
            unplaced=component is None,
            undefined=undefined,
        )
        if undefined:
            # NOT reported for a part that is merely unplaced: on a schematic being drawn
            # that is every part on the sheet, and a note per part would bury the ones
            # that mean something. This one means something -- a net is wired to a part
            # the document does not have.
            notes.append(
                f"{ref}: wired by a net, but neither on the board nor in the design"
            )
    return symbols


def _split_tall_layers(layers: list[list[str]], group_size: int) -> list[list[str]]:
    """Break a layer that is too tall to read into consecutive columns of its own.

    BFS DEPTH IS NOT A CONSTRAINT, IT IS A HINT. A layering that puts everything one hop
    from the root in one column is right about the distance and wrong about the shape: on
    the LM317 example ten of the eleven parts hang directly off U1, which drew a sheet
    three columns wide and ten rows tall -- 129 x 239 mm, nearly twice as tall as it was
    wide, on a circuit that fits comfortably across a page. Nothing was violated by that;
    a schematic has no precedence to respect, so a part moved one column further out only
    makes its own wire span one more channel.

    The cap is the side of a square: a group of n parts wants about sqrt(n) of them in a
    column, which is the same arithmetic the loose-parts block below already uses, and
    never fewer than three, so a small circuit is not spread into a strip. Chunks are
    consecutive in the reference order the layer already carries, which keeps R1 beside R2
    rather than scattering a group of equals, and is deterministic for the reason
    everything here is.
    """
    cap = max(3, math.ceil(math.sqrt(group_size)))
    out: list[list[str]] = []
    for layer in layers:
        for start in range(0, len(layer), cap):
            out.append(layer[start : start + cap])
    return out


def _assign_cells(
    symbols: dict[str, _Placed], adjacency: dict[str, set[str]]
) -> tuple[int, int]:
    """Put every symbol in a (column, row) cell and say how big the grid came out.

    Columns come from a breadth-first layering away from the most connected part, which on
    a perfboard circuit is almost always the IC everything hangs off; rows come from four
    barycentre sweeps, the standard crossing-reduction heuristic. Neither is clever. Both
    are deterministic, which is what a golden test needs and what stops the sheet
    rearranging itself when an unrelated net is edited.
    """
    order = sorted(symbols, key=_ref_sort_key)
    visited: set[str] = set()
    columns: list[list[str]] = []
    alone: list[str] = []

    for seed in order:
        if seed in visited:
            continue
        group: list[str] = []
        queue = deque([seed])
        visited.add(seed)
        while queue:
            ref = queue.popleft()
            group.append(ref)
            for neighbour in sorted(adjacency[ref], key=_ref_sort_key):
                if neighbour not in visited:
                    visited.add(neighbour)
                    queue.append(neighbour)

        if len(group) == 1:
            # A part no signal net reaches -- a mounting pillar, a part only ground
            # touches, a part nothing touches at all. One column each would draw sixteen
            # of them as a sheet two feet wide, so they are gathered into a block below
            # and the layering never sees them.
            alone.append(group[0])
            continue

        root = min(group, key=lambda ref: (-len(adjacency[ref]), _ref_sort_key(ref)))
        depth: dict[str, int] = {root: 0}
        queue = deque([root])
        while queue:
            ref = queue.popleft()
            for neighbour in sorted(adjacency[ref], key=_ref_sort_key):
                if neighbour not in depth:
                    depth[neighbour] = depth[ref] + 1
                    queue.append(neighbour)

        local: list[list[str]] = [[] for _ in range(max(depth.values()) + 1)]
        for ref in sorted(group, key=_ref_sort_key):
            local[depth[ref]].append(ref)
        columns.extend(_split_tall_layers(local, len(group)))

    _barycentre_sweeps(columns, adjacency)

    if alone:
        tall = max((len(column) for column in columns), default=0)
        per_column = max(tall, math.ceil(math.sqrt(len(alone))))
        for start in range(0, len(alone), per_column):
            columns.append(alone[start : start + per_column])

    rows = max((len(column) for column in columns), default=0)
    for index, column in enumerate(columns):
        offset = (rows - len(column)) // 2
        for position, ref in enumerate(column):
            symbols[ref].col = index
            symbols[ref].row = offset + position
    return len(columns), rows


def _barycentre_sweeps(
    columns: list[list[str]], adjacency: dict[str, set[str]], passes: int = 4
) -> None:
    """Reorder each column by the average position of its neighbours in the column beside
    it, alternating direction. Sorted in place; ties broken by reference so the result does
    not depend on sort stability alone."""
    position: dict[str, int] = {}
    for column in columns:
        for index, ref in enumerate(column):
            position[ref] = index

    for sweep in range(passes):
        forward = sweep % 2 == 0
        indices = (
            range(1, len(columns)) if forward else range(len(columns) - 2, -1, -1)
        )
        for index in indices:
            beside = set(columns[index - 1] if forward else columns[index + 1])
            if not beside:
                continue

            def barycentre(ref: str, beside: set[str] = beside) -> tuple[float, tuple[str, int, str]]:
                near = [position[other] for other in adjacency[ref] if other in beside]
                mean = sum(near) / len(near) if near else float(position[ref])
                return (mean, _ref_sort_key(ref))

            columns[index].sort(key=barycentre)
            for place, ref in enumerate(columns[index]):
                position[ref] = place


# ---------------------------------------------------------------------------
# The drawing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ResolvedNet:
    net: Net
    rail: bool
    #: (reference, pin) in the order the netlist gave them, deduplicated.
    pins: tuple[tuple[str, SymbolPin], ...]


def _tidy(points: Sequence[Point2]) -> tuple[Point2, ...]:
    """Drop repeated points so a run that turns nowhere is a straight line, not a corner."""
    cleaned: list[Point2] = []
    for point in points:
        if cleaned and abs(cleaned[-1].x - point.x) < 1e-9 and abs(cleaned[-1].y - point.y) < 1e-9:
            continue
        cleaned.append(point)
    return tuple(cleaned)


def _segments_of(path: tuple[Point2, ...]) -> Iterator[tuple[Point2, Point2]]:
    yield from itertools.pairwise(path)


def _label_box(text: str, x: Mm, baseline: Mm) -> tuple[Mm, Mm, Mm, Mm]:
    """The rectangle a net name occupies, given where its baseline goes.

    ``Label.at`` IS the baseline for a net name -- both renderers already read it that way
    -- so the clearance above the wire is baked into the point rather than applied twice,
    once here and once by whoever draws it.
    """
    width = len(text) * NET_LABEL_MM * NET_LABEL_ADVANCE
    return x, baseline - NET_LABEL_MM, x + width, baseline


def _box_is_clear(
    box: tuple[Mm, Mm, Mm, Mm], obstacles: Sequence[tuple[Point2, Point2]]
) -> bool:
    x0, y0, x1, y1 = box
    for start, end in obstacles:
        if max(start.x, end.x) < x0 or min(start.x, end.x) > x1:
            continue
        if max(start.y, end.y) < y0 or min(start.y, end.y) > y1:
            continue
        return False
    return True


def _net_label_at(
    text: str, span: tuple[Mm, Mm], wire_y: Mm, obstacles: Sequence[tuple[Point2, Point2]]
) -> Point2:
    """Where along its own run a net name can sit without a wire through it.

    A net name belongs at the left-hand end of the run it names, so that is the first
    thing tried and, on the fixtures here, where 29 of 41 of them stay. The rest slide
    RIGHT ALONG THE SAME RUN in half-grid steps until the band above the wire is clear --
    never to another wire, never off the run, so the name is still unambiguously attached
    to the thing it names. Failing that it goes back to the left end: a label that has to
    overlap something should overlap where a reader looks for it.

    Deterministic by construction. The candidates are generated left to right and the
    first clear one wins, so the same document gives the same sheet -- which is what the
    golden dumps need and what stops the sheet rearranging itself between runs.
    """
    left, right = span
    baseline = wire_y - NET_LABEL_CLEARANCE_MM
    start = left + NET_LABEL_INSET_MM
    width = len(text) * NET_LABEL_MM * NET_LABEL_ADVANCE
    step = GRID_MM / 2
    x = start
    while True:
        if _box_is_clear(_label_box(text, x, baseline), obstacles):
            return Point2(x=x, y=baseline)
        x += step
        if x + width > right:
            return Point2(x=start, y=baseline)


def build_schematic(
    doc: PerfDocument,
    lookup: FootprintLookup,
    options: SchematicOptions = DEFAULT_SCHEMATIC_OPTIONS,
) -> SchematicDrawing:
    """Draw the netlist.

    Works on whatever the document has. No netlist gives a sheet of unconnected symbols,
    which is a fair picture of a board nobody has declared anything about; no parts gives
    an empty sheet and says so in ``notes``.
    """
    notes: list[str] = []
    symbols = _collect_symbols(doc, lookup, notes)
    if not symbols:
        return SchematicDrawing(
            notes=("Nothing to draw: the document has no parts and no netlist.",)
        )

    pin_index: dict[tuple[str, str], SymbolPin] = {}
    for ref, placed in symbols.items():
        for pin in placed.body.pins:
            pin_index[(ref, pin.number)] = pin

    resolved: list[_ResolvedNet] = []
    for net in doc.nets:
        seen: set[tuple[str, str]] = set()
        picked: list[tuple[str, SymbolPin]] = []
        for node in net.nodes:
            node_key = (node.component_ref, node.pin)
            if node_key in seen:
                continue
            seen.add(node_key)
            found = pin_index.get(node_key)
            if found is None:
                notes.append(
                    f"{net.name}: {node.component_ref} pin {node.pin} is not on the sheet"
                )
                continue
            picked.append((node.component_ref, found))
        if not picked:
            notes.append(f"{net.name}: no pin of this net could be drawn")
            continue
        if len(picked) == 1:
            notes.append(f"{net.name}: only one pin, so it is drawn as a stub")
        resolved.append(
            _ResolvedNet(net=net, rail=_is_rail(net, options), pins=tuple(picked))
        )

    # Rails are deliberately absent from the graph. A ground net touching every part would
    # make every part adjacent to every other, and the layering below would put the whole
    # circuit in two columns -- which is exactly the hairball the glyphs exist to prevent,
    # arriving by the back door.
    adjacency: dict[str, set[str]] = {ref: set() for ref in symbols}
    for item in resolved:
        if item.rail:
            continue
        touched = sorted({ref for ref, _ in item.pins}, key=_ref_sort_key)
        for first in range(len(touched)):
            for second in range(first + 1, len(touched)):
                adjacency[touched[first]].add(touched[second])
                adjacency[touched[second]].add(touched[first])

    ncols, nrows = _assign_cells(symbols, adjacency)

    column_width = [0.0] * ncols
    row_height = [0.0] * nrows
    for placed in symbols.values():
        column_width[placed.col] = max(column_width[placed.col], placed.body.width)
        row_height[placed.row] = max(row_height[placed.row], placed.body.height)

    # -- which channel each run belongs in ----------------------------------
    #
    # Rails take a lane out of the same pool the trunks do. A ground glyph is three
    # horizontal bars, so a trunk running along the line it is drawn on reads as part of
    # it -- and unlike a crossing, there is no convention that says otherwise. Reserving
    # the lane costs at most one extra track in a channel, and rails pack into it densely
    # because each claims a single column rather than a span.
    def _rail_key(net_id: NetId, ref: str, number: str) -> str:
        return f"rail\x1f{net_id}\x1f{ref}\x1f{number}"

    pin_channel: dict[tuple[NetId, str, str], int] = {}
    trunk_channel: dict[NetId, int] = {}
    rail_channel: dict[str, int] = {}
    horizontal_runs: dict[int, list[tuple[str, float, float]]] = defaultdict(list)
    for item in resolved:
        spanned: list[int] = []
        for ref, pin in item.pins:
            channel = symbols[ref].col if pin.side == "left" else symbols[ref].col + 1
            pin_channel[(item.net.id, ref, pin.number)] = channel
            spanned.append(channel)
        if item.rail:
            upward = item.net.net_class == "power"
            for ref, pin in item.pins:
                channel = symbols[ref].row if upward else symbols[ref].row + 1
                key = _rail_key(item.net.id, ref, pin.number)
                rail_channel[key] = channel
                lane = float(pin_channel[(item.net.id, ref, pin.number)])
                horizontal_runs[channel].append((key, lane, lane))
            continue
        if len(item.pins) < 2:
            continue
        mean_row = sum(symbols[ref].row for ref, _ in item.pins) / len(item.pins)
        channel = min(nrows, int(mean_row) + 1)
        trunk_channel[item.net.id] = channel
        horizontal_runs[channel].append(
            (item.net.id, float(min(spanned)), float(max(spanned)))
        )

    horizontal_track: dict[str, int] = {}
    horizontal_tracks = [0] * (nrows + 1)
    for channel, runs in horizontal_runs.items():
        assigned, count = _assign_tracks(runs)
        horizontal_track.update(assigned)
        horizontal_tracks[channel] = count

    # -- y, which depends only on the horizontal channels -------------------
    horizontal_gap = [_channel_size(count) for count in horizontal_tracks]
    cursor = MARGIN_MM
    horizontal_channel_y = [0.0] * (nrows + 1)
    row_y = [0.0] * nrows
    for index in range(nrows):
        horizontal_channel_y[index] = cursor
        cursor += horizontal_gap[index]
        row_y[index] = cursor
        cursor += row_height[index]
    horizontal_channel_y[nrows] = cursor
    cursor += horizontal_gap[nrows]
    height = cursor + MARGIN_MM

    for placed in symbols.values():
        placed.y = row_y[placed.row] + (row_height[placed.row] - placed.body.height) / 2

    def _lane_y(channel: int, key: str) -> Mm:
        return horizontal_channel_y[channel] + GRID_MM + horizontal_track[key] * TRACK_PITCH_MM

    trunk_y: dict[NetId, float] = {
        net_id: _lane_y(channel, net_id) for net_id, channel in trunk_channel.items()
    }
    rail_y: dict[str, float] = {
        key: _lane_y(channel, key) for key, channel in rail_channel.items()
    }

    # -- x, which needs the y above to know which verticals overlap ---------
    def _segment_key(net_id: NetId, ref: str, number: str) -> str:
        return f"{net_id}\x1f{ref}\x1f{number}"

    vertical_runs: dict[int, list[tuple[str, float, float]]] = defaultdict(list)
    for item in resolved:
        for ref, pin in item.pins:
            anchor_y = symbols[ref].anchor_of(pin).y
            run_key = _segment_key(item.net.id, ref, pin.number)
            if item.rail:
                # Padded PAST the anchor by the glyph's depth, so nothing else is given the
                # same track through the space the bars are drawn in.
                glyph = rail_y[_rail_key(item.net.id, ref, pin.number)]
                beyond = glyph + (
                    -RAIL_GLYPH_DEPTH_MM if glyph < anchor_y else RAIL_GLYPH_DEPTH_MM
                )
                low, high = min(anchor_y, beyond), max(anchor_y, beyond)
            elif len(item.pins) < 2:
                low = high = anchor_y
            else:
                other = trunk_y[item.net.id]
                low, high = min(anchor_y, other), max(anchor_y, other)
            vertical_runs[pin_channel[(item.net.id, ref, pin.number)]].append(
                (run_key, low, high)
            )

    vertical_track: dict[str, int] = {}
    vertical_tracks = [0] * (ncols + 1)
    for channel, runs in vertical_runs.items():
        assigned, count = _assign_tracks(runs)
        vertical_track.update(assigned)
        vertical_tracks[channel] = count

    vertical_gap = [_channel_size(count) for count in vertical_tracks]
    cursor = MARGIN_MM
    vertical_channel_x = [0.0] * (ncols + 1)
    column_x = [0.0] * ncols
    for index in range(ncols):
        vertical_channel_x[index] = cursor
        cursor += vertical_gap[index]
        column_x[index] = cursor
        cursor += column_width[index]
    vertical_channel_x[ncols] = cursor
    cursor += vertical_gap[ncols]
    width = cursor + MARGIN_MM

    for placed in symbols.values():
        placed.x = column_x[placed.col] + (column_width[placed.col] - placed.body.width) / 2

    def _track_x(net_id: NetId, ref: str, number: str) -> Mm:
        key = _segment_key(net_id, ref, number)
        channel = pin_channel[(net_id, ref, number)]
        return vertical_channel_x[channel] + GRID_MM + vertical_track[key] * TRACK_PITCH_MM

    # -- emit ----------------------------------------------------------------
    wires: list[Wire] = []
    rails: list[Rail] = []
    junctions: list[Junction] = []
    net_labels: list[Label] = []
    # (name, the run's x span, the run's y). Placing a net name needs every OTHER run on
    # the sheet to already exist, and half of them are emitted after this one.
    pending_labels: list[tuple[str, tuple[Mm, Mm], Mm]] = []

    for item in resolved:
        net = item.net
        if item.rail:
            for ref, pin in item.pins:
                anchor = symbols[ref].anchor_of(pin)
                x = _track_x(net.id, ref, pin.number)
                end_y = rail_y[_rail_key(net.id, ref, pin.number)]
                rails.append(
                    Rail(
                        net_id=net.id,
                        net_name=net.name,
                        net_class=net.net_class,
                        path=_tidy((anchor, Point2(x=x, y=anchor.y), Point2(x=x, y=end_y))),
                        at=Point2(x=x, y=end_y),
                        direction="up" if end_y < anchor.y else "down",
                    )
                )
            continue

        if len(item.pins) == 1:
            ref, pin = item.pins[0]
            anchor = symbols[ref].anchor_of(pin)
            x = _track_x(net.id, ref, pin.number)
            path = _tidy((anchor, Point2(x=x, y=anchor.y)))
            if len(path) > 1:
                wires.append(
                    Wire(
                        net_id=net.id, net_name=net.name, net_class=net.net_class, path=path
                    )
                )
            pending_labels.append(
                (net.name, (min(anchor.x, x), max(anchor.x, x)), anchor.y)
            )
            continue

        y = trunk_y[net.id]
        branch_x: list[Mm] = []
        for ref, pin in item.pins:
            anchor = symbols[ref].anchor_of(pin)
            x = _track_x(net.id, ref, pin.number)
            branch_x.append(x)
            path = _tidy((anchor, Point2(x=x, y=anchor.y), Point2(x=x, y=y)))
            if len(path) > 1:
                wires.append(
                    Wire(
                        net_id=net.id, net_name=net.name, net_class=net.net_class, path=path
                    )
                )
        left, right = min(branch_x), max(branch_x)
        wires.append(
            Wire(
                net_id=net.id,
                net_name=net.name,
                net_class=net.net_class,
                path=(Point2(x=left, y=y), Point2(x=right, y=y)),
            )
        )
        for x in branch_x:
            if left < x < right:
                junctions.append(Junction(net_id=net.id, at=Point2(x=x, y=y)))
        pending_labels.append((net.name, (left, right), y))

    # Every pin the netlist reaches, so the rest can be marked. Read off `resolved` rather
    # than off `doc.nets`, because a node naming a pin the footprint does not have was
    # already dropped with a note and must not count as a connection.
    wired_pins = {(ref, pin.number) for item in resolved for ref, pin in item.pins}
    no_connects: list[NoConnect] = []
    for ref in sorted(symbols, key=_ref_sort_key):
        placed = symbols[ref]
        for pin in placed.body.pins:
            if (ref, pin.number) not in wired_pins:
                no_connects.append(
                    NoConnect(ref=ref, pin=pin.number, at=placed.anchor_of(pin))
                )

    obstacles: list[tuple[Point2, Point2]] = []
    for wire in wires:
        obstacles.extend(_segments_of(wire.path))
    for rail in rails:
        obstacles.extend(_segments_of(rail.path))
        obstacles.extend(rail_glyph_bars(rail))
    for name, span, y in pending_labels:
        at = _net_label_at(name, span, y, obstacles)
        net_labels.append(Label(text=name, at=at, kind="net", anchor="left"))

    ordered = sorted(symbols.values(), key=lambda placed: _ref_sort_key(placed.ref))
    part_labels: list[Label] = []
    for placed in ordered:
        centre = placed.x + placed.body.width / 2
        # Well clear of the box rather than snug to it: a reference is drawn at a fixed
        # PIXEL size, so how many millimetres of sheet it occupies grows as the view zooms
        # out -- and the one symbol with anything near its top edge is the LED.
        part_labels.append(
            Label(
                text=placed.ref,
                at=Point2(x=centre, y=placed.y - 0.75 * GRID_MM),
                kind="ref",
                anchor="centre",
            )
        )
        if options.show_values and placed.value:
            part_labels.append(
                Label(
                    text=placed.value,
                    at=Point2(x=centre, y=placed.y + placed.body.height + 0.4 * GRID_MM),
                    kind="value",
                    anchor="centre",
                )
            )
        if options.show_pin_numbers and placed.kind in ("ic", "connector", "box"):
            for pin in placed.body.pins:
                inset = LEAD_MM + 0.5 * GRID_MM
                if pin.side == "left":
                    at = Point2(x=placed.x + inset, y=placed.y + pin.at.y)
                    part_labels.append(
                        Label(text=pin.number, at=at, kind="pin", anchor="left")
                    )
                else:
                    at = Point2(x=placed.x + placed.body.width - inset, y=placed.y + pin.at.y)
                    part_labels.append(
                        Label(text=pin.number, at=at, kind="pin", anchor="right")
                    )

    drawn = tuple(
        Symbol(
            ref=placed.ref,
            value=placed.value,
            kind=placed.kind,
            footprint_id=placed.footprint_id,
            at=Point2(x=placed.x, y=placed.y),
            shapes=placed.body.shapes,
            pins=placed.body.pins,
            width=placed.body.width,
            height=placed.body.height,
            unplaced=placed.unplaced,
            undefined=placed.undefined,
        )
        for placed in ordered
    )

    return SchematicDrawing(
        symbols=drawn,
        wires=tuple(wires),
        rails=tuple(rails),
        junctions=tuple(junctions),
        no_connects=tuple(no_connects),
        labels=(*part_labels, *net_labels),
        width=width,
        height=height,
        notes=tuple(notes),
    )
