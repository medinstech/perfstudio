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
from typing import Any, cast

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
)

from perfstudio.command import CommandBus, DispatchResult
from perfstudio.commands import (
    AddConductorPayload,
    AddCutPayload,
    AddNetPayload,
    ConnectPinsPayload,
    DeleteCutPayload,
    MoveComponentPayload,
    NewConductor,
    NewSolderTraceConductor,
    NewWireConductor,
    PlaceComponentPayload,
)
from perfstudio.connectivity import FootprintLookup
from perfstudio.drc import DrcViolation
from perfstudio.geometry import (
    all_pin_holes,
    board_edge_margin_mm,
    board_outline_mm,
    column_label,
    edge_connector_holes,
    edge_finger_rect,
    format_hole,
    hole_key,
    hole_span_mm,
    holes_without_grid_pad,
    is_inside_board,
    legend_strip_mm,
    manhattan,
    pad_extent_mm,
    path_length_mm,
    printed_row_label,
    row_label,
    transform_offset,
    transform_pin_offset,
    undrilled_holes,
)

# The one place the wire-colour convention is defined, so the editor and the cut list a
# person actually works from cannot disagree about which wire is which.
from perfstudio.guide import COLOR_BY_NET_CLASS, SIGNAL_COLORS
from perfstudio.model import (
    Board,
    BoardLabels,
    BoardSide,
    ComponentInstance,
    Conductor,
    ConductorKind,
    EdgeConnector,
    Footprint,
    HoleCoord,
    MountingHole,
    Net,
    NetClass,
    NetNode,
    PerfDocument,
    TrackCut,
    contacts_every_path_hole,
)
from perfstudio.ratsnest import RatsnestLink, all_links, ratsnest
from perfstudio.stripboard import cut_holes, is_stripboard, segments

from .boardcolors import scheme_for
from .bodies import (
    BodyPlacement,
    BodyStyle,
    leads_for,
    placement_for,
    polarity_pin_offset,
    resistor_bands,
    style_for,
    surface_for,
)
from .scenetext import draw_label, draw_physical_label

# The window's own palette, for the overlays that sit ON the board but belong to the
# application rather than to the object -- see theme.py's note on why the two are apart.
from .theme import ACCENT as THEME_ACCENT
from .theme import BORDER as THEME_BORDER
from .theme import PANEL as THEME_PANEL
from .theme import TEXT as THEME_TEXT
from .theme import TEXT_DIM as THEME_TEXT_DIM

# --------------------------------------------------------------------------- theme

BACKGROUND = QColor("#12131a")
SUBSTRATE = {"FR4": QColor("#2e6b3f"), "FR2": QColor("#a8834e"), "FR1": QColor("#b8925c")}
SUBSTRATE_EDGE = QColor("#0d1a12")
#: The copper comes from the board's own scheme, because a phenolic board's pads are
#: BARE copper and a plated FR-4 board's are gold. These are the fallbacks for a painter
#: with no board to ask, and are the plated ones.
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
#: The part on the far side, seen through the board. Dim on purpose: on the solder side
#: the pads are the subject, and anything that competes with them for attention is a way
#: to solder the wrong pad.
BODY_SHADOW = QColor("#95a3b8")
BODY_SHADOW_EDGE = QColor("#6d7a8c")
RISK_RING = QColor("#e5484d")
RULER_TEXT = QColor("#8b93a7")
RULER_TEXT_MAJOR = QColor("#d3d9e8")
#: Silkscreen. White ink on the board it is printed on, and a dim ghost of it when the
#: board is turned over -- the same convention component bodies on the far side follow.
LEGEND_INK = QColor("#eef2f8")
LEGEND_INK_FAR = QColor(190, 200, 215, 70)
#: A mounting hole: the bore is a hole, so it is near black; the head ring is a keepout
#: rather than a thing, so it is a dashed outline in the warning colour.
MOUNT_BORE = QColor("#14151b")
MOUNT_EDGE = QColor("#0a0b0f")
MOUNT_KEEPOUT = QColor(229, 116, 61, 150)
#: A connector finger on the far face of the board.
FINGER_FAR = QColor(150, 130, 70, 110)
#: A track cut. The error colour, because a cut in the wrong hole is an error you cannot
#: see on the finished board from more than arm's length.
CUT_MARK = QColor("#e5484d")

#: Ratsnest colours by net class -- ground and power read as rails at a glance, which is
#: the same distinction the router orders its work by.
RATSNEST = {
    "ground": QColor("#7f8794"),
    "power": QColor("#e0723c"),
    "signal": QColor("#3fa9d4"),
}
RATSNEST_HIGHLIGHT = QColor("#ffd166")

#: kind -> (colour, stroke width mm, dashed).
#:
#: WIDTHS ARE PHYSICAL, and that is the point. A 1.9 mm pad is the reference: solder
#: sits inside it rather than swallowing it, tinned wire is about half a millimetre, and
#: insulated wire is thicker than bare because of the sleeve. These used to be set for
#: legibility alone, which made every trace wider than the pads it joined and turned a
#: routed board into a diagram of coloured bars with a board somewhere underneath.
#:
#: RED IS NOT A CONDUCTOR COLOUR here. It is the error and R5'-risk colour, and it was
#: also every insulated wire, so a fully correct board looked alarming. Insulated wire
#: now takes its NET's colour instead -- the same convention the build guide's cut list
#: prints, so the screen and the list a person works from agree.
CONDUCTOR_STYLE = {
    "solder-trace": (QColor("#b9bec6"), 0.9, False),
    "solder-trace-wired": (QColor("#aeb4bd"), 1.0, False),
    "bare-wire": (QColor("#dfe4ea"), 0.5, False),
    "insulated-wire": (QColor("#8e97a3"), 0.85, False),
    "top-jumper": (QColor("#8e97a3"), 0.75, True),
    "lead-bend": (QColor("#c2c8d0"), 0.45, False),
    "strip": (QColor("#c8a951"), 2.0, False),
}

#: Insulation colours, matching guide.py's cut list exactly. Named there and mapped to
#: screen colours here, because the guide names a colour a human buys ("black") and this
#: needs one that reads on a dark board.
_INSULATION_SCREEN = {
    "red": QColor("#e05252"),
    "black": QColor("#5c6470"),
    "yellow": QColor("#e2c541"),
    "green": QColor("#5cb85c"),
    "blue": QColor("#5b8dd9"),
    "white": QColor("#e8ecf2"),
    "orange": QColor("#e08b3c"),
    "violet": QColor("#a978d8"),
    "grey": QColor("#9aa2ad"),
    "brown": QColor("#a5764f"),
}

#: The copper spine inside a wired solder trace, drawn as a core through the solder.
SPINE_CORE = QColor("#c08a4a")


def insulation_color(net_class: NetClass | None, signal_index: int) -> QColor:
    """The colour a wire on this net should be, by the build guide's own convention.

    Red for power and black for ground, then a fixed cycle for signals -- assigned by
    net order, so the same board always gets the same colours in the editor and in the
    cut list. A convention that changed between the screen and the printout would be
    worse than none.
    """
    if net_class is None:
        return CONDUCTOR_STYLE["insulated-wire"][0]
    fixed = COLOR_BY_NET_CLASS.get(net_class)
    name = fixed if fixed is not None else SIGNAL_COLORS[signal_index % len(SIGNAL_COLORS)]
    return _INSULATION_SCREEN.get(name, CONDUCTOR_STYLE["insulated-wire"][0])

#: Millimetres of scene reserved outside the substrate for the hole-address rulers.
RULER_MARGIN_MM = 6.0

#: Label sizes in SCREEN PIXELS, not millimetres or points -- see ui/scenetext.py. These
#: hold their size while the board zooms, which is both what an annotation should do and
#: what keeps them clear of the size below which some font engines draw nothing.
RULER_LABEL_PX = 11
REF_LABEL_PX = 12


# --------------------------------------------------------------------- coordinates


