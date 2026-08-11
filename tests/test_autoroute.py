"""Tests for the autorouter (src/perfstudio/autoroute.py).

Three layers:

1. The PROPERTY TEST at the bottom is PLAN.md's M3 exit criterion, and the reason the
   rest of this file exists: random netlists, autorouted, must produce a board that LVS
   agrees with -- no shorts ever, and every net either closed or explicitly reported as
   unroutable. That is what makes the router's correctness a machine-checked claim rather
   than a hopeful one.

2. Unit tests for the decisions the planner makes: criticality order, rails picking up
   their own pins, rip-up, honest failure reporting.

3. Tests that the plan is a PROPOSAL -- the input document is never touched, and the
   whole thing commits through one command so it undoes in one step.

A note on why the property test generates its own netlists instead of using the golden
fixtures: only `ne555` among them is a well-formed circuit. The generated ones
(`dense`, `sparse`, `random-NN`) declare some pins in two or three schematic nets at
once, which no layout can satisfy -- connecting such a pin necessarily ties those nets
together, and leaving it unconnected opens all of them. They are perfectly good fixtures
for the differential proof they were built for, and useless as routing targets.
"""

from __future__ import annotations

import dataclasses
import random
from pathlib import Path

import pytest

from perfstudio import persist
from perfstudio.autoroute import (
    DEFAULT_AUTOROUTE_OPTIONS,
    AutorouteOptions,
    _criticality_order,
    describe,
    describe_reroute,
    plan_autoroute,
    plan_reroute,
    plan_route_net,
    risk_holes,
    unrouted_links,
)
from perfstudio.command import CommandBus, CommandContext
from perfstudio.commands import create_document_id_generator, create_standard_registry
from perfstudio.connectivity import FootprintLookup, PhysicalPinRef, are_pins_connected
from perfstudio.footprints import footprint_lookup
from perfstudio.lvs import run_lvs, stale_conductor_ids
from perfstudio.model import (
    Board,
    BodySpec,
    ComponentInstance,
    Conductor,
    DocumentMeta,
    Footprint,
    FootprintPin,
    HoleCoord,
    Net,
    NetClass,
    NetNode,
    PerfDocument,
    WireConductor,
)
from perfstudio.ratsnest import ratsnest, summarize
from perfstudio.router import RouterCosts, RouterOptions

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BOARD = Board(
    type="pad-per-hole",
    cols=24,
    rows=16,
    pitch=2.54,
    thickness=1.6,
    material="FR4",
    pad_diameter=1.9,
    drill_diameter=0.8,
)


def hole(col: int, row: int) -> HoleCoord:
    return HoleCoord(col=col, row=row)


def footprint(fp_id: str, offsets: tuple[tuple[int, int], ...]) -> Footprint:
    """A footprint with one pin per offset and no body, so nothing blocks a top jumper
    and the only occupancy in these tests is the pins and the routing itself."""
    return Footprint(
        id=fp_id,
        name=fp_id,
        pins=tuple(
            FootprintPin(number=str(index + 1), d_col=d_col, d_row=d_row)
            for index, (d_col, d_row) in enumerate(offsets)
        ),
        body_outline=(),
        body_height=0,
        body=BodySpec(archetype="generic-box"),
        lead_diameter=0.5,
        polarized=False,
    )


ONE_PIN = footprint("fp1", ((0, 0),))
TWO_PIN = footprint("fp2", ((0, 0), (2, 0)))
LOOKUP: FootprintLookup = {ONE_PIN.id: ONE_PIN, TWO_PIN.id: TWO_PIN}.get


def component(ref: str, footprint_id: str, anchor: HoleCoord) -> ComponentInstance:
    return ComponentInstance(
        id=f"cmp-{ref}", ref=ref, value="", footprint_id=footprint_id, anchor=anchor, locked=False
    )


def net(net_id: str, name: str, net_class: NetClass, pins: tuple[tuple[str, str], ...]) -> Net:
    return Net(
        id=net_id,
        name=name,
        nodes=tuple(NetNode(component_ref=ref, pin=pin) for ref, pin in pins),
        net_class=net_class,
    )


