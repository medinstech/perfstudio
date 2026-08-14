"""Stripboard: the board that arrives with its copper already joined up.

A pad-per-hole board is a grid of isolated islands and every connection is something you
ADD. Stripboard (Veroboard) is the opposite: whole rows of holes are already one strip of
copper, so the connections are already there and the design problem is deciding **where
to break them**. Everything else in this project is about adding copper; this module is
about copper that is subtracted.

THE PHYSICAL MODEL, and the one decision everything here rests on:

  **A cut destroys the copper AT a hole.** On a real board a track is broken with a spot
  face cutter or a drill bit twisted by hand in a hole, which takes the pad with it. So a
  cut is addressed by the hole it is made in (``TrackCut.at``), that hole has no copper
  afterwards, and the strip it was part of becomes two strips -- one either side.

  The alternative model, a cut BETWEEN two holes, describes a knife scored across the
  track. It is a real technique and it is not the one people use, because it is much
  harder to do reliably at 2.54 mm and much harder to inspect. Modelling it would also
  give a cut an ambiguous address, and every message in this application is addressed.

  A pin in a cut hole is therefore soldered to nothing -- which is a physical
  impossibility DRC reports, exactly as it reports a pin in a mounting bore.

WHAT A SEGMENT IS. One run of copper: the holes of one strip between two cuts, or between
a cut and the edge of the board. Two pins on the same segment are connected, and the
board did it, not the user. That is the whole of stripboard connectivity, and
``connectivity.py`` is where it is applied.

WHICH WAY THE STRIPS RUN is ``board.strip_axis``, and ``None`` means horizontal: the
stock is sold with the strips along the long side, and a board that declares nothing has
to behave like the board people actually buy rather than raising.

Pure and deterministic, like everything above ``ui/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .geometry import hole_key, is_inside_board
from .model import Board, HoleCoord, PerfDocument


@dataclass(frozen=True, slots=True)
class StripSegment:
    """One unbroken run of the board's own copper.

    ``index`` is the strip (the row for horizontal strips, the column for vertical ones)
    and ``ordinal`` counts segments along it from the top-left, so the pair identifies a
    segment without holding its holes -- which is what connectivity compares.
    """

    index: int
    ordinal: int
    holes: tuple[HoleCoord, ...]

    @property
    def id(self) -> tuple[int, int]:
        return (self.index, self.ordinal)

    @property
    def is_empty(self) -> bool:
        return not self.holes


def is_stripboard(board: Board) -> bool:
    return board.type == "stripboard"


def strip_axis(board: Board) -> Literal["horizontal", "vertical"]:
    """Which way the strips run. ``None`` is horizontal -- see the module docstring."""
    return board.strip_axis or "horizontal"


def cut_holes(doc: PerfDocument) -> frozenset[str]:
    """Holes whose copper has been drilled away, as ``geometry.hole_key`` strings.

    Empty on any board that is not stripboard, so a caller does not have to ask first: a
    pad-per-hole board with a stray cut in its file (hand-edited, or converted from a
    stripboard) has no tracks for a cut to mean anything to.
    """
    if not is_stripboard(doc.board):
        return frozenset()
    return frozenset(hole_key(cut.at) for cut in doc.cuts if is_inside_board(cut.at, doc.board))


def strip_index(board: Board, hole: HoleCoord) -> int:
    """Which strip a hole is on: its row for horizontal strips, its column for vertical."""
    return hole.row if strip_axis(board) == "horizontal" else hole.col


def position_along(board: Board, hole: HoleCoord) -> int:
    """How far along its own strip a hole sits: its column for horizontal strips, its row
    for vertical ones. The other half of ``strip_index``, and what orders the pins on a
    strip when the planner walks them looking for two nets sharing one."""
    return hole.col if strip_axis(board) == "horizontal" else hole.row


def _strip_length(board: Board) -> int:
    return board.cols if strip_axis(board) == "horizontal" else board.rows


def _strip_count(board: Board) -> int:
    return board.rows if strip_axis(board) == "horizontal" else board.cols


def _hole_at(board: Board, index: int, position: int) -> HoleCoord:
    if strip_axis(board) == "horizontal":
        return HoleCoord(position, index)
    return HoleCoord(index, position)


def segment_of(doc: PerfDocument, hole: HoleCoord) -> tuple[int, int] | None:
    """The segment a hole belongs to, or None if it belongs to none.

    None means one of three things and they are all "this hole is not joined to anything
    by the board": the board is not stripboard, the hole is off it, or the copper there
    has been cut away.

    The ordinal is the number of cuts before this hole along its own strip, which makes
    it stable under adding a cut somewhere else on the board -- and cheap, since it never
    walks the strip.
    """
    if not is_stripboard(doc.board) or not is_inside_board(hole, doc.board):
        return None
    cuts = cut_holes(doc)
    if hole_key(hole) in cuts:
        return None

    board = doc.board
    index = strip_index(board, hole)
    position = position_along(board, hole)
    before = sum(
        1
        for cut in doc.cuts
        if is_inside_board(cut.at, board)
        and strip_index(board, cut.at) == index
        and position_along(board, cut.at) < position
    )
    return (index, before)


def segments(doc: PerfDocument) -> tuple[StripSegment, ...]:
    """Every run of copper on the board, in reading order.

    For rendering and for the build guide. Connectivity does not use this -- it asks
    ``segment_of`` about the holes that actually carry something, because a 30 x 20 board
    has 600 holes and only a few dozen of them are soldered into.
    """
    if not is_stripboard(doc.board):
        return ()

    board = doc.board
    cuts = cut_holes(doc)
    out: list[StripSegment] = []
    for index in range(_strip_count(board)):
        ordinal = 0
        run: list[HoleCoord] = []
        for position in range(_strip_length(board)):
            hole = _hole_at(board, index, position)
            if hole_key(hole) in cuts:
                if run:
                    out.append(StripSegment(index, ordinal, tuple(run)))
                    run = []
                # A cut ends a segment whether or not one had started, so the ordinal
                # counts cuts rather than segments -- which is what segment_of computes
                # without walking, and the two have to agree.
                ordinal += 1
                continue
            run.append(hole)
        if run:
            out.append(StripSegment(index, ordinal, tuple(run)))
    return tuple(out)


def segment_holes(doc: PerfDocument, hole: HoleCoord) -> tuple[HoleCoord, ...]:
    """Every hole joined to this one by the board's own copper, including itself.

    Empty when the hole is cut, off the board, or the board has no strips.
    """
    id_ = segment_of(doc, hole)
    if id_ is None:
        return ()
    board = doc.board
    cuts = cut_holes(doc)
    index = strip_index(board, hole)
    position = position_along(board, hole)

    first = position
    while first - 1 >= 0 and hole_key(_hole_at(board, index, first - 1)) not in cuts:
        first -= 1
    last = position
    length = _strip_length(board)
    while last + 1 < length and hole_key(_hole_at(board, index, last + 1)) not in cuts:
        last += 1
    return tuple(_hole_at(board, index, p) for p in range(first, last + 1))


def joined_by_board(doc: PerfDocument, a: HoleCoord, b: HoleCoord) -> bool:
    """Whether the BOARD joins these two holes, before anything was soldered."""
    id_a = segment_of(doc, a)
    return id_a is not None and id_a == segment_of(doc, b)


def cut_between(
    doc: PerfDocument,
    a: HoleCoord,
    b: HoleCoord,
    occupied: frozenset[str] = frozenset(),
) -> HoleCoord | None:
    """A free hole on the strip between two holes, to break the copper joining them.

    The answer to "these two pins are on the same track and belong to different nets".
    Returns None when they are not on one segment (nothing to cut), or when every hole
    between them is occupied -- which is a real dead end on stripboard and the reason
    placement matters more here than on a pad-per-hole board.

    ``occupied`` is hole keys a drill may not go through, which the caller has already:
    ``occupancy.build_occupancy(...).occupied_holes()`` is the usual source. Passing
    nothing means "the board is empty", which is true of a board with nothing on it and
    the caller's problem otherwise -- this module deliberately knows nothing about
    footprints.

    The hole nearest the MIDDLE is preferred: a cut hard against a pin is the one most
    likely to lift the neighbouring pad when it is drilled, and the middle of the run is
    also where a later part is least likely to want to sit.
    """
    if not joined_by_board(doc, a, b) or a == b:
        return None

    board = doc.board
    index = strip_index(board, a)
    lo, hi = sorted((position_along(board, a), position_along(board, b)))
    if hi - lo < 2:
        return None  # Adjacent pins: there is no hole between them to drill out.

    middle = (lo + hi) / 2
    candidates = [
        _hole_at(board, index, position)
        for position in range(lo + 1, hi)
        if hole_key(_hole_at(board, index, position)) not in occupied
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda h: (abs(position_along(board, h) - middle), position_along(board, h)))
