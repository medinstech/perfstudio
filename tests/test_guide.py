"""Tests for the soldering guide (src/perfstudio/guide.py, guide_export.py).

The guide is the output the project exists to produce, and it is the one place where an
engine mistake reaches a person holding a soldering iron. So the tests here are less
about "the function returns something" and more about the three claims PLAN.md Sec 3
makes on its behalf:

  IT SAYS WHERE          -- every placed part and every conductor gets exactly one step,
                            and no step is silently dropped.
  IT SAYS WHICH WAY ROUND -- polarity comes from the registry's own pin NAMES, because no
                            single convention about pin 1 covers an electrolytic (+), an
                            LED (anode) and a diode (cathode) at once. Getting this
                            backwards is the commonest way a finished board is dead, so
                            test_polarity_is_read_from_the_pin_name_not_a_convention
                            pins all three against the real registry.
  IT SAYS HOW TO CHECK    -- every R5' proximity warning DRC raises becomes a specific
                            isolation measurement. That join is the differentiator, and
                            test_every_proximity_risk_becomes_a_measurement is what makes
                            it a fact rather than an intention.

Plus the arithmetic nobody would notice being wrong: the wire cut formula, the trace
resistance quoted back as an expectation, and the phase a check lands in.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import json
from pathlib import Path

import pytest

from perfstudio import persist
from perfstudio.autoroute import plan_autoroute
from perfstudio.command import CommandBus, CommandContext
from perfstudio.commands import create_document_id_generator, create_standard_registry
from perfstudio.connectivity import FootprintLookup
from perfstudio.drc import run_drc, trace_electrical
from perfstudio.footprints import footprint_lookup
from perfstudio.geometry import format_hole
from perfstudio.guide import (
    DEFAULT_GUIDE_OPTIONS,
    PHASE_BY_ARCHETYPE,
    PHASE_BY_CONDUCTOR,
    PHASE_TITLES,
    ConductorStep,
    GuideOptions,
    PartStep,
    all_checkpoints,
    all_steps,
    build_guide,
    describe,
)
from perfstudio.guide_export import bom_to_csv, cut_list_to_csv, guide_to_html, guide_to_json
from perfstudio.model import (
    Board,
    BodyArchetype,
    ComponentInstance,
    Conductor,
    DocumentMeta,
    HoleCoord,
    Net,
    NetClass,
    NetNode,
    PerfDocument,
    SolderTraceConductor,
    SpineSpec,
    WireConductor,
)

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tools" / "diffcheck" / "golden"
REGISTRY: FootprintLookup = footprint_lookup()

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


def golden(name: str) -> PerfDocument:
    result = persist.deserialize_document((GOLDEN_DIR / f"{name}.perf").read_text(encoding="utf-8"))
    assert result.ok, result.message
    return result.document


def hole(col: int, row: int) -> HoleCoord:
    return HoleCoord(col=col, row=row)


def component(ref: str, footprint_id: str, anchor: HoleCoord, value: str = "") -> ComponentInstance:
    return ComponentInstance(
        id=f"cmp-{ref}", ref=ref, value=value, footprint_id=footprint_id, anchor=anchor
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
    name: str = "test-board",
) -> PerfDocument:
    return PerfDocument(
        meta=DocumentMeta(
            name=name, created="2024-01-01T00:00:00.000Z", modified="2024-01-01T00:00:00.000Z"
        ),
        board=board,
        components=components,
        conductors=conductors,
        nets=nets,
    )


def routed_ne555() -> PerfDocument:
    """The NE555 fixture with the autorouter's output committed.

    A guide for an unrouted board is a legitimate thing to produce and says so in its
    warnings, but it exercises none of the conductor half. This is the realistic input.
    """
    doc = golden("ne555")
    plan = plan_autoroute(doc, REGISTRY)
    bus = CommandBus(
        doc, create_standard_registry(), CommandContext(next_id=create_document_id_generator(doc))
    )
    result = bus.dispatch("conductor.addMany", plan.payload())
    assert result.ok, result.message
    return bus.document


# ---------------------------------------------------------------------------
# Nothing is dropped
# ---------------------------------------------------------------------------


def test_every_placed_part_and_every_conductor_gets_exactly_one_step() -> None:
    """A build guide that silently omits a connection is worse than no guide: the user
    follows it to the end and has a board that does not work, with nothing to say which
    part was never covered."""
    doc = routed_ne555()
    guide = build_guide(doc, REGISTRY)

    part_refs = sorted(step.ref for step in all_steps(guide) if isinstance(step, PartStep))
    known = sorted(c.ref for c in doc.components if REGISTRY(c.footprint_id) is not None)
    assert part_refs == known

    conductor_ids = sorted(
        step.conductor_id for step in all_steps(guide) if isinstance(step, ConductorStep)
    )
    assert conductor_ids == sorted(c.id for c in doc.conductors)


def test_a_part_with_an_unknown_footprint_is_named_rather_than_skipped_silently() -> None:
    doc = make_doc(components=(component("X9", "no-such-footprint", hole(3, 3)),))

    guide = build_guide(doc, REGISTRY)

    assert not [step for step in all_steps(guide) if isinstance(step, PartStep)]
    codes = {warning.code for warning in guide.warnings}
    assert "unknown-footprint" in codes
    assert any("X9" in warning.message for warning in guide.warnings)


def test_building_the_same_board_twice_gives_the_same_guide() -> None:
    doc = routed_ne555()
    assert build_guide(doc, REGISTRY) == build_guide(doc, REGISTRY)


def test_an_empty_document_produces_a_guide_that_says_so() -> None:
    guide = build_guide(make_doc(), REGISTRY)

    assert guide.total_steps == 0
    codes = {warning.code for warning in guide.warnings}
    assert "no-netlist" in codes
    assert "no-conductors" in codes


# ---------------------------------------------------------------------------
# It says which way round
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("footprint_id", "pin", "must_contain"),
    [
        # The registry names pin 1 '+' here, so the guide says positive at pin 1's hole.
        ("c-elec-d5-p2", "1", "Positive"),
        ("c-elec-d5-p2", "2", "Negative"),
        # ...and names an LED's pin 1 'A', the ANODE -- the opposite meaning for the same
        # pin number. Any rule of the form "pin 1 is the cathode" gets this backwards.
        ("led-5mm", "1", "Anode"),
        ("led-5mm", "2", "Cathode"),
    ],
)
def test_polarity_is_read_from_the_pin_name_not_a_convention(
    footprint_id: str, pin: str, must_contain: str
) -> None:
    doc = make_doc(components=(component("P1", footprint_id, hole(4, 4)),))
    step = next(s for s in all_steps(build_guide(doc, REGISTRY)) if isinstance(s, PartStep))

    assert step.polarity is not None
    at = dict(step.pin_holes)[pin]
    fragment = f"{must_contain}"
    assert fragment in step.polarity
    # The claim is about a specific hole, and the hole has to be the right one.
    section = next(part for part in step.polarity.split("; ") if fragment in part)
    assert format_hole(at) in section


def test_an_unnamed_polarised_part_falls_back_to_the_stated_convention() -> None:
    """A DO-41 diode's pins are unnamed, so the convention has to carry it -- pin 1 is the
    cathode, as this registry and KiCad's own DO-41 both have it. Stated in one place and
    tested, rather than assumed in three."""
    doc = make_doc(components=(component("D1", "d-do41", hole(4, 4)),))
    step = next(s for s in all_steps(build_guide(doc, REGISTRY)) if isinstance(s, PartStep))

    assert step.polarity is not None
    assert "Cathode" in step.polarity
    assert format_hole(dict(step.pin_holes)["1"]) in step.polarity


def test_a_dip_is_oriented_by_pin_one() -> None:
    doc = make_doc(components=(component("U1", "dip-8", hole(3, 3)),))
    step = next(s for s in all_steps(build_guide(doc, REGISTRY)) if isinstance(s, PartStep))

    assert step.polarity is not None and "Pin 1" in step.polarity
    assert "8 pins" in step.span  # Not "pin 1 to pin 8 is N holes apart", which is a diagonal.


def test_a_resistor_has_no_polarity_note() -> None:
    doc = make_doc(components=(component("R1", "r-axial-5", hole(4, 4)),))
    step = next(s for s in all_steps(build_guide(doc, REGISTRY)) if isinstance(s, PartStep))
    assert step.polarity is None


def test_an_axial_part_gets_a_lead_bend_template() -> None:
    doc = make_doc(components=(component("R1", "r-axial-4", hole(4, 4)),))
    step = next(s for s in all_steps(build_guide(doc, REGISTRY)) if isinstance(s, PartStep))

    assert step.bend_template_mm == pytest.approx(4 * BOARD.pitch)
    assert "4 holes apart" in step.span


def test_a_multi_pin_part_has_nothing_to_bend_to() -> None:
    doc = make_doc(components=(component("Q1", "to92", hole(4, 4)),))
    step = next(s for s in all_steps(build_guide(doc, REGISTRY)) if isinstance(s, PartStep))
    assert step.bend_template_mm is None


# ---------------------------------------------------------------------------
# Ordering (PLAN.md Sec 7.1)
# ---------------------------------------------------------------------------


def test_parts_are_ordered_shortest_first() -> None:
    """A tall part fitted early stops the board lying flat, and a part that cannot be
    pressed down solders at an angle. That is the whole reason for the phase order."""
    doc = make_doc(
        components=(
            component("J1", "screw-terminal-2", hole(2, 12)),
            component("R1", "r-axial-4", hole(2, 2)),
            component("C1", "c-elec-d8-p3", hole(2, 8)),
            component("U1", "dip-8", hole(10, 2)),
        )
    )
    guide = build_guide(doc, REGISTRY)

    phase_of = {
        step.ref: phase.number
        for phase in guide.phases
        for step in phase.steps
        if isinstance(step, PartStep)
    }
    assert phase_of["R1"] < phase_of["U1"] < phase_of["C1"] < phase_of["J1"]


def test_every_archetype_has_a_phase() -> None:
    """A body type with no phase would fall through to the default and land among the
    connectors, which is a silent way to produce a build order nobody can follow."""
    from typing import get_args

    for archetype in get_args(BodyArchetype):
        assert archetype in PHASE_BY_ARCHETYPE, archetype


def test_every_conductor_kind_has_a_phase() -> None:
    from typing import get_args

    from perfstudio.model import ConductorKind

    for kind in get_args(ConductorKind):
        assert kind in PHASE_BY_CONDUCTOR, kind


def test_solder_side_work_comes_after_every_part_and_wires_after_that() -> None:
    doc = routed_ne555()
    guide = build_guide(doc, REGISTRY)

    part_phases = {
        phase.number
        for phase in guide.phases
        for step in phase.steps
        if isinstance(step, PartStep)
    }
    trace_phases = {
        phase.number
        for phase in guide.phases
        for step in phase.steps
        if isinstance(step, ConductorStep) and step.conductor_kind.startswith("solder-trace")
    }
    wire_phases = {
        phase.number
        for phase in guide.phases
        for step in phase.steps
        if isinstance(step, ConductorStep) and step.conductor_kind == "insulated-wire"
    }

    assert max(part_phases) < min(trace_phases)
    assert max(trace_phases) < min(wire_phases)


def test_phase_titles_and_summaries_exist_for_every_phase() -> None:
    guide = build_guide(routed_ne555(), REGISTRY)
    assert [phase.number for phase in guide.phases] == sorted(PHASE_TITLES)
    assert all(phase.title and phase.summary for phase in guide.phases)


# ---------------------------------------------------------------------------
# It says how to check -- the differentiator (PLAN.md Sec 7.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["dense", "random-02"])
def test_every_proximity_risk_becomes_a_measurement(fixture: str) -> None:
    """PLAN.md Sec 7.5's central claim, as an assertion.

    R5' is the 0.6 mm gap between a solder trace and the neighbouring pad of another net
    -- the commonest way a perfboard build fails. DRC predicts each one; the guide has to
    turn each one into a specific probe, so that the risk the tool foresaw and the
    measurement the user performs come off the same list and cannot drift apart.
    """
    doc = golden(fixture)
    proximity = [v for v in run_drc(doc, REGISTRY) if v.rule == "solder-trace-proximity"]
    assert proximity, f"{fixture} was chosen because it has proximity risks"

    guide = build_guide(doc, REGISTRY)
    risk_pairs = {
        (risk.hole, risk.neighbour)
        for step in all_steps(guide)
        if isinstance(step, ConductorStep)
        for risk in step.risks
    }
    check_pairs = {
        check.holes
        for check in all_checkpoints(guide)
        if check.kind == "isolation" and len(check.holes) == 2
    }

    for violation in proximity:
        pair = (violation.holes[0], violation.holes[1])
        assert pair in risk_pairs, f"DRC flagged {pair} and the guide did not mention it"
        assert pair in check_pairs, f"{pair} was mentioned but never turned into a measurement"


def test_a_continuity_check_lands_in_the_phase_that_finishes_its_net() -> None:
    """Checked earlier it would fail for a reason that is not a fault; checked later it is
    buried among a dozen others."""
    doc = routed_ne555()
    guide = build_guide(doc, REGISTRY)

    last_conductor_phase: dict[str, int] = {}
    for phase in guide.phases:
        for step in phase.steps:
            if isinstance(step, ConductorStep):
                last_conductor_phase[step.net_name] = max(
                    last_conductor_phase.get(step.net_name, 0), phase.number
                )

    seen = 0
    for phase in guide.phases:
        for check in phase.checkpoints:
            if check.kind != "continuity":
                continue
            seen += 1
            net_name = check.expected.rsplit(" ", 1)[-1].rstrip(".")
            assert phase.number == last_conductor_phase[net_name]
    assert seen > 0


def test_a_long_trace_gets_a_resistance_expectation_that_matches_drc() -> None:
    """The guide quotes a number back as something to measure, so it had better be the
    same number DRC would print two lines above it."""
    trace = SolderTraceConductor(
        id="cond-1",
        path=tuple(hole(2 + n, 6) for n in range(10)),
        spine=SpineSpec(material="tinned-copper", gauge=0.6),
        kind="solder-trace-wired",
        net_id="n1",
    )
    doc = make_doc(
        components=(component("R1", "r-axial-4", hole(2, 6)),),
        conductors=(trace,),
        nets=(net("n1", "GND", "ground", (("R1", "1"), ("R1", "2"))),),
    )

    guide = build_guide(doc, REGISTRY)
    step = next(s for s in all_steps(guide) if isinstance(s, ConductorStep))
    expected = trace_electrical(trace, BOARD)

    assert step.resistance_ohm == pytest.approx(expected.resistance_ohm)
    check = next(c for c in all_checkpoints(guide) if c.kind == "resistance")
    assert f"{expected.resistance_ohm * 1000:.1f}" in check.title


def test_a_short_trace_is_not_worth_probing() -> None:
    """The expected value is below what a hand multimeter resolves, so a "measurement"
    would be a ritual rather than a test."""
    doc = make_doc(
        components=(component("R1", "r-axial-4", hole(2, 6)),),
        conductors=(
            SolderTraceConductor(id="cond-1", path=(hole(2, 6), hole(3, 6)), net_id="n1"),
        ),
        nets=(net("n1", "GND", "ground", (("R1", "1"), ("R1", "2"))),),
    )
    guide = build_guide(doc, REGISTRY)
    assert not [c for c in all_checkpoints(guide) if c.kind == "resistance"]


def test_the_last_phase_gates_power_on() -> None:
    guide = build_guide(routed_ne555(), REGISTRY)
    closing = guide.phases[-1]

    power = [check for check in closing.checkpoints if check.kind == "power-on"]
    assert len(power) == 1
    assert power[0].blocking
    assert all(check.blocking for check in closing.checkpoints if check.kind == "polarity")


def test_polarised_parts_are_swept_before_power() -> None:
    doc = make_doc(
        components=(
            component("C1", "c-elec-d5-p2", hole(4, 4)),
            component("R1", "r-axial-4", hole(10, 4)),
        )
    )
    guide = build_guide(doc, REGISTRY)

    sweep = next(c for c in all_checkpoints(guide) if c.kind == "polarity")
    assert "C1" in sweep.instruction
    assert "R1" not in sweep.instruction


def test_a_board_with_no_netlist_gets_no_continuity_checks_and_is_told_so() -> None:
    doc = make_doc(
        components=(component("R1", "r-axial-4", hole(2, 2)),),
        conductors=(WireConductor(id="cond-1", path=(hole(2, 2), hole(8, 8))),),
    )
    guide = build_guide(doc, REGISTRY)

    assert not [c for c in all_checkpoints(guide) if c.kind == "continuity"]
    assert "no-netlist" in {w.code for w in guide.warnings}


def test_an_unroutable_board_is_reported_rather_than_described_as_finished() -> None:
    doc = golden("ne555")  # Imported, nothing routed.
    guide = build_guide(doc, REGISTRY)
    assert "lvs-open" in {w.code for w in guide.warnings}


# ---------------------------------------------------------------------------
# The cut list (PLAN.md Sec 7.3)
# ---------------------------------------------------------------------------


def test_cut_length_is_the_path_plus_both_ends() -> None:
    """path + 2 x (board thickness + bend allowance) + 2 x strip. The thickness is in
    there because the wire goes THROUGH the board and turns over on the far side;
    leaving it out is how a cut list produces wires that are each 4 mm short."""
    doc = make_doc(
        components=(component("R1", "r-axial-4", hole(2, 2)),),
        conductors=(
            WireConductor(id="cond-1", path=(hole(2, 2), hole(2, 6)), kind="insulated-wire"),
        ),
    )
    options = GuideOptions(bend_allowance_mm=3.0, strip_length_mm=5.0)
    guide = build_guide(doc, REGISTRY, options)

    cut = guide.cut_list[0]
    assert cut.path_mm == pytest.approx(4 * BOARD.pitch)
    assert cut.cut_mm == pytest.approx(4 * BOARD.pitch + 2 * (1.6 + 3.0) + 2 * 5.0)


def test_bare_wire_is_not_charged_a_stripping_allowance() -> None:
    doc = make_doc(
        conductors=(WireConductor(id="cond-1", path=(hole(2, 2), hole(2, 6)), kind="bare-wire"),),
    )
    guide = build_guide(doc, REGISTRY)

    cut = guide.cut_list[0]
    assert cut.strip_mm == 0.0
    assert cut.insulated is False
    assert cut.cut_mm == pytest.approx(4 * BOARD.pitch + 2 * (1.6 + 3.0))


def test_wire_colours_follow_the_convention_and_are_stable() -> None:
    doc = make_doc(
        components=(component("R1", "r-axial-4", hole(2, 2)),),
        conductors=(
            WireConductor(id="cond-1", path=(hole(2, 2), hole(6, 2)), net_id="n-gnd"),
            WireConductor(id="cond-2", path=(hole(2, 4), hole(6, 4)), net_id="n-vcc"),
            WireConductor(id="cond-3", path=(hole(2, 6), hole(6, 6)), net_id="n-sig"),
        ),
        nets=(
            net("n-gnd", "GND", "ground", (("R1", "1"), ("R1", "2"))),
            net("n-vcc", "VCC", "power", (("R1", "1"), ("R1", "2"))),
            net("n-sig", "OUT", "signal", (("R1", "1"), ("R1", "2"))),
        ),
    )
    colors = {cut.net_name: cut.color for cut in build_guide(doc, REGISTRY).cut_list}

    assert colors["GND"] == "black"
    assert colors["VCC"] == "red"
    assert colors["OUT"] not in ("black", "red")
    assert build_guide(doc, REGISTRY).cut_list == build_guide(doc, REGISTRY).cut_list


def test_a_spine_is_listed_as_wire_to_cut_too() -> None:
    doc = make_doc(
        conductors=(
            SolderTraceConductor(
                id="cond-1",
                path=tuple(hole(2 + n, 6) for n in range(8)),
                kind="solder-trace-wired",
                spine=SpineSpec(material="tinned-copper", gauge=0.6),
            ),
        ),
    )
    guide = build_guide(doc, REGISTRY)

    assert len(guide.spine_list) == 1
    assert guide.spine_list[0].pads == 8
    assert any("Tinned copper wire" in tool for tool in guide.tools)


def test_the_tool_list_only_mentions_what_the_board_needs() -> None:
    no_wire = build_guide(
        make_doc(components=(component("R1", "r-axial-4", hole(2, 2)),)), REGISTRY
    )
    assert not any("Wire strippers" in tool for tool in no_wire.tools)

    with_wire = build_guide(
        make_doc(
            conductors=(
                WireConductor(id="cond-1", path=(hole(2, 2), hole(8, 2)), kind="insulated-wire"),
            )
        ),
        REGISTRY,
    )
    assert any("Wire strippers" in tool for tool in with_wire.tools)


def test_the_iron_runs_cooler_on_phenolic_board() -> None:
    """FR-2's pads lift under sustained heat, which is the same fact DRC rule R5'' exists
    for. The guide has to say so where someone will read it."""
    fr4 = build_guide(make_doc(board=BOARD), REGISTRY)
    fr2 = build_guide(make_doc(board=dataclasses.replace(BOARD, material="FR2")), REGISTRY)

    assert fr2.iron.temperature_c < fr4.iron.temperature_c
    assert fr2.iron.max_dwell_s < fr4.iron.max_dwell_s
    assert "lift" in fr2.iron.note


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_html_is_self_contained() -> None:
    """It has to open from a USB stick, on a phone, in a room with no wifi, in five
    years. Every external reference is a way for that to stop being true."""
    html = guide_to_html(build_guide(routed_ne555(), REGISTRY))

    for forbidden in ("http://", "https://", "<script src", "<link ", "@import", "url("):
        assert forbidden not in html, f"guide HTML reaches outside itself: {forbidden}"


def test_html_carries_every_step_and_check_as_a_tickable_item() -> None:
    guide = build_guide(routed_ne555(), REGISTRY)
    html = guide_to_html(guide)

    boxes = html.count('type="checkbox"')
    # Phase 0's own checks are rendered too, so the count is steps + checks exactly.
    assert boxes == guide.total_steps + guide.checkpoint_count
    assert "localStorage" in html


def test_html_escapes_document_content() -> None:
    doc = make_doc(
        components=(component("<script>x</script>", "r-axial-4", hole(2, 2)),),
        name='evil "quoted" <b>name</b>',
    )
    html = guide_to_html(build_guide(doc, REGISTRY))

    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>name</b>" not in html


def test_html_names_holes_by_address() -> None:
    doc = make_doc(components=(component("R1", "r-axial-4", hole(2, 2)),))
    html = guide_to_html(build_guide(doc, REGISTRY))
    assert format_hole(hole(2, 2)) in html


def test_json_round_trips_and_carries_hole_addresses_with_the_numbers() -> None:
    guide = build_guide(routed_ne555(), REGISTRY)
    data = json.loads(guide_to_json(guide))

    assert data["document"] == guide.document_name
    assert len(data["phases"]) == len(guide.phases)
    assert data["totals"]["part_steps"] == guide.part_steps
    assert data["totals"]["checkpoints"] == guide.checkpoint_count

    first_part = next(
        step
        for phase in data["phases"]
        for step in phase["steps"]
        if step["kind"] == "part"
    )
    _number, at = first_part["pin_holes"][0]
    assert set(at) == {"col", "row", "ref"}


def test_json_names_the_generator_version() -> None:
    from perfstudio.version import __version__

    data = json.loads(guide_to_json(build_guide(make_doc(), REGISTRY)))
    assert __version__ in data["generator"]


def test_csv_has_one_row_per_wire_and_per_spine() -> None:
    guide = build_guide(routed_ne555(), REGISTRY)
    rows = list(csv.reader(io.StringIO(cut_list_to_csv(guide))))

    assert rows[0][0] == "type"
    assert len(rows) == 1 + len(guide.cut_list) + len(guide.spine_list)


def test_bom_csv_groups_identical_parts() -> None:
    doc = make_doc(
        components=(
            component("R1", "r-axial-4", hole(2, 2), value="10k"),
            component("R2", "r-axial-4", hole(2, 4), value="10k"),
            component("R3", "r-axial-4", hole(2, 6), value="1k"),
        )
    )
    rows = list(csv.reader(io.StringIO(bom_to_csv(build_guide(doc, REGISTRY)))))

    assert rows[0] == ["quantity", "value", "footprint", "references"]
    by_value = {row[1]: row for row in rows[1:]}
    assert by_value["10k"][0] == "2"
    assert by_value["10k"][3] == "R1, R2"
    assert by_value["1k"][0] == "1"


def test_describe_is_a_one_liner_with_the_counts_in_it() -> None:
    guide = build_guide(routed_ne555(), REGISTRY)
    line = describe(guide)

    assert str(guide.total_steps) in line
    assert "check" in line
    assert "\n" not in line


def test_default_options_are_the_documented_ones() -> None:
    assert DEFAULT_GUIDE_OPTIONS.strip_length_mm == 5.0
    assert DEFAULT_GUIDE_OPTIONS.bend_allowance_mm == 3.0
    assert DEFAULT_GUIDE_OPTIONS.resistance_check_min_pads == 5