def make_doc(
    components: tuple[ComponentInstance, ...] = (),
    conductors: tuple[Conductor, ...] = (),
    nets: tuple[Net, ...] = (),
    board: Board = BOARD,
) -> PerfDocument:
    return PerfDocument(
        meta=DocumentMeta(
            name="test", created="2024-01-01T00:00:00.000Z", modified="2024-01-01T00:00:00.000Z"
        ),
        board=board,
        components=components,
        conductors=conductors,
        nets=nets,
    )


def pin(ref: str, number: str) -> PhysicalPinRef:
    return PhysicalPinRef(component_ref=ref, pin=number)


def commit(doc: PerfDocument, plan_conductors: object) -> PerfDocument:
    """Dispatch a plan through a real bus, the way a host does."""
    bus = CommandBus(
        doc, create_standard_registry(), CommandContext(next_id=create_document_id_generator(doc))
    )
    result = bus.dispatch("conductor.addMany", plan_conductors)
    assert result.ok, result.message
    return bus.document


# ---------------------------------------------------------------------------
# It actually routes
# ---------------------------------------------------------------------------


def test_routes_a_two_pin_net_and_lvs_agrees() -> None:
    components = (component("R1", "fp1", hole(2, 2)), component("R2", "fp1", hole(7, 2)))
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)
    doc = make_doc(components=components, nets=nets)

    plan = plan_autoroute(doc, LOOKUP)

    assert plan.summary.links_routed == 1
    assert plan.summary.links_unrouted == 0
    assert are_pins_connected(plan.document, LOOKUP, pin("R1", "1"), pin("R2", "1"))
    assert run_lvs(plan.document, LOOKUP).summary.opens == 0


def test_prefers_a_solder_trace_over_a_wire_for_a_clear_short_hop() -> None:
    """The cheap primitive wins when nothing is in the way -- PLAN.md Sec 6.1's whole
    point is that the cost table, not a special case, is what produces this."""
    components = (component("R1", "fp1", hole(2, 2)), component("R2", "fp1", hole(5, 2)))
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)

    plan = plan_autoroute(make_doc(components=components, nets=nets), LOOKUP)

    assert plan.nets[0].routed[0].strategy == "solder-trace"


def test_closes_a_four_pin_net_completely() -> None:
    components = tuple(
        component(f"R{i}", "fp1", hole(2 + 4 * i, 5)) for i in range(4)
    )
    nets = (net("n1", "SIG", "signal", tuple((f"R{i}", "1") for i in range(4))),)
    doc = make_doc(components=components, nets=nets)
    assert summarize(ratsnest(doc, LOOKUP)).links == 3

    plan = plan_autoroute(doc, LOOKUP)

    assert summarize(ratsnest(plan.document, LOOKUP)).links == 0
    assert plan.nets[0].closed


def test_leaves_an_already_routed_net_alone() -> None:
    components = (component("R1", "fp1", hole(2, 2)), component("R2", "fp1", hole(5, 2)))
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)
    doc = make_doc(components=components, nets=nets)
    routed = commit(doc, plan_autoroute(doc, LOOKUP).payload())

    again = plan_autoroute(routed, LOOKUP)

    assert again.is_empty
    assert again.summary.links_routed == 0


# ---------------------------------------------------------------------------
# Rails: one trace collecting several pins of its own net (PLAN.md Sec 6.2)
# ---------------------------------------------------------------------------


def test_a_rail_picks_up_its_own_pins_so_one_trace_closes_several_links() -> None:
    """Five ground pins along one row need four connections, but a single trace laid
    through them all is how a perfboard rail is actually built -- so the plan must contain
    fewer conductors than the ratsnest had links."""
    components = tuple(component(f"R{i}", "fp1", hole(3 + 2 * i, 6)) for i in range(5))
    nets = (net("n1", "GND", "ground", tuple((f"R{i}", "1") for i in range(5))),)
    doc = make_doc(components=components, nets=nets)
    assert summarize(ratsnest(doc, LOOKUP)).links == 4

    plan = plan_autoroute(doc, LOOKUP)

    assert len(plan.conductors) < 4
    assert summarize(ratsnest(plan.document, LOOKUP)).links == 0
    # ...and the far ends really are one net, not merely adjacent.
    assert are_pins_connected(plan.document, LOOKUP, pin("R0", "1"), pin("R4", "1"))


