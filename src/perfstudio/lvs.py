"""LVS -- Layout versus Schematic.

``doc.nets`` is the schematic's INTENT, imported from a netlist. ``extract_physical_nets``
(connectivity.py) is what the board ACTUALLY connects, derived purely from conductors and
component pins. LVS compares the two and answers the question a builder actually cares
about before reaching for a soldering iron: does this board implement my circuit, yes or
no -- and if not, exactly where does it diverge. Along with the connectivity engine and
the soldering-guide checklists this module drives, this comparison is one of the three
things that justify the whole project: a schematic on its own is only ever an intention,
and a board on its own carries no memory of what it was supposed to be. LVS is the only
place the two meet.

Failure classes fall out directly from comparing an intent graph to a reality graph:
  - OPEN                  -- the schematic says "one net", the board says "more than one".
  - SHORT                 -- the schematic says "more than one net", the board says "one".
    A ground-to-power short is flagged with an unmistakable message, since it is the
    single most important thing this tool can catch before power is ever applied.
  - FLOATING CONDUCTOR     -- the board has a connection the schematic never asked for.
  - UNPLACED COMPONENT / UNKNOWN FOOTPRINT -- the schematic refers to hardware the board
    doesn't have (yet), so nothing about its connectivity can be judged.
  - UNROUTED NET           -- every pin exists, but not a single wire or trace has been
    run between any of them; this is the common state right after a fresh netlist import
    and deserves a plainer, less alarming message than "open" -- unrouted nets must be
    reported explicitly, never silently dropped.

Pure and deterministic: no I/O, no clock, no randomness. Issue ordering is fully sorted
so results are reproducible and diffable across runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal

from .connectivity import FootprintLookup, PhysicalNet, PhysicalPinRef, extract_physical_nets

# format_hole, not coord_to_hole_ref: everything below is building a MESSAGE, and the
# strict encoder raises on a negative column by design. A board can legally hold a part
# whose pins fall outside it -- rotating a part near an edge does exactly that, and DRC's
# job is to report it -- so a strict encoder here means LVS crashes while describing the
# very defect it exists to describe. See the note on geometry.format_hole.
from .geometry import format_hole
from .model import ComponentInstance, ConductorId, Net, NetClass, NetId, NetNode, PerfDocument

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

type LvsIssueKind = Literal[
    "open",  # pins the schematic says are one net are split across >1 physical net
    "short",  # pins from >1 schematic net share one physical net
    "floating-conductor",  # a conductor in a physical net containing no pins at all
    "unplaced-component",  # a schematic net references a component ref not present on the board
    "unknown-footprint",  # a placed component whose footprint_id the lookup cannot resolve
    "unrouted-net",  # a schematic net whose pins are all present but none are connected
]


@dataclass(frozen=True, slots=True)
class LvsIssue:
    kind: LvsIssueKind
    message: str
    net_names: tuple[str, ...]
    pins: tuple[PhysicalPinRef, ...]
    physical_net_ids: tuple[str, ...]
    #: Only populated for 'floating-conductor' and 'short'; empty otherwise.
    conductor_ids: tuple[ConductorId, ...] = ()


@dataclass(frozen=True, slots=True)
class LvsSummary:
    schematic_nets: int
    physical_nets: int
    matched_nets: int
    #: Under-connected nets: 'open' AND 'unrouted-net' together, never just one.
    opens: int
    shorts: int


@dataclass(frozen=True, slots=True)
class LvsResult:
    ok: bool
    issues: tuple[LvsIssue, ...]
    summary: LvsSummary


@dataclass(frozen=True, slots=True)
class ContinuityCheck:
    a: PhysicalPinRef
    b: PhysicalPinRef
    net_name: str


@dataclass(frozen=True, slots=True)
class IsolationCheck:
    a: PhysicalPinRef
    b: PhysicalPinRef
    net_a: str
    net_b: str


# ---------------------------------------------------------------------------
# Small deterministic helpers.
#
# These are plain string/pin comparators local to LVS's own output ordering and
# messages -- not a re-implementation of the union-find or hole maths that live in
# connectivity.py / geometry.py, which this module only ever calls into.
# ---------------------------------------------------------------------------

#: Identity key for a (component_ref, pin) pair. A plain tuple is used rather than a
#: formatted string (as the original TypeScript does for its Map keys) because Python
#: tuples are natively hashable -- there is no need to manufacture a string identity.
type _PinKey = tuple[str, str]


def _pin_key(component_ref: str, pin: str) -> _PinKey:
    return (component_ref, pin)


def _format_pin_ref(p: PhysicalPinRef) -> str:
    return f"{p.component_ref}.{p.pin}"


def _to_pin_ref(node: NetNode) -> PhysicalPinRef:
    return PhysicalPinRef(component_ref=node.component_ref, pin=node.pin)


def _issue_sort_key(issue: LvsIssue) -> tuple[str, str, int, str, str]:
    """Sort order for the final issues list: kind, then first net name, then first pin.

    An issue with no pins (e.g. 'floating-conductor') sorts before one with pins in the
    same kind/net-name group -- the leading 0 acts as a sentinel that is always less
    than the leading 1 attached to any real pin.
    """
    net_name = issue.net_names[0] if issue.net_names else ""
    if not issue.pins:
        return (issue.kind, net_name, 0, "", "")
    pin = issue.pins[0]
    return (issue.kind, net_name, 1, pin.component_ref, pin.pin)


def _physical_net_label(id_: str | None, physical_net_by_id: dict[str, PhysicalNet]) -> str:
    """Human label for a physical net group in an OPEN message: a hole ref, or "nowhere"."""
    if id_ is None:
        return "not connected to any other pin in this net"
    pn = physical_net_by_id.get(id_)
    lowest = pn.nodes[0] if pn is not None and pn.nodes else None
    return f"physical net {id_}" if lowest is None else f"near {format_hole(lowest.hole)}"


# ---------------------------------------------------------------------------
# LVS
# ---------------------------------------------------------------------------

type _PinStatus = Literal["ok", "unplaced", "unknown-footprint"]


@dataclass(frozen=True, slots=True)
class _ClassifiedNode:
    node: NetNode
    status: _PinStatus
    #: Only meaningful when status == "ok"; None means the pin resolved to no hole.
    physical_net_id: str | None


@dataclass(slots=True)
class _NetGroup:
    """Mutable accumulator for one physical-net (or "unresolved pin") bucket while
    grouping a single schematic net's OK pins, before sorting into the issue output."""

    physical_net_id: str | None
    members: list[_ClassifiedNode] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _SortedGroup:
    physical_net_id: str | None
    pins: tuple[PhysicalPinRef, ...]


