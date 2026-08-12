"""Tests for the three things a board can have that a bare grid of round pads cannot:
oblong pads, its own printed hole addresses, and mechanical features (mounting holes and
edge-connector fingers).

ORGANISED BY FEATURE RATHER THAN BY MODULE, which is the opposite of every other file
here, on purpose. Each of these three cuts across geometry, persistence, the command bus,
DRC, the guide and both renderers, and the interesting claims are the ones that span
those layers -- that an oblong pad's tighter gap reaches DRC's message, that a printed
legend removes a step from the build guide, that a mounting bore takes copper off pads it
was not drilled on and every layer agrees which. Split module by module, each of those
would become three unrelated-looking assertions in three files.

The single most load-bearing test is
``test_a_board_using_none_of_this_serializes_exactly_as_before``: these features add
fields to the .perf format, and they may only appear in a file that uses them. That is
what keeps the golden fixtures round-tripping byte-for-byte, and it is the whole reason
the document format version did not have to move.
"""

from __future__ import annotations

import dataclasses

import pytest

from perfstudio import persist
from perfstudio.command import CommandBus, CommandContext, create_id_generator
from perfstudio.commands import (
    DEFAULT_BOARD,
    AddEdgeConnectorPayload,
    AddMountingHolePayload,
    AddMountingHolesPayload,
    DeleteEdgeConnectorPayload,
    DeleteMountingHolePayload,
    SetBoardPayload,
    create_empty_document,
    create_standard_registry,
)
from perfstudio.drc import run_drc
from perfstudio.footprints import footprint_lookup
from perfstudio.geometry import (
    consumed_holes,
    copper_gap_mm,
    default_finger_length_mm,
    edge_connector_holes,
    edge_finger_rect,
    hole_key,
    hole_ref_to_coord,
    hole_to_mm,
    pad_edge_gap_mm,
    pad_extent_mm,
    printed_row_label,
)
from perfstudio.guide import build_guide
from perfstudio.model import (
    Board,
    BoardLabels,
    ComponentInstance,
    DocumentMeta,
    EdgeConnector,
    HoleCoord,
    MountingHole,
    PerfDocument,
    SolderTraceConductor,
)

META = DocumentMeta(name="features", created="2026-01-01", modified="2026-01-01")
LOOKUP = footprint_lookup()

#: Small enough to reason about, big enough for a part and four corner holes.
BOARD = dataclasses.replace(DEFAULT_BOARD, cols=12, rows=10)

#: 2.25 mm down a column against a 1.9 mm width, at 2.54 mm pitch: 0.29 mm of gap one
#: way and 0.64 mm the other. Those two numbers are what every oblong test below is about.
OBLONG = dataclasses.replace(BOARD, pad_shape="oblong", pad_length=2.25, pad_axis="vertical")


def _doc(board: Board = BOARD, **fields: object) -> PerfDocument:
    return dataclasses.replace(create_empty_document(META, board), **fields)  # type: ignore[arg-type]


def _bus(doc: PerfDocument) -> CommandBus:
    return CommandBus(doc, create_standard_registry(), CommandContext(next_id=create_id_generator()))


#: A frozen dataclass is immutable, so sharing one as a default is safe -- but it is
#: still built at import time, which linters flag and which reads worse than naming it.
_MIDDLE = HoleCoord(4, 4)


def _resistor(ref: str = "R1", at: HoleCoord = _MIDDLE) -> ComponentInstance:
    return ComponentInstance(id=f"cmp-{ref}", ref=ref, value="1k", footprint_id="r-axial-5", anchor=at)


# ---------------------------------------------------------------------------
# Pad shape
# ---------------------------------------------------------------------------


def test_a_round_pad_is_the_same_size_both_ways() -> None:
    assert pad_extent_mm(BOARD) == (BOARD.pad_diameter, BOARD.pad_diameter)
    assert pad_edge_gap_mm(BOARD, "horizontal") == pytest.approx(pad_edge_gap_mm(BOARD, "vertical"))


@pytest.mark.parametrize(
    ("axis", "expected"),
    [("vertical", (1.9, 2.25)), ("horizontal", (2.25, 1.9))],
)
def test_an_oblong_pad_is_longer_along_its_own_axis(axis: str, expected: tuple[float, float]) -> None:
    board = dataclasses.replace(OBLONG, pad_axis=axis)  # type: ignore[arg-type]
    assert pad_extent_mm(board) == pytest.approx(expected)