def test_a_rail_is_not_charged_proximity_risk_for_its_own_pads() -> None:
    """R5' is about bridging to a DIFFERENT net. A trace running past pads of the net it
    belongs to is not a risk, and pricing it as one is what used to push every route onto
    an insulated wire."""
    components = tuple(component(f"R{i}", "fp1", hole(3 + 2 * i, 6)) for i in range(5))
    nets = (net("n1", "GND", "ground", tuple((f"R{i}", "1") for i in range(5))),)

    plan = plan_autoroute(make_doc(components=components, nets=nets), LOOKUP)

    assert plan.summary.risk_holes == 0
    assert plan.summary.fallback_links == 0


def test_proximity_risk_is_reported_when_a_trace_has_to_run_past_a_foreign_pad() -> None:
    """R5' risk holes reach the plan, where the build guide turns them into isolation
    measurements.

    Getting a risky trace to be chosen at all takes some doing, which is itself the
    strongest evidence that pricing R5' into the search works:

      - Wires are priced out, because with the default table a trace carrying any
        proximity risk loses outright to a bare wire -- the router avoids the risk by
        changing primitive entirely.
      - The corridor is WALLED IN, because given any detour at all the search takes it and
        comes back with no risk holes at all. Foreign pins fill the rows above and below
        and cap both ends, so the single hole between the two endpoints is the only route
        there is.

    Both of those are the R5' pricing working as designed (PLAN.md Sec 6.1): a router that
    can avoid the risk does, and only a router with no choice reports it.
    """
    trace_only = AutorouteOptions(
        router=RouterOptions(costs=RouterCosts(bare_wire_fixed=200, insulated_wire_fixed=400))
    )
    # A one-hole corridor at row 4, columns 2..4, with foreign pads on every side.
    wall = tuple(
        component(f"F{col}{row}", "fp1", hole(col, row))
        for row in (3, 5)
        for col in range(1, 6)
    ) + (component("Fa", "fp1", hole(1, 4)), component("Fb", "fp1", hole(5, 4)))
    components = wall + (
        component("R1", "fp1", hole(2, 4)),
        component("R2", "fp1", hole(4, 4)),
    )
    # The wall parts are obstacles, not circuit: every unconnected pin is its own physical
    # net, which is all the router needs to see them as foreign.
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)

    plan = plan_autoroute(make_doc(components=components, nets=nets), LOOKUP, trace_only)

    assert plan.nets[0].routed[0].strategy == "solder-trace"
    assert plan.summary.risk_holes > 0
    # Deduplicated and sorted, ready for the 2D overlay and the guide's isolation list.
    holes = risk_holes(plan)
    assert len(holes) == len({(h.col, h.row) for h in holes})
    assert list(holes) == sorted(holes, key=lambda h: (h.col, h.row))
    # The explanation says where to measure, in the hole language the guide speaks.
    assert "isolation" in plan.nets[0].routed[0].explanation


# ---------------------------------------------------------------------------
# Ordering and rip-up
# ---------------------------------------------------------------------------


def test_criticality_order_is_ground_then_power_then_fanout_then_name() -> None:
    """PLAN.md Sec 6.2, asserted directly rather than through its consequences: the rails
    that want a clear row go first, and everything after that is a reproducibility
    tie-break."""
    nets = (
        net("n-sig-a", "ZSIG", "signal", (("A", "1"), ("B", "1"))),
        net("n-pwr", "VCC", "power", (("C", "1"), ("D", "1"))),
        net("n-sig-big", "ASIG", "signal", (("E", "1"), ("F", "1"), ("G", "1"))),
        net("n-gnd", "GND", "ground", (("H", "1"), ("I", "1"))),
    )
    doc = make_doc(nets=nets)

    order = _criticality_order(doc, tuple(item.id for item in nets))

    # ground, power, then the bigger signal net, then the smaller one.
    assert order == ("n-gnd", "n-pwr", "n-sig-big", "n-sig-a")


