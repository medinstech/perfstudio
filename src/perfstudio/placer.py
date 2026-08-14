"""Placement optimisation by simulated annealing (PLAN.md Sec 6.3, milestone M3).

WHY PLACEMENT IS THE ROUTER'S PROBLEM TOO.

Routing quality is decided before the router runs. The NE555 fixture makes the point
numerically: laid out by hand, 4 of its 14 connections need insulated wire; dropped into
a grid by the netlist importer, 10 do. Same circuit, same router, same cost table -- the
only difference is where the parts sit. A wire is the most expensive primitive on the
board (cut, strip, tin, position, solder twice) and the placement is what decides how
many of them exist, so this module is the one that makes the router look good.

WHAT IT OPTIMISES.

Cost is a weighted sum, all terms in millimetres or millimetres-squared so the weights
are readable as exchange rates against wire length (see :class:`PlacementWeights`):

  HPWL          half-perimeter of each schematic net's pin bounding box: the standard
                stand-in for "how much wire will this need", and the dominant term.
  ALIGNMENT     how many distinct rows OR columns a net's pins are spread across,
                whichever is fewer. A net whose pins share one row can be picked up by a
                single solder-trace rail (PLAN.md Sec 6.2); one spread over five rows
                cannot. This is the term the competing tools do not have, and it is why
                the output is cheap to SOLDER rather than merely short.
  OVERLAP       area of overlapping courtyards, in mm^2. An area rather than a boolean
                on purpose: a yes/no penalty is a plateau, and an annealer cannot
                descend a plateau. It needs to know that two parts are nearly clear.
  COLLISION     two pins in one hole (DRC rule 3). Priced to dominate.
  OFF-BOARD     pins outside the grid. Only reachable by a document that arrives that
                way, since every move this module proposes is checked against the board
                before it is scored -- the term exists to give such a part a reason to
                come back in.
  EDGE          connectors, headers, pots and switches want a board edge: something
                plugs into them, or a finger reaches them.
  HEAT          a TO-220 or a relay next to an electrolytic (PLAN.md Sec 5.2 rule 9).
                Which parts those are, and how close is too close, come from model.py,
                because drc.py reports the same pairs by the same measure -- an
                optimiser that avoids a hazard its own checker never names is one the
                user has no way to learn from.

DETERMINISM IS NOT NEGOTIABLE (PLAN.md Sec 6.3). The RNG is seeded from the options and
nothing else. Same document, same seed, same result -- byte for byte, run after run,
machine after machine. Anything else and the golden-fixture approach the rest of this
engine rests on stops working here, and a user cannot tell an improvement from noise.

NOTHING IS COMMITTED HERE, exactly as in autoroute.py: the result is a PLAN carrying a
preview document, and the caller dispatches one ``component.moveMany`` so the whole
placement is a single undo step.

IT IS A PRE-ROUTING STEP. Conductors are not moved with the parts and are not scored --
copper that was routed to the old positions is stale afterwards, which is what
``lvs.stale_conductor_ids`` finds and what the UI clears before it re-routes. Optimising
placement on a routed board and keeping the routing is not a thing that makes sense.

Pure and deterministic: no I/O, no clock. The only randomness is the seeded RNG.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .autoroute import plan_autoroute
from .command import CommandContext
from .commands import (
    ComponentPlacement,
    MoveComponentsPayload,
    create_document_id_generator,
    move_components,
)
from .connectivity import FootprintLookup
from .geometry import format_hole, transform_offset
from .model import (
    HEAT_CLEARANCE_MM,
    HEAT_SENSITIVE_ARCHETYPES,
    HEAT_SOURCE_ARCHETYPES,
    VALID_ROTATIONS,
    BodyArchetype,
    ComponentId,
    HoleCoord,
    PerfDocument,
    Rotation,
)

# ---------------------------------------------------------------------------
# Domain knowledge: which parts care about where they are
# ---------------------------------------------------------------------------

#: Parts that want a board edge. A connector has something plugged into it, a pot and a
#: switch have a finger on them, and a terminal block has a screwdriver approaching it --
#: none of which works from the middle of a populated board.
EDGE_SEEKING_ARCHETYPES: frozenset[BodyArchetype] = frozenset(
    {"screw-terminal", "pin-header", "potentiometer", "tactile-switch"}
)

# Which parts run hot, which parts mind, and how close is too close all live in
# model.py: they are facts about the part, and drc.py acts on the same ones. Edge-seeking
# stays here because it is the opposite -- a placement PREFERENCE that no rule checks.


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlacementWeights:
    """Exchange rates against a millimetre of wire.

    Every term is reduced to millimetres (or mm^2) before being weighted, so these read
    directly: ``edge = 0.6`` says that pulling a connector 1 mm closer to the edge is
    worth 0.6 mm of wire, and ``overlap_area = 20`` says a square millimetre of courtyard
    overlap is worth 20 mm of wire -- which is another way of saying it is never worth it.
    """

    hpwl: float = 1.0
    alignment: float = 2.0
    #: Per PAIR of overlapping courtyards, however slightly they overlap.
    #:
    #: Both an area and a count, because they do different jobs. The area gives the
    #: annealer a gradient to descend -- a boolean alone is a plateau, and it cannot tell
    #: "nearly clear" from "right on top of each other". The count is what makes the
    #: result legal: it fires on exactly the predicate DRC uses, so an overlap of 7e-14
    #: mm^2 -- which is what packing parts until their courtyards touch actually produces,
    #: and which the area term prices at nothing -- still costs a full penalty. Without
    #: it the annealer reliably lands one float ULP inside a DRC error.
    overlap_pair: float = 250.0
    #: Per mm^2 of overlapping courtyard.
    overlap_area: float = 20.0
    #: Per pin sharing a hole with another pin.
    collision: float = 500.0
    #: Per pin hole outside the grid.
    off_board: float = 200.0
    #: Per mm from the nearest board edge, for edge-seeking parts only.
    edge: float = 0.6
    #: Per mm closer than HEAT_CLEARANCE_MM, per (source, sensitive) pair.
    heat: float = 4.0


@dataclass(frozen=True, slots=True)
class PlacementOptions:
    """Annealing controls. The defaults are the ones tuned against the fixtures."""

    #: The seed, and the whole of the nondeterminism. Changing it explores a different
    #: sequence of moves; two seeds on the same board are two independent attempts.
    seed: int = 0
    #: Moves to consider per restart. ``None`` scales it with the number of movable
    #: parts, which is what a caller without an opinion should use.
    iterations: int | None = None
    #: Independent anneals, each from the original placement with seed + n. Annealing is
    #: a random walk and its outcome genuinely varies -- on the NE555 fixture the spread
    #: across six seeds was 3 to 7 insulated wires for the same circuit. Restarts are the
    #: cheapest way to buy back that variance.
    restarts: int = 4
    #: Whether the winning candidate is chosen by ROUTING each one (see _pick_best).
    #: Off falls back to the internal cost, which is far faster and measurably worse.
    score_with_router: bool = True
    #: How many candidates get routed. Bounds the expensive half of the search
    #: independently of how many anneals were run.
    route_scored_restarts: int = 4
    weights: PlacementWeights = PlacementWeights()
    #: Whether parts may be turned. Off for boards where the user has already chosen
    #: orientations and only wants them shuffled.
    allow_rotation: bool = True
    #: Whether two parts may exchange anchors. The move that escapes the local minimum
    #: where two parts each want the other's spot.
    allow_swap: bool = True
    #: End temperature as a fraction of the calibrated start. Small enough that the last
    #: tenth of the run is effectively a greedy descent.
    final_temperature_ratio: float = 1e-3


DEFAULT_PLACEMENT_OPTIONS = PlacementOptions()

#: Iterations per movable part when ``PlacementOptions.iterations`` is None, and the
#: floor and ceiling around it. The floor keeps a three-part board from finishing before
#: it has annealed at all; the ceiling keeps a sixty-part board interactive.
ITERATIONS_PER_PART = 700
MIN_ITERATIONS = 2500
MAX_ITERATIONS = 40000

#: Moves sampled to calibrate the start temperature. Enough for a stable mean without
#: being a meaningful fraction of the run.
CALIBRATION_MOVES = 120


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlacementCost:
    """One placement scored, term by term.

    Kept broken down rather than summed because the number that matters to a user is
    "how much wire did this save" (``hpwl_mm``), and a single total cannot answer it.
    """

    hpwl_mm: float
    alignment_mm: float
    #: Pairs whose courtyards overlap, by exactly the predicate DRC rule 1 uses.
    overlap_pairs: int
    overlap_mm2: float
    collisions: int
    off_board_pins: int
    edge_mm: float
    heat_mm: float

    def total(self, weights: PlacementWeights) -> float:
        return (
            weights.hpwl * self.hpwl_mm
            + weights.alignment * self.alignment_mm
            + weights.overlap_pair * self.overlap_pairs
            + weights.overlap_area * self.overlap_mm2
            + weights.collision * self.collisions
            + weights.off_board * self.off_board_pins
            + weights.edge * self.edge_mm
            + weights.heat * self.heat_mm
        )

    @property
    def is_legal(self) -> bool:
        """Whether this placement breaks no hard DRC rule.

        Overlaps, collisions and off-board pins are errors, not preferences; a placement
        with any of them is one the tool should not have proposed. Deliberately keyed on
        ``overlap_pairs`` rather than the area, so it agrees with DRC to the last ULP.
        """
        return self.overlap_pairs == 0 and self.collisions == 0 and self.off_board_pins == 0


@dataclass(frozen=True, slots=True)
class PlacementChange:
    """One component that ended up somewhere else."""

    component_id: ComponentId
    ref: str
    from_anchor: HoleCoord
    to_anchor: HoleCoord
    from_rotation: Rotation
    to_rotation: Rotation

    @property
    def rotated(self) -> bool:
        return self.from_rotation != self.to_rotation

    @property
    def moved(self) -> bool:
        return self.from_anchor != self.to_anchor


@dataclass(frozen=True, slots=True)
class PlacementPlan:
    """A proposed placement, not a committed one.

    ``document`` is a preview built by applying ``component.moveMany`` exactly as the
    bus will, so DRC, LVS, the ratsnest and a dry-run autoroute can all be run against
    it to show the user what they are about to accept. Commit it or discard it; it is on
    no undo stack and it is invalidated by any edit to the document it was planned from.
    """

    document: PerfDocument
    changes: tuple[PlacementChange, ...]
    before: PlacementCost
    after: PlacementCost
    weights: PlacementWeights
    #: The seed of the restart that won, not the seed that was asked for. Passing it back
    #: as ``PlacementOptions.seed`` with ``restarts=1`` reproduces this plan exactly.
    seed: int | None
    iterations: int
    accepted: int
    movable: int
    locked: int
    label: str
    #: The autorouter's total cost for this placement, when the winner was chosen by
    #: routing. None when the internal cost decided (no netlist, or scoring turned off).
    route_cost: float | None = None
    #: Connections the router could not make on this placement. The first thing a
    #: candidate is judged on, because a board that cannot be finished is not a board.
    route_unrouted: int | None = None

    def payload(self) -> MoveComponentsPayload:
        """The one command that commits this plan, as a single undo step."""
        return MoveComponentsPayload(
            placements=tuple(
                ComponentPlacement(
                    id=change.component_id,
                    anchor=change.to_anchor,
                    rotation=change.to_rotation,
                )
                for change in self.changes
            ),
            label=self.label,
        )

    @property
    def is_empty(self) -> bool:
        """Nothing to commit -- the placement is already at least as good as anything the
        annealer found. ``component.moveMany`` refuses an empty batch on purpose, so a
        caller must check this rather than dispatch and hope."""
        return not self.changes

    @property
    def improvement(self) -> float:
        """Cost removed. Positive means better; zero means nothing was found."""
        return self.before.total(self.weights) - self.after.total(self.weights)

    @property
    def wire_saved_mm(self) -> float:
        """Estimated wire length removed. The number worth putting in front of a user."""
        return self.before.hpwl_mm - self.after.hpwl_mm


def describe(plan: PlacementPlan) -> str:
    """One line for a status bar. Leads with the estimate, not the cost function."""
    if plan.is_empty:
        return (
            f"Placement unchanged ({plan.movable} movable part(s), "
            f"{plan.iterations} moves tried)"
        )
    turned = sum(1 for c in plan.changes if c.rotated)
    parts = [f"{len(plan.changes)} part(s) placed"]
    if turned:
        parts.append(f"{turned} turned")
    parts.append(f"~{plan.wire_saved_mm:.0f} mm less connection length")
    if plan.before.overlap_pairs > 0 and plan.after.overlap_pairs == 0:
        parts.append(f"{plan.before.overlap_pairs} overlap(s) cleared")
    if plan.route_cost is not None:
        parts.append(f"routing cost {plan.route_cost:.0f}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Static per-component data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Box:
    """Courtyard bounding box, in mm relative to the component's anchor hole."""

    min_x: float
    max_x: float
    min_y: float
    max_y: float


