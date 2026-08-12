"""Tests for the board as a thing you buy rather than a grid you configure.

Companion to test_board_features.py, split off because the subject is different. That
file is about what a board CAN have — oblong pads, a printed legend, mounting holes.
This one is about the boards that actually exist: the sizes suppliers stock, the two
families they come in, and the three details that had to be modelled before a rendering
of one could be held up against a photograph of it without the difference being obvious.

Those three, each of which started as a visible mismatch:

  THE BORDER IS NOT THE SAME ON BOTH AXES. A 5 x 7 cm board carries about 2.1 mm at the
  sides and 4.5 mm top and bottom. One figure puts the 1:1 printout millimetres out on
  one axis, and that printout is meant to be taped onto the board.

  THE CORNER HOLES ARE OUTSIDE THE GRID. On every real board the copper is untouched and
  the screws go in the border. A mounting hole pinned to a grid position cannot express
  that, and reports four pads destroyed that are perfectly intact.

  THE EDGE PADS STOP SHORT OF THE EDGE. The strip left outside them is where the row
  numbers are printed. Fingers that run to the edge swallow the legend.
"""

from __future__ import annotations

import dataclasses
import os

import pytest

from perfstudio import persist
from perfstudio.command import CommandBus, CommandContext, create_id_generator
from perfstudio.commands import (
    DEFAULT_BOARD,
    AddMountingHolesPayload,
    ApplyBoardPresetPayload,
    create_empty_document,
    create_standard_registry,
)
from perfstudio.footprints import footprint_lookup
from perfstudio.geometry import (
    STANDARD_PRESETS,
    board_edge_margin_mm,
    board_from_preset,
    board_outline_mm,
    board_size_mm,
    consumed_holes,
    edge_axis,
    edge_finger_rect,
    legend_strip_mm,
    mounting_hole_centre_mm,
    preset_edge_connectors,
    preset_mounting_holes,
    preset_strip_edges,
)
from perfstudio.model import (
    Board,
    DocumentMeta,
    EdgeConnector,
    HoleCoord,
    MountingHole,
    PerfDocument,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

META = DocumentMeta(name="presets", created="2026-01-01", modified="2026-01-01")
LOOKUP = footprint_lookup()
BOARD = dataclasses.replace(DEFAULT_BOARD, cols=12, rows=10)


def _doc(board: Board = BOARD, **fields: object) -> PerfDocument:
    return dataclasses.replace(create_empty_document(META, board), **fields)  # type: ignore[arg-type]


def _bus(doc: PerfDocument) -> CommandBus:
    return CommandBus(doc, create_standard_registry(), CommandContext(next_id=create_id_generator()))


# ---------------------------------------------------------------------------
# The sizes suppliers stock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", STANDARD_PRESETS, ids=lambda p: p.key)
def test_every_preset_is_exactly_its_advertised_size(preset) -> None:
    """The border is solved from the advertised size and the hole count, so a "5 x 7"
    comes out 50.0 x 70.0 mm rather than nearly that."""
    board = board_from_preset(preset, DEFAULT_BOARD)
    width, height = board_size_mm(board)
    assert width == pytest.approx(preset.width_mm, abs=0.05)
    assert height == pytest.approx(preset.height_mm, abs=0.05)
    assert (board.cols, board.rows) == (preset.cols, preset.rows)


def test_a_preset_border_is_not_the_same_on_both_axes() -> None:
    board = board_from_preset(
        next(p for p in STANDARD_PRESETS if p.name == "5 x 7 cm"), DEFAULT_BOARD
    )
    assert board.border_x_mm != pytest.approx(board.border_y_mm)
    assert board_edge_margin_mm(board, "horizontal") != pytest.approx(
        board_edge_margin_mm(board, "vertical")
    )


def test_the_two_board_families_differ_in_the_ways_that_matter() -> None:
    """A phenolic board is single-sided FR-2; an FR-4 one is neither. That is what
    separates the families, so the preset settles it rather than leaving it to be set
    twice and disagreed about once."""
    fr4 = board_from_preset(
        next(p for p in STANDARD_PRESETS if p.name == "5 x 7 cm"), DEFAULT_BOARD
    )
    assert not fr4.single_sided
    assert fr4.material == "FR4"

    phenolic = board_from_preset(
        next(p for p in STANDARD_PRESETS if p.single_sided), DEFAULT_BOARD
    )
    assert phenolic.single_sided
    assert phenolic.material == "FR2"


def test_every_preset_prints_its_own_addresses() -> None:
    """The legend is NOT one of the ways the families differ, which is the correction
    this test exists to hold. The orange pertinax board carries the same A..Z / 01..NN
    print the green one does — it is the cheapest marking on a board to apply, and every
    one of these you can buy has it.

    Getting it wrong is not a cosmetic miss. With no legend the editor falls back to its
    own ruler, which is drawn OUTSIDE the board in screen pixels; the addresses then
    exist on the screen and not on the board in your hand, and they are missing from the
    3D view and the 1:1 printout entirely.
    """
    for preset in STANDARD_PRESETS:
        board = board_from_preset(preset, DEFAULT_BOARD)
        assert board.labels is not None, f"{preset.name} ({preset.family}) prints nothing"
        assert board.labels.row_digits == 2, "these boards print 01, not 1"


def test_a_preset_does_not_disturb_the_pitch_or_the_pad() -> None:
    """A preset is a board SIZE. Pitch, pad and drill belong to the stock, and a preset
    that quietly changed them would make "5 x 7" mean something different each time."""
    for preset in STANDARD_PRESETS:
        board = board_from_preset(preset, DEFAULT_BOARD)
        assert board.pitch == DEFAULT_BOARD.pitch
        assert board.pad_diameter == DEFAULT_BOARD.pad_diameter
        assert board.drill_diameter == DEFAULT_BOARD.drill_diameter


def test_every_preset_survives_the_file_format() -> None:
    for preset in STANDARD_PRESETS:
        doc = _doc(board_from_preset(preset, DEFAULT_BOARD))
        result = persist.deserialize_document(persist.serialize_document(doc))
        assert result.ok, f"{preset.name}: {result}"
        assert result.document.board == doc.board


# ---------------------------------------------------------------------------
# Single-sided boards
# ---------------------------------------------------------------------------


def test_a_single_sided_board_has_copper_on_one_face_only() -> None:
    """The cheap brown phenolic board: from the component side there is no pad to solder
    to at all. The HOLES stay on both faces -- a face with neither renders as a blank
    slab that says nothing about where anything goes."""
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication(["perfstudio-tests"])
    from perfstudio.ui.view2d import BoardScene

    doc = _doc(dataclasses.replace(BOARD, single_sided=True, material="FR2"))
    top = BoardScene(doc, LOOKUP, side="top", show_rulers=False)
    bottom = BoardScene(doc, LOOKUP, side="bottom", show_rulers=False)

    assert top.pad_grid is not None and bottom.pad_grid is not None
    assert not top.pad_grid.copper
    assert bottom.pad_grid.copper


def test_a_double_sided_board_has_copper_on_both() -> None:
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication(["perfstudio-tests"])
    from perfstudio.ui.view2d import BoardScene

    for side in ("top", "bottom"):
        scene = BoardScene(_doc(), LOOKUP, side=side, show_rulers=False)
        assert scene.pad_grid is not None and scene.pad_grid.copper


def test_single_sidedness_survives_the_file_format() -> None:
    doc = _doc(dataclasses.replace(BOARD, single_sided=True))
    text = persist.serialize_document(doc)
    assert "singleSided" in text
    result = persist.deserialize_document(text)
    assert result.ok and result.document.board.single_sided


def test_a_double_sided_board_says_nothing_about_it_in_the_file() -> None:
    """Omitted at its default like every other addition, which is what keeps a board that
    uses none of these features byte-identical to what earlier builds wrote."""
    assert "singleSided" not in persist.serialize_document(_doc())


# ---------------------------------------------------------------------------
# Mounting holes in the border, where the real boards put them
# ---------------------------------------------------------------------------


BORDERED = dataclasses.replace(BOARD, border_x_mm=3.5, border_y_mm=3.5)


def test_a_corner_hole_pushed_into_the_border_destroys_no_pads() -> None:
    on_grid = _doc(
        BORDERED, mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(0, 0), diameter=2.2),)
    )
    in_border = _doc(
        BORDERED,
        mounting_holes=(
            MountingHole(
                id="mh-1", at=HoleCoord(0, 0), offset_x_mm=-3.0, offset_y_mm=-3.0, diameter=2.2
            ),
        ),
    )
    assert consumed_holes(on_grid), "on the grid it takes at least its own pad"
    assert consumed_holes(in_border) == frozenset()


