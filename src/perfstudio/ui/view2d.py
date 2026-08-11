"""2D board editor on QGraphicsView, rewired onto the real engine.

Promoted from ``prototypes/qt/view2d.py``. The scene still works in MILLIMETRES (one
scene unit = 1 mm) -- that is what makes the 1:1 PDF export exact -- but everything it
draws now comes from a real ``perfstudio.model.PerfDocument`` rather than the
prototype's throwaway ``board_model`` dataclasses, and dragging a part no longer
mutates anything directly: it dispatches ``component.move`` on a
``perfstudio.command.CommandBus`` and waits to be told the result.

MIRRORING. ``hole_to_screen``/``screen_to_hole`` below are the single place that turns
a hole coordinate into a scene position and back, for either board side. The solder
side is a genuine reflection about the HOLE SPAN (``geometry.hole_span_mm``), not the
substrate size (``geometry.board_size_mm``) -- the two differ by half a pitch, and
reflecting about the wrong one silently shifts every hole by that half pitch, which is
exactly the kind of bug that only shows up once someone has already soldered the board
backwards (see the long comment on ``hole_span_mm`` in geometry.py). Every item in this
file that needs a screen position -- pads, conductor paths, component anchors AND a
component's local body/pin offsets -- routes through these two functions (or the
matching local-offset negation), so flipping the board is one reflection applied
consistently everywhere, not a family of ad hoc sign flips that can drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
)

from perfstudio.command import CommandBus, DispatchResult
from perfstudio.commands import MoveComponentPayload, PlaceComponentPayload
from perfstudio.connectivity import FootprintLookup
from perfstudio.drc import DrcViolation
from perfstudio.geometry import (
    all_pin_holes,
    board_size_mm,
    column_label,
    hole_span_mm,
    is_inside_board,
    row_label,
    transform_offset,
    transform_pin_offset,
)
from perfstudio.model import (
    Board,
    BoardSide,
    ComponentInstance,
    Conductor,
    Footprint,
    HoleCoord,
    PerfDocument,
    contacts_every_path_hole,
)
from perfstudio.ratsnest import RatsnestLink, all_links, ratsnest

from .bodies import (
    BodyPlacement,
    BodyStyle,
    leads_for,
    placement_for,
    polarity_pin_offset,
    style_for,
)
from .scenetext import draw_label

# --------------------------------------------------------------------------- theme

BACKGROUND = QColor("#12131a")
SUBSTRATE = {"FR4": QColor("#2e6b3f"), "FR2": QColor("#a8834e"), "FR1": QColor("#b8925c")}
SUBSTRATE_EDGE = QColor("#0d1a12")
PAD = QColor("#c8a951")
PAD_RING = QColor("#8a7331")
PAD_SHEEN = QColor("#e4cd83")
DRILL = QColor("#22232a")
BODY_LINE = QColor("#31313a")
#: Tinned component lead, and the pin markers drawn over a body.
LEAD = QColor("#c2c8d0")
PIN_MARKER = QColor("#8d949e")
PIN_ONE = QColor("#f0f3f8")
LABEL = QColor("#15151a")
REF_LABEL = QColor("#eef1f8")
SELECTED = QColor("#4c9dff")
ERROR_OUTLINE = QColor("#e5484d")
RISK_RING = QColor("#e5484d")
RULER_TEXT = QColor("#8b93a7")
RULER_TEXT_MAJOR = QColor("#d3d9e8")

#: Ratsnest colours by net class -- ground and power read as rails at a glance, which is
#: the same distinction the router orders its work by.
RATSNEST = {
    "ground": QColor("#7f8794"),
    "power": QColor("#e0723c"),
    "signal": QColor("#3fa9d4"),
}
RATSNEST_HIGHLIGHT = QColor("#ffd166")

CONDUCTOR_STYLE = {
    # kind: (colour, width mm, dashed)
    "solder-trace": (QColor("#9aa0a6"), 1.5, False),
    "solder-trace-wired": (QColor("#7c848c"), 1.7, False),
    "bare-wire": (QColor("#b0b6bb"), 0.6, False),
    "insulated-wire": (QColor("#d32f2f"), 1.0, False),
    "top-jumper": (QColor("#388e3c"), 0.9, True),
    "lead-bend": (QColor("#c0c4c8"), 0.5, False),
    "strip": (QColor("#c8a951"), 2.0, False),
}

#: Millimetres of scene reserved outside the substrate for the hole-address rulers.
RULER_MARGIN_MM = 6.0

#: Label sizes in SCREEN PIXELS, not millimetres or points -- see ui/scenetext.py. These
#: hold their size while the board zooms, which is both what an annotation should do and
#: what keeps them clear of the size below which some font engines draw nothing.
RULER_LABEL_PX = 11
REF_LABEL_PX = 12


# --------------------------------------------------------------------- coordinates


def hole_to_screen(hole: HoleCoord, board: Board, side: BoardSide) -> QPointF:
    """Hole -> scene position (mm), for whichever side is being VIEWED.

    'top' is the identity mapping (col*pitch, row*pitch), same as
    ``geometry.hole_to_mm``. 'bottom' reflects x about the hole span's midpoint using
    ``geometry.hole_span_mm`` -- NOT ``board_size_mm`` -- so hole (0, r) lands exactly
    on the screen position hole (cols-1, r) occupies from the top, and vice versa.
    """
    x = hole.col * board.pitch
    y = hole.row * board.pitch
    if side == "bottom":
        span_w, _span_h = hole_span_mm(board)
        x = span_w - x
    return QPointF(x, y)


def screen_to_hole(point: QPointF, board: Board, side: BoardSide) -> HoleCoord:
    """Inverse of :func:`hole_to_screen`. Rounds to the nearest hole."""
    x = point.x()
    if side == "bottom":
        span_w, _span_h = hole_span_mm(board)
        x = span_w - x
    col = round(x / board.pitch)
    row = round(point.y() / board.pitch)
    return HoleCoord(col=col, row=row)


def _local_offset_mm(
    x_mm: float, y_mm: float, comp: ComponentInstance, side: BoardSide
) -> tuple[float, float]:
    """Millimetre variant of :func:`_local_offset`, for body geometry rather than pin steps.

    Pin offsets are grid steps and get scaled by the pitch afterwards; a body dimension is
    already in millimetres, so it must not be scaled again.
    """
    dx, dy = transform_offset(x_mm, y_mm, comp.rotation, comp.mirrored)
    if side == "bottom":
        dx = -dx
    return dx, dy


def _local_offset(x0: float, y0: float, comp: ComponentInstance, side: BoardSide) -> tuple[float, float]:
    """A component-local offset (body vertex or pin, in mm/grid-steps), transformed by
    the component's own rotation/mirror AND, if we are viewing the solder side, by the
    same x-reflection ``hole_to_screen`` applies to its anchor -- so the component's
    silhouette and pin markers flip along with the board instead of merely sliding to a
    mirrored anchor while still facing the wrong way.
    """
    dx, dy = transform_offset(x0, y0, comp.rotation, comp.mirrored)
    if side == "bottom":
        dx = -dx
    return dx, dy


# ------------------------------------------------------------------ pad grid item


class PadGridItem(QGraphicsItem):
    """The whole hole grid as ONE item, painting only what is exposed.

    Ported from the prototype essentially unchanged: Qt's ``exposedRect`` culling is
    what keeps a 6000-pad board cheap, and that had nothing to do with which model fed
    it.
    """

    def __init__(self, board: Board, side: BoardSide) -> None:
        super().__init__()
        self.board = board
        self.side = side
        self.drawn = 0
        self.setZValue(-90)

    def boundingRect(self) -> QRectF:
        w, h = board_size_mm(self.board)
        p = self.board.pitch
        return QRectF(-p / 2, -p / 2, w, h)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        b = self.board
        area = option.exposedRect
        pad_r = b.pad_diameter / 2
        drill_r = b.drill_diameter / 2

        c0 = max(0, int((area.left() - pad_r) / b.pitch) - 1)
        c1 = min(b.cols - 1, int((area.right() + pad_r) / b.pitch) + 1)
        r0 = max(0, int((area.top() - pad_r) / b.pitch) - 1)
        r1 = min(b.rows - 1, int((area.bottom() + pad_r) / b.pitch) + 1)

        painter.setPen(QPen(PAD_RING, 0.12))
        painter.setBrush(QBrush(PAD))
        count = 0
        for col in range(c0, c1 + 1):
            for row in range(r0, r1 + 1):
                p = hole_to_screen(HoleCoord(col, row), b, self.side)
                painter.drawEllipse(p, pad_r, pad_r)
                count += 1

        # A sheen arc on the upper-left of each pad. Purely cosmetic, and cheap: it reads
        # as tinned copper catching the light instead of a flat yellow disc, which is what
        # makes the board look like a board. Skipped when zoomed out far enough that the
        # arc would be sub-pixel anyway.
        if b.pad_diameter * (painter.transform().m11() or 1.0) >= 6:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(PAD_SHEEN, 0.16))
            sheen_r = pad_r * 0.68
            for col in range(c0, c1 + 1):
                for row in range(r0, r1 + 1):
                    p = hole_to_screen(HoleCoord(col, row), b, self.side)
                    rect = QRectF(p.x() - sheen_r, p.y() - sheen_r, sheen_r * 2, sheen_r * 2)
                    painter.drawArc(rect, 60 * 16, 100 * 16)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(DRILL))
        for col in range(c0, c1 + 1):
            for row in range(r0, r1 + 1):
                p = hole_to_screen(HoleCoord(col, row), b, self.side)
                painter.drawEllipse(p, drill_r, drill_r)
        self.drawn = count


# --------------------------------------------------------------------- rulers


class HoleRulerItem(QGraphicsItem):
    """Column letters along the top and row numbers down the left side.

    The whole tool speaks in hole addresses -- the build guide says "R3: C7 to C11", DRC
    says "check isolation at C7", the router's explanations name pads (PLAN.md Sec 4.1).
    Without a ruler the user has to count holes to find any of them, which is exactly the
    error-prone manual step this project exists to remove. The labels come from
    ``geometry.column_label``/``row_label``, so they cannot drift from the addresses the
    rest of the system prints.

    Every label is drawn on a dense board, so minor ticks are thinned by ``step`` while
    every fifth stays bright -- the same reading aid as a ruler's long and short marks.
    """

    def __init__(self, board: Board, side: BoardSide) -> None:
        super().__init__()
        self.board = board
        self.side = side
        self.setZValue(80)

    def boundingRect(self) -> QRectF:
        # Generously padded: the labels are a fixed pixel size, so the scene-space room they
        # need grows without limit as the view zooms out (see scenetext.label_extent_mm).
        # An over-large rect only widens a repaint region; too small leaves label debris on
        # the substrate when the view scrolls.
        w, h = board_size_mm(self.board)
        p = self.board.pitch
        pad = RULER_MARGIN_MM + 30
        return QRectF(-p / 2 - pad, -p / 2 - pad, w + pad, h + pad)

    def _label_step(self, scale: float) -> int:
        """How many holes to skip between labels, so they never collide.

        Derived from the current view scale rather than fixed: zoomed out, one label in
        five is legible and one per hole is a grey smear; zoomed in, every hole fits.
        """
        px_per_hole = self.board.pitch * scale
        if px_per_hole >= 22:
            return 1
        if px_per_hole >= 11:
            return 2
        return 5

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        board = self.board
        scale = painter.transform().m11() or 1.0
        step = self._label_step(scale)

        for col in range(board.cols):
            if col % step and (col + 1) % 5:
                continue
            major = (col + 1) % 5 == 0 or col == 0
            painter.setPen(QPen(RULER_TEXT_MAJOR if major else RULER_TEXT))
            x = hole_to_screen(HoleCoord(col, 0), board, self.side).x()
            draw_label(
                painter,
                QPointF(x, -board.pitch / 2 - 1.4),
                column_label(col),
                RULER_LABEL_PX,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                bold=major,
            )

        for row in range(board.rows):
            if row % step and (row + 1) % 5:
                continue
            major = (row + 1) % 5 == 0 or row == 0
            painter.setPen(QPen(RULER_TEXT_MAJOR if major else RULER_TEXT))
            draw_label(
                painter,
                QPointF(-board.pitch / 2 - 1.2, row * board.pitch),
                row_label(row),
                RULER_LABEL_PX,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                bold=major,
            )


# ------------------------------------------------------------- references and pins

#: Reference prefix per archetype, following the usual schematic letters. A part arrives with
#: no name, and "R4" tells a builder what it is at a glance where "cmp-4" tells them nothing --
#: and these are the names the build guide and every DRC message will use.
REF_PREFIXES: dict[str, str] = {
    "axial-cylinder": "R",
    "radial-electrolytic": "C",
    "disc-ceramic": "C",
    "box-film": "C",
    "dip": "U",
    "to92": "Q",
    "to220": "Q",
    "led-round": "LED",
    "pin-header": "J",
    "screw-terminal": "TB",
    "potentiometer": "RV",
    "tactile-switch": "SW",
    "crystal-hc49": "Y",
    "relay-box": "K",
    "generic-box": "X",
}


def reference_prefix(footprint: Footprint) -> str:
    """The schematic letter for a footprint. A polarized axial part is a diode, not a
    resistor -- the same distinction ui/bodies.py draws them by."""
    if footprint.body.archetype == "axial-cylinder" and footprint.polarized:
        return "D"
    return REF_PREFIXES.get(footprint.body.archetype, "X")


def next_reference(document: PerfDocument, footprint_id: str) -> str:
    """The next free reference for this kind of part, e.g. "R3".

    Counts from what is already on the board rather than from a counter, so it stays correct
    across undo, redo, save, load and a part being deleted from the middle of a sequence -- a
    duplicate ref is refused by the bus, and being refused because a hidden counter disagreed
    with the document would be baffling.
    """
    from perfstudio.footprints import get_footprint

    footprint = get_footprint(footprint_id)
    prefix = reference_prefix(footprint) if footprint is not None else "X"
    used = {c.ref for c in document.components}
    index = 1
    while f"{prefix}{index}" in used:
        index += 1
    return f"{prefix}{index}"


def _pin_holes_of(
    comp: ComponentInstance, lookup: FootprintLookup
) -> list[tuple[Any, HoleCoord]]:
    footprint = lookup(comp.footprint_id)
    if footprint is None:
        return []
    return list(all_pin_holes(comp, footprint))


# --------------------------------------------------------------- placement ghost


class PlacementGhostItem(QGraphicsItem):
    """The part about to be placed, drawn under the cursor before it exists.

    Placing blind -- click and see where it landed -- is the difference between an editor you
    can aim with and one you correct after the fact. The ghost is the real body from
    ui/bodies.py at the real size, so what you line up is what you get, and its pin markers
    show exactly which holes it will occupy.
    """

    def __init__(self, footprint: Footprint, board: Board, side: BoardSide) -> None:
        super().__init__()
        self.fp = footprint
        self.board = board
        self.side = side
        self.anchor = HoleCoord(0, 0)
        self.blocked = False
        self.setZValue(120)
        self.setOpacity(0.62)

    def set_anchor(self, anchor: HoleCoord, blocked: bool) -> None:
        if (anchor, blocked) == (self.anchor, self.blocked):
            return
        self.prepareGeometryChange()
        self.anchor = anchor
        self.blocked = blocked
        self.setPos(hole_to_screen(anchor, self.board, self.side))
        self.update()

    def boundingRect(self) -> QRectF:
        placement = placement_for(self.fp, self.board.pitch)
        reach = max(placement.size_x, placement.size_y) + 4 * self.board.pitch
        return QRectF(-reach, -reach, 2 * reach, 2 * reach)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        placement = placement_for(self.fp, self.board.pitch)
        style = style_for(self.fp)
        rect = _body_rect(placement)

        painter.setBrush(QBrush(QColor(style.fill)))
        painter.setPen(QPen(ERROR_OUTLINE if self.blocked else SELECTED, 0.3))
        painter.drawRect(rect)

        # The holes it will take. On a grid this is the thing you are actually aiming.
        painter.setBrush(QBrush(ERROR_OUTLINE if self.blocked else PIN_ONE))
        painter.setPen(QPen(QColor("#1b1d22"), 0.14))
        for pin in self.fp.pins:
            painter.drawEllipse(
                QPointF(pin.d_col * self.board.pitch, pin.d_row * self.board.pitch), 0.55, 0.55
            )


# ------------------------------------------------------------------ ratsnest overlay


class RatsnestItem(QGraphicsItem):
    """The connections the schematic still wants and the board does not yet make.

    Drawn as one item rather than one per link: a fresh netlist import can produce a few
    hundred of these, and they are decoration for the eye, never interactive.

    Highlighted nets are drawn brighter and thicker over the rest, which is how selecting
    a part answers "and what is this still supposed to connect to".
    """

    def __init__(
        self,
        links: Sequence[RatsnestLink],
        board: Board,
        side: BoardSide,
        highlight: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self.links = list(links)
        self.board = board
        self.side = side
        self.highlight = set(highlight)
        self.setZValue(30)

    def boundingRect(self) -> QRectF:
        w, h = board_size_mm(self.board)
        p = self.board.pitch
        return QRectF(-p / 2, -p / 2, w, h)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        # Highlighted links last, so they sit on top of the rest rather than under it.
        for link in sorted(self.links, key=lambda item: item.net_id in self.highlight):
            lit = link.net_id in self.highlight
            colour = RATSNEST_HIGHLIGHT if lit else RATSNEST.get(link.net_class, RATSNEST["signal"])
            pen = QPen(colour, 0.34 if lit else 0.2)
            pen.setDashPattern([3.5, 2.5] if lit else [2.0, 2.6])
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(
                hole_to_screen(link.from_, self.board, self.side),
                hole_to_screen(link.to, self.board, self.side),
            )


# ------------------------------------------------------------- risk-ring overlay


class RiskRingsItem(QGraphicsItem):
    """Red rings on the holes named by any ``solder-trace-proximity`` violation.

    A separate, cheap item rather than folded into the pad grid: it is rebuilt after
    every DRC run and there is no reason to disturb the pad-grid item (and its own
    exposedRect culling) to do it.
    """

    def __init__(self, holes: Sequence[HoleCoord], board: Board, side: BoardSide) -> None:
        super().__init__()
        self.holes = list(holes)
        self.board = board
        self.side = side
        self.setZValue(60)

    def boundingRect(self) -> QRectF:
        w, h = board_size_mm(self.board)
        p = self.board.pitch
        return QRectF(-p / 2 - 1, -p / 2 - 1, w + 2, h + 2)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        r = self.board.pad_diameter / 2 + 0.35
        pen = QPen(RISK_RING, 0.3)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for hole in self.holes:
            p = hole_to_screen(hole, self.board, self.side)
            painter.drawEllipse(p, r, r)


# ------------------------------------------------------------------ conductor item


class ConductorItem(QGraphicsItem):
    """Each conductor kind has to be tellable apart at a glance.

    ``model.contacts_every_path_hole`` (the engine's own predicate, not a re-derived
    copy of it) decides whether every hole gets a solder bead or only the two
    endpoints get a fillet -- this distinction is the heart of the whole data model and
    it must be read from the engine, never re-implemented here.
    """

    def __init__(self, conductor: Conductor, board: Board, side: BoardSide) -> None:
        super().__init__()
        self.conductor = conductor
        self.board = board
        self.side = side
        self.setZValue(-50 if conductor.side == "bottom" else 40)
        first, last = conductor.path[0], conductor.path[-1]
        self.setToolTip(f"{conductor.kind}  {first.col},{first.row} to {last.col},{last.row}")

    def _points(self) -> list[QPointF]:
        return [hole_to_screen(h, self.board, self.side) for h in self.conductor.path]

    def contact_points(self) -> list[QPointF]:
        """Where a solder joint is actually drawn: every hole for a conductor that
        contacts each one (a solder trace), or just the two endpoints for one that
        only contacts its ends (a wire) -- see ``model.contacts_every_path_hole``.
        Split out from :meth:`paint` so this distinction, the heart of the model, is
        assertable directly rather than only by inspecting rendered pixels.
        """
        pts = self._points()
        if contacts_every_path_hole(self.conductor):
            return pts
        return [pts[0], pts[-1]]

    def boundingRect(self) -> QRectF:
        pts = self._points()
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        pad = 2.0
        return QRectF(min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        k = self.conductor.kind
        colour, width, dashed = CONDUCTOR_STYLE.get(k, (QColor("#888"), 0.6, False))
        color_attr = getattr(self.conductor, "color", None)
        if color_attr:
            colour = QColor(color_attr)

        pts = self._points()
        pen = QPen(colour, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if dashed:
            pen.setDashPattern([2.0, 1.5])
        painter.setPen(pen)

        path = QPainterPath(pts[0])
        for p in pts[1:]:
            path.lineTo(p)
        painter.drawPath(path)

        contacts = self.contact_points()
        painter.setPen(Qt.PenStyle.NoPen)
        if contacts_every_path_hole(self.conductor):
            # Beads at every pad: this trace is soldered down all along its length.
            painter.setBrush(QBrush(colour.lighter(115)))
            for p in contacts:
                painter.drawEllipse(p, width * 0.62, width * 0.62)
        else:
            # Fillets at the endpoints only: a wire is soldered only at its two ends.
            painter.setBrush(QBrush(colour.lighter(125)))
            for p in contacts:
                painter.drawEllipse(p, width * 0.75, width * 0.75)


# --------------------------------------------------------------- component bodies
#
# Drawn from the real dimensions in ui/bodies.py, in the footprint's own coordinate frame.
# Every mark here earns its place by answering a question someone actually has while
# soldering: which end is the cathode, which way round does the chip go, where does the wire
# enter the terminal block. A pale rectangle answers none of them.


def _body_rect(placement: BodyPlacement) -> QRectF:
    return QRectF(
        placement.centre_x - placement.size_x / 2,
        placement.centre_y - placement.size_y / 2,
        placement.size_x,
        placement.size_y,
    )


def _keyed_direction(placement: BodyPlacement, keyed: tuple[float, float]) -> int:
    """+1 or -1: which way along the body's axis the keyed pin lies."""
    delta = (
        keyed[0] - placement.centre_x if placement.axis == "x" else keyed[1] - placement.centre_y
    )
    return 1 if delta >= 0 else -1


