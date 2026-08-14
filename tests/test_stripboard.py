"""Tests for stripboard: the board whose copper arrives already joined (stripboard.py).

Everything else in this project adds copper. Here the copper is there and the design
problem is where to break it, so the failure modes invert too: on a pad-per-hole board a
mistake usually leaves something UNconnected, and on stripboard it leaves two nets
shorted along a track nobody looked at.

The load-bearing assertions are the connectivity ones near the bottom. They are what LVS,
the ratsnest, the guide's continuity checks and the router all read.
"""

from __future__ import annotations

import dataclasses

import pytest

from perfstudio.command import CommandBus, CommandContext
from perfstudio.commands import (
    AddCutPayload,
    ApplyStripboardPlanPayload,
    create_document_id_generator,
    create_standard_registry,
)
from perfstudio.connectivity import PhysicalPinRef, are_pins_connected, extract_physical_nets
from perfstudio.drc import run_drc
from perfstudio.guide import build_guide
from perfstudio.guide_export import guide_to_html
from perfstudio.lvs import run_lvs
from perfstudio.model import (
    Board,
    BodySpec,
    ComponentInstance,
    DocumentMeta,
    Footprint,
    FootprintPin,
    HoleCoord,
    Net,
    NetNode,
    PerfDocument,
    TrackCut,
)
from perfstudio.stripboard import (
    cut_between,
    cut_holes,
    is_stripboard,
    joined_by_board,
    segment_holes,
    segment_of,
    segments,
    strip_axis,
)
from perfstudio.striproute import describe_plan, plan_stripboard

STRIPBOARD = Board(
    type="stripboard",
    cols=10,
    rows=6,
    pitch=2.54,
    thickness=1.6,
    material="FR2",  # what stripboard is actually sold as, and what R5'' cares about
    pad_diameter=1.9,
    drill_diameter=1.0,
    strip_axis="horizontal",
)

PAD_PER_HOLE = dataclasses.replace(STRIPBOARD, type="pad-per-hole", strip_axis=None)

PIN = Footprint(
    id="fp1",
    name="one pin",
    pins=(FootprintPin(number="1", d_col=0, d_row=0),),
    body_outline=(),
    body_height=0,
    body=BodySpec(archetype="generic-box"),
    lead_diameter=0.5,
    polarized=False,
)
LOOKUP = {PIN.id: PIN}.get


def _doc(board: Board = STRIPBOARD, *, cuts: tuple[TrackCut, ...] = ()) -> PerfDocument:
    return PerfDocument(
        meta=DocumentMeta(name="t", created="2026-01-01T00:00:00Z", modified="2026-01-01T00:00:00Z"),
        board=board,
        cuts=cuts,
    )


def _pin(ref: str, at: HoleCoord) -> ComponentInstance:
    return ComponentInstance(
        id=f"cmp-{ref}", ref=ref, value="", footprint_id="fp1", anchor=at,
        rotation=0, mirrored=False, locked=False,
    )


# ---------------------------------------------------------------------------
# Which way the strips run
# ---------------------------------------------------------------------------


def test_a_board_that_declares_no_axis_behaves_like_the_one_people_buy() -> None:
    """Stock is sold with the strips along the long side. Raising on a board that does
    not say would refuse to open a file that is perfectly readable."""
    assert strip_axis(dataclasses.replace(STRIPBOARD, strip_axis=None)) == "horizontal"


def test_only_a_stripboard_has_strips() -> None:
    assert is_stripboard(STRIPBOARD) is True
    assert is_stripboard(PAD_PER_HOLE) is False
    assert segments(_doc(PAD_PER_HOLE)) == ()
    assert segment_of(_doc(PAD_PER_HOLE), HoleCoord(1, 1)) is None


def test_a_cut_on_a_board_with_no_tracks_means_nothing() -> None:
    """A hand-edited file, or one converted from stripboard, may carry cuts that no
    longer describe anything. Reading it is not the moment to argue about that."""
    doc = _doc(PAD_PER_HOLE, cuts=(TrackCut(id="cut-1", at=HoleCoord(3, 2)),))

    assert cut_holes(doc) == frozenset()


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def test_an_uncut_board_is_one_segment_per_strip() -> None:
    runs = segments(_doc())

    assert len(runs) == STRIPBOARD.rows
    assert all(len(run.holes) == STRIPBOARD.cols for run in runs)
    assert runs[0].holes[0] == HoleCoord(0, 0)