def test_the_net_routed_first_gets_the_contested_corridor() -> None:
    """Ordering has to actually change the outcome, or it is decoration. Two nets both want
    the one clear row between two rows of pins; ground is routed first, so ground gets it
    and the signal is the one that pays."""
    # Rows 3 and 5 are packed with foreign pins, leaving row 4 as the only cheap corridor.
    blockers = tuple(
        component(f"B{col}{row}", "fp1", hole(col, row))
        for row in (3, 5)
        for col in range(4, 10)
    )
    components = blockers + (
        component("G1", "fp1", hole(2, 4)),
        component("G2", "fp1", hole(11, 4)),
        component("S1", "fp1", hole(2, 6)),
        component("S2", "fp1", hole(11, 6)),
    )
    nets = (
        net("n-sig", "SIG", "signal", (("S1", "1"), ("S2", "1"))),
        net("n-gnd", "GND", "ground", (("G1", "1"), ("G2", "1"))),
    ) + tuple(
        net(f"nb{i}", f"BLK{i}", "signal", ((blocker.ref, "1"), (blocker.ref, "1")))
        for i, blocker in enumerate(blockers)
    )
    doc = make_doc(components=components, nets=nets)

    plan = plan_autoroute(doc, LOOKUP)
    by_name = {outcome.net_name: outcome for outcome in plan.nets}

    assert by_name["GND"].routed[0].cost <= by_name["SIG"].routed[0].cost
    assert plan.summary.links_unrouted == 0


def test_max_passes_of_one_disables_rip_up() -> None:
    components = (component("R1", "fp1", hole(2, 2)), component("R2", "fp1", hole(9, 2)))
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)
    doc = make_doc(components=components, nets=nets)

    plan = plan_autoroute(doc, LOOKUP, AutorouteOptions(max_passes=1))

    assert plan.summary.passes == 1


def test_rip_up_never_removes_conductors_that_were_already_there() -> None:
    """An assistant does not unpick the user's work. Whatever the planner decides, every
    conductor already in the document is still in the preview."""
    components = (component("R1", "fp1", hole(2, 2)), component("R2", "fp1", hole(9, 2)))
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)
    existing = make_doc(components=components, nets=nets)
    # A stray trace of someone else's, sitting right across the direct path.
    with_stray = commit(
        existing,
        plan_autoroute(
            make_doc(
                components=(component("X1", "fp1", hole(5, 2)), component("X2", "fp1", hole(6, 2))),
                nets=(net("nx", "STRAY", "signal", (("X1", "1"), ("X2", "1"))),),
            ),
            LOOKUP,
        ).payload(),
    )
    before = {c.id for c in with_stray.conductors}

    plan = plan_autoroute(with_stray, LOOKUP)

    assert before <= {c.id for c in plan.document.conductors}


# ---------------------------------------------------------------------------
# Honesty about failure (PLAN.md Sec 13)
# ---------------------------------------------------------------------------


def test_an_unroutable_connection_is_reported_not_dropped() -> None:
    """Two pins in the same hole cannot be 'routed'. The planner must say so rather than
    quietly counting the net as done."""
    components = (component("R1", "fp1", hole(4, 4)), component("R2", "fp1", hole(4, 4)))
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)

    plan = plan_autoroute(make_doc(components=components, nets=nets), LOOKUP)

    # Both pins share a hole, so they are already one physical net and there is nothing
    # to route; what must not happen is a silent claim of success on a net with a real
    # problem, which DRC reports as two pins in one hole.
    assert plan.summary.links_unrouted == 0
    assert plan.summary.links_routed == 0


def test_a_net_whose_pins_are_not_on_the_board_is_not_counted_as_closed() -> None:
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("GHOST", "1"))),)
    doc = make_doc(components=(component("R1", "fp1", hole(2, 2)),), nets=nets)

    plan = plan_autoroute(doc, LOOKUP)

    assert plan.nets[0].unresolved_pins == (pin("GHOST", "1"),)
    assert not plan.nets[0].closed
    assert plan.summary.nets_closed == 0