def test_the_offset_moves_the_bore_and_the_screw_head_together() -> None:
    mount = MountingHole(id="mh-1", at=HoleCoord(2, 3), offset_x_mm=-1.5, offset_y_mm=0.5)
    centre = mounting_hole_centre_mm(mount, BORDERED)
    assert centre.x == pytest.approx(2 * BORDERED.pitch - 1.5)
    assert centre.y == pytest.approx(3 * BORDERED.pitch + 0.5)


def test_a_batch_of_corner_holes_can_go_to_four_different_corners() -> None:
    """One shared offset would send all four the same way, which is a corner hole in
    exactly one of them."""
    bus = _bus(_doc(BORDERED))
    result = bus.dispatch(
        "mounting-hole.addMany",
        AddMountingHolesPayload(
            ats=(HoleCoord(0, 0), HoleCoord(11, 0), HoleCoord(0, 9), HoleCoord(11, 9)),
            offsets=((-3, -3), (3, -3), (-3, 3), (3, 3)),
            diameter=2.2,
        ),
    )
    assert result.ok
    assert consumed_holes(bus.document) == frozenset(), "not one pad lost"


def test_a_batch_with_the_wrong_number_of_offsets_is_refused() -> None:
    bus = _bus(_doc())
    result = bus.dispatch(
        "mounting-hole.addMany",
        AddMountingHolesPayload(ats=(HoleCoord(1, 1), HoleCoord(2, 2)), offsets=((0.0, 0.0),)),
    )
    assert not result.ok
    assert result.code == "offset-count-mismatch"