def test_a_cut_splits_one_strip_and_leaves_the_others_alone() -> None:
    doc = _doc(cuts=(TrackCut(id="cut-1", at=HoleCoord(4, 2)),))

    runs = [run for run in segments(doc) if run.index == 2]

    assert [len(run.holes) for run in runs] == [4, 5]  # the cut hole belongs to neither
    assert len(segments(doc)) == STRIPBOARD.rows + 1


def test_the_cut_hole_itself_is_on_no_segment() -> None:
    """The copper there was drilled away. A pin in it is soldered to nothing, which is
    what DRC reports -- and what makes a cut inspectable on the real board."""
    doc = _doc(cuts=(TrackCut(id="cut-1", at=HoleCoord(4, 2)),))

    assert segment_of(doc, HoleCoord(4, 2)) is None
    assert segment_holes(doc, HoleCoord(4, 2)) == ()


def test_holes_either_side_of_a_cut_are_on_different_segments() -> None:
    doc = _doc(cuts=(TrackCut(id="cut-1", at=HoleCoord(4, 2)),))

    assert joined_by_board(doc, HoleCoord(3, 2), HoleCoord(5, 2)) is False
    assert joined_by_board(doc, HoleCoord(0, 2), HoleCoord(3, 2)) is True
    assert joined_by_board(doc, HoleCoord(5, 2), HoleCoord(9, 2)) is True


def test_segment_ids_do_not_move_when_a_cut_is_made_elsewhere() -> None:
    """The ordinal counts the cuts before a hole on its OWN strip, so cutting row 4 says
    nothing about row 2 -- and a connectivity pass keyed on these cannot be perturbed by
    an unrelated edit."""
    one = _doc(cuts=(TrackCut(id="cut-1", at=HoleCoord(4, 2)),))
    two = dataclasses.replace(one, cuts=(*one.cuts, TrackCut(id="cut-2", at=HoleCoord(1, 4))))

    assert segment_of(one, HoleCoord(7, 2)) == segment_of(two, HoleCoord(7, 2))


@pytest.mark.parametrize("axis", ["horizontal", "vertical"])
def test_the_axis_decides_what_a_strip_is(axis: str) -> None:
    board = dataclasses.replace(STRIPBOARD, strip_axis=axis)  # type: ignore[arg-type]
    doc = _doc(board)

    across_a_row = joined_by_board(doc, HoleCoord(0, 3), HoleCoord(9, 3))
    down_a_column = joined_by_board(doc, HoleCoord(3, 0), HoleCoord(3, 5))

    assert across_a_row is (axis == "horizontal")
    assert down_a_column is (axis == "vertical")


def test_segment_holes_gives_the_whole_run_a_hole_belongs_to() -> None:
    doc = _doc(cuts=(TrackCut(id="cut-1", at=HoleCoord(4, 2)),))

    assert segment_holes(doc, HoleCoord(6, 2)) == tuple(HoleCoord(c, 2) for c in range(5, 10))


# ---------------------------------------------------------------------------
# Where to cut
# ---------------------------------------------------------------------------


def test_a_cut_between_two_pins_lands_in_the_middle_of_the_run() -> None:
    """Hard against a pin is where a drill lifts the neighbouring pad, and the middle is
    also where a later part is least likely to want to sit."""
    doc = _doc()

    assert cut_between(doc, HoleCoord(2, 1), HoleCoord(8, 1)) == HoleCoord(5, 1)


def test_adjacent_pins_cannot_be_separated_by_a_cut() -> None:
    """There is no hole between them to drill out. This is a real dead end on stripboard
    and the reason placement matters more there than on a pad-per-hole board."""
    doc = _doc()

    assert cut_between(doc, HoleCoord(2, 1), HoleCoord(3, 1)) is None


def test_a_cut_is_not_offered_where_something_is_already_soldered() -> None:
    doc = _doc()
    occupied = frozenset({"3,1", "5,1"})

    at = cut_between(doc, HoleCoord(2, 1), HoleCoord(8, 1), occupied)

    assert at is not None
    assert at not in (HoleCoord(3, 1), HoleCoord(5, 1))