def test_the_gap_to_the_next_pad_depends_on_which_way_you_go() -> None:
    """The point of the whole feature. Solder crosses 0.29 mm far more readily than
    0.64 mm, so on this board a trace down a column is easy to make and easy to make by
    accident, while one along a row is neither."""
    assert pad_edge_gap_mm(OBLONG, "vertical") == pytest.approx(0.29)
    assert pad_edge_gap_mm(OBLONG, "horizontal") == pytest.approx(0.64)


def test_drc_quotes_the_gap_for_the_direction_it_found() -> None:
    doc = _doc(
        OBLONG,
        components=(_resistor("R1", HoleCoord(0, 0)), _resistor("R2", HoleCoord(0, 1))),
        conductors=(SolderTraceConductor(id="t1", path=(HoleCoord(0, 0), HoleCoord(1, 0))),),
    )
    proximity = [v for v in run_drc(doc, LOOKUP) if v.rule == "solder-trace-proximity"]
    assert proximity, "a trace beside another net's pad must raise R5'"
    down_column = [v for v in proximity if "down the column" in v.message]
    assert down_column, "the risky neighbour here is one ROW away"
    assert "0.29 mm" in down_column[0].message
    # And the same board must not describe that gap as the comfortable one.
    assert "0.64 mm of copper" not in down_column[0].message


def test_an_oblong_board_without_a_length_is_treated_as_round_rather_than_crashing() -> None:
    """A hand-edited file must still open and still be checkable -- the same rule that
    makes a diagonal solder-trace step a warning rather than a refusal."""
    broken = dataclasses.replace(OBLONG, pad_length=None)
    assert pad_extent_mm(broken) == (broken.pad_diameter, broken.pad_diameter)


def test_board_set_refuses_an_oblong_pad_that_is_not_longer_than_it_is_wide() -> None:
    bus = _bus(_doc())
    result = bus.dispatch(
        "board.set",
        SetBoardPayload(board=dataclasses.replace(BOARD, pad_shape="oblong", pad_length=1.5)),
    )
    assert not result.ok
    assert result.code == "invalid-board"


# ---------------------------------------------------------------------------
# The printed legend
# ---------------------------------------------------------------------------


def test_a_printed_row_label_is_padded_but_the_address_is_not() -> None:
    """The board may print "07" where the guide says row 7. They are the same address --
    the padding is how the board sets it -- and the parser must keep rejecting the padded
    form so that nothing starts treating "A07" as an address."""
    labels = BoardLabels(row_digits=2)
    assert printed_row_label(6, labels) == "07"
    assert printed_row_label(6, BoardLabels()) == "7"
    assert printed_row_label(99, labels) == "100", "padding widens, it never truncates"
    with pytest.raises(ValueError):
        hole_ref_to_coord("A07")


def test_the_guide_stops_asking_for_a1_to_be_marked_when_the_board_prints_it() -> None:
    plain = build_guide(_doc(), LOOKUP).phases[0].summary
    printed = build_guide(_doc(dataclasses.replace(BOARD, labels=BoardLabels())), LOOKUP).phases[0].summary

    assert "mark hole A1" in plain
    assert "mark hole A1" not in printed
    assert "printed A1" in printed


def test_the_guide_warns_which_way_solder_runs_on_an_oblong_board() -> None:
    summary = build_guide(_doc(OBLONG), LOOKUP).phases[0].summary
    assert "oblong" in summary
    assert "down a column" in summary


def test_board_set_refuses_a_legend_narrower_than_one_digit() -> None:
    bus = _bus(_doc())
    result = bus.dispatch(
        "board.set",
        SetBoardPayload(board=dataclasses.replace(BOARD, labels=BoardLabels(row_digits=0))),
    )
    assert not result.ok
    assert result.code == "invalid-board"


# ---------------------------------------------------------------------------
# Mounting holes
# ---------------------------------------------------------------------------


def test_an_m3_bore_takes_the_copper_off_its_orthogonal_neighbours_too() -> None:
    """The fact that makes this feature worth modelling at all. A 3.2 mm bore reaches
    1.6 mm out; the next pad's near edge is 2.54 - 0.95 = 1.59 mm away, so it goes. The
    diagonals, at 3.59 mm, do not."""
    doc = _doc(mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(5, 5)),))
    consumed = consumed_holes(doc)

    assert hole_key(HoleCoord(5, 5)) in consumed
    for neighbour in (HoleCoord(4, 5), HoleCoord(6, 5), HoleCoord(5, 4), HoleCoord(5, 6)):
        assert hole_key(neighbour) in consumed, f"{neighbour} should lose its pad"
    for diagonal in (HoleCoord(4, 4), HoleCoord(6, 6), HoleCoord(4, 6), HoleCoord(6, 4)):
        assert hole_key(diagonal) not in consumed, f"{diagonal} is far enough away"


