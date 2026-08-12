"""Hole coordinate system and grid geometry.

Pure and deterministic: no clock, no randomness, no I/O. This module is the bridge
between the abstract {col,row} addressing the document model uses and the two concrete
things a human or a renderer needs — the spreadsheet-style HoleRef ("A1", "AC12") the
soldering guide speaks, and millimetres on the physical board.

It also owns the orthogonal-adjacency invariant solder traces depend on, and the
placement maths that turns a footprint plus a component transform into absolute hole
positions. There is exactly one implementation of each of these in the codebase, on
purpose: when the renderer's idea of "rotated" drifts from the connectivity engine's,
parts get drawn in one place and wired in another, and nothing flags it until a
physical board comes back wrong.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .model import (
    Board,
    BoardEdge,
    BoardLabels,
    BoardMaterial,
    BoardSide,
    ComponentInstance,
    EdgeConnector,
    Footprint,
    FootprintPin,
    HoleCoord,
    HoleRef,
    Mm,
    MountingHole,
    PadAxis,
    PerfDocument,
    Point2,
    Rotation,
)

# ---------------------------------------------------------------------------
# Hole reference <-> coordinate
# ---------------------------------------------------------------------------

#: One or more uppercase letters (the column, in bijective base-26) followed by a
#: 1-indexed row with no leading zero. Anchored so trailing garbage is rejected.
_HOLE_REF_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")


def _column_letters(col: int) -> str:
    """Encode a 0-indexed column in spreadsheet notation.

    0 -> "A", 25 -> "Z", 26 -> "AA", 51 -> "AZ", 52 -> "BA", 701 -> "ZZ", 702 -> "AAA".

    This is bijective base-26, not plain base-26: a letter system has no digit for
    zero, so "AA" is not "0,0" but the number after "Z".
    """
    if not isinstance(col, int) or isinstance(col, bool) or col < 0:
        raise ValueError(f"Invalid hole column index: {col!r} (must be a non-negative integer).")
    n = col + 1
    letters = ""
    while n > 0:
        n -= 1
        letters = chr(65 + n % 26) + letters
        n //= 26
    return letters


def _column_index(letters: str) -> int:
    """Inverse of :func:`_column_letters`. Assumes ``letters`` already matches [A-Z]+."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def coord_to_hole_ref(c: HoleCoord) -> HoleRef:
    """Convert a 0-indexed hole coordinate to its human-facing address."""
    if not isinstance(c.row, int) or isinstance(c.row, bool) or c.row < 0:
        raise ValueError(f"Invalid hole row index: {c.row!r} (must be a non-negative integer).")
    return f"{_column_letters(c.col)}{c.row + 1}"


def column_label(col: int) -> str:
    """The column half of a hole address, e.g. 0 -> "A", 28 -> "AC".

    Exposed for the editor's rulers, which label an axis rather than a hole. They must
    read the same as every address in the build guide, so the encoding is shared rather
    than re-derived -- a ruler that disagreed with the guide would be worse than no ruler.
    """
    return _column_letters(col)


def row_label(row: int) -> str:
    """The row half of a hole address: 0-indexed in, 1-indexed out."""
    if not isinstance(row, int) or isinstance(row, bool) or row < 0:
        raise ValueError(f"Invalid hole row index: {row!r} (must be a non-negative integer).")
    return str(row + 1)


def printed_row_label(row: int, labels: BoardLabels) -> str:
    """The row half as the BOARD prints it, which may be zero-padded to a fixed width.

    A board that prints "01".."22" is showing the same address the guide calls row 1 —
    the padding is typography, not a different numbering. So this exists only for
    rendering a legend; nothing that parses or compares an address may use it, and
    :func:`hole_ref_to_coord` deliberately rejects the padded form.
    """
    return row_label(row).rjust(max(1, labels.row_digits), "0")


def hole_ref_to_coord(ref: HoleRef) -> HoleCoord:
    """Parse a human-facing hole address back into a 0-indexed coordinate.

    Strict on purpose: this is the canonical decoder and its round-trip with
    :func:`coord_to_hole_ref` is a guarantee. Rejects "1A", "", "A0", "A-1", "A1.5"
    and lowercase input.
    """
    match = _HOLE_REF_PATTERN.match(ref) if isinstance(ref, str) else None
    if match is None:
        raise ValueError(
            f"Malformed hole reference: {ref!r}. Expected uppercase column letters "
            f'followed by a 1-indexed row number, e.g. "A1" or "AC12".'
        )
    return HoleCoord(col=_column_index(match.group(1)), row=int(match.group(2)) - 1)


def format_hole(c: HoleCoord) -> str:
    """Name a hole for a message, without ever raising.

    :func:`coord_to_hole_ref` is strict because its round-trip is a guarantee, so it
    rejects negative coordinates. But the code that most needs to name a hole is the
    code reporting that something is in the wrong place, and an off-board component
    sits at a negative column by definition — a strict encoder there means the checker
    crashes on exactly the violation it exists to report.

    Use ``coord_to_hole_ref`` when the value must round-trip, ``format_hole`` whenever
    the result goes into a message for a human.
    """
    try:
        return coord_to_hole_ref(c)
    except ValueError:
        return f"(col {c.col}, row {c.row})"


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def hole_key(c: HoleCoord) -> str:
    """Stable string key for dict/set use.

    The comma makes the encoding unambiguous for any pair of integers, since an
    integer's decimal form never contains one.
    """
    return f"{c.col},{c.row}"


# ---------------------------------------------------------------------------
# Millimetre geometry
# ---------------------------------------------------------------------------


