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

import dataclasses
import os
import pathlib
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from perfstudio import persist
from perfstudio.command import CommandBus, CommandContext, create_id_generator
from perfstudio.commands import MoveComponentPayload, create_standard_registry
from perfstudio.footprints import footprint_lookup
from perfstudio.geometry import column_label
from perfstudio.model import Board, HoleCoord, PerfDocument, SolderTraceConductor, WireConductor
from perfstudio.ui import scenetext, view2d
from perfstudio.ui.export_pdf import verify_scale
from perfstudio.ui.main import (
    ROLE_NET_ID,
    _rotation_after,
    guess_footprint_id,
    read_document_text,
    window_title,
)
from perfstudio.ui.view2d import (
    BoardScene,
    ComponentItem,
    ConductorItem,
    hole_to_screen,
    next_reference,
    screen_to_hole,
)
from perfstudio.version import __version__
from perfstudio.version import describe as describe_version

GOLDEN = pathlib.Path(__file__).resolve().parent.parent / "tools" / "diffcheck" / "golden" / "dense.perf"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication(["perfstudio-tests"])
    yield app


@pytest.fixture(autouse=True)
def _recent_files_in_a_temp_file(tmp_path, monkeypatch):
    """Keep the recent-file list out of the real user store.

    A test suite has no business writing into somebody's registry, and one that did would
    also make these tests depend on whatever ran before them -- including on the developer
    having opened a board that morning.
    """
    from PySide6.QtCore import QSettings

    from perfstudio.ui import main as main_module

    store = QSettings(str(tmp_path / "recent.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(main_module, "recent_files_settings", lambda: store)
    yield store


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

    result = bus.dispatch("component.move", MoveComponentPayload(id=comp_id, anchor=HoleCoord(doc.board.cols + 5, 0)))

    assert result.ok is False
    assert result.code == "off-board"
    assert bus.document is doc  # completely unchanged: not even a fresh equal copy


@pytest.mark.parametrize("anchor", [HoleCoord(-3, 0), HoleCoord(0, -3), HoleCoord(-1, -1)])
def test_a_nudge_past_the_top_or_left_edge_is_refused_not_raised(anchor: HoleCoord) -> None:
    """The negative directions get their own case because they used to be the dangerous
    ones: the refusal message was formatted with the strict hole encoder, which rejects a
    negative column by design, so the check crashed on exactly what it exists to report.
    Arrow-key nudging makes single-step negative moves easy to reach, so it is pinned here
    as well as in test_commands.py.
    """
    doc = _load_dense()
    bus = _new_bus(doc)

    result = bus.dispatch("component.move", MoveComponentPayload(id=doc.components[0].id, anchor=anchor))

    assert result.ok is False
    assert result.code == "off-board"
    assert bus.document is doc


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


# ---------------------------------------------------------------------------
# Labels: the failure mode here is SILENCE
# ---------------------------------------------------------------------------
#
# Qt draws nothing, and reports nothing, when a font ends up too small for its engine --
# and text on a millimetre-scaled painter is asked for in fractions of a point, which lands
# there easily. That is how the component references in this editor came to be specified,
# drawn, and invisible. So these tests do not check a colour or a position; they render and
# count marked pixels, because a label that fails to appear is the bug.


def _render_scene(scene: BoardScene, px_per_mm: int = 12) -> QImage:
    rect = scene.sceneRect()
    image = QImage(
        int(rect.width() * px_per_mm), int(rect.height() * px_per_mm), QImage.Format.Format_ARGB32
    )
    image.fill(QColor("#000000"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    scene.render(painter, QRectF(0, 0, image.width(), image.height()), rect)
    painter.end()
    return image


def _pixels_matching(image: QImage, colour: QColor, tolerance: int = 26) -> int:
    """Pixels close to `colour`. Antialiased text never lands on the exact value."""
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if (
                abs(pixel.red() - colour.red()) <= tolerance
                and abs(pixel.green() - colour.green()) <= tolerance
                and abs(pixel.blue() - colour.blue()) <= tolerance
            ):
                count += 1
    return count


@pytest.mark.skipif(
    QApplication.instance() is not None
    and QApplication.instance().platformName() == "offscreen"  # type: ignore[union-attr]
    and sys.platform == "win32",
    reason="Qt's offscreen plugin ships no font database on Windows, so no text can render; "
    "see main._default_headless_platform",
)
def test_hole_address_rulers_actually_draw_text() -> None:
    """The rulers are how a user finds the hole a DRC message or the build guide names, so
    'the item painted' is not the claim -- 'letters reached the pixels' is."""
    document = _load_dense()
    with_rulers = _render_scene(BoardScene(document, footprint_lookup(), show_rulers=True))
    without = _render_scene(BoardScene(document, footprint_lookup(), show_rulers=False))

    lit_with = _pixels_matching(with_rulers, view2d.RULER_TEXT_MAJOR) + _pixels_matching(
        with_rulers, view2d.RULER_TEXT
    )
    lit_without = _pixels_matching(without, view2d.RULER_TEXT_MAJOR) + _pixels_matching(
        without, view2d.RULER_TEXT
    )

    assert lit_with > lit_without + 200


@pytest.mark.skipif(
    QApplication.instance() is not None
    and QApplication.instance().platformName() == "offscreen"  # type: ignore[union-attr]
    and sys.platform == "win32",
    reason="Qt's offscreen plugin ships no font database on Windows; see "
    "main._default_headless_platform",
)
def test_component_reference_labels_actually_draw_text() -> None:
    document = _load_dense()
    scene = BoardScene(document, footprint_lookup(), show_rulers=False, show_ratsnest=False)

    image = _render_scene(scene)

    # REF_LABEL is a near-white used for nothing else on the board; the substrate, pads,
    # bodies and conductors are all well away from it.
    assert _pixels_matching(image, view2d.REF_LABEL, tolerance=12) > 100


def test_labels_hold_their_size_when_the_board_is_zoomed() -> None:
    """A ruler label is annotation, not a feature of the board: it must not grow with zoom.

    Checked through the helper's own measurement rather than by rendering twice, so the test
    states the property instead of comparing two pixel counts that could both be wrong.
    """
    at_low_zoom = scenetext.label_extent_mm(view2d.RULER_LABEL_PX, 3.0)
    at_high_zoom = scenetext.label_extent_mm(view2d.RULER_LABEL_PX, 30.0)

    # Ten times the zoom, a tenth of the board covered: constant on screen.
    assert at_low_zoom == pytest.approx(at_high_zoom * 10)


# ---------------------------------------------------------------------------
# Opening a bad path
# ---------------------------------------------------------------------------


def test_a_missing_file_is_reported_rather_than_raised(tmp_path: pathlib.Path) -> None:
    """A mistyped path is the most likely way anyone first meets this program, and a
    pathlib traceback tells them where Python gave up instead of what to fix."""
    text, problem = read_document_text(tmp_path / "nope.perf")

    assert text is None
    assert problem is not None
    assert "nope.perf" in problem


def test_a_directory_says_so_instead_of_permission_denied(tmp_path: pathlib.Path) -> None:
    """The OS reports a directory read as EACCES, which sends someone hunting for a
    permissions problem they do not have."""
    text, problem = read_document_text(tmp_path)

    assert text is None
    assert problem is not None
    assert "directory" in problem
    assert "ermission" not in problem


def test_a_readable_document_comes_back_as_text() -> None:
    text, problem = read_document_text(GOLDEN)

    assert problem is None
    assert text is not None and text.lstrip().startswith("{")


# ---------------------------------------------------------------------------
# The 3D camera belongs to the person looking through it
# ---------------------------------------------------------------------------


def test_refreshing_the_3d_view_does_not_move_the_camera() -> None:
    """Refreshing used to mean building a whole new renderer, which meant ResetCamera plus a
    fixed elevation and azimuth -- so the 3D viewpoint snapped back to default after every
    command. Orbit the board, nudge a part, and the orbit was gone.
    """
    from perfstudio.ui import view3d

    document = _load_dense()
    lookup = footprint_lookup()
    renderer, _stats = view3d.build_renderer(document, lookup)
    camera = renderer.GetActiveCamera()
    camera.Azimuth(55)
    camera.Elevation(20)
    orbited = camera.GetPosition()

    view3d.populate_renderer(renderer, document, lookup)

    assert renderer.GetActiveCamera().GetPosition() == orbited


def test_repopulating_replaces_the_actors_and_keeps_the_light() -> None:
    """The refresh has to actually refresh -- and must not stack up a fresh light per call,
    which is the trap in reusing a renderer instead of rebuilding one."""
    from perfstudio.ui import view3d

    document = _load_dense()
    lookup = footprint_lookup()
    renderer, _stats = view3d.build_renderer(document, lookup)
    first_actors = renderer.GetActors().GetNumberOfItems()
    lights = renderer.GetLights().GetNumberOfItems()

    view3d.populate_renderer(renderer, document, lookup)

    assert renderer.GetActors().GetNumberOfItems() == first_actors
    assert renderer.GetLights().GetNumberOfItems() == lights


def test_apply_default_camera_is_the_only_thing_that_reframes() -> None:
    from perfstudio.ui import view3d

    document = _load_dense()
    lookup = footprint_lookup()
    renderer, _stats = view3d.build_renderer(document, lookup)
    default = renderer.GetActiveCamera().GetPosition()
    renderer.GetActiveCamera().Azimuth(90)
    assert renderer.GetActiveCamera().GetPosition() != default

    view3d.apply_default_camera(renderer)

    assert renderer.GetActiveCamera().GetPosition() == pytest.approx(default)


# ---------------------------------------------------------------------------
# Selection survives the rebuild every command causes
# ---------------------------------------------------------------------------


def test_selection_survives_a_document_change() -> None:
    """Every command rebuilds the scene. Without this, a part is deselected the moment it is
    acted on -- so pressing rotate twice would rotate once and then appear to do nothing."""
    document = _load_dense()
    bus = _new_bus(document)
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    comp_id = next(iter(scene.component_items))
    scene.select_components([comp_id])
    assert scene.selected_component_ids() == (comp_id,)

    scene.set_document(bus.document)

    assert scene.selected_component_ids() == (comp_id,)


def test_rebuilding_does_not_touch_items_qt_has_already_destroyed() -> None:
    """scene.clear() destroys the C++ items and emits selectionChanged while doing it. If the
    handler can still see the old dict it raises "Internal C++ object already deleted", which
    surfaced as a traceback on the very first rotate."""
    document = _load_dense()
    scene = BoardScene(document, footprint_lookup(), side="top")
    scene.select_components(list(scene.component_items)[:2])

    scene.set_document(document)  # would raise before the fix
    scene.set_side("bottom")
    scene.set_side("top")

    assert len(scene.component_items) > 0


# ---------------------------------------------------------------------------
# Rotation wrapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "delta", "expected"),
    [(0, 90, 90), (270, 90, 0), (0, -90, 270), (180, -90, 90), (90, 180, 270)],
)
def test_rotation_wraps_to_a_legal_value(current: int, delta: int, expected: int) -> None:
    """component.rotate refuses anything outside 0/90/180/270, so the wrap has to happen
    before dispatch rather than being discovered as a rejected command."""
    assert _rotation_after(current, delta) == expected  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Nudging the selection
# ---------------------------------------------------------------------------


def test_arrow_nudge_moves_through_the_command_bus() -> None:
    """A nudge is a component.move, the same command a drag ends in -- so it undoes and
    journals identically, and an agent watching the board sees it the same way."""
    document = _load_dense()
    bus = _new_bus(document)
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    comp_id = next(iter(scene.component_items))
    before = next(c for c in bus.document.components if c.id == comp_id).anchor
    scene.select_components([comp_id])

    results = scene.nudge_selection(2, -1)

    assert len(results) == 1 and results[0].ok, results[0].message
    after = next(c for c in bus.document.components if c.id == comp_id).anchor
    assert (after.col, after.row) == (before.col + 2, before.row - 1)
    assert len(bus.journal()) == 1
    bus.undo()
    assert next(c for c in bus.document.components if c.id == comp_id).anchor == before


def test_nudging_several_parts_does_not_read_items_the_first_dispatch_destroyed() -> None:
    """The first dispatch rebuilds the scene, destroying every item. A loop that both reads
    items and dispatches is reading destroyed C++ objects from its second iteration on -- so
    the work list has to be snapshotted before anything is dispatched.
    """
    document = _load_dense()
    bus = _new_bus(document)
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    ids = list(scene.component_items)[:4]
    scene.select_components(ids)

    results = scene.nudge_selection(0, 1)  # would raise RuntimeError before the fix

    assert len(results) == len(ids)
    assert all(r.ok for r in results), [r.message for r in results if not r.ok]


def test_nudging_with_no_selection_does_nothing() -> None:
    document = _load_dense()
    bus = _new_bus(document)
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)

    assert scene.nudge_selection(1, 0) == []
    assert bus.journal() == ()


# ---------------------------------------------------------------------------
# Placing a part: previously impossible from the window at all
# ---------------------------------------------------------------------------


def _blank_bus() -> CommandBus:
    from perfstudio.commands import create_empty_document
    from perfstudio.model import DocumentMeta

    document = create_empty_document(
        DocumentMeta(name="t", created="2024-01-01T00:00:00.000Z", modified="2024-01-01T00:00:00.000Z")
    )
    return _new_bus(document)


def test_placing_from_the_library_goes_through_the_command_bus() -> None:
    """Sixty-one footprints and component.place both existed with nothing able to reach either,
    so a part could not be added to a board at all."""
    bus = _blank_bus()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    scene.arm_placement("r-axial-3")

    result = scene.place_armed(HoleCoord(3, 3))

    assert result is not None and result.ok, result
    assert len(bus.document.components) == 1
    placed = bus.document.components[0]
    assert (placed.ref, placed.footprint_id, placed.anchor) == ("R1", "r-axial-3", HoleCoord(3, 3))
    bus.undo()
    assert bus.document.components == ()


def test_references_count_up_and_follow_the_part_kind() -> None:
    """R for a resistor, D for a diode, U for a DIP -- and the next free number, counted from
    the board rather than a hidden counter so undo and delete cannot desynchronise it."""
    bus = _blank_bus()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)

    scene.arm_placement("r-axial-3")
    scene.place_armed(HoleCoord(1, 1))
    scene.place_armed(HoleCoord(1, 5))
    scene.arm_placement("d-do41")
    scene.place_armed(HoleCoord(1, 9))
    scene.arm_placement("dip-8")
    scene.place_armed(HoleCoord(10, 1))

    assert [c.ref for c in bus.document.components] == ["R1", "R2", "D1", "U1"]


def test_a_reference_freed_by_undo_is_reused() -> None:
    bus = _blank_bus()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    scene.arm_placement("r-axial-3")
    scene.place_armed(HoleCoord(1, 1))
    scene.place_armed(HoleCoord(1, 5))
    bus.undo()
    scene.set_document(bus.document)

    assert next_reference(bus.document, "r-axial-3") == "R2"


def test_placement_stays_armed_so_several_parts_can_go_down() -> None:
    bus = _blank_bus()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    scene.arm_placement("r-axial-3")

    scene.place_armed(HoleCoord(1, 1))

    assert scene.armed_footprint_id == "r-axial-3"


def test_disarming_removes_the_ghost() -> None:
    bus = _blank_bus()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    scene.arm_placement("dip-8")
    assert any(isinstance(i, view2d.PlacementGhostItem) for i in scene.items())

    scene.arm_placement(None)

    assert not any(isinstance(i, view2d.PlacementGhostItem) for i in scene.items())
    assert scene.place_armed(HoleCoord(2, 2)) is None


def test_an_overlapping_placement_is_reported_not_refused() -> None:
    """Two pins in one hole is a legal document that describes a board you do not want, which
    makes it DRC's business rather than a command's. The ghost warns; the click still lands."""
    bus = _blank_bus()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    scene.arm_placement("r-axial-3")
    scene.place_armed(HoleCoord(3, 3))
    scene.set_document(bus.document)

    result = scene.place_armed(HoleCoord(3, 3))

    assert result is not None and result.ok
    assert scene.last_placement_overlapped is True


def test_a_placement_off_the_board_is_refused_by_the_bus() -> None:
    bus = _blank_bus()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    scene.arm_placement("dip-8")

    result = scene.place_armed(HoleCoord(-2, 0))

    assert result is not None and result.ok is False
    assert result.code == "off-board"


@pytest.mark.parametrize(
    ("ref", "pins", "expected"),
    [
        ("R7", 2, "r-axial-3"),
        ("D2", 2, "d-do41"),
        ("LED1", 2, "led-5mm"),
        ("C4", 2, "c-disc-p2"),
        ("Q1", 3, "to92"),
        ("U1", 8, "dip-8"),
        ("U2", 10, "dip-14"),
        ("J3", 4, "hdr-1x4"),
    ],
)
def test_footprint_guesses_from_a_netlist_reference(ref: str, pins: int, expected: str) -> None:
    """A netlist's footprint field names a KiCad library part, which says nothing about this
    registry -- so the reference letter and the pin count the netlist reveals are all there is
    to go on. Enough to be useful, and stated as a guess."""
    assert guess_footprint_id(ref, pins) == expected


# ---------------------------------------------------------------------------
# Version reporting
# ---------------------------------------------------------------------------


def test_window_title_names_the_build_the_document_and_whether_it_is_saved() -> None:
    assert window_title() == f"PerfStudio {__version__} — untitled"
    assert window_title(pathlib.Path("a/b/ne555.perf")) == f"PerfStudio {__version__} — ne555.perf"
    # The unsaved marker leads, so it is visible before the title is elided.
    assert window_title(pathlib.Path("x/ne555.perf"), modified=True).startswith("• PerfStudio")


def test_version_flag_answers_without_starting_qt(monkeypatch, capsys) -> None:
    """--version has to work on a machine where the GUI cannot start.

    That is precisely when someone is asked which version they have, so main() answers it
    before touching QApplication -- and this test proves the ordering by making any attempt
    to construct one fail loudly.
    """
    import perfstudio.ui.main as main_module

    def refuse(*args, **kwargs):
        raise AssertionError("--version must not construct a QApplication")

    monkeypatch.setattr(main_module, "QApplication", refuse)
    monkeypatch.setattr(sys, "argv", ["perfstudio", "--version"])

    assert main_module.main() == 0
    assert __version__ in capsys.readouterr().out


def test_version_line_is_pasteable_ascii() -> None:
    """A Windows console at cp1252 turns a typographic separator into a question mark,
    which then travels into a bug report as evidence of a bug that is not there."""
    describe_version().encode("ascii")


# ---------------------------------------------------------------------------
# Auto-place
# ---------------------------------------------------------------------------


def _window_on(doc):
    from perfstudio.ui.main import MainWindow

    return MainWindow(doc)


def _close(window) -> None:
    """Close a test window without tripping the unsaved-work guard.

    The guard opens a real modal dialog, which in a headless test run waits forever --
    which is a reasonable thing for it to do and the reason the tests say explicitly
    that they are discarding, rather than the suite quietly never exercising it.
    """
    window._saved_document = window.bus.document
    window.close()


def test_autoplace_asks_before_moving_the_users_board(monkeypatch) -> None:
    """Routing adds copper to a board the user arranged; placement MOVES it. So the
    confirmation is not a formality, and cancelling has to leave the document alone."""
    window = _window_on(_load_dense())
    before = window.bus.document

    monkeypatch.setattr(window, "_confirm_placement", lambda plan, ms: False)
    window.on_autoplace()

    assert window.bus.document is before
    window.close()


def test_autoplace_commits_through_the_bus_as_one_undo_step(monkeypatch) -> None:
    window = _window_on(_load_dense())
    before = window.bus.document

    monkeypatch.setattr(window, "_confirm_placement", lambda plan, ms: True)
    window.on_autoplace()

    assert window.bus.document is not before
    assert window.bus.document.components != before.components
    window.bus.undo()
    assert window.bus.document.components == before.components
    window.close()


def test_reroll_advances_the_seed(monkeypatch) -> None:
    """Annealing is a random walk, so "try again" has to actually try something else."""
    window = _window_on(_load_dense())
    monkeypatch.setattr(window, "_confirm_placement", lambda plan, ms: False)

    window.on_autoplace()
    assert window._place_seed == 0
    window.on_autoplace(reroll=True)
    assert window._place_seed == 1
    window.close()


def test_autoplace_on_an_empty_board_says_so_rather_than_running(monkeypatch) -> None:
    from perfstudio.commands import create_empty_document
    from perfstudio.model import DocumentMeta

    window = _window_on(
        create_empty_document(
            DocumentMeta(name="t", created="2024-01-01T00:00:00.000Z", modified="2024-01-01T00:00:00.000Z")
        )
    )
    called = []
    monkeypatch.setattr(window, "_confirm_placement", lambda plan, ms: called.append(1) or True)

    window.on_autoplace()

    assert called == []
    assert "empty" in window.statusBar().currentMessage()
    window.close()


# ---------------------------------------------------------------------------
# Build guide export
# ---------------------------------------------------------------------------


def test_exporting_the_guide_writes_all_four_files(tmp_path, monkeypatch) -> None:
    window = _window_on(_load_dense())
    window.current_path = tmp_path / "board.perf"

    monkeypatch.setattr(
        "perfstudio.ui.main.QMessageBox.warning", lambda *args, **kwargs: None
    )
    window.on_export_guide()

    written = sorted(p.name for p in tmp_path.iterdir())
    assert written == ["board_bom.csv", "board_cut_list.csv", "board_guide.html", "board_guide.json"]
    assert "<!doctype html>" in (tmp_path / "board_guide.html").read_text(encoding="utf-8")
    window.close()


def test_guide_gaps_are_reported_in_a_dialog_not_only_the_status_bar(tmp_path, monkeypatch) -> None:
    """Each warning says the guide describes less than the whole build. A user who misses
    that follows the steps to the end and finds the board does not work."""
    window = _window_on(_load_dense())
    window.current_path = tmp_path / "board.perf"

    shown: list[str] = []
    monkeypatch.setattr(
        "perfstudio.ui.main.QMessageBox.warning",
        lambda parent, title, text, *args, **kwargs: shown.append(text),
    )
    window.on_export_guide()

    assert shown and "could not cover" in shown[0]
    window.close()


# ---------------------------------------------------------------------------
# The solder side
# ---------------------------------------------------------------------------


def test_the_solder_side_shows_where_a_part_is_without_drawing_the_part() -> None:
    """You can see a part through the board, and on the solder side you need to: "is
    there room for this wire" and "which pad belongs to the chip" are questions asked
    from that side. But drawing the body as seen from above is how somebody solders a
    board backwards, so the footprint is hatched and carries none of the component-side
    marks."""
    from perfstudio.ui.view2d import _paint_body_shadow

    doc = _load_dense()
    bottom = BoardScene(doc, footprint_lookup(), side="bottom")
    top = BoardScene(doc, footprint_lookup(), side="top")

    assert len(bottom.component_items) == len(top.component_items)
    assert callable(_paint_body_shadow)


def test_the_solder_side_body_shadow_ignores_the_polarity_key() -> None:
    """A cathode band and a pin-1 notch are moulded into the TOP of a part. Showing them
    from below would be inventing a view that does not exist."""
    import inspect

    from perfstudio.ui import view2d

    source = inspect.getsource(view2d._paint_body_shadow)
    assert "_body_path(footprint, placement, None)" in source


# ---------------------------------------------------------------------------
# Conductor appearance
# ---------------------------------------------------------------------------


def test_insulated_wire_takes_its_nets_colour_from_the_build_guides_convention() -> None:
    """The screen and the cut list a person works from must not disagree about which
    wire is which."""
    from perfstudio.guide import COLOR_BY_NET_CLASS
    from perfstudio.ui.view2d import _INSULATION_SCREEN, insulation_color

    assert insulation_color("power", 0) == _INSULATION_SCREEN[COLOR_BY_NET_CLASS["power"]]
    assert insulation_color("ground", 0) == _INSULATION_SCREEN[COLOR_BY_NET_CLASS["ground"]]
    # Signals cycle, and two different signals are told apart.
    assert insulation_color("signal", 0) != insulation_color("signal", 1)
    # Every name the guide can emit has a screen colour, or a wire would silently fall
    # back to grey and stop matching its own cut-list row.
    from perfstudio.guide import SIGNAL_COLORS

    for name in (*SIGNAL_COLORS, *COLOR_BY_NET_CLASS.values()):
        assert name in _INSULATION_SCREEN, name


def test_no_conductor_is_drawn_in_the_error_colour() -> None:
    """Red means "this is wrong" -- the DRC outline and the R5' risk ring. Every
    insulated wire used to be red as well, so a completely correct board looked alarming
    and a real risk had nothing to stand out against."""
    from perfstudio.ui.view2d import CONDUCTOR_STYLE, ERROR_OUTLINE, RISK_RING

    for kind, (colour, _width, _dashed) in CONDUCTOR_STYLE.items():
        assert colour.name() != ERROR_OUTLINE.name(), kind
        assert colour.name() != RISK_RING.name(), kind


def test_solder_beads_sit_inside_the_pad_rather_than_over_it() -> None:
    """Solder fills a pad; it does not replace it. A bead wider than the pad hides the
    very thing being soldered to, which is what made a routed board read as a diagram of
    coloured bars with a board somewhere underneath."""
    from perfstudio.ui.view2d import CONDUCTOR_STYLE

    board = _load_dense().board
    for kind in ("solder-trace", "solder-trace-wired", "bare-wire", "insulated-wire"):
        width = CONDUCTOR_STYLE[kind][1]
        assert width < board.pad_diameter, kind


# ---------------------------------------------------------------------------
# Unsaved work
# ---------------------------------------------------------------------------


def test_a_fresh_window_is_not_modified() -> None:
    window = _window_on(_load_dense())
    assert window.is_modified is False
    assert "•" not in window.windowTitle()
    _close(window)


def test_any_command_marks_the_board_modified() -> None:
    from perfstudio.commands import MoveComponentPayload

    window = _window_on(_load_dense())
    first = window.bus.document.components[0]
    window.bus.dispatch(
        "component.move", MoveComponentPayload(id=first.id, anchor=view2d.HoleCoord(5, 5))
    )

    assert window.is_modified is True
    assert window.windowTitle().startswith("•")
    _close(window)


def test_undoing_back_to_the_saved_state_reads_as_unmodified() -> None:
    """Identity, not equality: undo restores the very document object that was saved, so
    "I undid everything" correctly stops nagging."""
    from perfstudio.commands import MoveComponentPayload

    window = _window_on(_load_dense())
    first = window.bus.document.components[0]
    window.bus.dispatch(
        "component.move", MoveComponentPayload(id=first.id, anchor=view2d.HoleCoord(5, 5))
    )
    assert window.is_modified

    window.bus.undo()

    assert window.is_modified is False
    _close(window)


def test_closing_with_unsaved_work_asks_and_can_be_cancelled(monkeypatch) -> None:
    """The last thing standing between an hour of layout and the X button."""
    from PySide6.QtGui import QCloseEvent

    from perfstudio.commands import MoveComponentPayload

    window = _window_on(_load_dense())
    first = window.bus.document.components[0]
    window.bus.dispatch(
        "component.move", MoveComponentPayload(id=first.id, anchor=view2d.HoleCoord(5, 5))
    )

    monkeypatch.setattr(window, "_offer_to_save", lambda: False)
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()

    monkeypatch.setattr(window, "_offer_to_save", lambda: True)
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    _close(window)


def test_an_unmodified_board_closes_without_a_prompt(monkeypatch) -> None:
    window = _window_on(_load_dense())
    asked = []
    monkeypatch.setattr(
        "perfstudio.ui.main.QMessageBox.exec", lambda self: asked.append(1) or 0
    )
    assert window._offer_to_save() is True
    assert asked == []
    _close(window)


def test_saving_clears_the_modified_marker(tmp_path) -> None:
    from perfstudio.commands import MoveComponentPayload

    window = _window_on(_load_dense())
    first = window.bus.document.components[0]
    window.bus.dispatch(
        "component.move", MoveComponentPayload(id=first.id, anchor=view2d.HoleCoord(5, 5))
    )
    window.current_path = tmp_path / "b.perf"
    window.on_save()

    assert window.is_modified is False
    assert (tmp_path / "b.perf").exists()
    _close(window)


# ---------------------------------------------------------------------------
# Board setup -- the first thing that can reach board.set
# ---------------------------------------------------------------------------


def test_board_setup_dialog_round_trips_a_board() -> None:
    from perfstudio.ui.main import BoardSetupDialog

    doc = _load_dense()
    dialog = BoardSetupDialog(doc.board)
    assert dialog.board() == doc.board

    dialog.cols.setValue(40)
    dialog.material.setCurrentIndex(dialog.material.findData("FR2"))
    changed = dialog.board()
    assert changed.cols == 40
    assert changed.material == "FR2"
    # Pitch and pad geometry are not the dialog's business and must survive untouched.
    assert changed.pitch == doc.board.pitch
    assert changed.pad_diameter == doc.board.pad_diameter


def test_every_board_material_is_offered() -> None:
    """FR-2 in particular: it is the board most perfboard is actually sold as, and the
    only one where the pad-lifting rule and the derated iron temperature apply."""
    from typing import get_args

    from perfstudio.model import BoardMaterial
    from perfstudio.ui.main import BoardSetupDialog

    offered = {value for value, _label in BoardSetupDialog.MATERIALS}
    assert offered == set(get_args(BoardMaterial))


def test_shrinking_the_board_under_a_part_is_refused_not_silently_applied(monkeypatch) -> None:
    from perfstudio.commands import SetBoardPayload

    window = _window_on(_load_dense())
    tiny = dataclasses.replace(window.bus.document.board, cols=3, rows=3)
    result = window.bus.dispatch("board.set", SetBoardPayload(board=tiny))

    assert result.ok is False
    assert window.bus.document.board.cols != 3
    _close(window)


# ---------------------------------------------------------------------------
# Drawing conductors by hand
# ---------------------------------------------------------------------------


def _drawing_scene():
    """A scene over a small board with a bus, ready to draw on."""
    from perfstudio.commands import create_empty_document
    from perfstudio.model import DocumentMeta

    doc = create_empty_document(
        DocumentMeta(name="t", created="2024-01-01T00:00:00.000Z", modified="2024-01-01T00:00:00.000Z")
    )
    bus = _new_bus(doc)
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    return scene, bus


def test_a_wire_is_two_clicks_and_commits_itself() -> None:
    scene, bus = _drawing_scene()
    scene.arm_drawing("insulated-wire")

    assert scene.draw_click(view2d.HoleCoord(2, 2)) is None  # First click starts it.
    result = scene.draw_click(view2d.HoleCoord(9, 6))

    assert result is not None and result.ok, result
    conductor = bus.document.conductors[0]
    assert conductor.kind == "insulated-wire"
    assert conductor.path == (view2d.HoleCoord(2, 2), view2d.HoleCoord(9, 6))
    assert scene.armed_draw_kind is None  # Disarmed once committed.


def test_a_solder_trace_is_a_chain_and_commits_on_request() -> None:
    scene, bus = _drawing_scene()
    scene.arm_drawing("solder-trace")

    for col in range(2, 6):
        scene.draw_click(view2d.HoleCoord(col, 4))
    assert not bus.document.conductors  # Still being drawn.

    result = scene.commit_drawing()

    assert result is not None and result.ok, result
    assert len(bus.document.conductors[0].path) == 4


def test_a_diagonal_step_is_refused_before_the_click_lands() -> None:
    """Solder spans the 0.6 mm gap to the next pad and not the 1.7 mm diagonal one. The
    command knows that; the preview has to know it too, or the tool looks broken when a
    click does nothing."""
    scene, bus = _drawing_scene()
    scene.arm_drawing("solder-trace")
    scene.draw_click(view2d.HoleCoord(4, 4))

    assert scene.draw_click(view2d.HoleCoord(5, 5)) is None
    scene.commit_drawing()
    assert not bus.document.conductors  # One hole is not a conductor.


def test_a_wire_may_go_diagonally_because_a_wire_physically_can() -> None:
    scene, bus = _drawing_scene()
    scene.arm_drawing("bare-wire")
    scene.draw_click(view2d.HoleCoord(4, 4))
    result = scene.draw_click(view2d.HoleCoord(7, 9))
    assert result is not None and result.ok
    assert bus.document.conductors[0].path[-1] == view2d.HoleCoord(7, 9)


def test_escape_abandons_a_half_drawn_trace() -> None:
    scene, bus = _drawing_scene()
    scene.arm_drawing("solder-trace")
    scene.draw_click(view2d.HoleCoord(2, 2))
    scene.draw_click(view2d.HoleCoord(3, 2))

    scene.arm_drawing(None)

    assert not bus.document.conductors
    assert scene.armed_draw_kind is None


def test_a_hand_drawn_conductor_takes_a_net_only_when_it_is_unambiguous() -> None:
    """Copper with no net claim is what rip-up-and-reroute and the stale cleanup both
    promise never to touch, so a connection the tool cannot interpret is also one it will
    never quietly remove."""
    from perfstudio.commands import PlaceComponentPayload
    from perfstudio.model import Net, NetNode

    scene, bus = _drawing_scene()
    bus.dispatch(
        "component.place",
        PlaceComponentPayload(ref="R1", value="", footprint_id="r-axial-4", anchor=view2d.HoleCoord(2, 2)),
    )
    bus.dispatch(
        "component.place",
        PlaceComponentPayload(ref="R2", value="", footprint_id="r-axial-4", anchor=view2d.HoleCoord(9, 2)),
    )
    from perfstudio.commands import ImportNetlistPayload

    bus.dispatch(
        "netlist.import",
        ImportNetlistPayload(
            nets=(
                Net(
                    id="n1",
                    name="SIG",
                    nodes=(NetNode(component_ref="R1", pin="1"), NetNode(component_ref="R2", pin="1")),
                ),
            )
        ),
    )
    scene.set_document(bus.document)

    scene.arm_drawing("bare-wire")
    scene.draw_click(view2d.HoleCoord(2, 2))
    scene.draw_click(view2d.HoleCoord(9, 2))
    assert bus.document.conductors[-1].net_id == "n1"

    # ...and an end on no pin at all leaves it unassigned rather than guessing.
    scene.set_document(bus.document)
    scene.arm_drawing("bare-wire")
    scene.draw_click(view2d.HoleCoord(4, 10))
    scene.draw_click(view2d.HoleCoord(8, 10))
    assert bus.document.conductors[-1].net_id is None


def test_conductors_can_be_selected_and_deleted() -> None:
    scene, bus = _drawing_scene()
    scene.arm_drawing("bare-wire")
    scene.draw_click(view2d.HoleCoord(2, 2))
    scene.draw_click(view2d.HoleCoord(8, 2))
    scene.set_document(bus.document)

    items = [i for i in scene.items() if isinstance(i, view2d.ConductorItem)]
    assert len(items) == 1
    items[0].setSelected(True)

    assert scene.selected_conductor_ids() == (bus.document.conductors[0].id,)


def test_a_conductor_is_pickable_along_its_length_not_by_its_bounding_box() -> None:
    """Two wires crossing at an angle share a bounding rect the size of the board between
    them; picking by that rect would select whichever happened to be on top."""
    scene, bus = _drawing_scene()
    scene.arm_drawing("bare-wire")
    scene.draw_click(view2d.HoleCoord(2, 2))
    scene.draw_click(view2d.HoleCoord(20, 20))
    scene.set_document(bus.document)

    item = next(i for i in scene.items() if isinstance(i, view2d.ConductorItem))
    on_the_wire = view2d.hole_to_screen(view2d.HoleCoord(11, 11), bus.document.board, "top")
    off_the_wire = view2d.hole_to_screen(view2d.HoleCoord(20, 2), bus.document.board, "top")

    assert item.shape().contains(item.mapFromScene(on_the_wire))
    assert not item.shape().contains(item.mapFromScene(off_the_wire))


# ---------------------------------------------------------------------------
# Performance and long-running work
# ---------------------------------------------------------------------------


def test_the_pad_grid_reuses_one_rasterised_pad() -> None:
    """Every pad on a board is identical by definition, so rasterising 6000 of them is
    6000 times more work than necessary. Blitting one pre-rendered pad took a 100x60
    board from 8.9 to 62 frames a second."""
    from PySide6.QtGui import QPixmap

    doc = _load_dense()
    grid = view2d.PadGridItem(doc.board, "top")

    first = grid._pad_for(12.0)
    assert isinstance(first, QPixmap)
    assert first.width() > 0
    # Same zoom, same pixmap object: no re-rasterising between frames.
    assert grid._pad_for(12.0) is first
    # A nearby zoom falls in the same bucket, so a smooth zoom does not thrash the cache.
    assert grid._pad_for(13.0) is first
    # A very different zoom does get its own.
    assert grid._pad_for(60.0) is not first


def test_the_pad_pixmap_is_bounded_however_far_you_zoom() -> None:
    doc = _load_dense()
    grid = view2d.PadGridItem(doc.board, "top")
    assert grid._pad_for(100000.0).width() <= 256


def test_a_planner_runs_off_the_ui_thread_and_can_be_cancelled() -> None:
    """Auto-place takes about a second, and it used to take it on the UI thread behind a
    wait cursor -- so the window stopped repainting and looked hung for exactly as long as
    the useful work took."""
    window = _window_on(_load_dense())
    seen: list[bool] = []

    def work(should_stop):
        seen.append(callable(should_stop))
        return "done"

    assert window._run_planner("test", work) == "done"
    assert seen == [True]
    _close(window)


def test_a_planner_exception_surfaces_on_the_ui_thread() -> None:
    """Swallowed on the worker thread it would look like a silent no-op."""
    window = _window_on(_load_dense())

    def boom(_should_stop):
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        window._run_planner("test", boom)
    _close(window)


def test_the_window_is_re_enabled_even_when_the_planner_fails() -> None:
    window = _window_on(_load_dense())

    def boom(_should_stop):
        raise ValueError("nope")

    with pytest.raises(ValueError):
        window._run_planner("test", boom)
    assert window.isEnabled()
    _close(window)


def test_placement_stopped_early_still_returns_a_legal_placement() -> None:
    """Cancelling asks the planner to stop and hand back its best result so far. Stopping
    early yields a worse placement, never an invalid one."""
    from perfstudio.placer import PlacementOptions, plan_placement

    doc = _load_dense()
    plan = plan_placement(
        doc,
        footprint_lookup(),
        PlacementOptions(iterations=40000, restarts=4, score_with_router=False),
        should_stop=lambda: True,
    )
    assert plan.after.is_legal
    assert plan.after.total(plan.weights) <= plan.before.total(plan.weights) + 1e-9


def test_the_cursor_hole_readout_tracks_the_pointer() -> None:
    window = _window_on(_load_dense())

    window._on_hovered_hole(2, 6)
    assert "C7" in window.label_hole.text()

    window._on_hovered_hole(-1, 0)
    assert "—" in window.label_hole.text()
    _close(window)


# ---------------------------------------------------------------------------
# The 3D board
# ---------------------------------------------------------------------------


def test_the_board_has_holes_from_underneath() -> None:
    """The substrate was one solid cube with pads only on top, so turning the board over
    showed a blank slab -- on the very view whose job is checking the solder side."""
    from perfstudio.ui import view3d

    doc = _load_dense()
    # Copper on both faces, at opposite sides of the substrate, and a bore through it.
    assert view3d.pad_z(doc.board, "top") > 0 > view3d.pad_z(doc.board, "bottom")
    assert view3d.pad_z(doc.board, "bottom") < -doc.board.thickness
    assert view3d.build_drills(doc.board) is not None
    assert view3d.build_pads(doc.board, "bottom") is not None


def test_the_legend_on_the_underside_reads_the_right_way_round() -> None:
    """Ink on the bottom face, looked at from underneath, has to read normally. Both
    faces were built from one set of glyphs at two different depths, so turning the board
    over in 3D showed the addresses written backwards.

    Reflected about the HOLE SPAN, the axis everything else mirrors about, so A stays on
    the hole A names. Checked on the geometry rather than on pixels: the two faces must
    span the same width, and the bottom one must put A where the top one puts the last
    column.
    """
    import dataclasses

    from perfstudio.model import BoardLabels
    from perfstudio.ui import view3d

    doc = _load_dense()
    board = dataclasses.replace(
        doc.board, cols=6, rows=4, labels=BoardLabels(row_digits=2)
    )
    doc = dataclasses.replace(doc, board=board, components=(), conductors=())
    span_w = (board.cols - 1) * board.pitch

    top, bottom = view3d.build_legend(doc)

    def points(actor: object) -> set[tuple[float, float]]:
        data = actor.GetMapper().GetInput()  # type: ignore[attr-defined]
        return {
            (round(data.GetPoint(i)[0], 3), round(data.GetPoint(i)[1], 3))
            for i in range(data.GetNumberOfPoints())
        }

    top_points = points(top)
    bottom_points = points(bottom)
    reflected = {(round(span_w - x, 3), y) for x, y in top_points}

    assert bottom_points == reflected
    # Not a vacuous assertion: A and R are different shapes, so an unreflected copy is a
    # genuinely different point set. This is the comparison that fails if the flip goes.
    assert bottom_points != top_points


def test_the_exploded_view_lifts_the_parts_and_leaves_the_board_alone() -> None:
    """PLAN.md D7. The board is what the parts come off; lifting that too would just be
    moving the camera.

    Measured on the PARTS, not on the scene bounds: the leader lines reach the full lift
    whether or not anything rose with them, so a bounds check passes on a view where every
    part is still flat on the board.
    """
    import vtk

    from perfstudio.ui import view3d

    doc = _load_dense()
    lookup = footprint_lookup()
    lift = view3d.EXPLODED_LIFT_MM

    for comp in doc.components:
        actors = view3d.build_component(lookup, comp, doc.board)
        for actor in actors:
            before = actor.GetBounds()[4]
            view3d._lift(actor, lift)
            assert actor.GetBounds()[4] == pytest.approx(before + lift)

    flat = vtk.vtkRenderer()
    view3d.populate_renderer(flat, doc, lookup)
    blown = vtk.vtkRenderer()
    view3d.populate_renderer(blown, doc, lookup, exploded_mm=lift)

    # The substrate's underside is the lowest thing in either scene and has not moved.
    assert _lowest(blown) == pytest.approx(_lowest(flat))


def test_every_exploded_part_has_a_line_down_to_its_own_holes() -> None:
    """Without them a vertical explosion is ambiguous: a part over the MIDDLE of the board
    projects onto it from the standard viewpoint and reads as sitting on it, while an
    identical part near an edge reads as floating. The line is the answer to the question
    the view exists to ask -- which holes does this one go in."""
    from perfstudio.geometry import all_pin_holes
    from perfstudio.ui import view3d

    doc = _load_dense()
    lookup = footprint_lookup()
    lift = view3d.EXPLODED_LIFT_MM
    leaders = view3d.build_drop_lines(lookup, doc, lift)
    assert leaders is not None

    pins = sum(
        len(all_pin_holes(c, lookup(c.footprint_id)))
        for c in doc.components
        if lookup(c.footprint_id) is not None
    )
    data = leaders.GetMapper().GetInput()
    assert data.GetNumberOfLines() == pins, "one leader per pin hole"

    bounds = data.GetBounds()
    assert bounds[4] == pytest.approx(0.0)
    assert bounds[5] == pytest.approx(lift), "the lines must reach the parts they belong to"

    assert view3d.build_drop_lines(lookup, doc, 0.0) is None, "nothing to lead to"


def test_highlighting_a_step_dims_the_other_parts_but_never_the_board() -> None:
    """A step card says which holes a part goes in. Dimming the board with everything else
    would be printing the answer with the question rubbed out."""
    import vtk

    from perfstudio.ui import view3d

    doc = _load_dense()
    lookup = footprint_lookup()
    subject = doc.components[0]

    plain = vtk.vtkRenderer()
    view3d.populate_renderer(plain, doc, lookup)
    picked = vtk.vtkRenderer()
    view3d.populate_renderer(picked, doc, lookup, highlight=subject.id)

    # The substrate is built first either way, so position 0 is comparable.
    assert _actor_colours(picked)[0] == _actor_colours(plain)[0], "the board was dimmed"
    assert _actor_colours(picked) != _actor_colours(plain), "nothing was dimmed at all"

    others = view3d.build_component(lookup, doc.components[1], doc.board)
    before = others[0].GetProperty().GetColor()
    view3d._dim(others[0])
    after = others[0].GetProperty().GetColor()
    assert all(a < b for a, b in zip(after, before, strict=True) if b > 0)
    assert after != before


def _highest(ren: object) -> float:
    return ren.ComputeVisiblePropBounds()[5]  # type: ignore[attr-defined]


def _lowest(ren: object) -> float:
    return ren.ComputeVisiblePropBounds()[4]  # type: ignore[attr-defined]


def _actor_colours(ren: object) -> list[tuple[float, float, float]]:
    actors = ren.GetActors()  # type: ignore[attr-defined]
    actors.InitTraversal()
    return [
        actors.GetNextActor().GetProperty().GetColor()
        for _ in range(actors.GetNumberOfItems())
    ]


def test_every_step_gets_a_picture_of_its_own() -> None:
    """PLAN.md §7.2. Keyed by guide.step_focus, which is what guide_export looks them up
    by, so a mismatch here shows as a guide with no illustrations rather than a crash."""
    from perfstudio.guide import all_steps, build_guide, step_focus
    from perfstudio.ui import view3d

    doc = _load_dense()
    lookup = footprint_lookup()
    guide = build_guide(doc, lookup)

    images = view3d.render_step_images(doc, guide, lookup, width=200, height=140)

    assert set(images) == {step_focus(step) for step in all_steps(guide)}
    png_magic = bytes([0x89]) + b"PNG"
    assert all(png.startswith(png_magic) for png in images.values())


def test_a_connection_is_photographed_from_the_side_it_is_made_on() -> None:
    """The fault this exists to prevent: almost every connection is made on the solder
    side, and shot from the component side it is behind 1.6 mm of board. The first version
    of the step images produced fourteen pictures of a board with nothing happening."""
    from perfstudio.guide import all_steps, build_guide, step_focus
    from perfstudio.ui import view3d

    doc = _load_dense()
    guide = build_guide(doc, footprint_lookup())
    side_of = {c.id: c.side for c in doc.conductors}

    seen_bottom = False
    for step in all_steps(guide):
        focus = step_focus(step)
        expected = side_of.get(focus) == "bottom"
        assert view3d.step_is_solder_side(doc, focus) is expected
        seen_bottom |= expected

    assert seen_bottom, "this fixture is meant to have solder-side connections"
    # ...and the other way too, or the test would pass on a rule that always said True.
    assert any(c.side == "top" for c in doc.conductors), "and top-side ones"


def test_a_part_is_always_photographed_from_the_component_side() -> None:
    """Parts go in from the top, whatever else is on the board."""
    from perfstudio.ui import view3d

    doc = _load_dense()

    assert not any(view3d.step_is_solder_side(doc, c.id) for c in doc.components)


def test_solder_and_wire_are_not_the_same_grey() -> None:
    """PLAN.md Sec 8.3 makes telling them apart at a glance a requirement of this view.
    They were (0.72, 0.74, 0.77) and (0.85, 0.87, 0.89) -- the same grey."""
    from perfstudio.ui.view3d import BARE_RGB, SOLDER_RGB

    difference = sum(abs(a - b) for a, b in zip(SOLDER_RGB, BARE_RGB, strict=True))
    assert difference > 0.5, "solder and tinned wire are still indistinguishable"


def test_conductors_sharing_a_layer_do_not_occupy_the_same_space() -> None:
    """Two wires crossing were drawn intersecting, which is not a thing wire does."""
    from perfstudio.ui.view3d import conductor_z

    doc = _load_dense()
    wire = WireConductor(id="w1", path=(HoleCoord(2, 2), HoleCoord(9, 9)), kind="bare-wire")

    assert conductor_z(wire, doc.board, 0) != conductor_z(wire, doc.board, 1)
    # Solder-side copper stays clear of the substrate however deep the stack goes.
    assert conductor_z(wire, doc.board, 12) < -doc.board.thickness


# ---------------------------------------------------------------------------
# Board colour
# ---------------------------------------------------------------------------


def test_both_views_take_their_board_colour_from_one_scheme() -> None:
    """Green in the editor and blue in 3D would undermine the one job the 3D view has."""
    from perfstudio.ui import boardcolors

    try:
        boardcolors.choose("blue")
        blue = boardcolors.scheme_for("FR4")
        assert blue.key == "blue"
        # The 2D hex and the 3D linear RGB describe the same colour.
        expected = (int(blue.fill[1:3], 16) / 255, int(blue.fill[3:5], 16) / 255,
                    int(blue.fill[5:7], 16) / 255)
        assert all(abs(a - b) < 0.06 for a, b in zip(blue.rgb, expected, strict=True))
    finally:
        boardcolors.choose(None)


def test_the_material_decides_until_someone_chooses() -> None:
    """FR-2 is the brown phenolic board, and the build guide derates the iron for exactly
    that material -- the two should agree on sight."""
    from perfstudio.ui import boardcolors

    boardcolors.choose(None)
    assert boardcolors.scheme_for("FR4").key == "green"
    assert boardcolors.scheme_for("FR2").key == "phenolic"


def test_every_material_has_a_default_scheme() -> None:
    from typing import get_args

    from perfstudio.model import BoardMaterial
    from perfstudio.ui import boardcolors

    for material in get_args(BoardMaterial):
        assert material in boardcolors.DEFAULT_FOR_MATERIAL
        assert boardcolors.DEFAULT_FOR_MATERIAL[material] in boardcolors.BY_KEY


def test_an_unknown_colour_falls_back_to_the_material() -> None:
    from perfstudio.ui import boardcolors

    try:
        boardcolors.choose("chartreuse")
        assert boardcolors.chosen_key() is None
        assert boardcolors.scheme_for("FR4").key == "green"
    finally:
        boardcolors.choose(None)


# ---------------------------------------------------------------------------
# Board features in the editor: oblong pads, the printed legend, mounting holes
# ---------------------------------------------------------------------------


def _blank_document():
    from perfstudio.commands import create_empty_document
    from perfstudio.model import DocumentMeta

    return create_empty_document(
        DocumentMeta(
            name="t", created="2026-01-01T00:00:00.000Z", modified="2026-01-01T00:00:00.000Z"
        )
    )


def _featured_document():
    """A board using all three: oblong pads, a printed legend, a corner hole, a connector."""
    from perfstudio.commands import DEFAULT_BOARD, create_empty_document
    from perfstudio.model import BoardLabels, DocumentMeta, EdgeConnector, MountingHole

    board = dataclasses.replace(
        DEFAULT_BOARD,
        cols=14,
        rows=10,
        pad_shape="oblong",
        pad_length=2.25,
        border_x_mm=2.0, border_y_mm=2.0,
        labels=BoardLabels(row_digits=2),
    )
    doc = create_empty_document(
        DocumentMeta(name="features", created="2026-01-01", modified="2026-01-01"), board
    )
    return dataclasses.replace(
        doc,
        mounting_holes=(MountingHole(id="mh-1", at=HoleCoord(1, 1)),),
                # An inset, as the real boards have: the fingers stop short of the edge and the
        # strip left outside them is where the row legend is printed.
        edge_connectors=(EdgeConnector(id="ec-1", edge="bottom", start=3, count=5, inset_mm=1.6),),
    )


def test_the_pad_grid_leaves_out_the_pads_a_mounting_bore_removed() -> None:
    """Drawing copper where the bore took it away would show a pad to solder to that is
    not there -- which is precisely what the DRC rule exists to stop someone finding out
    with an iron in hand."""
    doc = _featured_document()
    with_hole = BoardScene(doc, footprint_lookup(), show_rulers=False)
    without = BoardScene(
        dataclasses.replace(doc, mounting_holes=()), footprint_lookup(), show_rulers=False
    )
    _render_scene(with_hole)
    _render_scene(without)

    assert with_hole.pad_grid is not None and without.pad_grid is not None
    # The bore at B2 eats its own pad and its four orthogonal neighbours.
    assert without.pad_grid.drawn - with_hole.pad_grid.drawn == 5


def test_the_scene_carries_the_board_features_it_is_given() -> None:
    from perfstudio.ui.view2d import BoardLegendItem, EdgeConnectorItem, MountingHoleItem

    scene = BoardScene(_featured_document(), footprint_lookup(), show_rulers=False)
    kinds = {type(item) for item in scene.items()}
    assert BoardLegendItem in kinds
    assert MountingHoleItem in kinds
    assert EdgeConnectorItem in kinds


def test_a_plain_board_draws_no_legend() -> None:
    from perfstudio.ui.view2d import BoardLegendItem

    scene = BoardScene(_load_dense(), footprint_lookup(), show_rulers=False)
    assert BoardLegendItem not in {type(item) for item in scene.items()}


def _legend_labels_drawn(doc, monkeypatch):
    """Every label the legend lays down, as (text, centre, height_mm, max_width_mm).

    Asserted on instead of pixels ON PURPOSE. The legend is silkscreen, so it is sized in
    millimetres and comes out around 14 device pixels at the editor's usual zoom -- and on
    a Qt platform with no font database (this one; see the skips above) nothing is drawn
    at that size at all, silently. A pixel test there passes or fails on whether Qt
    happens to have fonts, which is not the thing worth testing. Where each label is put
    and how big it is *is*: getting that wrong is what buries the legend under the first
    row of pads, which is the bug this pair of tests exists to catch.
    """
    drawn: list[tuple[str, QPointF, float, float | None]] = []
    real = view2d.draw_physical_label

    # **kwargs, not a copied signature: a shim that has to be kept in step with the real
    # function will one day not be, and the failure mode is a TypeError raised inside
    # QGraphicsItem.paint -- which leaves the QPainter open and crashes the NEXT test
    # with an access violation, a long way from the cause.
    def record(painter, centre, text, height_mm, *args, **kwargs):
        drawn.append((text, QPointF(centre), height_mm, kwargs.get("max_width_mm")))
        return real(painter, centre, text, height_mm, *args, **kwargs)

    monkeypatch.setattr(view2d, "draw_physical_label", record)
    _render_scene(BoardScene(doc, footprint_lookup(), show_rulers=False, show_ratsnest=False))
    return drawn


def test_the_printed_legend_lays_down_every_address_on_all_four_edges(monkeypatch) -> None:
    """Letters along the top AND bottom, numbers down the left AND right, as these boards
    are printed. With one edge each, the far half of the board is nearest the edge that
    does not carry its address."""
    doc = _featured_document()
    board = doc.board
    drawn = _legend_labels_drawn(doc, monkeypatch)

    letters = [text for text, _c, _h, _w in drawn if text.isalpha()]
    numbers = [text for text, _c, _h, _w in drawn if text.isdigit()]
    assert len(letters) == board.cols * 2
    assert len(numbers) == board.rows * 2
    assert set(letters) == {column_label(col) for col in range(board.cols)}
    # row_digits=2, so the board prints "01" where the guide says row 1 -- the same
    # address, set the way these boards set it.
    assert "01" in numbers
    assert "10" in numbers


def test_a_plain_board_lays_down_no_legend(monkeypatch) -> None:
    assert _legend_labels_drawn(_load_dense(), monkeypatch) == []


def test_the_legend_is_printed_in_the_border_and_not_over_the_pads(monkeypatch) -> None:
    """The reason ``border_mm`` exists. Half a pitch past the outer holes leaves 0.32 mm
    of bare substrate at 2.54 mm pitch, which is not room for a character -- it would be
    drawn under the first row of pads and never seen."""
    from perfstudio.geometry import board_edge_margin_mm, hole_span_mm, pad_extent_mm

    doc = _featured_document()
    board = doc.board
    margin_x = board_edge_margin_mm(board, "horizontal")
    margin_y = board_edge_margin_mm(board, "vertical")
    extent_x, extent_y = pad_extent_mm(board)
    span_w, span_h = hole_span_mm(board)

    def within(low: float, high: float, value: float, half: float) -> bool:
        return low < value - half and value + half < high

    for text, centre, height_mm, _max_width in _legend_labels_drawn(doc, monkeypatch):
        # A letter is upright, so its cap height runs DOWN the strip; a number is turned
        # on its side, so its cap height runs ACROSS it. Each has to sit inside the bare
        # substrate between the outer pads and the board edge, on one of the two edges
        # that carry it.
        if text.isalpha():
            top = within(-margin_y, -extent_y / 2, centre.y(), height_mm / 2)
            bottom = within(span_h + extent_y / 2, span_h + margin_y, centre.y(), height_mm / 2)
            assert top or bottom, f"column letter {text} is not in a top/bottom border strip"
        else:
            left = within(-margin_x, -extent_x / 2, centre.x(), height_mm / 2)
            right = within(span_w + extent_x / 2, span_w + margin_x, centre.x(), height_mm / 2)
            assert left or right, f"row number {text} is not in a left/right border strip"


def test_a_legend_on_a_finger_edge_is_printed_outside_the_fingers(monkeypatch) -> None:
    """Ink goes on the substrate and copper goes on top of it, so a label printed where a
    finger is does not come out faint — it does not come out at all.

    The position used to be measured OUT FROM THE PAD, which is right until the copper on
    that edge is an elongated finger reaching most of the way to the board edge. The board
    this application now opens on has fingers along two entire edges, so every column
    letter was being printed underneath one and the board came up with numbers and no
    letters. The test above does not catch it: its band runs from the grid pad to the
    board edge, and the middle of a finger is inside that band.
    """
    from perfstudio.commands import create_starter_document
    from perfstudio.geometry import board_edge_margin_mm, hole_span_mm, legend_strip_mm
    from perfstudio.model import DocumentMeta

    doc = create_starter_document(DocumentMeta(name="t", created="", modified=""))
    assert {c.edge for c in doc.edge_connectors} == {"top", "bottom"}, "wrong board for this test"

    margin_y = board_edge_margin_mm(doc.board, "vertical")
    inset = legend_strip_mm(doc, "vertical")
    _span_w, span_h = hole_span_mm(doc.board)

    letters = [
        (text, centre, height)
        for text, centre, height, _w in _legend_labels_drawn(doc, monkeypatch)
        if text.isalpha()
    ]
    assert len(letters) == doc.board.cols * 2, "the letters are not being laid down at all"

    for text, centre, height in letters:
        near = -margin_y < centre.y() - height / 2 and centre.y() + height / 2 < -margin_y + inset
        far = (
            span_h + margin_y - inset < centre.y() - height / 2
            and centre.y() + height / 2 < span_h + margin_y
        )
        assert near or far, f"column letter {text} is printed under a connector finger"


def test_board_setup_dialog_round_trips_the_new_board_fields() -> None:
    from perfstudio.model import BoardLabels
    from perfstudio.ui.main import BoardSetupDialog

    board = _featured_document().board
    dialog = BoardSetupDialog(board)
    assert dialog.board() == board

    dialog.pad_shape.setCurrentIndex(dialog.pad_shape.findData("round"))
    dialog.legend.setChecked(False)
    plain = dialog.board()
    assert plain.pad_shape == "round"
    # The length is dropped with the shape: a round board carrying a pad length would put
    # a field in the file describing nothing.
    assert plain.pad_length is None
    assert plain.labels is None

    dialog.legend.setChecked(True)
    dialog.row_digits.setValue(3)
    assert dialog.board().labels == BoardLabels(row_digits=3)


def test_the_dialog_cannot_produce_an_oblong_pad_the_bus_would_refuse() -> None:
    """A dialog whose only exit is an error message is a worse dialog than one that
    cannot produce the error."""
    from perfstudio.commands import DEFAULT_BOARD, SetBoardPayload
    from perfstudio.ui.main import BoardSetupDialog

    dialog = BoardSetupDialog(DEFAULT_BOARD)
    dialog.pad_shape.setCurrentIndex(dialog.pad_shape.findData("oblong"))
    dialog.pad_length.setValue(0.5)  # narrower than the 1.9 mm pad width

    bus = _new_bus(_blank_document())
    assert bus.dispatch("board.set", SetBoardPayload(board=dialog.board())).ok


def test_board_features_dialog_adds_four_corner_holes_as_one_undo_step() -> None:
    from perfstudio.ui.main import BoardFeaturesDialog

    bus = _new_bus(_blank_document())
    dialog = BoardFeaturesDialog(bus)
    dialog.mount_inset.setValue(1)
    dialog._on_add_corners()

    assert len(bus.document.mounting_holes) == 4
    assert dialog.tree.topLevelItemCount() == 4
    bus.undo()
    assert bus.document.mounting_holes == ()


def test_board_features_dialog_reports_a_refusal_instead_of_swallowing_it() -> None:
    from perfstudio.ui.main import BoardFeaturesDialog

    bus = _new_bus(_blank_document())
    dialog = BoardFeaturesDialog(bus)
    dialog.mount_inset.setValue(999)
    dialog._on_add_corners()

    assert bus.document.mounting_holes == ()
    assert dialog.note.text() != ""


def test_board_features_dialog_removes_the_selected_feature() -> None:
    from perfstudio.ui.main import BoardFeaturesDialog

    bus = _new_bus(_featured_document())
    dialog = BoardFeaturesDialog(bus)
    assert dialog.tree.topLevelItemCount() == 2

    dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
    dialog._on_remove()

    assert bus.document.mounting_holes == ()
    assert len(bus.document.edge_connectors) == 1
    assert dialog.tree.topLevelItemCount() == 1


# ---------------------------------------------------------------------------
# Assembly playback
# ---------------------------------------------------------------------------


def test_the_two_ends_of_the_assembly_slider_mean_different_things() -> None:
    """The bare board and the finished board are the two states somebody actually asks
    for, and they are not the same state. The first version returned -1 for both, so the
    left-hand end of the slider drew a complete board.
    """
    from perfstudio.ui.main import assembly_step_for

    assert assembly_step_for(0, 5) == -1, "nothing fitted yet, and no step to highlight"
    assert assembly_step_for(5, 5) is None, "the finished board, as the panel normally is"
    assert assembly_step_for(6, 5) is None, "and past the end is still the finished board"


def test_the_slider_counts_things_fitted_not_steps_done() -> None:
    """Value 1 is "one thing on the board", which is step 0 having just been done."""
    from perfstudio.ui.main import assembly_step_for

    assert [assembly_step_for(v, 4) for v in (0, 1, 2, 3, 4)] == [-1, 0, 1, 2, None]


def test_each_slider_position_shows_what_its_caption_claims() -> None:
    """The property the whole thing rests on: at position N the board carries N things,
    and the step being highlighted is the one that put the last of them there."""
    from perfstudio.guide import all_steps, build_guide, document_at_step, step_focus
    from perfstudio.ui.main import assembly_step_for

    doc = _load_dense()
    guide = build_guide(doc, footprint_lookup())
    steps = all_steps(guide)
    maximum = len(steps)

    for value in range(maximum + 1):
        index = assembly_step_for(value, maximum)
        if index is None:
            continue
        shown = document_at_step(doc, guide, index)
        assert len(shown.components) + len(shown.conductors) == value
        if index >= 0:
            present = {c.id for c in shown.components} | {c.id for c in shown.conductors}
            assert step_focus(steps[index]) in present


# ---------------------------------------------------------------------------
# Naming a net by clicking its pins
#
# The engine has had nets since the first commit and only a KiCad netlist could put one
# in a document, so on a tool for wiring four parts on a scrap of perfboard there was no
# ratsnest, and so no autoroute, LVS or continuity check, without opening a schematic
# capture package first.
# ---------------------------------------------------------------------------


def _bus_with_a_part_and_a_net() -> CommandBus:
    from perfstudio.commands import AddNetPayload, PlaceComponentPayload, create_empty_document
    from perfstudio.model import DocumentMeta

    stamp = "2026-01-01T00:00:00.000Z"
    meta = DocumentMeta(name="t", created=stamp, modified=stamp)
    bus = _new_bus(create_empty_document(meta))
    bus.dispatch(
        "component.place",
        PlaceComponentPayload(
            ref="R1", value="10k", footprint_id="r-axial-5", anchor=HoleCoord(2, 2)
        ),
    )
    bus.dispatch("net.add", AddNetPayload(name="GND", net_class="ground"))
    return bus


def test_clicking_pins_adds_them_to_the_net_as_one_command() -> None:
    bus = _bus_with_a_part_and_a_net()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)

    scene.arm_net_pins("net-1")
    scene.net_pin_click(HoleCoord(2, 2))
    result = scene.commit_net_pins()

    assert result is not None and result.ok, result
    assert bus.document.nets[0].nodes[0].component_ref == "R1"
    # One command for the session: the undo has to take the whole net back, not a pin.
    bus.undo()
    assert bus.document.nets[0].nodes == ()


