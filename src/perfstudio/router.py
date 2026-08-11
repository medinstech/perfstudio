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

KNOWN COST-MODEL LIMITATION, kept for the same reason. The spine surcharge is applied
AFTER the search, once the path length is known (see ``_solder_trace_candidate``), so it
is not part of what A* minimises. A long detour that avoids proximity risk can therefore
be chosen over a shorter risky path and only then pick up the surcharge that makes it the
more expensive of the two -- e.g. a 12-step clear detour (12, then +6 spine = 18) beating
a 2-step path with one risk hole (14). Both routes are legal and buildable and the
difference is small, so this is a quality wart rather than a defect.

Expressing it properly means running the search twice -- once bounded to a pure trace's
maximum length, once unbounded -- and comparing the finished candidates. That is a real
improvement and it would change which path some existing routes take, which is exactly
what the golden fixtures exist to detect. It is deliberately NOT done here: the
differential proof against the TypeScript engine is worth more than the last few percent
of route quality, and a caller who needs the shorter route can already pick it out of
``RouteResult.alternatives``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .commands import NewConductor, NewSolderTraceConductor, NewWireConductor
from .connectivity import FootprintLookup, PhysicalNet, extract_physical_nets
from .geometry import (
    format_hole,
    hole_key,
    hole_to_mm,
    holes_under_line,
    is_inside_board,
    manhattan,
    neighbors4,
    path_length_mm,
    same_hole,
    segments_touch,
)
from .model import (
    Board,
    HoleCoord,
    NetId,
    PerfDocument,
    SolderBuildup,
    SpineSpec,
    is_crossing_blocked,
)
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
    #: One short insulated jumper carrying a solder trace over something it may not cross.
    #: Cheaper than ``insulated_wire_fixed`` because that prices a whole run -- measuring,
    #: cutting, stripping and dressing a wire across the board -- whereas a hop is a
    #: two-or-three-hole offcut. Pricing them the same would mean a run needing one crossing
    #: might as well be wire end to end, which is the opposite of what a builder wants.
    insulated_hop_fixed: float = 10


DEFAULT_ROUTER_COSTS = RouterCosts()


#: What the router may do when a connection cannot be made without crossing something that
#: must not be crossed. This is a judgement about the builder, not about the board, so it is
#: theirs to make:
#:
#:   "hop"    Solder trace as far as it goes, and one short insulated jumper over each
#:            obstacle. The default, because it is what someone building by hand does: most
#:            of the run is solder and only the crossing costs a piece of wire.
#:   "wire"   A crossing means a single insulated wire for the whole connection, end to end.
#:            For anyone who would rather run one clean wire than solder up to a jumper.
#:   "refuse" No wire of any kind. Solder traces only, and a connection that needs more is
#:            reported unrouted rather than made with wire the user did not ask for.
CrossingPolicy: TypeAlias = Literal["hop", "wire", "refuse"]


@dataclass(frozen=True, slots=True)
class RouterOptions:
    costs: RouterCosts = DEFAULT_ROUTER_COSTS
    #: Beyond this many pads, a pure solder trace is unreliable; a spine gets proposed.
    max_pure_solder_trace_pads: int = 6
    #: Top jumpers are ugly and block component space; off by default.
    allow_top_jumper: bool = False
    #: Search ceiling, so a hopeless request fails fast instead of scanning the board.
    max_expanded_nodes: int = 20000
    #: See :data:`CrossingPolicy`.
    crossing_policy: CrossingPolicy = "hop"
    #: How many blocked holes one insulated hop may span. A wire crossing occupies about one
    #: hole and a trace two or three; past that the obstacle is a wall, not something to step
    #: over, and the search should go round it instead.
    max_hop_holes: int = 3


DEFAULT_ROUTER_OPTIONS = RouterOptions()

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