def test_a_small_bore_takes_only_its_own_pad() -> None:
    doc = _doc(mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(5, 5), diameter=1.5),))
    assert consumed_holes(doc) == frozenset({hole_key(HoleCoord(5, 5))})


def test_a_board_with_no_mounting_holes_computes_no_keepout() -> None:
    assert consumed_holes(_doc()) == frozenset()


def test_a_pin_on_a_hole_with_no_pad_is_a_drc_error() -> None:
    doc = _doc(
        components=(_resistor("R1", HoleCoord(5, 5)),),
        mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(5, 5)),),
    )
    conflicts = [v for v in run_drc(doc, LOOKUP) if v.rule == "mounting-hole-conflict"]
    assert conflicts
    assert all(v.severity == "error" for v in conflicts)
    assert "mh-1" in conflicts[0].message


def test_a_conductor_soldered_to_a_hole_with_no_pad_is_a_drc_error() -> None:
    doc = _doc(
        conductors=(SolderTraceConductor(id="t1", path=(HoleCoord(4, 5), HoleCoord(5, 5))),),
        mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(5, 5)),),
    )
    conflicts = [v for v in run_drc(doc, LOOKUP) if v.rule == "mounting-hole-conflict"]
    assert [v.conductor_ids for v in conflicts] == [("t1",), ("t1",)], (
        "both pads under the bore are gone, and a trace is soldered at every one it crosses"
    )


def test_a_body_under_the_screw_head_is_a_warning_not_an_error() -> None:
    """The board is buildable; the screw just cannot be fitted afterwards. Worth saying
    while the layout can still move, which is before a standoff has been cut.

    A big washer two holes clear of the part, so the head reaches the body while the
    3.2 mm bore does not reach any of its pins -- otherwise the conflict error would fire
    too and this would not be testing the clearance rule on its own.
    """
    doc = _doc(
        components=(_resistor("R1", HoleCoord(7, 5)),),
        mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(5, 5), head_diameter=10.0),),
    )
    violations = run_drc(doc, LOOKUP)
    clearance = [v for v in violations if v.rule == "mounting-hole-clearance"]
    assert clearance
    assert clearance[0].severity == "warning"
    assert not [v for v in violations if v.rule == "mounting-hole-conflict"]


def test_a_screw_head_that_does_not_reach_the_part_says_nothing() -> None:
    doc = _doc(
        components=(_resistor("R1", HoleCoord(7, 5)),),
        mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(5, 5)),),
    )
    assert not [v for v in run_drc(doc, LOOKUP) if v.rule.startswith("mounting-hole")]


def test_the_guide_says_to_drill_before_anything_is_soldered() -> None:
    guide = build_guide(_doc(mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(1, 1)),)), LOOKUP)
    assert "drill the 1 mounting hole" in guide.phases[0].summary
    assert "B2" in guide.phases[0].summary
    assert any("drill" in tool.lower() for tool in guide.tools)


def test_mounting_holes_add_and_delete_through_the_bus() -> None:
    bus = _bus(_doc())
    assert bus.dispatch("mounting-hole.add", AddMountingHolePayload(at=HoleCoord(1, 1))).ok
    assert len(bus.document.mounting_holes) == 1
    id_ = bus.document.mounting_holes[0].id
    assert bus.dispatch("mounting-hole.delete", DeleteMountingHolePayload(id=id_)).ok
    assert bus.document.mounting_holes == ()


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (AddMountingHolePayload(at=HoleCoord(99, 99)), "off-board"),
        (AddMountingHolePayload(at=HoleCoord(1, 1), diameter=0), "invalid-mounting-hole"),
        (AddMountingHolePayload(at=HoleCoord(1, 1), diameter=6, head_diameter=3), "invalid-mounting-hole"),
    ],
)
def test_a_mounting_hole_that_makes_no_physical_sense_is_refused(payload: object, code: str) -> None:
    result = _bus(_doc()).dispatch("mounting-hole.add", payload)
    assert not result.ok
    assert result.code == code