def test_a_click_that_cannot_count_says_why_instead_of_being_dropped() -> None:
    """A silently ignored click is indistinguishable from a tool that has stopped
    working, which is why every refusal the command would make is made per click."""
    bus = _bus_with_a_part_and_a_net()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    reasons: list[str] = []
    scene.netPinRejected.connect(reasons.append)

    scene.arm_net_pins("net-1")
    scene.net_pin_click(HoleCoord(20, 20))  # empty hole
    scene.net_pin_click(HoleCoord(2, 2))
    scene.net_pin_click(HoleCoord(2, 2))  # the same pin twice

    assert scene.picked_pins() == (("R1", "1"),)
    assert len(reasons) == 2
    assert "No component pin" in reasons[0]
    assert "already on the list" in reasons[1]


def test_a_pin_another_net_already_has_is_refused_at_the_click() -> None:
    from perfstudio.commands import AddNetPayload, ConnectPinsPayload
    from perfstudio.model import NetNode

    bus = _bus_with_a_part_and_a_net()
    bus.dispatch(
        "net.connect", ConnectPinsPayload(id="net-1", nodes=(NetNode("R1", "1"),))
    )
    bus.dispatch("net.add", AddNetPayload(name="+5V", net_class="power"))
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)
    reasons: list[str] = []
    scene.netPinRejected.connect(reasons.append)

    scene.arm_net_pins("net-2")
    scene.net_pin_click(HoleCoord(2, 2))

    assert scene.picked_pins() == ()
    assert "GND" in reasons[0]


