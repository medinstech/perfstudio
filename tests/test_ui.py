"""Tests for perfstudio.ui.

Runs entirely headless (QT_QPA_PLATFORM=offscreen, set before PySide6 is imported) so
it works in CI with no display. The load-bearing tests here are the ones that would
catch the two failure modes this rewiring exists to prevent:

  - a UI-side model drifting from the engine (test_scene_item_counts_match_document,
    test_solder_trace_beads_every_hole_wire_fillets_ends): the scene must be built
    purely from a real PerfDocument and the engine's own contacts_every_path_hole
    predicate, never a re-derived copy of either.
  - a mutation that bypasses the command bus (test_drag_dispatches_component_move_*,
    test_undo_after_move_restores_previous_anchor, test_off_board_move_is_refused_*):
    every assertion here is against bus.document, never the scene's items, because the
    scene is only ever a view of that document.
  - the board being drawn backwards (test_hole_screen_round_trip_both_sides): mirroring
    must reflect about geometry.hole_span_mm, not board_size_mm (see the long comment
    on hole_span_mm in geometry.py and the one on hole_to_screen in view2d.py).
"""

from __future__ import annotations

import os
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from perfstudio import persist
from perfstudio.command import CommandBus, CommandContext, create_id_generator
from perfstudio.commands import MoveComponentPayload, create_standard_registry
from perfstudio.footprints import footprint_lookup
from perfstudio.model import Board, HoleCoord, PerfDocument, SolderTraceConductor, WireConductor
from perfstudio.ui.export_pdf import verify_scale
from perfstudio.ui.view2d import BoardScene, ComponentItem, ConductorItem, hole_to_screen, screen_to_hole

GOLDEN = pathlib.Path(__file__).resolve().parent.parent / "tools" / "diffcheck" / "golden" / "dense.perf"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication(["perfstudio-tests"])
    yield app


def _load_dense() -> PerfDocument:
    text = GOLDEN.read_text(encoding="utf-8")
    result = persist.deserialize_document(text)
    assert result.ok, f"golden fixture failed to load: {result}"
    assert not result.warnings, f"unexpected warnings loading golden fixture: {result.warnings}"
    return result.document


def _new_bus(document: PerfDocument) -> CommandBus:
    return CommandBus(document, create_standard_registry(), CommandContext(next_id=create_id_generator()))


def _drag(scene: BoardScene, comp_id: str, new_anchor: HoleCoord) -> list:
    """Simulates a drag-and-release without synthesizing real Qt mouse events:
    ``setPos`` on an item with ItemSendsGeometryChanges set runs exactly the same
    ``itemChange`` snapping code a live drag would, and ``commit_pending_moves`` is the
    same method ``mouseReleaseEvent`` calls.
    """
    item = scene.component_items[comp_id]
    board = scene.document.board
    item.setPos(hole_to_screen(new_anchor, board, scene.side))
    return scene.commit_pending_moves()


# ---------------------------------------------------------------------------
# Scene built from the real document, not a parallel model
# ---------------------------------------------------------------------------


def test_scene_item_counts_match_document() -> None:
    doc = _load_dense()
    lookup = footprint_lookup()
    scene = BoardScene(doc, lookup, side="top")

    # dense.perf deliberately includes one component ("X11", footprint "c-disc-1")
    # whose footprint id is not in the standard registry -- test_drc.py, test_lvs.py
    # and test_connectivity.py all load this same fixture through this same
    # footprint_lookup() and rely on exactly this to exercise their own
    # unknown-footprint handling. The scene must skip it the same way DRC/LVS/
    # connectivity do (never render an item it has no footprint geometry for), so the
    # item count is checked against the RESOLVABLE components, not the raw count.
    resolvable = [c for c in doc.components if lookup(c.footprint_id) is not None]
    assert len(resolvable) < len(doc.components), "fixture no longer exercises the unknown-footprint case"

    assert len(scene.component_items) == len(resolvable)
    component_items = [it for it in scene.items() if isinstance(it, ComponentItem)]
    assert len(component_items) == len(resolvable)
    conductor_items = [it for it in scene.items() if isinstance(it, ConductorItem)]
    assert len(conductor_items) == len(doc.conductors)


# ---------------------------------------------------------------------------
# Every mutation through the command bus
# ---------------------------------------------------------------------------


def test_drag_dispatches_component_move_and_changes_document() -> None:
    doc = _load_dense()
    lookup = footprint_lookup()
    bus = _new_bus(doc)
    scene = BoardScene(bus.document, lookup, side="top", bus=bus)

    comp_id = doc.components[0].id
    original_anchor = doc.components[0].anchor
    new_anchor = HoleCoord(original_anchor.col + 1, original_anchor.row)

    results = _drag(scene, comp_id, new_anchor)

    assert len(results) == 1
    assert results[0].ok, results[0].message
    assert results[0].description  # human-readable, e.g. "Move X1 to U17"

    # Assert against the BUS's document, never the scene's items.
    moved = next(c for c in bus.document.components if c.id == comp_id)
    assert moved.anchor == new_anchor
    assert bus.document is not doc  # a new document, never a mutation of the old one
    original_still = next(c for c in doc.components if c.id == comp_id)
    assert original_still.anchor == original_anchor  # the old document is untouched