def hole_to_mm(c: HoleCoord, board: Board) -> Point2:
    """Physical centre of a hole, in board-space millimetres.

    Hole {0,0} sits at the origin; x grows with col (rightward), y grows with row
    (downward), spaced by ``board.pitch``.
    """
    return Point2(c.col * board.pitch, c.row * board.pitch)


def is_inside_board(c: HoleCoord, board: Board) -> bool:
    return 0 <= c.col < board.cols and 0 <= c.row < board.rows


@dataclass(frozen=True, slots=True)
class RectMm:
    x: Mm
    y: Mm
    width: Mm
    height: Mm


def board_size_mm(board: Board) -> tuple[Mm, Mm]:
    """Physical size of the board in mm.

    THE CONVENTION, defined here once and nowhere else: the substrate extends half a
    pitch beyond the outermost hole centres on every side. So the hole centres span
    ``(cols - 1) * pitch`` while the board measures ``cols * pitch``. A 60-column board
    at 2.54 mm pitch is 152.4 mm wide, which is how perfboard is actually sold and cut.

    ``board.border_x_mm`` / ``border_y_mm`` widen that, for boards cut with a printed
    border round the grid. They are added here and nowhere else, which is what keeps a
    bordered board's substrate, printout and 3D solid the same size as each other.

    Must not be recomputed anywhere else. The 1:1-scale printable sheet, the 3D
    substrate and the 2D renderer have to agree to the last tenth of a millimetre,
    because the user tapes the printout onto the physical board.
    """
    return (
        board.cols * board.pitch + 2 * board.border_x_mm,
        board.rows * board.pitch + 2 * board.border_y_mm,
    )


def hole_span_mm(board: Board) -> tuple[Mm, Mm]:
    """Distance from the FIRST hole centre to the LAST: ``(cols - 1) * pitch``.

    This is NOT :func:`board_size_mm`, and confusing the two is a real hazard. Use this
    wherever holes must map onto holes — above all when mirroring the board to show the
    solder side, where the reflection ``x -> hole_span - x`` has to land hole 0 exactly
    on hole ``cols-1``. Reflecting about the physical board size instead shifts the
    whole grid by half a pitch, and the user solders the board backwards without the
    view ever looking obviously wrong.

    Rule of thumb: holes and routing use ``hole_span_mm``; substrate, printing and 3D
    use ``board_size_mm``.
    """
    return max(0, board.cols - 1) * board.pitch, max(0, board.rows - 1) * board.pitch


def board_edge_margin_mm(board: Board, axis: PadAxis = "horizontal") -> Mm:
    """How much substrate sits outside the outermost hole centres, on one axis.

    Half a pitch plus whatever border the board was cut with. This is the strip a printed
    legend has to fit inside, and the distance an edge-connector finger has to cross to
    reach the edge, so both ask this rather than assuming half a pitch.

    ``horizontal`` is the margin at the LEFT and RIGHT edges (the one a row number printed
    down the side has to fit inside); ``vertical`` is the margin at the top and bottom.
    They differ on a real board -- see the note on ``Board.border_x_mm``.
    """
    border = board.border_x_mm if axis == "horizontal" else board.border_y_mm
    return board.pitch / 2 + border


def board_outline_mm(board: Board) -> RectMm:
    """Board outline as a rect in the same mm space as :func:`hole_to_mm`."""
    width, height = board_size_mm(board)
    return RectMm(
        -board_edge_margin_mm(board, "horizontal"),
        -board_edge_margin_mm(board, "vertical"),
        width,
        height,
    )


# ---------------------------------------------------------------------------
# Copper: how big a pad is, and how close the next one's copper comes
# ---------------------------------------------------------------------------


def pad_extent_mm(board: Board) -> tuple[Mm, Mm]:
    """One pad's size as (across columns, across rows), in mm.

    A round pad is its diameter both ways. An oblong pad is ``pad_length`` along
    ``pad_axis`` and ``pad_diameter`` across it — so on a vertical-oblong board the pads
    nearly touch down a column while staying comfortably apart along a row.

    A missing ``pad_length`` on an oblong board falls back to the diameter (i.e. round)
    rather than raising: this is called from paint paths and from DRC, and a hand-edited
    file that omits the length must still draw and still be checkable. ``board.set``
    refuses that combination, so it cannot arrive from the editor.
    """
    if board.pad_shape != "oblong" or board.pad_length is None:
        return board.pad_diameter, board.pad_diameter
    long_axis = max(board.pad_length, board.pad_diameter)
    if board.pad_axis == "horizontal":
        return long_axis, board.pad_diameter
    return board.pad_diameter, long_axis


def pad_edge_gap_mm(board: Board, axis: PadAxis) -> Mm:
    """Copper-to-copper gap between two neighbouring PADS, along one axis.

    "horizontal" is the gap to the neighbour one column away, "vertical" one row away.
    On a round-pad board the two are equal and this is the familiar ~0.6 mm figure that
    R5' is about; on an oblong-pad board they differ, and which one applies depends on
    which way the trace is running. See :func:`copper_gap_mm` for the question DRC
    actually asks, which also accounts for edge-connector fingers.
    """
    extent_x, extent_y = pad_extent_mm(board)
    return max(0.0, board.pitch - (extent_x if axis == "horizontal" else extent_y))


def neighbour_axis(a: HoleCoord, b: HoleCoord) -> PadAxis:
    """Which axis separates two holes. Rows apart counts as vertical."""
    return "vertical" if a.row != b.row else "horizontal"