def run_lvs(doc: PerfDocument, lookup: FootprintLookup) -> LvsResult:
    physical_nets = extract_physical_nets(doc, lookup)
    physical_net_by_id: dict[str, PhysicalNet] = {pn.id: pn for pn in physical_nets}

    pin_to_physical_net_id: dict[_PinKey, str] = {}
    for pn in physical_nets:
        for pin in pn.pins:
            pin_to_physical_net_id[_pin_key(pin.component_ref, pin.pin)] = pn.id

    components_by_ref: dict[str, ComponentInstance] = {}
    for c in doc.components:
        components_by_ref.setdefault(c.ref, c)

    # Schematic pin -> the one net that declares it. Used to attribute physical-net
    # membership back to schematic intent for short detection.
    pin_to_net: dict[_PinKey, Net] = {}
    for net in doc.nets:
        for node in net.nodes:
            pin_to_net[_pin_key(node.component_ref, node.pin)] = net

    issues: list[LvsIssue] = []

    # --- Pass 1: floating conductors -- physical islands with no pin at all. ---
    for pn in physical_nets:
        if pn.pins:
            continue

        hole_refs = sorted({format_hole(n.hole) for n in pn.nodes})
        issues.append(
            LvsIssue(
                kind="floating-conductor",
                message=(
                    f"Conductor(s) {', '.join(pn.conductor_ids)} form an isolated island at "
                    f"{', '.join(hole_refs)} with no component pin attached -- likely a stray "
                    "solder trace, wire or bridge left over from editing."
                ),
                net_names=(),
                pins=(),
                physical_net_ids=(pn.id,),
                conductor_ids=pn.conductor_ids,
            )
        )

    # --- Pass 2: shorts -- physical nets that swallow pins from more than one schematic net. ---
    shorted_physical_net_ids: set[str] = set()

    for pn in physical_nets:
        per_net: dict[NetId, tuple[Net, list[PhysicalPinRef]]] = {}
        for pin in pn.pins:
            owning_net = pin_to_net.get(_pin_key(pin.component_ref, pin.pin))
            if owning_net is None:
                continue  # A pin absent from every schematic net is not an error.
            bucket = per_net.get(owning_net.id)
            if bucket is not None:
                bucket[1].append(pin)
            else:
                per_net[owning_net.id] = (owning_net, [pin])
        if len(per_net) < 2:
            continue

        shorted_physical_net_ids.add(pn.id)

        buckets = list(per_net.values())
        all_pins = sorted(pin for _bucket_net, pins in buckets for pin in pins)
        net_names = sorted(bucket_net.name for bucket_net, _pins in buckets)
        is_power_ground_short = any(bucket_net.net_class == "ground" for bucket_net, _pins in buckets) and any(
            bucket_net.net_class == "power" for bucket_net, _pins in buckets
        )

        lowest = pn.nodes[0] if pn.nodes else None
        hole_ref = pn.id if lowest is None else format_hole(lowest.hole)
        prefix = "CRITICAL SHORT (power tied to ground): " if is_power_ground_short else "SHORT: "
        quoted_net_names = " and ".join(f"'{n}'" for n in net_names)

        issues.append(
            LvsIssue(
                kind="short",
                message=(
                    f"{prefix}the physical connection at {hole_ref} ties together schematic nets "
                    f"{quoted_net_names} -- pins "
                    f"{', '.join(_format_pin_ref(p) for p in all_pins)}. "
                    "This is almost certainly a solder bridge; separate the pads and re-measure "
                    "isolation before applying power."
                ),
                net_names=tuple(net_names),
                pins=tuple(all_pins),
                physical_net_ids=(pn.id,),
                conductor_ids=pn.conductor_ids,
            )
        )

    # --- Pass 3: per schematic net -- unplaced / unknown-footprint / open / unrouted,
    # and which nets are MATCHED (realised exactly, no more, no less). ---
    matched_nets = 0

    for net in doc.nets:
        classified: list[_ClassifiedNode] = []
        for node in net.nodes:
            comp = components_by_ref.get(node.component_ref)
            if comp is None:
                classified.append(_ClassifiedNode(node=node, status="unplaced", physical_net_id=None))
                continue

            footprint = lookup(comp.footprint_id)
            if footprint is None:
                classified.append(
                    _ClassifiedNode(node=node, status="unknown-footprint", physical_net_id=None)
                )
                continue

            classified.append(
                _ClassifiedNode(
                    node=node,
                    status="ok",
                    physical_net_id=pin_to_physical_net_id.get(_pin_key(node.component_ref, node.pin)),
                )
            )

        unplaced = [cn for cn in classified if cn.status == "unplaced"]
        unknown_footprint = [cn for cn in classified if cn.status == "unknown-footprint"]
        ok = [cn for cn in classified if cn.status == "ok"]

        if unplaced:
            pins = sorted(_to_pin_ref(cn.node) for cn in unplaced)
            refs = sorted({p.component_ref for p in pins})
            issues.append(
                LvsIssue(
                    kind="unplaced-component",
                    message=(
                        f"Net '{net.name}' references pin(s) {', '.join(_format_pin_ref(p) for p in pins)}, "
                        f"but component(s) {', '.join(refs)} are not placed on the board."
                    ),
                    net_names=(net.name,),
                    pins=tuple(pins),
                    physical_net_ids=(),
                )
            )

        if unknown_footprint:
            pins = sorted(_to_pin_ref(cn.node) for cn in unknown_footprint)
            refs = sorted({p.component_ref for p in pins})
            issues.append(
                LvsIssue(
                    kind="unknown-footprint",
                    message=(
                        f"Net '{net.name}' references pin(s) {', '.join(_format_pin_ref(p) for p in pins)} on "
                        f"component(s) {', '.join(refs)}, whose footprint could not be resolved."
                    ),
                    net_names=(net.name,),
                    pins=tuple(pins),
                    physical_net_ids=(),
                )
            )

        sound = not unplaced and not unknown_footprint
        sole_physical_net_id: str | None = None

        if len(ok) >= 2:
            groups: dict[str, _NetGroup] = {}
            unresolved_counter = 0
            for cn in ok:
                key = cn.physical_net_id
                if key is None:
                    key = f"__unresolved-{unresolved_counter}"
                    unresolved_counter += 1
                existing = groups.get(key)
                if existing is not None:
                    existing.members.append(cn)
                else:
                    groups[key] = _NetGroup(physical_net_id=cn.physical_net_id, members=[cn])

            if len(groups) > 1:
                sound = False

                group_list = sorted(
                    (
                        _SortedGroup(
                            physical_net_id=g.physical_net_id,
                            pins=tuple(sorted(_to_pin_ref(m.node) for m in g.members)),
                        )
                        for g in groups.values()
                    ),
                    key=lambda g: g.pins[0],  # every group has >= 1 pin
                )

                all_pins = sorted(p for g in group_list for p in g.pins)
                physical_net_ids = sorted(
                    {g.physical_net_id for g in group_list if g.physical_net_id is not None}
                )

                # UNROUTED vs OPEN describes the STATE of the net, not its size: unrouted
                # means no two of its pins are connected to each other at all ("haven't
                # started"), open means some are and some aren't ("started and missed
                # one"). Deliberately independent of pin count -- if it keyed off the
                # number of pins, then straight after a netlist import the two-pin nets
                # would report 'open' while the three-pin nets reported 'unrouted', which
                # is two names for one situation.
                #
                # Note the consequence: a two-pin net can never be 'open', since two pins
                # are either joined or they are not. `summary.opens` therefore counts both
                # kinds below, so nothing goes missing from the under-connection total.
                kind: LvsIssueKind = "unrouted-net" if len(groups) == len(ok) else "open"

                group_text = "; ".join(
                    f"{{{', '.join(_format_pin_ref(p) for p in g.pins)}}} "
                    f"{_physical_net_label(g.physical_net_id, physical_net_by_id)}"
                    for g in group_list
                )

                message = (
                    f"Net '{net.name}' is unrouted: all {len(ok)} of its pins are present on the "
                    "board but none of them are connected to each other yet."
                    if kind == "unrouted-net"
                    else (
                        f"Net '{net.name}' is open: its pins are split across {len(groups)} separate "
                        f"physical connections instead of one -- {group_text}."
                    )
                )

                issues.append(
                    LvsIssue(
                        kind=kind,
                        message=message,
                        net_names=(net.name,),
                        pins=tuple(all_pins),
                        physical_net_ids=tuple(physical_net_ids),
                    )
                )
            else:
                only = next(iter(groups.values()))
                sole_physical_net_id = only.physical_net_id
        elif len(ok) == 1:
            single = ok[0]
            sole_physical_net_id = single.physical_net_id
            if sole_physical_net_id is None:
                sound = False

        if sound and sole_physical_net_id is not None and sole_physical_net_id in shorted_physical_net_ids:
            sound = False

        if sound and ok:
            matched_nets += 1

    issues.sort(key=_issue_sort_key)

    # Counts BOTH 'open' and 'unrouted-net': they are the two shapes of the same defect,
    # a net the board under-connects. Counting only 'open' would silently omit every
    # two-pin net, which can only ever be unrouted (see the kind selection above).
    opens = sum(1 for i in issues if i.kind in ("open", "unrouted-net"))
    shorts = sum(1 for i in issues if i.kind == "short")

    return LvsResult(
        ok=len(issues) == 0,
        issues=tuple(issues),
        summary=LvsSummary(
            schematic_nets=len(doc.nets),
            physical_nets=len(physical_nets),
            matched_nets=matched_nets,
            opens=opens,
            shorts=shorts,
        ),
    )