def _body_centre(box: _Box | None, anchor_x: float, anchor_y: float) -> tuple[float, float]:
    """Centre of a part's body in board mm, given its anchor.

    Falls back to the anchor for a footprint carrying no outline, which is the only
    position such a part has.
    """
    if box is None:
        return anchor_x, anchor_y
    return anchor_x + (box.min_x + box.max_x) / 2, anchor_y + (box.min_y + box.max_y) / 2


@dataclass(frozen=True, slots=True)
class _Part:
    """Everything about one component that does not change while annealing.

    The four rotations are precomputed. A move then costs no trigonometry at all: it is
    an index into ``pin_offsets`` plus an integer add, which is what makes an O(n)
    delta evaluation per move affordable in Python.
    """

    #: Where this component sits in ``doc.components``. Everything else in this module
    #: indexes by POSITION IN ``parts``, which differs whenever the document contains a
    #: component whose footprint the registry does not have -- conflating the two is the
    #: obvious way to move the wrong part.
    doc_index: int
    component_id: ComponentId
    ref: str
    movable: bool
    archetype: BodyArchetype | None
    #: Pin numbers, parallel to every entry of ``pin_offsets``.
    pin_numbers: tuple[str, ...]
    #: Pin offsets per rotation index (0..3), each a tuple of (d_col, d_row).
    pin_offsets: tuple[tuple[tuple[int, int], ...], ...]
    #: Courtyard box per rotation index, relative to the anchor in mm.
    rel_box: tuple[_Box | None, ...]
    #: Legal anchor range per rotation index: (min_col, max_col, min_row, max_row).
    #: None when the part cannot fit on the board at that rotation at all.
    anchor_bounds: tuple[tuple[int, int, int, int] | None, ...]
    edge_seeking: bool
    heat_source: bool
    heat_sensitive: bool