def pad_rect_mm(board: Board, hole: HoleCoord) -> RectMm:
    """The bounding rect of one ordinary pad's copper.

    An oblong pad is a stadium rather than a rectangle, but its bounding box touches the
    true outline exactly on the centre lines — which is where an orthogonal neighbour
    sits — so measuring gaps off the box is exact for the only question anyone asks of
    it, and much cheaper than the real thing.
    """
    centre = hole_to_mm(hole, board)
    extent_x, extent_y = pad_extent_mm(board)
    return RectMm(centre.x - extent_x / 2, centre.y - extent_y / 2, extent_x, extent_y)


def copper_rect_mm(doc: PerfDocument, hole: HoleCoord) -> RectMm:
    """The copper at one hole: its pad, or the connector finger that replaced it.

    A finger is NOT centred on its hole — it reaches out to the board edge — which is
    why gaps are measured between rects rather than by subtracting extents from the
    pitch.
    """
    connector = edge_connector_at(doc, hole)
    if connector is not None:
        return edge_finger_rect(connector, hole, doc.board)
    return pad_rect_mm(doc.board, hole)


def copper_gap_mm(doc: PerfDocument, a: HoleCoord, b: HoleCoord) -> Mm:
    """Edge-to-edge gap between the copper at two orthogonally adjacent holes.

    This is the number R5' is about — how far a blob of solder has to travel to bridge
    two nets — and it is not one number per board. It varies with the pad shape, with
    which way the neighbour lies, and with whether either hole has been widened into a
    connector finger. Clamped at zero: copper that already overlaps has no gap, and a
    negative one would read as a comfortable clearance in a message.
    """
    rect_a = copper_rect_mm(doc, a)
    rect_b = copper_rect_mm(doc, b)
    if neighbour_axis(a, b) == "horizontal":
        gap = max(rect_a.x, rect_b.x) - min(rect_a.x + rect_a.width, rect_b.x + rect_b.width)
    else:
        gap = max(rect_a.y, rect_b.y) - min(rect_a.y + rect_a.height, rect_b.y + rect_b.height)
    return max(0.0, gap)


# ---------------------------------------------------------------------------
# Mounting holes
# ---------------------------------------------------------------------------


def holes_without_grid_pad(doc: PerfDocument, side: BoardSide) -> frozenset[str]:
    """Holes where the ordinary grid pad must NOT be drawn on this face.

    Two quite different reasons, answered together because a renderer only wants one
    question answered: a mounting bore has removed the copper, or an edge-connector
    finger IS the pad here and drawing a round one underneath makes the finger look like
    something laid on top of the board rather than part of it.

    Face-sensitive for the second reason only. A connector on the far face leaves this
    one with its ordinary round pad, which is exactly what the board has.
    """
    without = set(consumed_holes(doc))
    for connector in doc.edge_connectors:
        if connector.face not in ("both", side):
            continue
        for hole in edge_connector_holes(connector, doc.board):
            without.add(hole_key(hole))
    return frozenset(without)


def undrilled_holes(doc: PerfDocument) -> frozenset[str]:
    """Grid positions that are NOT drilled, as :func:`hole_key` strings.

    An edge-connector finger is a solid contact. It has no bore: there is nothing to put
    a lead through, which is the difference between a finger and a pad and is why a
    finger is soldered to from the surface. The renderers were drilling straight through
    them because the grid drills every position it has, so the strip came out looking
    like an ordinary row of pads that happened to be long.

    Not face-sensitive, unlike :func:`holes_without_grid_pad`: a hole either goes through
    the board or it does not, whichever side the copper is on.
    """
    if not doc.edge_connectors:
        return frozenset()
    return frozenset(
        hole_key(hole)
        for connector in doc.edge_connectors
        for hole in edge_connector_holes(connector, doc.board)
    )


def mounting_hole_centre_mm(hole_mount: MountingHole, board: Board) -> Point2:
    """Where the bore actually is, in board-space mm.

    Its hole address plus its offset. Everything that asks where a mounting hole is goes
    through here: the offset is what lets a corner hole sit in the border, and a caller
    that used ``hole_to_mm(mount.at)`` directly would put it back on the grid and report
    pads destroyed that are perfectly intact.
    """
    centre = hole_to_mm(hole_mount.at, board)
    return Point2(centre.x + hole_mount.offset_x_mm, centre.y + hole_mount.offset_y_mm)


def mounting_bore_consumes(hole_mount: MountingHole, hole: HoleCoord, board: Board) -> bool:
    """Whether a mounting bore destroys the copper at ``hole``.

    A screw hole is far wider than a pad, so it takes out more than the hole it is drilled
    on. An M3 clearance bore (3.2 mm) centred on a 2.54 mm grid reaches 1.6 mm out, and
    the neighbouring pad's near edge is only 2.54 - 0.95 = 1.59 mm away — so the four
    orthogonal neighbours lose their copper too, while the diagonals (3.59 mm away)
    survive. That is a genuine property of the board and not a safety margin, which is
    why the test is a plain overlap of bore against pad rather than a tunable clearance.
    """
    centre = mounting_hole_centre_mm(hole_mount, board)
    pad = hole_to_mm(hole, board)
    extent_x, extent_y = pad_extent_mm(board)
    # Against the pad's inscribed radius: the smaller half-extent, so an oblong pad is
    # not reported as consumed on the strength of a corner the bore never reaches.
    reach = hole_mount.diameter / 2 + min(extent_x, extent_y) / 2
    return math.hypot(pad.x - centre.x, pad.y - centre.y) < reach