def test_a_mounting_offset_survives_the_file_format() -> None:
    doc = _doc(
        BORDERED,
        mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(0, 0), offset_x_mm=-3.0, offset_y_mm=-3.0),),
    )
    result = persist.deserialize_document(persist.serialize_document(doc))
    assert result.ok
    assert result.document.mounting_holes == doc.mounting_holes


# ---------------------------------------------------------------------------
# Fingers that stop short of the edge, so the legend has somewhere to go
# ---------------------------------------------------------------------------


def test_fingers_leave_the_legend_the_strip_they_were_inset_by() -> None:
    board = dataclasses.replace(BOARD, border_x_mm=3.0, border_y_mm=3.0)
    plain = _doc(board)
    inset = _doc(
        board,
        edge_connectors=(
            EdgeConnector(id="ec-1", edge="left", start=0, count=board.rows, inset_mm=1.5),
        ),
    )
    flush = _doc(
        board,
        edge_connectors=(
            EdgeConnector(id="ec-1", edge="left", start=0, count=board.rows, inset_mm=0.0),
        ),
    )
    assert legend_strip_mm(inset, "horizontal") == pytest.approx(1.5)
    assert legend_strip_mm(flush, "horizontal") == pytest.approx(0.0)
    # The other axis carries no fingers, so it keeps the whole margin less half a pad.
    assert legend_strip_mm(inset, "vertical") == legend_strip_mm(plain, "vertical")


def test_an_inset_finger_stops_short_of_the_board_edge_but_still_holds_its_hole() -> None:
    board = dataclasses.replace(BOARD, border_x_mm=3.0, border_y_mm=3.0)
    connector = EdgeConnector(id="ec-1", edge="left", start=0, count=1, inset_mm=1.5)
    rect = edge_finger_rect(connector, HoleCoord(0, 0), board)

    assert rect.x == pytest.approx(board_outline_mm(board).x + 1.5)
    assert rect.x < 0 < rect.x + rect.width


def test_an_inset_survives_the_file_format() -> None:
    doc = _doc(
        edge_connectors=(EdgeConnector(id="ec-1", edge="left", start=0, count=3, inset_mm=1.5),)
    )
    result = persist.deserialize_document(persist.serialize_document(doc))
    assert result.ok
    assert result.document.edge_connectors == doc.edge_connectors


# ---------------------------------------------------------------------------
# A preset is a product, not a grid size
# ---------------------------------------------------------------------------


def test_a_green_board_arrives_with_its_finger_strips_and_corner_holes() -> None:
    """Choosing "5 x 7 cm, double-sided" has to produce the board that comes in the
    envelope: oblong pads down two edges, a screw hole in each corner, a printed legend.
    A bare grid would be describing a board nobody sells."""
    preset = next(p for p in STANDARD_PRESETS if not p.single_sided and p.name == "5 x 7 cm")
    board = board_from_preset(preset, DEFAULT_BOARD)
    connectors = preset_edge_connectors(preset, board)
    holes = preset_mounting_holes(preset, board)

    assert len(connectors) == 2
    assert {c.edge for c in connectors} == set(preset_strip_edges(board))
    assert all(c.inset_mm > 0 for c in connectors), "the legend needs the strip outside them"
    assert len(holes) == 4
    assert board.labels is not None