def _body_path(
    footprint: Footprint,
    placement: BodyPlacement,
    keyed: tuple[float, float] | None,
) -> QPainterPath:
    """The body silhouette. A 'dcut' is a circle with the cathode side flattened, which is
    exactly how the flat on a real LED tells you which lead is which."""
    rect = _body_rect(placement)
    path = QPainterPath()
    if placement.silhouette == "circle":
        path.addEllipse(rect)
        return path
    if placement.silhouette == "rounded":
        radius = min(rect.width(), rect.height()) * 0.32
        path.addRoundedRect(rect, radius, radius)
        return path
    if placement.silhouette == "dcut" and keyed is not None:
        circle = QPainterPath()
        circle.addEllipse(rect)
        # Chop a chord off the keyed side.
        cut = QRectF(rect)
        inset = rect.width() * 0.12 if placement.axis == "x" else rect.height() * 0.12
        if _keyed_direction(placement, keyed) > 0:
            if placement.axis == "x":
                cut.setRight(rect.right() - inset)
            else:
                cut.setBottom(rect.bottom() - inset)
        else:
            if placement.axis == "x":
                cut.setLeft(rect.left() + inset)
            else:
                cut.setTop(rect.top() + inset)
        keep = QPainterPath()
        keep.addRect(cut)
        return circle.intersected(keep)
    if placement.silhouette == "dcut":
        path.addEllipse(rect)
        return path
    radius = min(rect.width(), rect.height()) * 0.12
    path.addRoundedRect(rect, radius, radius)
    return path