def test_holes_on_different_strips_have_nothing_to_cut() -> None:
    doc = _doc()

    assert cut_between(doc, HoleCoord(2, 1), HoleCoord(2, 4)) is None


# ---------------------------------------------------------------------------
# Connectivity: the part everything else reads
# ---------------------------------------------------------------------------


def test_two_pins_on_one_strip_are_connected_by_the_board_itself() -> None:
    """Nobody soldered this connection, which is the whole character of the board."""
    doc = dataclasses.replace(
        _doc(), components=(_pin("R1", HoleCoord(1, 2)), _pin("R2", HoleCoord(7, 2)))
    )

    assert are_pins_connected(doc, LOOKUP, PhysicalPinRef("R1", "1"), PhysicalPinRef("R2", "1"))


def test_two_pins_on_different_strips_are_not() -> None:
    doc = dataclasses.replace(
        _doc(), components=(_pin("R1", HoleCoord(1, 2)), _pin("R2", HoleCoord(1, 3)))
    )

    assert not are_pins_connected(doc, LOOKUP, PhysicalPinRef("R1", "1"), PhysicalPinRef("R2", "1"))


def test_a_cut_between_two_pins_disconnects_them() -> None:
    """The edit that stripboard design is made of."""
    doc = dataclasses.replace(
        _doc(), components=(_pin("R1", HoleCoord(1, 2)), _pin("R2", HoleCoord(7, 2)))
    )
    cut = dataclasses.replace(doc, cuts=(TrackCut(id="cut-1", at=HoleCoord(4, 2)),))

    assert are_pins_connected(doc, LOOKUP, PhysicalPinRef("R1", "1"), PhysicalPinRef("R2", "1"))
    assert not are_pins_connected(cut, LOOKUP, PhysicalPinRef("R1", "1"), PhysicalPinRef("R2", "1"))


def test_a_pin_in_a_cut_hole_is_joined_to_nothing_by_the_board() -> None:
    doc = dataclasses.replace(
        _doc(cuts=(TrackCut(id="cut-1", at=HoleCoord(4, 2)),)),
        components=(_pin("R1", HoleCoord(4, 2)), _pin("R2", HoleCoord(7, 2))),
    )

    assert not are_pins_connected(doc, LOOKUP, PhysicalPinRef("R1", "1"), PhysicalPinRef("R2", "1"))


def test_the_strips_do_not_register_the_holes_nobody_soldered_into() -> None:
    """A strip physically joins all ten holes in its row. The eight nobody used are
    electrically indistinguishable from the empty pads this engine deliberately does not
    register, and putting them in would flood every net listing and the LVS report."""
    doc = dataclasses.replace(
        _doc(), components=(_pin("R1", HoleCoord(1, 2)), _pin("R2", HoleCoord(7, 2)))
    )

    nets = extract_physical_nets(doc, LOOKUP)
    joined = next(net for net in nets if len(net.pins) == 2)

    assert {(n.hole.col, n.hole.row) for n in joined.nodes} == {(1, 2), (7, 2)}


def test_the_same_board_as_pad_per_hole_connects_nothing() -> None:
    """The one assertion that says pass 3 is gated rather than always on -- which is
    also why fifteen golden fixtures still reproduce byte for byte."""
    doc = dataclasses.replace(
        _doc(PAD_PER_HOLE), components=(_pin("R1", HoleCoord(1, 2)), _pin("R2", HoleCoord(7, 2)))
    )

    assert not are_pins_connected(doc, LOOKUP, PhysicalPinRef("R1", "1"), PhysicalPinRef("R2", "1"))


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _bus(doc: PerfDocument) -> CommandBus:
    return CommandBus(
        doc, create_standard_registry(), CommandContext(next_id=create_document_id_generator(doc))
    )