# ---------------------------------------------------------------------------
# Soldering-guide helpers -- the payoff of modelling schematic intent as data.
# ---------------------------------------------------------------------------


def floating_conductor_ids(doc: PerfDocument, lookup: FootprintLookup) -> tuple[ConductorId, ...]:
    """Conductors on islands with no component pin attached, sorted.

    The same condition the 'floating-conductor' issue reports, exposed on its own so a host can
    offer to clear them. This is what moving a part leaves behind: the trace that ran to its old
    pin hole is still there, now joined to nothing, while the router adds a fresh one to the
    pin's new home. Nothing electrical is lost by removing them -- by definition they connect
    nothing -- but they are still the user's copper, so the decision to delete belongs to a
    command they can see and undo, never to a silent cleanup.
    """
    ids: list[ConductorId] = []
    for physical_net in extract_physical_nets(doc, lookup):
        if physical_net.pins:
            continue
        ids.extend(physical_net.conductor_ids)
    return tuple(sorted(set(ids)))


def stale_conductor_ids(doc: PerfDocument, lookup: FootprintLookup) -> tuple[ConductorId, ...]:
    """Conductors whose own ``net_id`` claim has stopped being true, sorted.

    This is what moving a part actually leaves behind, and it is a wider condition than
    "floating". Move a chip and the trace that ran from its pin to a resistor is still soldered
    to the resistor, so it is not floating at all -- it is a piece of copper that says it
    implements net N while joining only ONE of net N's pins to an empty hole. The router then
    adds a fresh conductor to where the pin went, and the old one just accumulates.

    So the test is the conductor's own claim: it must reach at least two pins of the net it says
    it belongs to. Two is the point at which copper connects something; one or none connects
    nothing, whatever it is soldered to.

    A conductor with NO ``net_id`` is never reported. That is hand-drawn copper, which makes no
    claim this function could find false, and deleting someone's own wiring because a checker
    could not account for it would be exactly the wrong behaviour.
    """
    pins_by_net: dict[NetId, set[_PinKey]] = {}
    for net in doc.nets:
        pins_by_net[net.id] = {_pin_key(node.component_ref, node.pin) for node in net.nodes}

    stale: list[ConductorId] = []
    by_id = {c.id: c for c in doc.conductors}
    for physical_net in extract_physical_nets(doc, lookup):
        island_pins = {_pin_key(p.component_ref, p.pin) for p in physical_net.pins}
        for conductor_id in physical_net.conductor_ids:
            conductor = by_id.get(conductor_id)
            if conductor is None or conductor.net_id is None:
                continue
            declared = pins_by_net.get(conductor.net_id)
            if declared is None:
                continue  # Claims a net the document no longer has; not ours to judge.
            if len(island_pins & declared) < 2:
                stale.append(conductor_id)
    return tuple(sorted(set(stale)))


