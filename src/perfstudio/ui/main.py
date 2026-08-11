"""PerfStudio desktop application: the real engine behind the prototype's window.

Promoted from ``prototypes/qt/main.py``. Everything the prototype only gestured at in
its status-bar comment -- "(engine would now re-run DRC and re-route the nets it
touches)" -- actually happens here: every mutation goes through a
``perfstudio.command.CommandBus`` (never a direct write to the document), and every
successful command re-runs ``run_drc``/``run_lvs`` and repaints from the bus's own
document.

    python -m perfstudio.ui.main                 launch the app (blank document)
    python -m perfstudio.ui.main path/to.perf     launch the app, opening a document
    python -m perfstudio.ui.main --version        print the version and exit
    python -m perfstudio.ui.main --headless [path]
        render 2D/3D/PDF to files, run DRC and LVS, print counts and timings, and exit
        non-zero if the pipeline itself failed (bad file, a scale check that doesn't
        pass, a 3D render exception) -- NOT merely because DRC/LVS found violations,
        which is the normal, expected output of checking a real board.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import sys
import time
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QStatusBar,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from perfstudio import persist
from perfstudio.autoroute import (
    AutoroutePlan,
    UnroutedLink,
    describe_reroute,
    plan_autoroute,
    plan_reroute,
)
from perfstudio.autoroute import (
    describe as describe_plan,
)
from perfstudio.command import CommandBus, CommandContext, DispatchResult, HistoryEntry
from perfstudio.commands import (
    DEFAULT_BOARD,
    DeleteComponentPayload,
    DeleteConductorsPayload,
    ImportNetlistPayload,
    MirrorComponentPayload,
    PlaceComponentPayload,
    RotateComponentPayload,
    SetBoardPayload,
    UpdateComponentPayload,
    create_document_id_generator,
    create_empty_document,
    create_standard_registry,
)
from perfstudio.drc import DrcViolation, run_drc
from perfstudio.footprints import footprint_lookup, standard_footprints
from perfstudio.geometry import board_size_mm, format_hole, hole_span_mm
from perfstudio.guide import build_guide
from perfstudio.guide import describe as describe_guide
from perfstudio.guide_export import bom_to_csv, cut_list_to_csv, guide_to_html, guide_to_json
from perfstudio.lvs import LvsIssue, LvsResult, run_lvs, stale_conductor_ids
from perfstudio.model import (
    Board,
    BoardMaterial,
    BoardSide,
    ComponentInstance,
    DocumentMeta,
    Footprint,
    HoleCoord,
    NetId,
    PerfDocument,
    Rotation,
)
from perfstudio.parsers.kicad import parse_kicad_netlist
from perfstudio.placer import (
    PlacementOptions,
    PlacementPlan,
    plan_placement,
)
from perfstudio.placer import (
    describe as describe_placement,
)
from perfstudio.placer import (
    summarize_changes as summarize_placement,
)
from perfstudio.ratsnest import NetRatsnest, ratsnest, summarize
from perfstudio.version import __version__
from perfstudio.version import describe as describe_version

from . import view3d
from .export_pdf import export_pdf, verify_scale
from .theme import ERROR, OK, STYLESHEET, TEXT_DIM, WARNING
from .view2d import RULER_MARGIN_MM, BoardScene, BoardView, next_reference

ROLE_HOLES = int(Qt.ItemDataRole.UserRole) + 1
ROLE_COMPONENT_IDS = int(Qt.ItemDataRole.UserRole) + 2
ROLE_NET_ID = int(Qt.ItemDataRole.UserRole) + 3
ROLE_FOOTPRINT_ID = int(Qt.ItemDataRole.UserRole) + 4


def _now_iso() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _rotation_after(current: Rotation, delta: int) -> Rotation:
    """The next legal rotation, wrapping. ``model.VALID_ROTATIONS`` is 0/90/180/270 and the
    command refuses anything else, so the wrap happens here rather than being discovered as
    a rejected command."""
    turned = (int(current) + delta) % 360
    return cast(Rotation, turned)


#: Reference letter -> the registry footprint to try for it. The inverse of
#: view2d.REF_PREFIXES, used when a netlist gives a reference and a pin count and nothing this
#: registry can match: a KiCad netlist's footprint field names a KiCad library part.
_GUESS_BY_PREFIX: dict[str, str] = {
    "R": "r-axial-3",
    "D": "d-do41",
    "LED": "led-5mm",
    "C": "c-disc-p2",
    "Q": "to92",
    "Y": "xtal-hc49",
    "RV": "pot-3",
    "SW": "sw-tactile",
    "K": "relay-spdt",
    "TB": "screw-terminal-2",
}


def guess_footprint_id(ref: str, pin_count: int) -> str:
    """A first guess at a footprint from a schematic reference and how many pins it uses.

    A guess, stated as one: the netlist knows the part is called "U3" and that three of its pins
    appear in nets, which is genuinely all there is to go on. It is enough to be useful -- a "R"
    with two pins really is an axial resistor -- and it is why the parts land somewhere obvious
    for the user to correct rather than being quietly treated as final.
    """
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    if letters in ("U", "IC") or pin_count > 4:
        # An IC, sized to what the netlist actually uses, rounded up to a real DIP.
        for pins in (8, 14, 16, 18, 20, 24, 28, 40):
            if pin_count <= pins:
                return f"dip-{pins}"
        return "dip-40"
    if letters in ("J", "P", "CN"):
        return f"hdr-1x{max(pin_count, 1)}"
    if letters == "C" and pin_count == 2:
        return "c-disc-p2"
    return _GUESS_BY_PREFIX.get(letters, "r-axial-3")


def read_document_text(path: Path) -> tuple[str | None, str | None]:
    """Read a ``.perf`` file, returning ``(text, None)`` or ``(None, message)``.

    Shared by the command line and the Open dialog so a bad path reads the same either way,
    and neither can reach the user as a traceback -- which says where Python gave up rather
    than what they should fix. The directory case is called out separately because the OS
    reports it as "permission denied", which sends someone looking for a permissions problem
    they do not have.
    """
    if path.is_dir():
        return None, f"{path} is a directory, not a .perf document."
    try:
        return path.read_text(encoding="utf-8"), None
    except OSError as err:
        return None, f"Cannot open {path}: {err.strerror or err}."
    except UnicodeDecodeError:
        return None, f"Cannot open {path}: not UTF-8 text. A .perf document is JSON."


class BoardSetupDialog(QDialog):
    """Grid size and substrate for a board.

    The material is not a cosmetic choice and the dialog says so where the choice is
    made. FR-2 phenolic -- the cheap brown board most perfboard is actually sold as --
    lifts its pads under sustained heat, so DRC's pad-lifting rule only fires on it and
    the build guide drops the iron 30 degrees and halves the dwell time. Choosing the
    wrong one here means the tool's most useful safety advice never appears.
    """

    MATERIALS: tuple[tuple[BoardMaterial, str], ...] = (
        ("FR4", "FR-4 — glass epoxy, the green kind. Tolerates heat well."),
        ("FR2", "FR-2 — phenolic paper (\"pertinaks\"), the brown kind. Pads lift easily."),
        ("FR1", "FR-1 — phenolic paper, as FR-2."),
    )

    def __init__(self, board: Board, parent: QWidget | None = None, title: str = "New Board") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)

        self.cols = QSpinBox()
        self.cols.setRange(2, 400)
        self.cols.setValue(board.cols)
        self.rows = QSpinBox()
        self.rows.setRange(2, 400)
        self.rows.setValue(board.rows)
        self.material = QComboBox()
        for value, label in self.MATERIALS:
            self.material.addItem(label, value)
        index = self.material.findData(board.material)
        self.material.setCurrentIndex(max(0, index))

        self._size_note = QLabel()
        self.cols.valueChanged.connect(self._update_note)
        self.rows.valueChanged.connect(self._update_note)
        self._pitch = board.pitch
        self._update_note()

        form = QFormLayout()
        form.addRow("Columns", self.cols)
        form.addRow("Rows", self.rows)
        form.addRow("Material", self.material)
        form.addRow("", self._size_note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self._board = board

    def _update_note(self) -> None:
        # The physical size, because that is what someone holds against a piece of board
        # they already own -- "80 columns" means nothing at the shop.
        width = self.cols.value() * self._pitch
        height = self.rows.value() * self._pitch
        self._size_note.setText(
            f"<span style='color:{TEXT_DIM}'>{width:.1f} × {height:.1f} mm "
            f"({self.cols.value() * self.rows.value()} holes)</span>"
        )

    def board(self) -> Board:
        return dataclasses.replace(
            self._board,
            cols=self.cols.value(),
            rows=self.rows.value(),
            material=cast(BoardMaterial, self.material.currentData()),
        )


def window_title(path: Path | None = None, modified: bool = False) -> str:
    """The title bar names the build, the document, and whether it is saved.

    While the version carries a ``.dev`` suffix this is not decoration: pre-release
    builds get screenshotted into bug reports, and a screenshot that does not say which
    build it came from costs a round trip to find out.

    The bullet is the standard unsaved marker, and it is here rather than only in the
    close dialog because by the time the dialog appears the decision is already being
    forced -- the title is where someone notices in time to just press Ctrl+S.
    """
    name = f"PerfStudio {__version__}"
    document = f" — {path.name}" if path is not None else " — untitled"
    return f"{'• ' if modified else ''}{name}{document}"


def _find_repo_root() -> Path:
    """Best-effort discovery of the dev checkout root, for the headless default
    fixture. Falls back to cwd, which is also a perfectly fine place to look when the
    package has been installed rather than run from a checkout."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "tools" / "diffcheck" / "golden").is_dir():
            return parent
    return Path.cwd()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self, document: PerfDocument, path: Path | None = None) -> None:
        super().__init__()
        self.lookup = footprint_lookup()
        self.side: BoardSide = "top"
        self.current_path = path
        self.bus = self._new_bus(document)
        self._unsubscribe: Any = None
        self._last_drc_ms = 0.0
        self._last_violations: tuple[DrcViolation, ...] = ()
        self._last_lvs: LvsResult | None = None
        self._last_ratsnest: tuple[NetRatsnest, ...] = ()
        self._vtk_renderer: Any = None
        self._vtk_style: Any = None
        self.vtk_widget: Any = None
        #: The document changed while the 3D panel was hidden, so it needs re-actoring
        #: before it is shown again.
        self._3d_stale = False
        #: Advanced by "Try Another Arrangement". Held on the window rather than passed
        #: in, so pressing it repeatedly keeps exploring instead of re-running the same
        #: search and reporting the same answer.
        self._place_seed = 0
        #: Nets whose copper was laid out for a position a part has since left. See
        #: _track_moved_nets for why this is remembered rather than detected.
        self._nets_from_old_layout: set[NetId] = set()

        #: The document as it last hit disk. Identity comparison against the bus's
        #: current document is what "modified" means here -- see is_modified.
        self._saved_document = document
        self.setWindowTitle(window_title(path))
        self.resize(1500, 950)
        self.setStyleSheet(STYLESHEET)

        self.scene = BoardScene(self.bus.document, self.lookup, side=self.side, bus=self.bus)
        self.view = BoardView(self.scene)
        self.scene.moveCommitted.connect(self.on_move_committed)
        self.scene.selectionNetsChanged.connect(self._on_selection_nets_changed)
        self.scene.placementArmed.connect(self._on_placement_armed)
        self.scene.drawArmed.connect(self._on_draw_armed)
        self.scene.conductorDrawn.connect(self._on_conductor_drawn)
        self.scene.hoveredHole.connect(self._on_hovered_hole)
        self.scene.componentPlaced.connect(self._on_component_placed)

        # The 2D editor is the application; the 3D view is a panel you open when you want
        # it. See _build_3d_dock for why that is not just a layout preference.
        self.setCentralWidget(self.view)

        # Docks before menus: the View menu offers each dock's own toggleViewAction, so the
        # docks have to exist for the menu to be able to name them.
        self._build_library_dock()
        self._build_nets_dock()
        self._build_3d_dock()
        self._build_drc_dock()
        self._build_menu()
        self._build_toolbar()
        self._build_status_bar()

        self._subscribe_bus()
        self.on_bus_changed(self.bus.document, None)

    # -- bus wiring ----------------------------------------------------------

    def _new_bus(self, document: PerfDocument) -> CommandBus:
        """A bus whose id generator cannot collide with the document's existing ids.

        ``create_id_generator()`` restarts at zero, so on a document loaded from disk with
        conductors already named ``cond-1``.. the very next edit would be refused as a
        duplicate id. Every bus this window makes has to be seeded from the document it is
        given -- see ``commands.create_document_id_generator``.
        """
        return CommandBus(
            document,
            create_standard_registry(),
            CommandContext(next_id=create_document_id_generator(document)),
        )

    def _subscribe_bus(self) -> None:
        """Listen to the current bus, dropping any previous subscription first.

        Opening a document replaces the bus. Without unsubscribing, the abandoned bus keeps
        a live reference to this window and would still be able to repaint it -- so an undo
        on the old stack would redraw the new document's view with the old document.
        """
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = self.bus.subscribe(self.on_bus_changed)

    # -- 3D dock -------------------------------------------------------------

    def _build_3d_dock(self) -> None:
        """A closable panel, and nothing inside it until it is first opened.

        Three separate costs are avoided by keeping it shut, which is why this is a dock
        rather than a permanent half of the window:
          - Creating the widget at all means an OpenGL context and VTK's whole pipeline.
          - Every command rebuilt several thousand actors, whether or not anyone could see
            them (a 60x40 board is 2400 pads plus a part and conductor actor each).
          - Every command then rendered a frame into a widget that may be hidden.
        Now the widget is built on first open, and refreshes are skipped while hidden and
        deferred until it is shown again -- see _refresh_3d.
        """
        self.dock_3d = QDockWidget("3D View", self)
        self.dock_3d.setObjectName("dock3d")
        self._3d_placeholder = QLabel("Opening the 3D view builds it — this takes a moment.")
        self._3d_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._3d_placeholder.setWordWrap(True)
        container = QWidget()
        self._3d_layout = QVBoxLayout(container)
        self._3d_layout.setContentsMargins(0, 0, 0, 0)
        self._3d_layout.addWidget(self._3d_placeholder)
        self.dock_3d.setWidget(container)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_3d)
        self.dock_3d.setMinimumWidth(280)
        self.resizeDocks([self.dock_3d], [480], Qt.Orientation.Horizontal)
        self.dock_3d.hide()
        self.dock_3d.visibilityChanged.connect(self._on_3d_visibility_changed)

    def _on_3d_visibility_changed(self, visible: bool) -> None:
        if not visible:
            return
        if self.vtk_widget is None:
            self._create_3d_widget()
        elif self._3d_stale:
            self._refresh_3d()

    def _create_3d_widget(self) -> None:
        # Typed Any, not QWidget | None: the real runtime type is a
        # QVTKRenderWindowInteractor (VTK ships no stubs), which has methods (like
        # GetRenderWindow) QWidget itself does not declare.
        try:
            from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor

            widget: Any = QVTKRenderWindowInteractor()  # type: ignore[no-untyped-call]
            ren, _stats = view3d.build_renderer(
                self.bus.document, self.lookup, flipped=(self.side == "bottom")
            )
            widget.GetRenderWindow().AddRenderer(ren)
            widget.Initialize()
            widget.Start()
            # Trackball, explicitly. VTK's default interactor style is a "switch" that can
            # start in joystick mode, where holding the button keeps the camera spinning
            # instead of following the pointer -- which feels broken rather than different,
            # and is not something a user would think to go and change.
            style = view3d.trackball_style()
            widget.GetRenderWindow().GetInteractor().SetInteractorStyle(style)
            self._vtk_renderer = ren
            self._vtk_style = style
            self.vtk_widget = widget
            self._3d_placeholder.hide()
            self._3d_layout.addWidget(widget)
            self._3d_stale = False
        except Exception as exc:  # pragma: no cover - environment dependent
            print(f"[3D] Qt/VTK widget unavailable: {exc}", file=sys.stderr)
            self._vtk_renderer = None
            self._3d_placeholder.setText(f"3D view unavailable:\n{exc}")

    def _3d_is_live(self) -> bool:
        return self.vtk_widget is not None and self.dock_3d.isVisible()

    def _refresh_3d(self) -> None:
        """Re-actor the existing renderer. Deliberately does NOT touch the camera.

        Marks itself stale and returns immediately when nobody is looking, so a closed panel
        costs nothing per command; ``_on_3d_visibility_changed`` catches up on reopen.
        """
        if self.vtk_widget is None or self._vtk_renderer is None:
            return
        if not self.dock_3d.isVisible():
            self._3d_stale = True
            return
        view3d.populate_renderer(self._vtk_renderer, self.bus.document, self.lookup)
        self.vtk_widget.GetRenderWindow().Render()
        self._3d_stale = False

    def on_reset_3d_camera(self) -> None:
        if self._vtk_renderer is None:
            return
        view3d.apply_default_camera(self._vtk_renderer, flipped=(self.side == "bottom"))
        if self.vtk_widget is not None:
            self.vtk_widget.GetRenderWindow().Render()

    # -- menu ------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
        act_new = file_menu.addAction("&New Board…")
        act_new.setShortcut(QKeySequence.StandardKey.New)
        act_new.triggered.connect(self.on_new)
        act_open = file_menu.addAction("&Open…")
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self.on_open)
        act_save = file_menu.addAction("&Save")
        act_save.setShortcut(QKeySequence.StandardKey.Save)
        act_save.triggered.connect(self.on_save)
        act_save_as = file_menu.addAction("Save &As…")
        act_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        act_save_as.triggered.connect(self.on_save_as)
        file_menu.addSeparator()
        act_board = file_menu.addAction("&Board Setup…")
        act_board.setToolTip(
            "Grid size and substrate. The material is not cosmetic: it decides the iron "
            "temperature the build guide gives and whether the pad-lifting rule applies."
        )
        act_board.triggered.connect(self.on_board_setup)
        act_import = file_menu.addAction("&Import KiCad Netlist…")
        act_import.setShortcut(QKeySequence("Ctrl+I"))
        act_import.triggered.connect(self.on_import_netlist)
        file_menu.addSeparator()
        act_guide = file_menu.addAction("Export &Build Guide…")
        act_guide.setShortcut(QKeySequence("Ctrl+B"))
        act_guide.setToolTip(
            "Write the step-by-step soldering guide: one offline HTML file, the wire cut "
            "list and BOM as CSV, and the whole thing as JSON."
        )
        act_guide.triggered.connect(self.on_export_guide)
        act_pdf = file_menu.addAction("Export 1:1 PDF (component + solder side)…")
        act_pdf.triggered.connect(self.on_export_pdf)
        act_png = file_menu.addAction("Export 3D Snapshot PNG…")
        act_png.triggered.connect(self.on_export_3d_png)
        file_menu.addSeparator()
        act_quit = file_menu.addAction("&Quit")
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)

        edit_menu = menu.addMenu("&Edit")
        act_undo = edit_menu.addAction("&Undo")
        act_undo.setShortcut(QKeySequence.StandardKey.Undo)
        act_undo.triggered.connect(self.on_undo)
        act_redo = edit_menu.addAction("&Redo")
        act_redo.setShortcut(QKeySequence("Ctrl+Shift+Z"))
        act_redo.triggered.connect(self.on_redo)
        edit_menu.addSeparator()

        # The engine has had component.rotate and component.mirror since the first commit
        # and nothing in the window could reach them, so a part could only ever be placed
        # in the orientation it arrived in. Placing a DIP or an electrolytic without turning
        # it is not a real workflow.
        self.act_rotate_cw = edit_menu.addAction("Rotate &Clockwise")
        self.act_rotate_cw.setShortcut(QKeySequence("R"))
        self.act_rotate_cw.triggered.connect(lambda: self.on_rotate_selection(90))
        self.act_rotate_ccw = edit_menu.addAction("Rotate Counter-clock&wise")
        self.act_rotate_ccw.setShortcut(QKeySequence("Shift+R"))
        self.act_rotate_ccw.triggered.connect(lambda: self.on_rotate_selection(-90))
        self.act_mirror = edit_menu.addAction("&Mirror")
        self.act_mirror.setShortcut(QKeySequence("M"))
        self.act_mirror.triggered.connect(self.on_mirror_selection)
        self.act_lock = edit_menu.addAction("Toggle &Lock")
        self.act_lock.setShortcut(QKeySequence("L"))
        self.act_lock.triggered.connect(self.on_toggle_lock_selection)
        self.act_delete = edit_menu.addAction("&Delete")
        self.act_delete.setShortcut(QKeySequence.StandardKey.Delete)
        self.act_delete.triggered.connect(self.on_delete_selection)

        #: Actions that act on the selection, so they can be greyed out when there is none.
        #: A menu item that silently does nothing is indistinguishable from a broken one.
        self.selection_actions = (
            self.act_rotate_cw,
            self.act_rotate_ccw,
            self.act_mirror,
            self.act_lock,
            self.act_delete,
        )
        for action in self.selection_actions:
            action.setEnabled(False)

        draw_menu = menu.addMenu("&Draw")
        draw_menu.setToolTipsVisible(True)
        # The engine has had conductor.add since the first commit with nothing able to
        # reach it, so on a perfboard tool there was no way to run a wire or lay a solder
        # trace by hand -- only to accept the router's output whole or discard it.
        self.act_draw: dict[str, QAction] = {}
        for kind, label, shortcut, tip in (
            ("solder-trace", "&Solder Trace", "T",
             "Join adjacent pads with solder. Orthogonal steps only — solder spans the "
             "0.6 mm gap to the next pad and not the 1.7 mm diagonal one. Click each pad, "
             "then Enter or right-click to finish."),
            ("solder-trace-wired", "Solder Trace with S&pine", "Shift+T",
             "The same over a tinned-wire spine: about ten times lower resistance, and "
             "what a power or ground rail longer than five or six pads wants."),
            ("bare-wire", "&Bare Wire", "W",
             "Tinned wire on the solder side. Cannot cross other copper. Click both ends."),
            ("insulated-wire", "&Insulated Wire", "Shift+W",
             "May cross anything, at the cost of stripping it. Click both ends."),
            ("top-jumper", "Top &Jumper", "",
             "Insulated, routed over the component side. Occupies body space."),
        ):
            action = draw_menu.addAction(label)
            action.setCheckable(True)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.setToolTip(tip)
            action.triggered.connect(lambda checked, k=kind: self.on_draw_mode(k, checked))
            self.act_draw[kind] = action
        draw_menu.addSeparator()
        act_stop_draw = draw_menu.addAction("&Stop Drawing")
        act_stop_draw.setShortcut(QKeySequence("Escape"))
        act_stop_draw.triggered.connect(lambda: self.on_draw_mode("", False))

        place_menu = menu.addMenu("&Place")
        self.act_autoplace = place_menu.addAction("&Auto-place Board")
        self.act_autoplace.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self.act_autoplace.setToolTip(
            "Rearrange the unlocked parts to shorten the connections and make them "
            "solderable as traces rather than wires. Shows the result before applying it."
        )
        self.act_autoplace.triggered.connect(lambda: self.on_autoplace())
        act_reroll = place_menu.addAction("&Try Another Arrangement")
        act_reroll.setToolTip(
            "Search again from a different seed. Annealing is a random walk, so this is "
            "a real second answer rather than the same one twice."
        )
        act_reroll.triggered.connect(lambda: self.on_autoplace(reroll=True))

        route_menu = menu.addMenu("&Route")
        self.act_autoroute = route_menu.addAction("&Autoroute All Nets")
        self.act_autoroute.setShortcut(QKeySequence("Ctrl+R"))
        self.act_autoroute.triggered.connect(self.on_autoroute_all)
        self.act_route_selected = route_menu.addAction("Route Nets of &Selection")
        self.act_route_selected.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self.act_route_selected.triggered.connect(self.on_route_selection)
        route_menu.addSeparator()
        # Rip-up and re-route is a SEPARATE verb from autoroute, and deliberately not what
        # Ctrl+R does: autoroute completes a board, this one discards work to rebuild it.
        # See autoroute.ReroutePlan for the measurement that made it necessary.
        self.act_reroute_all = route_menu.addAction("Re-route &Everything")
        self.act_reroute_all.setToolTip(
            "Rip up the existing routing and plan it again from nothing. Use this after "
            "moving parts: autoroute only adds, so it leaves the copper laid out for "
            "where things used to be. Hand-drawn copper with no net is never touched."
        )
        self.act_reroute_all.triggered.connect(lambda: self.on_reroute(None))
        self.act_reroute_selected = route_menu.addAction("Re-route Nets of Se&lection")
        self.act_reroute_selected.setShortcut(QKeySequence("Ctrl+Alt+R"))
        self.act_reroute_selected.triggered.connect(self.on_reroute_selection)
        route_menu.addSeparator()
        self.act_clear_strays = route_menu.addAction("Remove S&tale Conductors")
        self.act_clear_strays.triggered.connect(self.on_clear_strays)

        view_menu = menu.addMenu("&View")
        act_flip = view_menu.addAction("Flip Board (component / solder side)")
        act_flip.setShortcut(QKeySequence("Ctrl+F"))
        act_flip.triggered.connect(self.on_flip_board)
        view_menu.addSeparator()
        act_fit: QAction = view_menu.addAction("&Fit Board")
        act_fit.setShortcut(QKeySequence("Ctrl+0"))
        act_fit.triggered.connect(self.view.fit_board)
        act_zoom_in = view_menu.addAction("Zoom &In")
        act_zoom_in.setShortcut(QKeySequence.StandardKey.ZoomIn)
        act_zoom_in.triggered.connect(lambda: self.view.zoom_by(1.25))
        act_zoom_out = view_menu.addAction("Zoom &Out")
        act_zoom_out.setShortcut(QKeySequence.StandardKey.ZoomOut)
        act_zoom_out.triggered.connect(lambda: self.view.zoom_by(1 / 1.25))
        view_menu.addSeparator()

        self.act_ratsnest = view_menu.addAction("Show &Ratsnest")
        self.act_ratsnest.setCheckable(True)
        self.act_ratsnest.setChecked(True)
        self.act_ratsnest.setShortcut(QKeySequence("Ctrl+E"))
        self.act_ratsnest.toggled.connect(self.scene.set_show_ratsnest)
        self.act_rulers = view_menu.addAction("Show Hole &Addresses")
        self.act_rulers.setCheckable(True)
        self.act_rulers.setChecked(True)
        self.act_rulers.toggled.connect(self.scene.set_show_rulers)
        view_menu.addSeparator()

        self.act_3d = self.dock_3d.toggleViewAction()
        self.act_3d.setText("Show &3D View")
        self.act_3d.setShortcut(QKeySequence("Ctrl+3"))
        self.act_3d.setToolTip("Open the 3D board view (Ctrl+3). Closed by default: it is the "
                              "most expensive thing in the window to keep up to date.")
        view_menu.addAction(self.act_3d)
        act_reset_3d = view_menu.addAction("Reset 3D &Camera")
        act_reset_3d.triggered.connect(self.on_reset_3d_camera)

        help_menu = menu.addMenu("&Help")
        act_about = help_menu.addAction("&About PerfStudio")
        act_about.triggered.connect(self.on_about)

    def _build_toolbar(self) -> None:
        """The half-dozen actions used constantly, where they can be reached without a menu.

        Text-only buttons: shipping an icon set is a separate piece of work, and unlabelled
        guesses at icons would be worse than words for actions as specific as "flip to the
        solder side".
        """
        bar = QToolBar("Main")
        bar.setMovable(False)
        self.addToolBar(bar)

        bar.addAction(self.act_autoplace)
        bar.addAction(self.act_autoroute)
        bar.addAction(self.act_route_selected)
        bar.addSeparator()
        rotate = bar.addAction("Rotate")
        rotate.setToolTip("Rotate the selected part(s) 90° clockwise (R; Shift+R for the other way)")
        rotate.triggered.connect(lambda: self.on_rotate_selection(90))
        mirror = bar.addAction("Mirror")
        mirror.setToolTip("Mirror the selected part(s) (M)")
        mirror.triggered.connect(self.on_mirror_selection)
        bar.addSeparator()
        flip = bar.addAction("Flip Side")
        flip.setToolTip("Switch between the component side and the mirrored solder side (Ctrl+F)")
        flip.triggered.connect(self.on_flip_board)
        bar.addAction(self.act_ratsnest)
        bar.addAction(self.act_3d)
        bar.addSeparator()
        fit = bar.addAction("Fit")
        fit.triggered.connect(self.view.fit_board)
        bar.addAction("Zoom +").triggered.connect(lambda: self.view.zoom_by(1.25))
        bar.addAction("Zoom −").triggered.connect(lambda: self.view.zoom_by(1 / 1.25))

    def _build_library_dock(self) -> None:
        """The footprint registry, with a filter box, arming placement on selection.

        Sixty-one footprints and ``component.place`` have both existed since the first commit
        with nothing in the window able to reach either -- so a part could not be added to a
        board at all. Grouped by archetype because that is how someone looks for a part ("I
        need an electrolytic"), and filtered by text because twenty-eight of the sixty-one are
        pin headers and scrolling past them is nobody's idea of a library.
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self.library_filter = QLineEdit()
        self.library_filter.setPlaceholderText("Filter parts…  (resistor, dip, 5mm)")
        self.library_filter.setClearButtonEnabled(True)
        self.library_filter.textChanged.connect(self._refresh_library)
        layout.addWidget(self.library_filter)

        self.library_tree = QTreeWidget()
        self.library_tree.setHeaderLabels(["Part", "Pins"])
        self.library_tree.setRootIsDecorated(True)
        header = self.library_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.library_tree.itemSelectionChanged.connect(self._on_library_selection_changed)
        layout.addWidget(self.library_tree)

        self.label_place_hint = QLabel("Pick a part, then click the board. Esc cancels.")
        self.label_place_hint.setWordWrap(True)
        self.label_place_hint.setStyleSheet(f"color: {TEXT_DIM};")
        layout.addWidget(self.label_place_hint)

        dock = QDockWidget("Parts", self)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.dock_library = dock
        self._refresh_library()

    def _refresh_library(self) -> None:
        needle = self.library_filter.text().strip().lower()
        tree = self.library_tree
        tree.blockSignals(True)
        tree.clear()
        by_archetype: dict[str, list[Footprint]] = {}
        for footprint in sorted(standard_footprints().values(), key=lambda f: f.name):
            haystack = f"{footprint.id} {footprint.name} {footprint.body.archetype}".lower()
            if needle and needle not in haystack:
                continue
            by_archetype.setdefault(footprint.body.archetype, []).append(footprint)

        for archetype in sorted(by_archetype):
            group = QTreeWidgetItem([archetype.replace("-", " "), ""])
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            tree.addTopLevelItem(group)
            for footprint in by_archetype[archetype]:
                leaf = QTreeWidgetItem([footprint.name, str(len(footprint.pins))])
                leaf.setData(0, ROLE_FOOTPRINT_ID, footprint.id)
                leaf.setToolTip(0, f"{footprint.id} — {len(footprint.pins)} pin(s)")
                group.addChild(leaf)
            # Expanded only when the filter has narrowed things down, otherwise the twenty-eight
            # pin headers bury everything else.
            group.setExpanded(bool(needle) or len(by_archetype[archetype]) <= 4)
        tree.blockSignals(False)

    def _on_library_selection_changed(self) -> None:
        items = self.library_tree.selectedItems()
        footprint_id = items[0].data(0, ROLE_FOOTPRINT_ID) if items else None
        self.scene.arm_placement(footprint_id)

    def _on_placement_armed(self, footprint_id: str) -> None:
        if not footprint_id:
            self.label_place_hint.setText("Pick a part, then click the board. Esc cancels.")
            self.view.viewport().unsetCursor()
            self.library_tree.clearSelection()
            return
        footprint = self.lookup(footprint_id)
        name = footprint.name if footprint is not None else footprint_id
        ref = next_reference(self.bus.document, footprint_id)
        self.label_place_hint.setText(f"Click a hole to place <b>{ref}</b> ({name}). Esc cancels.")
        self.view.viewport().setCursor(Qt.CursorShape.CrossCursor)

    def _on_component_placed(self, result: DispatchResult) -> None:
        if result.ok:
            # Placement stays armed: a board needs several parts, and re-picking from the list
            # between every one of them would be a needless round trip.
            #
            # An overlap is reported rather than prevented: the bus allows two pins in one hole
            # because it is a legal document, and DRC is what objects. Saying only "placed" for
            # something the ghost had just drawn in red would read as approval.
            note = (
                "  ·  it overlaps an existing pin — see DRC"
                if self.scene.last_placement_overlapped
                else ""
            )
            self.statusBar().showMessage(f"{result.description}{note} — Esc to stop placing.", 6000)
            self._on_placement_armed(self.scene.armed_footprint_id or "")
        else:
            self.statusBar().showMessage(f"Cannot place there: {result.message}", 6000)

    def _build_nets_dock(self) -> None:
        """The netlist, with what each net still needs.

        This is the board's to-do list, and it did not exist before: the only way to find
        out whether a net was finished was to read an LVS message. "Left" is the ratsnest's
        own count, so it reaches zero exactly when the net is closed.
        """
        self.nets_tree = QTreeWidget()
        self.nets_tree.setHeaderLabels(["Net", "Class", "Pins", "Left"])
        self.nets_tree.setAlternatingRowColors(True)
        self.nets_tree.setRootIsDecorated(False)
        # The net name absorbs the spare width and the three narrow columns keep their
        # content. Fixed widths pushed "Left" off the edge of the dock -- which is the one
        # column the panel exists for, so it must never be the one that gets clipped.
        header = self.nets_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.nets_tree.itemSelectionChanged.connect(self._on_net_selection_changed)
        self.nets_tree.itemDoubleClicked.connect(self._on_net_double_clicked)
        dock = QDockWidget("Nets", self)
        dock.setWidget(self.nets_tree)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.resizeDocks([dock], [300], Qt.Orientation.Horizontal)

    def _build_drc_dock(self) -> None:
        self.drc_tree = QTreeWidget()
        self.drc_tree.setHeaderLabels(["Rule / Kind", "Message"])
        self.drc_tree.setColumnWidth(0, 260)
        self.drc_tree.itemClicked.connect(self._on_drc_item_clicked)
        dock = QDockWidget("DRC / LVS", self)
        dock.setWidget(self.drc_tree)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

    def _build_status_bar(self) -> None:
        """Separate labelled fields rather than one long sentence.

        The previous single string concatenated the last action, DRC counts, timing and LVS
        totals; at that length nothing in it could be found at a glance, and the last action
        pushed the numbers around every time it changed length. Now the counts hold still in
        their own fields on the right, and only the transient message moves.
        """
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.label_selection = QLabel()
        self.label_drc = QLabel()
        self.label_lvs = QLabel()
        self.label_ratsnest = QLabel()
        self.label_side = QLabel()
        self.label_hole = QLabel()
        # All permanent, including the selection. QStatusBar.showMessage HIDES ordinary
        # widgets for as long as a message is up -- so a selection label added the obvious
        # way vanished at precisely the wrong moment, since acting on a part is exactly what
        # puts a message there. Permanent widgets are laid out left to right in the order
        # they are added, so the selection still comes first.
        for label in (
            self.label_selection,
            self.label_hole,
            self.label_ratsnest,
            self.label_lvs,
            self.label_drc,
            self.label_side,
        ):
            bar.addPermanentWidget(label)

    # -- the one repaint path: every successful command, undo and redo funnels here --

    def on_bus_changed(self, document: PerfDocument, entry: HistoryEntry | None) -> None:
        self._track_moved_nets(document, entry)
        self.scene.set_document(document)

        t0 = time.perf_counter()
        violations = run_drc(document, self.lookup)
        self._last_drc_ms = (time.perf_counter() - t0) * 1000
        self._last_violations = tuple(violations)
        self.scene.set_violations(violations)

        self._last_lvs = run_lvs(document, self.lookup)
        self._last_ratsnest = ratsnest(document, self.lookup)
        self._refresh_drc_panel(self._last_violations, self._last_lvs)
        self._refresh_nets_panel(self._last_ratsnest)
        self._refresh_status()
        # After the rebuild, so it reads the fresh document: a rotate has to show its new
        # angle, and a deleted part has to stop being described as selected.
        self._refresh_selection_state()
        self._refresh_3d()

        self._refresh_title()
        if entry is not None:
            self.statusBar().showMessage(entry.description, 6000)

    #: Commands after which a net's existing copper describes a board that no longer
    #: exists. Not a guess: the bus says exactly which command ran.
    _MOVING_COMMANDS = frozenset(
        {"component.move", "component.moveMany", "component.rotate", "component.mirror"}
    )

    def _track_moved_nets(self, document: PerfDocument, entry: HistoryEntry | None) -> None:
        """Remember which nets carry routing laid out for an older position.

        This cannot be detected by looking at the board afterwards, and that is worth
        knowing rather than working around. Move a part and the copper that ran to its
        old pin holes still joins the right pins of the right net: it is not floating, not
        stale, and removing any of it disconnects something. It is simply shaped for a
        board that is no longer there, and autoroute -- which only ever adds -- will lay
        more copper beside it.

        So instead of inferring, this listens. The bus says which command ran; a move, a
        rotate or a mirror marks that component's nets, and re-routing them clears the
        mark. Undo is deliberately not special-cased: an undone move still leaves the
        question of whether those nets want re-routing open.
        """
        if entry is None or entry.record.type not in self._MOVING_COMMANDS:
            return
        payload = entry.record.payload
        ids = {p.id for p in getattr(payload, "placements", ())} or {getattr(payload, "id", None)}
        refs = {c.ref for c in document.components if c.id in ids}
        self._nets_from_old_layout |= {
            net.id
            for net in document.nets
            if any(node.component_ref in refs for node in net.nodes)
        }

    def _on_hovered_hole(self, col: int, row: int) -> None:
        """The address under the pointer, always visible.

        The whole tool speaks hole addresses -- every DRC message, every guide step, every
        MCP argument -- and there was no way to tell which one the pointer was on except
        by counting along a ruler.
        """
        board = self.bus.document.board
        inside = 0 <= col < board.cols and 0 <= row < board.rows
        text = format_hole(HoleCoord(col, row)) if inside else "—"
        self.label_hole.setText(f'<span style="color:{TEXT_DIM}">hole</span> <b>{text}</b>')

    def _refresh_status(self) -> None:
        errors = sum(1 for v in self._last_violations if v.severity == "error")
        warns = sum(1 for v in self._last_violations if v.severity == "warning")
        drc_colour = ERROR if errors else (WARNING if warns else OK)
        self.label_drc.setText(
            f'<span style="color:{drc_colour}">DRC {errors} err / {warns} warn</span>'
            f'  <span style="color:{TEXT_DIM}">{self._last_drc_ms:.1f} ms</span>'
        )

        if self._last_lvs is not None:
            s = self._last_lvs.summary
            lvs_colour = OK if s.opens == 0 and s.shorts == 0 else ERROR
            self.label_lvs.setText(
                f'<span style="color:{lvs_colour}">LVS {s.matched_nets}/{s.schematic_nets}'
                f" · {s.opens} open · {s.shorts} short</span>"
            )

        rn = summarize(self._last_ratsnest)
        if rn.nets:
            colour = OK if rn.links == 0 else TEXT_DIM
            self.label_ratsnest.setText(
                f'<span style="color:{colour}">{rn.links} to route'
                f" · {rn.total_length_mm:.0f} mm</span>"
            )
        else:
            self.label_ratsnest.setText(f'<span style="color:{TEXT_DIM}">no netlist</span>')

        side = "component side" if self.side == "top" else "solder side (mirrored)"
        self.label_side.setText(f'<span style="color:{TEXT_DIM}">{side}</span>')

    def on_move_committed(self, results: list[DispatchResult]) -> None:
        """Failures never emit from the bus (see command.py), so they are handled here
        rather than in ``on_bus_changed``: repaint from the (unchanged) document -- the
        only "snap back" this app has -- and surface *why* in the status bar instead of
        pretending the drag never happened.
        """
        failed = [r for r in results if not r.ok]
        if not failed:
            return
        self.scene.set_document(self.bus.document)
        message = "; ".join(f"Move refused: {r.message}" for r in failed)
        self.statusBar().showMessage(message)

    # -- DRC / LVS dock ----------------------------------------------------

    def _refresh_drc_panel(self, violations: tuple[DrcViolation, ...], lvs: LvsResult) -> None:
        tree = self.drc_tree
        tree.clear()

        drc_root = QTreeWidgetItem(["DRC", f"{len(violations)} violation(s)"])
        tree.addTopLevelItem(drc_root)
        by_rule: dict[str, list[DrcViolation]] = {}
        for v in violations:
            by_rule.setdefault(v.rule, []).append(v)
        for rule in sorted(by_rule):
            items = by_rule[rule]
            rule_item = QTreeWidgetItem([f"{rule} ({items[0].severity})", f"{len(items)}"])
            drc_root.addChild(rule_item)
            for v in items:
                leaf = QTreeWidgetItem(["", v.message])
                leaf.setData(0, ROLE_HOLES, v.holes)
                leaf.setData(0, ROLE_COMPONENT_IDS, v.component_ids)
                rule_item.addChild(leaf)
        drc_root.setExpanded(True)

        s = lvs.summary
        lvs_root = QTreeWidgetItem(
            [
                "LVS",
                f"{s.matched_nets}/{s.schematic_nets} matched, {s.opens} open, {s.shorts} short, "
                f"{s.physical_nets} physical nets",
            ]
        )
        tree.addTopLevelItem(lvs_root)
        by_kind: dict[str, list[LvsIssue]] = {}
        for iss in lvs.issues:
            by_kind.setdefault(iss.kind, []).append(iss)
        for kind in sorted(by_kind):
            kind_issues = by_kind[kind]
            kind_item = QTreeWidgetItem([kind, f"{len(kind_issues)}"])
            lvs_root.addChild(kind_item)
            for iss in kind_issues:
                leaf = QTreeWidgetItem(["", iss.message])
                issue_refs = {p.component_ref for p in iss.pins}
                issue_component_ids = tuple(c.id for c in self.bus.document.components if c.ref in issue_refs)
                leaf.setData(0, ROLE_COMPONENT_IDS, issue_component_ids)
                kind_item.addChild(leaf)
        lvs_root.setExpanded(True)

    def _on_drc_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        holes = item.data(0, ROLE_HOLES)
        component_ids = item.data(0, ROLE_COMPONENT_IDS)
        if holes:
            self.view.center_on_holes(holes, self.bus.document.board, self.side)
        if component_ids:
            self.scene.select_components(component_ids)

    # -- nets dock ----------------------------------------------------------

    def _refresh_nets_panel(self, nets: tuple[NetRatsnest, ...]) -> None:
        tree = self.nets_tree
        # Selection is restored by net id, not by row: the rows are rebuilt after every
        # command, and a user who selected GND to watch it get routed should still have GND
        # selected afterwards.
        previously = self._selected_net_ids()
        tree.blockSignals(True)
        tree.clear()
        for entry in nets:
            remaining = len(entry.links)
            item = QTreeWidgetItem(
                [
                    entry.net_name,
                    entry.net_class,
                    str(len(entry.pin_holes) + len(entry.unresolved_pins)),
                    "done" if remaining == 0 else str(remaining),
                ]
            )
            item.setData(0, ROLE_NET_ID, entry.net_id)
            colour = OK if remaining == 0 else (WARNING if entry.unresolved_pins else TEXT_DIM)
            item.setForeground(3, QColor(colour))
            if entry.unresolved_pins:
                pins = ", ".join(f"{p.component_ref}.{p.pin}" for p in entry.unresolved_pins)
                item.setToolTip(0, f"Not on the board: {pins}")
            tree.addTopLevelItem(item)
            if entry.net_id in previously:
                item.setSelected(True)
        tree.blockSignals(False)

    def _selected_net_ids(self) -> tuple[NetId, ...]:
        return tuple(
            item.data(0, ROLE_NET_ID)
            for item in self.nets_tree.selectedItems()
            if item.data(0, ROLE_NET_ID)
        )

    def _on_net_selection_changed(self) -> None:
        net_ids = self._selected_net_ids()
        self.scene.set_highlighted_nets(net_ids)
        if not net_ids:
            return
        holes = [
            hole
            for entry in self._last_ratsnest
            if entry.net_id in set(net_ids)
            for hole in entry.pin_holes
        ]
        if holes:
            self.view.reveal_holes(holes, self.bus.document.board, self.side)

    def _on_net_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        net_id = item.data(0, ROLE_NET_ID)
        if net_id:
            self._route(only_net_ids=(net_id,))

    def _on_selection_nets_changed(self, net_ids: list[str]) -> None:
        """Selecting a part on the board highlights its nets in the dock too, so the two
        views never disagree about what is selected."""
        wanted = set(net_ids)
        self.nets_tree.blockSignals(True)
        for index in range(self.nets_tree.topLevelItemCount()):
            item = self.nets_tree.topLevelItem(index)
            if item is not None:
                item.setSelected(item.data(0, ROLE_NET_ID) in wanted)
        self.nets_tree.blockSignals(False)
        self._refresh_selection_state()

    def _refresh_selection_state(self) -> None:
        """Describe the selection, and enable the actions that need one.

        Everything the selection says about a part was previously only in a tooltip: its
        address, orientation and whether it is locked. Those are exactly the values someone
        placing a part is trying to get right, so they belong on screen.
        """
        components = self._selected_components()
        for action in self.selection_actions:
            action.setEnabled(bool(components))

        if not components:
            self.label_selection.clear()
            return
        if len(components) == 1:
            c = components[0]
            footprint = self.lookup(c.footprint_id)
            bits = [f"<b>{c.ref}</b>"]
            if c.value:
                bits.append(c.value)
            bits.append(footprint.name if footprint is not None else f"?{c.footprint_id}")
            bits.append(format_hole(c.anchor))
            if c.rotation:
                bits.append(f"{c.rotation}°")
            if c.mirrored:
                bits.append("mirrored")
            if c.locked:
                bits.append("locked")
            self.label_selection.setText(" · ".join(bits))
            return
        locked = sum(1 for c in components if c.locked)
        note = f", {locked} locked" if locked else ""
        self.label_selection.setText(f"<b>{len(components)} parts</b> selected{note}")

    # -- placement ----------------------------------------------------------

    def on_autoplace(self, reroll: bool = False) -> None:
        """Anneal the placement, show what it found, and commit only if the user accepts.

        Asked rather than applied, unlike autoroute. Routing adds copper to a board the
        user arranged; this MOVES the board they arranged, which is not a thing to do to
        someone without showing them the result first and what it bought.

        ``reroll`` advances the seed. Annealing is a random walk and the outcome genuinely
        varies -- on the NE555 fixture the spread across seeds was 3 to 7 insulated wires
        for the same circuit -- so "try again" is a real answer and not a placebo.
        """
        if not self.bus.document.components:
            self.statusBar().showMessage("Nothing to place: the board is empty.", 6000)
            return
        if reroll:
            self._place_seed += 1

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            t0 = time.perf_counter()
            plan = plan_placement(
                self.bus.document, self.lookup, PlacementOptions(seed=self._place_seed)
            )
            elapsed = (time.perf_counter() - t0) * 1000
        finally:
            QApplication.restoreOverrideCursor()

        if plan.is_empty:
            self.statusBar().showMessage(
                f"{describe_placement(plan)} ({elapsed:.0f} ms). "
                "Place > Try Another Arrangement searches again from a different seed.",
                8000,
            )
            return

        if not self._confirm_placement(plan, elapsed):
            return

        result = self.bus.dispatch("component.moveMany", plan.payload())
        if not result.ok:
            QMessageBox.warning(self, "Placement refused", f"[{result.code}] {result.message}")
            return

        stale = len(stale_conductor_ids(self.bus.document, self.lookup))
        note = f"  ·  {stale} conductor(s) are now stale; Ctrl+R clears and re-routes" if stale else ""
        self.statusBar().showMessage(f"{describe_placement(plan)}{note}", 0)

    def _confirm_placement(self, plan: PlacementPlan, elapsed_ms: float) -> bool:
        detail = [
            f"{len(plan.changes)} of {plan.movable} movable part(s) move"
            + (f", {plan.locked} locked part(s) stay put." if plan.locked else "."),
            f"Estimated connection length: {plan.before.hpwl_mm:.0f} mm → "
            f"{plan.after.hpwl_mm:.0f} mm.",
        ]
        if plan.route_cost is not None:
            detail.append(
                f"Best of {plan.iterations}-move searches, judged by routing each one; "
                f"router cost {plan.route_cost:.0f} (seed {plan.seed})."
            )
        if plan.before.overlap_pairs:
            detail.append(
                f"Overlapping bodies: {plan.before.overlap_pairs} → {plan.after.overlap_pairs}."
            )
        if plan.before.collisions:
            detail.append(f"Pins sharing a hole: {plan.before.collisions} → {plan.after.collisions}.")
        detail.append("")
        detail.extend(summarize_placement(plan, limit=10))

        box = QMessageBox(self)
        box.setWindowTitle("Apply this placement?")
        box.setText(f"<b>{describe_placement(plan)}</b>  <span>({elapsed_ms:.0f} ms)</span>")
        box.setInformativeText("\n".join(detail))
        box.setStandardButtons(QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Apply)
        return box.exec() == QMessageBox.StandardButton.Apply

    # -- routing ------------------------------------------------------------

    def on_autoroute_all(self) -> None:
        self._route(only_net_ids=None)

    def on_route_selection(self) -> None:
        """Route the nets of whatever is selected -- in the dock, or on the board."""
        net_ids = self._selected_net_ids()
        if not net_ids:
            selected_refs = {
                item.comp.ref
                for item in self.scene.component_items.values()
                if item.isSelected()
            }
            net_ids = tuple(
                net.id
                for net in self.bus.document.nets
                if any(node.component_ref in selected_refs for node in net.nodes)
            )
        if not net_ids:
            self.statusBar().showMessage(
                "Select a net in the Nets panel, or a part on the board, then route.", 6000
            )
            return
        self._route(only_net_ids=net_ids)

    def on_reroute_selection(self) -> None:
        net_ids = self._selected_net_ids()
        if not net_ids:
            selected_refs = {
                item.comp.ref for item in self.scene.component_items.values() if item.isSelected()
            }
            net_ids = tuple(
                net.id
                for net in self.bus.document.nets
                if any(node.component_ref in selected_refs for node in net.nodes)
            )
        if not net_ids:
            self.statusBar().showMessage(
                "Select a net in the Nets panel, or a part on the board, then re-route.", 6000
            )
            return
        self.on_reroute(net_ids)

    def on_reroute(self, only_net_ids: tuple[NetId, ...] | None) -> None:
        """Rip up and route again, as one undoable command.

        Asked before it happens, because it DISCARDS routing -- which autoroute never
        does. The dialog says how much copper goes and what replaces it, since "14
        conductors become 12" is the only honest way to describe throwing away work.
        """
        if not self.bus.document.nets:
            self.statusBar().showMessage("No netlist imported, so there is nothing to route.", 6000)
            return

        t0 = time.perf_counter()
        plan = plan_reroute(self.bus.document, self.lookup, only_net_ids=only_net_ids)
        elapsed = (time.perf_counter() - t0) * 1000

        if plan.is_empty:
            self.statusBar().showMessage(f"Nothing to re-route ({elapsed:.0f} ms)", 6000)
            return

        if plan.remove_ids:
            answer = QMessageBox.question(
                self,
                "Re-route?",
                f"<b>{describe_reroute(plan)}</b>"
                f"<p>{len(plan.remove_ids)} existing conductor(s) will be removed and "
                f"{len(plan.conductors)} planned in their place. Copper with no net "
                f"assigned is left alone.</p>"
                f"<p>One Ctrl+Z puts it all back.</p>",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        result = self.bus.dispatch("conductor.replace", plan.payload())
        if not result.ok:
            QMessageBox.warning(self, "Re-route refused", f"[{result.code}] {result.message}")
            return

        rerouted = set(only_net_ids) if only_net_ids else {n.id for n in self.bus.document.nets}
        self._nets_from_old_layout -= rerouted
        self.statusBar().showMessage(f"{describe_reroute(plan)}  ({elapsed:.0f} ms)", 0)
        self._report_unrouted_items(
            [item for outcome in plan.nets for item in outcome.unrouted]
        )

    def _clear_strays(self, quiet: bool = False) -> int:
        """Delete conductors that are attached to nothing, as one undo step.

        This is what a moved part leaves behind: the trace that ran to its old pin hole is still
        on the board, now joined to nothing, and autoroute adds a fresh one to where the pin went
        instead -- so the old copper simply accumulates. A floating conductor connects nothing by
        definition, so removing it loses no circuit; it is still the user's copper, which is why
        it goes through a named, undoable command rather than a silent cleanup.
        """
        strays = stale_conductor_ids(self.bus.document, self.lookup)
        if not strays:
            if not quiet:
                self.statusBar().showMessage(
                    "No stale conductors: every one still connects the net it claims.", 6000
                )
            return 0
        label = f"Remove {len(strays)} stale conductor(s)"
        result = self.bus.dispatch(
            "conductor.deleteMany", DeleteConductorsPayload(ids=strays, label=label)
        )
        if not result.ok:
            self.statusBar().showMessage(f"[{result.code}] {result.message}", 8000)
            return 0
        if not quiet:
            self.statusBar().showMessage(f"{label} — Ctrl+Z puts them back.", 6000)
        return len(strays)

    def on_clear_strays(self) -> None:
        self._clear_strays()

    def _route(self, only_net_ids: tuple[NetId, ...] | None) -> None:
        """Plan, then commit as one command.

        The plan is computed against the bus's current document and dispatched as a single
        ``conductor.addMany``, so a whole autoroute is one undo step -- and, like every other
        mutation in this application, it reaches the document only through the command bus.
        """
        if not self.bus.document.nets:
            self.statusBar().showMessage("No netlist imported, so there is nothing to route.", 6000)
            return

        # Autoroute ADDS. On a net whose parts have moved since it was routed that is the
        # wrong answer and produces a board that grows every time -- so the question is
        # put to the user rather than either behaviour being assumed. See
        # _track_moved_nets and autoroute.ReroutePlan.
        stale_nets = self._nets_from_old_layout & {
            net.id
            for net in self.bus.document.nets
            if any(c.net_id == net.id for c in self.bus.document.conductors)
        }
        if stale_nets and only_net_ids is None:
            names = sorted(n.name for n in self.bus.document.nets if n.id in stale_nets)
            answer = QMessageBox.question(
                self,
                "Re-route the nets whose parts moved?",
                f"<b>{', '.join(names)}</b> still carry the copper laid out before a part "
                f"moved.<p>Autoroute only adds, so routing now leaves that copper in place "
                f"and puts more beside it. Re-routing them rips it up and plans again.</p>",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.on_reroute(tuple(stale_nets))
                return

        # Cleared BEFORE planning, so the plan is made against the board as it will be rather
        # than around copper that is about to go. Its own undo entry, named, immediately before
        # the routing one.
        cleared = self._clear_strays(quiet=True)

        t0 = time.perf_counter()
        plan = plan_autoroute(self.bus.document, self.lookup, only_net_ids=only_net_ids)
        elapsed = (time.perf_counter() - t0) * 1000
        cleared_note = f"  ·  {cleared} stale conductor(s) removed first" if cleared else ""

        if plan.is_empty:
            self.statusBar().showMessage(
                f"Nothing to route: {describe_plan(plan)}{cleared_note} ({elapsed:.0f} ms)", 8000
            )
            return

        result = self.bus.dispatch("conductor.addMany", plan.payload())
        if not result.ok:
            QMessageBox.warning(
                self, "Routing refused", f"[{result.code}] {result.message}"
            )
            return

        # Said after the commit, and said in full: the failures are the part a user must not
        # miss (PLAN.md Sec 13), so they go in the status line and, if there are any, into a
        # dialog that names each one.
        self.statusBar().showMessage(f"{describe_plan(plan)}{cleared_note}  ({elapsed:.0f} ms)", 0)
        self._report_unrouted(plan)

    def _report_unrouted(self, plan: AutoroutePlan) -> None:
        self._report_unrouted_items([item for outcome in plan.nets for item in outcome.unrouted])

    def _report_unrouted_items(self, failures: list[UnroutedLink]) -> None:
        if not failures:
            return
        lines = [
            f"{item.link.net_name}: {format_hole(item.link.from_)} → "
            f"{format_hole(item.link.to)}\n    {item.reason}"
            for item in failures[:12]
        ]
        if len(failures) > 12:
            lines.append(f"... and {len(failures) - 12} more.")
        QMessageBox.information(
            self,
            "Some connections could not be routed",
            f"{len(failures)} connection(s) were left unrouted:\n\n" + "\n".join(lines),
        )

    # -- edit --------------------------------------------------------------

    def on_undo(self) -> None:
        self.bus.undo()

    def on_redo(self) -> None:
        self.bus.redo()

    # -- editing the selection ----------------------------------------------
    #
    # All three go through the bus, one command per selected part, exactly as a drag does.
    # Nothing here touches the document, so undo, the journal and an agent driving the same
    # board all keep working without any of these knowing about them.

    def _selected_components(self) -> list[ComponentInstance]:
        selected = set(self.scene.selected_component_ids())
        return [c for c in self.bus.document.components if c.id in selected]

    def _require_selection(self, what: str) -> list[ComponentInstance]:
        components = self._selected_components()
        if not components:
            self.statusBar().showMessage(f"Select a part on the board first, then {what}.", 6000)
        return components

    def _report_refusals(self, results: list[DispatchResult], done: str) -> None:
        """Say what happened, and say what was refused and why.

        A locked part silently ignoring a keypress is indistinguishable from a broken
        shortcut, so a refusal has to reach the status bar rather than being dropped.
        """
        refused = [r for r in results if not r.ok]
        if refused:
            reasons = "; ".join(dict.fromkeys(r.message for r in refused))
            self.statusBar().showMessage(f"{len(refused)} refused: {reasons}", 8000)
        elif results:
            self.statusBar().showMessage(done, 5000)

    def on_rotate_selection(self, delta: int) -> None:
        components = self._require_selection("rotate")
        if not components:
            return
        results = [
            self.bus.dispatch(
                "component.rotate",
                RotateComponentPayload(id=c.id, rotation=_rotation_after(c.rotation, delta)),
            )
            for c in components
        ]
        self._report_refusals(results, f"Rotated {len(results)} part(s)")

    def on_mirror_selection(self) -> None:
        components = self._require_selection("mirror")
        if not components:
            return
        results = [
            self.bus.dispatch("component.mirror", MirrorComponentPayload(id=c.id, mirrored=not c.mirrored))
            for c in components
        ]
        self._report_refusals(results, f"Mirrored {len(results)} part(s)")

    def on_draw_mode(self, kind: str, checked: bool) -> None:
        """Arm or disarm a drawing tool. Only one may be armed at a time."""
        wanted = kind if checked and kind else ""
        for name, action in self.act_draw.items():
            action.setChecked(name == wanted)
        self.scene.arm_drawing(cast(Any, wanted) if wanted else None)
        if wanted:
            two_point = wanted in ("bare-wire", "insulated-wire", "top-jumper")
            how = (
                "Click both ends."
                if two_point
                else "Click each pad along the run, then Enter or right-click to finish."
            )
            self.statusBar().showMessage(
                f"Drawing {wanted.replace('-', ' ')} — {how}  Esc cancels.", 0
            )
        else:
            self.statusBar().clearMessage()

    def _on_draw_armed(self, kind: str) -> None:
        """Keep the menu in step when the scene ends drawing by itself (Esc, or commit)."""
        for name, action in self.act_draw.items():
            action.setChecked(name == kind)

    def _on_conductor_drawn(self, result: Any) -> None:
        if result is not None and not result.ok:
            self.statusBar().showMessage(f"[{result.code}] {result.message}", 8000)

    def on_delete_selection(self) -> None:
        """Delete whatever is selected -- parts, conductors, or both.

        Conductors became selectable for this: before it, a single bad route could only be
        removed by undoing the whole autoroute or re-routing the entire board.
        """
        conductor_ids = self.scene.selected_conductor_ids()
        components = [
            item.comp
            for item in self.scene.component_items.values()
            if item.isSelected()
        ]
        if conductor_ids and not components:
            label = f"Delete {len(conductor_ids)} conductor(s)"
            if (
                QMessageBox.question(self, "Delete conductors", f"{label}?")
                != QMessageBox.StandardButton.Yes
            ):
                return
            result = self.bus.dispatch(
                "conductor.deleteMany",
                DeleteConductorsPayload(ids=conductor_ids, label=label),
            )
            if not result.ok:
                self.statusBar().showMessage(f"[{result.code}] {result.message}", 8000)
            return

        components = self._require_selection("delete it")
        if not components:
            return
        refs = ", ".join(c.ref for c in components)
        if (
            QMessageBox.question(
                self,
                "Delete parts",
                f"Delete {refs}?\n\nWires and traces are left in place -- DRC and LVS will "
                "point at anything left dangling.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        results = [
            self.bus.dispatch("component.delete", DeleteComponentPayload(id=c.id))
            for c in components
        ]
        self._report_refusals(results, f"Deleted {len(results)} part(s)")

    def on_toggle_lock_selection(self) -> None:
        components = self._require_selection("lock or unlock it")
        if not components:
            return
        # One shared target state, from the first part: toggling each independently would
        # scatter a mixed selection instead of doing what "lock these" plainly means.
        locking = not components[0].locked
        results = [
            self.bus.dispatch("component.update", UpdateComponentPayload(id=c.id, locked=locking))
            for c in components
        ]
        self._report_refusals(results, f"{'Locked' if locking else 'Unlocked'} {len(results)} part(s)")

    def on_flip_board(self) -> None:
        self.side = "bottom" if self.side == "top" else "top"
        self.scene.set_side(self.side)
        # set_side rebuilds the scene, which drops the highlight with everything else.
        self.scene.set_highlighted_nets(self._selected_net_ids())
        self._refresh_3d()
        self._refresh_status()

    # -- file ----------------------------------------------------------------

    def on_new(self) -> None:
        """Start a blank board, after asking about anything unsaved."""
        if not self._offer_to_save():
            return
        dialog = BoardSetupDialog(DEFAULT_BOARD, self, title="New Board")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        document = create_empty_document(
            DocumentMeta(name="untitled", created=_now_iso(), modified=_now_iso()),
            dialog.board(),
        )
        self.current_path = None
        self.bus = self._new_bus(document)
        self._subscribe_bus()
        self.scene.bus = self.bus
        self._nets_from_old_layout.clear()
        self.on_bus_changed(self.bus.document, None)
        self._mark_saved()
        self.view.fit_board()
        self.statusBar().showMessage(
            f"New {document.board.cols}×{document.board.rows} {document.board.material} board", 8000
        )

    def on_board_setup(self) -> None:
        """Change the grid or the substrate of the board already open.

        Through ``board.set`` like everything else, so shrinking a board that still has a
        part hanging off the new edge comes back as a refusal naming the part, rather
        than silently stranding it.
        """
        dialog = BoardSetupDialog(self.bus.document.board, self, title="Board Setup")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        board = dialog.board()
        if board == self.bus.document.board:
            return
        result = self.bus.dispatch("board.set", SetBoardPayload(board=board))
        if not result.ok:
            QMessageBox.warning(
                self,
                "Board not changed",
                f"[{result.code}] {result.message}\n\nMove or delete whatever is in the way "
                "and try again.",
            )
            return
        self.view.fit_board()

    def on_open(self) -> None:
        if not self._offer_to_save():
            return
        start_dir = str(self.current_path.parent) if self.current_path else str(Path.cwd())
        path_str, _ = QFileDialog.getOpenFileName(self, "Open .perf", start_dir, "PerfStudio documents (*.perf)")
        if not path_str:
            return
        self._load_path(Path(path_str))

    def _load_path(self, path: Path) -> None:
        # The file dialog only offers files that exist, but it does not guarantee one is
        # still there, still readable, or actually text by the time it is opened -- and an
        # unhandled read error here would take the whole window down over a bad pick.
        text, problem = read_document_text(path)
        if text is None:
            QMessageBox.critical(self, "Open failed", problem or "Could not read the file.")
            return
        result = persist.deserialize_document(text)
        if not result.ok:
            location = f" (at {result.path})" if result.path else ""
            QMessageBox.critical(self, "Open failed", f"[{result.code}] {result.message}{location}")
            return
        self.current_path = path
        self.bus = self._new_bus(result.document)
        self._subscribe_bus()
        self.scene.bus = self.bus
        self.on_bus_changed(self.bus.document, None)
        self.view.fit_board()
        note = f" ({len(result.warnings)} warning(s))" if result.warnings else ""
        self.statusBar().showMessage(f"Loaded {path.name}{note}", 8000)
        self._mark_saved()

    # -- netlist import ------------------------------------------------------

    def on_import_netlist(self) -> None:
        """Bring in the schematic's intent, which is how connections get defined at all.

        PLAN.md D3 settles this deliberately: a netlist import plus visual editing, rather than
        a schematic editor. The parser and the ``netlist.import`` command have both existed from
        the start with nothing able to reach them, so until now nets could only arrive by
        hand-editing a .perf file.
        """
        start_dir = str(self.current_path.parent) if self.current_path else str(Path.cwd())
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Import KiCad netlist", start_dir, "KiCad netlists (*.net);;All files (*)"
        )
        if not path_str:
            return
        path = Path(path_str)
        text, problem = read_document_text(path)
        if text is None:
            QMessageBox.critical(self, "Import failed", problem or "Could not read the file.")
            return
        try:
            imported = parse_kicad_netlist(text)
        except ValueError as err:
            QMessageBox.critical(self, "Import failed", f"{path.name}: {err}")
            return

        result = self.bus.dispatch("netlist.import", ImportNetlistPayload(nets=imported.nets))
        if not result.ok:
            QMessageBox.critical(self, "Import failed", f"[{result.code}] {result.message}")
            return

        note = f"Imported {len(imported.nets)} net(s) from {path.name}"
        if imported.warnings:
            shown = "\n".join(f"  • {w}" for w in imported.warnings[:12])
            more = f"\n  … and {len(imported.warnings) - 12} more" if len(imported.warnings) > 12 else ""
            QMessageBox.information(
                self, "Imported with warnings", f"{note}, with warnings:\n\n{shown}{more}"
            )
        self.statusBar().showMessage(note, 8000)
        self._offer_to_place_missing_parts()

    def _offer_to_place_missing_parts(self) -> None:
        """Offer a first-pass placement for the parts the netlist names but the board lacks.

        A netlist's own footprint strings are KiCad library names, which say nothing about
        PerfStudio's registry -- so the footprint is inferred from the reference letter and the
        pin count the netlist itself reveals. That guess is often right and never trusted: the
        parts land in a plain grid for the user to drag, rotate and lock, and every one of them
        goes through ``component.place`` so the whole lot undoes in one step.
        """
        document = self.bus.document
        placed = {c.ref for c in document.components}
        wanted: dict[str, set[str]] = {}
        for net in document.nets:
            for node in net.nodes:
                if node.component_ref not in placed:
                    wanted.setdefault(node.component_ref, set()).add(node.pin)
        if not wanted:
            return

        answer = QMessageBox.question(
            self,
            "Place the missing parts?",
            f"The netlist names {len(wanted)} part(s) that are not on the board yet:\n"
            f"  {', '.join(sorted(wanted)[:14])}"
            f"{'…' if len(wanted) > 14 else ''}\n\n"
            "Place them in a grid to drag into position? The footprint is guessed from each "
            "reference and its pin count, so check and change what it got wrong.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._place_parts_in_grid(wanted)

    def _place_parts_in_grid(self, wanted: dict[str, set[str]]) -> None:
        board = self.bus.document.board
        specs: list[PlaceComponentPayload] = []
        col, row, row_height = 1, 1, 0
        for ref in sorted(wanted):
            footprint_id = guess_footprint_id(ref, len(wanted[ref]))
            footprint = self.lookup(footprint_id)
            if footprint is None:
                continue
            width = max((p.d_col for p in footprint.pins), default=0) + 2
            height = max((p.d_row for p in footprint.pins), default=0) + 2
            if col + width >= board.cols:
                col, row = 1, row + row_height + 1
                row_height = 0
            if row + height >= board.rows:
                break  # Out of board; the rest stay unplaced and LVS will say so.
            specs.append(
                PlaceComponentPayload(
                    ref=ref, value="", footprint_id=footprint_id, anchor=HoleCoord(col, row)
                )
            )
            col += width
            row_height = max(row_height, height)

        results = [self.bus.dispatch("component.place", spec) for spec in specs]
        ok = sum(1 for r in results if r.ok)
        skipped = len(wanted) - ok
        message = f"Placed {ok} part(s)"
        if skipped:
            message += f"; {skipped} could not be placed and will show in LVS as unplaced"
        self.statusBar().showMessage(message, 10000)

    def on_save(self) -> bool:
        """Save, returning whether it happened. The bool is what the close guard needs."""
        if self.current_path is None:
            return self.on_save_as()
        self._save_to(self.current_path)
        return True

    def on_save_as(self) -> bool:
        default = str(self.current_path) if self.current_path else str(Path.cwd() / "board.perf")
        path_str, _ = QFileDialog.getSaveFileName(self, "Save As", default, "PerfStudio documents (*.perf)")
        if not path_str:
            return False
        path = Path(path_str)
        if path.suffix != ".perf":
            path = path.with_suffix(".perf")
        self.current_path = path
        self._save_to(path)
        return True

    # -- unsaved work --------------------------------------------------------

    @property
    def is_modified(self) -> bool:
        """Whether the board differs from what is on disk.

        Compared by IDENTITY, not by value, and that is exactly right here rather than a
        shortcut. Documents are immutable and every command builds a new one, so the
        saved document is a distinct object the moment anything is dispatched -- and
        undoing back to it restores that very object (``HistoryEntry.before`` is the same
        instance), so undoing your way back to the saved state correctly reads as
        unmodified again. A value comparison would give the same answer far more slowly,
        on every keystroke.
        """
        return self.bus.document is not self._saved_document

    def _mark_saved(self) -> None:
        self._saved_document = self.bus.document
        self._refresh_title()

    def _refresh_title(self) -> None:
        self.setWindowTitle(window_title(self.current_path, modified=self.is_modified))

    def _offer_to_save(self) -> bool:
        """Ask about unsaved work. False means the user cancelled the whole operation.

        Three buttons rather than two, because "are you sure?" with only yes and no makes
        the destructive answer the fast one. Discard is deliberately not the default.
        """
        if not self.is_modified:
            return True
        name = self.current_path.name if self.current_path else "this board"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"<b>{name} has changes that are not saved.</b>")
        box.setInformativeText("Saving keeps them; discarding loses them for good.")
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        answer = box.exec()
        if answer == QMessageBox.StandardButton.Save:
            return self.on_save()
        return answer == QMessageBox.StandardButton.Discard

    def closeEvent(self, event: Any) -> None:
        """The last thing standing between an hour of layout and the X button."""
        if self._offer_to_save():
            event.accept()
        else:
            event.ignore()

    def _save_to(self, path: Path) -> None:
        # meta.modified is host-stamped, not part of any command (core has no clock --
        # see persist.py/commands.py) -- so this replaces the document's meta for the
        # SERIALIZED copy only, without pushing that change through the bus.
        doc = self.bus.document
        stamped = dataclasses.replace(doc, meta=dataclasses.replace(doc.meta, modified=_now_iso()))
        path.write_text(persist.serialize_document(stamped), encoding="utf-8")
        self._mark_saved()
        self.statusBar().showMessage(f"Saved {path}")

    def on_export_pdf(self) -> None:
        base = self.current_path.with_suffix("") if self.current_path else Path.cwd() / "board"
        doc = self.bus.document
        # Overlays off: the 1:1 sheet is a soldering template held against the real board,
        # and the ratsnest draws what is NOT built yet. Printing it would put dashed lines
        # across the very sheet someone is using to decide where to put solder.
        top_scene = self._export_scene(doc, "top")
        bottom_scene = self._export_scene(doc, "bottom")
        p1 = export_pdf(doc.board, top_scene, base.with_name(base.name + "_component_side.pdf"))
        p2 = export_pdf(doc.board, bottom_scene, base.with_name(base.name + "_solder_side.pdf"), mirrored=True)
        self.statusBar().showMessage(f"Exported {p1.name} and {p2.name}", 8000)

    def _export_scene(self, doc: PerfDocument, side: BoardSide) -> BoardScene:
        """A scene for print: the board and nothing else."""
        return BoardScene(doc, self.lookup, side=side, show_ratsnest=False, show_rulers=False)

    def on_export_3d_png(self) -> None:
        out = self.current_path.with_suffix(".png") if self.current_path else Path.cwd() / "board_3d.png"
        view3d.render_offscreen(self.bus.document, self.lookup, str(out), flipped=(self.side == "bottom"))
        self.statusBar().showMessage(f"Exported {out}")

    def on_export_guide(self) -> None:
        """Write the build guide beside the document, and say what it could not cover.

        Four files rather than one, because they get used in different places: the HTML
        on a phone at the bench, the CSVs in a spreadsheet or an order, the JSON by
        whatever comes next. The 1:1 PDF sheets are a separate export because they are a
        separate thing -- a template you hold against the board, not a document you read.
        """
        base = self.current_path.with_suffix("") if self.current_path else Path.cwd() / "board"
        guide = build_guide(self.bus.document, self.lookup)

        written: list[Path] = []
        try:
            for suffix, text in (
                ("_guide.html", guide_to_html(guide)),
                ("_cut_list.csv", cut_list_to_csv(guide)),
                ("_bom.csv", bom_to_csv(guide)),
                ("_guide.json", guide_to_json(guide)),
            ):
                path = base.with_name(base.name + suffix)
                path.write_text(text, encoding="utf-8")
                written.append(path)
        except OSError as err:
            QMessageBox.critical(self, "Export failed", f"Could not write the guide: {err}")
            return

        self.statusBar().showMessage(
            f"{describe_guide(guide)} — {written[0].name} and {len(written) - 1} more", 0
        )
        if guide.warnings:
            # Said in a dialog, not just the status bar: each of these is a statement that
            # the guide describes less than the whole build, and a user who misses it will
            # follow the steps to the end and find the board does not work.
            lines = "\n".join(f"  • {w.message}" for w in guide.warnings)
            QMessageBox.warning(
                self,
                "The guide has gaps",
                f"Written to {written[0].parent}, with {len(guide.warnings)} thing(s) it "
                f"could not cover:\n\n{lines}",
            )

    def on_about(self) -> None:
        """The version, in a form someone can copy into a bug report.

        Selectable text rather than a picture: the whole point of the line is that it can be
        pasted, and QMessageBox renders it unselectable unless asked.
        """
        box = QMessageBox(self)
        box.setWindowTitle("About PerfStudio")
        box.setText(f"<b>PerfStudio {__version__}</b>")
        box.setInformativeText(
            f"{describe_version()}\n\n"
            "Perfboard layout design, verification and a soldering guide.\n"
            "Apache-2.0 · github.com/medinstech/perfstudio"
        )
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()


# ---------------------------------------------------------------------------
# Headless entry point
# ---------------------------------------------------------------------------


def _default_headless_platform() -> str:
    """Which Qt platform plugin to render headlessly with.

    "offscreen" everywhere except Windows. On Windows that plugin ships no font database at
    all -- ``QFontInfo(QFont()).family()`` comes back empty -- so every label renders as a
    missing-glyph box while looking perfect in the GUI. Since Windows always has a window
    station available, the normal plugin renders into a QImage without ever showing a window
    and gets real text. An explicit QT_QPA_PLATFORM still wins over this.
    """
    return "windows" if sys.platform == "win32" else "offscreen"


def headless(argv: list[str]) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", _default_headless_platform())
    app = QApplication.instance() or QApplication(sys.argv[:1])
    lookup = footprint_lookup()

    positional = [a for a in argv if not a.startswith("--")]
    perf_path = Path(positional[0]) if positional else _find_repo_root() / "tools" / "diffcheck" / "golden" / "dense.perf"

    out_dir = Path.cwd() / "headless_out"
    out_dir.mkdir(exist_ok=True)

    print(describe_version())
    print(f"document     {perf_path}")
    if not perf_path.exists():
        print(f"LOAD FAILED  no such file: {perf_path}")
        return 1
    text = perf_path.read_text(encoding="utf-8")
    result = persist.deserialize_document(text)
    if not result.ok:
        print(f"LOAD FAILED  [{result.code}] {result.message} (path={result.path})")
        return 1
    doc = result.document
    if result.warnings:
        print(f"warnings     {len(result.warnings)}")
        for w in result.warnings:
            print(f"  - {w}")

    board = doc.board
    w_mm, h_mm = board_size_mm(board)
    sw, sh = hole_span_mm(board)
    print(f"board        {board.cols}x{board.rows} {board.material}")
    print(f"substrate    {w_mm:.2f} x {h_mm:.2f} mm   (hole span {sw:.2f} x {sh:.2f} mm)")
    print(f"parts        {len(doc.components)}   conductors {len(doc.conductors)}   nets {len(doc.nets)}")

    scene = BoardScene(doc, lookup, side="top")

    # --- 2D render ---
    # The source rect includes the ruler margin, unlike the print path below: this PNG is
    # a picture OF THE EDITOR, so it should show what the editor shows.
    px_per_mm = 12
    margin = RULER_MARGIN_MM
    src_w, src_h = w_mm + margin + 4, h_mm + margin + 4
    img_w, img_h = int(src_w * px_per_mm), int(src_h * px_per_mm)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32)
    image.fill(QColor("#12131a"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    source = QRectF(-board.pitch / 2 - margin, -board.pitch / 2 - margin, src_w, src_h)
    t0 = time.perf_counter()
    scene.render(painter, QRectF(0, 0, img_w, img_h), source)
    t_2d = (time.perf_counter() - t0) * 1000
    painter.end()
    out_2d = out_dir / "out_2d.png"
    image.save(str(out_2d))
    print(f"\n2D render    {t_2d:6.1f} ms   -> {out_2d}")
    if scene.pad_grid is not None:
        print(f"pads painted {scene.pad_grid.drawn} of {board.cols * board.rows} (Qt culls the rest)")

    # --- 1:1 scale verification and print sheets ---
    # Built WITHOUT the editor overlays: see MainWindow._export_scene for why the ratsnest
    # must not reach a sheet someone solders from.
    print_top = BoardScene(doc, lookup, side="top", show_ratsnest=False, show_rulers=False)
    print_bottom = BoardScene(doc, lookup, side="bottom", show_ratsnest=False, show_rulers=False)

    check = verify_scale(print_top, board)
    print(
        f"\n1:1 check    {check.span_holes} holes at {check.dpi:.0f} dpi: "
        f"expected {check.expected_px:.3f} px, measured {check.measured_px:.3f} px, "
        f"error {check.error_mm * 1000:.2f} um  -> {'PASS' if check.ok else 'FAIL'}"
    )

    pdf_component = export_pdf(board, print_top, out_dir / "board_1to1_component_side.pdf")
    pdf_solder = export_pdf(board, print_bottom, out_dir / "board_1to1_solder_side.pdf", mirrored=True)
    print(f"PDF          {pdf_component.stat().st_size / 1024:.0f} KB -> {pdf_component.name}")
    print(f"PDF mirrored {pdf_solder.stat().st_size / 1024:.0f} KB -> {pdf_solder.name}")

    # --- DRC / LVS, timed. This is the number that matters for "is DRC fast enough to
    # run after every drag": see the docstring on view2d.BoardScene.mouseReleaseEvent
    # and main.py's on_bus_changed -- DRC runs on drag RELEASE, once, not per frame.
    t0 = time.perf_counter()
    violations = run_drc(doc, lookup)
    t_drc = (time.perf_counter() - t0) * 1000
    errors = sum(1 for v in violations if v.severity == "error")
    warns = sum(1 for v in violations if v.severity == "warning")
    print(f"\nDRC          {t_drc:6.1f} ms   {errors} errors, {warns} warnings ({len(violations)} total)")

    t0 = time.perf_counter()
    lvs_result = run_lvs(doc, lookup)
    t_lvs = (time.perf_counter() - t0) * 1000
    s = lvs_result.summary
    print(
        f"LVS          {t_lvs:6.1f} ms   {s.matched_nets}/{s.schematic_nets} nets matched, "
        f"{s.opens} open, {s.shorts} short, {s.physical_nets} physical nets"
    )

    # --- Ratsnest and a dry-run autoroute ---
    # Reported but NOT committed: headless mode inspects a document, it does not edit one.
    # The point is to show what routing this board would cost, and to give CI a number that
    # moves when the router's quality changes.
    t0 = time.perf_counter()
    remaining = summarize(ratsnest(doc, lookup))
    t_rats = (time.perf_counter() - t0) * 1000
    print(
        f"ratsnest     {t_rats:6.1f} ms   {remaining.links} connection(s) left across "
        f"{remaining.nets - remaining.closed_nets} open net(s), {remaining.total_length_mm:.0f} mm total"
    )

    if doc.nets:
        t0 = time.perf_counter()
        plan = plan_autoroute(doc, lookup)
        t_plan = (time.perf_counter() - t0) * 1000
        print(f"autoroute    {t_plan:6.1f} ms   (dry run, nothing committed)")
        print(f"             {describe_plan(plan)}")
        after = run_lvs(plan.document, lookup).summary
        print(
            f"             would leave LVS at {after.matched_nets}/{after.schematic_nets} matched, "
            f"{after.opens} open, {after.shorts} short"
        )

    # --- Placement, also a dry run. The number CI should watch is the routing cost
    # before and after: it is the one that says whether the placer is still earning its
    # runtime, and it moves when either the placer or the router changes.
    if doc.components:
        t0 = time.perf_counter()
        placement = plan_placement(doc, lookup)
        t_place = (time.perf_counter() - t0) * 1000
        print(f"\nauto-place   {t_place:6.1f} ms   (dry run, nothing committed)")
        print(f"             {describe_placement(placement)}")
        placed_errors = sum(
            1 for v in run_drc(placement.document, lookup) if v.severity == "error"
        )
        print(
            f"             HPWL {placement.before.hpwl_mm:.0f} -> {placement.after.hpwl_mm:.0f} mm, "
            f"overlaps {placement.before.overlap_pairs} -> {placement.after.overlap_pairs}, "
            f"DRC errors {errors} -> {placed_errors}"
        )

    # --- The build guide, written out. This is the project's actual output, so a
    # headless run that renders the board and does not produce it is only testing half
    # the pipeline.
    guide = build_guide(doc, lookup)
    (out_dir / "guide.html").write_text(guide_to_html(guide), encoding="utf-8")
    (out_dir / "guide.json").write_text(guide_to_json(guide), encoding="utf-8")
    (out_dir / "cut_list.csv").write_text(cut_list_to_csv(guide), encoding="utf-8")
    (out_dir / "bom.csv").write_text(bom_to_csv(guide), encoding="utf-8")
    print(f"\nbuild guide  {describe_guide(guide)}")
    print(f"             {guide.part_steps} part step(s), {guide.conductor_steps} connection(s), "
          f"{len(guide.cut_list)} wire(s) -> guide.html")
    for warning in guide.warnings:
        print(f"  ! {warning.code}: {warning.message}")

    # --- 3D render, offscreen: the build-guide image path ---
    try:
        t0 = time.perf_counter()
        stats = view3d.render_offscreen(doc, lookup, str(out_dir / "out_3d.png"))
        t_3d = (time.perf_counter() - t0) * 1000
        print(f"\n3D offscreen {t_3d:6.1f} ms   -> out_3d.png")
        print(f"actors       {stats['actors']} total for {stats['pads']} pads (instanced)")
        view3d.render_offscreen(doc, lookup, str(out_dir / "out_3d_solder.png"), flipped=True)
        print("             out_3d_solder.png (flipped to the solder side)")
    except Exception as exc:
        print(f"\n3D FAILED: {exc}")
        return 1

    print(f"\noutputs written to {out_dir}")
    del app
    return 0 if check.ok else 1


def main() -> int:
    # Answered before Qt is touched: --version has to work on a machine where the GUI
    # cannot start, since "it will not launch" is exactly when someone is asked which
    # version they have.
    if "--version" in sys.argv or "-V" in sys.argv:
        print(describe_version())
        return 0

    if "--headless" in sys.argv:
        return headless([a for a in sys.argv[1:] if a != "--headless"])

    app = QApplication(sys.argv)
    argv_paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    path: Path | None
    if argv_paths:
        path = Path(argv_paths[0])
        # Read and parse failures are reported the same way, because to the person who
        # mistyped a path they are the same mistake.
        text, problem = read_document_text(path)
        if text is None:
            print(problem, file=sys.stderr)
            return 1
        result = persist.deserialize_document(text)
        if not result.ok:
            location = f" (at {result.path})" if result.path else ""
            print(f"Failed to load {path}: [{result.code}] {result.message}{location}", file=sys.stderr)
            return 1
        document = result.document
    else:
        path = None
        document = create_empty_document(DocumentMeta(name="untitled", created=_now_iso(), modified=_now_iso()))

    window = MainWindow(document, path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