def _outline_rect(board: Board) -> QRectF:
    """The substrate as a Qt rect, from ``geometry.board_outline_mm``.

    Every item that needs to know where the board's edge is asks this rather than
    assuming half a pitch past the outer holes: a board with a printed border has more
    substrate than that, and an item that guessed would draw its edge in the wrong place
    while the substrate underneath drew its own somewhere else.
    """
    outline = board_outline_mm(board)
    return QRectF(outline.x, outline.y, outline.width, outline.height)


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

    ONE PAD IS DRAWN, ONCE, INTO A PIXMAP, and blitted per hole. That is the difference
    between a large board being usable and not: painting a 100x60 board (6000 holes) the
    obvious way -- a filled ring, a sheen arc and a drill circle per hole, three
    antialiased ellipse passes -- measured 124 ms a frame, which is 8 fps and feels like
    treacle to pan. Blitting a pre-rendered pad measured 12 ms. Ten times faster for a
    pad that looks the same, because every pad on a board is identical by definition and
    rasterising it 6000 times is 6000 times more work than necessary.

    Two approaches that did NOT work, recorded so nobody spends the afternoon again:
    collecting every ring into one even-odd QPainterPath and filling it in a single call
    took 5.8 SECONDS (Qt's scanline fill degrades badly past a few thousand subpaths),
    and turning antialiasing off only got 124 ms to 64 ms while making the board look
    cheap.

    Qt's ``exposedRect`` culling still does the first and largest share of the work: when
    zoomed in, most of the grid is never considered at all.
    """

    #: Pad sizes are bucketed to this many device pixels before the cache is rebuilt.
    #: Without bucketing, a smooth zoom re-rasterises the pad every frame and hands back
    #: the cost the cache exists to remove.
    _SIZE_BUCKET_PX = 4

    def __init__(
        self,
        board: Board,
        side: BoardSide,
        consumed: frozenset[str] = frozenset(),
        copper: bool = True,
    ) -> None:
        super().__init__()
        self.board = board
        self.side = side
        #: False draws the drilled holes with no copper round them -- the component side
        #: of a single-sided board, which still has every hole and none of the pads.
        #: Without this that face renders as a blank slab, which is the same mistake the
        #: 3D view's `build_drills` exists to correct.
        self.copper = copper
        #: Hole keys with no ordinary round pad on this face -- a mounting bore took the
        #: copper, or a connector finger IS the pad here. Passed in rather than derived,
        #: because it is a property of the whole document and this is a paint path: it
        #: must not walk the mounting holes once per hole.
        self.consumed = consumed
        self.drawn = 0
        self._pad_pixmap: QPixmap | None = None
        self._pad_pixmap_px = 0
        self.setZValue(-90)

    def boundingRect(self) -> QRectF:
        return _outline_rect(self.board)

    def _pad_for(self, long_px: float) -> QPixmap:
        """A pad rasterised for roughly this on-screen size, cached between frames.

        Not necessarily square. An oblong pad is a stadium -- a rectangle capped with a
        semicircle at each end, which is what the copper on those boards actually is --
        so the pixmap carries the pad's aspect ratio and everything below is expressed
        as a fraction of it rather than of a single "side".
        """
        bucket = max(
            self._SIZE_BUCKET_PX,
            round(long_px / self._SIZE_BUCKET_PX) * self._SIZE_BUCKET_PX,
        )
        # Rendered at twice the on-screen size so the downscale stays crisp when the zoom
        # sits between two buckets, and capped so a deep zoom cannot ask for a huge one.
        long_side = min(256, max(6, bucket * 2))
        if self._pad_pixmap is not None and self._pad_pixmap_px == long_side:
            return self._pad_pixmap

        extent_x, extent_y = pad_extent_mm(self.board)
        longest = max(extent_x, extent_y)
        width = max(3, round(long_side * extent_x / longest))
        height = max(3, round(long_side * extent_y / longest))

        pixmap = QPixmap(width, height)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.copper:
            drill_only = long_side * (self.board.drill_diameter / longest)
            painter.setPen(QPen(QColor("#2b1d0e"), max(1.0, long_side * 0.03)))
            painter.setBrush(QBrush(DRILL))
            painter.drawEllipse(
                QRectF(
                    (width - drill_only) / 2, (height - drill_only) / 2, drill_only, drill_only
                )
            )
            painter.end()
            self._pad_pixmap = pixmap
            self._pad_pixmap_px = long_side
            return pixmap
        inset = long_side * 0.03
        ring = QRectF(inset, inset, width - 2 * inset, height - 2 * inset)
        scheme = scheme_for(self.board.material)
        painter.setPen(QPen(QColor(scheme.pad_ring), max(1.0, long_side * 0.05)))
        painter.setBrush(QBrush(QColor(scheme.pad)))
        # Radius = half the short side, which turns the rounded rect into a true stadium
        # and, on a square pad, into the circle this used to draw.
        radius = min(ring.width(), ring.height()) / 2
        painter.drawRoundedRect(ring, radius, radius)

        # The sheen reads as tinned copper catching the light rather than a flat yellow
        # disc, and it is what makes the board look like a board. Skipped only when the
        # pad is too small on screen for the arc to be more than a smudge.
        if long_side >= 20:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(scheme.pad_sheen), max(1.0, long_side * 0.055)))
            sheen_w, sheen_h = width * 0.68, height * 0.68
            painter.drawArc(
                QRectF((width - sheen_w) / 2, (height - sheen_h) / 2, sheen_w, sheen_h),
                60 * 16,
                100 * 16,
            )

        drill_w = long_side * (self.board.drill_diameter / longest)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(DRILL))
        painter.drawEllipse(
            QRectF((width - drill_w) / 2, (height - drill_w) / 2, drill_w, drill_w)
        )
        painter.end()

        self._pad_pixmap = pixmap
        self._pad_pixmap_px = long_side
        return pixmap

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        b = self.board
        area = option.exposedRect
        extent_x, extent_y = pad_extent_mm(b)
        half_x, half_y = extent_x / 2, extent_y / 2

        c0 = max(0, int((area.left() - half_x) / b.pitch) - 1)
        c1 = min(b.cols - 1, int((area.right() + half_x) / b.pitch) + 1)
        r0 = max(0, int((area.top() - half_y) / b.pitch) - 1)
        r1 = min(b.rows - 1, int((area.bottom() + half_y) / b.pitch) + 1)

        px_per_mm = painter.transform().m11() or 1.0
        pixmap = self._pad_for(max(extent_x, extent_y) * abs(px_per_mm))
        source = QRectF(0, 0, pixmap.width(), pixmap.height())

        count = 0
        for col in range(c0, c1 + 1):
            for row in range(r0, r1 + 1):
                # A mounting bore removed this pad. Drawing it anyway would show copper
                # to solder to where there is a hole, which is the one thing the DRC rule
                # for it exists to prevent somebody discovering with an iron in hand.
                if self.consumed and hole_key(HoleCoord(col, row)) in self.consumed:
                    continue
                p = hole_to_screen(HoleCoord(col, row), b, self.side)
                painter.drawPixmap(
                    QRectF(p.x() - half_x, p.y() - half_y, extent_x, extent_y), pixmap, source
                )
                count += 1
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
        outline = _outline_rect(self.board)
        pad = RULER_MARGIN_MM + 30
        return outline.adjusted(-pad, -pad, pad, pad)

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
                QPointF(x, _outline_rect(board).top() - 1.4),
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
                QPointF(_outline_rect(board).left() - 1.2, row * board.pitch),
                row_label(row),
                RULER_LABEL_PX,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                bold=major,
            )


# ------------------------------------------------- printed legend and board features


class BoardLegendItem(QGraphicsItem):
    """The hole addresses the BOARD itself carries, printed on the substrate.

    Not the same thing as :class:`HoleRulerItem`, and the difference is the whole point.
    The ruler is an annotation this program draws outside the board and sizes in screen
    pixels, so it holds its size as you zoom. This is silkscreen: it is ink on the
    substrate, it is there when you are holding the board with the program closed, and
    it therefore scales with the board like every other physical dimension in this scene.
    That is also what makes it come out right on the 1:1 PDF, which is the copy that ends
    up taped to the board.

    It lives in the half-pitch margin the substrate extends past the outer hole centres
    (``geometry.board_size_mm``), because that is the only room there is. Real boards with
    a wider printed border have a margin this format does not model.

    SEEN FROM THE OTHER SIDE the legend is drawn dim, and NOT mirrored. Mirroring the
    glyphs would be the physically accurate thing and the useless one: reversed 1 mm text
    is noise, and what the reader wants from a label is the address, not the reflection.
    The position is mirrored -- that comes free from ``hole_to_screen`` -- so a label
    still sits over the hole it names.
    """

    def __init__(self, document: PerfDocument, labels: BoardLabels, side: BoardSide) -> None:
        super().__init__()
        self.document = document
        self.board = document.board
        self.labels = labels
        self.side = side
        #: Printed on this face, or seen faintly through the board from the other one.
        self.near_side = labels.face == "both" or labels.face == side
        self.setZValue(-95)  # On the substrate, under the pads: ink goes on first.

    def boundingRect(self) -> QRectF:
        return _outline_rect(self.board)

    def _free_strip_mm(self) -> tuple[float, float]:
        """Bare substrate between the outermost pads and the board edge, (across, down).

        NOT the whole margin. The outer pads eat into it -- half a pad's extent of it --
        and ink printed there would sit under copper, which on a 2.54 mm board is most of
        the margin gone. This is the strip a legend actually has, and both the size of the
        characters and where they are centred come from it.
        """
        return (
            legend_strip_mm(self.document, "horizontal"),
            legend_strip_mm(self.document, "vertical"),
        )

    def _text_height_mm(self) -> float:
        """Cap height. A real board prints this around 1.2 mm, and it is capped to
        whatever the free strip can hold when there is less room than that."""
        strip_x, strip_y = self._free_strip_mm()
        return min(1.15, min(strip_x, strip_y) * 0.6)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        board = self.board
        color = QColor(LEGEND_INK if self.near_side else LEGEND_INK_FAR)
        strip_x, strip_y = self._free_strip_mm()
        margin_x = board_edge_margin_mm(board, "horizontal")
        margin_y = board_edge_margin_mm(board, "vertical")
        height = self._text_height_mm()

        painter.save()
        painter.setPen(QPen(color))

        # Letters along the top (and bottom), numbers down the left (and right) -- the
        # layout every board in this class uses, and the reason `all_edges` defaults to
        # true: with one edge each, the far half of the board is nearest the edge that
        # does not carry its address, which is where counting starts again.
        #
        # Each run is centred in ITS OWN free strip, which is why the two offsets differ:
        # a column letter clears the copper above it, a row number clears it to its left.
        #
        # MEASURED IN FROM THE BOARD EDGE, not out from the pad. The two agree on a plain
        # board and do not on one with connector fingers, where the copper reaches most of
        # the way to the edge: measured from the pad, the letters land ON the fingers, and
        # since ink is drawn under copper they vanish entirely. `legend_strip_mm` already
        # reports the right WIDTH there (the fingers' inset); this is the matching
        # position for it.
        #
        # THE NUMBERS ARE TURNED ON THEIR SIDE, as they are on the real boards, and for a
        # reason rather than as decoration: the strip beside a row is narrow across and a
        # whole pitch deep, so an upright "07" has to shrink to fit while a turned one
        # does not.
        span_w, span_h = hole_span_mm(board)
        column_ys = [-(margin_y - strip_y / 2)]
        row_xs = [-(margin_x - strip_x / 2)]
        if self.labels.all_edges:
            column_ys.append(span_h + margin_y - strip_y / 2)
            row_xs.append(span_w + margin_x - strip_x / 2)
        elif self.side == "bottom":
            # A one-edge legend is on a PHYSICAL edge, and turning the board over moves
            # that edge to the other side of the screen. Reflected about the hole span
            # like every other x here, never about the substrate -- see the module note.
            row_xs = [span_w - x for x in row_xs]

        for col in range(board.cols):
            x = hole_to_screen(HoleCoord(col, 0), board, self.side).x()
            for y in column_ys:
                draw_physical_label(
                    painter, QPointF(x, y), column_label(col), height, max_width_mm=board.pitch * 0.9
                )

        for row in range(board.rows):
            text = printed_row_label(row, self.labels)
            for x in row_xs:
                draw_physical_label(
                    painter,
                    QPointF(x, row * board.pitch),
                    text,
                    height,
                    max_width_mm=board.pitch * 0.9,
                    rotation_deg=-90.0,
                )
        painter.restore()


class MountingHoleItem(QGraphicsItem):
    """A screw hole: the bore, and the ring the screw head will cover.

    The head ring is drawn because it is a keepout nobody can see otherwise -- the bore
    looks small and harmless, and the reason you cannot put a capacitor next to it is the
    washer, which is not on the board yet.
    """

    def __init__(self, mount: MountingHole, board: Board, side: BoardSide) -> None:
        super().__init__()
        self.mount = mount
        self.board = board
        self.side = side
        self.setZValue(-85)  # Over the pads it removed, under the parts.

    def boundingRect(self) -> QRectF:
        centre = hole_to_screen(self.mount.at, self.board, self.side)
        r = max(self.mount.head_diameter, self.mount.diameter) / 2 + 0.5
        return QRectF(centre.x() - r, centre.y() - r, 2 * r, 2 * r)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        centre = hole_to_screen(self.mount.at, self.board, self.side)
        head_r = self.mount.head_diameter / 2
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(MOUNT_KEEPOUT, 0.12)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawEllipse(centre, head_r, head_r)

        bore_r = self.mount.diameter / 2
        painter.setPen(QPen(MOUNT_EDGE, 0.15))
        painter.setBrush(QBrush(MOUNT_BORE))
        painter.drawEllipse(centre, bore_r, bore_r)


class StripGridItem(QGraphicsItem):
    """The copper the board came with: one bar per uncut run of holes.

    Stripboard's whole character is that these are already connected, and a board drawn
    as a grid of separate pads says the opposite of that. Two pins in one row are joined
    whether or not anybody drew a conductor, so the strip has to be visible or the screen
    is lying about the circuit.

    Drawn from ``stripboard.segments`` rather than from rows, so a cut visibly ends a bar
    -- which is what makes a cut inspectable at a glance, on screen and on the board.
    """

    def __init__(self, doc: PerfDocument, side: BoardSide) -> None:
        super().__init__()
        self.board = doc.board
        self.side = side
        self.segments = segments(doc)
        #: The strips are on the solder side. From the component side they are behind the
        #: board, so they are drawn faintly for reference rather than as copper in front
        #: of you -- the same rule the far-side hatching follows.
        self.far_side = side == "top"
        self.setZValue(-95)  # Under the pads: the strip is etched, then drilled through.

    @property
    def has_strips(self) -> bool:
        return bool(self.segments)

    def boundingRect(self) -> QRectF:
        return _outline_rect(self.board)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        if not self.segments:
            return
        scheme = scheme_for(self.board.material)
        colour = QColor(scheme.pad)
        if self.far_side:
            colour.setAlpha(70)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(colour))

        extent_x, extent_y = pad_extent_mm(self.board)
        area = option.exposedRect
        for segment in self.segments:
            first = hole_to_screen(segment.holes[0], self.board, self.side)
            last = hole_to_screen(segment.holes[-1], self.board, self.side)
            left, right = sorted((first.x(), last.x()))
            top, bottom = sorted((first.y(), last.y()))
            rect = QRectF(
                left - extent_x / 2,
                top - extent_y / 2,
                (right - left) + extent_x,
                (bottom - top) + extent_y,
            )
            if not area.intersects(rect):
                continue
            radius = min(extent_x, extent_y) / 2
            painter.drawRoundedRect(rect, radius, radius)


class TrackCutItem(QGraphicsItem):
    """Where the copper has been drilled away.

    A cross rather than a gap, and drawn in the error colour, because a cut is the one
    feature of a stripboard design that is invisible on the finished board from more than
    arm's length -- and getting one in the wrong hole is the classic way to spend an
    evening. The pad is already missing underneath it; this says why.
    """

    def __init__(self, cut: TrackCut, board: Board, side: BoardSide) -> None:
        super().__init__()
        self.cut = cut
        self.board = board
        self.side = side
        self.setZValue(-84)  # Over the pads and the strips, under the parts.

    def boundingRect(self) -> QRectF:
        centre = hole_to_screen(self.cut.at, self.board, self.side)
        r = max(pad_extent_mm(self.board)) / 2 + 0.4
        return QRectF(centre.x() - r, centre.y() - r, 2 * r, 2 * r)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        centre = hole_to_screen(self.cut.at, self.board, self.side)
        r = max(pad_extent_mm(self.board)) / 2
        painter.setBrush(QBrush(SUBSTRATE.get(self.board.material, SUBSTRATE["FR4"]).darker(140)))
        painter.setPen(QPen(CUT_MARK, 0.14))
        painter.drawEllipse(centre, r, r)
        arm = r * 0.55
        painter.drawLine(
            QPointF(centre.x() - arm, centre.y() - arm), QPointF(centre.x() + arm, centre.y() + arm)
        )
        painter.drawLine(
            QPointF(centre.x() - arm, centre.y() + arm), QPointF(centre.x() + arm, centre.y() - arm)
        )


class EdgeConnectorItem(QGraphicsItem):
    """The finger pads of one edge connector.

    Drawn from ``geometry.edge_finger_rect`` rather than from a local idea of where the
    edge is, so that what is drawn and what DRC measures its gaps against are the same
    rectangle.
    """

    def __init__(self, connector: EdgeConnector, board: Board, side: BoardSide) -> None:
        super().__init__()
        self.connector = connector
        self.board = board
        self.side = side
        self.near_side = connector.face == "both" or connector.face == side
        self.setZValue(-88)

    def boundingRect(self) -> QRectF:
        return _outline_rect(self.board)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        for hole in edge_connector_holes(self.connector, self.board):
            rect = edge_finger_rect(self.connector, hole, self.board)
            # The rect is in board mm; only x needs the solder-side reflection, and it
            # reflects about the hole span exactly as every other x in this file does.
            left = rect.x
            if self.side == "bottom":
                span_w, _ = hole_span_mm(self.board)
                left = span_w - (rect.x + rect.width)
            radius = min(rect.width, rect.height) * 0.25
            scheme = scheme_for(self.board.material)
            painter.setPen(QPen(QColor(scheme.pad_ring), 0.08))
            painter.setBrush(QBrush(QColor(scheme.pad) if self.near_side else FINGER_FAR))
            painter.drawRoundedRect(QRectF(left, rect.y, rect.width, rect.height), radius, radius)

            # NO HOLE. A finger is a solid contact, soldered to from the surface -- there
            # is nothing to put a lead through, and that is the whole difference between a
            # finger and a pad. This used to punch the drill back through on the reasoning
            # that the finger is copper laid OVER the pad; it is not, it is the pad, and
            # drawing a bore in it made the strip read as an ordinary row of long pads.


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


def describe_span(a: HoleCoord, b: HoleCoord, board: Board) -> str:
    """Two holes, and the three different distances between them.

    They are genuinely three answers and not one rounded three ways:

      * **holes across** is what you count on the board and what a footprint is written
        in -- "R3: C7 to C11, 4 holes" is the language the build guide already speaks.
      * **mm** is the straight, centre-to-centre distance, which is what a lead-bending
        jig and a pair of pliers are set to, and the only one of the three that answers
        "will this part physically reach".
      * **steps** is the orthogonal count, which is how long a solder trace between them
        would be -- solder crosses the 0.6 mm gap to an orthogonal neighbour and not the
        1.7 mm diagonal one, so a diagonal is 2 steps of copper and not 1.4.

    Same hole twice is not an error and not a measurement; it says so instead.
    """
    if a == b:
        return f"{format_hole(a)} — the same hole."
    d_col, d_row = abs(b.col - a.col), abs(b.row - a.row)
    mm = path_length_mm((a, b), board)
    steps = manhattan(a, b)
    span = (
        f"{d_col + 1} × {d_row + 1} holes"
        if d_col and d_row
        else f"{max(d_col, d_row) + 1} holes"
    )
    return (
        f"{format_hole(a)} → {format_hole(b)}   {span}   {mm:.2f} mm apart   "
        f"{steps} step(s) by trace"
    )


def next_net_name(document: PerfDocument) -> str:
    """The next free automatic net name, e.g. "N3".

    Counted from the document for the same reason ``next_reference`` is: a hidden counter
    would disagree with it after an undo, and the bus would refuse the name for a reason
    nobody could see. Short and neutral on purpose -- it is a placeholder for whatever the
    net turns out to be called, and renaming it is one dialog away.
    """
    used = {net.name for net in document.nets}
    index = 1
    while f"N{index}" in used:
        index += 1
    return f"N{index}"


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
        return _outline_rect(self.board)

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
        return _outline_rect(self.board).adjusted(-1, -1, 1, 1)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        r = self.board.pad_diameter / 2 + 0.35
        pen = QPen(RISK_RING, 0.3)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for hole in self.holes:
            p = hole_to_screen(hole, self.board, self.side)
            painter.drawEllipse(p, r, r)


class PickedPinsItem(QGraphicsItem):
    """The pins collected so far while naming a net by clicking them.

    Deliberately the ratsnest's highlight colour: the yellow already means "this is the
    net you are looking at" everywhere else in the view, and a pin picked for a net is
    the same statement made one click at a time.
    """

    def __init__(self, board: Board, side: BoardSide) -> None:
        super().__init__()
        self.holes: list[HoleCoord] = []
        self.board = board
        self.side = side
        self.setZValue(61)

    def set_holes(self, holes: Sequence[HoleCoord]) -> None:
        self.holes = list(holes)
        self.update()

    def boundingRect(self) -> QRectF:
        return _outline_rect(self.board).adjusted(-1, -1, 1, 1)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        r = self.board.pad_diameter / 2 + 0.35
        painter.setPen(QPen(RATSNEST_HIGHLIGHT, 0.35))
        painter.setBrush(QBrush(QColor(255, 209, 102, 70)))
        for hole in self.holes:
            painter.drawEllipse(hole_to_screen(hole, self.board, self.side), r, r)


# ------------------------------------------------------------------ conductor item


class DrawPreviewItem(QGraphicsItem):
    """The conductor being drawn, before it exists.

    Shows the chain committed so far solid and the step under the cursor dashed, so the
    difference between "this is what I have" and "this is where the next click goes" is
    visible without moving your eyes off the board. A step the command would refuse --
    a diagonal on a solder trace -- is drawn in the error colour rather than simply
    ignored, because silently dropping a click reads as the tool having stopped working.
    """

    def __init__(self, kind: ConductorKind, board: Board, side: BoardSide) -> None:
        super().__init__()
        self.kind = kind
        self.board = board
        self.side = side
        self.path: list[HoleCoord] = []
        # Not "cursor": QGraphicsItem already has a cursor() and shadowing it would make
        # the item's own cursor handling silently stop working.
        self.next_hole: HoleCoord | None = None
        self.next_ok = True
        self.setZValue(120)

    def boundingRect(self) -> QRectF:
        pitch = self.board.pitch
        return _outline_rect(self.board).adjusted(-pitch, -pitch, pitch, pitch)

    def set_path(self, path: list[HoleCoord], next_hole: HoleCoord | None, ok: bool) -> None:
        self.path = path
        self.next_hole = next_hole
        self.next_ok = ok
        self.update()

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        colour, width, _dashed = CONDUCTOR_STYLE.get(self.kind, (QColor("#888"), 0.6, False))
        points = [hole_to_screen(h, self.board, self.side) for h in self.path]

        if len(points) >= 2:
            pen = QPen(colour, width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath(points[0])
            for p in points[1:]:
                path.lineTo(p)
            painter.drawPath(path)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(colour.lighter(130)))
        for p in points:
            painter.drawEllipse(p, width * 0.7, width * 0.7)

        if self.next_hole is not None:
            end = hole_to_screen(self.next_hole, self.board, self.side)
            tint = colour if self.next_ok else ERROR_OUTLINE
            if points:
                pen = QPen(tint, width)
                pen.setDashPattern([1.6, 1.4])
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(points[-1], end)
            painter.setPen(QPen(tint, 0.22))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(end, self.board.pad_diameter * 0.62, self.board.pad_diameter * 0.62)


class ConductorItem(QGraphicsItem):
    """Each conductor kind has to be tellable apart at a glance.

    ``model.contacts_every_path_hole`` (the engine's own predicate, not a re-derived
    copy of it) decides whether every hole gets a solder bead or only the two
    endpoints get a fillet -- this distinction is the heart of the whole data model and
    it must be read from the engine, never re-implemented here.
    """

    def __init__(
        self,
        conductor: Conductor,
        board: Board,
        side: BoardSide,
        net_class: NetClass | None = None,
        signal_index: int = 0,
        hatch_far_side: bool = True,
    ) -> None:
        super().__init__()
        self.conductor = conductor
        self.board = board
        self.side = side
        self.net_class = net_class
        self.signal_index = signal_index
        self.hatch_far_side = hatch_far_side
        # Selectable, so a single bad route can be deleted instead of the whole autoroute
        # being undone or the entire board re-routed -- which were the only two options.
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setZValue(-50 if conductor.side == "bottom" else 40)
        first, last = conductor.path[0], conductor.path[-1]
        self.setToolTip(
            f"{conductor.kind.replace('-', ' ')}  {format_hole(first)} → {format_hole(last)}"
        )

    def _points(self) -> list[QPointF]:
        return [hole_to_screen(h, self.board, self.side) for h in self.conductor.path]

    def is_far_side(self) -> bool:
        """True when this conductor is on the face away from the one being viewed.

        Perfboard is opaque, so strictly you cannot see it at all -- but the whole point of
        the editor is to show it, and the honest way to do that is to mark it as being on
        the other side rather than to draw it as if it were in front of you. Public because
        the hatching is the visible consequence of this one predicate, and a test that has
        to inspect pixels to check which side a conductor was drawn on is testing the
        wrong thing.
        """
        return self.conductor.side != self.side

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

    @property
    def conductor_id(self) -> str:
        return self.conductor.id

    def shape(self) -> QPainterPath:
        """A clickable band along the path, so a conductor can be picked.

        A stroked path rather than the bounding rect: two wires crossing at an angle share
        a bounding rect the size of the board between them, and picking one would select
        whichever happened to be on top.
        """
        pts = self._points()
        line = QPainterPath(pts[0])
        for p in pts[1:]:
            line.lineTo(p)
        stroker = QPainterPathStroker()
        # A little wider than the conductor: at 2.54 mm pitch, half a pad's worth of slack
        # is the difference between clicking a wire and clicking near it.
        stroker.setWidth(max(CONDUCTOR_STYLE.get(self.conductor.kind, (None, 0.6, None))[1], 0.6) + 0.8)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        return stroker.createStroke(line)

    def _colour(self) -> QColor:
        """This conductor's colour: its own if it has one, else its net's, else its kind's."""
        explicit = getattr(self.conductor, "color", None)
        if explicit:
            return QColor(explicit)
        if self.conductor.kind in ("insulated-wire", "top-jumper"):
            return insulation_color(self.net_class, self.signal_index)
        return CONDUCTOR_STYLE.get(self.conductor.kind, (QColor("#888"), 0.6, False))[0]

    def _paint_through_the_board(
        self, painter: QPainter, path: QPainterPath, width: float, colour: QColor
    ) -> None:
        """A conductor on the far face: hatched, the way a part on the far face already is.

        The board is opaque. Drawing a solder-side trace solid while looking at the
        component side says "this is in front of you", which is the same misreading
        ``_paint_body_shadow`` exists to prevent for bodies -- and the one that gets a board
        soldered on the wrong face. Hatching is this application's established word for "on
        the other side", so a conductor and a part now say it the same way.

        Stroked into a fillable shape rather than dashed: a dash already means a top jumper
        (``CONDUCTOR_STYLE``), and giving one mark two meanings costs more than it saves.
        The outline keeps the run traceable end to end, which hatching alone does not at low
        zoom.
        """
        stroker = QPainterPathStroker()
        stroker.setWidth(width)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        body = stroker.createStroke(path)

        faded = QColor(colour)
        faded.setAlphaF(0.75)
        brush = QBrush(faded, Qt.BrushStyle.FDiagPattern)
        # Hatch patterns are defined in DEVICE pixels, so without this the hatch would hold
        # its screen size while the board zooms -- solid when zoomed in, gone when zoomed
        # out. The same inversion _paint_body_shadow needs, for the same reason.
        brush.setTransform(painter.transform().inverted()[0])
        painter.setBrush(brush)
        painter.setPen(QPen(SELECTED if self.isSelected() else faded, 0.1))
        painter.drawPath(body)

        # The joints stay solid. Where a conductor is soldered down is the one fact that
        # does not change with the face you are looking from -- the hole goes through the
        # board -- and it is what someone counts pads against.
        painter.setPen(QPen(colour.darker(140), 0.08))
        painter.setBrush(QBrush(colour))
        radius = min(width * 0.62, self.board.pad_diameter * 0.32)
        for point in self.contact_points():
            painter.drawEllipse(point, radius, radius)

    def paint(self, painter: QPainter, option: Any, widget: Any = None) -> None:
        kind = self.conductor.kind
        _default, width, dashed = CONDUCTOR_STYLE.get(kind, (QColor("#888"), 0.6, False))
        colour = self._colour()

        pts = self._points()
        path = QPainterPath(pts[0])
        for p in pts[1:]:
            path.lineTo(p)

        if self.hatch_far_side and self.is_far_side():
            self._paint_through_the_board(painter, path, width, colour)
            return

        # Insulated wire and top jumpers get a dark casing line under the colour, so a
        # coloured sleeve reads as a sleeve rather than as a painted line, and stays
        # legible where it crosses a pad of nearly its own brightness.
        if kind in ("insulated-wire", "top-jumper"):
            casing = QPen(QColor(0, 0, 0, 150), width + 0.22)
            casing.setCapStyle(Qt.PenCapStyle.RoundCap)
            casing.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(casing)
            painter.drawPath(path)

        if self.isSelected():
            halo = QPen(SELECTED, width + 0.7)
            halo.setCapStyle(Qt.PenCapStyle.RoundCap)
            halo.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(halo)
            painter.drawPath(path)

        pen = QPen(colour, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if dashed:
            pen.setDashPattern([2.2, 1.6])
        painter.setPen(pen)
        painter.drawPath(path)

        # The spine, drawn as a thin copper core running through the solder -- which is
        # exactly what it is, and the only thing distinguishing the two trace kinds on
        # screen. Their electrical difference is about an order of magnitude, so it has
        # to be visible.
        if kind == "solder-trace-wired":
            core = QPen(SPINE_CORE, 0.3)
            core.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(core)
            painter.drawPath(path)

        contacts = self.contact_points()
        every_hole = contacts_every_path_hole(self.conductor)
        if every_hole:
            # A bead at every pad: this trace is soldered down all along its length.
            # Sized to sit INSIDE the pad rather than over it -- solder fills the pad, it
            # does not replace it, and a bead wider than the pad hides the thing being
            # soldered to.
            radius = min(width * 0.85, self.board.pad_diameter * 0.42)
            fill = colour.lighter(112)
        else:
            # A fillet at each end only: a wire is soldered at its two ends and merely
            # passes over everything between them.
            radius = min(width * 0.95, self.board.pad_diameter * 0.40)
            fill = colour.lighter(120)

        # A thin darker rim around each joint. Without it a run of beads at 2.54 mm pitch
        # merges into one lumpy caterpillar brighter than the trace under it, and the eye
        # reads a necklace rather than a length of copper soldered down at every pad. The
        # rim keeps each joint individually countable -- which is what someone tracing the
        # run against the board is actually doing -- while letting the trace itself carry
        # the line.
        painter.setPen(QPen(colour.darker(145), 0.08))
        painter.setBrush(QBrush(fill))
        for p in contacts:
            painter.drawEllipse(p, radius, radius)

        # A highlight off the top-left of each joint: solder is domed and wet-looking, and
        # a flat disc is the one thing it never looks like. Skipped when zoomed out far
        # enough that it would be a single stray pixel of noise per pad.
        if painter.transform().m11() >= 6.0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(fill.lighter(135)))
            spot = radius * 0.34
            for p in contacts:
                painter.drawEllipse(
                    QPointF(p.x() - radius * 0.28, p.y() - radius * 0.28), spot, spot
                )


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


def _paint_body_shadow(
    painter: QPainter,
    footprint: Footprint,
    placement: BodyPlacement,
    selected: bool,
) -> None:
    """Where a part sits, seen through the board from the solder side.

    Hatched, not filled, and deliberately without a single one of the component-side
    marks. The distinction it has to carry is "something is on the other side here"
    rather than "here is a part", because on the solder side the second reading is how
    somebody solders a board backwards.

    Diagonal hatching rather than a tint because a tint competes with the pads for
    attention and this must never do that: on this side the pads ARE the subject.
    ``keyed=None`` is passed on purpose, so an LED's flat and a diode's band do not
    appear -- they are moulded into the top of the part and cannot be seen from below.
    """
    path = _body_path(footprint, placement, None)
    painter.setPen(QPen(QColor(SELECTED if selected else BODY_SHADOW_EDGE), 0.12))
    brush = QBrush(QColor(SELECTED if selected else BODY_SHADOW), Qt.BrushStyle.FDiagPattern)
    # Hatch patterns are defined in device pixels, so without this they would stay the
    # same size on screen while the board zooms -- turning into a solid block when zoomed
    # in and vanishing when zoomed out.
    brush.setTransform(painter.transform().inverted()[0])
    painter.setBrush(brush)
    painter.drawPath(path)


def _paint_body_sheen(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    placement: BodyPlacement,
    style: BodyStyle,
) -> None:
    """A highlight along the top of the body, so it reads as a solid object.

    A flat fill is what made every part look like a sticker printed on the board. Real
    parts are round or moulded and catch the light along one edge, and how MUCH they catch
    is the difference between a plastic case and a metal can -- which is a fact
    ``bodies.surface_for`` owns, so the 3D view shades the same part the same way.

    Drawn across the body's SHORT axis, because that is the direction a cylinder curves
    in: a resistor lying on the board is lit along its length, not around its ends.
    """
    sheen = surface_for(style).sheen
    if sheen <= 0.0:
        return
    highlight = QColor(style.fill).lighter(100 + int(sheen * 120))
    highlight.setAlphaF(min(1.0, 0.35 + sheen * 0.5))
    band = QRectF(rect)
    if placement.axis == "x":
        band.setHeight(rect.height() * 0.34)
        band.moveTop(rect.top() + rect.height() * 0.13)
    else:
        band.setWidth(rect.width() * 0.34)
        band.moveLeft(rect.left() + rect.width() * 0.13)
    painter.save()
    painter.setClipPath(path)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(highlight))
    radius = min(band.width(), band.height()) * 0.5
    painter.drawRoundedRect(band, radius, radius)
    painter.restore()


