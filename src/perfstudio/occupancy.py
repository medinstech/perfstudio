"""Occupancy index: what physically sits at each (hole, side).

This is the counterpart to connectivity.py, and the split between them is deliberate.
Connectivity answers "what is electrically joined" -- a wire contacts only its two
endpoints. Occupancy answers "what is physically in the way" -- that same wire runs
across every hole on its path, and the router may not lay a bare trace through them.

Keeping the two apart is what lets connectivity stay clean (no meaningless one-node nets
for holes a wire merely crosses) while the router still knows the board is full there.

Pure and deterministic: no I/O, no clock, no randomness.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .connectivity import FootprintLookup
from .geometry import all_pin_holes, hole_key, hole_to_mm, transform_offset
from .model import (
    BoardSide,
    ComponentId,
    ComponentInstance,
    ConductorId,
    ConductorKind,
    Footprint,
    HoleCoord,
    PerfDocument,
    is_crossing_blocked,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OccupyingPin:
    component_id: ComponentId
    component_ref: str
    pin: str


@dataclass(frozen=True, slots=True)
class OccupancyIndex:
    """Precomputed answer to "what is here" for every occupied (hole, side).

    Constructed once by :func:`build_occupancy`; queried through its methods rather
    than by reaching into the underlying maps, which stay private to this module's
    hole-key encoding.
    """

    _conductors_by_node: Mapping[str, tuple[ConductorId, ...]]
    _blocked_nodes: frozenset[str]
    _pins_by_hole: Mapping[str, OccupyingPin]
    _body_by_hole: Mapping[str, ComponentId]
    _sorted_holes: tuple[HoleCoord, ...]

    def conductors_at(self, hole: HoleCoord, side: BoardSide) -> tuple[ConductorId, ...]:
        """Conductors whose path runs across this hole on this side, contact or not."""
        return self._conductors_by_node.get(_node_key(hole, side), ())

    def pin_at(self, hole: HoleCoord) -> OccupyingPin | None:
        """The component pin in this hole, if any. A pin occupies both sides."""
        return self._pins_by_hole.get(hole_key(hole))

    def is_copper_blocked(self, hole: HoleCoord, side: BoardSide) -> bool:
        """True when a conductor that cannot be crossed already occupies this hole+side.

        A router laying a solder trace or bare wire must treat these as walls;
        insulated wire and top jumpers may pass over them.
        """
        return _node_key(hole, side) in self._blocked_nodes

    def body_covers(self, hole: HoleCoord) -> ComponentId | None:
        """Component whose body covers this hole on the component side, if any."""
        return self._body_by_hole.get(hole_key(hole))

    def occupied_holes(self) -> tuple[HoleCoord, ...]:
        """Every hole that has anything at all on it, for quick iteration."""
        return self._sorted_holes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node_key(hole: HoleCoord, side: BoardSide) -> str:
    return f"{hole_key(hole)}@{side}"


def build_occupancy(doc: PerfDocument, lookup: FootprintLookup) -> OccupancyIndex:
    conductors_by_node: dict[str, list[ConductorId]] = {}
    blocked_nodes: set[str] = set()
    pins_by_hole: dict[str, OccupyingPin] = {}
    body_by_hole: dict[str, ComponentId] = {}
    holes: dict[str, HoleCoord] = {}

    def remember(hole: HoleCoord) -> None:
        k = hole_key(hole)
        if k not in holes:
            holes[k] = hole

    # --- Component pins and bodies. ---
    for component in doc.components:
        footprint = lookup(component.footprint_id)
        if footprint is None:
            continue

        for pin, hole in all_pin_holes(component, footprint):
            remember(hole)
            pins_by_hole[hole_key(hole)] = OccupyingPin(
                component_id=component.id,
                component_ref=component.ref,
                pin=pin.number,
            )

        for hole in _body_footprint_holes(component, footprint, doc):
            remember(hole)
            body_by_hole[hole_key(hole)] = component.id

    # --- Conductors: every hole on the path is physically occupied, contact or not. ---
    #
    # KNOWN GAP, deliberately preserved -- see
    # tests/test_occupancy_golden.py::test_known_gap_occupancy_does_not_model_a_straight_run_geometrically.
    # A wire's `path` is its two ENDS, while the wire itself lies across every hole between
    # them, so those holes are missing from this index. The module docstring above describes
    # the swept behaviour, and only solder traces get it, because their paths already list
    # every hole.
    #
    # Filling it in here is the natural fix and it moves the golden output of this index, of
    # the router and of DRC all at once -- three differential suites against the TypeScript
    # engine. The router therefore handles it locally instead (router._trace_blocked_holes),
    # which fixes the routing defect without touching what this index promises. Widening it
    # properly is a decision to take on its own, not a side effect of a bug fix.
    for conductor in doc.conductors:
        blocks = is_crossing_blocked(conductor)
        for hole in conductor.path:
            remember(hole)
            k = _node_key(hole, conductor.side)
            bucket = conductors_by_node.get(k)
            if bucket is None:
                conductors_by_node[k] = [conductor.id]
            else:
                bucket.append(conductor.id)
            if blocks:
                blocked_nodes.add(k)

    sorted_holes = tuple(sorted(holes.values(), key=lambda h: (h.col, h.row)))

    return OccupancyIndex(
        _conductors_by_node=MappingProxyType(
            {k: tuple(v) for k, v in conductors_by_node.items()}
        ),
        _blocked_nodes=frozenset(blocked_nodes),
        _pins_by_hole=MappingProxyType(pins_by_hole),
        _body_by_hole=MappingProxyType(body_by_hole),
        _sorted_holes=sorted_holes,
    )


def _body_footprint_holes(
    component: ComponentInstance,
    footprint: Footprint,
    doc: PerfDocument,
) -> list[HoleCoord]:
    """Holes covered by a component's body outline on the component side.

    Uses the outline's axis-aligned bounding box, which is what DRC's overlap check
    uses too -- good enough for deciding whether a top-side jumper would have to run
    underneath a part.
    """
    if len(footprint.body_outline) == 0:
        return []

    anchor_mm = hole_to_mm(component.anchor, doc.board)
    min_x = math.inf
    max_x = -math.inf
    min_y = math.inf
    max_y = -math.inf
    for pt in footprint.body_outline:
        tx, ty = transform_offset(pt.x, pt.y, component.rotation, component.mirrored)
        x = anchor_mm.x + tx
        y = anchor_mm.y + ty
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y

    pitch = doc.board.pitch
    result: list[HoleCoord] = []
    for col in range(math.ceil(min_x / pitch), math.floor(max_x / pitch) + 1):
        for row in range(math.ceil(min_y / pitch), math.floor(max_y / pitch) + 1):
            if 0 <= col < doc.board.cols and 0 <= row < doc.board.rows:
                result.append(HoleCoord(col=col, row=row))
    return result


def can_cross_copper(kind: ConductorKind) -> bool:
    """Conductor kinds a router may lay over occupied copper."""
    return kind in ("insulated-wire", "top-jumper")
