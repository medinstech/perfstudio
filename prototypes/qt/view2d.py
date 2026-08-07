"""2D board editor on QGraphicsView.

The scene works in MILLIMETRES, one scene unit = 1 mm. That single decision is what
makes the 1:1 printing claim testable: painting the same scene into a QPrinter at
100% scale gives a physically correct sheet with no fudge factors.

What this prototype is trying to answer:
  - does QGraphicsView feel right for a grid CAD editor, or are we fighting it?
  - is per-item picking and grid-snapped dragging as cheap as it looks?
  - can the pad grid stay fast without hand-rolling a culling layer?
"""
from __future__ import annotations

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

from board_model import Board, Component, Conductor, Document, Footprint, hole_ref

# --------------------------------------------------------------------------- theme

SUBSTRATE = {"FR4": QColor("#2e6b3f"), "FR2": QColor("#a8834e"), "FR1": QColor("#b8925c")}
PAD = QColor("#c8a951")
PAD_RING = QColor("#8a7331")
DRILL = QColor("#2a2a2e")
BODY_FILL = QColor(230, 230, 235, 210)
BODY_LINE = QColor("#31313a")
LABEL = QColor("#15151a")
SELECTED = QColor("#0a84ff")
RISK = QColor("#e5484d")

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


# ------------------------------------------------------------------ pad grid item


class PadGridItem(QGraphicsItem):
    """The whole hole grid as ONE item, painting only what is exposed.

    A 100x60 board is 6000 pads. As individual QGraphicsItems that is survivable but
    wasteful; as one item using the exposedRect from the paint option it is trivial,
    and Qt does the culling for us — which is exactly the kind of thing we hand-wrote
    in the TypeScript renderer.
    """

    def __init__(self, board: Board) -> None:
        super().__init__()
        self.board = board
        self.drawn = 0
        self.setZValue(-90)

    def boundingRect(self) -> QRectF:
        w, h = self.board.size_mm
        p = self.board.pitch
        return QRectF(-p / 2, -p / 2, w, h)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        b = self.board
        area = option.exposedRect
        pad_r = b.pad_diameter / 2
        drill_r = b.drill_diameter / 2

        c0 = max(0, int((area.left() - pad_r) / b.pitch))
        c1 = min(b.cols - 1, int((area.right() + pad_r) / b.pitch) + 1)
        r0 = max(0, int((area.top() - pad_r) / b.pitch))
        r1 = min(b.rows - 1, int((area.bottom() + pad_r) / b.pitch) + 1)

        painter.setPen(QPen(PAD_RING, 0.12))
        painter.setBrush(QBrush(PAD))
        count = 0
        for col in range(c0, c1 + 1):
            for row in range(r0, r1 + 1):
                x, y = b.hole_to_mm(col, row)
                painter.drawEllipse(QPointF(x, y), pad_r, pad_r)
                count += 1

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(DRILL))
        for col in range(c0, c1 + 1):
            for row in range(r0, r1 + 1):
                x, y = b.hole_to_mm(col, row)
                painter.drawEllipse(QPointF(x, y), drill_r, drill_r)
        self.drawn = count


# ------------------------------------------------------------------ conductor item


class ConductorItem(QGraphicsItem):
    """Each conductor kind has to be tellable apart at a glance.

    A solder trace gets a bead at EVERY pad it crosses; a wire gets a fillet only at
    its two endpoints, because that is the physical and electrical truth (a wire
    passing over a pad does not connect to it).
    """

    def __init__(self, conductor: Conductor, board: Board) -> None:
        super().__init__()
        self.conductor = conductor
        self.board = board
        self.setZValue(-50 if conductor.side == "bottom" else 40)
        self.setToolTip(
            f"{conductor.kind}  {hole_ref(*conductor.path[0])} to {hole_ref(*conductor.path[-1])}"
        )

    def _points(self) -> list[QPointF]:
        return [QPointF(*self.board.hole_to_mm(c, r)) for c, r in self.conductor.path]

    def boundingRect(self) -> QRectF:
        pts = self._points()
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        pad = 2.0
        return QRectF(min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad,
                      max(ys) - min(ys) + 2 * pad)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        k = self.conductor.kind
        colour, width, dashed = CONDUCTOR_STYLE.get(k, (QColor("#888"), 0.6, False))
        if self.conductor.color:
            colour = QColor(self.conductor.color)

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

        if self.conductor.contacts_every_hole:
            # Beads at every pad: this trace is soldered down all along its length.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(colour.lighter(115)))
            for p in pts:
                painter.drawEllipse(p, width * 0.62, width * 0.62)
            if self.conductor.spine:
                spine = QPen(colour.darker(150), 0.35)
                painter.setPen(spine)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
        else:
            # Fillets at the endpoints only.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(colour.lighter(125)))
            for p in (pts[0], pts[-1]):
                painter.drawEllipse(p, width * 0.75, width * 0.75)