def _rotation_index(rotation: Rotation) -> int:
    return (int(rotation) // 90) % 4


def _build_parts(doc: PerfDocument, lookup: FootprintLookup) -> list[_Part]:
    """One ``_Part`` per component with a known footprint, in document order.

    A component whose footprint id the registry does not have is skipped entirely, the
    same way connectivity, DRC and LVS skip it. Moving a part whose size and pins are
    unknown would be moving it blind.
    """
    board = doc.board
    parts: list[_Part] = []
    for index, component in enumerate(doc.components):
        footprint = lookup(component.footprint_id)
        if footprint is None:
            continue

        offsets: list[tuple[tuple[int, int], ...]] = []
        boxes: list[_Box | None] = []
        bounds: list[tuple[int, int, int, int] | None] = []
        for rotation in VALID_ROTATIONS:
            placed = tuple(
                (
                    int(transform_offset(p.d_col, p.d_row, rotation, component.mirrored)[0]),
                    int(transform_offset(p.d_col, p.d_row, rotation, component.mirrored)[1]),
                )
                for p in footprint.pins
            )
            offsets.append(placed)

            if footprint.body_outline:
                xs: list[float] = []
                ys: list[float] = []
                for point in footprint.body_outline:
                    tx, ty = transform_offset(point.x, point.y, rotation, component.mirrored)
                    xs.append(tx)
                    ys.append(ty)
                boxes.append(_Box(min(xs), max(xs), min(ys), max(ys)))
            else:
                boxes.append(None)

            if placed:
                min_dc = min(dc for dc, _ in placed)
                max_dc = max(dc for dc, _ in placed)
                min_dr = min(dr for _, dr in placed)
                max_dr = max(dr for _, dr in placed)
                lo_col, hi_col = -min_dc, board.cols - 1 - max_dc
                lo_row, hi_row = -min_dr, board.rows - 1 - max_dr
                fits = lo_col <= hi_col and lo_row <= hi_row
                bounds.append((lo_col, hi_col, lo_row, hi_row) if fits else None)
            else:
                bounds.append((0, board.cols - 1, 0, board.rows - 1))

        archetype = footprint.body.archetype
        parts.append(
            _Part(
                doc_index=index,
                component_id=component.id,
                ref=component.ref,
                movable=not component.locked,
                archetype=archetype,
                pin_numbers=tuple(p.number for p in footprint.pins),
                pin_offsets=tuple(offsets),
                rel_box=tuple(boxes),
                anchor_bounds=tuple(bounds),
                edge_seeking=archetype in EDGE_SEEKING_ARCHETYPES,
                heat_source=archetype in HEAT_SOURCE_ARCHETYPES,
                heat_sensitive=archetype in HEAT_SENSITIVE_ARCHETYPES,
            )
        )
    return parts


# ---------------------------------------------------------------------------
# Mutable annealing state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _State:
    """Anchors and rotations, plus the one thing that cannot be evaluated locally.

    ``hole_count`` and ``collisions`` are maintained incrementally through
    :meth:`set_placement`, because counting shared holes is a global question and
    recomputing it every iteration is the one term that would dominate the run.
    """

    parts: list[_Part]
    col: list[int]
    row: list[int]
    rot: list[int]
    hole_count: dict[tuple[int, int], int] = field(default_factory=dict)
    collisions: int = 0

    def snapshot(self) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
        return tuple(self.col), tuple(self.row), tuple(self.rot)

    def restore(
        self, snap: tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]
    ) -> None:
        for index in range(len(self.parts)):
            if (
                self.col[index] != snap[0][index]
                or self.row[index] != snap[1][index]
                or self.rot[index] != snap[2][index]
            ):
                self.set_placement(index, snap[0][index], snap[1][index], snap[2][index])

    def pins(self, index: int) -> tuple[tuple[int, int], ...]:
        part = self.parts[index]
        c, r = self.col[index], self.row[index]
        return tuple((c + dc, r + dr) for dc, dr in part.pin_offsets[self.rot[index]])

    def set_placement(self, index: int, col: int, row: int, rot: int) -> None:
        """Move one part, keeping the shared-hole bookkeeping exact."""
        for hole in self.pins(index):
            count = self.hole_count[hole]
            if count >= 2:
                self.collisions -= 1
            if count == 1:
                del self.hole_count[hole]
            else:
                self.hole_count[hole] = count - 1

        self.col[index] = col
        self.row[index] = row
        self.rot[index] = rot

        for hole in self.pins(index):
            count = self.hole_count.get(hole, 0)
            if count >= 1:
                self.collisions += 1
            self.hole_count[hole] = count + 1