def test_cutting_a_track_is_one_command_and_undoes() -> None:
    doc = dataclasses.replace(
        _doc(), components=(_pin("R1", HoleCoord(1, 2)), _pin("R2", HoleCoord(7, 2)))
    )
    bus = _bus(doc)

    result = bus.dispatch("cut.add", AddCutPayload(at=HoleCoord(4, 2)))

    assert result.ok, result.message
    assert result.description == "Cut track at E3"
    assert not are_pins_connected(
        bus.document, LOOKUP, PhysicalPinRef("R1", "1"), PhysicalPinRef("R2", "1")
    )

    bus.undo()

    assert are_pins_connected(
        bus.document, LOOKUP, PhysicalPinRef("R1", "1"), PhysicalPinRef("R2", "1")
    )


def test_a_pad_per_hole_board_refuses_a_cut_with_a_reason() -> None:
    result = _bus(_doc(PAD_PER_HOLE)).dispatch("cut.add", AddCutPayload(at=HoleCoord(4, 2)))

    assert result.ok is False
    assert result.code == "not-stripboard"


# ---------------------------------------------------------------------------
# Routing one
# ---------------------------------------------------------------------------


def _net(id_: str, name: str, *pins: tuple[str, str]) -> Net:
    return Net(id=id_, name=name, nodes=tuple(NetNode(ref, pin) for ref, pin in pins))


def test_two_nets_sharing_a_strip_are_separated_by_one_cut() -> None:
    """The board shorts them before anybody picks up an iron. This is the stripboard
    design problem, and the whole reason the planner subtracts before it adds."""
    doc = dataclasses.replace(
        _doc(),
        components=(_pin("R1", HoleCoord(1, 2)), _pin("R2", HoleCoord(7, 2))),
        nets=(_net("n1", "IN", ("R1", "1")), _net("n2", "OUT", ("R2", "1"))),
    )

    plan = plan_stripboard(doc, LOOKUP)

    assert [cut.at for cut in plan.cuts] == [HoleCoord(4, 2)]
    assert not are_pins_connected(
        plan.document, LOOKUP, PhysicalPinRef("R1", "1"), PhysicalPinRef("R2", "1")
    )
    assert plan.problems == ()


def test_two_pins_of_one_net_on_one_strip_need_nothing_at_all() -> None:
    """The connection is already there and the board made it. A planner that laid a wire
    here would be adding work to a board that is already right."""
    doc = dataclasses.replace(
        _doc(),
        components=(_pin("R1", HoleCoord(1, 2)), _pin("R2", HoleCoord(7, 2))),
        nets=(_net("n1", "IN", ("R1", "1"), ("R2", "1")),),
    )

    plan = plan_stripboard(doc, LOOKUP)

    assert plan.is_empty
    assert "Nothing to do" in describe_plan(plan)


def test_a_net_split_across_two_strips_gets_a_link_over_the_component_side() -> None:
    """Not the solder side: that face is one sheet of parallel copper, and a wire laid
    across it shorts every strip it crosses. This is the mistake the planner must never
    make on the user's behalf."""
    doc = dataclasses.replace(
        _doc(),
        components=(_pin("R1", HoleCoord(1, 1)), _pin("R2", HoleCoord(6, 4))),
        nets=(_net("n1", "IN", ("R1", "1"), ("R2", "1")),),
    )

    plan = plan_stripboard(doc, LOOKUP)

    assert len(plan.conductors) == 1
    link = plan.conductors[0]
    assert link.kind == "top-jumper"
    assert link.side == "top"
    assert set(link.path) == {HoleCoord(1, 1), HoleCoord(6, 4)}
    assert are_pins_connected(
        plan.document, LOOKUP, PhysicalPinRef("R1", "1"), PhysicalPinRef("R2", "1")
    )


def test_pins_it_cannot_separate_are_reported_rather_than_routed_around() -> None:
    """Adjacent pins of different nets have no hole between them to drill. The fix is to
    move a part, which is the user's decision -- PLAN.md §13's trap is the planner that
    quietly leaves this out."""
    doc = dataclasses.replace(
        _doc(),
        components=(_pin("R1", HoleCoord(1, 2)), _pin("R2", HoleCoord(2, 2))),
        nets=(_net("n1", "IN", ("R1", "1")), _net("n2", "OUT", ("R2", "1"))),
    )

    plan = plan_stripboard(doc, LOOKUP)

    assert plan.cuts == ()
    assert [p.code for p in plan.problems] == ["cannot-separate"]
    assert "R1.1" in plan.problems[0].message and "R2.1" in plan.problems[0].message