def consumed_holes(doc: PerfDocument) -> frozenset[str]:
    """Every hole whose pad a mounting bore has destroyed, as :func:`hole_key` strings.

    Computed once and passed around rather than re-derived per hole: the renderer asks
    this of every hole on the board on every paint.
    """
    if not doc.mounting_holes:
        return frozenset()
    consumed: set[str] = set()
    for mount in doc.mounting_holes:
        # Only the immediate neighbourhood can be reached, so this stays O(mounting
        # holes) rather than O(holes) -- 3 pitches is comfortably past any plausible bore.
        span = int((mount.diameter + abs(mount.offset_x_mm) + abs(mount.offset_y_mm)) / doc.board.pitch) + 2
        for d_col in range(-span, span + 1):
            for d_row in range(-span, span + 1):
                hole = HoleCoord(mount.at.col + d_col, mount.at.row + d_row)
                if not is_inside_board(hole, doc.board):
                    continue
                if mounting_bore_consumes(mount, hole, doc.board):
                    consumed.add(hole_key(hole))
    return frozenset(consumed)


def mounting_head_covers(hole_mount: MountingHole, point: Point2, board: Board) -> bool:
    """Whether a screw head or washer would sit over this point on the component side."""
    centre = mounting_hole_centre_mm(hole_mount, board)
    return math.hypot(point.x - centre.x, point.y - centre.y) < hole_mount.head_diameter / 2


# ---------------------------------------------------------------------------
# Edge connectors
# ---------------------------------------------------------------------------


def edge_connector_holes(connector: EdgeConnector, board: Board) -> list[HoleCoord]:
    """The holes a connector's fingers sit on, in run order, clipped to the board."""
    holes: list[HoleCoord] = []
    for step in range(max(0, connector.count)):
        index = connector.start + step
        if connector.edge == "top":
            hole = HoleCoord(index, 0)
        elif connector.edge == "bottom":
            hole = HoleCoord(index, board.rows - 1)
        elif connector.edge == "left":
            hole = HoleCoord(0, index)
        else:
            hole = HoleCoord(board.cols - 1, index)
        if is_inside_board(hole, board):
            holes.append(hole)
    return holes


def edge_connector_at(doc: PerfDocument, hole: HoleCoord) -> EdgeConnector | None:
    """The connector whose finger covers this hole, if any."""
    for connector in doc.edge_connectors:
        if any(h == hole for h in edge_connector_holes(connector, doc.board)):
            return connector
    return None


def legend_strip_mm(doc: PerfDocument, axis: PadAxis) -> Mm:
    """Bare substrate a printed legend has on one axis, after the copper takes its share.

    Usually the margin less half a pad. But on an edge carrying connector fingers the
    copper reaches much further out, and what is left is only whatever inset those
    fingers were given — which is exactly the strip the row numbers are printed in on a
    real board. Returning the margin there instead would print the numbers underneath the
    fingers.
    """
    board = doc.board
    margin = board_edge_margin_mm(board, axis)
    extent_x, extent_y = pad_extent_mm(board)
    free = margin - (extent_x if axis == "horizontal" else extent_y) / 2
    for connector in doc.edge_connectors:
        if edge_axis(connector.edge) != axis:
            continue
        free = min(free, max(0.0, connector.inset_mm))
    return max(0.0, free)


def edge_axis(edge: BoardEdge) -> PadAxis:
    """Which margin an edge is measured across: left/right cross the horizontal one."""
    return "horizontal" if edge in ("left", "right") else "vertical"


def default_finger_length_mm(board: Board, edge: BoardEdge = "bottom") -> Mm:
    """How far a finger should reach in from the edge when it is not told.

    Enough to swallow its own hole and half a pitch beyond — which puts its inner end
    clear of the next hole's pad, so a finger still covers exactly one hole. On a
    flush-cut board this is one pitch; on a bordered one it is a pitch plus that edge's
    border, and using the flush figure there would leave the finger short of its own hole.
    """
    return board_edge_margin_mm(board, edge_axis(edge)) + board.pitch / 2


def edge_finger_rect(connector: EdgeConnector, hole: HoleCoord, board: Board) -> RectMm:
    """One finger's copper: from the board edge inward, centred on its hole across the run.

    Measured from the EDGE rather than from the hole centre, because that is what the
    finger is for — reaching the edge is the whole point — and because the outermost hole
    sits only half a pitch in, so an inward-measured length would have to be negative to
    describe anything useful.
    """
    outline = board_outline_mm(board)
    centre = hole_to_mm(hole, board)
    inset = max(0.0, connector.inset_mm)
    length = max(
        0.0,
        (
            connector.finger_length
            if connector.finger_length is not None
            else default_finger_length_mm(board, connector.edge)
        )
        - inset,
    )
    half_width = max(0.0, connector.finger_width) / 2
    if connector.edge == "top":
        return RectMm(centre.x - half_width, outline.y + inset, 2 * half_width, length)
    if connector.edge == "bottom":
        bottom = outline.y + outline.height - inset
        return RectMm(centre.x - half_width, bottom - length, 2 * half_width, length)
    if connector.edge == "left":
        return RectMm(outline.x + inset, centre.y - half_width, length, 2 * half_width)
    right = outline.x + outline.width - inset
    return RectMm(right - length, centre.y - half_width, length, 2 * half_width)


# ---------------------------------------------------------------------------
# The boards you can actually buy
# ---------------------------------------------------------------------------


#: Which family of board a preset is. Not decoration: it decides copper on one face or
#: two, whether the board carries a printed legend, and whether its outer rows are the
#: oblong finger pads. Those three travel together on the shelf, and setting them
#: separately is three chances to describe a board nobody sells.
BoardFamily: TypeAlias = Literal["double-sided-fr4", "single-sided-phenolic"]


