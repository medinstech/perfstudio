"""Net-level autorouting (PLAN.md Sec 6, milestone M3).

``router.route_connection`` routes ONE connection between two holes and is deliberately
narrow. This module is the layer above it: it works out which connections a board still
needs (``ratsnest``), decides what order to attempt them in, feeds them to the router one
at a time against a document that grows as it goes, and reports what it could not do.

WHY ORDER IS THE WHOLE PROBLEM.

Every route consumes board. A trace laid early takes the direct path and forces later
connections around it, so the same set of connections routed in a different order
produces a different -- often much worse -- board. Two mechanisms address that:

  CRITICALITY FIRST. Ground, then power, then the highest fan-out nets. These are the
  ones that want to become long rails along a row, and they are the ones that suffer
  most from being routed last (PLAN.md Sec 6.2). Signals are better at squeezing around
  a rail than a rail is at squeezing around signals.

  RIP-UP AND RETRY. After a pass, any net that either failed or had to fall back to an
  expensive strategy is promoted to the front of the order and the WHOLE PLAN is
  discarded and re-planned from the original document. The cheapest plan wins. This is
  ordering-based rip-up: it never edits a plan in place, which keeps every pass
  independent and the result deterministic.

WHAT IT WILL NOT DO.

It does not rip up conductors that were already in the document. A user's own routing,
and anything a previous autoroute committed, is left exactly as it is: this is an
assistant, and silently unpicking work someone did by hand is not assisting. Rip-up
applies only to routes the current planning run proposed itself.

It also does not pretend. PLAN.md Sec 13 names the trap every previous perfboard
autorouter fell into -- "it routed most of it and left four connections that were then
impossible to finish by hand". Connections that could not be routed come back in
``NetOutcome.unrouted`` with the router's own reason, and the summary counts them. A
caller that shows only the successes is misreporting, not the planner.

NOTHING IS COMMITTED HERE. The result is a PLAN: a list of new-conductor specs plus a
preview document showing their effect. The caller dispatches ``conductor.addMany`` once,
so the whole autoroute is a single undo step, and the ids in the preview document are
provisional -- the real ones are assigned by the real bus (see ``AutoroutePlan``).

Pure and deterministic: no I/O, no clock, no randomness. Same board, same plan.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .command import CommandContext, CommandError
from .commands import (
    AddConductorPayload,
    AddConductorsPayload,
    NewConductor,
    ReplaceConductorsPayload,
    add_conductor,
    create_document_id_generator,
)
from .connectivity import FootprintLookup, PhysicalPinRef
from .geometry import format_hole, path_length_mm
from .model import ConductorId, HoleCoord, NetClass, NetId, PerfDocument
from .ratsnest import NetRatsnest, RatsnestLink, ratsnest
from .router import (
    DEFAULT_ROUTER_OPTIONS,
    RouteRequest,
    RouterOptions,
    RouteStrategy,
    RoutingStyle,
    options_for_style,
    route_connection,
)

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

#: Net-class routing order (PLAN.md Sec 6.2). Ground first: it has the highest fan-out on
#: almost every board, it wants a clear rail, and every other net can be measured against
#: it afterwards. Power next, for the same reasons with one fewer pin.
NET_CLASS_PRIORITY: dict[NetClass, int] = {"ground": 0, "power": 1, "signal": 2}

#: Strategies whose use suggests the connection lost a race for board space. A solder
#: trace that had to become an insulated wire is legal, buildable and more work than it
#: needed to be -- exactly the signal a retry pass should act on.
FALLBACK_STRATEGIES: frozenset[RouteStrategy] = frozenset({"insulated-wire", "top-jumper"})

#: The two strategies that contact every hole they cross, and therefore the only ones a
#: rail can be built from: a rail exists to pick up the pins it runs over, and a wire
#: touches only its two ends.
RAIL_STRATEGIES: frozenset[RouteStrategy] = frozenset({"solder-trace", "solder-trace-wired"})


@dataclass(frozen=True, slots=True)
class AutorouteOptions:
    router: RouterOptions = DEFAULT_ROUTER_OPTIONS
    #: Ordering attempts, including the first. 1 disables rip-up entirely, which is what
    #: a test wanting to observe a single pass in isolation should ask for.
    max_passes: int = 3
    #: Charged once per separate conductor when comparing two ways to route one net
    #: (PLAN.md Sec 6.1: "a fixed penalty for every additional conductor"). Cost alone
    #: always prefers four short traces to one long rail, because it only counts solder;
    #: a person building the board pays per joint they have to prepare, position and
    #: inspect. Set to 0 to score purely on the router's own cost.
    conductor_penalty: float = 6.0
    #: Groups a net must still be split into before a rail is even attempted. Below this
    #: there is nothing for a rail to collect and the direct connection is simply better.
    rail_min_fanout: int = 3


DEFAULT_AUTOROUTE_OPTIONS = AutorouteOptions()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoutedLink:
    """One ratsnest connection the router turned into copper."""

    link: RatsnestLink
    strategy: RouteStrategy
    cost: float
    #: The router's own prose. Reused verbatim by the build guide, so it is kept, not
    #: regenerated from the fields above.
    explanation: str
    #: Holes this route runs close to another net (DRC R5'). These become measurement
    #: steps in the guide, which is the point of collecting them here rather than
    #: rediscovering them with a DRC pass afterwards.
    risk_holes: tuple[HoleCoord, ...]
    conductors: tuple[NewConductor, ...]


@dataclass(frozen=True, slots=True)
class UnroutedLink:
    """A connection the router declined, with the reason it gave. Never dropped."""

    link: RatsnestLink
    reason: str


@dataclass(frozen=True, slots=True)
class NetOutcome:
    net_id: NetId
    net_name: str
    net_class: NetClass
    routed: tuple[RoutedLink, ...]
    unrouted: tuple[UnroutedLink, ...]
    #: Pins the schematic names that are not on the board -- see ratsnest.NetRatsnest.
    unresolved_pins: tuple[PhysicalPinRef, ...]

    @property
    def closed(self) -> bool:
        """True when every connection this net still needed was made."""
        return not self.unrouted and not self.unresolved_pins


@dataclass(frozen=True, slots=True)
class AutorouteSummary:
    nets_considered: int
    nets_closed: int
    links_routed: int
    links_unrouted: int
    total_cost: float
    #: Routes that fell back to an insulated wire or a top jumper.
    fallback_links: int
    risk_holes: int
    #: Ordering passes actually run, including the first. >1 means rip-up did something.
    passes: int


@dataclass(frozen=True, slots=True)
class AutoroutePlan:
    """A proposed routing, not a committed one.

    ``document`` is a PREVIEW, so DRC, LVS and a 2D render can all be run against it to
    show the user what they are about to accept. It is built with the same id sequence the
    bus will use, so committing :meth:`payload` onto the document this plan was made from
    reproduces it exactly -- but it is still a preview: it was never dispatched, so it is
    on no undo stack, and planning against a document the user has since edited invalidates
    it. Commit it or discard it; do not persist it.
    """

    document: PerfDocument
    conductors: tuple[NewConductor, ...]
    nets: tuple[NetOutcome, ...]
    summary: AutorouteSummary
    label: str

    def payload(self) -> AddConductorsPayload:
        """The one command that commits this plan, as a single undo step."""
        return AddConductorsPayload(conductors=self.conductors, label=self.label)

    @property
    def is_empty(self) -> bool:
        """Nothing to commit. ``conductor.addMany`` refuses an empty batch on purpose,
        so a caller must check this rather than dispatch and hope."""
        return not self.conductors


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_autoroute(
    doc: PerfDocument,
    lookup: FootprintLookup,
    options: AutorouteOptions = DEFAULT_AUTOROUTE_OPTIONS,
    only_net_ids: tuple[NetId, ...] | None = None,
) -> AutoroutePlan:
    """Plan the routing of every schematic net (or just ``only_net_ids``).

    Nets already closed cost nothing to include: their ratsnest is empty, so they
    contribute no work and appear in the result with empty ``routed``/``unrouted``.
    """
    wanted = _nets_to_route(doc, only_net_ids)
    if not wanted:
        return _empty_plan(doc, only_net_ids)

    order = _criticality_order(doc, wanted)
    seen_orders: list[tuple[NetId, ...]] = []
    best: _Attempt | None = None
    passes_run = 0

    for pass_number in range(1, max(1, options.max_passes) + 1):
        attempt = _route_in_order(doc, lookup, order, options, pass_number)
        passes_run = pass_number
        if best is None or _rank(attempt) < _rank(best):
            best = attempt

        troubled = _nets_to_promote(attempt)
        if not troubled:
            break
        seen_orders.append(order)
        promoted = _promote(order, troubled)
        # Re-running an order already tried would reproduce its plan exactly (planning is
        # deterministic), so there is nothing left to learn -- stop instead of burning the
        # remaining passes on a known answer.
        if promoted in seen_orders or promoted == order:
            break
        order = promoted

    assert best is not None  # The loop runs at least once, since max_passes >= 1.
    return _to_plan(best, passes_run)


def plan_route_net(
    doc: PerfDocument,
    lookup: FootprintLookup,
    net_id: NetId,
    options: AutorouteOptions = DEFAULT_AUTOROUTE_OPTIONS,
) -> AutoroutePlan:
    """Route a single net. Same machinery, so a one-net run and a whole-board run cannot
    disagree about how a connection should be made."""
    return plan_autoroute(doc, lookup, options, only_net_ids=(net_id,))


# ---------------------------------------------------------------------------
# Trying every style and keeping the best
# ---------------------------------------------------------------------------
#
# A routing style is an OPINION about the builder, expressed as a cost table (see
# router.RoutingStyle). Until now the user had to pick one up front and live with it,
# which means guessing -- before seeing a single route -- whether this particular board
# routes better with solder or with wire. That is a question the tool can simply answer by
# trying, because planning is pure and a plan is cheap.
#
# THE TRAP THIS AVOIDS. Each style's plan carries a `total_cost` and comparing those is
# meaningless: they are quoted in different currencies. The `wire` table prices a solder
# step at 4 and an insulated wire at 6, so its plans look cheap BY ITS OWN DEFINITION of
# cheap, and a naive `min(total_cost)` would pick the wire style on every board. Cost is
# what makes each search good at finding a plan; it cannot be what compares two of them.
#
# So the comparison is on PHYSICAL FACTS, measured off the finished plan and identical in
# meaning whichever table produced it.

#: Every style, in the order they are tried. Fixed rather than derived from the Literal so
#: the order -- and therefore the tie-break -- is deterministic and reviewable.
ALL_ROUTING_STYLES: tuple[RoutingStyle, ...] = ("balanced", "solder", "wire", "lead-bend")

#: Weights for the build-effort score below. Each is a claim about what the person holding
#: the iron actually has to do, and each is stated here rather than buried at its use site.
#:
#:   A TRACE is cheap. Drag solder along a row of pads the parts are already sitting in:
#:   position it, solder it, look at it. This is the preferred primitive and the premise
#:   the whole project rests on (PLAN.md Sec 6.1), so it is charged least.
#:
#:   A WIRE is dear, and dear by a FIXED amount before a single millimetre of it exists:
#:   measure, cut, strip both ends, tin them, dress it flat, solder twice, inspect. Pricing
#:   a wire and a trace the same -- which the first version of this did -- undercharges the
#:   one primitive that has real preparation behind it, and no board comparison built on
#:   that is worth reading.
#:
#:   PER MILLIMETRE is the part of a wire that does scale: a 60 mm run across the board is
#:   more to dress and more to go wrong than a 10 mm hop. Trace length is deliberately NOT
#:   charged at all -- length is exactly what a trace is good at.
#:
#:   RISK is DRC rule R5', a trace running 0.6 mm from a pad of another net and the
#:   commonest way a hand-built board fails. Charged well below the 12 the balanced search
#:   uses, because by comparison time the route already succeeded and every risky hole has
#:   become a measurement step in the guide: it is a managed cost, not a veto. Set against
#:   the wire weight, this says one risky gap is worth about a third of a wire -- which is
#:   the exchange rate this whole comparison turns on, so it is the number to argue with.
TRACE_CONDUCTOR_EFFORT = 3.0
WIRE_CONDUCTOR_EFFORT = 10.0
WIRE_MM_EFFORT = 0.08
RISK_HOLE_EFFORT = 3.0

_WIRE_KINDS = frozenset({"bare-wire", "insulated-wire", "top-jumper"})


@dataclass(frozen=True, slots=True)
class VariantScore:
    """One plan measured in facts, not in the money that produced it."""

    #: Connections the router could not make. Categorical: see :meth:`key`.
    unrouted: int
    risk_holes: int
    #: Split, because a wire and a trace are not the same amount of work -- see the
    #: weights above. A lead bend counts as a trace: there is nothing to cut or strip.
    traces: int
    wires: int
    wire_mm: float

    @property
    def conductors(self) -> int:
        return self.traces + self.wires

    @property
    def effort(self) -> float:
        """What this board costs a person to build, once it is known to be routable."""
        return (
            self.traces * TRACE_CONDUCTOR_EFFORT
            + self.wires * WIRE_CONDUCTOR_EFFORT
            + self.wire_mm * WIRE_MM_EFFORT
            + self.risk_holes * RISK_HOLE_EFFORT
        )

    def key(self) -> tuple[int, float]:
        """Comparison key. Unrouted connections come FIRST and alone, as a gate rather than
        a term in the sum: a connection the user has to finish by hand is not something to
        be traded against a tidier board elsewhere, and PLAN.md Sec 13 names routing most
        of a board and leaving four impossible connections as the trap every previous
        perfboard autorouter fell into. Below that gate, effort decides.
        """
        return (self.unrouted, self.effort)


@dataclass(frozen=True, slots=True)
class AutorouteVariant:
    """One style's attempt, kept whether it won or lost.

    Losers are returned rather than discarded because the user's own preference is a
    legitimate reason to overrule this comparison, and they cannot exercise it against a
    number they were never shown.
    """

    style: RoutingStyle
    plan: AutoroutePlan
    score: VariantScore


@dataclass(frozen=True, slots=True)
class BestAutoroute:
    """The winning plan, and every variant that was measured to find it."""

    style: RoutingStyle
    plan: AutoroutePlan
    variants: tuple[AutorouteVariant, ...]

    @property
    def considered(self) -> int:
        return len(self.variants)


def score_plan(plan: AutoroutePlan, doc: PerfDocument) -> VariantScore:
    """Measure a plan in style-independent terms.

    ``doc`` supplies the board, since a length in millimetres depends on its pitch.
    """
    wires = [c for c in plan.conductors if c.kind in _WIRE_KINDS]
    return VariantScore(
        unrouted=plan.summary.links_unrouted,
        risk_holes=plan.summary.risk_holes,
        traces=len(plan.conductors) - len(wires),
        wires=len(wires),
        wire_mm=sum(path_length_mm(c.path, doc.board) for c in wires),
    )


def plan_best_autoroute(
    doc: PerfDocument,
    lookup: FootprintLookup,
    options: AutorouteOptions = DEFAULT_AUTOROUTE_OPTIONS,
    only_net_ids: tuple[NetId, ...] | None = None,
    styles: tuple[RoutingStyle, ...] = ALL_ROUTING_STYLES,
) -> BestAutoroute:
    """Route the board once per style and keep the plan that is best to build.

    Every style starts from the SAME original document, so the variants are genuine
    alternatives rather than a sequence of edits, and each runs the ordinary planner --
    criticality order, rip-up and retry, chain against rail -- so a variant is exactly what
    the user would have got by choosing that style by hand.

    ``options.router`` supplies the flags a style does not set (search ceiling, crossing
    policy, top jumpers); the style replaces the cost table and the flags it implies. That
    keeps a user's "never use a top jumper" honoured across all four variants instead of
    being silently reset by the sweep.

    Deterministic: fixed style order, and ties fall to the earlier style in that order --
    so ``balanced`` wins a dead heat and this reduces to today's behaviour when nothing
    beats it.
    """
    variants: list[AutorouteVariant] = []
    for style in styles or ALL_ROUTING_STYLES:
        router = options_for_style(style, options.router)
        plan = plan_autoroute(
            doc, lookup, dataclasses.replace(options, router=router), only_net_ids
        )
        variants.append(
            AutorouteVariant(style=style, plan=plan, score=score_plan(plan, doc))
        )

    # min() is stable, so an exact tie keeps the earliest style tried.
    winner = min(variants, key=lambda variant: variant.score.key())
    return BestAutoroute(
        style=winner.style, plan=winner.plan, variants=tuple(variants)
    )


def describe_best(best: BestAutoroute) -> str:
    """One line per style tried, cheapest first, saying what each would cost to build.

    Printed rather than summarised because the winner is chosen on a judgement the user is
    entitled to disagree with -- and a tool that says only "I picked solder" gives them
    nothing to disagree with.
    """
    lines = [
        f"{best.style} wins on {best.considered} variant"
        f"{'' if best.considered == 1 else 's'} tried"
    ]
    for variant in sorted(best.variants, key=lambda v: v.score.key()):
        score = variant.score
        mark = "*" if variant.style == best.style else " "
        unrouted = f"{score.unrouted} UNROUTED, " if score.unrouted else ""
        lines.append(
            f"  {mark} {variant.style:<10} {unrouted}"
            f"{variant.plan.summary.links_routed} routed, "
            f"{score.traces} trace{'' if score.traces == 1 else 's'}, "
            f"{score.wires} wire{'' if score.wires == 1 else 's'} ({score.wire_mm:.0f} mm), "
            # ASCII only: this line goes to the headless CLI, and a console on a Windows
            # code page raises UnicodeEncodeError on a character it cannot map rather than
            # dropping it -- which turns a report into a crash.
            f"{score.risk_holes} risk hole{'' if score.risk_holes == 1 else 's'}"
            f"  -> effort {score.effort:.0f}"
        )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ReroutePlan:
    """Rip up a net's existing copper and route it again from nothing.

    WHY THIS EXISTS AS A SEPARATE THING FROM ``plan_autoroute``.

    Autoroute only ADDS. It looks at what is still unconnected and connects it, which is
    the right behaviour when a board is half finished and the wrong one after a part has
    moved: the copper laid for the old position still joins the right pins, so it is
    neither stale nor redundant nor a DRC violation -- it simply describes a board that
    no longer exists, and the router adds more copper alongside it. Measured on the NE555
    fixture: routed fresh it takes 14 conductors; move one resistor and autoroute again
    and it is 16, none of which can be removed without disconnecting something. Routed
    again from scratch after the move it is 14.

    So the only way back to a clean board is to rip up and re-plan, and that is a
    different user intention from "finish the routing" -- it discards work. It gets its
    own function, its own command and its own menu entry rather than quietly becoming
    what Ctrl+R does.

    WHAT IT WILL REMOVE: conductors whose ``net_id`` names one of the nets being
    re-routed. That claim is the conductor saying "I am part of this net's routing", so
    replacing that routing replaces it. Copper with NO net_id is never touched -- that is
    hand-drawn work, which makes no claim this function could act on, and unpicking it
    would be exactly the wrong behaviour.
    """

    document: PerfDocument
    #: Conductors to delete. Empty means nothing was ripped up.
    remove_ids: tuple[ConductorId, ...]
    conductors: tuple[NewConductor, ...]
    nets: tuple[NetOutcome, ...]
    summary: AutorouteSummary
    label: str

    def payload(self) -> ReplaceConductorsPayload:
        """The one command that commits this plan, as a single undo step."""
        return ReplaceConductorsPayload(
            remove_ids=self.remove_ids, conductors=self.conductors, label=self.label
        )

    @property
    def is_empty(self) -> bool:
        return not self.remove_ids and not self.conductors


def plan_reroute(
    doc: PerfDocument,
    lookup: FootprintLookup,
    only_net_ids: tuple[NetId, ...] | None = None,
    options: AutorouteOptions = DEFAULT_AUTOROUTE_OPTIONS,
) -> ReroutePlan:
    """Rip up the routing of every net (or just ``only_net_ids``) and plan it again."""
    targets = {net.id for net in doc.nets}
    if only_net_ids is not None:
        targets &= set(only_net_ids)

    remove_ids = tuple(
        sorted(c.id for c in doc.conductors if c.net_id is not None and c.net_id in targets)
    )
    stripped = dataclasses.replace(
        doc, conductors=tuple(c for c in doc.conductors if c.id not in set(remove_ids))
    )

    plan = plan_autoroute(stripped, lookup, options, only_net_ids=only_net_ids)
    names = [net.name for net in doc.nets if net.id in targets]
    label = (
        f"Re-route {len(names)} net(s)"
        if len(names) != 1
        else f"Re-route {names[0]}"
    )
    return ReroutePlan(
        document=plan.document,
        remove_ids=remove_ids,
        conductors=plan.conductors,
        nets=plan.nets,
        summary=plan.summary,
        label=label,
    )


def describe_reroute(plan: ReroutePlan) -> str:
    """One line for a status bar, leading with what was thrown away."""
    if plan.is_empty:
        return "Nothing to re-route"
    parts = [f"{len(plan.remove_ids)} old conductor(s) ripped up"]
    parts.append(f"{plan.summary.links_routed} connection(s) re-routed")
    if plan.summary.links_unrouted:
        parts.append(f"{plan.summary.links_unrouted} could NOT be routed")
    net_count = len(plan.conductors)
    parts.append(f"{net_count} conductor(s) now")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# One ordering attempt
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Attempt:
    document: PerfDocument
    conductors: tuple[NewConductor, ...]
    outcomes: tuple[NetOutcome, ...]
    pass_number: int


def _route_in_order(
    doc: PerfDocument,
    lookup: FootprintLookup,
    order: tuple[NetId, ...],
    options: AutorouteOptions,
    pass_number: int,
) -> _Attempt:
    """Route every net once, in the given order, against a document that grows as it goes.

    Starts from the ORIGINAL document every time, which is what makes a retry a genuine
    rip-up of the previous plan rather than an addition to it.
    """
    working = doc
    planned: list[NewConductor] = []
    outcomes: dict[NetId, NetOutcome] = {}

    for net_id in order:
        entry = next((n for n in ratsnest(working, lookup) if n.net_id == net_id), None)
        if entry is None:
            continue

        # Two ways to route a net, scored against each other (PLAN.md Sec 6.1-6.2). Both
        # start from the same document AND from the same id counter, so the strategy that
        # loses leaves no trace -- see _fresh_ctx for why that matters to the caller.
        candidates = [_chain_net(working, lookup, net_id, options, _fresh_ctx(working))]
        # A RAIL IS A SOLDER CONCEPT, so a builder who has committed to wire does not get
        # one. It is a trace along a row that every pin on the way past is soldered into --
        # a wire cannot do that, it touches only its two ends. This is the one place a
        # preference has to be honoured OUTSIDE route_connection's candidate sort, because
        # _rail_net reaches past `result.best` to pick a contacting strategy itself; without
        # this, "route everything with wire" still came back with solder rails in it.
        if options.router.prefer != "wire":
            rail = _rail_net(working, lookup, net_id, entry, options, _fresh_ctx(working))
            if rail is not None:
                candidates.append(rail)
        chosen = min(candidates, key=lambda plan: (plan.failures, plan.score(options)))

        working = chosen.document
        planned.extend(chosen.conductors)
        outcomes[net_id] = NetOutcome(
            net_id=entry.net_id,
            net_name=entry.net_name,
            net_class=entry.net_class,
            routed=tuple(chosen.routed),
            unrouted=tuple(chosen.unrouted),
            unresolved_pins=entry.unresolved_pins,
        )

    # Reported in document order, not routing order: a user reading the report wants it to
    # line up with their netlist, and the order work happened in is an implementation
    # detail already visible in the undo label.
    ordered = tuple(outcomes[net.id] for net in doc.nets if net.id in outcomes)
    return _Attempt(
        document=working,
        conductors=tuple(planned),
        outcomes=ordered,
        pass_number=pass_number,
    )


# ---------------------------------------------------------------------------
# Routing one net: two competing strategies
# ---------------------------------------------------------------------------


def _pin_identity(doc: PerfDocument, pin: PhysicalPinRef) -> tuple[str, str] | None:
    """``(component_id, pin_number)`` for a ratsnest pin, or None if it is not placed.

    The ratsnest names pins by REFERENCE ("R1") because that is what a netlist says; a
    lead-bend conductor names them by component ID because that is what survives a
    rename. This is the one place the two meet.
    """
    for component in doc.components:
        if component.ref == pin.component_ref:
            return component.id, pin.pin
    return None


def _fresh_ctx(doc: PerfDocument) -> CommandContext:
    """An id source seeded from ``doc``, for exploring ONE candidate.

    Deliberately per candidate rather than one shared counter for the whole run. A shared
    counter would be consumed by the strategies that lose, so the surviving conductors
    would carry ids with gaps in them -- and the preview document would then disagree with
    the document a real ``conductor.addMany`` produces, which is precisely the promise
    ``AutoroutePlan`` makes. Seeding each candidate from the current working document means
    the winner's ids are exactly the ones the bus will assign.
    """
    return CommandContext(next_id=create_document_id_generator(doc))


@dataclass(frozen=True, slots=True)
class _NetPlan:
    """One way of routing one net, ready to be compared with another."""

    document: PerfDocument
    conductors: tuple[NewConductor, ...]
    routed: tuple[RoutedLink, ...]
    unrouted: tuple[UnroutedLink, ...]

    @property
    def failures(self) -> int:
        return len(self.unrouted)

    def score(self, options: AutorouteOptions) -> float:
        """Router cost plus a charge per separate conductor.

        Without the second term the comparison is not a comparison at all: four short
        traces always beat one rail on solder alone, because solder is exactly what a
        short trace uses least of. The per-conductor charge is what encodes the thing a
        builder knows -- one long run is less work, and less to get wrong, than four
        short ones (PLAN.md Sec 6.1).
        """
        return sum(item.cost for item in self.routed) + options.conductor_penalty * len(
            self.conductors
        )


def _apply_candidate(
    doc: PerfDocument, conductors: tuple[NewConductor, ...], ctx: CommandContext
) -> tuple[PerfDocument, str | None]:
    """Apply a candidate's conductors, or report why they could not be applied.

    Staged onto a local document and only returned on success, so a candidate carrying
    several conductors can never leave the caller's document holding half of them.
    """
    staged = doc
    for spec in conductors:
        try:
            staged = add_conductor.apply(staged, AddConductorPayload(conductor=spec), ctx)
        except CommandError as err:
            # The router should only ever produce dispatchable conductors, so this is a bug
            # rather than a routing outcome -- but reporting it as an unrouted connection
            # keeps the planner honest instead of losing a whole board's plan to it.
            return doc, f"[{err.code}] {err.message}"
    return staged, None


def _chain_net(
    doc: PerfDocument,
    lookup: FootprintLookup,
    net_id: NetId,
    options: AutorouteOptions,
    ctx: CommandContext,
) -> _NetPlan:
    """Route the net one ratsnest link at a time, shortest first.

    The ratsnest is recomputed after every route, not planned once up front. Two reasons,
    and the second is the interesting one:
      - An earlier net's copper may have merged two of this net's pins, so a link planned
        up front could already be redundant.
      - A trace laid through this net's own pin holes picks those pins up as it goes (see
        ``router.RouteRequest.net_holes``), so one route can close several links at once.
        Only a fresh ratsnest knows which are left; a pre-planned list would lay a
        redundant conductor for each.
    """
    working = doc
    routed: list[RoutedLink] = []
    unrouted: list[UnroutedLink] = []
    conductors: list[NewConductor] = []
    # Pin pairs already attempted and refused. A retry would be handed the identical board
    # and give the identical answer, so this is both the loop's progress guarantee and the
    # reason it cannot spin: every iteration either routes something (shrinking the
    # ratsnest) or adds a pair here, and pairs are finite.
    refused: set[tuple[PhysicalPinRef, PhysicalPinRef]] = set()

    while True:
        current = next((n for n in ratsnest(working, lookup) if n.net_id == net_id), None)
        if current is None:
            break
        # Shortest first: a short hop has few alternatives and should claim its ground
        # before a long run, which has many, spends the same board getting there.
        pending = sorted(
            (link for link in current.links if (link.a, link.b) not in refused),
            key=lambda item: (item.length_mm, item.a, item.b),
        )
        if not pending:
            break
        link = pending[0]

        result = route_connection(
            working,
            lookup,
            RouteRequest(
                from_=link.from_,
                to=link.to,
                net_id=link.net_id,
                net_holes=current.pin_holes,
                # Only the caller knows which component's pin this is, and a lead bend has
                # to name whose leg it folds.
                from_pin=_pin_identity(working, link.a),
            ),
            options.router,
        )
        if not result.ok or result.best is None:
            refused.add((link.a, link.b))
            unrouted.append(
                UnroutedLink(link=link, reason=result.reason or "The router found no route.")
            )
            continue

        best = result.best
        staged, failure = _apply_candidate(working, best.conductors, ctx)
        if failure is not None:
            refused.add((link.a, link.b))
            unrouted.append(UnroutedLink(link=link, reason=failure))
            continue

        working = staged
        conductors.extend(best.conductors)
        routed.append(
            RoutedLink(
                link=link,
                strategy=best.strategy,
                cost=best.cost,
                explanation=best.explanation,
                risk_holes=best.risk_holes,
                conductors=best.conductors,
            )
        )

    return _NetPlan(
        document=working,
        conductors=tuple(conductors),
        routed=tuple(routed),
        unrouted=tuple(unrouted),
    )


def _rail_net(
    doc: PerfDocument,
    lookup: FootprintLookup,
    net_id: NetId,
    entry: NetRatsnest,
    options: AutorouteOptions,
    ctx: CommandContext,
) -> _NetPlan | None:
    """Lay one rail along the axis most of this net's pins already share, then chain the rest.

    This is PLAN.md Sec 6.2's bus strategy, and it is how a person wires ground on
    perfboard: run a trace along the row, and let every pin on that row get soldered into
    it on the way past. Returns None when there is no rail to build -- too few pins left,
    no shared axis, or the router could not produce a *trace* along it (a wire would only
    join the two ends, which is not a rail at all).
    """
    if entry.group_count < options.rail_min_fanout:
        return None
    axis = _best_rail_axis(entry.pin_holes)
    if axis is None:
        return None
    start, end = axis

    result = route_connection(
        doc,
        lookup,
        RouteRequest(
            from_=start, to=end, net_id=net_id, net_holes=entry.pin_holes,
            from_pin=_pin_identity(doc, entry.links[0].a) if entry.links else None,
        ),
        options.router,
    )
    if not result.ok:
        return None
    # Deliberately not `result.best`: the cheapest candidate for a long run is usually a
    # bare wire, which contacts only its ends. `alternatives` exists for exactly this
    # ("use a wire instead", read backwards), so the rail picks the cheapest candidate that
    # actually contacts what it crosses.
    rail = next((c for c in result.alternatives if c.strategy in RAIL_STRATEGIES), None)
    if rail is None:
        return None

    staged, failure = _apply_candidate(doc, rail.conductors, ctx)
    if failure is not None:
        return None

    rail_step = RoutedLink(
        link=_rail_link(entry, start, end, doc),
        strategy=rail.strategy,
        cost=rail.cost,
        explanation=f"{entry.net_name} rail: {rail.explanation}",
        risk_holes=rail.risk_holes,
        conductors=rail.conductors,
    )

    # Whatever the rail did not reach is ordinary work: stubs from the remaining pins to
    # the rail, which the chain strategy already handles (the rail is now copper on this
    # net, and a pin already on it is a legal endpoint).
    rest = _chain_net(staged, lookup, net_id, options, ctx)
    return _NetPlan(
        document=rest.document,
        conductors=rail.conductors + rest.conductors,
        routed=(rail_step,) + rest.routed,
        unrouted=rest.unrouted,
    )


def _best_rail_axis(pin_holes: tuple[HoleCoord, ...]) -> tuple[HoleCoord, HoleCoord] | None:
    """The two extreme pins of the row or column holding the most of this net's pins.

    Whichever line already carries the most pins is the one a rail collects the most from,
    which is the practical reading of PLAN.md Sec 6.2's "median axis". Rows are preferred
    over columns and lower indices over higher ones purely so the choice is reproducible.
    """
    if len(pin_holes) < 2:
        return None

    by_row: dict[int, list[HoleCoord]] = {}
    by_col: dict[int, list[HoleCoord]] = {}
    for hole in pin_holes:
        by_row.setdefault(hole.row, []).append(hole)
        by_col.setdefault(hole.col, []).append(hole)

    best: tuple[int, int, int, HoleCoord, HoleCoord] | None = None
    for is_column, groups, sort_key in (
        (0, by_row, lambda h: h.col),
        (1, by_col, lambda h: h.row),
    ):
        for index in sorted(groups):
            members = sorted(groups[index], key=sort_key)
            if len(members) < 2:
                continue
            # Negated count so more pins sorts first, then rows before columns, then the
            # lower line index.
            candidate = (-len(members), is_column, index, members[0], members[-1])
            if best is None or candidate[:3] < best[:3]:
                best = candidate

    if best is None:
        return None
    return best[3], best[4]


def _rail_link(
    entry: NetRatsnest, start: HoleCoord, end: HoleCoord, doc: PerfDocument
) -> RatsnestLink:
    """A link describing the rail's span, for the rail's ``RoutedLink`` to point at.

    A rail's two ends are usually NOT one of the ratsnest's own links: the spanning tree
    joins nearest neighbours, while a rail deliberately spans the whole row. So this
    describes the span the rail actually covers rather than misattributing it to a link
    the planner did not route.
    """
    return RatsnestLink(
        net_id=entry.net_id,
        net_name=entry.net_name,
        net_class=entry.net_class,
        a=_pin_label(entry, start),
        b=_pin_label(entry, end),
        from_=start,
        to=end,
        length_mm=path_length_mm((start, end), doc.board),
    )


def _pin_label(entry: NetRatsnest, hole: HoleCoord) -> PhysicalPinRef:
    """Which pin sits at a rail end, for reporting.

    The ratsnest carries holes and, separately, the pins of each link, so a pin name is
    recovered from whichever link touches this hole. When none does -- possible, since a
    rail end need not be a link end -- the hole reference stands in. This is a LABEL for a
    human and the build guide, never an identity anything resolves a pin by.
    """
    for link in entry.links:
        if link.from_ == hole:
            return link.a
        if link.to == hole:
            return link.b
    return PhysicalPinRef(component_ref=format_hole(hole), pin="")


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def _nets_to_route(doc: PerfDocument, only_net_ids: tuple[NetId, ...] | None) -> tuple[NetId, ...]:
    if only_net_ids is None:
        return tuple(net.id for net in doc.nets)
    wanted = set(only_net_ids)
    return tuple(net.id for net in doc.nets if net.id in wanted)


def _criticality_order(doc: PerfDocument, wanted: tuple[NetId, ...]) -> tuple[NetId, ...]:
    """Ground, then power, then descending fan-out, then name (PLAN.md Sec 6.2).

    Name last purely as a tie-break, so two same-class same-size nets always come out in
    the same order and a plan is reproducible.
    """
    by_id = {net.id: net for net in doc.nets}
    return tuple(
        sorted(
            wanted,
            key=lambda net_id: (
                NET_CLASS_PRIORITY.get(by_id[net_id].net_class, len(NET_CLASS_PRIORITY)),
                -len(by_id[net_id].nodes),
                by_id[net_id].name,
                net_id,
            ),
        )
    )


def _nets_to_promote(attempt: _Attempt) -> tuple[NetId, ...]:
    """Nets worth trying earlier: any that failed, or that settled for a fallback strategy."""
    return tuple(
        outcome.net_id
        for outcome in attempt.outcomes
        if outcome.unrouted
        or any(routed.strategy in FALLBACK_STRATEGIES for routed in outcome.routed)
    )


def _promote(order: tuple[NetId, ...], troubled: tuple[NetId, ...]) -> tuple[NetId, ...]:
    """Move the troubled nets to the front, preserving relative order within both halves."""
    promote = set(troubled)
    front = [net_id for net_id in order if net_id in promote]
    back = [net_id for net_id in order if net_id not in promote]
    return tuple(front + back)


def _rank(attempt: _Attempt) -> tuple[int, float, int]:
    """Comparison key for picking the best pass: fewest failures, then cheapest.

    Cost is the router's own quality metric and already prices fallback strategies and
    R5' proximity risk, so there is no need to weigh those separately here. The pass
    number breaks exact ties towards the earlier pass, which keeps the choice stable.
    """
    unrouted = sum(len(outcome.unrouted) for outcome in attempt.outcomes)
    cost = sum(routed.cost for outcome in attempt.outcomes for routed in outcome.routed)
    return (unrouted, cost, attempt.pass_number)


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------


def _to_plan(attempt: _Attempt, passes: int) -> AutoroutePlan:
    routed_links = [routed for outcome in attempt.outcomes for routed in outcome.routed]
    worked_on = [outcome for outcome in attempt.outcomes if outcome.routed or outcome.unrouted]

    summary = AutorouteSummary(
        nets_considered=len(attempt.outcomes),
        nets_closed=sum(1 for outcome in attempt.outcomes if outcome.closed),
        links_routed=len(routed_links),
        links_unrouted=sum(len(outcome.unrouted) for outcome in attempt.outcomes),
        total_cost=sum(routed.cost for routed in routed_links),
        fallback_links=sum(1 for routed in routed_links if routed.strategy in FALLBACK_STRATEGIES),
        risk_holes=sum(len(routed.risk_holes) for routed in routed_links),
        passes=passes,
    )
    return AutoroutePlan(
        document=attempt.document,
        conductors=attempt.conductors,
        nets=attempt.outcomes,
        summary=summary,
        label=_label(worked_on, summary),
    )


def _label(worked_on: list[NetOutcome], summary: AutorouteSummary) -> str:
    """The undo-stack entry. Names the net when there is only one, because "Route GND" is
    what the user just asked for and "Autoroute 1 net" is a worse answer to it."""
    connections = f"{summary.links_routed} connection{'' if summary.links_routed == 1 else 's'}"
    if len(worked_on) == 1:
        return f"Route {worked_on[0].net_name} ({connections})"
    return f"Autoroute {len(worked_on)} nets ({connections})"


def _empty_plan(doc: PerfDocument, only_net_ids: tuple[NetId, ...] | None) -> AutoroutePlan:
    """No nets matched -- an empty netlist, or a net id that is not in the document."""
    reason = "no matching nets" if only_net_ids is not None else "no netlist imported"
    return AutoroutePlan(
        document=doc,
        conductors=(),
        nets=(),
        summary=AutorouteSummary(
            nets_considered=0,
            nets_closed=0,
            links_routed=0,
            links_unrouted=0,
            total_cost=0.0,
            fallback_links=0,
            risk_holes=0,
            passes=0,
        ),
        label=f"Autoroute ({reason})",
    )


# ---------------------------------------------------------------------------
# Reporting helpers, for a status line or the build guide
# ---------------------------------------------------------------------------


def describe(plan: AutoroutePlan) -> str:
    """One line for a status bar. Says what failed, because a plan that quietly reports
    only its successes is the exact failure mode PLAN.md Sec 13 warns about."""
    s = plan.summary
    if s.nets_considered == 0:
        return plan.label
    parts = [
        f"{s.links_routed} connection(s) routed across {s.nets_closed}/{s.nets_considered} nets"
    ]
    if s.links_unrouted:
        parts.append(f"{s.links_unrouted} could NOT be routed")
    if s.fallback_links:
        parts.append(f"{s.fallback_links} needed an insulated wire or jumper")
    if s.risk_holes:
        parts.append(f"{s.risk_holes} pad(s) to measure for isolation")
    if s.passes > 1:
        parts.append(f"{s.passes} ordering passes")
    return " · ".join(parts)


def unrouted_links(plan: AutoroutePlan) -> tuple[UnroutedLink, ...]:
    """Every connection the planner could not make, flattened."""
    return tuple(item for outcome in plan.nets for item in outcome.unrouted)


def risk_holes(plan: AutoroutePlan) -> tuple[HoleCoord, ...]:
    """Every R5' proximity hole the plan would create, for the 2D overlay and the guide's
    isolation checklist. Deduplicated, since two routes can flag the same pad."""
    seen: dict[tuple[int, int], HoleCoord] = {}
    for outcome in plan.nets:
        for routed in outcome.routed:
            for hole in routed.risk_holes:
                seen.setdefault((hole.col, hole.row), hole)
    return tuple(seen[key] for key in sorted(seen))