def _paint_resistor_bands(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    placement: BodyPlacement,
    bands: tuple[str, ...],
) -> None:
    """The printed colour code, across the body and clipped to it.

    Laid out from one end rather than centred, and with the tolerance band pushed to the
    far end, because that asymmetry is what tells a reader which way round to read the
    other three -- exactly as it does on the real part.
    """
    along = rect.width() if placement.axis == "x" else rect.height()
    if along <= 0:
        return
    width = along * 0.11
    painter.save()
    painter.setClipPath(path)
    painter.setPen(Qt.PenStyle.NoPen)
    for index, colour in enumerate(bands):
        # The first three sit in the near half; the tolerance band goes at the far end.
        fraction = 0.16 + index * 0.15 if index < len(bands) - 1 else 0.80
        band = QRectF(rect)
        if placement.axis == "x":
            band.setWidth(width)
            band.moveLeft(rect.left() + along * fraction)
        else:
            band.setHeight(width)
            band.moveTop(rect.top() + along * fraction)
        painter.setBrush(QBrush(QColor(colour)))
        painter.drawRect(band)
    painter.restore()


def _paint_body(
    painter: QPainter,
    footprint: Footprint,
    placement: BodyPlacement,
    style: BodyStyle,
    keyed: tuple[float, float] | None,
    selected: bool,
    pitch: float,
    bands: tuple[str, ...] | None = None,
) -> None:
    rect = _body_rect(placement)
    path = _body_path(footprint, placement, keyed)

    painter.setBrush(QBrush(QColor(style.fill)))
    painter.setPen(QPen(QColor(SELECTED if selected else style.edge), 0.2))
    painter.drawPath(path)

    accent = QColor(style.accent)
    archetype = footprint.body.archetype

    # Before the marks, so a cathode band or a pin-1 dot stays flat and readable on top of
    # it rather than being washed out by a highlight painted over it.
    _paint_body_sheen(painter, path, rect, placement, style)

    if bands:
        _paint_resistor_bands(painter, path, rect, placement, bands)

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

        # THE SOLDER SIDE SHOWS NO BODY, only where one IS. Turn a real board over and the
        # parts are on the far face: what you see is the substrate, the pads and the cut
        # lead ends. Drawing the silhouettes as if seen from above -- which this used to do
        # -- shows a view that does not exist, on the very side where a misreading gets
        # soldered in.
        #
        # But you can still see the part through the board, and you need to: "is there room
        # for this wire" and "which of these pads belongs to the chip" are solder-side
        # questions. So the footprint is hatched rather than drawn: the area is marked as
        # occupied, with none of the component-side detail -- no cathode band, no pin-1
        # notch, no tab -- that would make it read as a part you are looking at.
        if self.side == "bottom":
            painter.save()
            self._apply_local_transform(painter)
            _paint_body_shadow(painter, self.fp, placement, self.isSelected())
            painter.restore()

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
                painter,
                self.fp,
                placement,
                style,
                keyed,
                self.isSelected(),
                self.board.pitch,
                # From the document's own value, so the bands cannot disagree with the
                # netlist. None for anything that is not a resistor with a readable value.
                bands=resistor_bands(self.fp, self.comp.value),
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
    #: Emitted with the armed conductor kind, or an empty string when drawing ends.
    drawArmed = Signal(str)
    #: Emitted with the DispatchResult of a hand-drawn conductor.
    conductorDrawn = Signal(object)
    #: Emitted with the id of the net being filled by clicking pins, or "" when that ends.
    netPinsArmed = Signal(str)
    #: Emitted with the "U1.8" labels picked so far, after every pick.
    netPinsChanged = Signal(list)
    #: Emitted with why a click did not count: an empty hole, or a pin already spoken for.
    #: A silently ignored click reads as the tool having stopped working.
    netPinRejected = Signal(str)
    #: Emitted with the DispatchResult of the committed net.connect.
    netPinsCommitted = Signal(object)
    #: Emitted when the two-click connect tool is armed or disarmed.
    connectArmed = Signal(bool)
    #: Emitted with the pin waiting for its partner, as a one-item list, or empty.
    connectProgress = Signal(list)
    #: Emitted with the DispatchResult of a pair that was joined.
    pinsConnected = Signal(object)
    #: (col, row) under the cursor. The whole tool speaks hole addresses, so the status
    #: bar has to be able to say which one the pointer is on -- otherwise you count.
    hoveredHole = Signal(int, int)
    #: Emitted when the track-cutting tool is armed or disarmed.
    cutArmed = Signal(bool)
    #: Emitted with the DispatchResult of a cut made or taken back.
    cutMade = Signal(object)
    #: Emitted when the measuring tool is armed or disarmed.
    measureArmed = Signal(bool)
    #: The measurement as a sentence, live while the pointer moves, or "" when there is
    #: nothing to say. Text rather than numbers because every consumer of it is prose.
    measured = Signal(str)

    def __init__(
        self,
        document: PerfDocument,
        lookup: FootprintLookup,
        side: BoardSide = "top",
        bus: CommandBus | None = None,
        show_ratsnest: bool = True,
        show_rulers: bool = True,
        hatch_far_side: bool = True,
    ) -> None:
        super().__init__()
        self.lookup = lookup
        self.side = side
        self.bus = bus
        self.hatch_far_side = hatch_far_side
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
        self._draw_kind: ConductorKind | None = None
        self._draw_path: list[HoleCoord] = []
        self._draw_preview: DrawPreviewItem | None = None
        self._net_pin_target: str | None = None
        self._net_pin_picks: list[tuple[str, str]] = []
        self._net_pin_holes: list[HoleCoord] = []
        self._net_pin_item: PickedPinsItem | None = None
        self._connect_armed = False
        self._connect_first: tuple[str, str] | None = None
        self._connect_hole: HoleCoord | None = None
        self._connect_item: PickedPinsItem | None = None
        self._measure_armed = False
        self._measure_from: HoleCoord | None = None
        self._measure_item: PickedPinsItem | None = None
        self._cut_armed = False
        #: Set when a mode consumed a right press, read and cleared by the view before it
        #: decides whether to offer a context menu. See take_consumed_right_click.
        self._right_click_taken = False
        self._ghost: PlacementGhostItem | None = None
        #: Whether the last placement landed somewhere already occupied. Read by the host to
        #: say so, since the bus allows it and only DRC objects.
        self.last_placement_overlapped = False
        #: What every part placed from here is given as its value ("10k", "100nF"), set by
        #: the host from the parts panel. Held across placements rather than cleared with
        #: the armed footprint: five resistors of the same value is the case, and retyping
        #: it between each of them is the friction this exists to remove.
        self.placement_value = ""
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

    def set_hatch_far_side(self, hatch: bool) -> None:
        """Whether copper on the face away from the viewer is hatched or drawn solid.

        A full rebuild rather than an item-by-item update: the flag is read when each
        ConductorItem is built, and one source of truth for "what is in this scene" is worth
        more than saving a rebuild nobody is waiting on.
        """
        if hatch != self.hatch_far_side:
            self.hatch_far_side = hatch
            self._build()

    def set_show_rulers(self, show: bool) -> None:
        if show != self.show_rulers:
            self.show_rulers = show
            self._build()

    def legend_is_readable(self) -> bool:
        """Whether the board's own printed addresses can be read from the side in view.

        Public because the host greys out "Show Hole Addresses" when this is true: the
        ruler is suppressed then (see ``_build``), and a menu item that silently does
        nothing is worse than one that is visibly unavailable.
        """
        labels = self.document.board.labels
        return labels is not None and labels.face in ("both", self.side)

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
        self._draw_preview = None
        # The pin markers are RE-created rather than merely forgotten, because a rebuild
        # happens on every command and the mode survives one: a half-collected net whose
        # picks vanished off the board reads as the clicks having been lost.
        self._net_pin_item = None
        self._connect_item = None
        board = self.document.board
        outline = _outline_rect(board)
        # Room outside the substrate is reserved for the ruler, so it is only needed when
        # one is actually going to be drawn -- see the note beside HoleRulerItem below.
        margin = RULER_MARGIN_MM if (self.show_rulers and not self.legend_is_readable()) else 4.0
        self.setSceneRect(
            outline.left() - margin,
            outline.top() - margin,
            outline.width() + margin + 4,
            outline.height() + margin + 4,
        )
        self.setBackgroundBrush(QBrush(BACKGROUND))

        scheme = scheme_for(board.material)
        substrate = self.addRect(
            outline,
            QPen(QColor(scheme.edge), 0.4),
            QBrush(QColor(scheme.fill)),
        )
        substrate.setZValue(-100)

        # Ink first, then copper on top of it: the legend is printed on the substrate and
        # a pad sits over the print, which is also the order that keeps a label legible
        # where it passes close to the outer row of pads.
        if board.labels is not None:
            self.addItem(BoardLegendItem(self.document, board.labels, self.side))

        # A single-sided board has copper on the solder side ONLY. From the component
        # side you are looking at bare phenolic with holes drilled through it -- the holes
        # are still all there, and drawing neither them nor the pads would leave a blank
        # slab that says nothing about where anything goes.
        # `undrilled_holes` on top of the face-sensitive set, and unconditionally: a
        # finger has no bore, so its position gets neither a pad NOR a hole, on either
        # face. Without it the component side of a board whose fingers are on the solder
        # side shows a drilled hole where the board is solid.
        # The board's own copper goes UNDER the pads, because that is the order the
        # physical board is made in: the strip is etched and the holes are drilled
        # through it. Cut holes join the no-pad set for the same reason a mounting bore
        # does -- the copper there was drilled away, and drawing a pad on it would offer
        # something to solder to that is not there.
        strips = StripGridItem(self.document, self.side)
        if strips.has_strips:
            self.addItem(strips)
        self.pad_grid = PadGridItem(
            board,
            self.side,
            holes_without_grid_pad(self.document, self.side)
            | undrilled_holes(self.document)
            | cut_holes(self.document),
            copper=not (board.single_sided and self.side == "top"),
        )
        self.addItem(self.pad_grid)
        for cut in self.document.cuts:
            if is_stripboard(board):
                self.addItem(TrackCutItem(cut, board, self.side))

        for connector in self.document.edge_connectors:
            self.addItem(EdgeConnectorItem(connector, board, self.side))
        for mount in self.document.mounting_holes:
            self.addItem(MountingHoleItem(mount, board, self.side))

        # THE RULER STANDS DOWN WHEN THE BOARD SPEAKS FOR ITSELF. The ruler exists to name
        # a hole on a board that carries no addresses of its own; drawing it alongside a
        # printed legend puts the same twenty-four letters on screen twice, a few
        # millimetres apart and in two different styles, which reads as a rendering fault
        # rather than as two features. Only when the legend is on the far face -- where it
        # is a dim ghost rather than something you can read -- is the ruler wanted again.
        if self.show_rulers and not self.legend_is_readable():
            self.addItem(HoleRulerItem(board, self.side))

        net_class_by_id = {net.id: net.net_class for net in self.document.nets}
        signal_index = {
            net.id: index
            for index, net in enumerate(n for n in self.document.nets if n.net_class == "signal")
        }
        for conductor in self.document.conductors:
            self.addItem(
                ConductorItem(
                    conductor,
                    board,
                    self.side,
                    net_class=net_class_by_id.get(conductor.net_id or ""),
                    signal_index=signal_index.get(conductor.net_id or "", 0),
                    hatch_far_side=self.hatch_far_side,
                )
            )

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
        if self._net_pin_target is not None:
            self._net_pin_item = PickedPinsItem(board, self.side)
            self._net_pin_item.set_holes(self._net_pin_holes)
            self.addItem(self._net_pin_item)
        if self._connect_first is not None and self._connect_hole is not None:
            self._connect_item = PickedPinsItem(board, self.side)
            self._connect_item.set_holes([self._connect_hole])
            self.addItem(self._connect_item)

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
    #: Emitted with the id of a part that was double-clicked. What the host does about it
    #: (open its properties) is the host's business; the scene only reports the gesture.
    componentActivated = Signal(str)

    # -- drawing conductors by hand ------------------------------------------
    #
    # The engine has had conductor.add since the first commit and nothing in the window
    # could reach it, so on a perfboard tool there was no way to run a wire or lay a
    # solder trace yourself -- only to accept whatever the router produced or throw the
    # whole thing away.

    #: Kinds that are exactly two holes: click a start, click an end, done.
    _TWO_POINT_KINDS: frozenset[ConductorKind] = frozenset(
        {"bare-wire", "insulated-wire", "top-jumper"}
    )

    def arm_drawing(self, kind: ConductorKind | None) -> None:
        """Arm (or, with None, disarm) drawing a conductor of this kind."""
        self._clear_draw()
        self._draw_kind = kind
        if kind is not None:
            self.arm_placement(None)  # The board modes are mutually exclusive.
            self._disarm_net_pins()
            self._disarm_connect()
            self._disarm_measure()
            self._disarm_cutting()
            self._draw_preview = DrawPreviewItem(kind, self.document.board, self.side)
            self.addItem(self._draw_preview)
        self.drawArmed.emit(kind or "")

    @property
    def armed_draw_kind(self) -> ConductorKind | None:
        return self._draw_kind

    def _clear_draw(self) -> None:
        if self._draw_preview is not None:
            self.removeItem(self._draw_preview)
            self._draw_preview = None
        self._draw_path = []
        self._draw_kind = None

    def _step_is_legal(self, at: HoleCoord) -> bool:
        """Whether the next click may go here, by the same rule the command applies.

        A solder trace joins orthogonal neighbours only: at 2.54 mm pitch the orthogonal
        pad gap is about 0.6 mm and the diagonal one about 1.7 mm, and solder does not
        reliably span the second. Checked here so the preview can say so before the click
        rather than the command refusing after it.
        """
        if not is_inside_board(at, self.document.board):
            return False
        if not self._draw_path:
            return True
        if at in self._draw_path:
            return False
        last = self._draw_path[-1]
        if self._draw_kind in ("solder-trace", "solder-trace-wired"):
            return abs(at.col - last.col) + abs(at.row - last.row) == 1
        return at != last

    def draw_click(self, at: HoleCoord) -> DispatchResult | None:
        """Extend the conductor being drawn, committing it when it is complete."""
        if self._draw_kind is None or not self._step_is_legal(at):
            return None
        self._draw_path.append(at)
        if self._draw_kind in self._TWO_POINT_KINDS and len(self._draw_path) == 2:
            return self.commit_drawing()
        self._refresh_draw_preview(at)
        return None

    def commit_drawing(self) -> DispatchResult | None:
        """Dispatch the drawn conductor. Fewer than two holes is a cancel, not an error."""
        if self.bus is None or self._draw_kind is None or len(self._draw_path) < 2:
            self._clear_draw()
            self.drawArmed.emit("")
            return None

        kind = self._draw_kind
        path = tuple(self._draw_path)
        spec: NewConductor
        if kind in ("solder-trace", "solder-trace-wired"):
            spec = NewSolderTraceConductor(
                path=path, kind=cast(Any, kind), net_id=self._net_for(path)
            )
        else:
            spec = NewWireConductor(path=path, kind=cast(Any, kind), net_id=self._net_for(path))

        result = self.bus.dispatch("conductor.add", AddConductorPayload(conductor=spec))
        self._clear_draw()
        self.drawArmed.emit("")
        self.conductorDrawn.emit(result)
        return result

    def _net_for(self, path: tuple[HoleCoord, ...]) -> str | None:
        """The schematic net this conductor evidently belongs to, or None.

        Assigned only when both ends land on pins that share exactly one net -- an
        unambiguous case. Anything else stays unassigned, which is not a failure: copper
        with no net claim is what rip-up-and-reroute and the stale-conductor cleanup both
        promise never to touch, so a hand-drawn connection the tool cannot interpret is
        also one it will never quietly remove.
        """
        pins_at: dict[tuple[int, int], set[tuple[str, str]]] = {}
        for comp in self.document.components:
            for pin, hole in _pin_holes_of(comp, self.lookup):
                pins_at.setdefault((hole.col, hole.row), set()).add((comp.ref, pin.number))

        ends = [pins_at.get((path[0].col, path[0].row), set()),
                pins_at.get((path[-1].col, path[-1].row), set())]
        if not all(ends):
            return None
        candidates = [
            net.id
            for net in self.document.nets
            if all(
                any((node.component_ref, node.pin) in end for node in net.nodes) for end in ends
            )
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _refresh_draw_preview(self, cursor: HoleCoord | None) -> None:
        if self._draw_preview is None:
            return
        ok = cursor is None or self._step_is_legal(cursor)
        self._draw_preview.set_path(list(self._draw_path), cursor, ok)

    # -- naming a net by clicking its pins -----------------------------------
    #
    # A net could only ever arrive from a KiCad netlist, which made a schematic capture
    # package a prerequisite for the ratsnest -- and so for autoroute, LVS and the guide's
    # continuity tests. Nobody opens KiCad to wire four parts on a scrap of perfboard.
    #
    # A MODE over the board rather than a dialog listing refs, for the same reason
    # placement is one: the pin IS the decision, and on a perfboard a pin is a hole you
    # can point at. A list of "U1.8, U1.7, C2.2" asks the user to do the translation the
    # board is already showing them.

    def arm_net_pins(self, net_id: str | None) -> None:
        """Arm (or, with None, disarm) collecting pins for a net.

        An unknown id disarms rather than collecting clicks for a net that is not there.
        """
        if net_id is not None and not any(n.id == net_id for n in self.document.nets):
            net_id = None
        if net_id is not None:
            # The board modes are mutually exclusive: a click means one thing.
            self.arm_placement(None)
            self.arm_drawing(None)
            self._disarm_connect()
            self._disarm_measure()
            self._disarm_cutting()
        self._clear_net_pins()
        self._net_pin_target = net_id
        if net_id is not None:
            self._net_pin_item = PickedPinsItem(self.document.board, self.side)
            self.addItem(self._net_pin_item)
        self.netPinsArmed.emit(net_id or "")
        self.netPinsChanged.emit([])

    @property
    def armed_net_id(self) -> str | None:
        return self._net_pin_target

    def picked_pins(self) -> tuple[tuple[str, str], ...]:
        """(ref, pin) collected so far. Also what the seam tests assert on."""
        return tuple(self._net_pin_picks)

    def _disarm_net_pins(self) -> None:
        """End the mode without committing, for the other two modes to call."""
        if self._net_pin_target is None:
            return
        self._clear_net_pins()
        self._net_pin_target = None
        self.netPinsArmed.emit("")

    def _clear_net_pins(self) -> None:
        if self._net_pin_item is not None:
            self.removeItem(self._net_pin_item)
            self._net_pin_item = None
        self._net_pin_picks = []
        self._net_pin_holes = []

    def _pin_at(self, hole: HoleCoord) -> tuple[str, str] | None:
        """The component pin occupying a hole, as (ref, pin number).

        Two parts on one hole is a legal document that DRC objects to, so this takes the
        first in document order -- deterministic, and the overlap is already reported by
        the rule that owns it.
        """
        for comp in self.document.components:
            for pin, at in _pin_holes_of(comp, self.lookup):
                if at.col == hole.col and at.row == hole.row:
                    return comp.ref, pin.number
        return None

    def _net_holding(self, ref: str, pin: str) -> Net | None:
        for net in self.document.nets:
            if any((node.component_ref, node.pin) == (ref, pin) for node in net.nodes):
                return net
        return None

    def net_pin_click(self, at: HoleCoord) -> None:
        """Add the pin at this hole to the list being collected, or say why not.

        Every refusal the command would make is made HERE, one click at a time, rather
        than letting a session accumulate pins and then bounce the whole batch at commit
        for something the user did five clicks ago.
        """
        target = self._net_pin_target
        if target is None:
            return

        found = self._pin_at(at)
        if found is None:
            self.netPinRejected.emit(f"No component pin at {format_hole(at)}.")
            return

        ref, pin = found
        if (ref, pin) in self._net_pin_picks:
            self.netPinRejected.emit(f"{ref}.{pin} is already on the list.")
            return

        holder = self._net_holding(ref, pin)
        if holder is not None and holder.id == target:
            self.netPinRejected.emit(f"{ref}.{pin} is already on {holder.name}.")
            return
        if holder is not None:
            self.netPinRejected.emit(
                f"{ref}.{pin} belongs to {holder.name}. Disconnect it there first -- "
                f"a pin can only be on one net."
            )
            return

        self._net_pin_picks.append((ref, pin))
        self._net_pin_holes.append(at)
        if self._net_pin_item is not None:
            self._net_pin_item.set_holes(self._net_pin_holes)
        self.netPinsChanged.emit([f"{r}.{p}" for r, p in self._net_pin_picks])

    # -- joining two pins, which is the whole job in two clicks --------------
    #
    # Naming a net, then filling it, then routing it is the honest model and it was also
    # four steps deep in two menus before a single pin could be joined to anything. Most
    # of the time the user is not thinking "I shall declare a net"; they are pointing at
    # two legs and saying THOSE go together. This is that, and it produces exactly the
    # same documents the long way round does -- one command per pair, on the same bus.

    # -- cutting a track, which is how a stripboard is designed --------------
    #
    # On a pad-per-hole board every connection is added. On stripboard the copper is
    # already there in whole rows and the design is decided by where it is BROKEN, so
    # this is that board's primary edit -- the counterpart of drawing a solder trace.

    def arm_cutting(self, on: bool) -> None:
        """Arm (or disarm) cutting tracks. Silently refuses on a board with no tracks."""
        if on and not is_stripboard(self.document.board):
            on = False
        if on:
            self.arm_placement(None)
            self.arm_drawing(None)
            self._disarm_net_pins()
            self._disarm_connect()
            self._disarm_measure()
        self._cut_armed = on
        self.cutArmed.emit(on)

    @property
    def cut_armed(self) -> bool:
        return self._cut_armed

    def _disarm_cutting(self) -> None:
        if not self._cut_armed:
            return
        self._cut_armed = False
        self.cutArmed.emit(False)

    def cut_at(self, hole: HoleCoord) -> TrackCut | None:
        return next((c for c in self.document.cuts if c.at == hole), None)

    def cut_click(self, at: HoleCoord) -> DispatchResult | None:
        """Cut the track at this hole, or take back the cut already there.

        A toggle rather than an add-only tool, because a cut is small, invisible on the
        finished board and easy to put one hole out -- so the fix has to be the same
        gesture as the mistake. Each direction is still its own command and its own undo
        step; nothing here edits the document.
        """
        if not self._cut_armed or self.bus is None:
            return None
        if not is_inside_board(at, self.document.board):
            return None

        existing = self.cut_at(at)
        result = (
            self.bus.dispatch("cut.delete", DeleteCutPayload(id=existing.id))
            if existing is not None
            else self.bus.dispatch("cut.add", AddCutPayload(at=at))
        )
        self.cutMade.emit(result)
        return result

    # -- measuring, which is the one tool that changes nothing ---------------
    #
    # Perfboard work is full of distances that have to be right before anything is
    # soldered: how far apart to bend a resistor's legs, whether a TO-220 clears the
    # capacitor beside it, how long a wire has to be cut. All of it was countable off the
    # ruler and none of it was readable, so people counted holes on the screen with a
    # finger. Three numbers answer nearly all of it, and they are not the same number:
    # the hole span (how many holes across), the straight distance in mm (what you set a
    # pair of pliers or a lead-bending jig to), and the orthogonal step count (how long a
    # solder trace between the two would be).

    def arm_measure(self, on: bool) -> None:
        """Arm (or disarm) measuring between two holes."""
        if on:
            # The board modes are mutually exclusive: a click means one thing.
            self.arm_placement(None)
            self.arm_drawing(None)
            self._disarm_net_pins()
            self._disarm_connect()
            self._disarm_cutting()
        self._clear_measure()
        self._measure_armed = on
        self.measureArmed.emit(on)
        self.measured.emit("Click the first hole." if on else "")

    @property
    def measure_armed(self) -> bool:
        return self._measure_armed

    def measure_from(self) -> HoleCoord | None:
        """The hole a measurement started from, if one is half made."""
        return self._measure_from

    def _disarm_measure(self) -> None:
        if not self._measure_armed:
            return
        self._clear_measure()
        self._measure_armed = False
        self.measureArmed.emit(False)
        self.measured.emit("")

    def _clear_measure(self) -> None:
        if self._measure_item is not None:
            self.removeItem(self._measure_item)
            self._measure_item = None
        self._measure_from = None

    def measure_click(self, at: HoleCoord) -> None:
        """Take the first hole, or finish the measurement at the second.

        Finishing leaves the tool armed and the answer on screen: measuring one distance
        is almost never what somebody is doing -- they are comparing several -- and a
        tool that disarmed itself after each one would have to be re-armed for every
        comparison.
        """
        if not self._measure_armed or not is_inside_board(at, self.document.board):
            return
        if self._measure_from is None or self._measure_from == at:
            self._measure_from = at
            if self._measure_item is None:
                self._measure_item = PickedPinsItem(self.document.board, self.side)
                self.addItem(self._measure_item)
            self._measure_item.set_holes([at])
            self.measured.emit(f"From {format_hole(at)} — click the second hole.")
            return

        if self._measure_item is not None:
            self._measure_item.set_holes([self._measure_from, at])
        self.measured.emit(describe_span(self._measure_from, at, self.document.board))
        self._measure_from = None

    # -- leaving whatever mode you are in ------------------------------------

    @property
    def in_a_mode(self) -> bool:
        """Whether a click on the board currently means something other than 'select'."""
        return (
            self._armed_footprint is not None
            or self._draw_kind is not None
            or self._net_pin_target is not None
            or self._connect_armed
            or self._measure_armed
            or self._cut_armed
        )

    def leave_mode(self) -> None:
        """Disarm every board mode. This is what Escape means, wherever it is pressed.

        ONE method rather than a branch per mode at each call site, and that is the fix
        for a real bug rather than tidiness. Escape is a window shortcut, so it fires
        wherever the focus is -- and it fires BEFORE this scene sees the key at all, which
        made the placement branch that used to live in ``keyPressEvent`` unreachable in the
        running application: a part armed from the library could not be cancelled from the
        keyboard at all, and the hint under the list said "Esc cancels" the whole time.
        The window's shortcut and this handler now call the same method, so they cannot
        cancel different sets of things.
        """
        self.arm_placement(None)
        self.arm_drawing(None)
        self.arm_net_pins(None)
        self.arm_connect(False)
        self.arm_measure(False)
        self.arm_cutting(False)

    def arm_connect(self, on: bool) -> None:
        """Arm (or disarm) joining pins by clicking two of them."""
        if on:
            self.arm_placement(None)
            self.arm_drawing(None)
            self._disarm_net_pins()
            self._disarm_measure()
            self._disarm_cutting()
        self._clear_connect()
        self._connect_armed = on
        self.connectArmed.emit(on)
        self.connectProgress.emit([])

    @property
    def connect_armed(self) -> bool:
        return self._connect_armed

    def connect_from(self) -> tuple[str, str] | None:
        """The pin waiting for its partner, if a pair is half made. For the banner, and
        what the seam tests assert on."""
        return self._connect_first

    def _disarm_connect(self) -> None:
        if not self._connect_armed:
            return
        self._clear_connect()
        self._connect_armed = False
        self.connectArmed.emit(False)

    def _clear_connect(self) -> None:
        self._connect_first = None
        self._connect_hole = None
        if self._connect_item is not None:
            self.removeItem(self._connect_item)
            self._connect_item = None

    def connect_click(self, at: HoleCoord) -> DispatchResult | None:
        """Take the first pin, or join the second to it.

        Four outcomes, and each is stated rather than guessed at:
          - neither pin is on a net  -> a new net, named for you, holding both
          - one of them is           -> the other joins THAT net, which is what a person
                                        pointing at a pin and then at a rail means
          - both, and the same net   -> nothing to do, and it says so
          - both, different nets     -> refused, naming both. Merging two nets is a
                                        decision about the circuit, not about two clicks,
                                        and it is not one this tool may take on its own.
        """
        if not self._connect_armed:
            return None

        found = self._pin_at(at)
        if found is None:
            self.netPinRejected.emit(f"No component pin at {format_hole(at)}.")
            return None

        first = self._connect_first
        if first is None:
            self._connect_first = found
            self._connect_hole = at
            self._connect_item = PickedPinsItem(self.document.board, self.side)
            self._connect_item.set_holes([at])
            self.addItem(self._connect_item)
            self.connectProgress.emit([f"{found[0]}.{found[1]}"])
            return None

        if found == first:
            self.netPinRejected.emit(f"{found[0]}.{found[1]} is already the pin you started from.")
            return None

        result = self._join_pins(first, found)
        # Cleared either way: a refused pair is not a pair you are still in the middle of,
        # and leaving the first pin armed would make the next click join something the
        # user has stopped thinking about.
        self._clear_connect()
        self.connectProgress.emit([])
        if result is not None:
            self.pinsConnected.emit(result)
        return result

    def _join_pins(
        self, first: tuple[str, str], second: tuple[str, str]
    ) -> DispatchResult | None:
        if self.bus is None:
            return None
        net_a = self._net_holding(*first)
        net_b = self._net_holding(*second)
        a = NetNode(component_ref=first[0], pin=first[1])
        b = NetNode(component_ref=second[0], pin=second[1])

        if net_a is not None and net_b is not None:
            if net_a.id == net_b.id:
                self.netPinRejected.emit(
                    f"{first[0]}.{first[1]} and {second[0]}.{second[1]} are both already on "
                    f"{net_a.name}."
                )
            else:
                self.netPinRejected.emit(
                    f"{first[0]}.{first[1]} is on {net_a.name} and {second[0]}.{second[1]} is "
                    f"on {net_b.name}. Joining two nets is a change to the circuit — "
                    f"disconnect one of the pins first."
                )
            return None

        if net_a is not None:
            return self.bus.dispatch("net.connect", ConnectPinsPayload(id=net_a.id, nodes=(b,)))
        if net_b is not None:
            return self.bus.dispatch("net.connect", ConnectPinsPayload(id=net_b.id, nodes=(a,)))
        return self.bus.dispatch(
            "net.add",
            AddNetPayload(name=next_net_name(self.document), net_class="signal", nodes=(a, b)),
        )

    def commit_net_pins(self) -> DispatchResult | None:
        """Dispatch ONE ``net.connect`` for everything picked. Nothing picked is a cancel.

        One command for the whole session on purpose: "GND is these five pins" was one
        decision, and an undo that leaves three of them attached is a state nobody asked
        for.
        """
        target = self._net_pin_target
        picks = list(self._net_pin_picks)
        self._clear_net_pins()
        self._net_pin_target = None
        self.netPinsArmed.emit("")

        if self.bus is None or target is None or not picks:
            return None

        result = self.bus.dispatch(
            "net.connect",
            ConnectPinsPayload(
                id=target,
                nodes=tuple(NetNode(component_ref=ref, pin=pin) for ref, pin in picks),
            ),
        )
        self.netPinsCommitted.emit(result)
        return result

    def selected_conductor_ids(self) -> tuple[str, ...]:
        return tuple(
            item.conductor_id
            for item in self.items()
            if isinstance(item, ConductorItem) and item.isSelected()
        )

    def arm_placement(self, footprint_id: str | None) -> None:
        """Arm (or, with None, disarm) placing a footprint on the next board click."""
        self._armed_footprint = None if footprint_id is None else self.lookup(footprint_id)
        self._armed_id = footprint_id if self._armed_footprint is not None else None
        self._clear_ghost()
        if self._armed_footprint is not None:
            self._disarm_net_pins()
            self._disarm_connect()
            self._disarm_measure()
            self._disarm_cutting()
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
        at = screen_to_hole(event.scenePos(), self.document.board, self.side)
        if self._ghost is not None:
            self._ghost.set_anchor(at, self._placement_blocked(at))
        if self._draw_preview is not None:
            self._refresh_draw_preview(at)
        # Live, because the question is nearly always "how far is it to about there"
        # rather than "how far is it to exactly that hole": the answer has to move with
        # the pointer or it is a two-click way of asking something already answered.
        if self._measure_from is not None and is_inside_board(at, self.document.board):
            self.measured.emit(describe_span(self._measure_from, at, self.document.board))
        self.hoveredHole.emit(at.col, at.row)
        super().mouseMoveEvent(event)

    def take_consumed_right_click(self) -> bool:
        """Whether the last right press was a MODE's, and clear the record of it.

        Right-click already means something on this board: it finishes a trace, commits a
        net, or leaves the tool. Those are handled on the press, and a context menu event
        follows the release -- by which time the mode has been left, so a menu built on
        "is a mode armed" would pop up on top of the very gesture that ended it. The press
        says it took the click; the view asks once and the answer is spent.
        """
        taken = self._right_click_taken
        self._right_click_taken = False
        return taken

    def mousePressEvent(self, event: Any) -> None:
        if self.in_a_mode and event.button() == Qt.MouseButton.RightButton:
            self._right_click_taken = True
        if self._cut_armed:
            at = screen_to_hole(event.scenePos(), self.document.board, self.side)
            if event.button() == Qt.MouseButton.LeftButton:
                self.cut_click(at)
            elif event.button() == Qt.MouseButton.RightButton:
                self._disarm_cutting()
            event.accept()
            return
        if self._measure_armed:
            at = screen_to_hole(event.scenePos(), self.document.board, self.side)
            if event.button() == Qt.MouseButton.LeftButton:
                self.measure_click(at)
            elif event.button() == Qt.MouseButton.RightButton:
                self._disarm_measure()
            event.accept()
            return
        if self._connect_armed:
            at = screen_to_hole(event.scenePos(), self.document.board, self.side)
            if event.button() == Qt.MouseButton.LeftButton:
                self.connect_click(at)
            elif event.button() == Qt.MouseButton.RightButton:
                self._disarm_connect()
            event.accept()
            return
        if self._net_pin_target is not None:
            at = screen_to_hole(event.scenePos(), self.document.board, self.side)
            if event.button() == Qt.MouseButton.LeftButton:
                self.net_pin_click(at)
            elif event.button() == Qt.MouseButton.RightButton:
                # Right-click finishes the net, the same gesture that finishes a trace.
                self.commit_net_pins()
            event.accept()
            return
        if self._draw_kind is not None:
            at = screen_to_hole(event.scenePos(), self.document.board, self.side)
            if event.button() == Qt.MouseButton.LeftButton:
                self.draw_click(at)
            elif event.button() == Qt.MouseButton.RightButton:
                # Right-click ends a trace where it is, which is what a chain-drawing tool
                # in any editor does. Under two holes it cancels instead.
                self.commit_drawing()
            event.accept()
            return
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
                value=self.placement_value,
                footprint_id=self._armed_id,
                anchor=anchor,
            ),
        )
        self.componentPlaced.emit(result)
        return result

    def mouseReleaseEvent(self, event: Any) -> None:
        super().mouseReleaseEvent(event)
        self.commit_pending_moves()

    def component_at(self, pos: QPointF) -> ComponentItem | None:
        """The topmost part under a scene position, or None.

        Walks this scene's own component table rather than calling ``itemAt``, which
        answers a different question: it returns the topmost item of ANY kind, so a
        conductor or a ratsnest line lying over a part -- which is the normal state of a
        routed board -- would swallow the click and report no part at all. Ties go to the
        highest z, so two overlapping parts resolve to the one the user can see.
        """
        best: ComponentItem | None = None
        for item in self.component_items.values():
            if not item.contains(item.mapFromScene(pos)):
                continue
            if best is None or item.zValue() >= best.zValue():
                best = item
        return best

    def mouseDoubleClickEvent(self, event: Any) -> None:
        """Double-clicking a part asks the host to open it.

        Ignored outright while a mode is armed. Every mode gives the FIRST click of the
        pair a meaning of its own -- a double-click while drawing has already laid two
        steps of a trace -- so opening a dialog on top of that would interrupt the tool
        with a window the user did not ask for.
        """
        if self.in_a_mode or event.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        item = self.component_at(event.scenePos())
        if item is None:
            super().mouseDoubleClickEvent(event)
            return
        self.componentActivated.emit(item.comp.id)
        event.accept()

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
        if event.key() == Qt.Key.Key_Escape and self.in_a_mode:
            self.leave_mode()
            event.accept()
            return
        if self._net_pin_target is not None and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            self.commit_net_pins()
            event.accept()
            return
        if self._draw_kind is not None and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            self.commit_drawing()
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