@dataclass(frozen=True, slots=True)
class BoardPreset:
    """One perfboard as it is sold: a size, and which of the two families it belongs to.

    Perfboard is not bought by hole count -- it is bought as "a 5 by 7", and the listing
    quotes the centimetres. So the presets are keyed on that, and the hole count is what
    falls out of it: the grid is what fits once the printed border is taken off, which is
    why a 4 x 6 cm board is 20 x 14 and not the 15 x 23 that dividing by the pitch
    suggests.

    The border is then DERIVED rather than quoted, so the outline is exactly the
    advertised size to the tenth of a millimetre. That matters more here than anywhere
    else: the 1:1 PDF export gets taped onto the physical board.
    """

    #: What the listing calls it, e.g. "5 x 7 cm".
    name: str
    width_mm: Mm
    height_mm: Mm
    cols: int
    rows: int
    family: BoardFamily = "double-sided-fr4"

    @property
    def single_sided(self) -> bool:
        return self.family == "single-sided-phenolic"

    @property
    def material(self) -> BoardMaterial:
        return "FR2" if self.single_sided else "FR4"

    @property
    def key(self) -> str:
        return f"{self.width_mm:g}x{self.height_mm:g}-{self.family}"


#: The sizes every prototyping supplier stocks, in both families.
#:
#: THE GREEN DOUBLE-SIDED BOARD carries a printed A..Z / 01..NN legend, plated holes on
#: both faces, oblong finger pads down two of its edges and a screw hole in each corner.
#: THE ORANGE-BROWN PHENOLIC BOARD carries none of that: copper on the solder side only,
#: round pads everywhere, no legend, no fingers. They are different products, and a
#: preset that produced a bare grid for both would be describing neither.
STANDARD_PRESETS: tuple[BoardPreset, ...] = (
    BoardPreset("2 x 8 cm", 20.0, 80.0, 5, 30),
    BoardPreset("3 x 7 cm", 30.0, 70.0, 10, 24),
    BoardPreset("4 x 6 cm", 40.0, 60.0, 14, 20),
    BoardPreset("5 x 7 cm", 50.0, 70.0, 18, 24),
    BoardPreset("6 x 8 cm", 60.0, 80.0, 22, 30),
    BoardPreset("7 x 9 cm", 70.0, 90.0, 27, 35),
    BoardPreset("9 x 15 cm", 90.0, 150.0, 34, 58),
    BoardPreset("12 x 18 cm", 120.0, 180.0, 46, 70),
    BoardPreset("15 x 20 cm", 150.0, 200.0, 58, 78),
    BoardPreset("20 x 30 cm", 200.0, 300.0, 78, 118),
    BoardPreset("5 x 7 cm", 50.0, 70.0, 18, 26, family="single-sided-phenolic"),
    BoardPreset("7 x 9 cm", 70.0, 90.0, 27, 35, family="single-sided-phenolic"),
    BoardPreset("9 x 15 cm", 90.0, 150.0, 34, 58, family="single-sided-phenolic"),
    BoardPreset("15 x 20 cm", 150.0, 200.0, 58, 78, family="single-sided-phenolic"),
)

#: Bare substrate left outside a finger, for the legend printed there.
FINGER_INSET_MM: Mm = 1.5
#: An M2 clearance hole, which is what fits in these boards' corners.
CORNER_HOLE_MM: Mm = 2.2
#: Substrate a finger strip leaves between itself and a corner bore.
#:
#: Not merely "does not overlap". Trimming to first contact left the 5 x 7 preset with
#: 0.01 mm of board between the copper and the drill and the 4 x 6 with 0.09 mm, which is
#: not clearance -- a hole drilled that close to the edge of a pad in paper phenolic
#: breaks out into it. 0.3 mm is the smallest gap these boards are made with.
FINGER_BORE_CLEARANCE_MM: Mm = 0.3


def board_from_preset(preset: BoardPreset, base: Board) -> Board:
    """``base`` resized to a preset, with the border solved so the outline is exact.

    The border is whatever is left of the advertised size once the grid is taken out,
    halved. A negative result would mean the grid does not fit the board at all, so it is
    clamped -- a preset with a bad hole count then draws a flush-cut board rather than a
    board smaller than its own holes.
    """
    border_x = max(0.0, (preset.width_mm - preset.cols * base.pitch) / 2)
    border_y = max(0.0, (preset.height_mm - preset.rows * base.pitch) / 2)
    return dataclasses.replace(
        base,
        cols=preset.cols,
        rows=preset.rows,
        material=preset.material,
        single_sided=preset.single_sided,
        border_x_mm=round(border_x, 4),
        border_y_mm=round(border_y, 4),
        # BOTH families print their addresses on the substrate. The phenolic board was
        # given none at first, on the reasoning that it is the stripped-down product --
        # no fingers, no corner holes, copper on one face. That was wrong about the one
        # thing it is not stripped of: the orange pertinax boards carry the same
        # A..Z / 01..NN legend the green ones do, and it is the cheapest marking on the
        # board to apply. Without it the editor falls back to its own ruler, drawn OUTSIDE
        # the board in screen pixels, and the board on screen stops being the board in
        # your hand.
        labels=BoardLabels(row_digits=2),
        # The oblong pads on these boards are the EDGE STRIP, never the whole grid --
        # every interior pad is round. They come from `preset_edge_connectors`, not from
        # a board-wide pad shape.
        pad_shape="round",
        pad_length=None,
    )


def preset_strip_edges(board: Board) -> tuple[BoardEdge, ...]:
    """The two edges a preset board's oblong finger pads run along.

    THE SHORT EDGES OF THE BOARD, which is where they are on the real thing: a strip of
    contacts belongs across the narrow end, not down the length. On a 5 x 7 board that is
    the two 5 cm edges, so a portrait board gets them top and bottom.

    This used to pick the pair with the wider BORDER, on the reasoning that the border is
    where the room is. That happens to give the same answer on the green boards and the
    wrong one on the phenolic, whose borders are near enough equal (2.14 mm against
    1.98 mm) that a tenth of a millimetre decided which way the strips ran. A physical
    fact about the board should not turn on a rounding difference.
    """
    width, height = board_size_mm(board)
    return ("top", "bottom") if width <= height else ("left", "right")