def test_two_mounting_holes_cannot_share_a_hole() -> None:
    bus = _bus(_doc())
    bus.dispatch("mounting-hole.add", AddMountingHolePayload(at=HoleCoord(1, 1)))
    result = bus.dispatch("mounting-hole.add", AddMountingHolePayload(at=HoleCoord(1, 1)))
    assert not result.ok
    assert result.code == "duplicate-mounting-hole"


def test_four_corner_holes_are_one_undo_step() -> None:
    bus = _bus(_doc())
    corners = (HoleCoord(1, 1), HoleCoord(10, 1), HoleCoord(1, 8), HoleCoord(10, 8))
    assert bus.dispatch("mounting-hole.addMany", AddMountingHolesPayload(ats=corners)).ok
    assert len(bus.document.mounting_holes) == 4
    bus.undo()
    assert bus.document.mounting_holes == (), "one Ctrl+Z must not leave three holes drilled"


def test_a_batch_with_one_bad_hole_adds_none_of_them() -> None:
    bus = _bus(_doc())
    result = bus.dispatch(
        "mounting-hole.addMany",
        AddMountingHolesPayload(ats=(HoleCoord(1, 1), HoleCoord(99, 99))),
    )
    assert not result.ok
    assert bus.document.mounting_holes == ()


def test_shrinking_the_board_will_not_strand_a_mounting_hole() -> None:
    bus = _bus(_doc(mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(11, 9)),)))
    result = bus.dispatch(
        "board.set", SetBoardPayload(board=dataclasses.replace(BOARD, cols=6, rows=6))
    )
    assert not result.ok
    assert result.code == "would-strand-mounting-hole"


# ---------------------------------------------------------------------------
# Edge connectors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("edge", "expected"),
    [
        ("top", [HoleCoord(2, 0), HoleCoord(3, 0)]),
        ("bottom", [HoleCoord(2, 9), HoleCoord(3, 9)]),
        ("left", [HoleCoord(0, 2), HoleCoord(0, 3)]),
        ("right", [HoleCoord(11, 2), HoleCoord(11, 3)]),
    ],
)
def test_a_run_of_fingers_sits_along_the_edge_it_names(edge: str, expected: list[HoleCoord]) -> None:
    connector = EdgeConnector(id="ec-1", edge=edge, start=2, count=2)  # type: ignore[arg-type]
    assert edge_connector_holes(connector, BOARD) == expected


def test_a_finger_reaches_the_board_edge() -> None:
    """Reaching the edge is the entire point of a finger, and it is measured from the
    edge inward for exactly that reason."""
    connector = EdgeConnector(id="ec-1", edge="bottom", start=0, count=1)
    rect = edge_finger_rect(connector, HoleCoord(0, BOARD.rows - 1), BOARD)
    board_bottom = (BOARD.rows - 1) * BOARD.pitch + BOARD.pitch / 2
    assert rect.y + rect.height == pytest.approx(board_bottom)


def test_a_finger_reaches_its_own_hole_on_a_bordered_board() -> None:
    """A fixed length measured from the edge reaches the hole on a flush-cut board and
    stops short of it on one with a printed border -- and a finger that does not include
    its own hole is not a finger. So the length is derived from the board unless asked
    for."""
    bordered = dataclasses.replace(BOARD, border_x_mm=2.0, border_y_mm=2.0)
    connector = EdgeConnector(id="ec-1", edge="bottom", start=0, count=1)
    hole = HoleCoord(0, bordered.rows - 1)
    rect = edge_finger_rect(connector, hole, bordered)
    centre_y = hole_to_mm(hole, bordered).y

    assert rect.y < centre_y < rect.y + rect.height, "the finger must contain its hole"
    assert default_finger_length_mm(bordered) > default_finger_length_mm(BOARD)


def test_a_hand_written_finger_length_is_taken_as_given() -> None:
    connector = EdgeConnector(id="ec-1", edge="top", start=0, count=1, finger_length=1.9)
    rect = edge_finger_rect(connector, HoleCoord(0, 0), BOARD)
    assert rect.height == pytest.approx(1.9)