def test_a_cut_is_not_planned_through_a_pin() -> None:
    doc = dataclasses.replace(
        _doc(),
        components=(
            _pin("R1", HoleCoord(1, 2)),
            _pin("R2", HoleCoord(3, 2)),
            _pin("R3", HoleCoord(6, 2)),
        ),
        nets=(_net("n1", "IN", ("R1", "1"), ("R2", "1")), _net("n2", "OUT", ("R3", "1"))),
    )

    plan = plan_stripboard(doc, LOOKUP)

    assert [cut.at for cut in plan.cuts] == [HoleCoord(4, 2)]
    assert HoleCoord(3, 2) not in [cut.at for cut in plan.cuts]


def test_planning_is_deterministic() -> None:
    """Same board, same plan -- the property every planner in this project holds, and
    what makes a plan safe to show a user before they accept it."""
    doc = dataclasses.replace(
        _doc(),
        components=(
            _pin("R1", HoleCoord(1, 1)),
            _pin("R2", HoleCoord(6, 4)),
            _pin("R3", HoleCoord(8, 1)),
        ),
        nets=(_net("n1", "IN", ("R1", "1"), ("R2", "1")), _net("n2", "OUT", ("R3", "1"))),
    )

    first = plan_stripboard(doc, LOOKUP)
    second = plan_stripboard(doc, LOOKUP)

    assert [c.at for c in first.cuts] == [c.at for c in second.cuts]
    assert [c.path for c in first.conductors] == [c.path for c in second.conductors]


def test_a_plan_commits_as_one_command_and_undoes_as_one() -> None:
    """The cuts and the links are one decision. Split in two, a single Ctrl+Z leaves a
    board cut apart with nothing linking it -- or linked with nothing cut, which is a
    short across two nets."""
    doc = dataclasses.replace(
        _doc(),
        components=(
            _pin("R1", HoleCoord(1, 2)),
            _pin("R2", HoleCoord(7, 2)),
            _pin("R3", HoleCoord(1, 4)),
        ),
        nets=(
            _net("n1", "IN", ("R1", "1"), ("R3", "1")),
            _net("n2", "OUT", ("R2", "1")),
        ),
    )
    plan = plan_stripboard(doc, LOOKUP)
    assert plan.cuts and plan.conductors, "this fixture is meant to need both"
    bus = _bus(doc)

    result = bus.dispatch("stripboard.apply", plan.payload())

    assert result.ok, result.message
    assert len(bus.document.cuts) == len(plan.cuts)
    assert len(bus.document.conductors) == len(plan.conductors)

    bus.undo()

    assert bus.document.cuts == ()
    assert bus.document.conductors == ()


def test_the_preview_document_is_what_the_bus_produces() -> None:
    """A plan is shown to the user before it is accepted, so the preview has to be the
    board they will get -- ids included."""
    doc = dataclasses.replace(
        _doc(),
        components=(_pin("R1", HoleCoord(1, 1)), _pin("R2", HoleCoord(6, 4))),
        nets=(_net("n1", "IN", ("R1", "1"), ("R2", "1")),),
    )
    plan = plan_stripboard(doc, LOOKUP)
    bus = _bus(doc)

    bus.dispatch("stripboard.apply", plan.payload())

    assert bus.document.conductors == plan.document.conductors
    assert bus.document.cuts == plan.document.cuts


def test_an_empty_plan_is_refused_rather_than_left_on_the_undo_stack() -> None:
    result = _bus(_doc()).dispatch("stripboard.apply", ApplyStripboardPlanPayload())

    assert result.ok is False
    assert result.code == "nothing-to-apply"