def _finger_run_clear_of_bores(
    board: Board, edge: BoardEdge, mounts: tuple[MountingHole, ...]
) -> tuple[int, int]:
    """(start, count) for a finger strip that stops short of the corner screw holes.

    A bore removes copper. Run the strip the full width of the board and the corner holes
    are drilled through the end contacts of it -- measured at 0.21 mm of overlap on the
    2 x 8 and 6 x 8 presets, and clear on the 5 x 7 only by luck of the arithmetic. On
    the real boards the strip stops and the screw goes outside it.

    Trimmed rather than shifted: the strip is where it is, and what changes is how far
    along it runs.
    """
    span = board.rows if edge in ("left", "right") else board.cols
    probe = EdgeConnector(id="probe", edge=edge, start=0, count=span, inset_mm=FINGER_INSET_MM)
    clear: list[int] = []
    for index, hole in enumerate(edge_connector_holes(probe, board)):
        rect = edge_finger_rect(probe, hole, board)
        if not any(_bore_touches_rect(mount, rect, board) for mount in mounts):
            clear.append(index)
    if not clear:
        return 0, 0
    # One contiguous run: the obstructions are corner holes, so what is left is the
    # middle. Taking first..last rather than the individual indices keeps the model's
    # "a connector is a run of fingers" invariant.
    return clear[0], clear[-1] - clear[0] + 1


def _bore_touches_rect(mount: MountingHole, rect: RectMm, board: Board) -> bool:
    centre = mounting_hole_centre_mm(mount, board)
    near_x = min(max(centre.x, rect.x), rect.x + rect.width)
    near_y = min(max(centre.y, rect.y), rect.y + rect.height)
    return (
        math.hypot(centre.x - near_x, centre.y - near_y)
        < mount.diameter / 2 + FINGER_BORE_CLEARANCE_MM
    )


def preset_edge_connectors(preset: BoardPreset, board: Board) -> tuple[EdgeConnector, ...]:
    """The finger strips a preset's board is sold with.

    BOTH families have them, on their two short edges. The phenolic board was given none
    at all at first, on the reasoning that it is the stripped-down product -- and it is,
    but not of these.

    The run stops clear of the board's own corner screw holes, which is why this asks
    :func:`preset_mounting_holes` rather than leaving the two to be combined by a caller
    who has no way to know they interfere.
    """
    mounts = preset_mounting_holes(preset, board)
    runs = {edge: _finger_run_clear_of_bores(board, edge, mounts) for edge in preset_strip_edges(board)}
    return tuple(
        EdgeConnector(
            id=f"ec-{edge}",
            edge=edge,
            start=runs[edge][0],
            count=runs[edge][1],
            finger_width=round(min(2.0, board.pitch * 0.8), 3),
            inset_mm=FINGER_INSET_MM,
            # A single-sided board has copper on ONE face, and its fingers are no
            # exception: "both" put a strip of contacts on the bare phenolic side, where
            # the board has nothing but substrate.
            face="bottom" if board.single_sided else "both",
        )
        for edge in preset_strip_edges(board)
    )


def corner_hole_offset_mm(board: Board, diameter: Mm) -> Mm:
    """How far diagonally out of the grid a corner mounting hole should sit.

    Far enough that the bore misses the corner pad, near enough that it stays on the
    substrate. Zero when those two cannot both hold, which is the flush-cut case: the
    hole then has to go on the grid and eat the pads around it, and DRC says so.
    """
    extent_x, extent_y = pad_extent_mm(board)
    clears_pad = (max(extent_x, extent_y) / 2 + diameter / 2) / math.sqrt(2)
    stays_on_board = (
        min(board_edge_margin_mm(board, "horizontal"), board_edge_margin_mm(board, "vertical"))
        - diameter / 2
        - 0.2
    )
    return round(stays_on_board, 3) if stays_on_board >= clears_pad else 0.0


def preset_mounting_holes(preset: BoardPreset, board: Board) -> tuple[MountingHole, ...]:
    """A screw hole in each corner, in the BORDER, where these boards have them.

    Returns nothing when the border cannot take the bore -- on a flush-cut board there is
    nowhere for it to go, and a hole hanging off the edge is worse than no hole.
    """
    if preset.single_sided:
        return ()
    offset = corner_hole_offset_mm(board, CORNER_HOLE_MM)
    if offset <= 0:
        return ()
    corners = (
        (HoleCoord(0, 0), -1, -1),
        (HoleCoord(board.cols - 1, 0), 1, -1),
        (HoleCoord(0, board.rows - 1), -1, 1),
        (HoleCoord(board.cols - 1, board.rows - 1), 1, 1),
    )
    return tuple(
        MountingHole(
            id=f"mh-{index + 1}",
            at=at,
            offset_x_mm=sign_x * offset,
            offset_y_mm=sign_y * offset,
            diameter=CORNER_HOLE_MM,
            head_diameter=CORNER_HOLE_MM * 2,
        )
        for index, (at, sign_x, sign_y) in enumerate(corners)
    )


# ---------------------------------------------------------------------------
# Neighbours
# ---------------------------------------------------------------------------


def neighbors4(c: HoleCoord, board: Board) -> list[HoleCoord]:
    """Orthogonal neighbours in deterministic compass order N, E, S, W, clipped."""
    candidates = (
        HoleCoord(c.col, c.row - 1),
        HoleCoord(c.col + 1, c.row),
        HoleCoord(c.col, c.row + 1),
        HoleCoord(c.col - 1, c.row),
    )
    return [n for n in candidates if is_inside_board(n, board)]