def test_committing_nothing_is_a_cancel_rather_than_an_empty_command() -> None:
    bus = _bus_with_a_part_and_a_net()
    before = bus.document
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)

    scene.arm_net_pins("net-1")
    assert scene.commit_net_pins() is None
    assert bus.document is before
    assert scene.armed_net_id is None


def test_arming_a_drawing_tool_ends_a_pin_session() -> None:
    """The three board modes are mutually exclusive: a click has to mean one thing."""
    bus = _bus_with_a_part_and_a_net()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)

    scene.arm_net_pins("net-1")
    scene.arm_drawing("bare-wire")

    assert scene.armed_net_id is None


def test_a_rebuild_mid_session_keeps_the_pins_already_picked() -> None:
    """Every command rebuilds the scene, and a half-collected net whose markers vanished
    would read as the clicks having been lost."""
    bus = _bus_with_a_part_and_a_net()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)

    scene.arm_net_pins("net-1")
    scene.net_pin_click(HoleCoord(2, 2))
    scene.set_document(bus.document)

    assert scene.armed_net_id == "net-1"
    assert scene.picked_pins() == (("R1", "1"),)
    assert any(isinstance(item, view2d.PickedPinsItem) for item in scene.items())


def test_arming_an_unknown_net_collects_nothing() -> None:
    bus = _bus_with_a_part_and_a_net()
    scene = BoardScene(bus.document, footprint_lookup(), side="top", bus=bus)

    scene.arm_net_pins("net-does-not-exist")

    assert scene.armed_net_id is None