def _paint_body(
    painter: QPainter,
    footprint: Footprint,
    placement: BodyPlacement,
    style: BodyStyle,
    keyed: tuple[float, float] | None,
    selected: bool,
    pitch: float,
) -> None:
    rect = _body_rect(placement)
    path = _body_path(footprint, placement, keyed)

    painter.setBrush(QBrush(QColor(style.fill)))
    painter.setPen(QPen(QColor(SELECTED if selected else style.edge), 0.2))
    painter.drawPath(path)

    accent = QColor(style.accent)
    archetype = footprint.body.archetype

    if archetype == "axial-cylinder" and keyed is not None:
        # A diode's cathode band. Clipped to the body so it cannot spill past a rounded end.
        painter.setClipPath(path)
        band = QRectF(rect)
        width = max(rect.width() * 0.17, 0.45) if placement.axis == "x" else rect.width()
        height = rect.height() if placement.axis == "x" else max(rect.height() * 0.17, 0.45)
        if placement.axis == "x":
            band.setWidth(width)
            if _keyed_direction(placement, keyed) > 0:
                band.moveRight(rect.right() - rect.width() * 0.16)
            else:
                band.moveLeft(rect.left() + rect.width() * 0.16)
        else:
            band.setHeight(height)
            if _keyed_direction(placement, keyed) > 0:
                band.moveBottom(rect.bottom() - rect.height() * 0.16)
            else:
                band.moveTop(rect.top() + rect.height() * 0.16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(accent))
        painter.drawRect(band)
        painter.setClipping(False)

    elif archetype == "radial-electrolytic" and keyed is not None:
        # The printed stripe marks the NEGATIVE side, so it goes opposite pin 1.
        painter.setClipPath(path)
        stripe = QRectF(rect)
        if placement.axis == "x":
            stripe.setWidth(rect.width() * 0.22)
            if _keyed_direction(placement, keyed) > 0:
                stripe.moveLeft(rect.left())
            else:
                stripe.moveRight(rect.right())
        else:
            stripe.setHeight(rect.height() * 0.22)
            if _keyed_direction(placement, keyed) > 0:
                stripe.moveTop(rect.top())
            else:
                stripe.moveBottom(rect.bottom())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(accent))
        painter.drawRect(stripe)
        painter.setClipping(False)

    elif archetype == "dip" and keyed is not None:
        # The moulded notch at the keyed end and a dot by pin 1 -- both, because a real
        # package has both and people look for whichever they are used to.
        #
        # CLIPPED to the body and drawn in a lighter shade of it. Painting the notch as a
        # filled circle in the edge colour instead put a large near-black disc bulging out of
        # a near-black package: invisible as a mark, and wrong as a silhouette. An indent has
        # to read as part of the body, which means inside its outline and lit differently.
        painter.setClipPath(path)
        notch = min(rect.width(), rect.height()) * 0.17
        painter.setBrush(QBrush(QColor(style.fill).lighter(190)))
        painter.setPen(Qt.PenStyle.NoPen)
        if placement.axis == "y":
            cy = rect.top() if _keyed_direction(placement, keyed) < 0 else rect.bottom()
            painter.drawEllipse(QPointF(rect.center().x(), cy), notch, notch)
        else:
            cx = rect.left() if _keyed_direction(placement, keyed) < 0 else rect.right()
            painter.drawEllipse(QPointF(cx, rect.center().y()), notch, notch)
        painter.setClipping(False)

        dot = min(rect.width(), rect.height()) * 0.075
        painter.setBrush(QBrush(accent))
        painter.drawEllipse(
            QPointF(
                rect.center().x() + (keyed[0] - placement.centre_x) * 0.6,
                rect.center().y() + (keyed[1] - placement.centre_y) * 0.6,
            ),
            dot,
            dot,
        )

    elif archetype == "to220":
        # The metal tab, along the edge away from the pins.
        painter.setPen(QPen(QColor(style.edge), 0.12))
        painter.setBrush(QBrush(accent))
        tab = QRectF(rect)
        if placement.axis == "x":
            tab.setHeight(rect.height() * 0.34)
            tab.moveTop(rect.top())
        else:
            tab.setWidth(rect.width() * 0.34)
            tab.moveLeft(rect.left())
        painter.drawRect(tab)

    elif archetype == "pin-header":
        _paint_pin_marks(painter, footprint, pitch, accent, square=True)

    elif archetype == "screw-terminal":
        _paint_pin_marks(painter, footprint, pitch, accent, square=False)

    elif archetype in ("potentiometer", "tactile-switch"):
        # The shaft or the button: the thing a finger or a screwdriver has to reach.
        radius = min(rect.width(), rect.height()) * (0.15 if archetype == "potentiometer" else 0.24)
        painter.setPen(QPen(QColor(style.edge), 0.14))
        painter.setBrush(QBrush(accent))
        painter.drawEllipse(rect.center(), radius, radius)


