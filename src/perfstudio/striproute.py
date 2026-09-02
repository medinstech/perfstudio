"""Routing a stripboard, which is a different problem from routing a perfboard.

``autoroute.py`` answers "what copper do I add". On stripboard the copper is already
there, in whole rows, so the question splits in two and the first half is subtraction:

  **WHERE MUST THE TRACK BE BROKEN?** Two pins of different nets sitting on one strip are
  shorted by the board itself, before anybody picks up an iron. Every such pair needs a
  cut between them, and if there is no free hole between them there is no cut to make --
  which is a real dead end, reported rather than routed around, because the fix is to
  move a part and that is the user's decision.

  **WHAT IS STILL NOT JOINED?** After the cuts, a net's pins sit in some number of
  separate islands. Each island has to be linked to the next, and on stripboard that is a
  wire link over the COMPONENT side: the solder side is one sheet of parallel copper, so
  anything laid across it there shorts every strip it crosses.

WHY THE CUTS AND THE LINKS ARE ONE COMMAND. They are one decision. A cut removes a
connection the board was providing and a link puts back the one the circuit wanted;
committing them separately would put a state on the undo stack that nobody designed --
a board cut apart with nothing linking it, or worse, linked with nothing cut, which is a
short. ``stripboard.apply`` takes both.

WHAT THIS DOES NOT DO. It does not move parts. On stripboard placement IS most of the
design -- pins that want to share a net want to share a row -- and an autorouter that
quietly rearranged the board would be answering a question nobody asked. It reports what
it could not separate and leaves the decision where it belongs. ``placer.py`` is the tool
for that half, and it reads the same rule this module refuses by: ``cannot-separate``
below and its ``strip_conflict`` term count the same pairs, out of
``stripboard.MIN_SEPARABLE_GAP``. Two answers to "can this board be wired" would mean an
optimiser packing boards this planner then declines to finish.

Pure and deterministic, like every planner above it: the same board plans the same way.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from itertools import pairwise

from .command import CommandContext, CommandError
from .commands import (
    AddConductorPayload,
    ApplyStripboardPlanPayload,
    NewConductor,
    NewWireConductor,
    add_conductor,
    create_document_id_generator,
)
from .connectivity import FootprintLookup, PhysicalPinRef, extract_physical_nets
from .geometry import all_pin_holes, format_hole, hole_key, manhattan
from .model import HoleCoord, NetId, PerfDocument, TrackCut
from .stripboard import (
    cut_between,
    is_stripboard,
    joined_by_board,
    position_along,
    strip_index,
)

# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedCut:
    at: HoleCoord
    #: The two pins the board was shorting together, named the way the guide names them.
    between: tuple[PhysicalPinRef, PhysicalPinRef]
    reason: str


@dataclass(frozen=True, slots=True)
class PlannedLink:
    net_id: NetId
    net_name: str
    from_hole: HoleCoord
    to_hole: HoleCoord
    holes: int


@dataclass(frozen=True, slots=True)
class StripProblem:
    """Something the planner could not do, with the reason and where.

    PLAN.md Sec 13 names the trap this exists to avoid: an autorouter that routes most of
    a board and leaves a handful of connections that are then impossible to finish by
    hand, without saying so. Everything unresolved comes back here.
    """

    code: str
    message: str
    holes: tuple[HoleCoord, ...] = ()


@dataclass(frozen=True, slots=True)
class StripboardPlan:
    """A proposed set of cuts and links. Nothing is committed until the caller says so."""

    document: PerfDocument
    cuts: tuple[TrackCut, ...]
    conductors: tuple[NewConductor, ...]
    planned_cuts: tuple[PlannedCut, ...]
    links: tuple[PlannedLink, ...]
    problems: tuple[StripProblem, ...]
    label: str

    @property
    def is_empty(self) -> bool:
        return not self.cuts and not self.conductors

    def payload(self) -> ApplyStripboardPlanPayload:
        """The one command that commits this plan, as a single undo step."""
        return ApplyStripboardPlanPayload(
            cuts=tuple(cut.at for cut in self.cuts),
            conductors=self.conductors,
            cut_ids=tuple(cut.id for cut in self.cuts),
            label=self.label,
        )


def describe_plan(plan: StripboardPlan) -> str:
    """One line, for a status bar or a confirmation dialog."""
    if plan.is_empty and not plan.problems:
        return "Nothing to do: every net is already right on this board."
    bits = [f"{len(plan.cuts)} cut(s)", f"{len(plan.links)} link(s)"]
    if plan.problems:
        bits.append(f"{len(plan.problems)} problem(s)")
    return ", ".join(bits)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_stripboard(
    doc: PerfDocument,
    lookup: FootprintLookup,
    only_net_ids: tuple[NetId, ...] | None = None,
) -> StripboardPlan:
    """Work out the cuts and links this board needs, without touching it.

    Cuts first and links second, and the order is not a style: a cut changes what is
    connected to what, so planning the links against the uncut board would link islands
    that are about to be separated and miss the ones the cuts create.
    """
    if not is_stripboard(doc.board):
        return StripboardPlan(
            document=doc,
            cuts=(),
            conductors=(),
            planned_cuts=(),
            links=(),
            problems=(
                StripProblem(
                    code="not-stripboard",
                    # States the fact rather than describing a replan that did not happen:
                    # this returns an EMPTY plan, so "autoroute plans it as a perfboard"
                    # read as a report on work nothing here did.
                    message=(
                        f"This board is {doc.board.type}, which has no tracks to cut or "
                        f"link; use autoroute on it."
                    ),
                ),
            ),
            label="",
        )

    wanted = _wanted_nets(doc, only_net_ids)
    pins = _pin_holes(doc, lookup)
    problems: list[StripProblem] = []

    working, cuts, planned_cuts, cut_problems = _plan_cuts(doc, pins, wanted)
    problems.extend(cut_problems)

    conductors, links, link_problems = _plan_links(working, lookup, pins, wanted)
    problems.extend(link_problems)

    ctx = CommandContext(next_id=create_document_id_generator(working))
    preview = working
    for spec in conductors:
        try:
            preview = add_conductor.apply(preview, AddConductorPayload(conductor=spec), ctx)
        except CommandError as err:  # pragma: no cover - a planner bug, reported not raised
            problems.append(StripProblem(code=err.code, message=err.message))

    return StripboardPlan(
        document=preview,
        cuts=cuts,
        conductors=tuple(conductors),
        planned_cuts=tuple(planned_cuts),
        links=tuple(links),
        problems=tuple(problems),
        label=f"Autoroute stripboard: {len(cuts)} cut(s), {len(conductors)} link(s)",
    )


def _wanted_nets(doc: PerfDocument, only_net_ids: tuple[NetId, ...] | None) -> dict[NetId, str]:
    ids = set(only_net_ids) if only_net_ids is not None else None
    return {net.id: net.name for net in doc.nets if ids is None or net.id in ids}


def _pin_holes(doc: PerfDocument, lookup: FootprintLookup) -> dict[str, tuple[PhysicalPinRef, HoleCoord]]:
    """Every pin on the board by hole key, so a hole can name what is standing in it.

    Two pins in one hole is a document DRC objects to and this planner cannot fix, so the
    first in document order wins -- the same rule the 2D scene uses for the same reason.
    """
    out: dict[str, tuple[PhysicalPinRef, HoleCoord]] = {}
    for component in doc.components:
        footprint = lookup(component.footprint_id)
        if footprint is None:
            continue
        for pin, hole in all_pin_holes(component, footprint):
            out.setdefault(hole_key(hole), (PhysicalPinRef(component.ref, pin.number), hole))
    return out


def _net_of_pin(doc: PerfDocument, wanted: dict[NetId, str]) -> dict[tuple[str, str], NetId]:
    return {
        (node.component_ref, node.pin): net.id
        for net in doc.nets
        if net.id in wanted
        for node in net.nodes
    }


def _plan_cuts(
    doc: PerfDocument,
    pins: dict[str, tuple[PhysicalPinRef, HoleCoord]],
    wanted: dict[NetId, str],
) -> tuple[PerfDocument, tuple[TrackCut, ...], list[PlannedCut], list[StripProblem]]:
    """Break every strip that is shorting two different nets together.

    Walks the pins along each strip in order and looks only at CONSECUTIVE pairs: cutting
    between neighbours is the only cut that separates anything, and a pass over every pair
    would propose the same cut several times over.

    A pin whose net nobody declared is left alone rather than treated as its own net --
    an undeclared pin is a gap in the schematic, and cutting the board because of one
    would be the tool acting on a guess.
    """
    net_of = _net_of_pin(doc, wanted)
    # A drill may not go through a hole that has anything soldered into it: a pin, or any
    # hole a conductor touches. Blunt on purpose -- the question is only "may a bit go
    # here", and the answer is no for all of them.
    occupied = frozenset(pins) | frozenset(
        hole_key(hole) for conductor in doc.conductors for hole in conductor.path
    )

    by_strip: dict[int, list[tuple[int, PhysicalPinRef, HoleCoord]]] = {}
    for pin_ref, hole in pins.values():
        if (pin_ref.component_ref, pin_ref.pin) not in net_of:
            continue
        index = strip_index(doc.board, hole)
        position = position_along(doc.board, hole)
        by_strip.setdefault(index, []).append((position, pin_ref, hole))

    working = doc
    cuts: list[TrackCut] = []
    planned: list[PlannedCut] = []
    problems: list[StripProblem] = []
    next_id = create_document_id_generator(doc)

    for index in sorted(by_strip):
        row = sorted(by_strip[index], key=lambda entry: (entry[0], entry[1]))
        for (_, first_ref, first_hole), (_, second_ref, second_hole) in pairwise(row):
            net_a = net_of[(first_ref.component_ref, first_ref.pin)]
            net_b = net_of[(second_ref.component_ref, second_ref.pin)]
            if net_a == net_b:
                continue
            if not joined_by_board(working, first_hole, second_hole):
                continue  # An earlier cut already separated them.

            at = cut_between(working, first_hole, second_hole, occupied)
            if at is None:
                problems.append(
                    StripProblem(
                        code="cannot-separate",
                        message=(
                            f"{first_ref.component_ref}.{first_ref.pin} ({wanted[net_a]}) and "
                            f"{second_ref.component_ref}.{second_ref.pin} ({wanted[net_b]}) share "
                            f"the strip through {format_hole(first_hole)} and there is no free "
                            f"hole between them to cut. Move one of the parts."
                        ),
                        holes=(first_hole, second_hole),
                    )
                )
                continue

            cut = TrackCut(id=next_id("cut"), at=at)
            cuts.append(cut)
            planned.append(
                PlannedCut(
                    at=at,
                    between=(first_ref, second_ref),
                    reason=(
                        f"{wanted[net_a]} and {wanted[net_b]} both sit on this strip; the board "
                        f"joins them until it is cut."
                    ),
                )
            )
            working = dataclasses.replace(working, cuts=(*working.cuts, cut))

    return working, tuple(cuts), planned, problems


def _plan_links(
    doc: PerfDocument,
    lookup: FootprintLookup,
    pins: dict[str, tuple[PhysicalPinRef, HoleCoord]],
    wanted: dict[NetId, str],
) -> tuple[list[NewConductor], list[PlannedLink], list[StripProblem]]:
    """Join up the islands each net is left in, nearest pair first.

    Islands come from the CONNECTIVITY engine rather than from the strips, so a link the
    user already fitted, a solder blob between two rows and a strip all count as the one
    thing they physically are -- something that already joins these pins. The alternative,
    grouping by strip, would propose a link parallel to every wire already on the board.
    """
    islands_on_board = extract_physical_nets(doc, lookup)
    island_of: dict[tuple[str, str], str] = {}
    for island in islands_on_board:
        for pin in island.pins:
            island_of[(pin.component_ref, pin.pin)] = island.id

    hole_of = {(ref.component_ref, ref.pin): hole for ref, hole in pins.values()}

    conductors: list[NewConductor] = []
    links: list[PlannedLink] = []
    problems: list[StripProblem] = []

    for net in doc.nets:
        if net.id not in wanted:
            continue
        islands: dict[str, list[HoleCoord]] = {}
        missing = 0
        for node in net.nodes:
            key = (node.component_ref, node.pin)
            hole = hole_of.get(key)
            island_id = island_of.get(key)
            if hole is None or island_id is None:
                missing += 1
                continue
            islands.setdefault(island_id, []).append(hole)
        if missing:
            problems.append(
                StripProblem(
                    code="pin-not-found",
                    message=(
                        f"{missing} pin(s) of {net.name} could not be found on the board: the "
                        f"part is not placed, its footprint is not in the library, or the "
                        f"netlist names a pin the footprint does not have. Nothing was linked "
                        f"to them."
                    ),
                )
            )
        if len(islands) < 2:
            continue

        # Nearest-pair merging: join the two closest islands, merge them, repeat. The
        # links a person would fit, in the order they would fit them, and short links
        # first means a later one is never made redundant by an earlier one.
        groups = [sorted(holes, key=lambda h: (h.col, h.row)) for _, holes in sorted(islands.items())]
        while len(groups) > 1:
            best: tuple[int, HoleCoord, HoleCoord, int, int] | None = None
            for i, left in enumerate(groups):
                for j, right in enumerate(groups[i + 1 :], start=i + 1):
                    for a in left:
                        for b in right:
                            distance = manhattan(a, b)
                            candidate = (distance, a, b, i, j)
                            if best is None or _link_key(candidate) < _link_key(best):
                                best = candidate
            assert best is not None  # len(groups) > 1 means at least one pair exists.
            distance, a, b, i, j = best
            conductors.append(
                NewWireConductor(
                    path=(a, b),
                    # A link goes over the COMPONENT side. The solder side of a stripboard
                    # is one sheet of parallel copper, so a wire laid there shorts every
                    # strip it crosses -- which is the one mistake this planner must never
                    # make on the user's behalf.
                    kind="top-jumper",
                    side="top",
                    net_id=net.id,
                )
            )
            links.append(
                PlannedLink(
                    net_id=net.id, net_name=net.name, from_hole=a, to_hole=b, holes=distance
                )
            )
            groups[i] = sorted(groups[i] + groups[j], key=lambda h: (h.col, h.row))
            groups.pop(j)

    return conductors, links, problems


def _link_key(candidate: tuple[int, HoleCoord, HoleCoord, int, int]) -> tuple[int, int, int, int, int]:
    """Distance first, then position: two links of equal length must be chosen between
    the same way on every run, or the plan stops being reproducible."""
    distance, a, b, _i, _j = candidate
    return (distance, a.col, a.row, b.col, b.row)