def test_undo_after_move_restores_previous_anchor() -> None:
    doc = _load_dense()
    lookup = footprint_lookup()
    bus = _new_bus(doc)
    scene = BoardScene(bus.document, lookup, side="top", bus=bus)

    comp_id = doc.components[0].id
    original_anchor = doc.components[0].anchor
    new_anchor = HoleCoord(original_anchor.col + 1, original_anchor.row)

    results = _drag(scene, comp_id, new_anchor)
    assert results[0].ok

    restored = bus.undo()
    moved_back = next(c for c in restored.components if c.id == comp_id)
    assert moved_back.anchor == original_anchor


def test_off_board_move_is_refused_and_document_unchanged() -> None:
    doc = _load_dense()
    bus = _new_bus(doc)
    comp_id = doc.components[0].id

    # A positive out-of-range column, not a negative one: commands.assert_hole_on_board
    # formats its refusal message with geometry.coord_to_hole_ref, which raises on a
    # negative coordinate by design (it is the STRICT encoder; geometry.format_hole is
    # the crash-safe one meant for exactly this kind of message -- see its docstring).
    # commands.py uses the strict one here, so an off-board move with col < 0 currently
    # raises instead of returning ok=False. That looks like a real bug in the engine's
    # own error-message formatting, but src/perfstudio/ outside ui/ is out of scope for
    # this change, so this test exercises the (also off-board, also refused) case that
    # does not hit it.
    result = bus.dispatch("component.move", MoveComponentPayload(id=comp_id, anchor=HoleCoord(doc.board.cols + 5, 0)))

    assert result.ok is False
    assert result.code == "off-board"
    assert bus.document is doc  # completely unchanged: not even a fresh equal copy


def test_locked_component_move_is_refused_via_the_bus() -> None:
    """The UI deliberately leaves locked components draggable (see the note in
    ComponentItem.__init__) so this refusal path is reachable and its message can be
    surfaced, rather than the item flags silently preventing the attempt.
    """
    doc = _load_dense()
    from dataclasses import replace

    target = doc.components[0]
    locked_components = tuple(replace(c, locked=True) if c.id == target.id else c for c in doc.components)
    locked_doc = replace(doc, components=locked_components)
    bus = _new_bus(locked_doc)

    new_anchor = HoleCoord(target.anchor.col + 1, target.anchor.row)
    result = bus.dispatch("component.move", MoveComponentPayload(id=target.id, anchor=new_anchor))

    assert result.ok is False
    assert result.code == "component-locked"
    assert bus.document is locked_doc


# ---------------------------------------------------------------------------
# Mirroring: the test that catches a board drawn backwards
# ---------------------------------------------------------------------------


def test_hole_screen_round_trip_both_sides() -> None:
    doc = _load_dense()
    board = doc.board
    for side in ("top", "bottom"):
        for col in range(board.cols):
            for row in range(board.rows):
                hole = HoleCoord(col, row)
                point = hole_to_screen(hole, board, side)
                assert screen_to_hole(point, board, side) == hole, f"{side} round-trip failed for {hole}"


def test_bottom_side_actually_reflects_about_hole_span_not_board_size() -> None:
    """A sharper version of the round-trip test: pins down that the reflection axis is
    hole_span_mm, not board_size_mm, by checking a concrete pair of holes rather than
    only checking self-consistency (a bug that reflects consistently about the WRONG
    axis would still pass a pure round-trip check).
    """
    from perfstudio.geometry import board_size_mm, hole_span_mm

    doc = _load_dense()
    board = doc.board
    span_w, _ = hole_span_mm(board)
    size_w, _ = board_size_mm(board)
    assert span_w != size_w  # the whole point: they differ by half a pitch

    first = hole_to_screen(HoleCoord(0, 5), board, "bottom")
    last = hole_to_screen(HoleCoord(board.cols - 1, 5), board, "bottom")
    assert first.x() == pytest.approx((board.cols - 1) * board.pitch)
    assert last.x() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 1:1 print scale
# ---------------------------------------------------------------------------


def test_scale_check_passes_exactly() -> None:
    doc = _load_dense()
    lookup = footprint_lookup()
    scene = BoardScene(doc, lookup, side="top")

    check = verify_scale(scene, doc.board)

    assert check.ok
    assert check.error_mm == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# The heart of the model: beads vs. fillets
# ---------------------------------------------------------------------------


def test_solder_trace_beads_every_hole_wire_fillets_ends_only() -> None:
    board = Board(
        type="pad-per-hole",
        cols=10,
        rows=10,
        pitch=2.54,
        thickness=1.6,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=1.0,
    )
    path = (HoleCoord(2, 2), HoleCoord(3, 2), HoleCoord(4, 2), HoleCoord(4, 3))
    trace = SolderTraceConductor(id="cond-t", path=path, buildup="normal")
    wire = WireConductor(id="cond-w", path=path, kind="bare-wire", side="bottom")

    trace_item = ConductorItem(trace, board, "top")
    wire_item = ConductorItem(wire, board, "top")

    assert len(trace_item.contact_points()) == len(path) == 4
    assert len(wire_item.contact_points()) == 2

    expected_ends = [hole_to_screen(path[0], board, "top"), hole_to_screen(path[-1], board, "top")]
    assert wire_item.contact_points() == expected_ends