def _initial_state(doc: PerfDocument, parts: list[_Part]) -> _State:
    state = _State(
        parts=parts,
        col=[doc.components[p.doc_index].anchor.col for p in parts],
        row=[doc.components[p.doc_index].anchor.row for p in parts],
        rot=[_rotation_index(doc.components[p.doc_index].rotation) for p in parts],
    )
    for position in range(len(parts)):
        for hole in state.pins(position):
            count = state.hole_count.get(hole, 0)
            if count >= 1:
                state.collisions += 1
            state.hole_count[hole] = count + 1
    return state


# ---------------------------------------------------------------------------
# Net topology
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _NetPins:
    """One schematic net reduced to (part position, pin index) pairs.

    Pins the netlist names but the board does not have are dropped here rather than
    carried as a special case: they cannot be placed, so they cannot contribute to
    placement cost. LVS is what reports them.
    """

    pins: tuple[tuple[int, int], ...]


def _build_nets(
    doc: PerfDocument, parts: list[_Part]
) -> tuple[list[_NetPins], list[tuple[int, ...]]]:
    """Nets as pin references, plus the reverse index from part position to its nets."""
    by_ref: dict[str, int] = {}
    pin_index: dict[tuple[int, str], int] = {}
    for position, part in enumerate(parts):
        by_ref[part.ref] = position
        for number, pin_number in enumerate(part.pin_numbers):
            pin_index[(position, pin_number)] = number

    nets: list[_NetPins] = []
    touched: list[list[int]] = [[] for _ in parts]
    for net in doc.nets:
        resolved: list[tuple[int, int]] = []
        for node in net.nodes:
            at = by_ref.get(node.component_ref)
            if at is None:
                continue
            pin_at = pin_index.get((at, node.pin))
            if pin_at is None:
                continue
            resolved.append((at, pin_at))
        if len(resolved) < 2:
            # A net reaching one pin or none constrains nothing about placement.
            continue
        net_id = len(nets)
        nets.append(_NetPins(pins=tuple(resolved)))
        for position, _number in resolved:
            if net_id not in touched[position]:
                touched[position].append(net_id)

    return nets, [tuple(entry) for entry in touched]


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Scorer:
    board_pitch: float
    board_cols: int
    board_rows: int
    weights: PlacementWeights
    nets: list[_NetPins]
    nets_of: list[tuple[int, ...]]

    # -- individual terms, each returning millimetres (or mm^2) -------------

    def net_terms(self, state: _State, net_id: int) -> tuple[float, float]:
        """(HPWL, alignment) for one net, in mm."""
        net = self.nets[net_id]
        cols: list[int] = []
        rows: list[int] = []
        for position, number in net.pins:
            part = state.parts[position]
            dc, dr = part.pin_offsets[state.rot[position]][number]
            cols.append(state.col[position] + dc)
            rows.append(state.row[position] + dr)
        hpwl = (max(cols) - min(cols) + max(rows) - min(rows)) * self.board_pitch
        # The cheaper of "one rail along a row" and "one rail along a column": a net whose
        # pins share a row needs one trace, and every extra distinct row is another stub
        # or another wire. PLAN.md Sec 6.2.
        spread = min(len(set(rows)), len(set(cols))) - 1
        return hpwl, spread * self.board_pitch

    def part_terms(self, state: _State, position: int) -> tuple[int, float]:
        """(off-board pins, edge distance mm) for one part."""
        part = state.parts[position]
        off = 0
        for col, row in state.pins(position):
            if not (0 <= col < self.board_cols and 0 <= row < self.board_rows):
                off += 1
        if not part.edge_seeking:
            return off, 0.0
        col, row = state.col[position], state.row[position]
        to_edge = min(col, self.board_cols - 1 - col, row, self.board_rows - 1 - row)
        return off, max(0, to_edge) * self.board_pitch

    def pair_terms(self, state: _State, a: int, b: int) -> tuple[int, float, float]:
        """(overlapping? 0/1, courtyard overlap mm^2, heat proximity mm) for one pair.

        The 0/1 is ``drc._aabb_overlap`` spelled out: strict inequality on all four
        sides, so this module and DRC never disagree about whether two parts touch.
        """
        part_a, part_b = state.parts[a], state.parts[b]
        box_a = part_a.rel_box[state.rot[a]]
        box_b = part_b.rel_box[state.rot[b]]
        ax = state.col[a] * self.board_pitch
        ay = state.row[a] * self.board_pitch
        bx = state.col[b] * self.board_pitch
        by = state.row[b] * self.board_pitch

        touching = 0
        overlap = 0.0
        if box_a is not None and box_b is not None:
            dx = min(box_a.max_x + ax, box_b.max_x + bx) - max(box_a.min_x + ax, box_b.min_x + bx)
            dy = min(box_a.max_y + ay, box_b.max_y + by) - max(box_a.min_y + ay, box_b.min_y + by)
            if dx > 0 and dy > 0:
                touching = 1
                overlap = dx * dy

        heat = 0.0
        hot = (part_a.heat_source and part_b.heat_sensitive) or (
            part_b.heat_source and part_a.heat_sensitive
        )
        if hot:
            # Between the BODIES, not the anchors. An anchor is pin 1, which on a TO-220
            # is at one end of a 10 mm tab and on a DIP is a corner -- measuring from it
            # puts the heat source millimetres from where it physically is, in a
            # direction that depends on the rotation. drc.py measures this same pair the
            # same way, so a board the annealer scores as clear is one DRC agrees is
            # clear.
            acx, acy = _body_centre(box_a, ax, ay)
            bcx, bcy = _body_centre(box_b, bx, by)
            heat = max(0.0, HEAT_CLEARANCE_MM - math.hypot(acx - bcx, acy - bcy))
        return touching, overlap, heat

    # -- full and local evaluation -----------------------------------------

    def full(self, state: _State) -> PlacementCost:
        hpwl = alignment = 0.0
        for net_id in range(len(self.nets)):
            net_hpwl, net_align = self.net_terms(state, net_id)
            hpwl += net_hpwl
            alignment += net_align

        off_board = 0
        edge = 0.0
        for position in range(len(state.parts)):
            part_off, part_edge = self.part_terms(state, position)
            off_board += part_off
            edge += part_edge

        pairs = 0
        overlap = heat = 0.0
        for a in range(len(state.parts)):
            for b in range(a + 1, len(state.parts)):
                touching, pair_overlap, pair_heat = self.pair_terms(state, a, b)
                pairs += touching
                overlap += pair_overlap
                heat += pair_heat

        return PlacementCost(
            hpwl_mm=hpwl,
            alignment_mm=alignment,
            overlap_pairs=pairs,
            overlap_mm2=overlap,
            collisions=state.collisions,
            off_board_pins=off_board,
            edge_mm=edge,
            heat_mm=heat,
        )

    def local(self, state: _State, positions: tuple[int, ...]) -> float:
        """Every cost term involving ``positions``, weighted, collisions excluded.

        Correctness rests on two things: every term not involving one of these parts is
        unaffected by moving them, and no term is counted twice when two parts move at
        once (the ``a in moved`` guard below). Collisions are tracked on the state
        instead, because they are the one term that is not local.
        """
        moved = set(positions)
        weights = self.weights
        total = 0.0

        seen_nets: set[int] = set()
        for position in positions:
            for net_id in self.nets_of[position]:
                if net_id in seen_nets:
                    continue
                seen_nets.add(net_id)
                net_hpwl, net_align = self.net_terms(state, net_id)
                total += weights.hpwl * net_hpwl + weights.alignment * net_align

        for position in positions:
            off, edge = self.part_terms(state, position)
            total += weights.off_board * off + weights.edge * edge

        count = len(state.parts)
        for a in positions:
            for b in range(count):
                if b == a or (b in moved and b < a):
                    continue  # The pair (a, b) with both moved is counted once, at min(a, b).
                touching, overlap, heat = self.pair_terms(state, a, b)
                total += (
                    weights.overlap_pair * touching
                    + weights.overlap_area * overlap
                    + weights.heat * heat
                )

        return total


