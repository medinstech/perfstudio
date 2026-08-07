"""Connectivity engine.

Determines what is ACTUALLY electrically connected on the board, as opposed to what the
schematic *intends* to be connected (see ``PerfDocument.nets``). This is the most
correctness-critical module in the project: DRC, LVS and the soldering guide's
continuity/isolation tests are all derived from its output.

Model: a union-find (disjoint set, path compression + union by rank) over nodes
identified by (hole, side), where side is 'top' | 'bottom'.

Connection semantics -- the crux of this module:

 a) Every component pin occupies a hole. Its lead passes through the board, so it
    creates nodes at that hole on BOTH sides and unions them together.

 b) For 'solder-trace', 'solder-trace-wired' and 'strip': EVERY hole along ``path`` is
    an electrical contact -- the conductor is soldered down at each pad it crosses. All
    consecutive holes on the conductor's side are unioned.

 c) For 'bare-wire', 'insulated-wire', 'top-jumper' and 'lead-bend': ONLY the two
    endpoints (path[0] and path[-1]) are soldered. Intermediate points are routing
    geometry, not electrical contacts -- a wire passing over a pad does not connect to
    it.

A node exists here only if something makes electrical contact at it: a component pin,
or a conductor contact point. The pads a wire merely passes over are deliberately NOT
registered. They are electrically indistinguishable from the thousands of empty pads on
the board, and registering them would emit a flood of meaningless single-node nets that
every consumer (LVS floating-conductor reporting in particular) would have to filter
back out. That a wire physically occupies a hole is a geometric fact and belongs to the
router's occupancy index (occupancy.py), not to the connectivity graph.

Pure and deterministic: no I/O, no clock, no randomness. Output ordering is fully
sorted so extraction is reproducible and diffable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise

from .geometry import all_pin_holes, hole_key
from .model import (
    BoardSide,
    ConductorId,
    Footprint,
    HoleCoord,
    PerfDocument,
    contacts_every_path_hole,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

#: Resolves a footprint id to its definition. Unknown ids are looked up a lot (once per
#: component per extraction), so callers typically hand in a dict.get-backed closure.
FootprintLookup = Callable[[str], Footprint | None]


@dataclass(frozen=True, slots=True, order=True)
class PhysicalNodeRef:
    hole: HoleCoord
    side: BoardSide


@dataclass(frozen=True, slots=True, order=True)
class PhysicalPinRef:
    component_ref: str
    pin: str


@dataclass(frozen=True, slots=True)
class PhysicalNet:
    #: Stable, derived from its lowest-sorted node -- not a counter.
    id: str
    nodes: tuple[PhysicalNodeRef, ...]
    pins: tuple[PhysicalPinRef, ...]
    conductor_ids: tuple[ConductorId, ...]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node_key(hole: HoleCoord, side: BoardSide) -> str:
    """String key for a (hole, side) node, used as the union-find element identity.

    Built on geometry.hole_key so there is exactly one hole-encoding in the codebase.
    """
    return f"{hole_key(hole)}@{side}"


def _node_sort_key(node: PhysicalNodeRef) -> tuple[int, int, str]:
    return (node.hole.col, node.hole.row, node.side)


def _pin_sort_key(pin: PhysicalPinRef) -> tuple[str, str]:
    return (pin.component_ref, pin.pin)


# ---------------------------------------------------------------------------
# Union-find (disjoint set): path compression + union by rank
# ---------------------------------------------------------------------------


class _DisjointSet:
    """Union-find over opaque string keys."""

    __slots__ = ("_parent", "_rank")

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def make_set(self, x: str) -> None:
        """Idempotent: registers ``x`` as its own singleton set if not already known."""
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0

    def find(self, x: str) -> str:
        self.make_set(x)

        root = x
        while True:
            parent = self._parent.get(root)
            if parent is None or parent == root:
                break
            root = parent

        # Path compression: re-point every visited node directly at the root.
        cur = x
        while cur != root:
            nxt = self._parent.get(cur)
            if nxt is None:
                break
            self._parent[cur] = root
            cur = nxt

        return root

    def union(self, a: str, b: str) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return

        rank_a = self._rank.get(ra, 0)
        rank_b = self._rank.get(rb, 0)

        if rank_a < rank_b:
            self._parent[ra] = rb
        elif rank_a > rank_b:
            self._parent[rb] = ra
        else:
            self._parent[rb] = ra
            self._rank[ra] = rank_a + 1


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Group:
    """Mutable accumulator for one union-find root, before sorting into a PhysicalNet."""

    nodes: list[PhysicalNodeRef] = field(default_factory=list)
    pins: list[PhysicalPinRef] = field(default_factory=list)
    conductor_ids: list[ConductorId] = field(default_factory=list)


def _net_id_for(lowest: PhysicalNodeRef) -> str:
    return f"net:{lowest.hole.col}:{lowest.hole.row}:{lowest.side}"


def extract_physical_nets(doc: PerfDocument, lookup: FootprintLookup) -> list[PhysicalNet]:
    """Every electrically-distinct island on the board, sorted deterministically."""
    ds = _DisjointSet()
    node_info: dict[str, PhysicalNodeRef] = {}

    def touch(hole: HoleCoord, side: BoardSide) -> str:
        key = _node_key(hole, side)
        if key not in node_info:
            node_info[key] = PhysicalNodeRef(hole=hole, side=side)
        ds.make_set(key)
        return key

    # --- Pass 1: component pins bridge top and bottom at their hole (rule a). ---
    pin_entries: list[tuple[PhysicalPinRef, str]] = []

    for component in doc.components:
        footprint = lookup(component.footprint_id)
        if footprint is None:
            continue  # Unknown footprint: skip silently, record nothing.

        for pin, hole in all_pin_holes(component, footprint):
            top_key = touch(hole, "top")
            bottom_key = touch(hole, "bottom")
            ds.union(top_key, bottom_key)
            pin_entries.append(
                (PhysicalPinRef(component_ref=component.ref, pin=pin.number), top_key)
            )

    # --- Pass 2: conductors (rules b and c). ---
    # Only CONTACT holes become nodes. For rule-b kinds that is every hole along the
    # path; for rule-c kinds it is the two endpoints only. Holes a rule-c conductor
    # merely passes over are not registered -- see the note in the module docstring.
    conductor_contact: dict[ConductorId, str] = {}

    for conductor in doc.conductors:
        path = conductor.path
        if len(path) == 0:
            continue

        contact: str | None

        if contacts_every_path_hole(conductor):
            keys = [touch(h, conductor.side) for h in path]
            for a, b in pairwise(keys):
                ds.union(a, b)
            contact = keys[0]
        else:
            first = path[0]
            last = path[-1]
            first_key = touch(first, conductor.side)
            last_key = touch(last, conductor.side)
            ds.union(first_key, last_key)
            contact = first_key

        if contact is not None:
            conductor_contact[conductor.id] = contact

    # --- Assemble groups by union-find root. ---
    groups: dict[str, _Group] = {}

    def group_for(root: str) -> _Group:
        g = groups.get(root)
        if g is None:
            g = _Group()
            groups[root] = g
        return g

    for key, info in node_info.items():
        group_for(ds.find(key)).nodes.append(info)
    for pin_ref, key in pin_entries:
        group_for(ds.find(key)).pins.append(pin_ref)
    for conductor_id, key in conductor_contact.items():
        group_for(ds.find(key)).conductor_ids.append(conductor_id)

    # --- Build sorted, deterministic output. ---
    with_lowest: list[tuple[PhysicalNet, PhysicalNodeRef]] = []

    for group in groups.values():
        nodes = sorted(group.nodes, key=_node_sort_key)
        if not nodes:
            continue  # Unreachable: a group always has >= 1 node.
        lowest = nodes[0]

        pins = sorted(group.pins, key=_pin_sort_key)
        conductor_ids = sorted(set(group.conductor_ids))

        net = PhysicalNet(
            id=_net_id_for(lowest),
            nodes=tuple(nodes),
            pins=tuple(pins),
            conductor_ids=tuple(conductor_ids),
        )
        with_lowest.append((net, lowest))

    with_lowest.sort(key=lambda entry: _node_sort_key(entry[1]))
    return [net for net, _lowest in with_lowest]


def net_of_pin(
    doc: PerfDocument, lookup: FootprintLookup, pin: PhysicalPinRef
) -> PhysicalNet | None:
    """The physical net containing a given pin, if any."""
    nets = extract_physical_nets(doc, lookup)
    for net in nets:
        if any(p == pin for p in net.pins):
            return net
    return None


def are_pins_connected(
    doc: PerfDocument,
    lookup: FootprintLookup,
    a: PhysicalPinRef,
    b: PhysicalPinRef,
) -> bool:
    """True iff the two pins end up in the same physical net."""
    nets = extract_physical_nets(doc, lookup)

    def net_of(pin: PhysicalPinRef) -> PhysicalNet | None:
        for net in nets:
            if any(p == pin for p in net.pins):
                return net
        return None

    net_a = net_of(a)
    net_b = net_of(b)
    return net_a is not None and net_b is not None and net_a.id == net_b.id
