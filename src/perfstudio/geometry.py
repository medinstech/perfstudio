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

import math
import re
from dataclasses import dataclass

from .model import (
    Board,
    ComponentInstance,
    Footprint,
    FootprintPin,
    HoleCoord,
    HoleRef,
    Mm,
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

    Must not be recomputed anywhere else. The 1:1-scale printable sheet, the 3D
    substrate and the 2D renderer have to agree to the last tenth of a millimetre,
    because the user tapes the printout onto the physical board.
    """
    return board.cols * board.pitch, board.rows * board.pitch


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


def board_outline_mm(board: Board) -> RectMm:
    """Board outline as a rect in the same mm space as :func:`hole_to_mm`."""
    width, height = board_size_mm(board)
    half = board.pitch / 2
    return RectMm(-half, -half, width, height)


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