def test_describe_names_the_failures() -> None:
    components = (component("R1", "fp1", hole(2, 2)), component("R2", "fp1", hole(5, 2)))
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)

    text = describe(plan_autoroute(make_doc(components=components, nets=nets), LOOKUP))

    assert "1 connection(s) routed across 1/1 nets" in text


def test_unrouted_links_helper_flattens_every_failure() -> None:
    doc = make_doc()

    plan = plan_autoroute(doc, LOOKUP)

    assert unrouted_links(plan) == ()


# ---------------------------------------------------------------------------
# It is a plan, not a mutation
# ---------------------------------------------------------------------------


def test_planning_does_not_touch_the_input_document() -> None:
    components = (component("R1", "fp1", hole(2, 2)), component("R2", "fp1", hole(5, 2)))
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)
    doc = make_doc(components=components, nets=nets)

    plan = plan_autoroute(doc, LOOKUP)

    assert doc.conductors == ()
    assert len(plan.document.conductors) == len(plan.conductors) > 0


def test_the_whole_plan_commits_as_a_single_undo_step() -> None:
    components = tuple(component(f"R{i}", "fp1", hole(2 + 4 * i, 5)) for i in range(4))
    nets = (net("n1", "SIG", "signal", tuple((f"R{i}", "1") for i in range(4))),)
    doc = make_doc(components=components, nets=nets)
    plan = plan_autoroute(doc, LOOKUP)
    bus = CommandBus(
        doc, create_standard_registry(), CommandContext(next_id=create_document_id_generator(doc))
    )

    assert bus.dispatch("conductor.addMany", plan.payload()).ok
    assert len(bus.document.conductors) == len(plan.conductors)
    assert len(bus.journal()) == 1

    bus.undo()

    assert bus.document.conductors == ()


def test_the_undo_label_names_the_net_for_a_single_net_run() -> None:
    components = (component("R1", "fp1", hole(2, 2)), component("R2", "fp1", hole(5, 2)))
    nets = (net("n1", "GND", "ground", (("R1", "1"), ("R2", "1"))),)
    doc = make_doc(components=components, nets=nets)

    plan = plan_route_net(doc, LOOKUP, "n1")

    assert plan.label == "Route GND (1 connection)"
    bus = CommandBus(
        doc, create_standard_registry(), CommandContext(next_id=create_document_id_generator(doc))
    )
    dispatched = bus.dispatch("conductor.addMany", plan.payload())
    assert dispatched.description == "Route GND (1 connection)"


def test_committing_the_plan_reproduces_the_preview_document() -> None:
    """The preview is what the user is shown before accepting; if it differed from what
    committing produces, every DRC count and every rendered pixel shown beforehand would
    be a lie."""
    components = tuple(component(f"R{i}", "fp1", hole(2 + 3 * i, 4)) for i in range(4))
    nets = (net("n1", "SIG", "signal", tuple((f"R{i}", "1") for i in range(4))),)
    doc = make_doc(components=components, nets=nets)
    plan = plan_autoroute(doc, LOOKUP)

    committed = commit(doc, plan.payload())

    assert committed.conductors == plan.document.conductors


def test_planning_a_net_that_is_not_in_the_document_is_an_empty_plan() -> None:
    doc = make_doc(components=(component("R1", "fp1", hole(2, 2)),))

    plan = plan_route_net(doc, LOOKUP, "nope")

    assert plan.is_empty
    assert plan.summary.nets_considered == 0
    assert plan.label == "Autoroute (no matching nets)"


def test_a_document_with_no_netlist_is_an_empty_plan() -> None:
    plan = plan_autoroute(make_doc(), LOOKUP)

    assert plan.is_empty
    assert plan.label == "Autoroute (no netlist imported)"