def continuity_checks(doc: PerfDocument) -> tuple[ContinuityCheck, ...]:
    """Pairs that MUST read continuous, per schematic net: a spanning chain
    (pin0-pin1, pin1-pin2, ...), not the full O(n^2) cross product. n-1 measurements
    prove the same fact as n(n-1)/2 and a human will actually perform them.

    Purely derived from schematic intent (``doc.nets``) -- it does not need a
    FootprintLookup or the physical board, because it defines what a human should go
    measure, independent of whether the board currently satisfies it yet.
    """
    checks: list[ContinuityCheck] = []

    for net in doc.nets:
        if len(net.nodes) < 2:
            continue  # A single-pin net has nothing to prove continuous.

        pins = sorted(_to_pin_ref(n) for n in net.nodes)
        for a, b in pairwise(pins):
            checks.append(ContinuityCheck(a=a, b=b, net_name=net.name))

    return tuple(checks)


#: Default cap on the number of isolation pairs returned by :func:`isolation_checks`.
#: The full cross product of distinct schematic net pairs is O(n^2) and unusable as a
#: manual checklist once a design has more than a handful of nets, so the list below is
#: a bounded, PRIORITISED SAMPLE -- not an exhaustive isolation matrix. A caller must not
#: treat a list at this length as proof every pair was considered.
DEFAULT_ISOLATION_CHECK_CAP: int = 40