def test_an_orange_phenolic_board_arrives_with_its_legend_and_nothing_else() -> None:
    """Copper on one face, round pads everywhere, no fingers, no corner holes — but the
    printed addresses are there, because they are on the real board."""
    preset = next(p for p in STANDARD_PRESETS if p.single_sided)
    board = board_from_preset(preset, DEFAULT_BOARD)

    assert preset_edge_connectors(preset, board) == ()
    assert preset_mounting_holes(preset, board) == ()
    assert board.single_sided
    assert board.labels is not None
    assert board.pad_shape == "round"


def test_the_finger_strips_go_on_the_edges_with_room_for_them() -> None:
    """Derived from the border rather than named, because the answer flips with the
    aspect ratio -- and a strip down the cramped side has nowhere to put the legend."""
    for preset in STANDARD_PRESETS:
        if preset.single_sided:
            continue
        board = board_from_preset(preset, DEFAULT_BOARD)
        edges = preset_strip_edges(board)
        wider_axis = "vertical" if board.border_y_mm > board.border_x_mm else "horizontal"
        assert all(edge_axis(edge) == wider_axis for edge in edges)


def test_the_corner_holes_of_every_green_preset_destroy_no_pads() -> None:
    for preset in STANDARD_PRESETS:
        if preset.single_sided:
            continue
        board = board_from_preset(preset, DEFAULT_BOARD)
        doc = _doc(board, mounting_holes=preset_mounting_holes(preset, board))
        assert consumed_holes(doc) == frozenset(), f"{preset.name} loses pads to its own screws"


def test_applying_a_preset_is_one_undo_step() -> None:
    """Board, fingers and corner holes are one decision. Four commands would put four
    entries in the history and leave a half-applied board partway down the undo stack."""
    preset = next(p for p in STANDARD_PRESETS if not p.single_sided and p.name == "5 x 7 cm")
    bus = _bus(_doc())
    board = board_from_preset(preset, bus.document.board)

    result = bus.dispatch(
        "board.applyPreset",
        ApplyBoardPresetPayload(
            board=board,
            edge_connectors=preset_edge_connectors(preset, board),
            mounting_holes=preset_mounting_holes(preset, board),
        ),
    )
    assert result.ok, result.message
    assert len(bus.history()) == 1
    assert (bus.document.board.cols, bus.document.board.rows) == (preset.cols, preset.rows)
    assert len(bus.document.edge_connectors) == 2
    assert len(bus.document.mounting_holes) == 4

    bus.undo()
    assert bus.document.edge_connectors == ()
    assert bus.document.mounting_holes == ()


def test_swapping_between_two_presets_does_not_keep_the_old_boards_fingers() -> None:
    """The fingers belong to the board being replaced. Merging them would leave a run
    along an edge that no longer has the rows for it."""
    big = next(p for p in STANDARD_PRESETS if not p.single_sided and p.name == "9 x 15 cm")
    small = next(p for p in STANDARD_PRESETS if not p.single_sided and p.name == "3 x 7 cm")
    bus = _bus(_doc())

    for preset in (big, small):
        board = board_from_preset(preset, bus.document.board)
        result = bus.dispatch(
            "board.applyPreset",
            ApplyBoardPresetPayload(
                board=board,
                edge_connectors=preset_edge_connectors(preset, board),
                mounting_holes=preset_mounting_holes(preset, board),
            ),
        )
        assert result.ok, f"{preset.name}: {result.message}"

    board = bus.document.board
    assert (board.cols, board.rows) == (small.cols, small.rows)
    for connector in bus.document.edge_connectors:
        limit = board.cols if connector.edge in ("top", "bottom") else board.rows
        assert connector.start + connector.count <= limit


def test_a_preset_still_refuses_to_strand_a_part() -> None:
    """Shrinking is the one thing a preset does that can destroy work, so it goes through
    the same check ``board.set`` uses rather than round it."""
    from perfstudio.model import ComponentInstance

    big = next(p for p in STANDARD_PRESETS if not p.single_sided and p.name == "9 x 15 cm")
    board = board_from_preset(big, DEFAULT_BOARD)
    doc = _doc(
        board,
        components=(
            ComponentInstance(
                id="cmp-1", ref="R1", value="1k", footprint_id="r-axial-5",
                anchor=HoleCoord(board.cols - 1, board.rows - 1),
            ),
        ),
    )
    bus = _bus(doc)
    small = next(p for p in STANDARD_PRESETS if not p.single_sided and p.name == "2 x 8 cm")
    tiny = board_from_preset(small, board)
    result = bus.dispatch("board.applyPreset", ApplyBoardPresetPayload(board=tiny))

    assert not result.ok
    assert result.code == "would-strand-component"
    assert bus.document.board.cols == board.cols, "and nothing moved"