def neighbors8(c: HoleCoord, board: Board) -> list[HoleCoord]:
    """The 4 orthogonal directions first, then the 4 diagonals, clipped to the board."""
    candidates = (
        HoleCoord(c.col, c.row - 1),
        HoleCoord(c.col + 1, c.row),
        HoleCoord(c.col, c.row + 1),
        HoleCoord(c.col - 1, c.row),
        HoleCoord(c.col + 1, c.row - 1),
        HoleCoord(c.col + 1, c.row + 1),
        HoleCoord(c.col - 1, c.row + 1),
        HoleCoord(c.col - 1, c.row - 1),
    )
    return [n for n in candidates if is_inside_board(n, board)]


# ---------------------------------------------------------------------------
# Distances and adjacency
# ---------------------------------------------------------------------------


def manhattan(a: HoleCoord, b: HoleCoord) -> int:
    return abs(a.col - b.col) + abs(a.row - b.row)


def chebyshev(a: HoleCoord, b: HoleCoord) -> int:
    return max(abs(a.col - b.col), abs(a.row - b.row))


def same_hole(a: HoleCoord, b: HoleCoord) -> bool:
    return a.col == b.col and a.row == b.row


def is_adjacent4(a: HoleCoord, b: HoleCoord) -> bool:
    return manhattan(a, b) == 1


def is_adjacent8(a: HoleCoord, b: HoleCoord) -> bool:
    return chebyshev(a, b) == 1


# ---------------------------------------------------------------------------
# Path validation and measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrthogonalChainResult:
    ok: bool
    index: int = 0
    reason: str = ""


def validate_orthogonal_chain(path: tuple[HoleCoord, ...]) -> OrthogonalChainResult:
    """Validate the solder-trace path invariant.

    Consecutive holes must be 4-neighbours, since solder cannot reliably span a
    diagonal gap. Also rejects paths shorter than 2 and paths revisiting a hole.

    Adjacency is checked BEFORE the revisit check, which matters: a path like
    A1 -> B1 -> A1 is orthogonally valid at every step and must be caught as a revisit,
    not misreported as a bad step.
    """
    if len(path) < 2:
        return OrthogonalChainResult(
            ok=False, index=0, reason=f"A trace path must contain at least 2 holes (got {len(path)})."
        )

    seen = {hole_key(path[0])}
    for i in range(1, len(path)):
        prev, cur = path[i - 1], path[i]
        if not is_adjacent4(prev, cur):
            return OrthogonalChainResult(
                ok=False,
                index=i,
                reason=(
                    f"Hole at index {i} ({hole_key(cur)}) is not 4-adjacent (orthogonal) to the "
                    f"previous hole ({hole_key(prev)}); solder traces cannot span a diagonal gap."
                ),
            )
        key = hole_key(cur)
        if key in seen:
            return OrthogonalChainResult(
                ok=False,
                index=i,
                reason=f"Hole at index {i} ({key}) revisits a hole already used earlier in the path.",
            )
        seen.add(key)
    return OrthogonalChainResult(ok=True)


# ---------------------------------------------------------------------------
# Which holes a straight run passes over
# ---------------------------------------------------------------------------


def js_round(x: float) -> int:
    """Replicates JavaScript's ``Math.round`` (round half towards +Infinity).

    Python's builtin ``round()`` uses round-half-to-even, which disagrees with JavaScript
    exactly at ``.5`` boundaries -- and :func:`holes_under_line`'s sampled ``t`` values land
    there often enough (any axis-aligned or 45-degree run) that the divergence is not
    academic. ``floor(x + 0.5)`` matches the ECMAScript specification's algorithm, and since
    both sides are IEEE-754 doubles doing the same ``+`` and ``floor``, it matches bit for bit.

    Lives here rather than in router.py because occupancy.py needs the same sampling, and two
    copies of a rounding rule this load-bearing would eventually drift apart.
    """
    return math.floor(x + 0.5)


def holes_under_line(from_: HoleCoord, to: HoleCoord) -> list[HoleCoord]:
    """Holes a straight run physically passes over, endpoints included.

    Sampled along the segment at a quarter of a pitch, which is dense enough that no hole on
    the line is missed. This is the PHYSICAL footprint of a wire, which is not the same as the
    two holes its ``path`` records: a wire contacts only its ends but lies across everything
    between them.
    """
    steps = max(abs(to.col - from_.col), abs(to.row - from_.row)) * 4
    seen: set[str] = set()
    result: list[HoleCoord] = []
    for i in range(steps + 1):
        t = 0.0 if steps == 0 else i / steps
        hole = HoleCoord(
            col=js_round(from_.col + (to.col - from_.col) * t),
            row=js_round(from_.row + (to.row - from_.row) * t),
        )
        key = hole_key(hole)
        if key not in seen:
            seen.add(key)
            result.append(hole)
    return result


# ---------------------------------------------------------------------------
# Do two runs of conductor physically cross?
# ---------------------------------------------------------------------------


def _orientation(a: HoleCoord, b: HoleCoord, c: HoleCoord) -> int:
    """Sign of the cross product (b-a) x (c-a): +1 left, -1 right, 0 collinear.

    Exact integer arithmetic. Hole coordinates are whole numbers, so there is no tolerance
    to choose and no near-miss to get wrong.
    """
    value = (b.col - a.col) * (c.row - a.row) - (b.row - a.row) * (c.col - a.col)
    return (value > 0) - (value < 0)