def _pair_priority(class_a: NetClass, class_b: NetClass) -> int:
    """Priority bucket for a net-class pair: lower sorts first."""
    classes = {class_a, class_b}
    if "power" in classes and "ground" in classes:
        return 0  # the one that matters before power-on
    if "power" in classes and "signal" in classes:
        return 1
    return 2  # everything else: ground/signal, power/power, signal/signal, ground/ground, ...


def isolation_checks(doc: PerfDocument) -> tuple[IsolationCheck, ...]:
    """Pairs that MUST read open (isolated): one representative pin per pair of distinct
    schematic nets, power/ground pairs first, then power/signal, then the rest -- see
    :data:`DEFAULT_ISOLATION_CHECK_CAP` for why the result is capped rather than exhaustive.
    """
    representative: dict[NetId, PhysicalPinRef] = {}
    for net in doc.nets:
        if not net.nodes:
            continue
        sorted_pins = sorted(_to_pin_ref(n) for n in net.nodes)
        representative[net.id] = sorted_pins[0]

    nets = [n for n in doc.nets if n.id in representative]

    candidates: list[tuple[int, Net, Net, PhysicalPinRef, PhysicalPinRef]] = []
    for i in range(len(nets)):
        for j in range(i + 1, len(nets)):
            net_a = nets[i]
            net_b = nets[j]
            pin_a = representative[net_a.id]
            pin_b = representative[net_b.id]
            candidates.append((_pair_priority(net_a.net_class, net_b.net_class), net_a, net_b, pin_a, pin_b))

    candidates.sort(key=lambda c: (c[0], c[1].name, c[2].name))

    return tuple(
        IsolationCheck(a=pin_a, b=pin_b, net_a=net_a.name, net_b=net_b.name)
        for _priority, net_a, net_b, pin_a, pin_b in candidates[:DEFAULT_ISOLATION_CHECK_CAP]
    )