class ViewOverlay(QLabel):
    """A line of text pinned over the board itself.

    The status bar is the bottom edge of a window a metre wide and the cursor is in the
    middle of the board, which is exactly where "why is my click doing nothing" gets
    asked. A mode is that question's usual answer, so what is armed and how to leave it
    has to be where the eye already is.

    TRANSPARENT TO THE MOUSE, without exception. This sits over the top of the board and
    the whole point of a mode is that the next click goes to the board -- an overlay that
    ate the click it is describing would be worse than no overlay.
    """

    def __init__(self, parent: Any, style: str) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setStyleSheet(style)
        self.hide()

    def show_text(self, text: str) -> None:
        """Set the text and show it, or hide entirely when there is nothing to say."""
        if not text:
            self.hide()
            return
        self.setText(text)
        self.adjustSize()
        self.show()


#: Armed mode. Blue: this is a state the user chose and can leave, not a problem.
MODE_BANNER_STYLE = f"""
    background: rgba(44, 95, 160, 235);
    color: {THEME_TEXT};
    border: 1px solid {THEME_ACCENT};
    border-radius: 6px;
    padding: 6px 14px;
"""

#: An empty board. Dim, because it is guidance rather than a message about anything wrong,
#: and OPAQUE, because the pad grid showing through a paragraph is what makes people stop
#: reading it. There is nothing on the board underneath worth seeing anyway -- that is the
#: condition this block appears in.
EMPTY_HINT_STYLE = f"""
    background: {THEME_PANEL};
    color: {THEME_TEXT_DIM};
    border: 1px solid {THEME_BORDER};
    border-radius: 8px;
    padding: 16px 24px;
"""