def _paint_pin_marks(
    painter: QPainter,
    footprint: Footprint,
    pitch: float,
    accent: QColor,
    square: bool,
) -> None:
    """A contact per pin: square for a header's pins, round for a terminal's screw heads."""
    painter.setPen(QPen(QColor("#00000060"), 0.1))
    painter.setBrush(QBrush(accent))
    size = 0.62
    for pin in footprint.pins:
        centre = QPointF(pin.d_col * pitch, pin.d_row * pitch)
        if square:
            painter.drawRect(QRectF(centre.x() - size / 2, centre.y() - size / 2, size, size))
        else:
            painter.drawEllipse(centre, size * 0.85, size * 0.85)


# ------------------------------------------------------------------ component item


class ComponentItem(QGraphicsItem):
    """A placed part. Movable (unless locked), and snapped to the hole grid while
    dragging.

    Dragging never mutates ``self.comp`` -- it is a frozen snapshot from the document
    the scene was last built from. ``itemChange`` only tracks the SNAPPED HOLE the item
    is currently hovering over (``pending_anchor``); the scene compares that against
    ``comp.anchor`` on release to decide whether (and where) to dispatch
    ``component.move``. If the bus refuses the move, the next rebuild redraws this item
    back at ``comp.anchor`` -- there is no separate "snap back" code path, just the one
    source of truth.
    """

    def __init__(self, comp: ComponentInstance, fp: Footprint, board: Board, side: BoardSide) -> None:
        super().__init__()
        self.comp = comp
        self.fp = fp
        self.board = board
        self.side = side
        self.pending_anchor: HoleCoord = comp.anchor
        self.has_error = False
        # Locked components stay draggable on purpose: the bus (not the item flags) is
        # what refuses the move ("component-locked"), and that refusal has to be
        # reachable and its message surfaced, rather than the UI silently pre-empting
        # the attempt -- see the module docstring and MainWindow.on_move_committed.
        flags = (
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
        )
        self.setFlags(flags)
        self.setZValue(10)
        lock_note = "  [locked]" if comp.locked else ""
        self.setToolTip(f"{comp.ref}  {comp.value}\n{fp.name}{lock_note}")
        self._sync_position()

    def _sync_position(self) -> None:
        self.setPos(hole_to_screen(self.comp.anchor, self.board, self.side))

    def set_error(self, has_error: bool) -> None:
        if has_error != self.has_error:
            self.has_error = has_error
            self.update()

    def _local_outline(self) -> QPolygonF:
        poly = QPolygonF()
        for pt in self.fp.body_outline:
            dx, dy = _local_offset(pt.x, pt.y, self.comp, self.side)
            poly.append(QPointF(dx, dy))
        return poly

    def boundingRect(self) -> QRectF:
        # The top margin has to hold a fixed-pixel-size ref label, which occupies more scene
        # space the further out the view is zoomed (see scenetext.label_extent_mm). Sized for
        # the lowest zoom at which the label is still drawn.
        top = 1.5 + REF_LABEL_PX / 3.0
        return self._local_outline().boundingRect().adjusted(-1.5, -top, 1.5, 3.0)

    def _apply_local_transform(self, painter: QPainter) -> None:
        """Enter the footprint's own coordinate frame, so a body can be drawn as a shape.

        Mirrors ``geometry.transform_offset`` exactly -- mirror about the vertical axis, then
        rotate clockwise -- with the solder side's reflection outermost, matching
        ``_local_offset``. QPainter applies the transforms it is given innermost-last, which
        is why they are called in the reverse of that order here.
        """
        if self.side == "bottom":
            painter.scale(-1.0, 1.0)
        painter.rotate(float(self.comp.rotation))
        if self.comp.mirrored:
            painter.scale(-1.0, 1.0)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        placement = placement_for(self.fp, self.board.pitch)
        style = style_for(self.fp)
        keyed = polarity_pin_offset(self.fp, self.board.pitch)

        # The courtyard, faint and only when it matters. It is what DRC checks for overlap,
        # and it is padded well beyond the part -- drawing it as the body is what used to make
        # every component an oversized pale rectangle.
        if self.isSelected() or self.has_error:
            outline_pen = QPen(SELECTED if self.isSelected() else ERROR_OUTLINE, 0.22)
            outline_pen.setDashPattern([2.0, 2.0])
            painter.setPen(outline_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(self._local_outline())

        # THE SOLDER SIDE SHOWS NO BODY. Turn a real board over and the parts are on the far
        # face: what you see is the substrate, the pads, and the cut lead ends poking through.
        # Mirroring the silhouettes and drawing them as if seen from above -- which is what
        # this did -- shows you a view that does not exist, on the very side where a
        # misreading gets soldered in.
        if self.side == "top":
            # Leads, in item coordinates: a line does not care which way its frame is turned,
            # and the endpoints already come from the shared transform.
            leads = leads_for(self.fp, placement, self.board.pitch)
            if leads:
                lead_pen = QPen(LEAD, self.fp.lead_diameter or 0.5)
                lead_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(lead_pen)
                for lead in leads:
                    start = _local_offset_mm(lead.from_x, lead.from_y, self.comp, self.side)
                    end = _local_offset_mm(lead.to_x, lead.to_y, self.comp, self.side)
                    painter.drawLine(QPointF(*start), QPointF(*end))

            painter.save()
            self._apply_local_transform(painter)
            _paint_body(
                painter, self.fp, placement, style, keyed, self.isSelected(), self.board.pitch
            )
            painter.restore()

        self._paint_pin_ends(painter, keyed)

    def _paint_pin_ends(self, painter: QPainter, keyed: tuple[float, float] | None) -> None:
        """The holes this part occupies.

        Drawn on both sides, but they are the WHOLE story on the solder side: a cut lead end
        sitting in its pad is exactly what a person looks at while soldering. Pin 1 is filled
        brighter on a keyed part, because "which pad is pin 1, from the back" is the question
        that decides whether the part goes in the right way round.
        """
        solder_side = self.side == "bottom"
        radius = 0.62 if solder_side else 0.5
        for pin in self.fp.pins:
            dx, dy = _local_offset(pin.d_col, pin.d_row, self.comp, self.side)
            centre = QPointF(dx * self.board.pitch, dy * self.board.pitch)
            marked = pin.number == "1" and keyed is not None
            if self.isSelected():
                edge = SELECTED
            elif marked:
                edge = QColor("#1b1d22")
            else:
                edge = QColor("#42464b")
            painter.setPen(QPen(edge, 0.18 if solder_side else 0.14))
            painter.setBrush(QBrush(PIN_ONE if marked else PIN_MARKER))
            painter.drawEllipse(centre, radius, radius)

        # The ref sits just above the body, so it lands on the substrate rather than on the
        # part -- which means it needs a LIGHT colour. It was previously drawn in near-black
        # The ref sits just above the courtyard, on the substrate rather than on the part, so
        # it needs a LIGHT colour. It used to be drawn in near-black -- fine against a body,
        # invisible against dark green FR4 -- so in practice no part was labelled at all.
        # Skipped when zoomed out far enough that the text would be an unreadable smear.
        scale = painter.transform().m11() or 1.0
        if scale >= 3.0:
            rect = self._local_outline().boundingRect()
            anchor = QPointF(rect.left(), rect.top())
            align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom
            # A one-pixel dark copy underneath: a hairline shadow is what keeps the label
            # readable where it crosses a pad or another part, not just over bare substrate.
            painter.setPen(QPen(LABEL))
            draw_label(
                painter, anchor, self.comp.ref, REF_LABEL_PX, align, bold=True,
                offset=QPointF(1, -1),
            )
            painter.setPen(QPen(SELECTED if self.isSelected() else REF_LABEL))
            draw_label(
                painter, anchor, self.comp.ref, REF_LABEL_PX, align, bold=True,
                offset=QPointF(0, -2),
            )

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene():
            # Snap to the nearest hole, but do NOT clamp to the board bounds: dragging
            # a part past the edge and releasing there must reach the bus's own
            # off-board refusal (see BoardScene.commit_pending_moves), not be silently
            # prevented by the item itself. The board substrate visually bounds the
            # scene anyway, so the UX cost of not clamping is small.
            p: QPointF = value
            snapped = screen_to_hole(p, self.board, self.side)
            self.pending_anchor = snapped
            return hole_to_screen(snapped, self.board, self.side)
        return super().itemChange(change, value)


# ------------------------------------------------------------------------- scene


class BoardScene(QGraphicsScene):
    """The 2D board, rebuilt from a ``PerfDocument`` snapshot each time it changes.

    Read-only by itself: it never mutates ``self.document``. When a bus is supplied,
    dragging a component dispatches ``component.move`` on release and emits
    ``moveCommitted`` with the ``DispatchResult`` list so the host (``main.py``) can
    decide how to react -- rerun DRC, update the status bar, rebuild from
    ``bus.document`` -- rather than the scene reaching into that policy itself.
    """

    moveCommitted = Signal(list)
    #: Emitted with the ids of the schematic nets under the current selection, so the host
    #: can update a net list or a status line without the scene knowing either exists.
    selectionNetsChanged = Signal(list)

    def __init__(
        self,
        document: PerfDocument,
        lookup: FootprintLookup,
        side: BoardSide = "top",
        bus: CommandBus | None = None,
        show_ratsnest: bool = True,
        show_rulers: bool = True,
    ) -> None:
        super().__init__()
        self.lookup = lookup
        self.side = side
        self.bus = bus
        self.document = document
        self.violations: tuple[DrcViolation, ...] = ()
        self.component_items: dict[str, ComponentItem] = {}
        self.pad_grid: PadGridItem | None = None
        self.show_ratsnest = show_ratsnest
        self.show_rulers = show_rulers
        self.highlighted_nets: tuple[str, ...] = ()
        self._risk_item: RiskRingsItem | None = None
        self._ratsnest_item: RatsnestItem | None = None
        self._ratsnest_links: tuple[RatsnestLink, ...] = ()
        self._armed_footprint: Footprint | None = None
        self._armed_id: str | None = None
        self._ghost: PlacementGhostItem | None = None
        #: Whether the last placement landed somewhere already occupied. Read by the host to
        #: say so, since the bus allows it and only DRC objects.
        self.last_placement_overlapped = False
        self._build()
        self.selectionChanged.connect(self._on_selection_changed)

    # -- (re)building -----------------------------------------------------

    def set_document(self, document: PerfDocument) -> None:
        self.document = document
        self._build()

    def set_side(self, side: BoardSide) -> None:
        if side != self.side:
            self.side = side
            self._build()

    def set_show_ratsnest(self, show: bool) -> None:
        if show != self.show_ratsnest:
            self.show_ratsnest = show
            self._rebuild_ratsnest()

    def set_show_rulers(self, show: bool) -> None:
        if show != self.show_rulers:
            self.show_rulers = show
            self._build()

    def set_highlighted_nets(self, net_ids: Sequence[str]) -> None:
        """Light up the given schematic nets' remaining connections."""
        wanted = tuple(net_ids)
        if wanted != self.highlighted_nets:
            self.highlighted_nets = wanted
            self._rebuild_ratsnest()

    def _build(self) -> None:
        # Selection survives a rebuild. Every command triggers one, so without this a part
        # is deselected the instant it is acted on -- pressing R twice would rotate once and
        # then do nothing, which reads as the key having stopped working.
        previously_selected = {
            comp_id for comp_id, item in self.component_items.items() if item.isSelected()
        }
        # The dict is emptied BEFORE clear(), not after. clear() destroys the underlying C++
        # items and Qt emits selectionChanged while doing so, which reaches
        # _on_selection_changed -- and if that handler can still see the old dict, it touches
        # wrappers whose C++ object is gone and raises "Internal C++ object already deleted".
        # Emptying first means the handler sees an empty selection, which is the truth.
        self.component_items = {}
        self.clear()
        self._risk_item = None
        self._ratsnest_item = None
        self._ghost = None
        board = self.document.board
        w, h = board_size_mm(board)
        margin = RULER_MARGIN_MM if self.show_rulers else 4.0
        self.setSceneRect(
            -board.pitch / 2 - margin, -board.pitch / 2 - margin, w + margin + 4, h + margin + 4
        )
        self.setBackgroundBrush(QBrush(BACKGROUND))

        substrate = self.addRect(
            QRectF(-board.pitch / 2, -board.pitch / 2, w, h),
            QPen(SUBSTRATE_EDGE, 0.4),
            QBrush(SUBSTRATE.get(board.material, SUBSTRATE["FR4"])),
        )
        substrate.setZValue(-100)

        self.pad_grid = PadGridItem(board, self.side)
        self.addItem(self.pad_grid)

        if self.show_rulers:
            self.addItem(HoleRulerItem(board, self.side))

        for conductor in self.document.conductors:
            self.addItem(ConductorItem(conductor, board, self.side))

        for comp in self.document.components:
            fp = self.lookup(comp.footprint_id)
            if fp is None:
                continue
            item = ComponentItem(comp, fp, board, self.side)
            self.addItem(item)
            self.component_items[comp.id] = item
            if comp.id in previously_selected:
                item.setSelected(True)

        # Recomputed on every rebuild rather than cached across one: a rebuild follows a
        # committed command, which is exactly when what remains to be connected changes.
        self._ratsnest_links = all_links(ratsnest(self.document, self.lookup))
        self._rebuild_ratsnest()
        self._apply_violations()
        # The ghost is an item like any other, so the rebuild above removed it. Placement is a
        # mode that survives placing a part -- you usually want several -- so it comes back.
        if self._armed_footprint is not None:
            self._ghost = PlacementGhostItem(self._armed_footprint, board, self.side)
            self.addItem(self._ghost)

    # -- ratsnest -----------------------------------------------------------

    def _rebuild_ratsnest(self) -> None:
        if self._ratsnest_item is not None:
            self.removeItem(self._ratsnest_item)
            self._ratsnest_item = None
        if not self.show_ratsnest or not self._ratsnest_links:
            return
        self._ratsnest_item = RatsnestItem(
            self._ratsnest_links, self.document.board, self.side, self.highlighted_nets
        )
        self.addItem(self._ratsnest_item)

    def ratsnest_links(self) -> tuple[RatsnestLink, ...]:
        """What the scene is currently drawing as outstanding, for a status readout."""
        return self._ratsnest_links

    # -- DRC overlay --------------------------------------------------------

    def set_violations(self, violations: Sequence[DrcViolation]) -> None:
        self.violations = tuple(violations)
        self._apply_violations()

    def _apply_violations(self) -> None:
        board = self.document.board
        risk_holes: list[HoleCoord] = []
        error_component_ids: set[str] = set()
        for v in self.violations:
            if v.rule == "solder-trace-proximity":
                risk_holes.extend(v.holes)
            if v.severity == "error":
                error_component_ids.update(v.component_ids)

        if self._risk_item is not None:
            self.removeItem(self._risk_item)
            self._risk_item = None
        if risk_holes:
            self._risk_item = RiskRingsItem(risk_holes, board, self.side)
            self.addItem(self._risk_item)

        for comp_id, item in self.component_items.items():
            item.set_error(comp_id in error_component_ids)

    # -- selection helpers, used by the DRC/LVS dock -------------------------

    def select_components(self, component_ids: Sequence[str]) -> None:
        wanted = set(component_ids)
        for comp_id, item in self.component_items.items():
            item.setSelected(comp_id in wanted)

    def _on_selection_changed(self) -> None:
        """Selecting a part highlights every net it belongs to.

        This is the question a user asks constantly while placing -- "what is this still
        supposed to reach?" -- and the ratsnest already knows. The scene only reports the
        net ids; what to do with them is the host's business.
        """
        selected_refs = {
            item.comp.ref for item in self.component_items.values() if item.isSelected()
        }
        if not selected_refs:
            self.set_highlighted_nets(())
            self.selectionNetsChanged.emit([])
            return
        net_ids = [
            net.id
            for net in self.document.nets
            if any(node.component_ref in selected_refs for node in net.nodes)
        ]
        self.set_highlighted_nets(net_ids)
        self.selectionNetsChanged.emit(net_ids)

    def selected_component_ids(self) -> tuple[str, ...]:
        return tuple(
            comp_id for comp_id, item in self.component_items.items() if item.isSelected()
        )

    # -- dragging: dispatch on release, never per-frame ----------------------

    # -- placing a new part -------------------------------------------------
    #
    # Nothing in this application could add a component at all: the registry has sixty-one
    # footprints and `component.place` has existed since the first commit, with no way to reach
    # either. Placement is a MODE rather than a dialog because the position is the decision --
    # you aim it on the grid, and the ghost shows the holes it will take before you commit.

    #: Emitted with the DispatchResult of a placement attempt, successful or refused.
    componentPlaced = Signal(object)
    #: Emitted with the armed footprint id, or an empty string when placement is cancelled.
    placementArmed = Signal(str)

    def arm_placement(self, footprint_id: str | None) -> None:
        """Arm (or, with None, disarm) placing a footprint on the next board click."""
        self._armed_footprint = None if footprint_id is None else self.lookup(footprint_id)
        self._armed_id = footprint_id if self._armed_footprint is not None else None
        self._clear_ghost()
        if self._armed_footprint is not None:
            self._ghost = PlacementGhostItem(self._armed_footprint, self.document.board, self.side)
            self.addItem(self._ghost)
        self.placementArmed.emit(self._armed_id or "")

    @property
    def armed_footprint_id(self) -> str | None:
        return self._armed_id

    def _clear_ghost(self) -> None:
        if self._ghost is not None:
            self.removeItem(self._ghost)
            self._ghost = None

    def _placement_blocked(self, anchor: HoleCoord) -> bool:
        """Would this placement be refused, or land on an occupied hole?

        Only a preview -- the bus is still the authority and still gets to refuse. Showing it
        red beforehand saves the user a click and a status-bar message.
        """
        footprint = self._armed_footprint
        if footprint is None:
            return True
        board = self.document.board
        occupied = {
            (hole.col, hole.row)
            for comp in self.document.components
            for _pin, hole in _pin_holes_of(comp, self.lookup)
        }
        for pin in footprint.pins:
            d_col, d_row = transform_pin_offset(pin.d_col, pin.d_row, 0, False)
            hole = HoleCoord(anchor.col + d_col, anchor.row + d_row)
            if not is_inside_board(hole, board):
                return True
            if (hole.col, hole.row) in occupied:
                return True
        return False

    def mouseMoveEvent(self, event: Any) -> None:
        if self._ghost is not None:
            anchor = screen_to_hole(event.scenePos(), self.document.board, self.side)
            self._ghost.set_anchor(anchor, self._placement_blocked(anchor))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if self._armed_footprint is not None and event.button() == Qt.MouseButton.LeftButton:
            anchor = screen_to_hole(event.scenePos(), self.document.board, self.side)
            self.place_armed(anchor)
            event.accept()
            return
        if self._armed_footprint is not None and event.button() == Qt.MouseButton.RightButton:
            self.arm_placement(None)  # Right-click cancels, as it does in every editor.
            event.accept()
            return
        super().mousePressEvent(event)

    def place_armed(self, anchor: HoleCoord) -> DispatchResult | None:
        """Dispatch ``component.place`` for the armed footprint. Also the seam tests drive.

        A placement that lands on an occupied hole is NOT refused here. Two pins in one hole is
        a legal document that describes a board you probably do not want, which makes it DRC's
        business, not a command's (see the division of responsibility in commands.py). The ghost
        warns in red beforehand and ``overlapped`` says so afterwards, so the user is told twice
        and still gets to decide.
        """
        if self.bus is None or self._armed_footprint is None or self._armed_id is None:
            return None
        self.last_placement_overlapped = self._placement_blocked(anchor)
        result = self.bus.dispatch(
            "component.place",
            PlaceComponentPayload(
                # From the BUS's document, not this scene's. The scene holds a snapshot that is
                # only refreshed after a command lands, so reading the reference from it would
                # hand out a name that is already taken -- and the bus would refuse the second
                # part of every pair as a duplicate ref.
                ref=next_reference(self.bus.document, self._armed_id),
                value="",
                footprint_id=self._armed_id,
                anchor=anchor,
            ),
        )
        self.componentPlaced.emit(result)
        return result

    def mouseReleaseEvent(self, event: Any) -> None:
        super().mouseReleaseEvent(event)
        self.commit_pending_moves()

    #: Hole steps an arrow key moves the selection, plain and with Shift held.
    NUDGE_STEP = 1
    NUDGE_STEP_FAST = 5

    def keyPressEvent(self, event: Any) -> None:
        """Arrow keys move the selected parts one hole at a time.

        Dispatched as ``component.move``, the same command a drag ends in -- so a nudge
        undoes, journals and reaches an agent's view of the board identically. Placing a part
        exactly is far easier by keyboard than by dragging at 2.54 mm pitch, and the arrow
        keys were previously spent on scrolling a view that already pans with the middle
        button and both scrollbars.
        """
        deltas = {
            Qt.Key.Key_Left: (-1, 0),
            Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1),
            Qt.Key.Key_Down: (0, 1),
        }
        if event.key() == Qt.Key.Key_Escape and self._armed_footprint is not None:
            self.arm_placement(None)
            event.accept()
            return
        delta = deltas.get(Qt.Key(event.key()))
        if delta is None or self.bus is None or not self.selected_component_ids():
            super().keyPressEvent(event)
            return
        fast = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        step = self.NUDGE_STEP_FAST if fast else self.NUDGE_STEP
        self.nudge_selection(delta[0] * step, delta[1] * step)
        event.accept()

    def nudge_selection(self, d_col: int, d_row: int) -> list[DispatchResult]:
        """Move every selected part by a hole offset. Also the seam tests drive."""
        if self.bus is None:
            return []
        # SNAPSHOT FIRST, then dispatch. The first dispatch rebuilds this scene, which
        # destroys every item -- so a loop that both reads items and dispatches is reading
        # destroyed objects from its second iteration onwards.
        targets = [
            (item.comp.id, item.comp.anchor)
            for item in self.component_items.values()
            if item.isSelected()
        ]
        results = [
            self.bus.dispatch(
                "component.move",
                MoveComponentPayload(
                    id=comp_id, anchor=HoleCoord(col=anchor.col + d_col, row=anchor.row + d_row)
                ),
            )
            for comp_id, anchor in targets
        ]
        if results:
            self.moveCommitted.emit(results)
        return results

    def commit_pending_moves(self) -> list[DispatchResult]:
        """Dispatch ``component.move`` for every item whose snapped position differs
        from its last-known document anchor. Called on mouse release (never per drag
        frame -- see the module docstring), and directly by tests so drag behaviour is
        exercisable without synthesizing real Qt mouse events.
        """
        if self.bus is None:
            return []
        # Snapshotted before any dispatch, for the reason given in nudge_selection: the first
        # dispatch rebuilds the scene and destroys these items. This loop happened to survive
        # that because pending_anchor and comp are plain Python attributes, which a wrapper
        # whose C++ object is gone will still hand over -- an accident, not a design.
        pending = [
            (comp_id, item.pending_anchor)
            for comp_id, item in self.component_items.items()
            if item.pending_anchor != item.comp.anchor
        ]
        results = [
            self.bus.dispatch("component.move", MoveComponentPayload(id=comp_id, anchor=anchor))
            for comp_id, anchor in pending
        ]
        if results:
            self.moveCommitted.emit(results)
        return results