def _on_segment(a: HoleCoord, b: HoleCoord, point: HoleCoord) -> bool:
    """Is ``point`` on segment a-b, given it is already known to be collinear with it?"""
    return (
        min(a.col, b.col) <= point.col <= max(a.col, b.col)
        and min(a.row, b.row) <= point.row <= max(a.row, b.row)
    )


def segments_touch(a1: HoleCoord, a2: HoleCoord, b1: HoleCoord, b2: HoleCoord) -> bool:
    """True when two straight runs meet anywhere other than at a shared endpoint.

    This is what "crossing" means physically, and it is NOT the same as "sharing a hole".
    Two wires running diagonally across a board can cross in the middle of a cell, touching
    no common hole at all -- which is the ordinary case for point-to-point wiring, and was
    invisible to a check that only compared hole lists. A board can therefore be routed with
    two bare wires lying across each other and be reported clean.

    A shared ENDPOINT is not a crossing: that is a deliberate junction, two conductors
    meeting at a pad. Everything else counts, including a T (one run ending part-way along
    another) and collinear overlap, because in both the copper genuinely touches.
    """
    shared_endpoints = {(a1.col, a1.row), (a2.col, a2.row)} & {
        (b1.col, b1.row),
        (b2.col, b2.row),
    }
    if shared_endpoints:
        # Meeting at a pad is a junction. Two runs sharing BOTH endpoints are duplicates
        # lying on top of each other, which is a genuine overlap rather than a junction.
        if len(shared_endpoints) < 2:
            return False

    o1 = _orientation(a1, a2, b1)
    o2 = _orientation(a1, a2, b2)
    o3 = _orientation(b1, b2, a1)
    o4 = _orientation(b1, b2, a2)

    if o1 != o2 and o3 != o4:
        return True  # Proper crossing, or a T touching the interior of the other run.

    # Collinear and overlapping: same line, and at least one endpoint inside the other run.
    if o1 == o2 == o3 == o4 == 0:
        return (
            _on_segment(a1, a2, b1)
            or _on_segment(a1, a2, b2)
            or _on_segment(b1, b2, a1)
            or _on_segment(b1, b2, a2)
        )
    return False


def paths_cross(a: tuple[HoleCoord, ...], b: tuple[HoleCoord, ...]) -> HoleCoord | None:
    """The first segment pair of two paths that touch, reported as the crossing's start hole.

    Returns None when the two paths are geometrically clear of each other. A hole rather than
    a point because every message and every measurement step in this system is addressed by
    hole (PLAN.md Sec 4.1) -- naming the segment's start is what lets a violation say "near
    C7" instead of quoting a coordinate nobody can find on the board.
    """
    for index in range(len(a) - 1):
        for other in range(len(b) - 1):
            if segments_touch(a[index], a[index + 1], b[other], b[other + 1]):
                return a[index]
    return None


def path_length_mm(path: tuple[HoleCoord, ...], board: Board) -> Mm:
    """Sum of Euclidean segment lengths along a path, in mm."""
    total = 0.0
    for i in range(1, len(path)):
        p1 = hole_to_mm(path[i - 1], board)
        p2 = hole_to_mm(path[i], board)
        total += math.hypot(p2.x - p1.x, p2.y - p1.y)
    return total


# ---------------------------------------------------------------------------
# Component placement
# ---------------------------------------------------------------------------


def transform_offset(x0: float, y0: float, rotation: Rotation, mirrored: bool) -> tuple[float, float]:
    """Apply a component's placement transform to an offset, in any unit.

    Coordinate system is screen-like: col -> +x (right), row -> +y (down).

    Order matches how a physical part is placed: it is first flipped over (mirrored
    about the vertical axis through the anchor), THEN rotated.
      - Mirror: (x, y) -> (-x, y)
      - Rotation is clockwise; each 90-degree step maps (x, y) -> (-y, x).
        Check: (1, 0) "right" -> (0, 1) "down", correct for a y-down system.

    Pin offsets (grid steps) and body outlines (millimetres) rotate by the same rule,
    so both go through this one function.
    """
    x, y = (-x0 if mirrored else x0), y0
    for _ in range((rotation // 90) % 4):
        x, y = -y, x
    # Negating 0 gives -0.0, which is harmless arithmetically but trips strict equality
    # assertions. Normalise it so callers never observe it.
    return x + 0.0, y + 0.0


def transform_pin_offset(
    d_col: int, d_row: int, rotation: Rotation, mirrored: bool
) -> tuple[int, int]:
    """Grid-step variant of :func:`transform_offset`."""
    x, y = transform_offset(d_col, d_row, rotation, mirrored)
    return int(x), int(y)


def pin_hole(component: ComponentInstance, footprint: Footprint, pin_number: str) -> HoleCoord | None:
    """Absolute hole a given footprint pin lands on, or None if there is no such pin."""
    for pin in footprint.pins:
        if pin.number == pin_number:
            d_col, d_row = transform_pin_offset(
                pin.d_col, pin.d_row, component.rotation, component.mirrored
            )
            return HoleCoord(component.anchor.col + d_col, component.anchor.row + d_row)
    return None


def all_pin_holes(
    component: ComponentInstance, footprint: Footprint
) -> list[tuple[FootprintPin, HoleCoord]]:
    """Absolute holes for every pin of a footprint, placed by a component instance."""
    result: list[tuple[FootprintPin, HoleCoord]] = []
    for pin in footprint.pins:
        d_col, d_row = transform_pin_offset(
            pin.d_col, pin.d_row, component.rotation, component.mirrored
        )
        result.append(
            (pin, HoleCoord(component.anchor.col + d_col, component.anchor.row + d_row))
        )
    return result