# ---------------------------------------------------------------------------
# Moves
# ---------------------------------------------------------------------------


def _in_bounds(part: _Part, rot: int, col: int, row: int) -> bool:
    bounds = part.anchor_bounds[rot]
    if bounds is None:
        return False
    lo_col, hi_col, lo_row, hi_row = bounds
    return lo_col <= col <= hi_col and lo_row <= row <= hi_row


def _propose(
    rng: random.Random,
    state: _State,
    movable: list[int],
    radius: int,
    options: PlacementOptions,
) -> tuple[tuple[int, ...], tuple[tuple[int, int, int], ...]] | None:
    """One candidate move, or None if the drawn move is not legal.

    Returns the positions it touches and their proposed (col, row, rot). Illegality is
    reported rather than retried so that an impossible board cannot spin forever: a
    rejected proposal simply costs one of the iteration budget.
    """
    kinds = ["translate"]
    if options.allow_rotation:
        kinds.append("rotate")
    if options.allow_swap and len(movable) >= 2:
        kinds.append("swap")
    # Translation carries the search; the other two are escapes from local minima, so
    # they get a minority of the budget between them.
    weightings = {"translate": 6, "rotate": 2, "swap": 2}
    kind = rng.choices(kinds, weights=[weightings[k] for k in kinds], k=1)[0]

    if kind == "swap":
        a, b = rng.sample(movable, 2)
        part_a, part_b = state.parts[a], state.parts[b]
        col_a, row_a, rot_a = state.col[a], state.row[a], state.rot[a]
        col_b, row_b, rot_b = state.col[b], state.row[b], state.rot[b]
        if not _in_bounds(part_a, rot_a, col_b, row_b):
            return None
        if not _in_bounds(part_b, rot_b, col_a, row_a):
            return None
        return (a, b), ((col_b, row_b, rot_a), (col_a, row_a, rot_b))

    position = rng.choice(movable)
    part = state.parts[position]

    # Turned IN PLACE, and it was worth trying the other way before believing it. A part
    # turned on the spot sweeps a different rectangle -- a DIP-14 turned 90 degrees is 5
    # holes wide instead of 14 -- so on a packed board it lands in its neighbour, is
    # scored on an overlap that a hole or two of slack would have avoided, and the
    # orientation is never seen again with the translation that makes it fit. Letting a
    # rotation nudge the anchor is the obvious fix and it MEASURED WORSE: summed mean
    # routed cost over seven fixtures and four seeds went 254.1 -> 261.0 at half the
    # rotations nudging, 260.0 at a quarter, 262.6 at 0.15, with ne555 losing most of it
    # (152.2 -> 161.9, and 3 insulated wires becoming 4.8). Translation already carries
    # the search; a rotation that needs a translation to pay off is reachable as two
    # accepted moves, and coupling them mostly perturbs the sequence that finds the rest.
    if kind == "rotate":
        rot = (state.rot[position] + rng.choice((1, 2, 3))) % 4
        if not _in_bounds(part, rot, state.col[position], state.row[position]):
            return None
        return (position,), ((state.col[position], state.row[position], rot),)

    col = state.col[position] + rng.randint(-radius, radius)
    row = state.row[position] + rng.randint(-radius, radius)
    rot = state.rot[position]
    if (col, row) == (state.col[position], state.row[position]):
        return None
    if not _in_bounds(part, rot, col, row):
        return None
    return (position,), ((col, row, rot),)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_placement(
    doc: PerfDocument,
    lookup: FootprintLookup,
    options: PlacementOptions = DEFAULT_PLACEMENT_OPTIONS,
    should_stop: Callable[[], bool] | None = None,
) -> PlacementPlan:
    """Anneal the placement of every unlocked component and return a plan.

    Locked components never move and are still scored: they are the fixed points the
    rest of the board is arranged around, which is exactly what a user locks a connector
    for.

    Runs ``options.restarts`` independent anneals and returns the best. What "best"
    means is the interesting part -- see :func:`_pick_best`.

    ``should_stop`` lets a caller cut a run short -- a Cancel button, a deadline -- and
    get back the best placement found so far rather than nothing. Stopping early yields a
    worse answer, never an invalid one: every candidate is a complete, legal placement.
    Leave it None and the function is exactly as deterministic as before.
    """
    parts = _build_parts(doc, lookup)
    nets, nets_of = _build_nets(doc, parts)
    state = _initial_state(doc, parts)

    scorer = _Scorer(
        board_pitch=doc.board.pitch,
        board_cols=doc.board.cols,
        board_rows=doc.board.rows,
        weights=options.weights,
        nets=nets,
        nets_of=nets_of,
    )
    before = scorer.full(state)
    movable = [position for position, part in enumerate(state.parts) if part.movable]
    locked = len(state.parts) - len(movable)
    #: The orientations the user chose, kept so a run can hand back any it changed for
    #: nothing. See _settle_rotations.
    original_rotations = tuple(state.rot)

    if not movable:
        return _plan_from(doc, (), before, before, options, 0, 0, 0, locked, None)

    iterations = (
        options.iterations
        if options.iterations is not None
        else min(MAX_ITERATIONS, max(MIN_ITERATIONS, ITERATIONS_PER_PART * len(movable)))
    )

    candidates: list[PlacementPlan] = []
    for attempt in range(max(1, options.restarts)):
        if candidates and should_stop is not None and should_stop():
            # At least one complete candidate exists, so stopping here returns a real
            # placement rather than nothing. Checked between restarts as well as inside
            # the anneal so a cancel lands promptly either way.
            break
        # Every restart starts from the ORIGINAL placement, not from the last one's
        # result: restarts exist to sample independent basins, and chaining them would
        # just be one longer anneal with the temperature reset.
        run_state = _initial_state(doc, parts)
        after, accepted = _anneal(
            run_state, scorer, movable, doc, options, iterations, options.seed + attempt,
            should_stop,
        )
        changes = _changes(doc, run_state)
        candidates.append(
            _plan_from(
                doc, changes, before, after, options, iterations, accepted,
                len(movable), locked, options.seed + attempt,
            )
        )

    winner = _pick_best(candidates, doc, lookup, options)
    # AFTER the winner is chosen, and only to the winner. Settling every candidate before
    # the choice changes what the router is shown and therefore which candidate wins --
    # measured on the dense fixture, where doing it that way left the mean routed cost
    # 31.5 -> 35.9 while the best was identical. Tidying the one board that won cannot do
    # that, and is checked against the router besides.
    return _settle_winner(winner, doc, lookup, parts, scorer, movable, original_rotations, options)