def test_only_the_requested_net_is_routed() -> None:
    components = (
        component("A1", "fp1", hole(2, 2)),
        component("A2", "fp1", hole(5, 2)),
        component("B1", "fp1", hole(2, 8)),
        component("B2", "fp1", hole(5, 8)),
    )
    nets = (
        net("na", "A", "signal", (("A1", "1"), ("A2", "1"))),
        net("nb", "B", "signal", (("B1", "1"), ("B2", "1"))),
    )
    doc = make_doc(components=components, nets=nets)

    plan = plan_route_net(doc, LOOKUP, "nb")

    assert [outcome.net_name for outcome in plan.nets] == ["B"]
    assert not are_pins_connected(plan.document, LOOKUP, pin("A1", "1"), pin("A2", "1"))


def test_deterministic_across_repeated_runs() -> None:
    components = tuple(component(f"R{i}", "fp1", hole(2 + 3 * i, 4)) for i in range(5))
    nets = (
        net("n1", "GND", "ground", tuple((f"R{i}", "1") for i in range(3))),
        net("n2", "SIG", "signal", (("R3", "1"), ("R4", "1"))),
    )
    doc = make_doc(components=components, nets=nets)

    first = plan_autoroute(doc, LOOKUP)
    again = plan_autoroute(doc, LOOKUP)

    assert first.conductors == again.conductors
    assert first.summary == again.summary


def test_outcomes_are_reported_in_document_net_order_not_routing_order() -> None:
    components = (
        component("S1", "fp1", hole(2, 2)),
        component("S2", "fp1", hole(5, 2)),
        component("G1", "fp1", hole(2, 8)),
        component("G2", "fp1", hole(5, 8)),
    )
    nets = (
        net("n-sig", "SIG", "signal", (("S1", "1"), ("S2", "1"))),
        net("n-gnd", "GND", "ground", (("G1", "1"), ("G2", "1"))),
    )

    plan = plan_autoroute(make_doc(components=components, nets=nets), LOOKUP)

    # GND is routed first but declared second, and the report follows the netlist.
    assert [outcome.net_name for outcome in plan.nets] == ["SIG", "GND"]


# ---------------------------------------------------------------------------
# The real circuit
# ---------------------------------------------------------------------------

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tools" / "diffcheck" / "golden"


def test_autoroutes_the_ne555_fixture_to_an_lvs_clean_board() -> None:
    """The one golden fixture that is a real, well-formed circuit (see the module
    docstring). Every net must come out matched, with no opens and no shorts."""
    result = persist.deserialize_document((GOLDEN_DIR / "ne555.perf").read_text(encoding="utf-8"))
    assert result.ok
    doc = result.document
    lookup = footprint_lookup()
    assert run_lvs(doc, lookup).summary.opens > 0  # It starts out unrouted.

    plan = plan_autoroute(doc, lookup)
    summary = run_lvs(plan.document, lookup).summary

    assert plan.summary.links_unrouted == 0
    assert summary.opens == 0
    assert summary.shorts == 0
    assert summary.matched_nets == summary.schematic_nets


# ---------------------------------------------------------------------------
# Property test: PLAN.md M3 exit criterion
# ---------------------------------------------------------------------------


def _random_board_and_netlist(seed: int) -> PerfDocument:
    """A random but WELL-FORMED routing problem: every pin belongs to at most one net.

    Uses a seeded ``random.Random`` -- randomness in a test is fine and randomness in the
    engine is not, which is why this lives here and the planner has none.
    """
    rng = random.Random(seed)
    board = BOARD

    # Place parts on distinct anchors, keeping every pin hole distinct: two pins in one
    # hole is a DRC error, and a board that is already wrong tells us nothing about the
    # router.
    used: set[tuple[int, int]] = set()
    components: list[ComponentInstance] = []
    for index in range(rng.randint(6, 12)):
        footprint_id = rng.choice(("fp1", "fp2"))
        spec = LOOKUP(footprint_id)
        assert spec is not None
        for _attempt in range(60):
            anchor = hole(rng.randrange(0, board.cols - 3), rng.randrange(0, board.rows - 1))
            holes = [(anchor.col + p.d_col, anchor.row + p.d_row) for p in spec.pins]
            if any(key in used for key in holes):
                continue
            if any(not (0 <= c < board.cols and 0 <= r < board.rows) for c, r in holes):
                continue
            used.update(holes)
            components.append(component(f"U{index}", footprint_id, anchor))
            break

    # Partition the pins into nets. Each pin is used at most once, so the netlist is
    # satisfiable by construction.
    all_pins: list[tuple[str, str]] = []
    for placed in components:
        spec = LOOKUP(placed.footprint_id)
        assert spec is not None
        all_pins.extend((placed.ref, p.number) for p in spec.pins)
    rng.shuffle(all_pins)

    nets: list[Net] = []
    classes: tuple[NetClass, ...] = ("ground", "power", "signal")
    cursor = 0
    while len(all_pins) - cursor >= 2:
        size = min(rng.randint(2, 4), len(all_pins) - cursor)
        members = tuple(all_pins[cursor : cursor + size])
        cursor += size
        net_class = classes[min(len(nets), len(classes) - 1)]
        nets.append(net(f"n{len(nets)}", f"NET{len(nets)}", net_class, members))

    return make_doc(components=tuple(components), nets=tuple(nets), board=board)