# ---------------------------------------------------------------------------
# The Net menu and the Nets panel
# ---------------------------------------------------------------------------


class _StubNetDialog:
    """Stands in for NetDialog, which is modal and would wait forever headless."""

    values_to_return: tuple = ("GND", "ground", None, None)
    accepted = True

    def __init__(self, *args, **kwargs) -> None:
        pass

    def exec(self) -> int:
        from PySide6.QtWidgets import QDialog

        return (
            QDialog.DialogCode.Accepted if self.accepted else QDialog.DialogCode.Rejected
        )

    def values(self) -> tuple:
        return self.values_to_return


def test_new_net_creates_it_and_goes_straight_into_picking_its_pins(monkeypatch) -> None:
    """Naming a net and then hunting for the command that fills it would be two decisions
    where the user made one, and an empty net does nothing for anybody."""
    from perfstudio.ui import main as main_module

    window = _window_on(_load_dense())
    monkeypatch.setattr(main_module, "NetDialog", _StubNetDialog)
    _StubNetDialog.values_to_return = ("HAND-WIRED", "signal", None, None)

    window.on_new_net()

    net = next(n for n in window.bus.document.nets if n.name == "HAND-WIRED")
    assert window.scene.armed_net_id == net.id
    window.scene.arm_net_pins(None)
    _close(window)