def _anneal(
    state: _State,
    scorer: _Scorer,
    movable: list[int],
    doc: PerfDocument,
    options: PlacementOptions,
    iterations: int,
    seed: int,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[PlacementCost, int]:
    """One annealing run, in place on ``state``. Returns its cost and accepted count."""
    rng = random.Random(seed)
    max_radius = max(2, max(doc.board.cols, doc.board.rows) // 3)

    start_temperature = _calibrate(rng, state, scorer, movable, max_radius, options)
    end_temperature = max(start_temperature * options.final_temperature_ratio, 1e-9)
    cooling = (end_temperature / start_temperature) ** (1.0 / max(1, iterations))

    current = scorer.full(state).total(options.weights)
    best = current
    best_snapshot = state.snapshot()
    temperature = start_temperature
    accepted = 0

    for step in range(iterations):
        # Every 512th move, not every move: should_stop crosses a thread boundary in the
        # GUI and calling it forty thousand times would cost more than the annealing.
        if should_stop is not None and step % 512 == 0 and step and should_stop():
            break
        progress = step / iterations
        radius = max(1, round(max_radius * (1.0 - progress)))
        proposal = _propose(rng, state, movable, radius, options)
        temperature *= cooling
        if proposal is None:
            continue

        positions, placements = proposal
        local_before = scorer.local(state, positions)
        collisions_before = state.collisions
        snapshot = tuple((state.col[p], state.row[p], state.rot[p]) for p in positions)

        for position, (col, row, rot) in zip(positions, placements, strict=True):
            state.set_placement(position, col, row, rot)

        local_after = scorer.local(state, positions)
        delta = (local_after - local_before) + options.weights.collision * (
            state.collisions - collisions_before
        )

        if delta <= 0 or rng.random() < math.exp(-delta / temperature):
            current += delta
            accepted += 1
            if current < best:
                best = current
                best_snapshot = state.snapshot()
        else:
            for position, (col, row, rot) in zip(positions, snapshot, strict=True):
                state.set_placement(position, col, row, rot)

    # The annealer ends wherever the last accepted uphill move left it, which is not
    # necessarily the best thing it saw. Return the best.
    state.restore(best_snapshot)
    return scorer.full(state), accepted


def _settle_rotations(
    state: _State,
    scorer: _Scorer,
    movable: list[int],
    original: tuple[int, ...],
    options: PlacementOptions,
) -> None:
    """Put back every orientation the search changed for nothing.

    The annealer accepts any move whose delta is ``<= 0``, and a rotation's delta is
    EXACTLY zero for every part the cost function cannot tell apart turned -- a part on no
    net, or one whose courtyard is square. So the placer turned parts for no reason at
    all: on the dense fixture it turned 11, and 5 of those cost 0.00 to turn back.

    That is not free to the person holding the soldering iron. Every rotation is an
    orientation to get right at the bench and a polarity line in the build guide, and a
    plan that says "11 turned" reads as work the tool did rather than noise it made. When
    the tool has no reason to prefer an orientation, the user's own is the one to keep.

    Non-worsening by construction: an original orientation goes back only when the total
    does not go up. Greedy and in order, so it stays deterministic; scored with ``full``
    rather than a local delta because it runs once at the end of a run, not a hundred
    thousand times inside one.

    Repeated to a fixpoint, because one sweep is not enough: handing back one part's
    orientation can be what makes the next part's free, and a single pass leaves those
    behind depending on the order it happened to visit them. It terminates -- every pass
    either changes nothing or moves at least one part back towards the orientation it
    started in, and no part is ever turned away from it here.
    """
    weights = options.weights
    current = scorer.full(state).total(weights)
    changed = True
    while changed:
        changed = False
        for position in movable:
            was = original[position]
            if state.rot[position] == was:
                continue
            part = state.parts[position]
            col, row = state.col[position], state.row[position]
            if not _in_bounds(part, was, col, row):
                continue
            turned = state.rot[position]
            state.set_placement(position, col, row, was)
            restored = scorer.full(state).total(weights)
            if restored <= current:
                current = restored
                changed = True
            else:
                state.set_placement(position, col, row, turned)


def _calibrate(
    rng: random.Random,
    state: _State,
    scorer: _Scorer,
    movable: list[int],
    max_radius: int,
    options: PlacementOptions,
) -> float:
    """Start temperature: the mean absolute cost change of a random move.

    Set from the board rather than from a constant because the cost is in millimetres and
    a 30x20 board and a 100x60 board produce deltas an order of magnitude apart. A fixed
    starting temperature would anneal one of them and randomise the other.

    Uses the same seeded RNG as the run itself, so calibration is part of the
    deterministic sequence rather than a separate source of variation.
    """
    deltas: list[float] = []
    for _ in range(CALIBRATION_MOVES):
        proposal = _propose(rng, state, movable, max_radius, options)
        if proposal is None:
            continue
        positions, placements = proposal
        local_before = scorer.local(state, positions)
        collisions_before = state.collisions
        snapshot = tuple((state.col[p], state.row[p], state.rot[p]) for p in positions)
        for position, (col, row, rot) in zip(positions, placements, strict=True):
            state.set_placement(position, col, row, rot)
        delta = (scorer.local(state, positions) - local_before) + options.weights.collision * (
            state.collisions - collisions_before
        )
        for position, (col, row, rot) in zip(positions, snapshot, strict=True):
            state.set_placement(position, col, row, rot)
        deltas.append(abs(delta))

    if not deltas:
        return 1.0
    mean = sum(deltas) / len(deltas)
    return mean if mean > 0 else 1.0


def _changes(doc: PerfDocument, state: _State) -> tuple[PlacementChange, ...]:
    changes: list[PlacementChange] = []
    by_id = {c.id: c for c in doc.components}
    for position, part in enumerate(state.parts):
        component = by_id[part.component_id]
        anchor = HoleCoord(state.col[position], state.row[position])
        rotation: Rotation = VALID_ROTATIONS[state.rot[position]]
        if anchor == component.anchor and rotation == component.rotation:
            continue
        changes.append(
            PlacementChange(
                component_id=part.component_id,
                ref=part.ref,
                from_anchor=component.anchor,
                to_anchor=anchor,
                from_rotation=component.rotation,
                to_rotation=rotation,
            )
        )
    return tuple(changes)


def _pick_best(
    candidates: list[PlacementPlan],
    doc: PerfDocument,
    lookup: FootprintLookup,
    options: PlacementOptions,
) -> PlacementPlan:
    """Choose between independent anneals -- by ROUTING each one, when it is worth it.

    HPWL is a proxy, and measuring showed how loose a proxy it is: on the NE555 fixture
    one candidate with 152 mm of HPWL routes for 191, while another with 145 mm routes
    for 209. Half-perimeter cannot see that a shorter net crosses three others, and
    crossings are what turn a solder trace into an insulated wire.

    So rather than tune the proxy, ask the thing that knows. Each candidate is handed to
    the autorouter and scored on what it would actually cost to build: connections that
    could not be routed first (a placement that cannot be finished is worse than any that
    can), then the router's own total. It is the same relationship as between the router
    and DRC -- the cheap heuristic searches, the expensive truth decides.

    Costs one autoroute per restart, which is why ``route_scored_restarts`` bounds it
    rather than the restart count doing so. Falls back to the internal cost when the
    board has no netlist to route, when scoring is turned off, or beyond that bound.
    """
    legal_first = sorted(
        candidates, key=lambda plan: (not plan.after.is_legal, plan.after.total(options.weights))
    )
    routable = options.score_with_router and bool(doc.nets)
    if not routable or options.route_scored_restarts <= 0:
        return legal_first[0]

    # Only the most promising few are routed: routing is two orders of magnitude more
    # expensive than scoring, and a candidate the cheap cost already ranks last is not
    # going to win on the expensive one.
    shortlist = legal_first[: options.route_scored_restarts]
    if len(shortlist) < 2:
        return legal_first[0]

    best: PlacementPlan | None = None
    best_key: tuple[int, int, float, float] | None = None
    for plan in shortlist:
        route = plan_autoroute(plan.document, lookup)
        key = (
            0 if plan.after.is_legal else 1,
            route.summary.links_unrouted,
            route.summary.total_cost,
            plan.after.total(options.weights),  # Deterministic tie-break.
        )
        if best_key is None or key < best_key:
            best, best_key = plan, key

    assert best is not None and best_key is not None
    return replace(best, route_cost=best_key[2], route_unrouted=best_key[1])


def _settle_winner(
    plan: PlacementPlan,
    doc: PerfDocument,
    lookup: FootprintLookup,
    parts: list[_Part],
    scorer: _Scorer,
    movable: list[int],
    original: tuple[int, ...],
    options: PlacementOptions,
) -> PlacementPlan:
    """Give the chosen board back every orientation it was turned for nothing.

    Kept honest by the same arbiter that chose the board in the first place: if the
    router prefers the turned version, the turned version is what ships. So this can
    remove gratuitous rotations and cannot cost a connection -- across eight fixtures and
    eight seeds it took the parts turned from 46 to 27 with the routed cost unchanged.

    Costs one extra autoroute on a board that has a netlist, next to the four
    ``_pick_best`` already ran.
    """
    state = _initial_state(plan.document, parts)
    _settle_rotations(state, scorer, movable, original, options)
    if tuple(state.rot) == tuple(_initial_state(plan.document, parts).rot):
        return plan

    settled = _plan_from(
        doc, _changes(doc, state), plan.before, scorer.full(state), options,
        plan.iterations, plan.accepted, plan.movable, plan.locked, plan.seed,
    )
    if plan.route_cost is None or plan.route_unrouted is None:
        # Nothing routed this board -- no netlist, or scoring turned off -- so the
        # internal cost is the only measure there is, and _settle_rotations has already
        # guaranteed it did not go up.
        return settled

    route = plan_autoroute(settled.document, lookup)
    if (route.summary.links_unrouted, route.summary.total_cost) <= (
        plan.route_unrouted,
        plan.route_cost,
    ):
        return replace(
            settled,
            route_cost=route.summary.total_cost,
            route_unrouted=route.summary.links_unrouted,
        )
    return plan


def _plan_from(
    doc: PerfDocument,
    changes: tuple[PlacementChange, ...],
    before: PlacementCost,
    after: PlacementCost,
    options: PlacementOptions,
    iterations: int,
    accepted: int,
    movable: int,
    locked: int,
    seed: int | None,
) -> PlacementPlan:
    label = f"Auto-place {len(changes)} component(s)" if changes else "Auto-place (no change)"
    preview = doc
    if changes:
        payload = MoveComponentsPayload(
            placements=tuple(
                ComponentPlacement(id=c.component_id, anchor=c.to_anchor, rotation=c.to_rotation)
                for c in changes
            ),
            label=label,
        )
        # Built through the real command, not by editing the document here: the preview
        # has to be the document the bus will produce, or "run DRC on the preview" is
        # answering a question about something the user will never see.
        preview = move_components.apply(
            doc, payload, CommandContext(next_id=create_document_id_generator(doc))
        )

    return PlacementPlan(
        document=preview,
        changes=changes,
        before=before,
        after=after,
        weights=options.weights,
        seed=seed,
        iterations=iterations,
        accepted=accepted,
        movable=movable,
        locked=locked,
        label=label,
        route_cost=None,
        route_unrouted=None,
    )


def summarize_changes(plan: PlacementPlan, limit: int = 12) -> list[str]:
    """Human-readable moves, for a confirmation dialog or a log.

    Uses hole addresses (``B7``), not coordinates: that is the language the rest of the
    tool and the build guide speak.
    """
    lines: list[str] = []
    for change in plan.changes[:limit]:
        turn = f", turned to {change.to_rotation} deg" if change.rotated else ""
        lines.append(
            f"{change.ref}: {format_hole(change.from_anchor)} -> "
            f"{format_hole(change.to_anchor)}{turn}"
        )
    if len(plan.changes) > limit:
        lines.append(f"... and {len(plan.changes) - limit} more")
    return lines