#: Zoom bounds in scene pixels per millimetre. Below the minimum a whole board is a few
#: hundred pixels and nothing is distinguishable; above the maximum a single pad fills the
#: viewport. Both ends are reachable by accident on a trackpad, so they are clamped.
MIN_SCALE = 1.5
MAX_SCALE = 90.0


class BoardView(QGraphicsView):
    def __init__(self, scene: BoardScene) -> None:
        super().__init__(scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.scale(6, 6)  # ~6 px per mm to start
        self._panning = False
        self._pan_origin = QPointF()

    def current_scale(self) -> float:
        return float(self.transform().m11())

    def wheelEvent(self, event: Any) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        target = self.current_scale() * factor
        if not (MIN_SCALE <= target <= MAX_SCALE):
            return
        self.scale(factor, factor)

    def zoom_by(self, factor: float) -> None:
        """Zoom about the viewport centre, for a toolbar button or a keyboard shortcut."""
        target = self.current_scale() * factor
        if not (MIN_SCALE <= target <= MAX_SCALE):
            return
        anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.scale(factor, factor)
        self.setTransformationAnchor(anchor)

    def fit_board(self) -> None:
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    # Middle-button pan. Rubber-band selection owns the left button, and reaching for a
    # scrollbar to cross a 60-column board is the kind of friction that makes an editor
    # feel unfinished.
    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_origin = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._panning:
            delta = event.position() - self._pan_origin
            self._pan_origin = event.position()
            h = self.horizontalScrollBar()
            v = self.verticalScrollBar()
            h.setValue(h.value() - int(delta.x()))
            v.setValue(v.value() - int(delta.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if self._panning and event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    @staticmethod
    def _holes_rect(
        holes: Sequence[HoleCoord], board: Board, side: BoardSide, margin: float
    ) -> QRectF:
        pts = [hole_to_screen(h, board, side) for h in holes]
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        return QRectF(
            min(xs) - margin,
            min(ys) - margin,
            max(xs) - min(xs) + 2 * margin,
            max(ys) - min(ys) + 2 * margin,
        )

    def center_on_holes(self, holes: Sequence[HoleCoord], board: Board, side: BoardSide) -> None:
        """Zoom to frame the given holes. For the DRC/LVS dock, where the point of clicking
        a violation is to get a close look at the two pads it names."""
        if not holes:
            return
        self.fitInView(self._holes_rect(holes, board, side, 6.0), Qt.AspectRatioMode.KeepAspectRatio)

    def reveal_holes(self, holes: Sequence[HoleCoord], board: Board, side: BoardSide) -> None:
        """Bring the given holes into view WITHOUT changing zoom, unless they cannot fit.

        Used when selecting a whole net: a net can span the board, so framing it would zoom
        right out, and framing a two-pin net would zoom right in. Either way the user loses
        the working magnification they had chosen, having asked only to see a net.
        """
        if not holes:
            return
        rect = self._holes_rect(holes, board, side, 4.0)
        viewport = self.mapToScene(self.viewport().rect()).boundingRect()
        if rect.width() > viewport.width() or rect.height() > viewport.height():
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
            return
        if not viewport.contains(rect):
            self.centerOn(rect.center())
