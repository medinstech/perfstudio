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
from perfstudio.commands import MoveComponentPayload
from perfstudio.connectivity import FootprintLookup
from perfstudio.drc import DrcViolation
from perfstudio.geometry import board_size_mm, hole_span_mm, transform_offset
from perfstudio.model import (
    Board,
    BoardSide,
    Conductor,
    ComponentInstance,
    Footprint,
    HoleCoord,
    PerfDocument,
    contacts_every_path_hole,
)

# --------------------------------------------------------------------------- theme

SUBSTRATE = {"FR4": QColor("#2e6b3f"), "FR2": QColor("#a8834e"), "FR1": QColor("#b8925c")}
PAD = QColor("#c8a951")
PAD_RING = QColor("#8a7331")
DRILL = QColor("#2a2a2e")
BODY_FILL = QColor(230, 230, 235, 210)
BODY_LINE = QColor("#31313a")
LABEL = QColor("#15151a")
SELECTED = QColor("#0a84ff")
ERROR_OUTLINE = QColor("#e5484d")
RISK_RING = QColor("#e5484d")

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

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(DRILL))
        for col in range(c0, c1 + 1):
            for row in range(r0, r1 + 1):
                p = hole_to_screen(HoleCoord(col, row), b, self.side)
                painter.drawEllipse(p, drill_r, drill_r)
        self.drawn = count


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
        return self._local_outline().boundingRect().adjusted(-1.5, -1.5, 1.5, 3.0)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        poly = self._local_outline()
        painter.setBrush(QBrush(BODY_FILL))
        if self.isSelected():
            pen_color, pen_width = SELECTED, 0.35
        elif self.has_error:
            pen_color, pen_width = ERROR_OUTLINE, 0.35
        else:
            pen_color, pen_width = BODY_LINE, 0.2
        painter.setPen(QPen(pen_color, pen_width))
        painter.drawPolygon(poly)

        # Pin markers, with pin 1 called out on polarized parts.
        painter.setPen(QPen(QColor("#42464b"), 0.15))
        for pin in self.fp.pins:
            dx, dy = _local_offset(pin.d_col, pin.d_row, self.comp, self.side)
            centre = QPointF(dx * self.board.pitch, dy * self.board.pitch)
            first = pin.number == "1"
            painter.setBrush(QBrush(QColor("#e5484d") if first and self.fp.polarized else QColor("#7c848c")))
            painter.drawEllipse(centre, 0.55, 0.55)

        font = QFont()
        font.setPointSizeF(1.6)
        painter.setFont(font)
        painter.setPen(QPen(LABEL))
        rect = poly.boundingRect()
        painter.drawText(QPointF(rect.left(), rect.top() - 0.5), self.comp.ref)

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

    def __init__(
        self,
        document: PerfDocument,
        lookup: FootprintLookup,
        side: BoardSide = "top",
        bus: CommandBus | None = None,
    ) -> None:
        super().__init__()
        self.lookup = lookup
        self.side = side
        self.bus = bus
        self.document = document
        self.violations: tuple[DrcViolation, ...] = ()
        self.component_items: dict[str, ComponentItem] = {}
        self.pad_grid: PadGridItem | None = None
        self._risk_item: RiskRingsItem | None = None
        self._build()

    # -- (re)building -----------------------------------------------------

    def set_document(self, document: PerfDocument) -> None:
        self.document = document
        self._build()

    def set_side(self, side: BoardSide) -> None:
        if side != self.side:
            self.side = side
            self._build()

    def _build(self) -> None:
        self.clear()
        self.component_items = {}
        self._risk_item = None
        board = self.document.board
        w, h = board_size_mm(board)
        self.setSceneRect(-board.pitch / 2 - 4, -board.pitch / 2 - 4, w + 8, h + 8)
        self.setBackgroundBrush(QBrush(QColor("#15161a")))

        substrate = self.addRect(
            QRectF(-board.pitch / 2, -board.pitch / 2, w, h),
            QPen(QColor("#1d1f24"), 0.3),
            QBrush(SUBSTRATE.get(board.material, SUBSTRATE["FR4"])),
        )
        substrate.setZValue(-100)

        self.pad_grid = PadGridItem(board, self.side)
        self.addItem(self.pad_grid)

        for conductor in self.document.conductors:
            self.addItem(ConductorItem(conductor, board, self.side))

        for comp in self.document.components:
            fp = self.lookup(comp.footprint_id)
            if fp is None:
                continue
            item = ComponentItem(comp, fp, board, self.side)
            self.addItem(item)
            self.component_items[comp.id] = item

        self._apply_violations()

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

    # -- dragging: dispatch on release, never per-frame ----------------------

    def mouseReleaseEvent(self, event: Any) -> None:
        super().mouseReleaseEvent(event)
        self.commit_pending_moves()

    def commit_pending_moves(self) -> list[DispatchResult]:
        """Dispatch ``component.move`` for every item whose snapped position differs
        from its last-known document anchor. Called on mouse release (never per drag
        frame -- see the module docstring), and directly by tests so drag behaviour is
        exercisable without synthesizing real Qt mouse events.
        """
        if self.bus is None:
            return []
        results: list[DispatchResult] = []
        for comp_id, item in self.component_items.items():
            if item.pending_anchor != item.comp.anchor:
                result = self.bus.dispatch(
                    "component.move",
                    MoveComponentPayload(id=comp_id, anchor=item.pending_anchor),
                )
                results.append(result)
        if results:
            self.moveCommitted.emit(results)
        return results


class BoardView(QGraphicsView):
    def __init__(self, scene: BoardScene) -> None:
        super().__init__(scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.scale(6, 6)  # ~6 px per mm to start

    def wheelEvent(self, event: Any) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def center_on_holes(self, holes: Sequence[HoleCoord], board: Board, side: BoardSide) -> None:
        """Select-and-centre support for the DRC/LVS dock: frame the given holes."""
        if not holes:
            return
        pts = [hole_to_screen(h, board, side) for h in holes]
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        margin = 6.0
        rect = QRectF(min(xs) - margin, min(ys) - margin, max(xs) - min(xs) + 2 * margin, max(ys) - min(ys) + 2 * margin)
        self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
