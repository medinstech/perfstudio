"""The generated sheet as a file: SVG, and through it PDF and PNG.

``schematic.py`` decides what is on the sheet; this file decides what that looks like on
paper, and knows nothing about perfboard. Same shape as ``guide_export.py``, and the same
rule: pure, deterministic, stdlib only -- no Qt, no clock, no filesystem, strings out. That
is what lets an SVG be compared against a golden file rather than looked at, and what lets
the MCP server and a headless run produce the same sheet the desktop app does.

**SVG IS THE ONLY RENDERER FOR PAPER, AND PDF AND PNG COME OUT OF IT.**
``ui/export_schematic.py`` hands this string to Qt's SVG renderer to paginate or
rasterise it. Three writers over one ``SchematicDrawing`` would be three chances for the
printed sheet, the emailed PNG and the embedded SVG to disagree about what the circuit is,
and the one thing worse than no export is three exports that differ.

**IT IS NOT A SECOND COPY OF THE PANEL, THOUGH, AND THAT IS DELIBERATE.** ``ui/viewsch.py``
paints for a screen and this writes for paper, which are different jobs in two measurable
ways. Screen labels hold a PIXEL size, because a reference that shrank to nothing when the
sheet was fitted to the panel would make the fitted view the one view that says nothing
(``ui/scenetext.py`` sets this out at length); paper labels are millimetres of sheet,
because paper does not zoom -- exactly the split ``export_pdf`` already makes against
``scenetext``. And the panel is light ink on a dark sheet, which is right for a screen at
midnight and wrong for every printer. What the two must NOT decide separately is geometry,
which is why the rail glyph's bars come from ``schematic.rail_glyph_bars`` rather than from
a constant in each file.

**INK ON WHITE, MONOCHROME BY DEFAULT.** The panel colours the three net classes; a printed
sheet does not, and the reason is already written down in the rail glyph: a schematic is
read at a glance and often printed in black, so a reader should never have to know a colour
convention to tell a rail that sinks from one that sources. The glyphs say it, the labels
say it, and the sheet then survives a photocopier. ``SheetInk`` keeps the three classes as
separate fields so a caller that knows its output is a screen can colour them; they are the
same black by default because most outputs are not.

**THE NOTES ARE PRINTED WITH THE SHEET.** ``SchematicDrawing.notes`` is where a pin the
netlist names and the footprint does not, or a part nothing defines, ends up. Dropping them
on the way to paper would hand somebody a picture of a circuit nobody has -- the exact thing
``schematic.py`` built the field to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from .model import Mm, Point2
from .schematic import (
    MARGIN_MM,
    Label,
    Rail,
    SchematicDrawing,
    Symbol,
    Wire,
    rail_glyph_bars,
)
from .version import __version__

# ---------------------------------------------------------------------------
# Ink
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SheetInk:
    """Colours and sizes for one exported sheet. Millimetres throughout.

    Text sizes are millimetres of sheet rather than points, for the reason the whole
    coordinate system is millimetres: a schematic exported at 1.6 mm text and printed on A3
    has 1.6 mm text, and nothing in the pipeline needs a scale factor to make that true.
    1.27 mm is KiCad's default and reads as small-but-clear on a 2.54 mm grid, so a
    reference a shade above it and a pin number a shade below is the range this stays in.
    """

    background: str = "#ffffff"
    #: Symbol bodies and reference designators. The sheet's main weight.
    ink: str = "#111111"
    #: Values and pin numbers -- present, subordinate, and still black enough to photocopy.
    dim: str = "#444444"
    #: A part nothing in the document defines. Drawn dashed as well as coloured, so the
    #: defect survives a monochrome print, which is the point of not relying on colour.
    undefined: str = "#a11414"
    signal: str = "#111111"
    power: str = "#111111"
    ground: str = "#111111"

    wire_mm: Mm = 0.25
    body_mm: Mm = 0.35
    dot_mm: Mm = 0.60
    #: On, off. A dash long enough to read as deliberate at a glance and at print size.
    dash_mm: tuple[Mm, Mm] = (1.4, 0.9)

    ref_mm: Mm = 1.7
    value_mm: Mm = 1.4
    net_mm: Mm = 1.3
    pin_mm: Mm = 1.0
    title_mm: Mm = 3.2
    note_mm: Mm = 1.6

    #: A stack that resolves to something on every platform this ships to and degrades to
    #: the system sans-serif where it does not. Named rather than left to the renderer
    #: because "whatever the default is" differs between a browser, Qt and Inkscape.
    font_family: str = "DejaVu Sans, Helvetica, Arial, sans-serif"


PAPER = SheetInk()


# ---------------------------------------------------------------------------
# Writing numbers and text
# ---------------------------------------------------------------------------


def _n(value: float) -> str:
    """A coordinate, short and stable.

    Three decimals is a thousandth of a millimetre, far finer than anything the layout
    computes on purpose and coarse enough that a platform whose libm rounds the last bit
    differently still writes the same file -- the property the golden comparison rests on
    (``tests/test_footprints.py`` learned this the hard way on macOS arm64).

    Negative zero is normalised away for the same reason: ``-0`` and ``0`` are the same
    point and would otherwise be a diff nobody caused.
    """
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _pt(point: Point2) -> str:
    return f"{_n(point.x)},{_n(point.y)}"


def _points(path: tuple[Point2, ...]) -> str:
    return " ".join(_pt(point) for point in path)


def _text(
    content: str,
    x: Mm,
    y: Mm,
    size: Mm,
    colour: str,
    anchor: str,
    ink: SheetInk,
    bold: bool = False,
) -> str:
    """One text element, positioned by its BASELINE.

    The vertical placement is arithmetic here rather than a ``dominant-baseline``
    attribute, and it has to be: Qt's SVG renderer implements SVG Tiny 1.2, which has no
    such attribute, and it is the renderer that produces the PDF. An attribute nothing
    implements does not fail -- it is ignored, and every reference lands on top of its own
    symbol on the printed sheet while the browser preview looks perfect.
    """
    weight = ' font-weight="bold"' if bold else ""
    return (
        f'<text x="{_n(x)}" y="{_n(y)}" font-size="{_n(size)}" fill="{colour}"'
        f' text-anchor="{anchor}" font-family="{escape(ink.font_family, quote=True)}"{weight}>'
        f"{escape(content)}</text>"
    )


#: ``Label.anchor`` to SVG's own word for it. Tiny 1.2 does have ``text-anchor``.
_ANCHOR = {"left": "start", "centre": "middle", "right": "end"}


def _baseline(label: Label, size: Mm) -> Mm:
    """Where the baseline goes so the label sits where the panel puts it.

    ``schematic.py`` places a label by an anchor point and a kind, and the panel turns the
    kind into a Qt alignment: a reference sits ABOVE its point, a value below it, a pin
    number centred on it. Those three readings are reproduced here in the only currency an
    SVG has, which is the baseline offset. The fractions are ordinary font metrics -- a cap
    height is about 0.7 of the em and a descender about 0.2 -- and they only have to be
    close, because a millimetre of sheet is a fifth of a grid step.
    """
    if label.kind == "value":  # below the point
        return label.at.y + size * 0.8
    if label.kind == "pin":  # centred on it
        return label.at.y + size * 0.35
    return label.at.y  # ref, net: above it


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------

#: Blank space above the sheet when a title is drawn, and the room one note takes, both as
#: multiples of their own text size. A line and a half of leading is what makes a stack of
#: notes read as a list rather than as a paragraph.
_TITLE_BAND = 2.4
_NOTE_LEADING = 1.7


def _class_colour(net_class: str, ink: SheetInk) -> str:
    if net_class == "power":
        return ink.power
    if net_class == "ground":
        return ink.ground
    return ink.signal


def _wire(run: Wire | Rail, ink: SheetInk) -> str:
    colour = _class_colour(run.net_class, ink)
    return (
        f'<polyline points="{_points(run.path)}" fill="none" '
        f'stroke="{colour}" stroke-width="{_n(ink.wire_mm)}"/>'
    )


def _glyph(rail: Rail, ink: SheetInk) -> str:
    colour = _class_colour(rail.net_class, ink)
    return "".join(
        f'<line x1="{_n(start.x)}" y1="{_n(start.y)}" x2="{_n(end.x)}" y2="{_n(end.y)}" '
        f'stroke="{colour}" stroke-width="{_n(ink.body_mm)}"/>'
        for start, end in rail_glyph_bars(rail)
    )


def _symbol(symbol: Symbol, ink: SheetInk) -> str:
    """One symbol, drawn in its own coordinates inside a translate.

    A ``transform`` rather than baked-in absolute coordinates, so the file says the same
    thing the model does -- a symbol is a shape plus a place -- and so a reader comparing an
    SVG against ``tests/schematic_golden/`` is comparing the same two numbers.
    """
    colour = ink.undefined if symbol.undefined else ink.ink
    dashes = (
        f' stroke-dasharray="{_n(ink.dash_mm[0])} {_n(ink.dash_mm[1])}"'
        if symbol.undefined
        else ""
    )
    body: list[str] = []
    for shape in symbol.shapes:
        common = f'stroke="{colour}" stroke-width="{_n(ink.body_mm)}"{dashes}'
        if shape.kind == "circle":
            centre = shape.points[0]
            body.append(
                f'<circle cx="{_n(centre.x)}" cy="{_n(centre.y)}" r="{_n(shape.radius)}" '
                f"fill=\"none\" {common}/>"
            )
        elif shape.kind == "polygon":
            fill = colour if shape.filled else "none"
            body.append(f'<polygon points="{_points(shape.points)}" fill="{fill}" {common}/>')
        else:
            body.append(f'<polyline points="{_points(shape.points)}" fill="none" {common}/>')
    return (
        f'<g transform="translate({_pt(symbol.at)})">' + "".join(body) + "</g>"
    )


def _label(label: Label, ink: SheetInk) -> str:
    if label.kind == "ref":
        size, colour, bold = ink.ref_mm, ink.ink, True
    elif label.kind == "value":
        size, colour, bold = ink.value_mm, ink.dim, False
    elif label.kind == "net":
        size, colour, bold = ink.net_mm, ink.signal, False
    else:
        size, colour, bold = ink.pin_mm, ink.dim, False
    return _text(
        label.text,
        label.at.x,
        _baseline(label, size),
        size,
        colour,
        _ANCHOR[label.anchor],
        ink,
        bold=bold,
    )


def drawing_to_svg(
    drawing: SchematicDrawing,
    title: str | None = None,
    ink: SheetInk = PAPER,
) -> str:
    """The sheet as one self-contained SVG document.

    ``title`` is the caller's -- usually the document name -- because this module has no
    filesystem and therefore no opinion about what a document is called. It is drawn in a
    band above the sheet and put in the SVG's own ``<title>``, which is what a browser tab
    and an image viewer show.

    The output carries the version that wrote it, in a ``<desc>``, for the reason
    ``guide_to_json`` does: an exported file outlives the session that produced it, and the
    first question about a sheet that looks wrong is which build drew it.
    """
    sheet_w = max(drawing.width, 2 * MARGIN_MM)
    sheet_h = max(drawing.height, 2 * MARGIN_MM)

    title_band = ink.title_mm * _TITLE_BAND if title else 0.0
    notes_band = (
        ink.note_mm * (_NOTE_LEADING * len(drawing.notes) + 1.0) if drawing.notes else 0.0
    )

    out: list[str] = [
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        f'width="{_n(sheet_w)}mm" height="{_n(sheet_h + title_band + notes_band)}mm" '
        f'viewBox="0 {_n(-title_band)} {_n(sheet_w)} {_n(sheet_h + title_band + notes_band)}">'
    ]
    out.append(f"<title>{escape(title or 'Schematic')}</title>")
    out.append(f"<desc>PerfStudio {__version__}</desc>")
    out.append(
        f'<rect x="0" y="{_n(-title_band)}" width="{_n(sheet_w)}" '
        f'height="{_n(sheet_h + title_band + notes_band)}" fill="{ink.background}"/>'
    )

    if title:
        out.append(
            _text(
                title,
                MARGIN_MM,
                -title_band + ink.title_mm,
                ink.title_mm,
                ink.ink,
                "start",
                ink,
                bold=True,
            )
        )

    # The order a draughtsman would use, and the order the panel paints in: wires first so
    # a symbol sits on top of the line reaching it, then rails, dots, bodies, text.
    out.append('<g stroke-linecap="round" stroke-linejoin="round">')
    for wire in drawing.wires:
        out.append(_wire(wire, ink))
    for rail in drawing.rails:
        out.append(_wire(rail, ink))
        out.append(_glyph(rail, ink))
    for junction in drawing.junctions:
        out.append(
            f'<circle cx="{_n(junction.at.x)}" cy="{_n(junction.at.y)}" '
            f'r="{_n(ink.dot_mm)}" fill="{ink.signal}"/>'
        )
    for symbol in drawing.symbols:
        out.append(_symbol(symbol, ink))
    out.append("</g>")

    for label in drawing.labels:
        out.append(_label(label, ink))

    for index, note in enumerate(drawing.notes):
        out.append(
            _text(
                f"· {note}",
                MARGIN_MM,
                sheet_h + ink.note_mm * (1.0 + _NOTE_LEADING * index),
                ink.note_mm,
                ink.dim,
                "start",
                ink,
            )
        )

    out.append("</svg>")
    return "\n".join(out) + "\n"


__all__ = ["PAPER", "SheetInk", "drawing_to_svg"]