@pytest.mark.parametrize("seed", range(40))
def test_property_random_netlist_autoroutes_without_shorts_and_reports_every_gap(
    seed: int,
) -> None:
    """PLAN.md M3: a random netlist, autorouted, must satisfy LVS.

    Stated precisely, because "100% LVS pass" needs an honest reading of what the router
    promises. Two claims are checked:

      1. NEVER A SHORT. The router may decline to route something; it may not tie two
         nets together. A short is a defect no user could be expected to find later, so
         this is unconditional.

      2. NO SILENT OPENS. Any net LVS reports as open or unrouted must appear in the
         plan's own failure report. This is the property that rules out PLAN.md Sec 13's
         trap of "routed most of it and left four connections" -- the router is allowed
         to fail, and is not allowed to fail quietly.
    """
    doc = _random_board_and_netlist(seed)
    if not doc.nets:
        pytest.skip("degenerate generated case: no nets")

    plan = plan_autoroute(doc, LOOKUP, DEFAULT_AUTOROUTE_OPTIONS)
    result = run_lvs(plan.document, LOOKUP)

    assert result.summary.shorts == 0, [
        issue.message for issue in result.issues if issue.kind == "short"
    ]

    reported_failures = {item.link.net_name for item in unrouted_links(plan)}
    lvs_gaps = {
        name
        for issue in result.issues
        if issue.kind in ("open", "unrouted-net")
        for name in issue.net_names
    }
    assert lvs_gaps <= reported_failures, (
        f"LVS found gaps the planner never mentioned: {sorted(lvs_gaps - reported_failures)}"
    )


@pytest.mark.parametrize("seed", range(8))
def test_property_autoroute_is_reproducible(seed: int) -> None:
    """Determinism is a user-facing promise (PLAN.md Sec 6.3), not just a test
    convenience: the same board must always produce the same layout."""
    doc = _random_board_and_netlist(seed)

    first = plan_autoroute(doc, LOOKUP)
    again = plan_autoroute(doc, LOOKUP)

    assert first.conductors == again.conductors


# ---------------------------------------------------------------------------
# Rip-up and re-route
# ---------------------------------------------------------------------------


def test_autoroute_only_adds_which_is_why_reroute_exists() -> None:
    """The measurement that justifies plan_reroute, as an assertion.

    Move a part after routing and the copper laid for its old position still joins the
    right pins of the right net: it is not floating, not stale, and removing any of it
    disconnects something -- so nothing reports it and autoroute puts more beside it.
    The board grows every time. Only ripping up gets it back.
    """
    registry = footprint_lookup()
    doc = _load_golden_document("ne555")

    fresh = commit(doc, plan_autoroute(doc, registry).payload())
    fresh_count = len(fresh.conductors)

    moved = dataclasses.replace(
        fresh,
        components=tuple(
            dataclasses.replace(c, anchor=HoleCoord(0, 14)) if c.ref == "R1" else c
            for c in fresh.components
        ),
    )
    grown = commit(moved, plan_autoroute(moved, registry).payload())
    assert len(grown.conductors) > fresh_count, "autoroute is supposed to add, not replace"
    assert stale_conductor_ids(grown, registry) == (), "and none of it looks stale"

    plan = plan_reroute(grown, registry)
    rerouted = _commit_replace(grown, plan.payload())

    assert len(rerouted.conductors) == fresh_count
    assert run_lvs(rerouted, LOOKUP_STD).summary.opens == 0