RouteStrategy: TypeAlias = Literal[
    "solder-trace",
    "solder-trace-wired",
    "bare-wire",
    "insulated-wire",
    "top-jumper",
    #: Solder trace with one or more short insulated jumpers where it has to cross something.
    #: Several conductors in one candidate -- which RouteCandidate.conductors has always been
    #: a tuple for.
    "solder-trace-hopped",
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
    #: Holes the SCHEMATIC intends to be this same net, whether or not the board joins
    #: them yet -- normally every pin hole of the net (``ratsnest.NetRatsnest.pin_holes``).
    #:
    #: Without this, the router can only see PHYSICAL nets, so every unrouted pin of the
    #: very net being routed looks like foreign copper: passing beside one is charged R5'
    #: proximity risk and passing through one is forbidden outright. Both are wrong, and
    #: together they rule out the standard perfboard technique -- run a rail along the row
    #: and let it pick up each pin on the way (PLAN.md Sec 6.2). Supplying these holes
    #: says "this ground is mine", which makes the rail both legal and cheap.
    #:
    #: Empty by default, so a caller that does not supply it gets exactly the
    #: point-to-point behaviour the golden fixtures pin down.
    net_holes: tuple[HoleCoord, ...] = ()


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
        doc=doc,
        occupancy=occupancy,
        net_at=net_at,
        opts=options,
        own_net_id=request.net_id,
        net_holes=frozenset(hole_key(hole) for hole in request.net_holes),
        declared_own_nets=frozenset(
            net_id for net_id in (net_at(hole) for hole in request.net_holes) if net_id is not None
        ),
        blocked_segments=_blocked_segments(doc),
        swept_blocked_holes=_trace_blocked_holes(doc),
    )

    candidates: list[RouteCandidate] = []
    buildup: SolderBuildup = request.buildup if request.buildup is not None else "normal"
    trace = _solder_trace_candidate(ctx, from_, to, buildup)
    if trace is not None:
        candidates.append(trace)

    # What may be used when solder alone cannot get there is the BUILDER's decision, so it is
    # read from the options rather than assumed -- see CrossingPolicy.
    policy = options.crossing_policy
    if policy == "hop":
        hopped = _hopping_trace_candidate(ctx, from_, to, buildup)
        if hopped is not None:
            candidates.append(hopped)
    if policy != "refuse":
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
        refused = (
            " No wire is allowed by the current crossing policy, so only a solder trace was "
            "tried."
            if policy == "refuse"
            else " Every strategy was blocked — try moving a part, or allow a top jumper."
        )
        return RouteResult(
            ok=False,
            alternatives=(),
            reason=f"No route found from {format_hole(from_)} to {format_hole(to)}.{refused}",
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
    #: Hole keys of ``RouteRequest.net_holes``. Empty for a plain point-to-point request.
    net_holes: frozenset[str] = frozenset()
    #: Segments of existing conductors that may not be crossed, on the solder side.
    #: Checked geometrically, because occupancy is per HOLE and two runs cross between holes
    #: (see geometry.segments_touch) -- which is how the router used to produce boards with
    #: bare wires lying across each other that DRC then reported as clean.
    blocked_segments: tuple[tuple[HoleCoord, HoleCoord], ...] = ()
    #: Hole keys those segments sweep across, including the ones a wire merely passes over.
    #: Precomputed so the trace search can reject them in constant time.
    swept_blocked_holes: frozenset[str] = frozenset()
    #: Physical net ids those declared holes already sit in -- precomputed here rather
    #: than rediscovered per neighbour, since the set is fixed for the whole search.
    declared_own_nets: frozenset[str] = frozenset()


def _blocked_segments(doc: PerfDocument) -> tuple[tuple[HoleCoord, HoleCoord], ...]:
    """Every solder-side run of existing conductor that a new one may not cross."""
    segments: list[tuple[HoleCoord, HoleCoord]] = []
    for conductor in doc.conductors:
        if conductor.side != "bottom" or not is_crossing_blocked(conductor):
            continue
        for index in range(len(conductor.path) - 1):
            segments.append((conductor.path[index], conductor.path[index + 1]))
    return tuple(segments)


def _trace_blocked_holes(doc: PerfDocument) -> frozenset[str]:
    """Holes a new solder trace may not use because existing copper lies across them.

    The holes a conductor's `path` LISTS are already blocked by the occupancy index; these are
    the ones it only passes over, which for a wire is everything between its two ends. Kept
    here rather than folded into occupancy because that index's golden output is part of the
    differential proof against the TypeScript engine, and widening it moves three suites at
    once -- see the note in occupancy.build_occupancy.
    """
    keys: set[str] = set()
    for conductor in doc.conductors:
        if conductor.side != "bottom" or not is_crossing_blocked(conductor):
            continue
        for index in range(len(conductor.path) - 1):
            for hole in holes_under_line(conductor.path[index], conductor.path[index + 1]):
                keys.add(hole_key(hole))
    return frozenset(keys)


def _crosses_existing(ctx: _RouteContext, a: HoleCoord, b: HoleCoord) -> bool:
    """Would a straight run from ``a`` to ``b`` lie across existing uncrossable copper?"""
    return any(
        segments_touch(a, b, start, end) for start, end in ctx.blocked_segments
    )


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
        f"{len(path)} pads from {format_hole(from_)} to {format_hole(to)}"
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


# ---------------------------------------------------------------------------
# Strategy 1b: solder trace that hops what it may not cross
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Step:
    """One step of a hopping path: where it lands, and whether it got there over an obstacle."""

    hole: HoleCoord
    hopped: bool


def _hopping_trace_candidate(
    ctx: _RouteContext, from_: HoleCoord, to: HoleCoord, buildup: SolderBuildup
) -> RouteCandidate | None:
    """Solder trace most of the way, with a short insulated jumper over each blockage.

    This is how a perfboard actually gets built when two connections have to cross. You do not
    run wire the whole way and you cannot run solder through another net's copper; you solder
    up to the obstacle, bridge it with a scrap of insulated wire, and carry on soldering. The
    result is several conductors for one connection, which ``RouteCandidate.conductors`` has
    always been a tuple to allow.

    Only offered under ``CrossingPolicy`` "hop". Under "wire" the whole run becomes one
    insulated wire instead, and under "refuse" neither is offered and the connection is
    reported unrouted -- the user's call, not the router's.
    """
    steps = _find_hopping_path(ctx, from_, to)
    if steps is None:
        return None
    if not any(step.hopped for step in steps):
        return None  # No obstacle: the plain solder-trace candidate already covers this.

    costs = ctx.opts.costs
    conductors: list[NewConductor] = []
    risk_holes: list[HoleCoord] = []
    cost = 0.0
    hops = 0

    run: list[HoleCoord] = [steps[0].hole]
    for step in steps[1:]:
        if not step.hopped:
            run.append(step.hole)
            continue
        # A hop closes the trace run before it, then jumps.
        cost += _emit_trace_run(ctx, run, buildup, conductors, risk_holes, from_, to)
        hop_from, hop_to = run[-1], step.hole
        conductors.append(
            NewWireConductor(
                path=(hop_from, hop_to),
                kind="insulated-wire",
                side="bottom",
                net_id=ctx.own_net_id,
                # Above the copper it steps over, so the 3D view stacks it correctly.
                layer_z=1,
            )
        )
        cost += costs.insulated_hop_fixed + path_length_mm((hop_from, hop_to), ctx.doc.board) * (
            costs.insulated_wire_per_mm
        )
        hops += 1
        run = [step.hole]
    cost += _emit_trace_run(ctx, run, buildup, conductors, risk_holes, from_, to)

    if not conductors:
        return None

    trace_count = sum(1 for c in conductors if c.kind in ("solder-trace", "solder-trace-wired"))
    parts = [
        f"solder trace from {format_hole(from_)} to {format_hole(to)} in {trace_count} run(s), "
        f"with {hops} insulated hop(s) over conductors it may not cross"
    ]
    if risk_holes:
        holes_text = ", ".join(format_hole(hole) for hole in risk_holes)
        parts.append(
            f"{len(risk_holes)} pad(s) sit next to a different net ({holes_text}) — check "
            "isolation there after soldering"
        )
    return RouteCandidate(
        strategy="solder-trace-hopped",
        conductors=tuple(conductors),
        cost=cost,
        explanation=f"Hopped solder trace: {'; '.join(parts)}.",
        risk_holes=tuple(risk_holes),
    )


def _emit_trace_run(
    ctx: _RouteContext,
    run: list[HoleCoord],
    buildup: SolderBuildup,
    conductors: list[NewConductor],
    risk_holes: list[HoleCoord],
    from_: HoleCoord,
    to: HoleCoord,
) -> float:
    """Append one solder-trace conductor for ``run`` and return its cost.

    A run of a single hole is not a conductor -- it is the landing pad of one hop and the
    take-off of the next, which happens when two obstacles sit two holes apart. The pad is
    still joined, by the two wires that meet on it.
    """
    if len(run) < 2:
        return 0.0
    costs = ctx.opts.costs
    risky = [hole for hole in run if _has_foreign_neighbour(ctx, hole, from_, to)]
    risk_holes.extend(risky)
    needs_spine = len(run) > ctx.opts.max_pure_solder_trace_pads
    conductors.append(
        NewSolderTraceConductor(
            path=tuple(run),
            buildup=buildup,
            spine=SpineSpec(material="tinned-copper", gauge=0.6) if needs_spine else None,
            net_id=ctx.own_net_id,
            layer_z=0,
            kind="solder-trace-wired" if needs_spine else "solder-trace",
            side="bottom",
        )
    )
    return (
        (len(run) - 1) * costs.solder_trace_step
        + len(risky) * costs.proximity_risk
        + (costs.solder_trace_spine_fixed if needs_spine else 0.0)
    )


def _find_hopping_path(
    ctx: _RouteContext, from_: HoleCoord, to: HoleCoord
) -> list[_Step] | None:
    """A* over holes where, as well as stepping to a free neighbour, the search may JUMP over
    up to ``max_hop_holes`` blocked ones in a straight line and land on a free hole.

    Separate from ``_find_solder_trace_path`` rather than a flag on it, because that function
    reproduces the TypeScript engine's search exactly and forty-five golden routes depend on
    it doing so. A hop is a different move with a different cost, and bolting it on would put
    the differential proof at risk for no gain.
    """
    costs = ctx.opts.costs
    start_key = hole_key(from_)
    goal_key = hole_key(to)

    g_score: dict[str, float] = {start_key: 0.0}
    came_from: dict[str, tuple[HoleCoord, bool]] = {}
    open_list: list[_OpenEntry] = [
        _OpenEntry(hole=from_, f=manhattan(from_, to) * costs.solder_trace_step)
    ]
    closed: set[str] = set()
    expanded = 0

    while open_list:
        best_index = 0
        for i in range(1, len(open_list)):
            if open_list[i].f < open_list[best_index].f:
                best_index = i
        current = open_list.pop(best_index)
        current_key = hole_key(current.hole)
        if current_key == goal_key:
            return _reconstruct_steps(came_from, current.hole, from_)
        if current_key in closed:
            continue
        closed.add(current_key)

        expanded += 1
        if expanded > ctx.opts.max_expanded_nodes:
            return None

        g = g_score.get(current_key, math.inf)
        for move_to, hopped, step_cost in _moves_from(ctx, current.hole, from_, to):
            next_key = hole_key(move_to)
            if next_key in closed:
                continue
            tentative = g + step_cost
            if tentative >= g_score.get(next_key, math.inf):
                continue
            g_score[next_key] = tentative
            came_from[next_key] = (current.hole, hopped)
            open_list.append(
                _OpenEntry(
                    hole=move_to,
                    f=tentative + manhattan(move_to, to) * costs.solder_trace_step,
                )
            )
    return None


def _moves_from(
    ctx: _RouteContext, hole: HoleCoord, from_: HoleCoord, to: HoleCoord
) -> list[tuple[HoleCoord, bool, float]]:
    """Legal moves out of a hole: (destination, was it a hop, cost)."""
    costs = ctx.opts.costs
    goal_key = hole_key(to)
    moves: list[tuple[HoleCoord, bool, float]] = []

    for direction_col, direction_row in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        step = HoleCoord(col=hole.col + direction_col, row=hole.row + direction_row)
        if not is_inside_board(step, ctx.doc.board):
            continue
        is_goal = hole_key(step) == goal_key
        if is_goal or _is_traversable_by_trace(ctx, step):
            cost = costs.solder_trace_step
            if _has_foreign_neighbour(ctx, step, from_, to):
                cost += costs.proximity_risk
            moves.append((step, False, cost))
            continue

        # Blocked. Try to jump over it and land on clear ground beyond, which is what a short
        # insulated jumper does. The landing hole must be free (or the goal); the holes flown
        # over need not be, since the wire is insulated.
        for span in range(2, ctx.opts.max_hop_holes + 2):
            landing = HoleCoord(
                col=hole.col + direction_col * span, row=hole.row + direction_row * span
            )
            if not is_inside_board(landing, ctx.doc.board):
                break
            landing_is_goal = hole_key(landing) == goal_key
            if not landing_is_goal and not _is_traversable_by_trace(ctx, landing):
                continue
            moves.append(
                (
                    landing,
                    True,
                    costs.insulated_hop_fixed
                    + path_length_mm((hole, landing), ctx.doc.board) * costs.insulated_wire_per_mm,
                )
            )
            break
    return moves


def _reconstruct_steps(
    came_from: dict[str, tuple[HoleCoord, bool]], goal: HoleCoord, start: HoleCoord
) -> list[_Step]:
    # Each entry records how the search ARRIVED at that hole, so a hole's flag must be read
    # from its own entry. Carrying the flag over to the predecessor instead shifts every hop
    # one step back along the path: the jumper gets placed before the obstacle, and the trace
    # after it inherits the jump -- producing a "solder trace" with a hole missing from its
    # chain, which is not a solder trace at all.
    steps: list[_Step] = []
    cursor = goal
    while not same_hole(cursor, start):
        entry = came_from.get(hole_key(cursor))
        steps.append(_Step(hole=cursor, hopped=entry is not None and entry[1]))
        if entry is None:
            break
        cursor = entry[0]
    steps.append(_Step(hole=start, hopped=False))
    steps.reverse()
    return steps


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
    # Also refuse holes an existing WIRE lies across. Occupancy indexes a wire by its two
    # ends only (a documented gap it keeps for its own differential proof), so without this
    # the search would run a solder trace straight through the middle of an existing wire --
    # which DRC's conductor-crossing rule then, correctly, calls an error. A router must not
    # produce what the checker rejects.
    if hole_key(hole) in ctx.swept_blocked_holes:
        return False
    pin = ctx.occupancy.pin_at(hole)
    # A foreign pin in the way is a hard stop: soldering across it would short it in. A pin
    # the caller has declared part of this same net is the opposite -- running the trace
    # through it is how a rail collects its pins (see RouteRequest.net_holes), and doing so
    # is what makes one long rail cheaper than a fan of separate hops.
    if pin and hole_key(hole) not in ctx.net_holes:
        return False
    return True


def _has_foreign_neighbour(
    ctx: _RouteContext, hole: HoleCoord, from_: HoleCoord, to: HoleCoord
) -> bool:
    """Does this hole have an orthogonal neighbour belonging to a different net?

    At 2.54 mm pitch the pad-edge gap is well under a millimetre, so this is where a
    dragged bead of solder ends up somewhere it should not. DRC rule R5', priced into
    the search.

    "Different" is judged against the physical nets of the two endpoints AND against any
    holes the caller declared as this net's own (``RouteRequest.net_holes``): a bridge to
    a pad that is supposed to be on this net is not a defect, so charging risk for it
    would price the router out of the rails it should be building.
    """
    own_nets: set[str] = set(ctx.declared_own_nets)
    for endpoint in (from_, to):
        net_id = ctx.net_at(endpoint)
        if net_id is not None:
            own_nets.add(net_id)
    for neighbour in neighbors4(hole, ctx.doc.board):
        if same_hole(neighbour, from_) or same_hole(neighbour, to):
            continue
        if hole_key(neighbour) in ctx.net_holes:
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
    crossed = holes_under_line(from_, to)

    if kind == "bare-wire":
        # Bare wire cannot cross another conductor's copper or sit on a foreign pad.
        for hole in crossed:
            if same_hole(hole, from_) or same_hole(hole, to):
                continue
            if ctx.occupancy.is_copper_blocked(hole, "bottom"):
                return None
            if ctx.occupancy.pin_at(hole):
                return None
        # ...and it cannot lie ACROSS one either. The hole checks above only see copper the
        # line lands on; two runs at an angle cross between holes, touching none in common.
        # Without this the router happily produced boards with bare wires resting on each
        # other -- physically a short, and the exact defect DRC's conductor-crossing rule
        # reports (see geometry.segments_touch).
        if _crosses_existing(ctx, from_, to):
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
        f"{format_hole(from_)} to {format_hole(to)} — {note}."
    )

    return RouteCandidate(
        strategy=kind,
        conductors=(conductor,),
        cost=fixed + length_mm * per_mm,
        explanation=explanation,
        risk_holes=(),
    )


# ---------------------------------------------------------------------------
# Helpers used by callers
# ---------------------------------------------------------------------------


def connection_length_mm(from_: HoleCoord, to: HoleCoord, board: Board) -> float:
    """Straight-line distance in mm, for callers ordering work by how far apart pins are."""
    a = hole_to_mm(from_, board)
    b = hole_to_mm(to, board)
    return math.hypot(b.x - a.x, b.y - a.y)
