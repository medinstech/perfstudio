"""The schematic panel: the sheet ``schematic.py`` generates, on screen and clickable.

WHAT IS EDITABLE HERE IS THE CIRCUIT, NEVER THE DRAWING. ``perfstudio.schematic`` derives
a whole sheet from the document and stores nothing; this file paints one, reports clicks,
and hands them back to the window as commands -- add a part, join two pins, place the lot.
Nothing here moves a SYMBOL, because a moved symbol would have to be remembered, and
remembering it would put schematic geometry into the ``.perf`` format, which PLAN.md D3
settled against and the byte-for-byte persistence tests would notice immediately.

ONE ITEM PAINTS THE WHOLE SHEET, AND THE HIT TESTING IS DONE BY HAND. A symbol per
``QGraphicsItem`` would give hover and selection for free, and would also mean a bounding
rect and a ``shape()`` per symbol kind, ten of which exist. The drawing is a few hundred
primitives, it is rebuilt wholesale whenever the document changes, and "which symbol is
under this point" is a rectangle test -- so one item, one repaint, and two small
predicates below. What that buys is that the panel has no state the drawing does not
already carry, which is why refreshing it cannot leave anything stale behind.

CROSS-PROBING IS THE POINT OF HAVING IT IN THE WINDOW. LVS says "net VOUT is open" and the
board shows copper; until now there was nothing to look at that said what VOUT *is*.
Clicking a symbol selects that part on the board, clicking a net highlights it there, and
a selection made on the board or in the Nets dock lights up here -- so the two views are
two views of one thing rather than two applications in one window.

Labels are drawn at a fixed PIXEL size through ``scenetext.draw_label``, the same as the
board's reference designators and for the same reason: they are annotation, not artwork,
and a reference that shrank to nothing when the sheet was fitted to the panel would make
the fitted view -- the one people actually look at -- the one view that says nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QMouseEvent,
    QPainter,
    QPen,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QStyleOptionGraphicsItem,
    QWidget,
)

from perfstudio.model import Point2
from perfstudio.schematic import (
    Rail,
    SchematicDrawing,
    Symbol,
    Wire,
    rail_glyph_bars,
)

from . import theme
from .scenetext import draw_label

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
#
# A third colour table, and the reason there are three is set out in CLAUDE.md: `theme`
# colours the application, `view2d`'s block colours the physical object, and this colours
# a DRAWING -- which is neither. A schematic is ink on paper, so it is drawn as ink on
# paper: one line weight for everything, colour used only to separate the three net
# classes and to say what is selected.

SHEET = "#161a21"
INK = "#c8d0de"
INK_DIM = "#7d8698"
SIGNAL = "#7fb2e5"
POWER = "#e0a33c"
GROUND = "#8f97a8"
HIGHLIGHT = theme.ACCENT
UNDEFINED = theme.WARNING

#: Line weights in millimetres of sheet. A schematic has one weight for wires and a
#: slightly heavier one for symbol bodies, which is what makes a body read as a thing and a
#: wire as a connection between things.
WIRE_MM = 0.30
BODY_MM = 0.40
HIGHLIGHT_MM = 0.85

#: Junction dot radius, in millimetres of sheet, so it is part of the drawing rather than
#: of the zoom. The rail glyph gets no constant here at all: its bars sit in room the LAYOUT
#: cleared, so ``schematic.rail_glyph_bars`` owns them and both renderers ask it rather than
#: each holding a half-width that could drift wider than the clearance.
DOT_MM = 0.75

REF_PX = 11
VALUE_PX = 10
NET_PX = 9
PIN_PX = 8

#: Scene-space padding on the item's bounding rect. The labels do not shrink with the
#: sheet, so the room they need GROWS as the view zooms out (``scenetext`` explains why).
#: An over-large rect only widens a repaint region; too small leaves label debris behind.
BOUNDS_PAD_MM = 40.0

#: How close a click has to land to a wire to count as hitting it, in millimetres.
PICK_MM = 1.6

#: The same for a pin, and deliberately larger. A wire is a long target and a pin is a
#: point; the pins of a DIP are one 2.54 mm pitch apart, so this stays under half of that
#: or two neighbouring pins would compete for the same click.
PIN_PICK_MM = 1.2


def _point(point: Point2) -> QPointF:
    return QPointF(point.x, point.y)


def _class_colour(net_class: str) -> str:
    if net_class == "power":
        return POWER
    if net_class == "ground":
        return GROUND
    return SIGNAL


def pin_anchor(drawing: SchematicDrawing, ref: str, number: str) -> QPointF | None:
    """Where a named pin sits on the sheet, in scene millimetres."""
    for symbol in drawing.symbols:
        if symbol.ref != ref:
            continue
        for pin in symbol.pins:
            if pin.number == number:
                return QPointF(symbol.at.x + pin.at.x, symbol.at.y + pin.at.y)
    return None


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------


class SheetItem(QGraphicsItem):
    """Everything on the sheet, painted in one pass.

    Order matters and is the order a draughtsman would use: wires first so a symbol sits on
    top of the line reaching it, then rails, then junction dots, then bodies, then text.
    """

    def __init__(self, drawing: SchematicDrawing) -> None:
        super().__init__()
        self.drawing = drawing
        self.highlight_refs: frozenset[str] = frozenset()
        self.highlight_nets: frozenset[str] = frozenset()
        #: The pin waiting for its partner while a wire is being drawn.
        self.pending_pin: tuple[str, str] | None = None

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, self.drawing.width, self.drawing.height).adjusted(
            -BOUNDS_PAD_MM, -BOUNDS_PAD_MM, BOUNDS_PAD_MM, BOUNDS_PAD_MM
        )

    def set_highlight(self, refs: Iterable[str], net_ids: Iterable[str]) -> None:
        refs_set, nets_set = frozenset(refs), frozenset(net_ids)
        if refs_set == self.highlight_refs and nets_set == self.highlight_nets:
            return
        self.highlight_refs, self.highlight_nets = refs_set, nets_set
        self.update()

    # -- painting ------------------------------------------------------------

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        drawing = self.drawing

        for wire in drawing.wires:
            self._stroke(painter, wire.path, wire.net_class, wire.net_id in self.highlight_nets)
        for rail in drawing.rails:
            self._stroke(painter, rail.path, rail.net_class, rail.net_id in self.highlight_nets)
            self._rail_glyph(painter, rail)

        for junction in drawing.junctions:
            lit = junction.net_id in self.highlight_nets
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(HIGHLIGHT if lit else SIGNAL)))
            painter.drawEllipse(_point(junction.at), DOT_MM, DOT_MM)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        for symbol in drawing.symbols:
            self._symbol(painter, symbol)

        if self.pending_pin is not None:
            # An open ring on the pin waiting for its partner, the same gesture the board
            # uses: half a decision has been made and it has to be visible, because the
            # other half is a click somewhere else entirely.
            anchor = pin_anchor(drawing, *self.pending_pin)
            if anchor is not None:
                pen = QPen(QColor(HIGHLIGHT))
                pen.setWidthF(HIGHLIGHT_MM * 0.7)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(anchor, PICK_MM, PICK_MM)

        self._labels(painter)

    def _stroke(
        self, painter: QPainter, path: Sequence[Point2], net_class: str, lit: bool
    ) -> None:
        pen = QPen(QColor(HIGHLIGHT if lit else _class_colour(net_class)))
        pen.setWidthF(HIGHLIGHT_MM if lit else WIRE_MM)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(QPolygonF([_point(point) for point in path]))

    def _rail_glyph(self, painter: QPainter, rail: Rail) -> None:
        """Three shrinking bars for ground, a bar on a stem for power.

        Two glyphs rather than one coloured one: a schematic is read at a glance and often
        printed in black, and a reader should not have to know a colour convention to tell
        the rail that sinks from the rail that sources.

        WHERE the bars are is not this file's to decide -- ``schematic.rail_glyph_bars``
        answers that for the exported sheet too, and a glyph drawn one way on screen and
        another on paper would be two answers to the question the glyph exists to answer.
        """
        lit = rail.net_id in self.highlight_nets
        pen = QPen(QColor(HIGHLIGHT if lit else _class_colour(rail.net_class)))
        pen.setWidthF(HIGHLIGHT_MM if lit else BODY_MM)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        for start, end in rail_glyph_bars(rail):
            painter.drawLine(_point(start), _point(end))

    def _symbol(self, painter: QPainter, symbol: Symbol) -> None:
        lit = symbol.ref in self.highlight_refs
        colour = HIGHLIGHT if lit else (UNDEFINED if symbol.undefined else INK)
        pen = QPen(QColor(colour))
        pen.setWidthF(HIGHLIGHT_MM if lit else BODY_MM)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        if symbol.undefined:
            # A net is wired to a part nothing in the document defines, so its pins are
            # whatever the netlist happened to mention. Dashed because it is a hole in the
            # design. NOT dashed merely for being unplaced: on a schematic being drawn that
            # is every symbol on the sheet, and a sheet drawn entirely in dashes says
            # nothing.
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.save()
        painter.translate(symbol.at.x, symbol.at.y)
        painter.setPen(pen)
        for shape in symbol.shapes:
            if shape.kind == "circle":
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(_point(shape.points[0]), shape.radius, shape.radius)
                continue
            polygon = QPolygonF([_point(point) for point in shape.points])
            if shape.kind == "polygon":
                painter.setBrush(QBrush(QColor(colour)) if shape.filled else Qt.BrushStyle.NoBrush)
                painter.drawPolygon(polygon)
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPolyline(polygon)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.restore()

    def _labels(self, painter: QPainter) -> None:
        alignment = {
            "left": Qt.AlignmentFlag.AlignLeft,
            "centre": Qt.AlignmentFlag.AlignHCenter,
            "right": Qt.AlignmentFlag.AlignRight,
        }
        for label in self.drawing.labels:
            if label.kind == "ref":
                colour, size, bold = INK, REF_PX, True
                vertical = Qt.AlignmentFlag.AlignBottom
            elif label.kind == "value":
                colour, size, bold = INK_DIM, VALUE_PX, False
                vertical = Qt.AlignmentFlag.AlignTop
            elif label.kind == "net":
                colour, size, bold = SIGNAL, NET_PX, False
                vertical = Qt.AlignmentFlag.AlignBottom
            else:
                colour, size, bold = INK_DIM, PIN_PX, False
                vertical = Qt.AlignmentFlag.AlignVCenter
            painter.setPen(QPen(QColor(colour)))
            draw_label(
                painter,
                _point(label.at),
                label.text,
                size,
                alignment[label.anchor] | vertical,
                bold=bold,
            )


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------


class SchematicView(QGraphicsView):
    """Pan, zoom, and report what was clicked.

    ``set_drawing`` deliberately leaves the viewpoint alone -- the same rule
    ``view3d.populate_renderer`` follows about the camera. Editing one net must not throw
    away the part of the sheet somebody was looking at; ``fit`` is the only thing that
    moves the view, and it is called on the first drawing and from the button.
    """

    #: A symbol was clicked. Carries the reference, which is what the board speaks.
    partClicked = Signal(str)
    #: A symbol was double-clicked.
    partActivated = Signal(str)
    #: A wire or rail was clicked. Carries the net id.
    netClicked = Signal(str)
    #: A pin was clicked while wiring. Carries the reference and the pin number.
    pinClicked = Signal(str, str)
    #: Nothing was clicked.
    cleared = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.item: SheetItem | None = None
        self._fitted = False
        self.wiring = False
        self.pending_pin: tuple[str, str] | None = None
        self.setBackgroundBrush(QBrush(QColor(SHEET)))
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)

    def set_drawing(self, drawing: SchematicDrawing) -> None:
        refs = self.item.highlight_refs if self.item else frozenset()
        nets = self.item.highlight_nets if self.item else frozenset()
        self._scene.clear()
        self.item = SheetItem(drawing)
        self.item.set_highlight(refs, nets)
        self.item.pending_pin = self.pending_pin
        self._scene.addItem(self.item)
        self._scene.setSceneRect(self.item.boundingRect())
        if not self._fitted and drawing.symbols:
            self.fit()

    def set_highlight(self, refs: Iterable[str], net_ids: Iterable[str]) -> None:
        if self.item is not None:
            self.item.set_highlight(refs, net_ids)

    def set_wiring(self, on: bool) -> None:
        """Arm or disarm joining two pins by clicking them.

        Panning is turned off while it is armed. A drag on an empty part of the sheet is
        how you pan, and it is also how a missed click on a pin reads -- so leaving both
        gestures on the left button means every near miss scrolls the sheet out from under
        the pin you were aiming at.
        """
        self.wiring = on
        self.set_pending_pin(None)
        self.setDragMode(
            QGraphicsView.DragMode.NoDrag if on else QGraphicsView.DragMode.ScrollHandDrag
        )
        self.setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.ArrowCursor)

    def set_pending_pin(self, pin: tuple[str, str] | None) -> None:
        self.pending_pin = pin
        if self.item is not None:
            self.item.pending_pin = pin
            self.item.update()

    def pin_at(self, scene_pos: QPointF) -> tuple[str, str] | None:
        """The nearest pin within ``PIN_PICK_MM``, as ``(reference, pin number)``.

        Nearest rather than first, for the reason ``net_at`` is: the pins of a DIP are one
        pitch apart and picking whichever was built first would make half of them
        unreachable.
        """
        if self.item is None:
            return None
        best: tuple[float, str, str] | None = None
        for symbol in self.item.drawing.symbols:
            for pin in symbol.pins:
                dx = scene_pos.x() - (symbol.at.x + pin.at.x)
                dy = scene_pos.y() - (symbol.at.y + pin.at.y)
                distance = (dx * dx + dy * dy) ** 0.5
                if distance <= PIN_PICK_MM and (best is None or distance < best[0]):
                    best = (distance, symbol.ref, pin.number)
        return (best[1], best[2]) if best is not None else None

    def fit(self) -> None:
        if self.item is None:
            return
        sheet = QRectF(0.0, 0.0, self.item.drawing.width, self.item.drawing.height)
        if sheet.isEmpty():
            return
        self.fitInView(sheet, Qt.AspectRatioMode.KeepAspectRatio)
        self._fitted = True

    # -- picking -------------------------------------------------------------

    def symbol_at(self, scene_pos: QPointF) -> Symbol | None:
        if self.item is None:
            return None
        for symbol in self.item.drawing.symbols:
            box = QRectF(symbol.at.x, symbol.at.y, symbol.width, symbol.height)
            if box.contains(scene_pos):
                return symbol
        return None

    def net_at(self, scene_pos: QPointF) -> tuple[str, str] | None:
        """The nearest wire or rail within ``PICK_MM``, as ``(net_id, net_name)``.

        Nearest rather than first: two nets can run a millimetre apart in a crowded
        channel, and picking whichever happened to be built first would make one of them
        unselectable.
        """
        if self.item is None:
            return None
        best: tuple[float, str, str] | None = None
        runs: list[Wire | Rail] = [*self.item.drawing.wires, *self.item.drawing.rails]
        for run in runs:
            for start, end in zip(run.path, run.path[1:], strict=False):
                distance = _distance_to_segment(scene_pos, _point(start), _point(end))
                if distance <= PICK_MM and (best is None or distance < best[0]):
                    best = (distance, run.net_id, run.net_name)
        return (best[1], best[2]) if best is not None else None

    # -- gestures ------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        steps = event.angleDelta().y() / 120.0
        if not steps:
            return
        factor = 1.18**steps
        self.scale(factor, factor)
        self._fitted = True
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.wiring:
            pin = self.pin_at(self.mapToScene(event.position().toPoint()))
            if pin is not None:
                self.pinClicked.emit(pin[0], pin[1])
            else:
                # A miss cancels the half-made pair rather than leaving it armed. The
                # alternative is a stale first pin joining itself to whatever is clicked
                # three gestures later.
                self.set_pending_pin(None)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            where = self.mapToScene(event.position().toPoint())
            symbol = self.symbol_at(where)
            if symbol is not None:
                self.partClicked.emit(symbol.ref)
                event.accept()
                return
            net = self.net_at(where)
            if net is not None:
                self.netClicked.emit(net[0])
                event.accept()
                return
            self.cleared.emit()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        symbol = self.symbol_at(self.mapToScene(event.position().toPoint()))
        if symbol is not None:
            self.partActivated.emit(symbol.ref)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        where = self.mapToScene(event.position().toPoint())
        self.setToolTip(self.describe(where))
        super().mouseMoveEvent(event)

    def describe(self, scene_pos: QPointF) -> str:
        """What is under the cursor, as one line. Empty when that is nothing.

        Separate from the tooltip so it can be tested without a mouse, which is the only
        way this file gets tested at all on a machine with no font database.

        While wiring it names PINS and nothing else, because a pin is the only thing a
        click can land on then — a tooltip offering a net under the cursor would be
        describing something the next click cannot select.
        """
        if self.wiring:
            pin = self.pin_at(scene_pos)
            return f"{pin[0]}.{pin[1]}" if pin is not None else ""
        symbol = self.symbol_at(scene_pos)
        if symbol is not None:
            parts = [symbol.ref]
            if symbol.value:
                parts.append(symbol.value)
            if symbol.footprint_id:
                parts.append(symbol.footprint_id)
            if symbol.undefined:
                parts.append("not in the design")
            elif symbol.unplaced:
                parts.append("not placed yet")
            return " · ".join(parts)
        net = self.net_at(scene_pos)
        return net[1] if net is not None else ""


def _distance_to_segment(point: QPointF, start: QPointF, end: QPointF) -> float:
    dx, dy = end.x() - start.x(), end.y() - start.y()
    length = dx * dx + dy * dy
    if length <= 1e-12:
        return QPointF(point - start).manhattanLength()
    t = ((point.x() - start.x()) * dx + (point.y() - start.y()) * dy) / length
    t = max(0.0, min(1.0, t))
    near = QPointF(start.x() + t * dx, start.y() + t * dy)
    delta = point - near
    return float((delta.x() ** 2 + delta.y() ** 2) ** 0.5)