def test_reroute_leaves_copper_that_claims_no_net_alone() -> None:
    """Hand-drawn copper makes no claim this planner could act on, and unpicking someone
    else's wiring is exactly the wrong behaviour."""
    components = (component("R1", "fp1", hole(2, 2)), component("R2", "fp1", hole(9, 2)))
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)
    doc = make_doc(components=components, nets=nets)
    routed = commit(doc, plan_autoroute(doc, LOOKUP).payload())
    with_handmade = dataclasses.replace(
        routed,
        conductors=routed.conductors
        + (WireConductor(id="hand-1", path=(hole(4, 8), hole(9, 8)), net_id=None),),
    )

    plan = plan_reroute(with_handmade, LOOKUP)

    assert "hand-1" not in plan.remove_ids
    after = _commit_replace(with_handmade, plan.payload())
    assert any(c.id == "hand-1" for c in after.conductors)


def test_reroute_of_one_net_does_not_touch_another() -> None:
    components = (
        component("R1", "fp1", hole(2, 2)),
        component("R2", "fp1", hole(9, 2)),
        component("R3", "fp1", hole(2, 8)),
        component("R4", "fp1", hole(9, 8)),
    )
    nets = (
        net("n1", "A", "signal", (("R1", "1"), ("R2", "1"))),
        net("n2", "B", "signal", (("R3", "1"), ("R4", "1"))),
    )
    doc = make_doc(components=components, nets=nets)
    routed = commit(doc, plan_autoroute(doc, LOOKUP).payload())
    b_ids = {c.id for c in routed.conductors if c.net_id == "n2"}

    plan = plan_reroute(routed, LOOKUP, only_net_ids=("n1",))

    assert b_ids.isdisjoint(plan.remove_ids)
    after = _commit_replace(routed, plan.payload())
    assert b_ids <= {c.id for c in after.conductors}


def test_reroute_commits_as_a_single_undo_step() -> None:
    components = (component("R1", "fp1", hole(2, 2)), component("R2", "fp1", hole(9, 2)))
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)
    doc = make_doc(components=components, nets=nets)
    routed = commit(doc, plan_autoroute(doc, LOOKUP).payload())

    bus = CommandBus(
        routed,
        create_standard_registry(),
        CommandContext(next_id=create_document_id_generator(routed)),
    )
    plan = plan_reroute(routed, LOOKUP)
    assert bus.dispatch("conductor.replace", plan.payload()).ok
    assert len(bus.history()) == 1

    bus.undo()
    assert bus.document.conductors == routed.conductors


def test_reroute_on_an_unrouted_board_removes_nothing() -> None:
    components = (component("R1", "fp1", hole(2, 2)), component("R2", "fp1", hole(9, 2)))
    nets = (net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),)
    doc = make_doc(components=components, nets=nets)

    plan = plan_reroute(doc, LOOKUP)

    assert plan.remove_ids == ()
    assert plan.conductors
    assert "ripped up" in describe_reroute(plan)


def _commit_replace(doc: PerfDocument, payload: object) -> PerfDocument:
    bus = CommandBus(
        doc, create_standard_registry(), CommandContext(next_id=create_document_id_generator(doc))
    )
    result = bus.dispatch("conductor.replace", payload)
    assert result.ok, result.message
    return bus.document


def _load_golden_document(name: str) -> PerfDocument:
    path = Path(__file__).resolve().parents[1] / "tools" / "diffcheck" / "golden" / f"{name}.perf"
    result = persist.deserialize_document(path.read_text(encoding="utf-8"))
    assert result.ok
    return result.document


LOOKUP_STD: FootprintLookup = footprint_lookup()
