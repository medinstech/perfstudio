"""Connection router (PLAN.md Sec 6).

Ported from the original TypeScript engine (packages/core/src/router.ts).

WHAT THIS IS, AND DELIBERATELY IS NOT.

This is not a press-the-button-and-get-a-finished-board autorouter. Every existing
perfboard tool that promised that produced the complaint the plan quotes: "it routed
most of it and left four connections that were then impossible to finish by hand."
The target here is an interactive assistant: route ONE connection well, fast enough
that dragging a part can re-route what it touched, and be honest when nothing works.

HOW IT DECIDES.

Perfboard offers several physically different ways to join two points, so the router
evaluates each as a candidate strategy and picks the cheapest feasible one. That maps
directly onto the cost table in PLAN.md Sec 6.1:

  solder trace        cheap per step, but orthogonal only, cannot cross other copper
  wired solder trace  same path, plus a spine -- for long runs and current-carrying rails
  bare wire           cheap, straight, but cannot cross other copper
  insulated wire       crosses anything, costs preparation time
  top jumper           last resort: visible, and it occupies component space

THE PART THAT MATTERS MOST.

The solder-trace search puts R5' -- the ~0.6 mm gap to a neighbouring pad of another
net -- into the COST, not into a post-hoc warning. A router that merely avoids illegal
routes produces boards that are legal and unpleasant to solder. A router that prices
the risk produces boards that are legal AND buildable, and that is the whole argument
for this project existing (PLAN.md Sec 6.1).

Deterministic: no clock, no RNG. The same board and request always give the same route.

PORT FIDELITY NOTE. The A* open list below is a plain list, scanned linearly for the
lowest f-score each iteration, exactly as the TypeScript source does. A binary heap
would have a better asymptotic constant, but it would also change which of several
equal-f nodes is popped first, and that can silently pick a different (still optimal,
but different) path -- which would break byte-for-byte agreement with the golden
fixtures dumped from the TypeScript engine. Keep the linear scan.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .commands import NewConductor, NewSolderTraceConductor, NewWireConductor
from .connectivity import FootprintLookup, PhysicalNet, extract_physical_nets
from .geometry import (
    coord_to_hole_ref,
    format_hole,
    hole_key,
    hole_to_mm,
    is_inside_board,
    manhattan,
    neighbors4,
    path_length_mm,
    same_hole,
)
from .model import Board, HoleCoord, NetId, PerfDocument, SolderBuildup, SpineSpec
from .occupancy import OccupancyIndex, build_occupancy

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouterCosts:
    """Per-strategy cost table (PLAN.md Sec 6.1).

    Money is a proxy for a person's real judgement: solder is cheapest for a short
    hop, but nobody drags it twenty pads, so past ``max_pure_solder_trace_pads`` a
    wire wins on cost -- see ``RouterOptions.max_pure_solder_trace_pads``. The R5'
    proximity risk is charged here too, not merely flagged, so the A* search below
    steers around risky ground instead of only reporting it afterwards.
    """

    #: Per hole stepped along a pure solder trace. Cheap: this is the preferred primitive.
    solder_trace_step: float = 1
    #: One-off cost of preparing and laying a wire spine along a trace.
    solder_trace_spine_fixed: float = 6
    bare_wire_fixed: float = 8
    bare_wire_per_mm: float = 0.15
    insulated_wire_fixed: float = 18
    insulated_wire_per_mm: float = 0.2
    top_jumper_fixed: float = 40
    top_jumper_per_mm: float = 0.3
    #: Charged per trace hole that has a different-net pad as an orthogonal neighbour.
    #: This is DRC rule R5' expressed as money instead of a warning, so the search
    #: steers around risky ground instead of merely reporting it afterwards.
    proximity_risk: float = 12


DEFAULT_ROUTER_COSTS = RouterCosts()


@dataclass(frozen=True, slots=True)
class RouterOptions:
    costs: RouterCosts = DEFAULT_ROUTER_COSTS
    #: Beyond this many pads, a pure solder trace is unreliable; a spine gets proposed.
    max_pure_solder_trace_pads: int = 6
    #: Top jumpers are ugly and block component space; off by default.
    allow_top_jumper: bool = False
    #: Search ceiling, so a hopeless request fails fast instead of scanning the board.
    max_expanded_nodes: int = 20000


DEFAULT_ROUTER_OPTIONS = RouterOptions()

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

RouteStrategy: TypeAlias = Literal[
    "solder-trace", "solder-trace-wired", "bare-wire", "insulated-wire", "top-jumper"
]


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    strategy: RouteStrategy
    conductors: tuple[NewConductor, ...]
    cost: float
    #: Why this came out the way it did. Surfaced in the UI and reused by the guide.
    explanation: str
    #: Trace holes that sit next to a different net -- these become measurement steps.
    risk_holes: tuple[HoleCoord, ...]


@dataclass(frozen=True, slots=True)
class RouteResult:
    ok: bool
    best: RouteCandidate | None = None
    #: Every feasible strategy, cheapest first. Lets the UI offer "use a wire instead".
    alternatives: tuple[RouteCandidate, ...] = ()
    #: Populated only when nothing was feasible. Never fails silently.
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RouteRequest:
    #: Trailing underscore: ``from`` is a Python keyword.
    from_: HoleCoord
    to: HoleCoord
    #: Net being routed. Holes already on this net are free to pass through.
    net_id: NetId | None = None
    buildup: SolderBuildup | None = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def route_connection(
    doc: PerfDocument,
    lookup: FootprintLookup,
    request: RouteRequest,
    options: RouterOptions = DEFAULT_ROUTER_OPTIONS,
) -> RouteResult:
    from_ = request.from_
    to = request.to

    if not is_inside_board(from_, doc.board) or not is_inside_board(to, doc.board):
        return RouteResult(ok=False, alternatives=(), reason="Endpoint is outside the board.")
    if same_hole(from_, to):
        return RouteResult(ok=False, alternatives=(), reason="Start and end are the same hole.")

    occupancy = build_occupancy(doc, lookup)
    net_at = _build_net_index(doc, lookup)
    ctx = _RouteContext(
        doc=doc, occupancy=occupancy, net_at=net_at, opts=options, own_net_id=request.net_id
    )

    candidates: list[RouteCandidate] = []
    buildup: SolderBuildup = request.buildup if request.buildup is not None else "normal"
    trace = _solder_trace_candidate(ctx, from_, to, buildup)
    if trace is not None:
        candidates.append(trace)
    bare = _straight_wire_candidate(ctx, from_, to, "bare-wire")
    if bare is not None:
        candidates.append(bare)
    insulated = _straight_wire_candidate(ctx, from_, to, "insulated-wire")
    if insulated is not None:
        candidates.append(insulated)
    if options.allow_top_jumper:
        jumper = _straight_wire_candidate(ctx, from_, to, "top-jumper")
        if jumper is not None:
            candidates.append(jumper)

    candidates.sort(key=lambda c: (c.cost, c.strategy))
    if not candidates:
        return RouteResult(
            ok=False,
            alternatives=(),
            reason=(
                f"No route found from {format_hole(from_)} to {format_hole(to)}. "
                "Every strategy was blocked — try moving a part, or allow a top jumper."
            ),
        )
    best = candidates[0]
    return RouteResult(ok=True, best=best, alternatives=tuple(candidates))


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RouteContext:
    doc: PerfDocument
    occupancy: OccupancyIndex
    #: Physical net id occupying a hole's solder side, if any.
    net_at: Callable[[HoleCoord], str | None]
    opts: RouterOptions
    own_net_id: NetId | None


def _build_net_index(doc: PerfDocument, lookup: FootprintLookup) -> Callable[[HoleCoord], str | None]:
    nets: list[PhysicalNet] = extract_physical_nets(doc, lookup)
    by_hole: dict[str, str] = {}
    for net in nets:
        for node in net.nodes:
            by_hole[hole_key(node.hole)] = net.id

    def net_at(hole: HoleCoord) -> str | None:
        return by_hole.get(hole_key(hole))

    return net_at


# ---------------------------------------------------------------------------
# Strategy 1: solder trace, via A* over the 4-neighbour grid
# ---------------------------------------------------------------------------


def _solder_trace_candidate(
    ctx: _RouteContext, from_: HoleCoord, to: HoleCoord, buildup: SolderBuildup
) -> RouteCandidate | None:
    path = _find_solder_trace_path(ctx, from_, to)
    if path is None:
        return None

    costs = ctx.opts.costs
    max_pure_solder_trace_pads = ctx.opts.max_pure_solder_trace_pads
    risk_holes = tuple(hole for hole in path if _has_foreign_neighbour(ctx, hole, from_, to))
    step_cost = (len(path) - 1) * costs.solder_trace_step
    risk_cost = len(risk_holes) * costs.proximity_risk

    needs_spine = len(path) > max_pure_solder_trace_pads
    kind: Literal["solder-trace", "solder-trace-wired"] = (
        "solder-trace-wired" if needs_spine else "solder-trace"
    )
    spine_cost = costs.solder_trace_spine_fixed if needs_spine else 0.0

    conductor: NewConductor = NewSolderTraceConductor(
        path=tuple(path),
        buildup=buildup,
        spine=SpineSpec(material="tinned-copper", gauge=0.6) if needs_spine else None,
        net_id=ctx.own_net_id,
        layer_z=0,
        kind=kind,
        side="bottom",
    )

    parts: list[str] = [
        f"{len(path)} pads from {coord_to_hole_ref(from_)} to {coord_to_hole_ref(to)}"
    ]
    if needs_spine:
        parts.append(
            f"longer than {max_pure_solder_trace_pads} pads, so a tinned-copper spine is "
            "proposed — it drops the resistance by roughly an order of magnitude and makes "
            "the joint repeatable"
        )
    if risk_holes:
        holes_text = ", ".join(format_hole(hole) for hole in risk_holes)
        parts.append(
            f"{len(risk_holes)} pad(s) sit next to a different net ({holes_text}) — check "
            "isolation there after soldering"
        )

    return RouteCandidate(
        strategy=kind,
        conductors=(conductor,),
        cost=step_cost + risk_cost + spine_cost,
        explanation=f"Solder trace: {'; '.join(parts)}.",
        risk_holes=risk_holes,
    )


@dataclass(frozen=True, slots=True)
class _OpenEntry:
    hole: HoleCoord
    f: float


def _find_solder_trace_path(
    ctx: _RouteContext, from_: HoleCoord, to: HoleCoord
) -> list[HoleCoord] | None:
    """A* over holes, 4-connected.

    Blocked cells are holes whose solder-side copper already belongs to something
    else; the two endpoints are always allowed since they are what we are joining.
    The proximity risk is charged as step cost so the search prefers routes that
    keep clear of foreign pads rather than merely legal ones.
    """
    costs = ctx.opts.costs
    max_expanded_nodes = ctx.opts.max_expanded_nodes
    start_key = hole_key(from_)
    goal_key = hole_key(to)

    g_score: dict[str, float] = {start_key: 0.0}
    came_from: dict[str, HoleCoord] = {}
    open_list: list[_OpenEntry] = [
        _OpenEntry(hole=from_, f=manhattan(from_, to) * costs.solder_trace_step)
    ]
    closed: set[str] = set()
    expanded = 0

    while open_list:
        # Small boards and short routes: a linear scan beats the constant factor of a
        # heap. (A heap would also change tie-breaking order among equal-f nodes,
        # which can pick a different equal-cost path -- see the module docstring.)
        best_index = 0
        for i in range(1, len(open_list)):
            if open_list[i].f < open_list[best_index].f:
                best_index = i
        current = open_list.pop(best_index)

        current_key = hole_key(current.hole)
        if current_key == goal_key:
            return _reconstruct(came_from, current.hole, from_)
        if current_key in closed:
            continue
        closed.add(current_key)

        expanded += 1
        if expanded > max_expanded_nodes:
            return None

        g = g_score.get(current_key, math.inf)
        for next_hole in neighbors4(current.hole, ctx.doc.board):
            next_key = hole_key(next_hole)
            if next_key in closed:
                continue
            is_endpoint = next_key == goal_key
            if not is_endpoint and not _is_traversable_by_trace(ctx, next_hole):
                continue

            step = costs.solder_trace_step
            if _has_foreign_neighbour(ctx, next_hole, from_, to):
                step += costs.proximity_risk

            tentative = g + step
            if tentative >= g_score.get(next_key, math.inf):
                continue

            g_score[next_key] = tentative
            came_from[next_key] = current.hole
            open_list.append(
                _OpenEntry(
                    hole=next_hole, f=tentative + manhattan(next_hole, to) * costs.solder_trace_step
                )
            )

    return None


def _is_traversable_by_trace(ctx: _RouteContext, hole: HoleCoord) -> bool:
    """A trace may pass through a hole that is empty, or already on the net being routed."""
    if ctx.occupancy.is_copper_blocked(hole, "bottom"):
        return False
    pin = ctx.occupancy.pin_at(hole)
    # A foreign pin in the way is a hard stop: soldering across it would short it in.
    if pin:
        return False
    return True


def _has_foreign_neighbour(
    ctx: _RouteContext, hole: HoleCoord, from_: HoleCoord, to: HoleCoord
) -> bool:
    """Does this hole have an orthogonal neighbour belonging to a different net?

    At 2.54 mm pitch the pad-edge gap is well under a millimetre, so this is where a
    dragged bead of solder ends up somewhere it should not. DRC rule R5', priced into
    the search.
    """
    own_nets: set[str] = set()
    for endpoint in (from_, to):
        net_id = ctx.net_at(endpoint)
        if net_id is not None:
            own_nets.add(net_id)
    for neighbour in neighbors4(hole, ctx.doc.board):
        if same_hole(neighbour, from_) or same_hole(neighbour, to):
            continue
        net_id = ctx.net_at(neighbour)
        if net_id is not None and net_id not in own_nets:
            return True
    return False


def _reconstruct(
    came_from: dict[str, HoleCoord], goal: HoleCoord, start: HoleCoord
) -> list[HoleCoord]:
    path: list[HoleCoord] = [goal]
    cursor = goal
    while not same_hole(cursor, start):
        prev = came_from.get(hole_key(cursor))
        if prev is None:
            break
        path.append(prev)
        cursor = prev
    path.reverse()
    return path


# ---------------------------------------------------------------------------
# Strategies 2-4: straight wires
# ---------------------------------------------------------------------------


def _straight_wire_candidate(
    ctx: _RouteContext,
    from_: HoleCoord,
    to: HoleCoord,
    kind: Literal["bare-wire", "insulated-wire", "top-jumper"],
) -> RouteCandidate | None:
    costs = ctx.opts.costs
    crossed = _holes_under_straight_line(from_, to)

    if kind == "bare-wire":
        # Bare wire cannot cross another conductor's copper or sit on a foreign pad.
        for hole in crossed:
            if same_hole(hole, from_) or same_hole(hole, to):
                continue
            if ctx.occupancy.is_copper_blocked(hole, "bottom"):
                return None
            if ctx.occupancy.pin_at(hole):
                return None
    if kind == "top-jumper":
        # A top jumper must not have to run underneath a component body.
        for hole in crossed:
            if ctx.occupancy.body_covers(hole):
                return None

    length_mm = path_length_mm((from_, to), ctx.doc.board)

    if kind == "bare-wire":
        fixed = costs.bare_wire_fixed
        per_mm = costs.bare_wire_per_mm
    elif kind == "insulated-wire":
        fixed = costs.insulated_wire_fixed
        per_mm = costs.insulated_wire_per_mm
    else:
        fixed = costs.top_jumper_fixed
        per_mm = costs.top_jumper_per_mm

    if kind == "top-jumper":
        side: Literal["top", "bottom"] = "top"
    else:
        side = "bottom"
    layer_z = 1 if kind == "insulated-wire" else 0

    conductor: NewConductor = NewWireConductor(
        path=(from_, to),
        kind=kind,
        side=side,
        gauge_awg=None,
        color=None,
        net_id=ctx.own_net_id,
        layer_z=layer_z,
    )

    if kind == "bare-wire":
        note = "clear straight run on the solder side"
    elif kind == "insulated-wire":
        note = "insulated, so it may pass over other conductors"
    else:
        note = "component-side jumper — visible, and it takes up board space"

    explanation = (
        f"{kind.replace('-', ' ', 1)}: {length_mm:.1f} mm from "
        f"{coord_to_hole_ref(from_)} to {coord_to_hole_ref(to)} — {note}."
    )

    return RouteCandidate(
        strategy=kind,
        conductors=(conductor,),
        cost=fixed + length_mm * per_mm,
        explanation=explanation,
        risk_holes=(),
    )


def _js_math_round(x: float) -> int:
    """Replicates JavaScript's ``Math.round`` (round half towards +Infinity).

    Python's builtin ``round()`` uses round-half-to-even, which disagrees with
    JavaScript exactly at ``.5`` boundaries -- and ``_holes_under_straight_line``'s
    sampled ``t`` values land there often enough (any axis-aligned or 45-degree run)
    that the divergence is not academic. ``floor(x + 0.5)`` matches the ECMAScript
    specification's algorithm, and since both sides are IEEE-754 doubles doing the
    same ``+`` and ``floor``, it matches bit for bit.
    """
    return math.floor(x + 0.5)


def _holes_under_straight_line(from_: HoleCoord, to: HoleCoord) -> list[HoleCoord]:
    """Holes a straight wire physically passes over, so occupancy can be checked.

    Sampled along the segment at a fraction of the pitch, which is dense enough that
    no hole on the line is missed.
    """
    steps = max(abs(to.col - from_.col), abs(to.row - from_.row)) * 4
    seen: set[str] = set()
    result: list[HoleCoord] = []
    for i in range(steps + 1):
        t = 0.0 if steps == 0 else i / steps
        hole = HoleCoord(
            col=_js_math_round(from_.col + (to.col - from_.col) * t),
            row=_js_math_round(from_.row + (to.row - from_.row) * t),
        )
        k = hole_key(hole)
        if k not in seen:
            seen.add(k)
            result.append(hole)
    return result


# ---------------------------------------------------------------------------
# Helpers used by callers
# ---------------------------------------------------------------------------


def connection_length_mm(from_: HoleCoord, to: HoleCoord, board: Board) -> float:
    """Straight-line distance in mm, for callers ordering work by how far apart pins are."""
    a = hole_to_mm(from_, board)
    b = hole_to_mm(to, board)
    return math.hypot(b.x - a.x, b.y - a.y)