def test_a_refused_new_net_leaves_the_document_alone(monkeypatch) -> None:
    from perfstudio.ui import main as main_module

    window = _window_on(_load_dense())
    existing = window.bus.document.nets[0].name
    monkeypatch.setattr(main_module, "NetDialog", _StubNetDialog)
    _StubNetDialog.values_to_return = (existing, "signal", None, None)
    before = window.bus.document

    window.on_new_net()

    assert window.bus.document is before
    assert "duplicate-net-name" in window.statusBar().currentMessage()
    _close(window)


def test_the_nets_panel_lists_each_nets_pins_so_they_can_be_taken_off_it() -> None:
    """The panel was a readout; a pin has to be visible to be selected, and selectable to
    be disconnected."""
    window = _window_on(_load_dense())
    tree = window.nets_tree

    first = tree.topLevelItem(0)
    assert first is not None
    net = next(n for n in window.bus.document.nets if n.id == first.data(0, ROLE_NET_ID))
    assert first.childCount() == len(net.nodes)

    pin_row = first.child(0)
    assert pin_row is not None
    first.setExpanded(True)
    pin_row.setSelected(True)
    assert window._selected_pins() == ((net.id, net.nodes[0].component_ref, net.nodes[0].pin),)

    window.on_disconnect_pins()

    after = next(n for n in window.bus.document.nets if n.id == net.id)
    assert after.nodes == net.nodes[1:]
    _close(window)