def test_a_finger_covers_exactly_one_hole() -> None:
    """The limitation that keeps this feature out of the connectivity engine: a finger is
    its own pad and nothing more, so nothing about what is joined to what changes."""
    connector = EdgeConnector(id="ec-1", edge="bottom", start=3, count=1)
    doc = _doc(edge_connectors=(connector,))
    covered = edge_connector_holes(connector, doc.board)
    assert covered == [HoleCoord(3, 9)]
    rect = edge_finger_rect(connector, covered[0], doc.board)
    # The next hole inward is at row 8; the finger must not reach its pad.
    assert rect.y > (BOARD.rows - 2) * BOARD.pitch + BOARD.pad_diameter / 2


def test_fingers_leave_a_smaller_gap_than_the_pads_they_replace() -> None:
    """A wider pad is a narrower gap, and R5' is about the gap -- so widening a pad into
    a finger has to reach DRC rather than being a drawing."""
    plain = _doc()
    fingered = _doc(edge_connectors=(EdgeConnector(id="ec-1", edge="bottom", start=0, count=4),))
    a, b = HoleCoord(1, 9), HoleCoord(2, 9)
    assert copper_gap_mm(fingered, a, b) < copper_gap_mm(plain, a, b)
    assert copper_gap_mm(fingered, a, b) == pytest.approx(BOARD.pitch - 2.0)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (AddEdgeConnectorPayload(edge="bottom", start=0, count=0), "invalid-edge-connector"),
        (AddEdgeConnectorPayload(edge="bottom", start=10, count=5), "invalid-edge-connector"),
        (AddEdgeConnectorPayload(edge="bottom", start=0, count=2, finger_width=2.54), "invalid-edge-connector"),
        (AddEdgeConnectorPayload(edge="bottom", start=0, count=2, finger_length=3.0), "invalid-edge-connector"),
    ],
)
def test_a_connector_the_model_cannot_express_is_refused(payload: object, code: str) -> None:
    """Fingers as wide as the pitch are one piece of copper across two nets; fingers
    longer than the pitch reach the next hole in and join two rows. Neither is a thing
    the document can hold, so neither may be stored."""
    result = _bus(_doc()).dispatch("edge-connector.add", payload)
    assert not result.ok
    assert result.code == code


def test_two_connectors_cannot_claim_the_same_pad() -> None:
    bus = _bus(_doc())
    assert bus.dispatch("edge-connector.add", AddEdgeConnectorPayload(edge="bottom", start=0, count=6)).ok
    result = bus.dispatch("edge-connector.add", AddEdgeConnectorPayload(edge="bottom", start=4, count=3))
    assert not result.ok
    assert result.code == "overlapping-edge-connector"


def test_connectors_on_different_edges_do_not_clash() -> None:
    bus = _bus(_doc())
    assert bus.dispatch("edge-connector.add", AddEdgeConnectorPayload(edge="bottom", start=0, count=6)).ok
    assert bus.dispatch("edge-connector.add", AddEdgeConnectorPayload(edge="top", start=0, count=6)).ok
    assert len(bus.document.edge_connectors) == 2


def test_an_edge_connector_can_be_deleted() -> None:
    bus = _bus(_doc(edge_connectors=(EdgeConnector(id="ec-1", edge="top", start=0, count=3),)))
    assert bus.dispatch("edge-connector.delete", DeleteEdgeConnectorPayload(id="ec-1")).ok
    assert bus.document.edge_connectors == ()


def test_shrinking_the_board_will_not_strand_a_connector() -> None:
    bus = _bus(_doc(edge_connectors=(EdgeConnector(id="ec-1", edge="top", start=0, count=10),)))
    result = bus.dispatch("board.set", SetBoardPayload(board=dataclasses.replace(BOARD, cols=6)))
    assert not result.ok
    assert result.code == "would-strand-edge-connector"


def test_the_guide_says_the_fingers_are_there_because_nothing_else_will() -> None:
    """No step covers them -- they came with the board -- so without this the builder
    never reads that the board has them at all."""
    doc = _doc(edge_connectors=(EdgeConnector(id="ec-1", edge="bottom", start=2, count=4),))
    codes = {w.code: w.message for w in build_guide(doc, LOOKUP).warnings}
    assert "edge-connector" in codes
    assert "C10" in codes["edge-connector"], "it should name where the run starts"


# ---------------------------------------------------------------------------
# The format
# ---------------------------------------------------------------------------


def test_a_board_using_none_of_this_serializes_exactly_as_before() -> None:
    """THE load-bearing test of this file. Every one of these fields is omitted at its
    default, so a document that uses no new feature produces the bytes a build predating
    them produced. That is what keeps the 15 golden fixtures round-tripping, and it is why
    DOCUMENT_FORMAT_VERSION did not have to move."""
    text = persist.serialize_document(_doc())
    for key in ("padShape", "padLength", "padAxis", "labels", "mountingHoles", "edgeConnectors"):
        assert key not in text, f"{key} leaked into a document that does not use it"