def test_a_routed_stripboard_matches_its_schematic_under_lvs() -> None:
    """The end-to-end property, and the one that says the whole chain agrees: plan, apply,
    and the board the schematic asked for is the board that is there."""
    doc = dataclasses.replace(
        _doc(),
        components=(
            _pin("R1", HoleCoord(1, 1)),
            _pin("R2", HoleCoord(6, 4)),
            _pin("R3", HoleCoord(8, 1)),
            _pin("R4", HoleCoord(1, 4)),
        ),
        nets=(
            _net("n1", "IN", ("R1", "1"), ("R2", "1")),
            _net("n2", "OUT", ("R3", "1"), ("R4", "1")),
        ),
    )
    bus = _bus(doc)
    plan = plan_stripboard(doc, LOOKUP)

    assert bus.dispatch("stripboard.apply", plan.payload()).ok

    result = run_lvs(bus.document, LOOKUP)
    assert result.summary.opens == 0, [issue.message for issue in result.issues]
    assert result.summary.shorts == 0, [issue.message for issue in result.issues]


def test_drc_reports_a_pin_standing_in_a_cut_hole() -> None:
    """The stripboard twin of the mounting-hole rule: the cut took the pad with it, so
    there is nothing there to solder to. An error, not a warning -- this board cannot
    work rather than probably will not."""
    doc = dataclasses.replace(
        _doc(cuts=(TrackCut(id="cut-1", at=HoleCoord(4, 2)),)),
        components=(_pin("R1", HoleCoord(4, 2)),),
    )

    violations = [v for v in run_drc(doc, LOOKUP) if v.rule == "cut-track-conflict"]

    assert len(violations) == 1
    assert violations[0].severity == "error"
    assert "E3" in violations[0].message


def test_a_pad_per_hole_board_never_sees_the_cut_rule() -> None:
    """Gated, like every other stripboard behaviour -- which is why fifteen golden DRC
    fixtures still reproduce byte for byte."""
    doc = dataclasses.replace(
        _doc(PAD_PER_HOLE),
        cuts=(TrackCut(id="cut-1", at=HoleCoord(4, 2)),),
        components=(_pin("R1", HoleCoord(4, 2)),),
    )

    assert [v for v in run_drc(doc, LOOKUP) if v.rule == "cut-track-conflict"] == []


def test_the_build_guide_puts_the_cuts_first_and_checks_them() -> None:
    """They are made from the copper side with a drill, and once a part is over a hole
    there is no way back to it. A cut that did not go through looks exactly like one that
    did, so each gets an isolation probe on the bare board."""
    doc = dataclasses.replace(
        _doc(cuts=(TrackCut(id="cut-1", at=HoleCoord(4, 2)),)),
        components=(_pin("R1", HoleCoord(1, 2)), _pin("R2", HoleCoord(7, 2))),
        nets=(_net("n1", "IN", ("R1", "1")), _net("n2", "OUT", ("R2", "1"))),
    )

    guide = build_guide(doc, LOOKUP)

    assert [job.at for job in guide.track_cuts] == [HoleCoord(4, 2)]
    assert guide.track_cuts[0].strip == "row 3"
    prep = guide.phases[0]
    cut_checks = [c for c in prep.checkpoints if "D3" in c.title and "F3" in c.title]
    assert len(cut_checks) == 1
    assert cut_checks[0].blocking is True

    html = guide_to_html(guide)
    assert "Cut these tracks first" in html
    assert "row 3" in html


def test_a_perfboard_guide_says_nothing_about_cutting_tracks() -> None:
    doc = dataclasses.replace(_doc(PAD_PER_HOLE), components=(_pin("R1", HoleCoord(1, 2)),))

    guide = build_guide(doc, LOOKUP)

    assert guide.track_cuts == ()
    assert "Cut these tracks first" not in guide_to_html(guide)


def test_a_plan_made_against_a_board_that_has_moved_on_is_refused() -> None:
    """The cut is already there, so applying the plan again would be a no-op that reads
    as a success. Refusing names the hole instead."""
    doc = dataclasses.replace(
        _doc(),
        components=(_pin("R1", HoleCoord(1, 2)), _pin("R2", HoleCoord(7, 2))),
        nets=(_net("n1", "IN", ("R1", "1")), _net("n2", "OUT", ("R2", "1"))),
    )
    plan = plan_stripboard(doc, LOOKUP)
    bus = _bus(doc)
    bus.dispatch("stripboard.apply", plan.payload())

    again = bus.dispatch("stripboard.apply", plan.payload())

    assert again.ok is False
    assert again.code == "duplicate-cut"