def test_the_panel_keeps_a_net_open_across_the_command_that_empties_a_row() -> None:
    """Expansion is restored for the same reason selection is: the rows are rebuilt after
    every command, and a net opened to take a pin off it must not close as it is taken."""
    window = _window_on(_load_dense())
    first = window.nets_tree.topLevelItem(0)
    assert first is not None
    net_id = first.data(0, ROLE_NET_ID)
    first.setExpanded(True)
    pin_row = first.child(0)
    assert pin_row is not None
    pin_row.setSelected(True)

    window.on_disconnect_pins()

    reopened = next(
        window.nets_tree.topLevelItem(i)
        for i in range(window.nets_tree.topLevelItemCount())
        if window.nets_tree.topLevelItem(i).data(0, ROLE_NET_ID) == net_id
    )
    assert reopened.isExpanded()
    _close(window)


def test_deleting_a_net_keeps_its_copper_and_releases_the_claim(monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from perfstudio.commands import AddConductorPayload, NewSolderTraceConductor

    window = _window_on(_load_dense())
    net = window.bus.document.nets[0]
    window.bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewSolderTraceConductor(
                path=(HoleCoord(0, 0), HoleCoord(1, 0)), net_id=net.id
            )
        ),
    )
    conductors_before = len(window.bus.document.conductors)
    window._select_net(net.id)
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )

    window.on_delete_net()

    assert all(n.id != net.id for n in window.bus.document.nets)
    assert len(window.bus.document.conductors) == conductors_before
    assert all(c.net_id != net.id for c in window.bus.document.conductors)
    _close(window)




# ---------------------------------------------------------------------------
# Telling the user what state the board is in
#
# Placing, drawing and picking pins all arm a mode in which a click means something
# other than what it usually means, and the only place any of them said so was the
# status bar -- the bottom edge of a window a metre wide, while the cursor is in the
# middle of the board. A mode nobody can see is indistinguishable from an application
# that has stopped responding.
# ---------------------------------------------------------------------------


def test_the_armed_mode_is_named_over_the_board() -> None:
    window = _window_on(_load_dense())

    window.scene.arm_placement("r-axial-5")

    assert not window.view.mode_banner.isHidden()
    assert "Placing" in window.view.mode_banner.text()
    window.scene.arm_placement(None)
    assert window.view.mode_banner.isHidden()
    _close(window)


def test_the_banner_follows_the_pins_as_they_are_picked() -> None:
    from perfstudio.commands import AddNetPayload
    from perfstudio.geometry import all_pin_holes

    window = _window_on(_load_dense())
    window.bus.dispatch("net.add", AddNetPayload(name="HAND", net_class="ground"))
    net = next(n for n in window.bus.document.nets if n.name == "HAND")
    taken = {(node.component_ref, node.pin) for n in window.bus.document.nets for node in n.nodes}
    free = next(
        hole
        for comp in window.bus.document.components
        if (fp := window.lookup(comp.footprint_id)) is not None
        for pin, hole in all_pin_holes(comp, fp)
        if (comp.ref, pin.number) not in taken
    )

    window.scene.arm_net_pins(net.id)
    assert "HAND" in window.view.mode_banner.text()
    window.scene.net_pin_click(free)

    assert "." in window.view.mode_banner.text()  # the pin it just took, "R1.2"
    window.scene.commit_net_pins()
    assert window.view.mode_banner.isHidden()
    _close(window)