def test_every_new_field_survives_a_round_trip() -> None:
    doc = _doc(
        dataclasses.replace(OBLONG, pad_axis="horizontal", labels=BoardLabels(face="top", row_digits=3)),
        mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(1, 1), diameter=2.8, head_diameter=5.5),),
        edge_connectors=(
            EdgeConnector(
                id="ec-1", edge="left", start=1, count=3, finger_width=1.8, finger_length=2.0, face="both"
            ),
        ),
    )
    text = persist.serialize_document(doc)
    result = persist.deserialize_document(text)
    assert result.ok, result
    assert not result.warnings
    assert result.document == doc
    assert persist.serialize_document(result.document) == text


def test_a_file_written_before_these_features_still_loads() -> None:
    """No `mountingHoles` key, no `edgeConnectors` key, no pad shape -- which is every
    .perf file that exists today."""
    text = persist.serialize_document(_doc())
    assert "mountingHoles" not in text
    result = persist.deserialize_document(text)
    assert result.ok, result
    assert result.document.mounting_holes == ()
    assert result.document.edge_connectors == ()
    assert result.document.board.pad_shape == "round"


def test_an_oblong_board_with_no_length_loads_with_a_warning() -> None:
    """Hand-edited files open rather than locking the user out; the problem is reported."""
    text = persist.serialize_document(_doc()).replace(
        '"drillDiameter": 1', '"drillDiameter": 1,\n    "padShape": "oblong"'
    )
    result = persist.deserialize_document(text)
    assert result.ok, result
    assert any("oblong" in w for w in result.warnings)
    assert result.document.board.pad_shape == "oblong"


def test_mounting_holes_are_written_in_a_stable_order() -> None:
    """Diff stability only -- mounting holes are independent of one another, so document
    order carries no meaning and sorting it keeps a one-hole change to a one-hole diff."""
    scrambled = _doc(
        mounting_holes=(
            MountingHole(id="mh-2", at=HoleCoord(9, 8)),
            MountingHole(id="mh-1", at=HoleCoord(1, 1)),
        )
    )
    ordered = persist.deserialize_document(persist.serialize_document(scrambled))
    assert ordered.ok
    assert [m.id for m in ordered.document.mounting_holes] == ["mh-1", "mh-2"]


# ---------------------------------------------------------------------------
# The ruler and the legend must not say the same thing twice
# ---------------------------------------------------------------------------


def test_a_printed_legend_is_not_shadowed_by_the_editors_own_ruler() -> None:
    """Two sets of the same twenty-four letters, a few millimetres apart and in two
    different styles, reads as a rendering fault rather than as two features. The ruler
    exists for boards that carry no addresses; this board carries them."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication(["perfstudio-tests"])
    from perfstudio.ui.view2d import BoardLegendItem, BoardScene, HoleRulerItem

    printed = _doc(dataclasses.replace(BOARD, border_x_mm=2.0, border_y_mm=2.0, labels=BoardLabels()))
    scene = BoardScene(printed, LOOKUP, side="top", show_rulers=True)
    kinds = {type(item) for item in scene.items()}
    assert BoardLegendItem in kinds
    assert HoleRulerItem not in kinds
    assert scene.legend_is_readable()


def test_the_ruler_comes_back_when_the_legend_is_on_the_far_face() -> None:
    """Seen through the board the legend is a dim ghost, not something to read an address
    off — so the thing that can be read has to be there."""
    from perfstudio.ui.view2d import BoardScene, HoleRulerItem

    board = dataclasses.replace(BOARD, border_x_mm=2.0, border_y_mm=2.0, labels=BoardLabels(face="top"))
    scene = BoardScene(_doc(board), LOOKUP, side="bottom", show_rulers=True)
    assert not scene.legend_is_readable()
    assert HoleRulerItem in {type(item) for item in scene.items()}


def test_a_board_with_no_legend_keeps_its_ruler() -> None:
    from perfstudio.ui.view2d import BoardScene, HoleRulerItem

    scene = BoardScene(_doc(), LOOKUP, side="top", show_rulers=True)
    assert not scene.legend_is_readable()
    assert HoleRulerItem in {type(item) for item in scene.items()}
