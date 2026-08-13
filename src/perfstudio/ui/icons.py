"""Toolbar icons, drawn in code.

WHY NOT A SET OF SVG FILES. Twenty line drawings as assets would mean a Qt resource
system or a data directory to find at runtime, a licence to track, and a second place
where the palette lives -- and the first time the chrome colour changed, half of them
would be the old grey. These are drawn with the same ``theme`` constants every panel uses,
so an icon cannot fall out of step with the window around it.

Each drawing works in a 100 x 100 box and knows nothing about the icon's real size, so
the same function serves a 22 px toolbar and a hidpi screen. The pixmap is rendered at
twice the requested size and told so, which is what keeps the strokes sharp on a 200%
display instead of blurred like a scaled bitmap.

WHAT THE DRAWINGS SAY. Every conductor icon shows the SAME two pads and differs only in
what runs between them -- a solid bar for solder, a thin line for bare wire, a sleeved
line for insulated, an arc that lifts over the board for a top jumper. That is the one
distinction this application is about (``model.ConductorKind``), so it is the distinction
the icons are built around rather than four unrelated pictures.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from .theme import ACCENT, TEXT

#: Logical icon size. 22 px is the toolbar's, and the only size anything asks for.
SIZE = 22

#: Everything is drawn in this box and scaled down, so the code reads in round numbers.
BOX = 100.0

#: Stroke width in box units: 8/100 of the icon, which lands on a crisp ~1.8 px at 22 px.
STROKE = 8.0

_cache: dict[str, QIcon] = {}


def _pen(colour: str, width: float = STROKE) -> QPen:
    pen = QPen(QColor(colour), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _pad(painter: QPainter, x: float, y: float, r: float = 11.0) -> None:
    """A pad: the ring every conductor icon starts and ends on."""
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QPointF(x, y), r, r)


def _arrow_head(painter: QPainter, tip: QPointF, dx: float, dy: float) -> None:
    """A two-stroke head at ``tip``, opening back along (dx, dy)."""
    painter.drawLine(tip, QPointF(tip.x() + dx, tip.y() + dy))
    painter.drawLine(tip, QPointF(tip.x() + dy, tip.y() - dx))


# ---------------------------------------------------------------------------
# The drawings
# ---------------------------------------------------------------------------


def _save(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(14, 14, 72, 72), 8, 8)
    p.drawRect(QRectF(34, 14, 32, 26))  # the shutter
    p.drawRect(QRectF(28, 56, 44, 30))  # the label


def _turn_arrow(p: QPainter, leftward: bool) -> None:
    """The undo/redo curve: over the top, with the head on the end it points at.

    One function for both, mirrored about the centre, so the pair cannot drift into
    looking like two different gestures.
    """
    # The drawing below points LEFT, which is undo; redo is the mirror of it.
    sign = 1.0 if leftward else -1.0

    def x(value: float) -> float:
        return 50 + sign * (value - 50)

    path = QPainterPath(QPointF(x(78), 74))
    path.cubicTo(QPointF(x(88), 26), QPointF(x(34), 18), QPointF(x(24), 46))
    p.drawPath(path)
    # The head sits on the tip and opens back along the curve, which at this point is
    # heading down and outwards.
    tip = QPointF(x(24), 46)
    p.drawLine(tip, QPointF(x(46), 42))
    p.drawLine(tip, QPointF(x(26), 22))


def _undo(p: QPainter) -> None:
    _turn_arrow(p, leftward=True)


def _redo(p: QPainter) -> None:
    _turn_arrow(p, leftward=False)


def _conductor(p: QPainter, run: Callable[[QPainter], None]) -> None:
    """Two pads with something between them: the shape every conductor icon shares."""
    _pad(p, 22, 72)
    _pad(p, 78, 28)
    run(p)


def _solder_trace(p: QPainter) -> None:
    def run(p: QPainter) -> None:
        p.setPen(_pen(TEXT, 15))  # thick: solder is the widest thing on the board
        p.drawLine(QPointF(30, 64), QPointF(70, 36))

    _conductor(p, run)


def _solder_trace_wired(p: QPainter) -> None:
    """The same run with a wire spine down it -- the whole difference between the two."""

    def run(p: QPainter) -> None:
        p.setPen(_pen(TEXT, 15))
        p.drawLine(QPointF(30, 64), QPointF(70, 36))
        p.setPen(_pen(ACCENT, 5))
        p.drawLine(QPointF(28, 62), QPointF(72, 34))

    _conductor(p, run)


def _bare_wire(p: QPainter) -> None:
    def run(p: QPainter) -> None:
        p.setPen(_pen(TEXT, 6))
        p.drawLine(QPointF(30, 64), QPointF(70, 36))

    _conductor(p, run)


def _insulated_wire(p: QPainter) -> None:
    def run(p: QPainter) -> None:
        p.setPen(_pen(TEXT, 6))
        p.drawLine(QPointF(30, 64), QPointF(70, 36))
        # The sleeve, which is the whole difference between this and bare wire.
        p.setPen(_pen(TEXT, 16))
        p.drawLine(QPointF(42, 55), QPointF(58, 45))

    _conductor(p, run)


def _top_jumper(p: QPainter) -> None:
    def run(p: QPainter) -> None:
        path = QPainterPath(QPointF(26, 66))
        path.cubicTo(QPointF(30, 16), QPointF(70, 16), QPointF(74, 32))
        p.setPen(_pen(TEXT, 6))
        p.drawPath(path)

    _conductor(p, run)


def _new_net(p: QPainter) -> None:
    _pad(p, 30, 74, 10)
    _pad(p, 76, 40, 10)
    p.drawLine(QPointF(38, 68), QPointF(68, 46))
    p.setPen(_pen(ACCENT, 10))
    p.drawLine(QPointF(14, 26), QPointF(42, 26))
    p.drawLine(QPointF(28, 12), QPointF(28, 40))


def _connect(p: QPainter) -> None:
    """Two pads and the link between them, with the second pad picked out.

    The accent is doing real work: this is the two-click tool, and the icon says the
    second click is the one that makes the connection.
    """
    _pad(p, 24, 70, 11)
    p.drawLine(QPointF(33, 62), QPointF(67, 38))
    p.setPen(_pen(ACCENT))
    _pad(p, 76, 30, 11)


def _autoroute(p: QPainter) -> None:
    _pad(p, 18, 78, 9)
    _pad(p, 82, 22, 9)
    p.setPen(_pen(ACCENT, 7))
    path = QPainterPath(QPointF(26, 78))
    for point in (QPointF(50, 78), QPointF(50, 50), QPointF(72, 50), QPointF(72, 30)):
        path.lineTo(point)
    p.drawPath(path)


def _autoplace(p: QPainter) -> None:
    """A part moved into its place: where it was, where it goes, and the move."""
    pen = _pen(TEXT, 6)
    pen.setStyle(Qt.PenStyle.DashLine)
    p.setPen(pen)
    p.drawRoundedRect(QRectF(10, 14, 36, 24), 4, 4)
    p.setPen(_pen(TEXT))
    p.drawRoundedRect(QRectF(54, 58, 36, 24), 4, 4)
    p.setPen(_pen(ACCENT, 7))
    p.drawLine(QPointF(40, 44), QPointF(62, 56))
    p.drawLine(QPointF(62, 56), QPointF(44, 56))
    p.drawLine(QPointF(62, 56), QPointF(62, 38))


def _rotate(p: QPainter) -> None:
    p.drawArc(QRectF(20, 20, 60, 60), 40 * 16, 270 * 16)
    _arrow_head(p, QPointF(72, 26), -4, 20)


def _mirror(p: QPainter) -> None:
    pen = _pen(TEXT, 5)
    pen.setStyle(Qt.PenStyle.DashLine)
    p.setPen(pen)
    p.drawLine(QPointF(50, 10), QPointF(50, 90))
    p.setPen(_pen(TEXT))
    for direction in (-1, 1):
        path = QPainterPath(QPointF(50 + 12 * direction, 24))
        path.lineTo(QPointF(50 + 34 * direction, 50))
        path.lineTo(QPointF(50 + 12 * direction, 76))
        path.closeSubpath()
        p.drawPath(path)


def _flip(p: QPainter) -> None:
    """The board turned over: the face you are on, the face you would get, and the turn.

    Drawn as two boards side by side rather than one board with an arrow through it,
    because the thing being switched is WHICH SIDE you are looking at, and a picture of a
    single board says nothing about there being two of them.
    """
    p.drawRoundedRect(QRectF(10, 44, 34, 44), 5, 5)
    pen = _pen(TEXT, 6)
    pen.setStyle(Qt.PenStyle.DashLine)
    p.setPen(pen)
    p.drawRoundedRect(QRectF(56, 44, 34, 44), 5, 5)
    p.setPen(_pen(ACCENT, 7))
    path = QPainterPath(QPointF(20, 36))
    path.cubicTo(QPointF(34, 8), QPointF(66, 8), QPointF(80, 36))
    p.drawPath(path)
    p.drawLine(QPointF(80, 36), QPointF(62, 32))
    p.drawLine(QPointF(80, 36), QPointF(80, 16))


def _ratsnest(p: QPainter) -> None:
    for x, y in ((22, 30), (78, 26), (34, 78), (76, 74)):
        p.setBrush(QColor(TEXT))
        p.drawEllipse(QPointF(x, y), 7, 7)
    p.setBrush(Qt.BrushStyle.NoBrush)
    pen = _pen(ACCENT, 6)
    pen.setStyle(Qt.PenStyle.DashLine)
    p.setPen(pen)
    p.drawLine(QPointF(22, 30), QPointF(78, 26))
    p.drawLine(QPointF(22, 30), QPointF(34, 78))
    p.drawLine(QPointF(34, 78), QPointF(76, 74))


def _cube(p: QPainter) -> None:
    top = [QPointF(50, 12), QPointF(86, 32), QPointF(50, 52), QPointF(14, 32)]
    path = QPainterPath(top[0])
    for point in top[1:]:
        path.lineTo(point)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QPointF(14, 32), QPointF(14, 68))
    p.drawLine(QPointF(86, 32), QPointF(86, 68))
    p.drawLine(QPointF(50, 52), QPointF(50, 88))
    p.drawLine(QPointF(14, 68), QPointF(50, 88))
    p.drawLine(QPointF(86, 68), QPointF(50, 88))


def _fit(p: QPainter) -> None:
    for x, y, dx, dy in ((16, 16, 1, 1), (84, 16, -1, 1), (16, 84, 1, -1), (84, 84, -1, -1)):
        p.drawLine(QPointF(x, y), QPointF(x + 22 * dx, y))
        p.drawLine(QPointF(x, y), QPointF(x, y + 22 * dy))
    p.drawRect(QRectF(38, 38, 24, 24))


def _delete(p: QPainter) -> None:
    p.drawLine(QPointF(16, 26), QPointF(84, 26))
    p.drawLine(QPointF(40, 26), QPointF(40, 16))
    p.drawLine(QPointF(60, 26), QPointF(60, 16))
    p.drawLine(QPointF(40, 16), QPointF(60, 16))
    path = QPainterPath(QPointF(24, 26))
    path.lineTo(QPointF(30, 88))
    path.lineTo(QPointF(70, 88))
    path.lineTo(QPointF(76, 26))
    p.drawPath(path)


def _import(p: QPainter) -> None:
    """A netlist arriving from somewhere else: a sheet, and an arrow into the board."""
    p.drawRoundedRect(QRectF(12, 14, 44, 56), 5, 5)
    p.setPen(_pen(ACCENT, 7))
    p.drawLine(QPointF(46, 78), QPointF(84, 78))
    _arrow_head(p, QPointF(86, 78), -16, -8)


def _guide(p: QPainter) -> None:
    """The build guide: a numbered list."""
    p.drawRoundedRect(QRectF(18, 12, 64, 76), 6, 6)
    p.setPen(_pen(ACCENT, 7))
    for y in (34, 52, 70):
        p.drawLine(QPointF(32, y), QPointF(68, y))


#: Name -> drawing. The names are what the toolbar and the menus ask for.
DRAWINGS: dict[str, Callable[[QPainter], None]] = {
    "save": _save,
    "undo": _undo,
    "redo": _redo,
    "import": _import,
    "guide": _guide,
    "solder-trace": _solder_trace,
    "solder-trace-wired": _solder_trace_wired,
    "bare-wire": _bare_wire,
    "insulated-wire": _insulated_wire,
    "top-jumper": _top_jumper,
    "new-net": _new_net,
    "connect": _connect,
    "autoroute": _autoroute,
    "autoplace": _autoplace,
    "rotate": _rotate,
    "mirror": _mirror,
    "flip": _flip,
    "ratsnest": _ratsnest,
    "3d": _cube,
    "fit": _fit,
    "delete": _delete,
}


def icon(name: str) -> QIcon:
    """The named icon, drawn once and kept.

    An unknown name gives an empty icon rather than raising: a missing picture is a
    cosmetic fault, and taking the window down over one would not be.
    """
    cached = _cache.get(name)
    if cached is not None:
        return cached

    draw = DRAWINGS.get(name)
    if draw is None:
        return QIcon()

    scale = 2  # Drawn at 2x and told so, which is what keeps it sharp on a hidpi screen.
    pixmap = QPixmap(SIZE * scale, SIZE * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(SIZE * scale / BOX, SIZE * scale / BOX)
    painter.setPen(_pen(TEXT))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    draw(painter)
    painter.end()
    pixmap.setDevicePixelRatio(float(scale))

    built = QIcon(pixmap)
    _cache[name] = built
    return built


# ---------------------------------------------------------------------------
# Part pictures
# ---------------------------------------------------------------------------
#
# The library listed sixty-one parts as sixty-one lines of grey text, which is a poor way
# to answer the question people actually bring to it: "which of these is the fat blue one
# with a stripe". So each row now carries a small picture of the part.
#
# THE COLOURS ARE NOT DECORATION AND THEY ARE NOT CHOSEN HERE. Every one comes from
# ``bodies.style_for`` -- the same fill, edge and accent the 2D board and the 3D view draw
# that part with. An electrolytic is the same dark blue in the library, on the board and in
# the render, so picking one from the list and finding it on the board is recognition
# rather than reading. A second palette would drift the first time either was touched.
#
# The VIEW is per archetype, and deliberately not the board's top-down one: a resistor is
# recognised side-on by its bands, a DIP from above by its notch. These are pictures of
# parts as they sit in a drawer, because that is the question this list answers -- the
# board next to it is what shows the footprint.


#: Icon size for a tree row. Smaller than the toolbar's, which is a button.
PART_SIZE = 20

#: Tinned component lead. Matches ``view2d.LEAD``, kept local so the icons do not drag in
#: the whole 2D scene module to draw a wire.
LEAD = "#c2c8d0"

_part_cache: dict[str, QIcon] = {}


def _fill(p: QPainter, style: Any, colour: str | None = None) -> None:
    p.setBrush(QColor(colour or style.fill))
    p.setPen(_pen(style.edge, 5))


def _leads(p: QPainter, xs: tuple[float, ...], top: float, bottom: float) -> None:
    p.setPen(_pen(LEAD, 6))
    for x in xs:
        p.drawLine(QPointF(x, top), QPointF(x, bottom))


def _axial(p: QPainter, style: Any, polarized: bool) -> None:
    """A resistor side-on -- or, polarized, a diode: same archetype, different object."""
    p.setPen(_pen(LEAD, 6))
    p.drawLine(QPointF(4, 50), QPointF(26, 50))
    p.drawLine(QPointF(74, 50), QPointF(96, 50))
    _fill(p, style)
    p.drawRoundedRect(QRectF(24, 32, 52, 36), 14, 14)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(style.accent))
    if polarized:
        p.drawRect(QRectF(64, 33, 8, 34))  # the cathode band, at one end only
    else:
        for x in (36, 46, 56):
            p.drawRect(QRectF(x, 33, 6, 34))


def _electrolytic(p: QPainter, style: Any) -> None:
    _leads(p, (42, 60), 70, 96)
    _fill(p, style)
    p.drawRoundedRect(QRectF(26, 12, 50, 62), 10, 10)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(style.accent))
    p.drawRect(QRectF(30, 18, 12, 50))  # the polarity stripe down one side
    p.setBrush(QColor(style.edge))
    p.drawEllipse(QRectF(30, 24, 12, 12))


def _disc(p: QPainter, style: Any) -> None:
    _leads(p, (40, 60), 66, 96)
    _fill(p, style)
    p.drawEllipse(QPointF(50, 42), 30, 30)


def _box_film(p: QPainter, style: Any) -> None:
    _leads(p, (38, 62), 70, 96)
    _fill(p, style)
    p.drawRoundedRect(QRectF(20, 16, 60, 56), 6, 6)
    p.setPen(_pen(style.accent, 5))
    p.drawLine(QPointF(30, 34), QPointF(70, 34))


def _dip(p: QPainter, style: Any) -> None:
    """From above, because the notch is how a DIP is recognised and which way up it goes."""
    p.setPen(_pen(LEAD, 7))
    for y in (30, 50, 70):
        p.drawLine(QPointF(8, y), QPointF(24, y))
        p.drawLine(QPointF(76, y), QPointF(92, y))
    _fill(p, style)
    p.drawRoundedRect(QRectF(22, 14, 56, 72), 4, 4)
    p.setPen(_pen(style.accent, 5))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(38, 6, 24, 20), 180 * 16, 180 * 16)  # the pin-1 notch


def _to92(p: QPainter, style: Any) -> None:
    _leads(p, (36, 50, 64), 68, 96)
    _fill(p, style)
    path = QPainterPath()
    path.moveTo(QPointF(24, 56))
    path.arcTo(QRectF(20, 10, 60, 60), 190, -200)
    path.closeSubpath()
    p.drawPath(path)


def _to220(p: QPainter, style: Any) -> None:
    _leads(p, (36, 50, 64), 74, 96)
    # The tab first: it is metal, it is the part that bolts down, and it is why this
    # archetype is the one the heat rules care about.
    p.setBrush(QColor("#a8b0ba"))
    p.setPen(_pen("#5a626c", 5))
    p.drawRoundedRect(QRectF(28, 8, 44, 26), 4, 4)
    p.setBrush(QColor("#2b3038"))
    p.drawEllipse(QPointF(50, 18), 7, 7)
    _fill(p, style)
    p.drawRoundedRect(QRectF(26, 30, 48, 46), 4, 4)


def _led(p: QPainter, style: Any) -> None:
    # One lead longer than the other, which is the only thing on an LED that says which
    # way round it goes once it is out of the packet.
    p.setPen(_pen(LEAD, 6))
    p.drawLine(QPointF(40, 66), QPointF(40, 96))
    p.drawLine(QPointF(60, 66), QPointF(60, 84))
    _fill(p, style)
    path = QPainterPath()
    path.moveTo(QPointF(24, 70))
    path.lineTo(QPointF(24, 44))
    path.arcTo(QRectF(24, 14, 52, 60), 180, -180)
    path.lineTo(QPointF(76, 70))
    path.closeSubpath()
    p.drawPath(path)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(style.accent))
    p.drawEllipse(QRectF(34, 26, 12, 20))  # the highlight on the lens


def _pin_header(p: QPainter, style: Any) -> None:
    p.setPen(_pen(style.accent, 7))
    for x in (26, 42, 58, 74):
        p.drawLine(QPointF(x, 14), QPointF(x, 44))
        p.drawLine(QPointF(x, 66), QPointF(x, 92))
    _fill(p, style)
    p.drawRect(QRectF(16, 40, 68, 28))


def _screw_terminal(p: QPainter, style: Any) -> None:
    _leads(p, (34, 66), 76, 96)
    _fill(p, style)
    p.drawRoundedRect(QRectF(16, 20, 68, 58), 5, 5)
    p.setBrush(QColor(style.accent))
    p.setPen(_pen(style.edge, 4))
    for x in (34, 66):
        p.drawEllipse(QPointF(x, 40), 12, 12)
        p.drawLine(QPointF(x - 7, 40), QPointF(x + 7, 40))


def _potentiometer(p: QPainter, style: Any) -> None:
    _leads(p, (30, 50, 70), 78, 96)
    _fill(p, style)
    p.drawRoundedRect(QRectF(18, 34, 64, 46), 5, 5)
    p.setBrush(QColor(style.accent))
    p.drawEllipse(QPointF(50, 30), 18, 18)
    p.setPen(_pen(style.edge, 6))
    p.drawLine(QPointF(50, 18), QPointF(50, 30))


def _tactile(p: QPainter, style: Any) -> None:
    p.setPen(_pen(LEAD, 6))
    for x, y in ((22, 26), (22, 74), (78, 26), (78, 74)):
        p.drawLine(QPointF(x, y), QPointF(x + (10 if x > 50 else -10), y))
    _fill(p, style)
    p.drawRoundedRect(QRectF(22, 16, 56, 68), 5, 5)
    p.setBrush(QColor(style.accent))
    p.drawEllipse(QPointF(50, 50), 16, 16)


def _crystal(p: QPainter, style: Any) -> None:
    _leads(p, (38, 62), 72, 96)
    _fill(p, style)
    p.drawRoundedRect(QRectF(22, 14, 56, 60), 24, 24)
    p.setPen(_pen(style.accent, 5))
    p.drawLine(QPointF(32, 28), QPointF(32, 60))  # the highlight down a metal can


def _relay(p: QPainter, style: Any) -> None:
    _leads(p, (28, 50, 72), 78, 96)
    _fill(p, style)
    p.drawRoundedRect(QRectF(14, 16, 72, 62), 6, 6)
    p.setPen(_pen(style.accent, 5))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(QRectF(26, 28, 48, 22))


def _generic(p: QPainter, style: Any) -> None:
    _leads(p, (36, 64), 72, 96)
    _fill(p, style)
    p.drawRoundedRect(QRectF(20, 20, 60, 52), 5, 5)


#: Archetype -> drawing. One per member of ``model.BodyArchetype``; a test fails if the
#: model gains one and this does not, because a part with no picture in a list of pictures
#: reads as a broken row rather than a missing icon.
PART_DRAWINGS: dict[str, Callable[[QPainter, Any], None]] = {
    "radial-electrolytic": _electrolytic,
    "disc-ceramic": _disc,
    "box-film": _box_film,
    "dip": _dip,
    "to92": _to92,
    "to220": _to220,
    "led-round": _led,
    "pin-header": _pin_header,
    "screw-terminal": _screw_terminal,
    "potentiometer": _potentiometer,
    "tactile-switch": _tactile,
    "crystal-hc49": _crystal,
    "relay-box": _relay,
    "generic-box": _generic,
}


def part_icon(footprint: Any) -> QIcon:
    """A small colour picture of this part, in the colours the board draws it in."""
    from .bodies import style_for

    archetype = footprint.body.archetype
    style = style_for(footprint)
    key = f"{archetype}:{footprint.polarized}:{style.fill}"
    cached = _part_cache.get(key)
    if cached is not None:
        return cached

    scale = 2
    pixmap = QPixmap(PART_SIZE * scale, PART_SIZE * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(PART_SIZE * scale / BOX, PART_SIZE * scale / BOX)
    if archetype == "axial-cylinder":
        _axial(painter, style, bool(footprint.polarized))
    else:
        PART_DRAWINGS.get(archetype, _generic)(painter, style)
    painter.end()
    pixmap.setDevicePixelRatio(float(scale))

    built = QIcon(pixmap)
    _part_cache[key] = built
    return built