# ------------------------------------------------------------------ component item


class ComponentItem(QGraphicsItem):
    """A placed part. Movable, and snapped to the hole grid on release."""

    def __init__(self, comp: Component, fp: Footprint, board: Board) -> None:
        super().__init__()
        self.comp = comp
        self.fp = fp
        self.board = board
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setZValue(10)
        self.setToolTip(f"{comp.ref}  {comp.value}\n{fp.name}\nanchor {hole_ref(comp.col, comp.row)}")
        self._sync_position()

    def _sync_position(self) -> None:
        x, y = self.board.hole_to_mm(self.comp.col, self.comp.row)
        self.setPos(x, y)

    def _local_outline(self) -> QPolygonF:
        poly = QPolygonF()
        for px, py in self.fp.body_outline:
            dx, dy = self.comp.transform_offset(px, py)
            poly.append(QPointF(dx, dy))
        return poly

    def boundingRect(self) -> QRectF:
        return self._local_outline().boundingRect().adjusted(-1.5, -1.5, 1.5, 3.0)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        poly = self._local_outline()
        painter.setBrush(QBrush(BODY_FILL))
        painter.setPen(QPen(SELECTED if self.isSelected() else BODY_LINE,
                            0.35 if self.isSelected() else 0.2))
        painter.drawPolygon(poly)

        # Pin markers, with pin 1 called out because getting a part round the wrong
        # way is one of the failure modes the tool exists to prevent.
        painter.setPen(QPen(QColor("#42464b"), 0.15))
        for pin in self.fp.pins:
            dx, dy = self.comp.transform_offset(pin.d_col, pin.d_row)
            centre = QPointF(dx * self.board.pitch, dy * self.board.pitch)
            first = pin.number == "1"
            painter.setBrush(QBrush(QColor("#e5484d") if first and self.fp.polarized
                                    else QColor("#7c848c")))
            painter.drawEllipse(centre, 0.55, 0.55)

        font = QFont()
        font.setPointSizeF(1.6)
        painter.setFont(font)
        painter.setPen(QPen(LABEL))
        rect = poly.boundingRect()
        painter.drawText(QPointF(rect.left(), rect.top() - 0.5), self.comp.ref)

    def itemChange(self, change, value):  # noqa: ANN001, ANN201
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene():
            # Snap to the hole grid while dragging — a part can only ever sit in holes.
            p: QPointF = value
            pitch = self.board.pitch
            col = round(p.x() / pitch)
            row = round(p.y() / pitch)
            col = max(0, min(self.board.cols - 1, col))
            row = max(0, min(self.board.rows - 1, row))
            self.comp.col, self.comp.row = col, row
            return QPointF(col * pitch, row * pitch)
        return super().itemChange(change, value)


# ------------------------------------------------------------------------- scene


class BoardScene(QGraphicsScene):
    componentMoved = Signal(str, int, int)

    def __init__(self, doc: Document) -> None:
        super().__init__()
        self.doc = doc
        b = doc.board
        w, h = b.size_mm
        self.setSceneRect(-b.pitch / 2 - 4, -b.pitch / 2 - 4, w + 8, h + 8)
        self.setBackgroundBrush(QBrush(QColor("#15161a")))

        substrate = self.addRect(
            QRectF(-b.pitch / 2, -b.pitch / 2, w, h),
            QPen(QColor("#1d1f24"), 0.3),
            QBrush(SUBSTRATE.get(b.material, SUBSTRATE["FR4"])),
        )
        substrate.setZValue(-100)

        self.pad_grid = PadGridItem(b)
        self.addItem(self.pad_grid)

        for conductor in doc.conductors:
            self.addItem(ConductorItem(conductor, b))

        self.component_items: list[ComponentItem] = []
        for comp in doc.components:
            fp = doc.footprint(comp.footprint_id)
            if fp is None:
                continue
            item = ComponentItem(comp, fp, b)
            self.addItem(item)
            self.component_items.append(item)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        super().mouseReleaseEvent(event)
        for item in self.selectedItems():
            if isinstance(item, ComponentItem):
                self.componentMoved.emit(item.comp.ref, item.comp.col, item.comp.row)


class BoardView(QGraphicsView):
    def __init__(self, scene: BoardScene) -> None:
        super().__init__(scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.scale(6, 6)  # ~6 px per mm to start

    def wheelEvent(self, event) -> None:  # noqa: ANN001
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
