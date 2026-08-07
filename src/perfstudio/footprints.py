"""Parametric through-hole footprint library.

Ported line-for-line from the original TypeScript engine
(packages/core/src/footprints.ts). PerfStudio deliberately generates
footprints (and, from the same BodySpec, the matching 3D body) from a handful
of numeric parameters rather than shipping a mesh/footprint library: zero
assets, guaranteed agreement between the 2D footprint and the 3D body, and no
share-alike asset licence to inherit.

Why this file exists as a straight port rather than a redesign: the
acceptance criterion for this port is bit-for-bit reproduction of the
TypeScript registry (tools/diffcheck/golden/footprints.expected.json), field
by field, down to the last IEEE-754 double. That means preserving the exact
arithmetic and generation order of the original, not just its intent.

Conventions used throughout this file (carried over from the TS source):
 - Every pin offset (d_col/d_row) is an integer grid step on the standard
   2.54 mm perfboard pitch (model.STANDARD_PITCH_MM), regardless of the pitch
   of whatever board a component eventually lands on. body_outline and
   body_height are always millimetres.
 - ANCHOR CONVENTION: pin "1" always sits at grid offset (0, 0) - for
   two-lead parts, inline parts (TO-92, TO-220, headers, ...) and DIP
   packages alike. This keeps the anchor a real, physical pin in every case
   rather than an arbitrary geometric centre.
 - body_outline is a closed polygon (vertices only, no repeated closing
   point) in mm relative to the anchor. It doubles as the courtyard for
   overlap DRC, so every generator guarantees its bounding box contains every
   pin position plus a clearance margin (COURTYARD_MARGIN_MM, half a grid
   step) - even when the physical package is narrower than the pins fanned
   out to reach the grid (see to92_footprint).
 - Physical dimensions (body length/diameter/height, DIP row spacing, etc.)
   are realistic, commonly-seen values for each part family, not a
   transcription of any single manufacturer's datasheet: this library trades
   datasheet-exact dimensions for parts that always land cleanly on the
   2.54 mm grid.
 - `polarized` means "swapping the leads changes the circuit's behaviour":
   true for diodes, electrolytics and LEDs; false for everything else,
   including parts (DIPs, transistors, pots) whose pins are
   non-interchangeable for other reasons (a fixed pinout, not electrical
   polarity).
 - BodySpec.dims keys are the flexible, archetype-specific labels used
   verbatim by the TypeScript source ("rowSpacing", "tabHeight", ...), not
   snake_case: dims is a free-form dict, not a model field, so there is
   nothing here for persist.py to rename.
 - Pure and deterministic: no I/O, no clock, no randomness. Every function
   here computes its result solely from its arguments.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from .model import STANDARD_PITCH_MM, BodySpec, Footprint, FootprintPin, Mm, Point2

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

#: Default courtyard clearance beyond a footprint's pins/body: half a grid step.
COURTYARD_MARGIN_MM: Mm = STANDARD_PITCH_MM / 2


def _make_pin(number: str, d_col: int, d_row: int, name: str | None = None) -> FootprintPin:
    """Builds a FootprintPin. `name` stays None when not supplied."""
    return FootprintPin(number=number, d_col=d_col, d_row=d_row, name=name)


def _pin_mm(d_col: int, d_row: int) -> Point2:
    """Millimetre position of a grid offset, on the standard (not board-specific) pitch."""
    return Point2(d_col * STANDARD_PITCH_MM, d_row * STANDARD_PITCH_MM)


def _to_mm(pins: tuple[FootprintPin, ...]) -> list[Point2]:
    """Millimetre positions of every pin, in the same order."""
    return [_pin_mm(p.d_col, p.d_row) for p in pins]


@dataclass(frozen=True, slots=True)
class _BBox:
    min_x: Mm
    max_x: Mm
    min_y: Mm
    max_y: Mm


def _pins_bounding_box(pins_mm: list[Point2]) -> _BBox:
    """Axis-aligned bounding box of a set of mm points. Empty input yields a degenerate box at the origin."""
    if not pins_mm:
        return _BBox(0, 0, 0, 0)
    first = pins_mm[0]
    min_x = max_x = first.x
    min_y = max_y = first.y
    for p in pins_mm:
        if p.x < min_x:
            min_x = p.x
        if p.x > max_x:
            max_x = p.x
        if p.y < min_y:
            min_y = p.y
        if p.y > max_y:
            max_y = p.y
    return _BBox(min_x, max_x, min_y, max_y)


def _rect_outline(
    pins_mm: list[Point2],
    min_width_mm: Mm,
    min_height_mm: Mm,
    margin_mm: Mm,
) -> tuple[Point2, ...]:
    """Rectangular courtyard outline (4 vertices, closed implicitly), centred on the
    pins' bounding box. Never smaller than `min_width_mm` x `min_height_mm`, so it
    always covers both the physical body and the full pin span (relevant when
    leads are fanned out wider than the body to reach the grid), then adds
    `margin_mm` clearance on every side.
    """
    bbox = _pins_bounding_box(pins_mm)
    cx = (bbox.min_x + bbox.max_x) / 2
    cy = (bbox.min_y + bbox.max_y) / 2
    half_w = max(bbox.max_x - bbox.min_x, min_width_mm) / 2 + margin_mm
    half_h = max(bbox.max_y - bbox.min_y, min_height_mm) / 2 + margin_mm
    return (
        Point2(cx - half_w, cy - half_h),
        Point2(cx + half_w, cy - half_h),
        Point2(cx + half_w, cy + half_h),
        Point2(cx - half_w, cy + half_h),
    )


def _circle_outline(
    pins_mm: list[Point2],
    min_diameter_mm: Mm,
    margin_mm: Mm,
    sides: int = 24,
) -> tuple[Point2, ...]:
    """Circular courtyard outline approximated with `sides` vertices (default 24,
    divisible by 4, so the cardinal points land exactly on the bounding box edges).
    Centred on the pins' bounding box, radius large enough to cover both
    `min_diameter_mm` and the farthest pin, plus `margin_mm` clearance.
    """
    bbox = _pins_bounding_box(pins_mm)
    cx = (bbox.min_x + bbox.max_x) / 2
    cy = (bbox.min_y + bbox.max_y) / 2
    max_dist = min_diameter_mm / 2
    for p in pins_mm:
        d = math.hypot(p.x - cx, p.y - cy)
        if d > max_dist:
            max_dist = d
    r = max_dist + margin_mm
    pts: list[Point2] = []
    for i in range(sides):
        theta = (2 * math.pi * i) / sides
        pts.append(Point2(cx + r * math.cos(theta), cy + r * math.sin(theta)))
    return tuple(pts)


def _format_mm_token(value: Mm) -> str:
    """Compact, deterministic token for a value in an auto-generated id/name: "5", "6.3"."""
    if value == int(value):
        return str(int(value))
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text


# ---------------------------------------------------------------------------
# Axial two-lead parts: resistors, DO-41 / DO-35 diodes
# ---------------------------------------------------------------------------


def axial_footprint(
    *,
    span_holes: int,
    body_length_mm: Mm,
    body_diameter_mm: Mm,
    lead_diameter_mm: Mm | None = None,
    polarized: bool = False,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """Axial two-lead part lying flat on the board: resistors, DO-41/DO-35 diodes.
    Pin 1 is the anchor at (0,0); pin 2 sits `span_holes` grid steps to the right on
    the same row. The body is centred between the leads and the courtyard covers
    the full lead span even when it is longer than the body itself.
    """
    lead_diameter = 0.5 if lead_diameter_mm is None else lead_diameter_mm
    pins = (_make_pin("1", 0, 0), _make_pin("2", span_holes, 0))
    outline = _rect_outline(_to_mm(pins), body_length_mm, body_diameter_mm, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else (
        f"axial-{span_holes}h-{_format_mm_token(body_length_mm)}x{_format_mm_token(body_diameter_mm)}"
    )
    fp_name = name if name is not None else (
        f"Axial ({span_holes}-hole span, {body_length_mm}x{body_diameter_mm} mm body)"
    )
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins,
        body_outline=outline,
        body_height=body_diameter_mm,
        body=BodySpec(archetype="axial-cylinder", dims={"length": body_length_mm, "diameter": body_diameter_mm}),
        lead_diameter=lead_diameter,
        polarized=polarized,
    )


# ---------------------------------------------------------------------------
# Radial electrolytic capacitors
# ---------------------------------------------------------------------------


def radial_electrolytic_footprint(
    *,
    pitch_holes: int,
    can_diameter_mm: Mm,
    can_height_mm: Mm,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """Radial electrolytic capacitor: a round can standing upright on two leads.
    Pin 1 (the anchor, "+") sits at (0,0); pin 2 ("-") sits `pitch_holes` grid
    steps to the right.
    """
    lead_diameter = 0.5 if lead_diameter_mm is None else lead_diameter_mm
    pins = (_make_pin("1", 0, 0, "+"), _make_pin("2", pitch_holes, 0, "-"))
    outline = _circle_outline(_to_mm(pins), can_diameter_mm, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else f"c-elec-d{_format_mm_token(can_diameter_mm)}-p{pitch_holes}"
    fp_name = name if name is not None else f"Electrolytic capacitor, {can_diameter_mm} mm dia, {pitch_holes}-hole pitch"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins,
        body_outline=outline,
        body_height=can_height_mm,
        body=BodySpec(archetype="radial-electrolytic", dims={"diameter": can_diameter_mm, "height": can_height_mm}),
        lead_diameter=lead_diameter,
        polarized=True,
    )


# ---------------------------------------------------------------------------
# Disc ceramic capacitors
# ---------------------------------------------------------------------------


def disc_ceramic_footprint(
    *,
    pitch_holes: int,
    body_diameter_mm: Mm,
    body_thickness_mm: Mm | None = None,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """Disc ceramic capacitor: a flat round disc standing on edge, two leads down.
    Pin 1 is the anchor at (0,0); pin 2 sits `pitch_holes` grid steps to the right.
    """
    body_thickness = 2.5 if body_thickness_mm is None else body_thickness_mm
    lead_diameter = 0.5 if lead_diameter_mm is None else lead_diameter_mm
    pins = (_make_pin("1", 0, 0), _make_pin("2", pitch_holes, 0))
    outline = _rect_outline(_to_mm(pins), body_diameter_mm, body_thickness, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else f"c-disc-d{_format_mm_token(body_diameter_mm)}-p{pitch_holes}"
    fp_name = name if name is not None else f"Disc ceramic capacitor, {body_diameter_mm} mm dia, {pitch_holes}-hole pitch"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins,
        body_outline=outline,
        body_height=body_diameter_mm,
        body=BodySpec(archetype="disc-ceramic", dims={"diameter": body_diameter_mm, "thickness": body_thickness}),
        lead_diameter=lead_diameter,
        polarized=False,
    )


# ---------------------------------------------------------------------------
# Boxed film capacitors
# ---------------------------------------------------------------------------


def box_film_capacitor_footprint(
    *,
    pitch_holes: int,
    body_length_mm: Mm,
    body_width_mm: Mm,
    body_height_mm: Mm,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """Boxed film capacitor: a rectangular block, two leads down from the bottom.
    Pin 1 is the anchor at (0,0); pin 2 sits `pitch_holes` grid steps to the right.
    """
    lead_diameter = 0.5 if lead_diameter_mm is None else lead_diameter_mm
    pins = (_make_pin("1", 0, 0), _make_pin("2", pitch_holes, 0))
    outline = _rect_outline(_to_mm(pins), body_length_mm, body_width_mm, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else (
        f"c-film-{_format_mm_token(body_length_mm)}x{_format_mm_token(body_width_mm)}-p{pitch_holes}"
    )
    fp_name = name if name is not None else f"Film capacitor, {body_length_mm}x{body_width_mm} mm, {pitch_holes}-hole pitch"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins,
        body_outline=outline,
        body_height=body_height_mm,
        body=BodySpec(
            archetype="box-film",
            dims={"length": body_length_mm, "width": body_width_mm, "height": body_height_mm},
        ),
        lead_diameter=lead_diameter,
        polarized=False,
    )


# ---------------------------------------------------------------------------
# DIP packages
# ---------------------------------------------------------------------------


def dip_footprint(
    *,
    pin_count: int,
    wide: bool = False,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """DIP package: two rows of pins, `pin_count / 2` per side, rows 3 grid holes
    apart (0.3" narrow, the standard) or 6 holes apart (0.6" wide, `wide=True`).

    Pin 1 is the anchor at (0,0), at the top of the left column. Numbering runs
    counter-clockwise as on a real DIP viewed from above with pin 1 at the top
    left: 1..pin_count/2 go DOWN the left column (d_col 0), then pin_count/2+1..
    pin_count go back UP the right column (d_col = row spacing), so the highest
    pin number ends up beside pin 1 at the top of the package, same as the real
    part's pin-1 notch marks both ends of that row.
    """
    if not isinstance(pin_count, int) or pin_count < 4 or pin_count % 2 != 0:
        raise ValueError(f"dip_footprint: pin_count must be an even integer >= 4 (got {pin_count}).")
    per_side = pin_count // 2
    row_spacing_holes = 6 if wide else 3
    lead_diameter = 0.46 if lead_diameter_mm is None else lead_diameter_mm

    pins: list[FootprintPin] = []
    for i in range(per_side):
        pins.append(_make_pin(str(i + 1), 0, i))
    for i in range(per_side):
        number = str(per_side + i + 1)
        d_row = per_side - 1 - i
        pins.append(_make_pin(number, row_spacing_holes, d_row))
    pins_t = tuple(pins)

    outline = _rect_outline(_to_mm(pins_t), 0, 0, COURTYARD_MARGIN_MM)
    row_spacing_mm = row_spacing_holes * STANDARD_PITCH_MM
    # Approximate realistic package dims: body is a little narrower than the row
    # spacing and a little longer than the pin column span (the plastic overhangs
    # the outermost pins at each end). Not tied to a specific datasheet.
    body_length_mm = (per_side - 1) * STANDARD_PITCH_MM + 2.2
    body_width_mm = row_spacing_mm - 0.2

    fp_id = id if id is not None else f"dip-{pin_count}{'-wide' if wide else ''}"
    wide_suffix = ' (0.6" wide)' if wide else ""
    fp_name = name if name is not None else f"DIP-{pin_count}{wide_suffix}"

    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins_t,
        body_outline=outline,
        body_height=5,
        body=BodySpec(
            archetype="dip",
            dims={"length": body_length_mm, "width": body_width_mm, "rowSpacing": row_spacing_mm},
        ),
        lead_diameter=lead_diameter,
        polarized=False,
    )


# ---------------------------------------------------------------------------
# TO-92
# ---------------------------------------------------------------------------


def to92_footprint(
    *,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """TO-92 transistor package, inline-on-grid variant: 3 pins in a row, one hole
    apart. Real TO-92 leads are much closer together at the body and fan out to
    reach this pitch; the courtyard therefore covers the full fanned-out pin span,
    not just the (narrower) physical body, since that is what actually needs
    clearance on the board.
    """
    lead_diameter = 0.45 if lead_diameter_mm is None else lead_diameter_mm
    pins = (_make_pin("1", 0, 0), _make_pin("2", 1, 0), _make_pin("3", 2, 0))
    outline = _rect_outline(_to_mm(pins), 4.5, 3.7, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else "to92"
    fp_name = name if name is not None else "TO-92 (inline, on-grid)"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins,
        body_outline=outline,
        body_height=5.2,
        body=BodySpec(archetype="to92", dims={"width": 4.5, "depth": 3.7}),
        lead_diameter=lead_diameter,
        polarized=False,
    )


# ---------------------------------------------------------------------------
# TO-220
# ---------------------------------------------------------------------------


def to220_footprint(
    *,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """TO-220 power package: 3 pins in a row at 2.54 mm pitch, tall body, plus the
    metal mounting tab above it (tabHeight / tabHoleDiameter in `body.dims`).
    """
    lead_diameter = 0.7 if lead_diameter_mm is None else lead_diameter_mm
    pins = (_make_pin("1", 0, 0), _make_pin("2", 1, 0), _make_pin("3", 2, 0))
    outline = _rect_outline(_to_mm(pins), 10.0, 4.6, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else "to220"
    fp_name = name if name is not None else "TO-220"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins,
        body_outline=outline,
        body_height=20,
        body=BodySpec(
            archetype="to220",
            dims={"width": 10.0, "depth": 4.6, "tabHeight": 3.5, "tabHoleDiameter": 3.4},
        ),
        lead_diameter=lead_diameter,
        polarized=False,
    )


# ---------------------------------------------------------------------------
# LEDs
# ---------------------------------------------------------------------------

#: Typical total height above the board for each standard round LED size.
_LED_HEIGHT_BY_DIAMETER_MM: dict[int, Mm] = {3: 4.8, 5: 8.6, 10: 13.0}


def led_footprint(
    *,
    diameter_mm: int,
    body_height_mm: Mm | None = None,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """Round LED: 3, 5 or 10 mm. Pin 1 (the anchor, anode "A") sits at (0,0); pin 2
    (cathode "K", the flat-side/shorter lead) sits one hole to the right.
    """
    lead_diameter = 0.5 if lead_diameter_mm is None else lead_diameter_mm
    body_height = _LED_HEIGHT_BY_DIAMETER_MM[diameter_mm] if body_height_mm is None else body_height_mm
    pins = (_make_pin("1", 0, 0, "A"), _make_pin("2", 1, 0, "K"))
    outline = _circle_outline(_to_mm(pins), diameter_mm, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else f"led-{diameter_mm}mm"
    fp_name = name if name is not None else f"LED, {diameter_mm} mm round"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins,
        body_outline=outline,
        body_height=body_height,
        body=BodySpec(archetype="led-round", dims={"diameter": diameter_mm}),
        lead_diameter=lead_diameter,
        polarized=True,
    )


# ---------------------------------------------------------------------------
# Pin headers
# ---------------------------------------------------------------------------


def pin_header_footprint(
    *,
    rows: int,
    cols: int,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """Pin header, single or dual row, 2.54 mm pitch. Pin 1 is the anchor at (0,0).

    1xN: pins run left to right, 1, 2, 3, ... N.
    2xN: the standard zig-zag numbering used by IDC/box headers (e.g. Raspberry
    Pi GPIO): column-major, so pin 1 and pin 2 are the top/bottom pair in the
    first column, pin 3 and pin 4 the next column, and so on.
    """
    if not isinstance(cols, int) or cols < 1:
        raise ValueError(f"pin_header_footprint: cols must be a positive integer (got {cols}).")
    lead_diameter = 0.64 if lead_diameter_mm is None else lead_diameter_mm
    pins: list[FootprintPin] = []
    if rows == 1:
        for i in range(cols):
            pins.append(_make_pin(str(i + 1), i, 0))
    else:
        for i in range(cols):
            pins.append(_make_pin(str(2 * i + 1), i, 0))
            pins.append(_make_pin(str(2 * i + 2), i, 1))
    pins_t = tuple(pins)
    outline = _rect_outline(_to_mm(pins_t), 0, 0, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else f"hdr-{rows}x{cols}"
    fp_name = name if name is not None else f"Pin header, {rows}x{cols}"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins_t,
        body_outline=outline,
        body_height=8.5,
        body=BodySpec(
            archetype="pin-header",
            dims={
                "length": (cols - 1) * STANDARD_PITCH_MM,
                "width": (rows - 1) * STANDARD_PITCH_MM,
                "height": 8.5,
            },
        ),
        lead_diameter=lead_diameter,
        polarized=False,
    )


# ---------------------------------------------------------------------------
# Screw terminals
# ---------------------------------------------------------------------------


def screw_terminal_footprint(
    *,
    ways: int,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """Screw terminal block, 5.08 mm pitch (2 grid holes per way). Pin 1 is the
    anchor at (0,0).
    """
    if not isinstance(ways, int) or ways < 2:
        raise ValueError(f"screw_terminal_footprint: ways must be an integer >= 2 (got {ways}).")
    lead_diameter = 0.8 if lead_diameter_mm is None else lead_diameter_mm
    pins: list[FootprintPin] = []
    for i in range(ways):
        pins.append(_make_pin(str(i + 1), i * 2, 0))
    pins_t = tuple(pins)
    body_length_mm = ways * 5.08 + 1.5
    body_width_mm = 8.0
    outline = _rect_outline(_to_mm(pins_t), body_length_mm, body_width_mm, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else f"screw-terminal-{ways}"
    fp_name = name if name is not None else f"Screw terminal, {ways}-way, 5.08 mm pitch"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins_t,
        body_outline=outline,
        body_height=10,
        body=BodySpec(
            archetype="screw-terminal",
            dims={"length": body_length_mm, "width": body_width_mm, "height": 10},
        ),
        lead_diameter=lead_diameter,
        polarized=False,
    )


# ---------------------------------------------------------------------------
# Potentiometer
# ---------------------------------------------------------------------------


def potentiometer_footprint(
    *,
    body_diameter_mm: Mm | None = None,
    body_height_mm: Mm | None = None,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """Potentiometer, perfboard-friendly inline variant: 3 pins in a row, one hole
    apart, with the round body centred over them (real panel pots have pins that
    don't sit on a clean 2.54 mm grid; this is the on-grid approximation, same
    philosophy as to92_footprint).
    """
    body_diameter = 16 if body_diameter_mm is None else body_diameter_mm
    body_height = 10 if body_height_mm is None else body_height_mm
    lead_diameter = 0.5 if lead_diameter_mm is None else lead_diameter_mm
    pins = (_make_pin("1", 0, 0), _make_pin("2", 1, 0), _make_pin("3", 2, 0))
    outline = _circle_outline(_to_mm(pins), body_diameter, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else "pot-3"
    fp_name = name if name is not None else "Potentiometer, 3-pin inline"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins,
        body_outline=outline,
        body_height=body_height,
        body=BodySpec(archetype="potentiometer", dims={"diameter": body_diameter, "height": body_height}),
        lead_diameter=lead_diameter,
        polarized=False,
    )


# ---------------------------------------------------------------------------
# Tactile switch
# ---------------------------------------------------------------------------


def tactile_switch_footprint(
    *,
    body_size_mm: Mm | None = None,
    body_height_mm: Mm | None = None,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """Tactile switch, perfboard-friendly 4-pin variant: pins at the corners of a
    2-hole x 1-hole rectangle (pins 1/2 on the left, both the same node; pins
    3/4 on the right, both the same node) - the on-grid approximation of the
    common 4-leg tactile switch.
    """
    body_size = 6.0 if body_size_mm is None else body_size_mm
    body_height = 4.3 if body_height_mm is None else body_height_mm
    lead_diameter = 0.5 if lead_diameter_mm is None else lead_diameter_mm
    pins = (_make_pin("1", 0, 0), _make_pin("2", 0, 1), _make_pin("3", 2, 0), _make_pin("4", 2, 1))
    outline = _rect_outline(_to_mm(pins), body_size, body_size, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else "sw-tactile"
    fp_name = name if name is not None else "Tactile switch, 4-pin"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins,
        body_outline=outline,
        body_height=body_height,
        body=BodySpec(archetype="tactile-switch", dims={"width": body_size, "depth": body_size}),
        lead_diameter=lead_diameter,
        polarized=False,
    )


# ---------------------------------------------------------------------------
# HC-49 crystal
# ---------------------------------------------------------------------------


def crystal_hc49_footprint(
    *,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """HC-49/U crystal, mounted standing upright on 2 leads, 2 holes apart (5.08 mm;
    the real lead pitch is 4.88 mm, close enough that this is the standard
    perfboard approximation).
    """
    lead_diameter = 0.45 if lead_diameter_mm is None else lead_diameter_mm
    pins = (_make_pin("1", 0, 0), _make_pin("2", 2, 0))
    outline = _rect_outline(_to_mm(pins), 4.65, 3.5, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else "xtal-hc49"
    fp_name = name if name is not None else "Crystal, HC-49/U"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins,
        body_outline=outline,
        body_height=13.46,
        body=BodySpec(archetype="crystal-hc49", dims={"width": 4.65, "depth": 3.5}),
        lead_diameter=lead_diameter,
        polarized=False,
    )


# ---------------------------------------------------------------------------
# Small relay
# ---------------------------------------------------------------------------


def relay_footprint(
    *,
    lead_diameter_mm: Mm | None = None,
    id: str | None = None,
    name: str | None = None,
) -> Footprint:
    """Small SPDT relay (e.g. Songle SRD-05VDC-SL-C style), on-grid approximation:
    2 coil pins in one column (2 holes apart), 3 switch pins (NO/COM/NC) in a
    second column 5 holes over (1 hole apart from each other). Real relay
    pinouts are rarely a clean 2.54 mm grid; verify against your specific part's
    datasheet before relying on this for anything but layout planning.
    """
    lead_diameter = 0.6 if lead_diameter_mm is None else lead_diameter_mm
    pins = (
        _make_pin("1", 0, 0),  # coil
        _make_pin("2", 0, 2),  # coil
        _make_pin("3", 5, 0),  # NO
        _make_pin("4", 5, 1),  # COM
        _make_pin("5", 5, 2),  # NC
    )
    outline = _rect_outline(_to_mm(pins), 19, 15, COURTYARD_MARGIN_MM)
    fp_id = id if id is not None else "relay-spdt"
    fp_name = name if name is not None else "Small SPDT relay"
    return Footprint(
        id=fp_id,
        name=fp_name,
        pins=pins,
        body_outline=outline,
        body_height=15,
        body=BodySpec(archetype="relay-box", dims={"length": 19, "width": 15}),
        lead_diameter=lead_diameter,
        polarized=False,
    )


# ---------------------------------------------------------------------------
# Standard registry
# ---------------------------------------------------------------------------


def _build_standard_footprints() -> dict[str, Footprint]:
    footprint_list: list[Footprint] = []

    # Resistors: axial, 4 standard lead spans.
    resistor_spans = (
        (3, 5.0, 2.0),
        (4, 6.3, 2.3),
        (5, 6.3, 2.5),
        (6, 9.0, 3.6),
    )
    for span, length, diameter in resistor_spans:
        footprint_list.append(
            axial_footprint(
                span_holes=span,
                body_length_mm=length,
                body_diameter_mm=diameter,
                id=f"r-axial-{span}",
                name=f"Resistor (axial, {span}-hole span)",
            )
        )

    # Diodes: DO-41 / DO-35, axial and polarized.
    footprint_list.append(
        axial_footprint(
            span_holes=4,
            body_length_mm=5.2,
            body_diameter_mm=2.7,
            polarized=True,
            id="d-do41",
            name="Diode, DO-41",
        )
    )
    footprint_list.append(
        axial_footprint(
            span_holes=3,
            body_length_mm=3.5,
            body_diameter_mm=2.0,
            polarized=True,
            id="d-do35",
            name="Diode, DO-35",
        )
    )

    # Radial electrolytic capacitors.
    electrolytics = (
        (5, 7, 2),
        (6.3, 11, 2),
        (8, 11.5, 3),
        (10, 12.5, 3),
    )
    for dia, height, pitch in electrolytics:
        footprint_list.append(
            radial_electrolytic_footprint(
                pitch_holes=pitch,
                can_diameter_mm=dia,
                can_height_mm=height,
                id=f"c-elec-d{_format_mm_token(dia)}-p{pitch}",
                name=f"Electrolytic capacitor, {dia} mm dia, {pitch}-hole pitch",
            )
        )

    # Disc ceramic capacitors.
    footprint_list.append(
        disc_ceramic_footprint(
            pitch_holes=2,
            body_diameter_mm=5,
            id="c-disc-p2",
            name="Disc ceramic capacitor, 2-hole pitch",
        )
    )
    footprint_list.append(
        disc_ceramic_footprint(
            pitch_holes=3,
            body_diameter_mm=7.5,
            id="c-disc-p3",
            name="Disc ceramic capacitor, 3-hole pitch",
        )
    )

    # Boxed film capacitors.
    footprint_list.append(
        box_film_capacitor_footprint(
            pitch_holes=2,
            body_length_mm=7,
            body_width_mm=4,
            body_height_mm=6,
            id="c-film-p2",
            name="Film capacitor, 2-hole pitch",
        )
    )
    footprint_list.append(
        box_film_capacitor_footprint(
            pitch_holes=3,
            body_length_mm=10,
            body_width_mm=5,
            body_height_mm=8,
            id="c-film-p3",
            name="Film capacitor, 3-hole pitch",
        )
    )

    # DIP packages, standard 0.3" narrow row spacing.
    dip_counts = (8, 14, 16, 18, 20, 28, 40)
    for n in dip_counts:
        footprint_list.append(dip_footprint(pin_count=n, id=f"dip-{n}", name=f"DIP-{n}"))
    # DIP-40 also commonly comes in a 0.6" wide body.
    footprint_list.append(
        dip_footprint(pin_count=40, wide=True, id="dip-40-wide", name='DIP-40 (0.6" wide)')
    )

    # TO-92, TO-220.
    footprint_list.append(to92_footprint(id="to92", name="TO-92"))
    footprint_list.append(to220_footprint(id="to220", name="TO-220"))

    # LEDs.
    for dia in (3, 5, 10):
        footprint_list.append(led_footprint(diameter_mm=dia, id=f"led-{dia}mm", name=f"LED, {dia} mm round"))

    # Pin headers, 1xN and 2xN, over a sensible range of common sizes.
    header_counts = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 20)
    for n in header_counts:
        footprint_list.append(
            pin_header_footprint(rows=1, cols=n, id=f"hdr-1x{n}", name=f"Pin header, 1x{n}")
        )
        footprint_list.append(
            pin_header_footprint(rows=2, cols=n, id=f"hdr-2x{n}", name=f"Pin header, 2x{n}")
        )

    # Screw terminals, 5.08 mm pitch.
    footprint_list.append(
        screw_terminal_footprint(ways=2, id="screw-terminal-2", name="Screw terminal, 2-way")
    )
    footprint_list.append(
        screw_terminal_footprint(ways=3, id="screw-terminal-3", name="Screw terminal, 3-way")
    )

    # Potentiometer, tactile switch, crystal, relay.
    footprint_list.append(potentiometer_footprint(id="pot-3", name="Potentiometer, 3-pin inline"))
    footprint_list.append(tactile_switch_footprint(id="sw-tactile", name="Tactile switch, 4-pin"))
    footprint_list.append(crystal_hc49_footprint(id="xtal-hc49", name="Crystal, HC-49/U"))
    footprint_list.append(relay_footprint(id="relay-spdt", name="Small SPDT relay"))

    registry: dict[str, Footprint] = {}
    for fp in footprint_list:
        if fp.id in registry:
            raise ValueError(f"Duplicate footprint id in standard registry: {fp.id}")
        registry[fp.id] = fp
    return registry


_cached_registry: dict[str, Footprint] | None = None


def standard_footprints() -> dict[str, Footprint]:
    """The full standard footprint library, keyed by id. Built once (lazily) and
    cached: construction is pure and deterministic, so sharing the same dict
    instance across calls is safe and avoids rebuilding ~60 footprints per call.
    """
    global _cached_registry
    if _cached_registry is None:
        _cached_registry = _build_standard_footprints()
    return _cached_registry


def get_footprint(id: str) -> Footprint | None:
    """Looks up a single standard footprint by id."""
    return standard_footprints().get(id)


def footprint_lookup() -> Callable[[str], Footprint | None]:
    """A lookup function over the standard registry, in the shape connectivity.py
    and the DRC/LVS modules expect (`(footprint_id) -> Footprint | None`).
    """
    return lambda id: get_footprint(id)