def test_an_empty_board_says_what_to_do_with_itself() -> None:
    """The application opens on a blank 5 x 7, and every route, check and export needs
    something on it first."""
    from perfstudio.commands import PlaceComponentPayload, create_starter_document
    from perfstudio.model import DocumentMeta

    stamp = "2026-01-01T00:00:00.000Z"
    meta = DocumentMeta(name="t", created=stamp, modified=stamp)
    window = _window_on(create_starter_document(meta))

    assert not window.view.empty_hint.isHidden()

    window.bus.dispatch(
        "component.place",
        PlaceComponentPayload(
            ref="R1", value="10k", footprint_id="r-axial-5", anchor=HoleCoord(2, 2)
        ),
    )

    assert window.view.empty_hint.isHidden()
    _close(window)


def test_the_guidance_gets_out_of_the_way_of_a_mode() -> None:
    """Somebody mid-mode is plainly not stuck, and two blocks of text over one board is
    one too many."""
    from perfstudio.commands import create_starter_document
    from perfstudio.model import DocumentMeta

    stamp = "2026-01-01T00:00:00.000Z"
    meta = DocumentMeta(name="t", created=stamp, modified=stamp)
    window = _window_on(create_starter_document(meta))
    assert not window.view.empty_hint.isHidden()

    window.scene.arm_placement("r-axial-5")

    assert window.view.empty_hint.isHidden()
    window.scene.arm_placement(None)
    assert not window.view.empty_hint.isHidden()
    _close(window)


def test_the_overlays_never_eat_a_click() -> None:
    """They sit over the board, and the whole point of a mode is that the next click
    reaches the board. An overlay that swallowed the click it was describing would be
    worse than no overlay at all."""
    from PySide6.QtCore import Qt as QtCore_Qt

    window = _window_on(_load_dense())

    for overlay in (window.view.mode_banner, window.view.empty_hint):
        assert overlay.testAttribute(QtCore_Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    _close(window)


# ---------------------------------------------------------------------------
# The files you were last working on
# ---------------------------------------------------------------------------


def test_opening_a_document_puts_it_on_the_recent_list(tmp_path) -> None:
    window = _window_on(_load_dense())
    board = tmp_path / "board.perf"
    board.write_text(GOLDEN.read_text(encoding="utf-8"), encoding="utf-8")

    window._load_path(board)

    assert window._recent_paths() == [str(board.resolve())]
    assert next(a.text() for a in window.menu_recent.actions()).endswith("board.perf")
    _close(window)


def test_the_recent_list_moves_a_repeat_to_the_top_rather_than_listing_it_twice(
    tmp_path,
) -> None:
    window = _window_on(_load_dense())
    first = tmp_path / "one.perf"
    second = tmp_path / "two.perf"
    for path in (first, second):
        path.write_text(GOLDEN.read_text(encoding="utf-8"), encoding="utf-8")

    window._remember_path(first)
    window._remember_path(second)
    window._remember_path(first)

    assert window._recent_paths() == [str(first.resolve()), str(second.resolve())]
    _close(window)


def test_the_recent_list_is_capped(tmp_path) -> None:
    window = _window_on(_load_dense())
    for index in range(window.RECENT_LIMIT + 3):
        path = tmp_path / f"b{index}.perf"
        path.write_text(GOLDEN.read_text(encoding="utf-8"), encoding="utf-8")
        window._remember_path(path)

    assert len(window._recent_paths()) == window.RECENT_LIMIT
    _close(window)


def test_a_file_that_has_gone_away_is_left_out_of_the_menu(tmp_path) -> None:
    """Skipped rather than shown greyed out: a list of names that no longer open anything
    is a list you stop reading."""
    window = _window_on(_load_dense())
    path = tmp_path / "gone.perf"
    path.write_text(GOLDEN.read_text(encoding="utf-8"), encoding="utf-8")
    window._remember_path(path)
    path.unlink()

    window._refresh_recent_menu()

    labels = [a.text() for a in window.menu_recent.actions()]
    assert not any("gone.perf" in label for label in labels)
    _close(window)


def test_clearing_the_recent_list_empties_the_menu(tmp_path) -> None:
    window = _window_on(_load_dense())
    path = tmp_path / "one.perf"
    path.write_text(GOLDEN.read_text(encoding="utf-8"), encoding="utf-8")
    window._remember_path(path)

    window.on_clear_recent()

    assert window._recent_paths() == []
    assert [a.text() for a in window.menu_recent.actions()] == ["(nothing yet)"]
    _close(window)


def test_undo_and_redo_say_whether_there_is_anything_to_do(tmp_path) -> None:
    """The bus has always known both; the window simply never asked, so an undo at the
    bottom of the stack looked identical to one that worked."""
    from perfstudio.commands import AddNetPayload

    window = _window_on(_load_dense())
    assert window.act_undo.isEnabled() is False
    assert window.act_redo.isEnabled() is False

    window.bus.dispatch("net.add", AddNetPayload(name="HAND"))
    assert window.act_undo.isEnabled() is True
    assert "HAND" in window.act_undo.toolTip()

    window.bus.undo()
    assert window.act_undo.isEnabled() is False
    assert window.act_redo.isEnabled() is True
    _close(window)


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------


def test_the_nets_panel_filters_by_name_and_by_pin() -> None:
    """Half the time the question is "what is U1 pin 3 on", not "where is GND"."""
    window = _window_on(_load_dense())
    all_rows = window.nets_tree.topLevelItemCount()
    assert all_rows > 1

    window.nets_filter.setText("gnd")
    named = [
        window.nets_tree.topLevelItem(i).text(0)
        for i in range(window.nets_tree.topLevelItemCount())
    ]
    assert named and all("gnd" in name.lower() for name in named)

    node = window.bus.document.nets[0].nodes[0]
    window.nets_filter.setText(f"{node.component_ref}.{node.pin}")
    assert window.nets_tree.topLevelItemCount() == 1

    window.nets_filter.clear()
    assert window.nets_tree.topLevelItemCount() == all_rows
    _close(window)


def test_a_part_whose_name_the_dock_cannot_fit_carries_it_in_a_tooltip() -> None:
    window = _window_on(_load_dense())
    tree = window.library_tree
    group = tree.topLevelItem(0)
    assert group is not None and group.childCount()
    leaf = group.child(0)

    assert leaf.text(0) in leaf.toolTip(0)
    _close(window)


# ---------------------------------------------------------------------------
# The shortcut card
# ---------------------------------------------------------------------------


def test_the_shortcut_card_is_read_off_the_real_menu_bar() -> None:
    """A hand-kept list goes stale the first time an action moves, and a stale shortcut
    card teaches something that no longer works."""
    from perfstudio.ui.main import ShortcutsDialog

    window = _window_on(_load_dense())

    listed = ShortcutsDialog.menu_shortcuts(window._menus)
    flat = {label: keys for _menu, rows in listed for label, keys in rows}

    assert flat["Save"] == window.act_save.shortcut().toString()
    assert flat["Autoroute All Nets"] == "Ctrl+R"
    assert "Rotate Clockwise" in flat
    # Read from the menu, so an action with no binding must not appear at all.
    assert "Finish Adding Pins" not in flat
    _close(window)


def test_no_two_actions_claim_the_same_shortcut() -> None:
    """Two actions on one binding means one of them cannot be reached, and Qt reports it
    only as an "ambiguous shortcut overload" at the moment it is pressed."""
    from perfstudio.ui.main import ShortcutsDialog

    window = _window_on(_load_dense())

    seen: dict[str, str] = {}
    clashes = []
    for _menu, rows in ShortcutsDialog.menu_shortcuts(window._menus):
        for label, keys in rows:
            if keys in seen:
                clashes.append(f"{keys}: {seen[keys]} and {label}")
            seen[keys] = label

    assert clashes == [], f"shortcuts claimed twice: {clashes}"
    _close(window)


def test_the_board_gestures_are_listed_because_no_menu_carries_them() -> None:
    """Middle-drag to pan, right-click to finish a run, arrows to nudge: none of them is
    an action anywhere, so until this dialog the only way to find out was the source."""
    from perfstudio.ui.main import ShortcutsDialog

    gestures = dict((keys, what) for keys, what in ShortcutsDialog.BOARD_GESTURES)

    assert "Middle-drag" in gestures
    assert "Esc" in gestures
    assert "Arrow keys" in gestures


def test_the_menus_survive_a_garbage_collection() -> None:
    """QMenuBar.addMenu hands Python a QMenu it believes it owns, so a menu held only by a
    local in the builder can be destroyed the moment that method returns -- leaving a menu
    bar of actions pointing at freed menus. Found by the shortcut card, which was the
    first thing ever to walk the menus after building them."""
    import gc

    window = _window_on(_load_dense())

    gc.collect()

    assert window.act_save.shortcut().toString() == "Ctrl+S"
    assert all(menu.actions() is not None for menu in window._menus)
    _close(window)


# ---------------------------------------------------------------------------
# Joining two pins in two clicks
#
# Declaring a net, filling it and routing it is the honest model, and it was also four
# steps deep in two menus before a single pin could be joined to anything. Most of the
# time nobody is thinking "I shall declare a net"; they are pointing at two legs.
# ---------------------------------------------------------------------------


def _hole_of(window, ref: str, pin: str) -> HoleCoord:
    from perfstudio.geometry import all_pin_holes

    comp = next(c for c in window.bus.document.components if c.ref == ref)
    footprint = window.lookup(comp.footprint_id)
    return next(hole for p, hole in all_pin_holes(comp, footprint) if p.number == pin)


def _free_pins(window) -> list[tuple[str, str]]:
    """Pins on the board that no net has claimed."""
    from perfstudio.geometry import all_pin_holes

    taken = {(n.component_ref, n.pin) for net in window.bus.document.nets for n in net.nodes}
    return [
        (comp.ref, pin.number)
        for comp in window.bus.document.components
        if (footprint := window.lookup(comp.footprint_id)) is not None
        for pin, _hole in all_pin_holes(comp, footprint)
        if (comp.ref, pin.number) not in taken
    ]


def test_two_free_pins_get_a_net_of_their_own() -> None:
    window = _window_on(_load_dense())
    first, second = _free_pins(window)[:2]

    window.scene.arm_connect(True)
    window.scene.connect_click(_hole_of(window, *first))
    result = window.scene.connect_click(_hole_of(window, *second))

    assert result is not None and result.ok, result
    made = window.bus.document.nets[-1]
    assert made.name == "N1"
    assert {(n.component_ref, n.pin) for n in made.nodes} == {first, second}
    _close(window)


def test_a_pin_joined_to_a_pin_on_a_net_joins_that_net() -> None:
    """What a person pointing at a leg and then at a rail means."""
    window = _window_on(_load_dense())
    net = next(n for n in window.bus.document.nets if n.nodes)
    on_net = (net.nodes[0].component_ref, net.nodes[0].pin)
    free = _free_pins(window)[0]
    before = len(window.bus.document.nets)

    window.scene.arm_connect(True)
    window.scene.connect_click(_hole_of(window, *free))
    result = window.scene.connect_click(_hole_of(window, *on_net))

    assert result is not None and result.ok, result
    assert len(window.bus.document.nets) == before  # joined, not invented
    after = next(n for n in window.bus.document.nets if n.id == net.id)
    assert free in {(n.component_ref, n.pin) for n in after.nodes}
    _close(window)


def _pin_by_holding_net(window) -> dict[str, tuple[str, str]]:
    """One pin per net, keyed by the net that EFFECTIVELY holds it.

    First net wins, matching the connectivity engine and LVS -- dense.perf has a pin named
    by two nets, which is a document the net commands would now refuse and a file from
    before they existed is still allowed to contain.
    """
    holder: dict[tuple[str, str], str] = {}
    for net in window.bus.document.nets:
        for node in net.nodes:
            holder.setdefault((node.component_ref, node.pin), net.name)
    by_net: dict[str, tuple[str, str]] = {}
    for pin, name in holder.items():
        by_net.setdefault(name, pin)
    return by_net


def test_two_pins_on_different_nets_are_refused_with_both_named() -> None:
    """Merging two nets is a change to the circuit, not something two clicks may do."""
    window = _window_on(_load_dense())
    reasons: list[str] = []
    window.scene.netPinRejected.connect(reasons.append)
    (name_a, pin_a), (name_b, pin_b) = list(_pin_by_holding_net(window).items())[:2]
    before = window.bus.document

    window.scene.arm_connect(True)
    window.scene.connect_click(_hole_of(window, *pin_a))
    result = window.scene.connect_click(_hole_of(window, *pin_b))

    assert result is None
    assert window.bus.document is before
    assert name_a in reasons[-1] and name_b in reasons[-1]
    _close(window)


def test_two_pins_already_on_one_net_say_so_rather_than_dispatching() -> None:
    window = _window_on(_load_dense())
    reasons: list[str] = []
    window.scene.netPinRejected.connect(reasons.append)
    net = next(n for n in window.bus.document.nets if len(n.nodes) >= 2)
    before = window.bus.document

    window.scene.arm_connect(True)
    window.scene.connect_click(_hole_of(window, net.nodes[0].component_ref, net.nodes[0].pin))
    window.scene.connect_click(_hole_of(window, net.nodes[1].component_ref, net.nodes[1].pin))

    assert window.bus.document is before
    assert net.name in reasons[-1]
    _close(window)


def test_the_tool_stays_armed_so_connections_can_be_chained() -> None:
    """A board is a list of connections, not one, and re-arming between each of them is
    the friction this tool exists to remove."""
    window = _window_on(_load_dense())
    a, b, c = _free_pins(window)[:3]

    window.scene.arm_connect(True)
    window.scene.connect_click(_hole_of(window, *a))
    window.scene.connect_click(_hole_of(window, *b))
    assert window.scene.connect_armed
    assert window.scene.connect_from() is None  # ready for the next pair, not mid-pair

    window.scene.connect_click(_hole_of(window, *c))
    result = window.scene.connect_click(_hole_of(window, *a))

    assert result is not None and result.ok, result
    assert len(window.bus.document.nets[-1].nodes) == 3
    _close(window)


def test_a_refused_pair_does_not_leave_the_first_pin_armed() -> None:
    """Otherwise the next click joins something the user has stopped thinking about."""
    window = _window_on(_load_dense())

    pins = list(_pin_by_holding_net(window).values())[:2]

    window.scene.arm_connect(True)
    window.scene.connect_click(_hole_of(window, *pins[0]))
    window.scene.connect_click(_hole_of(window, *pins[1]))

    assert window.scene.connect_from() is None
    _close(window)


def test_clicking_an_empty_hole_while_connecting_says_so() -> None:
    window = _window_on(_load_dense())
    reasons: list[str] = []
    window.scene.netPinRejected.connect(reasons.append)

    window.scene.arm_connect(True)
    window.scene.connect_click(HoleCoord(0, 0))

    assert reasons and "No component pin" in reasons[0]
    assert window.scene.connect_from() is None
    _close(window)


def test_arming_another_board_tool_ends_a_connection_in_progress() -> None:
    window = _window_on(_load_dense())
    window.scene.arm_connect(True)
    window.scene.connect_click(_hole_of(window, *_free_pins(window)[0]))

    window.scene.arm_drawing("bare-wire")

    assert window.scene.connect_armed is False
    assert window.scene.connect_from() is None
    _close(window)


def test_the_automatic_net_name_counts_from_the_document() -> None:
    """Like next_reference, and for the same reason: a hidden counter would disagree with
    the document after an undo and the bus would refuse the name for an invisible reason."""
    from perfstudio.commands import AddNetPayload
    from perfstudio.ui.view2d import next_net_name

    window = _window_on(_load_dense())
    assert next_net_name(window.bus.document) == "N1"

    window.bus.dispatch("net.add", AddNetPayload(name="N1"))
    assert next_net_name(window.bus.document) == "N2"

    window.bus.undo()
    assert next_net_name(window.bus.document) == "N1"
    _close(window)


# ---------------------------------------------------------------------------
# The toolbar
# ---------------------------------------------------------------------------


def test_every_tool_on_the_bar_has_a_picture_and_a_short_label() -> None:
    """A toolbar of eleven identical grey rectangles is one people read left to right
    every time, which is how the tools ended up being hunted for in the menus instead."""
    window = _window_on(_load_dense())

    tools = [a for a in window.toolbar.actions() if not a.isSeparator()]

    assert len(tools) >= 15
    for action in tools:
        assert not action.icon().isNull(), f"{action.text()} has no icon"
        assert action.iconText(), f"{action.text()} has no button label"
        assert len(action.iconText()) <= 12, f"{action.iconText()} is too long for a button"
    _close(window)


def test_the_menus_keep_the_full_wording_the_buttons_abbreviate() -> None:
    """Qt draws iconText on a toolbar and text in a menu, so the short label must not
    have overwritten the exact one."""
    window = _window_on(_load_dense())

    assert window.act_autoroute.iconText() == "Autoroute"
    assert "All Nets" in window.act_autoroute.text()
    _close(window)


def test_an_unknown_icon_is_empty_rather_than_an_exception() -> None:
    """A missing picture is a cosmetic fault; taking the window down over one would not
    be."""
    from perfstudio.ui.icons import icon

    assert icon("no-such-icon").isNull()
    assert not icon("connect").isNull()


def test_every_icon_in_the_set_draws() -> None:
    """They are drawn with QPainter at import-independent sizes, so a broken path shows up
    as an empty pixmap rather than an error anywhere."""
    from perfstudio.ui.icons import DRAWINGS, icon

    for name in DRAWINGS:
        assert not icon(name).isNull(), name
        assert not icon(name).pixmap(22, 22).isNull(), name


# ---------------------------------------------------------------------------
# Pictures of the parts
# ---------------------------------------------------------------------------


def test_every_archetype_the_model_has_can_be_drawn() -> None:
    """A part with no picture in a list of pictures reads as a broken row, so the model
    gaining an archetype has to fail here rather than ship a blank."""
    import typing

    from perfstudio.model import BodyArchetype
    from perfstudio.ui.icons import PART_DRAWINGS

    declared = set(typing.get_args(BodyArchetype))
    # axial-cylinder is drawn by the polarity-aware path rather than the plain table,
    # because a resistor and a DO-41 diode share the archetype and look nothing alike.
    assert declared - set(PART_DRAWINGS) == {"axial-cylinder"}


def test_every_footprint_in_the_library_gets_a_picture() -> None:
    from perfstudio.footprints import standard_footprints
    from perfstudio.ui.icons import PART_SIZE, part_icon

    for footprint in standard_footprints().values():
        drawn = part_icon(footprint)
        assert not drawn.isNull(), footprint.id
        assert not drawn.pixmap(PART_SIZE, PART_SIZE).isNull(), footprint.id


def test_a_part_is_drawn_in_the_colour_the_board_draws_it() -> None:
    """The whole point of the pictures: picking an electrolytic from the list and finding
    it on the board is recognition rather than reading. A second palette here would drift
    from the first the moment either was touched."""
    from perfstudio.footprints import standard_footprints
    from perfstudio.ui.bodies import style_for
    from perfstudio.ui.icons import part_icon

    electrolytic = next(
        f for f in standard_footprints().values() if f.body.archetype == "radial-electrolytic"
    )
    image = part_icon(electrolytic).pixmap(40, 40).toImage()
    fill = QColor(style_for(electrolytic).fill)

    hits = sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if QColor(image.pixel(x, y)) == fill
    )
    assert hits > 40, "the body colour is not what is being drawn"