class BoardView(QGraphicsView):
    #: Gap between the top of the viewport and the mode banner.
    BANNER_MARGIN_PX = 10

    #: Emitted with the viewport position of a right-click that wants a menu. WHAT is on
    #: that menu is the host's business -- it owns the actions, and a view that built its
    #: own would be a second list of what can be done to a part, free to disagree with the
    #: menu bar about which of them are available.
    contextMenuRequested = Signal(QPoint)

    def __init__(self, scene: BoardScene) -> None:
        super().__init__(scene)
        self.board_scene = scene
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.scale(6, 6)  # ~6 px per mm to start
        self._panning = False
        self._pan_origin = QPointF()
        # Children of the VIEWPORT, not of the view: the viewport is what the scrollbars
        # leave behind, so an overlay parented to it stays inside the board area and does
        # not drift under a scrollbar when one appears.
        self.mode_banner = ViewOverlay(self.viewport(), MODE_BANNER_STYLE)
        self.empty_hint = ViewOverlay(self.viewport(), EMPTY_HINT_STYLE)
        self.viewport().installEventFilter(self)

    # -- overlays ------------------------------------------------------------

    def show_mode(self, text: str) -> None:
        """Say which mode is armed and how to leave it, or clear it with an empty string."""
        self.mode_banner.show_text(text)
        self._place_overlays()

    def set_empty_hint(self, text: str) -> None:
        """Guidance for a board with nothing on it. Empty text takes it away."""
        self.empty_hint.show_text(text)
        self._place_overlays()

    #: How wide the guidance block is allowed to get. Measured rather than chosen: a line
    #: much longer than this stops being read as a sentence and starts being skipped.
    HINT_WIDTH_PX = 460

    def _place_overlays(self) -> None:
        area = self.viewport().rect()

        # isHidden(), never isVisible(). A widget is "visible" only once every ancestor is
        # too, so during window construction -- which is exactly when the first hint is set
        # -- these labels are shown but not yet visible, and an isVisible() test would skip
        # placing them and leave them where the un-laid-out viewport put them.
        banner = self.mode_banner
        if not banner.isHidden():
            # One line if it fits, because the banner is a label rather than a paragraph
            # and a two-line label reads as a warning. Only a viewport too narrow for the
            # sentence makes it wrap.
            banner.setWordWrap(False)
            banner.adjustSize()
            widest = max(area.width() - 2 * self.BANNER_MARGIN_PX, 120)
            if banner.width() > widest:
                banner.setWordWrap(True)
                banner.setFixedWidth(widest)
                banner.adjustSize()
            banner.move(
                area.center().x() - banner.width() // 2,
                area.top() + self.BANNER_MARGIN_PX,
            )

        hint = self.empty_hint
        if not hint.isHidden():
            hint.setFixedWidth(min(self.HINT_WIDTH_PX, max(area.width() - 80, 160)))
            hint.adjustSize()
            hint.move(
                area.center().x() - hint.width() // 2, area.center().y() - hint.height() // 2
            )

    def eventFilter(self, watched: Any, event: Any) -> bool:
        """Re-place the overlays whenever the area under them changes size.

        Watching the VIEWPORT rather than overriding resizeEvent on the view, because the
        viewport is what actually changes: a scrollbar appearing takes 15 px off it without
        the view being resized at all, and the first layout after show() resizes the
        viewport when the view has already had its resize event. Getting this wrong leaves
        the guidance block sitting off to one side of the board, which is exactly where a
        first-time user is not looking.
        """
        if watched is self.viewport() and event.type() == QEvent.Type.Resize:
            self._place_overlays()
        return bool(super().eventFilter(watched, event))

    def contextMenuEvent(self, event: Any) -> None:
        """Ask the host for a menu, unless a mode has a claim on this click.

        Two guards rather than one, and both are needed. ``in_a_mode`` covers a platform
        that delivers the context-menu event on the PRESS; ``take_consumed_right_click``
        covers one that delivers it on the release, after a press that ended the mode --
        which is Windows, and is exactly where a menu would appear over a trace the user
        had just finished.
        """
        if self.board_scene.in_a_mode or self.board_scene.take_consumed_right_click():
            event.accept()
            return
        self.contextMenuRequested.emit(event.pos())
        event.accept()

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