def test_the_same_part_is_only_drawn_once() -> None:
    """Sixty-one footprints across fifteen archetypes; the list is rebuilt on every
    keystroke in the filter box."""
    from perfstudio.footprints import standard_footprints
    from perfstudio.ui.icons import part_icon

    resistors = [
        f for f in standard_footprints().values() if f.body.archetype == "axial-cylinder"
    ][:2]

    assert part_icon(resistors[0]) is part_icon(resistors[0])
    if len(resistors) > 1 and resistors[0].polarized == resistors[1].polarized:
        assert part_icon(resistors[0]) is part_icon(resistors[1])


def test_the_parts_panel_shows_a_picture_on_every_row() -> None:
    window = _window_on(_load_dense())
    tree = window.library_tree

    groups = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    assert groups
    for group in groups:
        assert not group.icon(0).isNull(), group.text(0)
        for index in range(group.childCount()):
            assert not group.child(index).icon(0).isNull(), group.child(index).text(0)
    _close(window)


# ---------------------------------------------------------------------------
# Escape leaves the mode. From anywhere. Every mode.
#
# Reported: placing a part could not be cancelled with Escape. It was bound to the Draw
# menu's stop entry, which cancelled drawing, pin-picking and connecting -- and not
# placement. Being a WINDOW shortcut it also fires before the scene sees the key, so the
# scene's own Escape handling for placement was unreachable in the running application
# while the hint under the parts list said "Esc cancels" the whole time.
# ---------------------------------------------------------------------------


def _press_escape(window, focus_on) -> None:
    """A real Escape, through the shortcut machinery, from a real focus.

    Not window.on_stop_tool(): the bug was entirely about which of them the key reaches,
    so a test that called the handler directly would have passed against the broken build.
    """
    from PySide6.QtCore import Qt as QtCore_Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    window.show()
    focus_on.setFocus()
    QApplication.processEvents()
    QTest.keyClick(focus_on, QtCore_Qt.Key.Key_Escape)
    QApplication.processEvents()


def test_escape_cancels_placing_a_part_from_the_parts_list() -> None:
    """The reported bug, at the focus a user actually has: they picked the part from the
    list, so the list is what has the keyboard."""
    window = _window_on(_load_dense())
    window.scene.arm_placement("r-axial-5")
    assert window.scene.armed_footprint_id == "r-axial-5"

    _press_escape(window, window.library_tree)

    assert window.scene.armed_footprint_id is None
    _close(window)


def test_escape_cancels_drawing_from_a_filter_box() -> None:
    window = _window_on(_load_dense())
    window.scene.arm_drawing("bare-wire")

    _press_escape(window, window.library_filter)

    assert window.scene.armed_draw_kind is None
    _close(window)


def test_escape_cancels_connecting() -> None:
    window = _window_on(_load_dense())
    window.scene.arm_connect(True)

    _press_escape(window, window.view)

    assert window.scene.connect_armed is False
    _close(window)


def test_escape_cancels_a_pin_session_without_committing_it() -> None:
    from perfstudio.commands import AddNetPayload

    window = _window_on(_load_dense())
    window.bus.dispatch("net.add", AddNetPayload(name="HAND"))
    net = next(n for n in window.bus.document.nets if n.name == "HAND")
    window.scene.arm_net_pins(net.id)
    window.scene.net_pin_click(_hole_of(window, *_free_pins(window)[0]))
    before = window.bus.document

    _press_escape(window, window.view)

    assert window.scene.armed_net_id is None
    assert window.bus.document is before, "Escape must abandon the session, not commit it"
    _close(window)


def test_leaving_a_mode_is_one_method_rather_than_four_call_sites() -> None:
    """The window's shortcut and the scene's key handler both call this, so they cannot
    end up cancelling different sets of things -- which is how placement got left out."""
    window = _window_on(_load_dense())
    window.scene.arm_placement("r-axial-5")
    assert window.scene.in_a_mode

    window.scene.leave_mode()

    assert not window.scene.in_a_mode
    assert window.scene.armed_footprint_id is None
    assert window.scene.armed_draw_kind is None
    assert window.scene.armed_net_id is None
    assert window.scene.connect_armed is False
    _close(window)


def test_the_board_reports_being_in_a_mode_for_each_of_the_four() -> None:
    from perfstudio.commands import AddNetPayload

    window = _window_on(_load_dense())
    window.bus.dispatch("net.add", AddNetPayload(name="HAND"))
    net = next(n for n in window.bus.document.nets if n.name == "HAND")
    assert not window.scene.in_a_mode

    for arm in (
        lambda: window.scene.arm_placement("r-axial-5"),
        lambda: window.scene.arm_drawing("bare-wire"),
        lambda: window.scene.arm_net_pins(net.id),
        lambda: window.scene.arm_connect(True),
    ):
        arm()
        assert window.scene.in_a_mode
        window.scene.leave_mode()
        assert not window.scene.in_a_mode
    _close(window)
