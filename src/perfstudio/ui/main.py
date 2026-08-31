"""PerfStudio desktop application: the real engine behind the prototype's window.

Promoted from ``prototypes/qt/main.py``. Everything the prototype only gestured at in
its status-bar comment -- "(engine would now re-run DRC and re-route the nets it
touches)" -- actually happens here: every mutation goes through a
``perfstudio.command.CommandBus`` (never a direct write to the document), and every
successful command re-runs ``run_drc``/``run_lvs`` and repaints from the bus's own
document.

    python -m perfstudio.ui.main                 launch the app (blank document)
    python -m perfstudio.ui.main path/to.perf     launch the app, opening a document
    python -m perfstudio.ui.main --lang tr        launch in Turkish (or PERFSTUDIO_LANG=tr)
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
import math
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from PySide6.QtCore import (
    QEventLoop,
    QFileSystemWatcher,
    QPoint,
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSlider,
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
    AutorouteOptions,
    AutoroutePlan,
    UnroutedLink,
    describe_best,
    describe_reroute,
    plan_autoroute,
    plan_best_autoroute,
    plan_reroute,
)
from perfstudio.autoroute import (
    describe as describe_plan,
)
from perfstudio.command import CommandBus, CommandContext, DispatchResult, HistoryEntry
from perfstudio.commands import (
    AddEdgeConnectorPayload,
    AddMountingHolesPayload,
    AddNetPayload,
    AddPartPayload,
    ApplyBoardPresetPayload,
    DeleteComponentPayload,
    DeleteConductorsPayload,
    DeleteEdgeConnectorPayload,
    DeleteMountingHolePayload,
    DeleteNetPayload,
    DeletePartPayload,
    DisconnectPinsPayload,
    ImportNetlistPayload,
    MirrorComponentPayload,
    PartPlacement,
    PlaceBlockPayload,
    PlaceComponentPayload,
    PlacePartsPayload,
    RotateComponentPayload,
    SetBoardPayload,
    SetHeightLimitPayload,
    UnplaceComponentPayload,
    UpdateComponentPayload,
    UpdateNetPayload,
    UpdatePartPayload,
    create_document_id_generator,
    create_empty_document,
    create_standard_registry,
    create_starter_document,
)
from perfstudio.connectivity import FootprintLookup
from perfstudio.drc import DrcViolation, run_drc
from perfstudio.footprints import (
    axial_footprint,
    box_film_capacitor_footprint,
    dip_footprint,
    disc_ceramic_footprint,
    footprint_lookup,
    generic_box_footprint,
    get_footprint,
    led_footprint,
    pin_header_footprint,
    radial_electrolytic_footprint,
    screw_terminal_footprint,
    standard_footprints,
)
from perfstudio.geometry import (
    STANDARD_PRESETS,
    BoardPreset,
    board_edge_margin_mm,
    board_from_preset,
    edge_connector_holes,
    format_hole,
    pad_edge_gap_mm,
    pad_extent_mm,
    preset_edge_connectors,
    preset_mounting_holes,
)
from perfstudio.guide import (
    Guide,
    GuideStep,
    PartStep,
    all_steps,
    build_guide,
    document_at_step,
    step_focus,
)
from perfstudio.guide import describe as describe_guide
from perfstudio.guide_export import bom_to_csv, cut_list_to_csv, guide_to_html, guide_to_json
from perfstudio.lvs import LvsIssue, LvsResult, run_lvs, stale_conductor_ids
from perfstudio.model import (
    Board,
    BoardEdge,
    BoardLabels,
    BoardMaterial,
    BoardSide,
    BoardType,
    ComponentInstance,
    DocumentMeta,
    EdgeConnector,
    Footprint,
    HoleCoord,
    MountingHole,
    NetClass,
    NetId,
    NetNode,
    PadAxis,
    PadShape,
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
from perfstudio.router import RoutingStyle, options_for_style
from perfstudio.schematic import build_schematic
from perfstudio.schematic_export import drawing_to_svg
from perfstudio.stripboard import is_stripboard
from perfstudio.striproute import StripboardPlan, plan_stripboard
from perfstudio.striproute import describe_plan as describe_strip_plan
from perfstudio.updates import RELEASES_PAGE_URL, Release, is_check_due
from perfstudio.version import __version__
from perfstudio.version import describe as describe_version

from . import icons, updater, view3d
from .boardcolors import SCHEMES as BOARD_SCHEMES
from .boardcolors import choose as choose_board_colour
from .boardcolors import chosen_key as chosen_board_colour
from .clipboard import block_from_json, block_to_json, paste_payload, paste_position
from .export_pdf import export_pdf
from .export_schematic import SchematicRenderError, svg_to_pdf, svg_to_png
from .i18n import language as current_language
from .i18n import set_language, t
from .theme import ERROR, OK, STYLESHEET, TEXT_DIM, WARNING
from .view2d import BoardScene, BoardView, hole_to_screen, join_pins, next_reference
from .viewsch import SchematicView

#: What the Preferred Connection menu can be set to: one of the router's styles, or "best"
#: to route with every style and keep whichever produces the board that is least work to
#: build. "best" is a UI concept and stays here -- the engine is given a concrete style.
type StylePreference = RoutingStyle | Literal["best"]


#: Where everything remembered between runs is kept: the recent-file list, the window
#: layout, and the view preferences. A function rather than a constant so a test can point
#: it at a temporary file instead of the real user store -- a test suite has no business
#: writing into somebody's registry, and one that did would also make these tests depend on
#: whatever ran before them, including on the developer having opened a board that morning.
def app_settings() -> QSettings:
    return QSettings("PerfStudio", "PerfStudio")


RECENT_FILES_KEY = "recentFiles"


def _stored_bool(settings: QSettings, key: str, default: bool) -> bool:
    """A tick from the store, whichever way the platform round-tripped it.

    QSettings is not one format. On Windows it is the registry, which keeps a bool a
    bool; the INI backend the tests use writes the string ``"true"``, which is truthy
    either way it is spelled -- so a plain ``bool(value)`` would restore every toggle to
    ON and the bug would be invisible on the developer's own machine.
    """
    stored = settings.value(key, default)
    if isinstance(stored, str):
        return stored.strip().lower() in ("true", "1", "yes")
    return bool(stored)


#: The session: where the window was and what it was showing. Grouped under one prefix so
#: the whole lot can be cleared by hand without touching the recent-file list.
#:
#: What is NOT here is as deliberate as what is. The board SIDE is not remembered -- being
#: dropped onto the solder side of a board you have just opened, mirrored, with no memory
#: of asking for it, is disorienting in a way a dock width is not. Nor is the document:
#: this application opens what it is given.
GEOMETRY_KEY = "session/geometry"
WINDOW_STATE_KEY = "session/windowState"
BOARD_COLOUR_KEY = "session/boardColour"
RATSNEST_KEY = "session/showRatsnest"
RULERS_KEY = "session/showRulers"
HATCH_KEY = "session/hatchFarSide"
ROUTING_STYLE_KEY = "session/routingStyle"
LANGUAGE_KEY = "session/language"
#: Whether this person has ever placed a part. The blank-board guidance is for the first
#: launch, and repeating it forever is the application explaining its own front door to
#: somebody who has walked through it a hundred times.
HAS_PLACED_KEY = "session/hasPlacedAPart"

#: How long after the window appears the daily update check runs. Long enough that the
#: board is drawn and the first paint is over; short enough that somebody who opened the
#: application to see whether there is an update does not conclude it has no such thing.
UPDATE_CHECK_DELAY_MS = 1500

ROLE_HOLES = int(Qt.ItemDataRole.UserRole) + 1
ROLE_COMPONENT_IDS = int(Qt.ItemDataRole.UserRole) + 2
ROLE_NET_ID = int(Qt.ItemDataRole.UserRole) + 3
ROLE_FOOTPRINT_ID = int(Qt.ItemDataRole.UserRole) + 4
ROLE_STEP_INDEX = int(Qt.ItemDataRole.UserRole) + 5
ROLE_FINDING_KEY = int(Qt.ItemDataRole.UserRole) + 6
#: (component ref, pin number) on a pin row under a net. The row also carries
#: ROLE_NET_ID, so selecting a pin highlights its net exactly as selecting the net does.
ROLE_PIN = int(Qt.ItemDataRole.UserRole) + 5


def _now_iso() -> str:
    now = datetime.datetime.now(datetime.UTC)
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


#: Substrate to add outside the hole grid when a board carries a printed legend, so the
#: characters have somewhere to go. Roughly what the boards being modelled have.
LEGEND_BORDER_MM = 2.0


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

    PAD_SHAPES: tuple[tuple[PadShape, str], ...] = (
        ("round", "Round"),
        ("oblong", "Oblong — solder bridges easily along the long axis"),
    )

    PAD_AXES: tuple[tuple[PadAxis, str], ...] = (
        ("vertical", "Down a column"),
        ("horizontal", "Along a row"),
    )

    # The kind of board, which is not a display option: on stripboard whole rows of holes
    # arrive already joined, so connectivity, DRC, the router and the build guide all
    # answer differently. The description says what it means rather than naming a product,
    # because "Veroboard" is a brand and "stripboard" is what the rest of the world calls
    # the same thing.
    BOARD_TYPES: tuple[tuple[BoardType, str], ...] = (
        ("pad-per-hole", "Pad per hole — every hole is its own island"),
        ("stripboard", "Stripboard — whole rows joined; you cut the track to separate"),
    )

    STRIP_AXES: tuple[tuple[Literal["horizontal", "vertical"], str], ...] = (
        ("horizontal", "Along a row"),
        ("vertical", "Down a column"),
    )

    def __init__(self, board: Board, parent: QWidget | None = None, title: str = "New Board") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        # Assigned before any widget signal can fire: `_update_note` asks `board()` for
        # the pad gaps it prints, and `board()` builds its result from this.
        self._board = board
        #: The product chosen from the list, if one was. What makes the difference between
        #: "resize this board" and "this is a different board, with the fingers and corner
        #: holes that come with it".
        self._preset: BoardPreset | None = None

        # Perfboard is bought as "a 5 by 7", never as a hole count, so the sizes actually
        # stocked come first and the spin boxes below are the escape hatch. Picking one
        # also settles the things that travel with a board family -- a phenolic board is
        # single-sided with no printed legend, an FR-4 one is neither.
        self.preset = QComboBox()
        self.preset.addItem(t("Custom size"), "")
        for family, heading in (
            ("double-sided-fr4", t("Double-sided green, plated holes")),
            ("single-sided-phenolic", t("Single-sided orange phenolic")),
        ):
            # A separator per family rather than one flat list: the two are different
            # products, not two settings of one, and the list is what makes that visible
            # before anything is chosen.
            self.preset.insertSeparator(self.preset.count())
            heading_index = self.preset.count()
            self.preset.addItem(heading, "")
            # Disabled through the item's flags rather than QStandardItemModel.item(),
            # which QComboBox.model() is not typed as returning.
            self.preset.setItemData(
                heading_index, QColor(TEXT_DIM), Qt.ItemDataRole.ForegroundRole
            )
            self.preset.setItemData(heading_index, 0, Qt.ItemDataRole.UserRole - 1)
            for entry in STANDARD_PRESETS:
                if entry.family != family:
                    continue
                self.preset.addItem(f"    {entry.name}  ·  {entry.cols} × {entry.rows}", entry.key)
        self.preset.currentIndexChanged.connect(self._on_preset)

        self.board_type = QComboBox()
        for type_value, type_label in self.BOARD_TYPES:
            self.board_type.addItem(t(type_label), type_value)
        self.board_type.setCurrentIndex(max(0, self.board_type.findData(board.type)))
        self.strip_axis = QComboBox()
        for strip_value, strip_label in self.STRIP_AXES:
            self.strip_axis.addItem(t(strip_label), strip_value)
        self.strip_axis.setCurrentIndex(
            max(0, self.strip_axis.findData(board.strip_axis or "horizontal"))
        )

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

        # Pad shape is here rather than buried somewhere, because on a board whose pads
        # are oblong it changes which way a solder trace is easy to make -- the gap to the
        # next pad along the long axis can be half what it is across. DRC and the build
        # guide both say so, and neither can if this is not set to match the board in the
        # user's hand.
        self.pad_shape = QComboBox()
        for shape, shape_label in self.PAD_SHAPES:
            self.pad_shape.addItem(t(shape_label), shape)
        self.pad_shape.setCurrentIndex(max(0, self.pad_shape.findData(board.pad_shape)))

        self.pad_length = QDoubleSpinBox()
        self.pad_length.setRange(0.1, 20.0)
        self.pad_length.setSingleStep(0.05)
        self.pad_length.setDecimals(2)
        self.pad_length.setSuffix(" mm")
        self.pad_length.setValue(board.pad_length or round(board.pad_diameter * 1.2, 2))

        self.pad_axis = QComboBox()
        for axis, axis_label in self.PAD_AXES:
            self.pad_axis.addItem(t(axis_label), axis)
        self.pad_axis.setCurrentIndex(max(0, self.pad_axis.findData(board.pad_axis)))

        self.legend = QCheckBox(t("Addresses printed on the board"))
        self.legend.setToolTip(
            # One literal, not two concatenated: tests/test_i18n.py scans for translated
            # strings and an implicit concatenation inside t() is invisible to it, so the
            # translation would silently never be used.
            t("Boards carrying their own A-Z / 01-22 legend, printed on the board itself.")
        )
        self.legend.setChecked(board.labels is not None)

        self.row_digits = QSpinBox()
        self.row_digits.setRange(1, 4)
        self.row_digits.setValue(board.labels.row_digits if board.labels else 2)
        self.row_digits.setToolTip(t('2 prints row 7 as "07", the way most such boards do.'))

        self._size_note = QLabel()
        self.cols.valueChanged.connect(self._update_note)
        self.rows.valueChanged.connect(self._update_note)
        self.board_type.currentIndexChanged.connect(self._update_enabled)
        self.pad_shape.currentIndexChanged.connect(self._update_enabled)
        self.legend.toggled.connect(self._update_enabled)
        self._pitch = board.pitch
        self._pad_diameter = board.pad_diameter
        self._update_note()

        form = QFormLayout()
        form.addRow(t("Board"), self.preset)
        form.addRow(t("Type"), self.board_type)
        form.addRow(t("Strips run"), self.strip_axis)
        form.addRow(t("Columns"), self.cols)
        form.addRow(t("Rows"), self.rows)
        form.addRow(t("Material"), self.material)
        form.addRow(t("Pad shape"), self.pad_shape)
        form.addRow(t("Pad length"), self.pad_length)
        form.addRow(t("Long axis"), self.pad_axis)
        form.addRow("", self.legend)
        form.addRow(t("Row digits"), self.row_digits)
        form.addRow("", self._size_note)
        self._update_enabled()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_preset(self) -> None:
        """Apply a stocked size. Silent when the user picks "Custom size" back again --
        the numbers they have already typed are the point of that entry."""
        key = self.preset.currentData()
        entry = next((p for p in STANDARD_PRESETS if p.key == key), None)
        if entry is None:
            return
        board = board_from_preset(entry, self._board)
        self._board = board
        self._preset = entry
        self.cols.setValue(board.cols)
        self.rows.setValue(board.rows)
        self.material.setCurrentIndex(max(0, self.material.findData(board.material)))
        self.pad_shape.setCurrentIndex(max(0, self.pad_shape.findData(board.pad_shape)))
        self.legend.setChecked(board.labels is not None)
        if board.labels is not None:
            self.row_digits.setValue(board.labels.row_digits)
        self._update_enabled()

    def _update_enabled(self) -> None:
        oblong = self.pad_shape.currentData() == "oblong"
        self.pad_length.setEnabled(oblong)
        self.pad_axis.setEnabled(oblong)
        self.row_digits.setEnabled(self.legend.isChecked())
        # Which way the strips run is a question only stripboard has an answer to.
        self.strip_axis.setEnabled(self.board_type.currentData() == "stripboard")
        self._update_note()

    def _update_note(self) -> None:
        # The physical size, because that is what someone holds against a piece of board
        # they already own -- "80 columns" means nothing at the shop.
        width = self.cols.value() * self._pitch
        height = self.rows.value() * self._pitch
        note = (
            f"{width:.1f} × {height:.1f} mm ({self.cols.value() * self.rows.value()} holes)"
        )
        # The gap the R5' rule is about, quoted while the shape is being chosen rather
        # than only once DRC runs. On a round board the two numbers are the same and one
        # is enough; on an oblong one the difference between them IS the choice.
        board = self.board()
        gaps = pad_edge_gap_mm(board, "horizontal"), pad_edge_gap_mm(board, "vertical")
        if abs(gaps[0] - gaps[1]) < 1e-9:
            note += f" — {gaps[0]:.2f} mm between pads"
        else:
            note += f" — {gaps[0]:.2f} mm between pads along a row, {gaps[1]:.2f} mm down a column"
        self._size_note.setText(f"<span style='color:{TEXT_DIM}'>{note}</span>")

    def preset_features(
        self,
    ) -> tuple[tuple[EdgeConnector, ...], tuple[MountingHole, ...]] | None:
        """What the caller needs to apply a whole product rather than just a size."""
        return _preset_features(self._preset, self.board())

    def board(self) -> Board:
        # A legend needs somewhere to be printed. Half a pitch past the outer holes leaves
        # 0.32 mm of bare substrate at 2.54 mm pitch, which is not room for a character --
        # the boards that carry a legend are physically wider at the edge, so turning the
        # legend on gives the board that border rather than drawing text under the pads.
        # Each axis keeps its own border: a preset solves them separately so the outline
        # is the advertised size to the tenth of a millimetre, and averaging them here
        # would throw that away. Only a legend with nowhere to go raises either of them.
        border_x, border_y = self._board.border_x_mm, self._board.border_y_mm
        if self.legend.isChecked():
            border_x = max(border_x, LEGEND_BORDER_MM)
            border_y = max(border_y, LEGEND_BORDER_MM)

        shape = cast(PadShape, self.pad_shape.currentData())
        # The length is carried only when it means something. Writing it out on a round
        # board would put a field in the .perf file that describes nothing, and the format
        # omits anything at its default precisely so an unused feature leaves no trace.
        length = round(self.pad_length.value(), 3) if shape == "oblong" else None
        if length is not None and length <= self._pad_diameter:
            # The dialog's own floor: board.set refuses this, and a dialog that can only
            # be dismissed by an error message is a worse dialog than one that cannot
            # produce the error.
            length = round(self._pad_diameter + 0.05, 3)
        # strip_axis is written only on a board that has strips, for the reason pad_length
        # is written only on an oblong one: a field describing nothing does not belong in
        # the file, and the format omits its defaults precisely so an unused feature
        # leaves no trace.
        board_type = cast(BoardType, self.board_type.currentData())
        return dataclasses.replace(
            self._board,
            type=board_type,
            strip_axis=(
                cast(Literal["horizontal", "vertical"], self.strip_axis.currentData())
                if board_type == "stripboard"
                else None
            ),
            cols=self.cols.value(),
            rows=self.rows.value(),
            material=cast(BoardMaterial, self.material.currentData()),
            pad_shape=shape,
            pad_length=length,
            pad_axis=cast(PadAxis, self.pad_axis.currentData()),
            border_x_mm=border_x,
            border_y_mm=border_y,
            labels=(
                BoardLabels(row_digits=self.row_digits.value())
                if self.legend.isChecked()
                else None
            ),
        )


def _corner_offset_mm(board: Board, diameter: float) -> float:
    """How far diagonally OUT of the grid a corner mounting hole should sit.

    Real boards put their corner holes in the border, clear of every pad — look at any
    of them and the copper grid is untouched. That is only possible if the border is wide
    enough to take the bore; on a flush-cut board there is nowhere to go, and the hole
    has to land on the grid and eat the pads around it, which is what DRC then reports.

    Returns 0 when it will not fit, so the caller gets the old on-grid behaviour rather
    than a hole hanging off the edge of the board.
    """
    extent_x, extent_y = pad_extent_mm(board)
    # Far enough out that the bore misses the corner pad, measured on the diagonal.
    clears_pad = (max(extent_x, extent_y) / 2 + diameter / 2) / math.sqrt(2)
    # Near enough in that the bore stays on the substrate, on the tighter axis.
    stays_on_board = min(
        board_edge_margin_mm(board, "horizontal"), board_edge_margin_mm(board, "vertical")
    ) - diameter / 2 - 0.2
    if stays_on_board < clears_pad:
        return 0.0
    return round(stays_on_board, 3)


class BoardFeaturesDialog(QDialog):
    """Mounting holes and edge-connector fingers: what the board has, and how to change it.

    Every button here dispatches straight onto the bus rather than collecting an edit to
    apply on OK. Two reasons. Each change is then its own undo step, which is what a
    person means by "undo that" when they have added a connector and then four holes; and
    the board redraws underneath the dialog as they work, which is the only way to see
    that a corner hole has landed where a part already is.

    The corner holes go in as ONE command (``mounting-hole.addMany``) because putting a
    hole in each corner is one decision -- see the payload's own note.
    """

    EDGES: tuple[tuple[BoardEdge, str], ...] = (
        ("top", "Top"),
        ("bottom", "Bottom"),
        ("left", "Left"),
        ("right", "Right"),
    )

    def __init__(self, bus: CommandBus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bus = bus
        self.setWindowTitle(t("Board Features"))
        self.setMinimumWidth(560)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([t("Feature"), t("Where"), t("Size")])
        self.tree.setRootIsDecorated(False)

        self.act_remove = QPushButton(t("Remove"))
        self.act_remove.clicked.connect(self._on_remove)

        self.mount_diameter = QDoubleSpinBox()
        self.mount_diameter.setRange(0.5, 12.0)
        self.mount_diameter.setSingleStep(0.1)
        self.mount_diameter.setDecimals(1)
        self.mount_diameter.setSuffix(" mm")
        self.mount_diameter.setValue(3.2)

        self.mount_inset = QSpinBox()
        self.mount_inset.setRange(0, 20)
        self.mount_inset.setValue(1)
        self.mount_inset.setToolTip(
            t("How many holes in from each corner; 0 uses the corner hole itself.")
        )

        add_corners = QPushButton(t("Add Corner Holes"))
        add_corners.clicked.connect(self._on_add_corners)

        self.edge = QComboBox()
        for value, label in self.EDGES:
            self.edge.addItem(t(label), value)
        self.edge.setCurrentIndex(1)

        self.finger_start = QSpinBox()
        self.finger_start.setRange(0, 999)
        self.finger_count = QSpinBox()
        self.finger_count.setRange(1, 999)
        self.finger_count.setValue(8)

        add_connector = QPushButton(t("Add Edge Connector"))
        add_connector.clicked.connect(self._on_add_connector)

        # 0 means "no limit", which is why the range starts there rather than at a
        # buildable height: with no case chosen there is no height to be too tall for,
        # and a spin box cannot be empty. The suffix says so, so it does not read as a
        # limit of zero.
        self.height_limit = QDoubleSpinBox()
        self.height_limit.setRange(0.0, 200.0)
        self.height_limit.setSingleStep(1.0)
        self.height_limit.setDecimals(1)
        self.height_limit.setSpecialValueText(t("No limit"))
        self.height_limit.setSuffix(" mm")
        self.height_limit.setToolTip(
            t("Clear height inside the case, above the board. Taller parts are reported by DRC.")
        )

        apply_height = QPushButton(t("Set Height Limit"))
        apply_height.clicked.connect(self._on_set_height_limit)

        height = QFormLayout()
        height.addRow(t("Height limit"), self.height_limit)
        height.addRow("", apply_height)

        mounting = QFormLayout()
        mounting.addRow(t("Hole diameter"), self.mount_diameter)
        mounting.addRow(t("Inset (holes)"), self.mount_inset)
        mounting.addRow("", add_corners)

        connector = QFormLayout()
        connector.addRow(t("Edge"), self.edge)
        connector.addRow(t("First hole"), self.finger_start)
        connector.addRow(t("Fingers"), self.finger_count)
        connector.addRow("", add_connector)

        self.note = QLabel()
        self.note.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tree)
        layout.addWidget(self.act_remove)
        layout.addLayout(mounting)
        layout.addLayout(connector)
        layout.addLayout(height)
        layout.addWidget(self.note)
        layout.addWidget(buttons)
        self._reload()

    def _reload(self) -> None:
        doc = self.bus.document
        self.tree.clear()
        for mount in doc.mounting_holes:
            item = QTreeWidgetItem(
                [
                    t("Mounting hole"),
                    format_hole(mount.at),
                    f"⌀{mount.diameter:g} mm, {mount.head_diameter:g} mm head",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, ("mounting-hole.delete", mount.id))
            self.tree.addTopLevelItem(item)
        for conn in doc.edge_connectors:
            holes = edge_connector_holes(conn, doc.board)
            where = (
                f"{format_hole(holes[0])}–{format_hole(holes[-1])}" if holes else conn.edge
            )
            item = QTreeWidgetItem(
                [
                    t("Edge connector"),
                    f"{conn.edge}, {where}",
                    f"{conn.count} × {conn.finger_width:g} mm",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, ("edge-connector.delete", conn.id))
            self.tree.addTopLevelItem(item)
        for column in range(3):
            self.tree.resizeColumnToContents(column)
        self.act_remove.setEnabled(self.tree.topLevelItemCount() > 0)
        # Read back rather than left as typed, so the box shows what the document says
        # after an undo -- the dialog stays open while the board changes underneath it.
        self.height_limit.setValue(doc.height_limit_mm if doc.height_limit_mm else 0.0)

    def _say(self, result: DispatchResult) -> None:
        """Report a refusal in place. The bus refuses for reasons a user can act on --
        a hole off the board, two connectors claiming one pad -- so the message is worth
        showing verbatim rather than replacing with "could not add"."""
        if result.ok:
            self.note.setText("")
            self._reload()
        else:
            self.note.setText(f"<span style='color:{WARNING}'>{result.message}</span>")

    def _on_remove(self) -> None:
        item = self.tree.currentItem() or self.tree.topLevelItem(0)
        if item is None:
            return
        command, id_ = item.data(0, Qt.ItemDataRole.UserRole)
        payload: Any = (
            DeleteMountingHolePayload(id=id_)
            if command == "mounting-hole.delete"
            else DeleteEdgeConnectorPayload(id=id_)
        )
        self._say(self.bus.dispatch(command, payload))

    def _on_add_corners(self) -> None:
        board = self.bus.document.board
        inset = self.mount_inset.value()
        far_col, far_row = board.cols - 1 - inset, board.rows - 1 - inset
        if far_col <= inset or far_row <= inset:
            self.note.setText(
                f"<span style='color:{WARNING}'>"
                + t("That inset does not fit on this board.")
                + "</span>"
            )
            return
        corners = (
            HoleCoord(inset, inset),
            HoleCoord(far_col, inset),
            HoleCoord(inset, far_row),
            HoleCoord(far_col, far_row),
        )
        diameter = self.mount_diameter.value()
        offset = _corner_offset_mm(board, diameter)
        self._say(
            self.bus.dispatch(
                "mounting-hole.addMany",
                AddMountingHolesPayload(
                    ats=corners,
                    # Signs put each hole outside its own corner rather than all four in
                    # the same direction.
                    offsets=tuple(
                        (
                            -offset if hole.col <= inset else offset,
                            -offset if hole.row <= inset else offset,
                        )
                        for hole in corners
                    ),
                    diameter=diameter,
                    # A washer is roughly twice the bolt's clearance hole, which is the
                    # keepout DRC warns about. Guessed here rather than asked for: a
                    # person fitting an M3 screw does not want to be quizzed about it.
                    head_diameter=round(diameter * 2, 2),
                    label=f"Drill 4 corner mounting holes ({diameter:g} mm)",
                ),
            )
        )

    def _on_add_connector(self) -> None:
        self._say(
            self.bus.dispatch(
                "edge-connector.add",
                AddEdgeConnectorPayload(
                    edge=cast(BoardEdge, self.edge.currentData()),
                    start=self.finger_start.value(),
                    count=self.finger_count.value(),
                ),
            )
        )

    def _on_set_height_limit(self) -> None:
        value = self.height_limit.value()
        self._say(
            self.bus.dispatch(
                "height-limit.set",
                SetHeightLimitPayload(height_limit_mm=None if value <= 0 else value),
            )
        )


class NetDialog(QDialog):
    """What a net is called, what kind it is, and what it carries.

    The last part is not padding. ``current_a`` and ``voltage_v`` had no way into a
    document at all -- the KiCad netlist format does not carry them -- and three things
    read them: DRC's current-capacity rule, DRC's creepage rule, and the wire gauge the
    build guide prints on its cut list. Without a number here all three stay silent, so
    this dialog is the only place a board can be told it is carrying two amps.

    Both are optional and blank means "not stated", which is a different thing from zero.
    """

    NET_CLASSES: tuple[tuple[NetClass, str], ...] = (
        ("signal", "Signal"),
        ("ground", "Ground — routed first, and wants a rail"),
        ("power", "Power — routed after ground, same reason"),
    )

    def __init__(
        self,
        parent: QWidget | None = None,
        title: str = "New Net",
        name: str = "",
        net_class: NetClass = "signal",
        current_a: float | None = None,
        voltage_v: float | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)

        form = QFormLayout()
        self.name = QLineEdit(name)
        # Not translated, like every other example of the tool's own vocabulary: a net
        # called GND is called GND in Turkish too, and a catalogue entry mapping the
        # string to itself is one the tests correctly refuse.
        self.name.setPlaceholderText("GND, +5V, OUT…")
        form.addRow(t("Name"), self.name)

        self.net_class = QComboBox()
        for value, label in self.NET_CLASSES:
            self.net_class.addItem(t(label), value)
        index = self.net_class.findData(net_class)
        if index >= 0:
            self.net_class.setCurrentIndex(index)
        form.addRow(t("Class"), self.net_class)

        # Zero is the "not stated" position rather than a value, because a net carrying
        # no current is exactly what saying nothing means.
        self.current = QDoubleSpinBox()
        self.current.setRange(0.0, 100.0)
        self.current.setDecimals(2)
        self.current.setSingleStep(0.1)
        self.current.setSuffix(" A")
        self.current.setSpecialValueText(t("not stated"))
        self.current.setValue(current_a if current_a is not None else 0.0)
        self.current.setToolTip(
            t(
                "Wakes DRC's current-capacity rule and picks the wire gauge on the build "
                "guide's cut list. Nothing else in the application can set it."
            )
        )
        form.addRow(t("Current"), self.current)

        # Voltage needs a real "unset", and unlike current it may legitimately be
        # negative, so the bottom of the range cannot double as the empty value.
        self.voltage = QDoubleSpinBox()
        self.voltage.setRange(-1000.0, 1000.0)
        self.voltage.setDecimals(1)
        self.voltage.setSuffix(" V")
        self.voltage.setValue(voltage_v if voltage_v is not None else 0.0)
        self.voltage_stated = QCheckBox(t("state a voltage"))
        self.voltage_stated.setChecked(voltage_v is not None)
        self.voltage.setEnabled(voltage_v is not None)
        self.voltage_stated.toggled.connect(self.voltage.setEnabled)
        self.voltage.setToolTip(
            t(
                "Wakes DRC's creepage rule above the mains threshold. A -12 V rail is an "
                "ordinary value here, which is why it needs its own tick rather than a zero."
            )
        )
        row = QHBoxLayout()
        row.addWidget(self.voltage_stated)
        row.addWidget(self.voltage)
        form.addRow(t("Voltage"), row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def values(self) -> tuple[str, NetClass, float | None, float | None]:
        current = self.current.value()
        return (
            self.name.text().strip(),
            cast(NetClass, self.net_class.currentData()),
            current if current > 0 else None,
            self.voltage.value() if self.voltage_stated.isChecked() else None,
        )


class ComponentDialog(QDialog):
    """What a placed part is called and what it IS.

    The value was the one field on the document no human could reach. Every part the
    window placed was given ``value=""`` and nothing could change it afterwards, while
    an agent on the MCP server has been able to pass one to ``place_component`` since
    that server existed -- so the tool's own build guide printed "Resistor x 4" where it
    meant "10k x 4", because ``guide._bom`` groups on exactly this field.

    ONLY what ``component.update`` carries: reference, value and lock. Rotation is a
    command of its own and putting it here would turn one press of OK into two entries
    on the undo stack for what the user experienced as one edit -- so it stays on R and
    Shift+R, which is where it can be seen happening anyway.

    Everything else on the part is shown and not editable: a footprint is chosen by
    placing, and an anchor by dragging.
    """

    def __init__(
        self,
        component: ComponentInstance,
        footprint: Footprint | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Part Properties"))
        self.setMinimumWidth(380)

        form = QFormLayout()
        self.ref = QLineEdit(component.ref)
        self.ref.setPlaceholderText("R1, C3, U2…")
        self.ref.setToolTip(
            t(
                "The designator the schematic uses. Renaming one that a net names takes "
                "it off that net, so rename before importing a netlist rather than after."
            )
        )
        form.addRow(t("Reference"), self.ref)

        self.value = QLineEdit(component.value)
        self.value.setPlaceholderText("10k, 100nF, NE555…")
        self.value.setToolTip(
            t(
                "What the part actually is. This is the column the build guide's bill of "
                "materials groups on, so a blank one becomes a line you cannot order."
            )
        )
        form.addRow(t("Value"), self.value)

        self.locked = QCheckBox(t("locked — auto-placement leaves it where it is"))
        self.locked.setChecked(component.locked)
        form.addRow("", self.locked)

        # The facts, so the dialog answers "which part is this" as well as renaming it.
        # A connector is placed by its pin 1 and named after nothing in particular, and at
        # this point the user has usually double-clicked to find out which one they hit.
        name = footprint.name if footprint is not None else component.footprint_id
        pins = f"{len(footprint.pins)}" if footprint is not None else "?"
        height = f"{footprint.body_height:.1f} mm" if footprint is not None else "?"
        for label, fact in (
            (t("Footprint"), f"{name}  ({component.footprint_id})"),
            (t("Pins"), pins),
            (t("Pin 1 at"), f"{format_hole(component.anchor)}  ·  {component.rotation}°"),
            (t("Height"), height),
        ):
            shown = QLabel(fact)
            shown.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            shown.setStyleSheet(f"color: {TEXT_DIM};")
            form.addRow(label, shown)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)
        # The value is what this was opened for in nearly every case -- the reference is
        # already right, because the application generated it.
        self.value.setFocus()
        self.value.selectAll()

    def values(self) -> tuple[str, str, bool]:
        return self.ref.text().strip(), self.value.text().strip(), self.locked.isChecked()


class GoToPartDialog(QDialog):
    """Find a part by name on a board too big to scan by eye.

    A board of any size has no way to answer "where is R37" other than reading the screen
    until it turns up -- and the parts that need finding are the ones on a dense board,
    which is exactly where reading the screen does not work.

    Filters on reference, value and footprint together, because which of the three someone
    remembers depends on why they are looking: "R37" from a DRC message, "10k" from the
    schematic, "TO-220" from the pile of parts on the bench.
    """

    def __init__(
        self,
        components: Sequence[ComponentInstance],
        lookup: FootprintLookup,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Go to Part"))
        self.resize(460, 480)
        self._components = tuple(components)
        self._lookup = lookup

        self.filter = QLineEdit()
        self.filter.setPlaceholderText(t("Filter parts…  (R37, 10k, TO-220, C7)"))
        self.filter.textChanged.connect(self._refilter)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(self.filter)
        layout.addWidget(self.list)
        layout.addWidget(buttons)
        self.setLayout(layout)
        self._refilter("")
        # Focus in the filter box, because typing is what somebody opening this came to
        # do: the list is how they confirm it, not how they search it.
        self.filter.setFocus()

    def _describe(self, component: ComponentInstance) -> str:
        footprint = self._lookup(component.footprint_id)
        name = footprint.name if footprint is not None else component.footprint_id
        value = f"  {component.value}" if component.value else ""
        return f"{component.ref}{value}  ·  {name}  ·  {format_hole(component.anchor)}"

    def _refilter(self, text: str) -> None:
        needle = text.strip().lower()
        self.list.clear()
        for component in self._components:
            row = self._describe(component)
            if needle and needle not in row.lower():
                continue
            item = QListWidgetItem(row)
            item.setData(Qt.ItemDataRole.UserRole, component.id)
            self.list.addItem(item)
        # Pre-selected, so Enter goes straight to the obvious answer once a filter has
        # narrowed it to one. Without this the dialog asks to be clicked as well as typed.
        if self.list.count():
            self.list.setCurrentRow(0)

    def chosen_id(self) -> str | None:
        item = self.list.currentItem()
        if item is None:
            return None
        chosen = item.data(Qt.ItemDataRole.UserRole)
        return str(chosen) if chosen is not None else None


@dataclasses.dataclass(frozen=True, slots=True)
class _CustomField:
    """One number the user sets. ``kind`` picks the widget and the units."""

    key: str
    label: str
    kind: Literal["int", "mm", "bool"]
    minimum: float
    maximum: float
    default: float


@dataclasses.dataclass(frozen=True, slots=True)
class _CustomFamily:
    """A family of parts the engine can generate, and the numbers it needs.

    ``build`` calls the ENGINE's own generator rather than assembling an id here. That is
    the whole discipline of this dialog: ``footprints.GENERATED_ID_GRAMMAR`` is one place,
    and a second copy of it in the interface would be a second thing to keep in step -- and
    the one that goes wrong silently, because a wrong id does not fail, it just names a
    part nobody has.
    """

    label: str
    fields: tuple[_CustomField, ...]
    build: Callable[[dict[str, float]], Footprint]


def _custom_families() -> tuple[_CustomFamily, ...]:
    """Built by a call rather than held as a constant, because the labels go through
    ``t()`` and the language is chosen after this module is imported."""

    def whole(values: dict[str, float], key: str) -> int:
        return round(values[key])

    return (
        _CustomFamily(
            label=t("Any rectangular part"),
            fields=(
                _CustomField("cols", t("Pins across"), "int", 1, 64, 4),
                _CustomField("rows", t("Rows of pins"), "int", 1, 64, 2),
                _CustomField("col_step", t("Holes between pins"), "int", 1, 20, 1),
                _CustomField("row_step", t("Holes between rows"), "int", 1, 20, 3),
                _CustomField("width", t("Body width (mm)"), "mm", 0.5, 200, 15),
                _CustomField("depth", t("Body depth (mm)"), "mm", 0.5, 200, 10),
                _CustomField("height", t("Body height (mm)"), "mm", 0.5, 200, 8),
            ),
            build=lambda v: generic_box_footprint(
                cols=whole(v, "cols"),
                rows=whole(v, "rows"),
                col_step=whole(v, "col_step"),
                row_step=whole(v, "row_step"),
                width_mm=v["width"],
                depth_mm=v["depth"],
                height_mm=v["height"],
            ),
        ),
        _CustomFamily(
            label=t("DIP (dual in-line)"),
            fields=(
                _CustomField("pins", t("Pins"), "int", 4, 64, 20),
                _CustomField("wide", t("Wide body (0.6 in)"), "bool", 0, 1, 0),
            ),
            build=lambda v: dip_footprint(
                pin_count=whole(v, "pins"), wide=bool(round(v["wide"]))
            ),
        ),
        _CustomFamily(
            label=t("Pin header"),
            fields=(
                _CustomField("rows", t("Rows"), "int", 1, 8, 1),
                _CustomField("cols", t("Pins per row"), "int", 1, 64, 12),
            ),
            build=lambda v: pin_header_footprint(
                rows=whole(v, "rows"), cols=whole(v, "cols")
            ),
        ),
        _CustomFamily(
            label=t("Screw terminal"),
            fields=(_CustomField("ways", t("Ways"), "int", 2, 24, 4),),
            build=lambda v: screw_terminal_footprint(ways=whole(v, "ways")),
        ),
        _CustomFamily(
            label=t("Axial part (resistor, diode, choke)"),
            fields=(
                _CustomField("span", t("Lead span (holes)"), "int", 1, 40, 8),
                _CustomField("length", t("Body length (mm)"), "mm", 0.5, 200, 12),
                _CustomField("diameter", t("Body diameter (mm)"), "mm", 0.5, 100, 5),
                _CustomField("polarized", t("Polarised (banded end)"), "bool", 0, 1, 0),
            ),
            build=lambda v: axial_footprint(
                span_holes=whole(v, "span"),
                body_length_mm=v["length"],
                body_diameter_mm=v["diameter"],
                polarized=bool(round(v["polarized"])),
            ),
        ),
        _CustomFamily(
            label=t("Electrolytic capacitor"),
            fields=(
                _CustomField("pitch", t("Lead pitch (holes)"), "int", 1, 20, 5),
                _CustomField("diameter", t("Can diameter (mm)"), "mm", 1, 60, 16),
                _CustomField("height", t("Can height (mm)"), "mm", 1, 100, 25),
            ),
            build=lambda v: radial_electrolytic_footprint(
                pitch_holes=whole(v, "pitch"),
                can_diameter_mm=v["diameter"],
                can_height_mm=v["height"],
            ),
        ),
        _CustomFamily(
            label=t("Disc ceramic capacitor"),
            fields=(
                _CustomField("pitch", t("Lead pitch (holes)"), "int", 1, 20, 2),
                _CustomField("diameter", t("Disc diameter (mm)"), "mm", 1, 40, 7),
                _CustomField("thickness", t("Disc thickness (mm)"), "mm", 0.5, 20, 3),
            ),
            build=lambda v: disc_ceramic_footprint(
                pitch_holes=whole(v, "pitch"),
                body_diameter_mm=v["diameter"],
                body_thickness_mm=v["thickness"],
            ),
        ),
        _CustomFamily(
            label=t("Film capacitor"),
            fields=(
                _CustomField("pitch", t("Lead pitch (holes)"), "int", 1, 20, 3),
                _CustomField("length", t("Body length (mm)"), "mm", 1, 60, 10),
                _CustomField("width", t("Body width (mm)"), "mm", 1, 40, 5),
                _CustomField("height", t("Body height (mm)"), "mm", 1, 60, 8),
            ),
            build=lambda v: box_film_capacitor_footprint(
                pitch_holes=whole(v, "pitch"),
                body_length_mm=v["length"],
                body_width_mm=v["width"],
                body_height_mm=v["height"],
            ),
        ),
        _CustomFamily(
            label=t("LED"),
            fields=(_CustomField("diameter", t("LED diameter (mm)"), "int", 3, 10, 5),),
            build=lambda v: led_footprint(diameter_mm=whole(v, "diameter")),
        ),
    )


class CustomPartDialog(QDialog):
    """Describe a part the library does not have, and get its identifier back.

    WHAT IT IS NOT is the point of the shape it has. It is not a footprint editor -- there
    is no outline to draw and nothing is saved anywhere -- because a drawn outline would be
    STATE, state would be a document field, and a document field would reopen the
    byte-for-byte .perf format for something that can be computed. That is PLAN.md D6's
    argument about meshes and D3's about symbol positions, arriving a third time.

    What comes out is an ID that carries its own parameters, so the part travels with the
    board: mail somebody a .perf using ``box-4x2-p1-r3-15x10x8`` and it opens as the same
    part on their machine, with no library to install and nothing to go missing.

    The preview line is not decoration either. It shows the identifier that will land in
    the document, and the dialog refuses to close while the engine will not build it -- so
    a combination that cannot be a part is refused HERE, where the numbers that caused it
    are still on screen, rather than as an unknown footprint three steps later.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Custom Part"))
        self._families = _custom_families()
        self._footprint: Footprint | None = None
        self._widgets: dict[str, QWidget] = {}

        self.family = QComboBox()
        for family in self._families:
            self.family.addItem(family.label)
        self.family.currentIndexChanged.connect(self._rebuild_form)

        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 0, 0, 0)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color: {TEXT_DIM};")

        self.identifier = QLabel()
        self.identifier.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.identifier.setStyleSheet("font-family: monospace;")
        self.identifier.setToolTip(
            t(
                "The identifier this part is stored under. It carries the dimensions, so "
                "the part travels with the board rather than living in a library the next "
                "person has to have."
            )
        )

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(self.family)
        layout.addWidget(self.form_host)
        layout.addWidget(self.identifier)
        layout.addWidget(self.summary)
        layout.addWidget(self.buttons)
        self.setLayout(layout)
        self._rebuild_form()

    # -- the form ------------------------------------------------------------

    def _rebuild_form(self) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self._widgets.clear()
        for field in self._families[self.family.currentIndex()].fields:
            widget: QWidget
            if field.kind == "bool":
                box = QCheckBox()
                box.setChecked(bool(round(field.default)))
                box.toggled.connect(self._refresh)
                widget = box
            elif field.kind == "int":
                spin = QSpinBox()
                spin.setRange(int(field.minimum), int(field.maximum))
                spin.setValue(int(field.default))
                spin.valueChanged.connect(self._refresh)
                widget = spin
            else:
                dspin = QDoubleSpinBox()
                dspin.setDecimals(2)
                dspin.setSingleStep(0.5)
                dspin.setRange(field.minimum, field.maximum)
                dspin.setValue(field.default)
                dspin.valueChanged.connect(self._refresh)
                widget = dspin
            self._widgets[field.key] = widget
            self.form.addRow(field.label, widget)
        self._refresh()

    def _values(self) -> dict[str, float]:
        values: dict[str, float] = {}
        for key, widget in self._widgets.items():
            if isinstance(widget, QCheckBox):
                values[key] = 1.0 if widget.isChecked() else 0.0
            elif isinstance(widget, QSpinBox):
                values[key] = float(widget.value())
            elif isinstance(widget, QDoubleSpinBox):
                values[key] = widget.value()
        return values

    def _refresh(self) -> None:
        """Rebuild the part and say what it is, or say why there is nothing.

        The engine's own generators raise on a combination that cannot be a part -- an odd
        DIP pin count, a one-way terminal block -- and this catches that rather than
        pre-empting it, so the dialog cannot come to disagree with what the engine will
        accept when the id reaches it.
        """
        family = self._families[self.family.currentIndex()]
        try:
            footprint: Footprint | None = family.build(self._values())
        except (ValueError, KeyError):
            footprint = None
        # Built here and resolved again through the lookup, because the id is what actually
        # travels: a part this dialog can build but the grammar cannot read back is a part
        # that would vanish the next time the document was opened.
        if footprint is not None and get_footprint(footprint.id) is None:
            footprint = None
        self._footprint = footprint

        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setEnabled(footprint is not None)
        if footprint is None:
            self.identifier.setText("")
            self.summary.setText(t("Those measurements do not make a part."))
            return
        self.identifier.setText(footprint.id)
        self.summary.setText(
            t("{name} — {pins} pin(s), {height} mm tall").format(
                name=footprint.name,
                pins=len(footprint.pins),
                height=f"{footprint.body_height:g}",
            )
        )

    def chosen(self) -> Footprint | None:
        """The part described, or None. Valid once the dialog has been accepted."""
        return self._footprint


class AddPartDialog(QDialog):
    """Pick a part for the schematic: what it is, what it is called, what it is worth.

    A footprint and not a "symbol", which looks like the wrong question to ask on a
    schematic and is the right one here. This tool generates the symbol FROM the footprint
    (``schematic.symbol_kind_for``), and the footprint is what the board, the guide, the
    3D view and the BOM all need anyway — so asking for a symbol now would mean asking for
    the footprint again at placement, and leaving room for the two to disagree.

    The reference is filled in from the footprint the moment one is picked, counting the
    board AND the design, so the common case is: type two letters, press Enter twice.
    """

    def __init__(self, document: PerfDocument, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Add a Part"))
        self.resize(460, 520)
        self._document = document
        #: Every reference this dialog has proposed, so it can tell its own suggestion
        #: from something the user typed. A set rather than one value: scrolling the list
        #: proposes several before anything is chosen. Per instance, not per class -- a
        #: shared one would remember the last dialog's suggestions and overwrite a typed
        #: reference that happened to match.
        self._suggestions: set[str] = set()
        #: Parts described in this dialog, plus any the document already uses that the
        #: library does not have. Not saved anywhere: the id is the definition, so the
        #: document is already carrying everything there is to keep.
        self._custom: dict[str, Footprint] = {
            footprint.id: footprint
            for footprint in (
                get_footprint(used)
                for used in {
                    *(component.footprint_id for component in document.components),
                    *(part.footprint_id for part in document.parts),
                }
            )
            if footprint is not None and footprint.id not in standard_footprints()
        }

        self.filter = QLineEdit()
        self.filter.setPlaceholderText(t("Filter parts…  (resistor, dip-8, TO-220)"))
        self.filter.textChanged.connect(self._refilter)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda _now, _then: self._suggest_reference())
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())

        self.ref = QLineEdit()
        self.ref.setToolTip(
            t(
                "The designator this part is known by, on the schematic and on the board. "
                "It has to be free on both — the two are one namespace."
            )
        )
        self.value = QLineEdit()
        self.value.setPlaceholderText("10k, 100nF, NE555…")
        self.value.setToolTip(
            t(
                "What is printed on the part. It reaches the bill of materials, the guide's "
                "step text and a resistor's colour bands in 3D, so it is worth filling in."
            )
        )

        form = QFormLayout()
        form.addRow(t("Reference"), self.ref)
        form.addRow(t("Value"), self.value)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        self.custom = QPushButton(t("Custom Part…"))
        self.custom.setToolTip(
            t(
                "Describe a part the library does not have. It is stored as an identifier "
                "that carries its own dimensions, so it travels with the board."
            )
        )
        self.custom.clicked.connect(self.on_custom_part)

        layout = QVBoxLayout()
        layout.addWidget(self.filter)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.custom)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)
        self._refilter("")
        self.filter.setFocus()

    def on_custom_part(self) -> None:
        dialog = CustomPartDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        footprint = dialog.chosen()
        if footprint is None:
            return
        self._custom[footprint.id] = footprint
        self._refilter(self.filter.text())
        self.select_footprint(footprint.id)

    def _refilter(self, text: str) -> None:
        needle = text.strip().lower()
        self.list.clear()
        # Custom parts first and never filtered out: one was just described, or is already
        # on this board, and burying it under sixty-one library parts would make the
        # dialog's own answer the hardest thing in it to find.
        offered = list(self._custom.values()) + sorted(
            standard_footprints().values(), key=lambda f: f.name
        )
        for footprint in offered:
            custom = footprint.id in self._custom
            haystack = f"{footprint.id} {footprint.name} {footprint.body.archetype}".lower()
            if needle and not custom and needle not in haystack:
                continue
            item = QListWidgetItem(f"{footprint.name}  ·  {len(footprint.pins)} pin(s)")
            item.setData(Qt.ItemDataRole.UserRole, footprint.id)
            item.setIcon(icons.part_icon(footprint))
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _suggest_reference(self) -> None:
        """Refill the reference whenever the kind of part changes.

        Only while the field still holds a suggestion this dialog made — a reference the
        user has typed over is a decision, and overwriting it because they scrolled the
        list would throw that decision away.
        """
        footprint_id = self.chosen_footprint_id()
        if footprint_id is None:
            return
        suggested = next_reference(self._document, footprint_id)
        if self.ref.text().strip() in ("", *self._suggestions):
            self.ref.setText(suggested)
        self._suggestions.add(suggested)

    def select_footprint(self, footprint_id: str) -> None:
        """Start on this footprint, for editing a part that already has one.

        A part whose footprint is a custom one is not in the library list, so it is put
        there first. Without that, opening the properties of a part the library does not
        have would silently land on whatever happened to be first -- and pressing OK would
        change the part into a resistor.
        """
        if footprint_id not in self._custom and footprint_id not in standard_footprints():
            found = get_footprint(footprint_id)
            if found is not None:
                self._custom[footprint_id] = found
                self._refilter(self.filter.text())
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == footprint_id:
                self.list.setCurrentRow(index)
                return

    def chosen_footprint_id(self) -> str | None:
        item = self.list.currentItem()
        if item is None:
            return None
        chosen = item.data(Qt.ItemDataRole.UserRole)
        return str(chosen) if chosen is not None else None

    def values(self) -> tuple[str, str, str] | None:
        """``(reference, value, footprint id)``, or None if nothing was chosen."""
        footprint_id = self.chosen_footprint_id()
        if footprint_id is None:
            return None
        return self.ref.text().strip(), self.value.text().strip(), footprint_id


class ShortcutsDialog(QDialog):
    """Every keyboard shortcut, READ OFF THE MENU BAR rather than listed by hand.

    A hand-kept list is a list that goes stale the first time an action moves, and a stale
    shortcut card is worse than none: it teaches something that no longer works. Walking
    the real QMenuBar means this dialog cannot describe a binding the application does not
    have, and cannot miss one it does.

    The board gestures at the top are the exception, and they are the reason this dialog
    earns its place. Middle-drag to pan, right-click to finish a run, arrows to nudge a
    part a hole at a time -- none of them is an action on any menu, so until now the only
    way to find out was to read the source.
    """

    #: (gesture, what it does). Not actions, so nothing can derive them; they are listed
    #: here because they are otherwise undiscoverable.
    BOARD_GESTURES: tuple[tuple[str, str], ...] = (
        ("Middle-drag", "Pan the board"),
        ("Wheel", "Zoom about the pointer"),
        ("Drag a part", "Move it, snapping to the nearest hole"),
        ("Arrow keys", "Nudge the selected part one hole (Shift: five)"),
        ("Right-click", "Finish the trace or net being drawn"),
        ("Enter", "Finish it without moving the pointer"),
        ("Esc", "Leave the current mode"),
        ("Double-click a net", "Route just that net"),
        ("Click a DRC row", "Zoom to the holes it names"),
    )

    def __init__(self, menus: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("Keyboard Shortcuts"))
        self.resize(560, 640)

        tree = QTreeWidget()
        tree.setHeaderLabels([t("Action"), t("Shortcut")])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setIndentation(12)
        header = tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)

        gestures = QTreeWidgetItem([t("On the board"), ""])
        tree.addTopLevelItem(gestures)
        for gesture, what in self.BOARD_GESTURES:
            gestures.addChild(QTreeWidgetItem([what, gesture]))
        gestures.setExpanded(True)

        for menu_title, rows in self.menu_shortcuts(menus):
            group = QTreeWidgetItem([menu_title, ""])
            tree.addTopLevelItem(group)
            for label, keys in rows:
                group.addChild(QTreeWidgetItem([label, keys]))
            group.setExpanded(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout()
        layout.addWidget(tree)
        layout.addWidget(buttons)
        self.setLayout(layout)

    @staticmethod
    def menu_shortcuts(menus: Any) -> list[tuple[str, list[tuple[str, str]]]]:
        """(menu, [(action, keys)]) for every action carrying a shortcut.

        Takes the window's own list of QMenus -- which includes the submenus as entries of
        their own -- rather than re-deriving it from the menu bar. That is not a shortcut
        in the code: asking a menu-bar action for its ``menu()`` hands Python a QMenu it
        believes it owns, and the next garbage collection then DESTROYS the real menu. The
        first version of this dialog did exactly that and left the window with a menu bar
        of actions pointing at freed menus.

        Static, so a test can assert against a window's real bindings without opening a
        dialog to do it.
        """
        found: list[tuple[str, list[tuple[str, str]]]] = []
        for menu in menus:
            rows = _shortcut_rows(menu.actions())
            if rows:
                found.append((_plain(menu.title()), rows))
        return found


def _shortcut_rows(actions: Any) -> list[tuple[str, str]]:
    return [
        (_plain(action.text()), action.shortcut().toString())
        for action in actions
        if not action.isSeparator() and not action.shortcut().isEmpty()
    ]


def _plain(label: str) -> str:
    """A menu label as a person reads it: no accelerator marker, no trailing ellipsis."""
    return label.replace("&", "").removesuffix("…").strip()


def _preset_features(
    preset: BoardPreset | None, board: Board
) -> tuple[tuple[EdgeConnector, ...], tuple[MountingHole, ...]] | None:
    """The finger strips and corner holes the chosen product is sold with.

    None when nothing was chosen from the list, which is the signal to leave the board's
    existing features alone and merely resize -- a person who typed a column count did
    not ask for their connectors to be rebuilt.
    """
    if preset is None:
        return None
    return preset_edge_connectors(preset, board), preset_mounting_holes(preset, board)


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


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


def assembly_step_for(value: int, maximum: int) -> int | None:
    """What a position on the assembly slider means.

    The slider counts THINGS FITTED, not steps, so its two ends are the two states a
    person actually asks for: 0 is the bare board out of the envelope, and ``maximum`` is
    the finished one. Step *k* is therefore at value *k+1*.

    ``None`` means the finished board — the whole document, nothing picked out, which is
    what the panel shows when nobody has touched the slider.

    Separated from the widget because the arithmetic is where this goes wrong: the first
    version returned ``value - 1`` throughout, so the BARE-BOARD end produced -1, met the
    same "show everything" branch as the finished end, and drew a complete board at the
    position that means nothing has been fitted yet.
    """
    if value >= maximum:
        return None
    return value - 1


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
        #: The same, for the build-guide panel: building a guide runs DRC and LVS, which
        #: is not worth paying for to fill a panel nobody has open.
        self._guide_stale = True
        #: And for the schematic sheet. Drawing one is cheap next to a guide, but it
        #: rebuilds a whole QGraphicsScene, and a closed panel should cost nothing on
        #: every keystroke for the same reason the other two do not.
        self._schematic_stale = True
        #: What is selected ON THE SHEET, which is not always something the board can
        #: select: a part in the design has no board item to click. Held here rather than
        #: in the panel so that a redraw -- which throws the whole scene away -- cannot
        #: lose it.
        self._schematic_ref: str | None = None
        #: Custom parts described in this window but not yet placed. Everything already ON
        #: the board is derived from the document instead (``custom_footprints``), because
        #: the id carries the definition and a saved board therefore needs nothing
        #: remembered for it.
        self._described_here: dict[str, Footprint] = {}
        #: Assembly playback. The slider and its friends do not exist until the 3D panel
        #: is first opened, so everything that reads them checks for None first.
        self.assembly_slider: Any = None
        self._assembly_doc: PerfDocument | None = None
        self._assembly_guide: Guide | None = None
        self._assembly_cached: tuple[GuideStep, ...] = ()
        #: Advanced by "Try Another Arrangement". Held on the window rather than passed
        #: in, so pressing it repeatedly keeps exploring instead of re-running the same
        #: search and reporting the same answer.
        self._place_seed = 0
        #: Nets whose copper was laid out for a position a part has since left. See
        #: _track_moved_nets for why this is remembered rather than detected.
        self._nets_from_old_layout: set[NetId] = set()
        #: Which primitive the router should reach for first. See router.RoutingStyle, and
        #: StylePreference for the extra "best" value that measures rather than assumes.
        self._routing_style: StylePreference = "balanced"
        #: The hole under the pointer, kept because Paste lands there. Starts off the
        #: board on purpose: before the pointer has been over the board at all there is
        #: no such hole, and (0, 0) would be a lie that pasted a block into A1.
        self._hovered_hole = HoleCoord(-1, -1)
        #: The document as it last passed through the file: written by a save, filled by
        #: a load. What lets the file watcher tell somebody else's write from our own.
        self._disk_text: str | None = None
        # PLAN.md §9.3 -- the window notices when the file changes underneath it, so an
        # agent that only writes files is a participant rather than a source of stale
        # screens. See _reload_if_changed for what it does about it.
        self._file_watcher = QFileSystemWatcher(self)
        self._file_watcher.fileChanged.connect(self._on_file_changed)
        self._watch_timer = QTimer(self)
        self._watch_timer.setSingleShot(True)
        self._watch_timer.timeout.connect(self._reload_if_changed)

        #: Set once the user has ever placed a part, in any session. See _refresh_empty_hint.
        self._has_placed_a_part = _stored_bool(app_settings(), HAS_PLACED_KEY, False)

        #: The update check. Built on first use and never in this constructor: the test
        #: suite builds a great many windows and none of them should reach the network.
        #: See ui/updater.py, and consider_checking_for_updates for who starts one.
        self._update_checker: updater.UpdateChecker | None = None
        self._update_release: Release | None = None
        self._update_asked_by_hand = False
        self._downloaded_update: str | None = None

        #: The document as it last hit disk. Identity comparison against the bus's
        #: current document is what "modified" means here -- see is_modified.
        self._saved_document = document
        self.setWindowTitle(window_title(path))
        self.resize(1500, 950)
        self.setStyleSheet(STYLESHEET)
        # A board and a netlist both arrive as files, and dragging one onto the window is
        # the first thing anybody tries. See dropEvent.
        self.setAcceptDrops(True)

        self.scene = BoardScene(self.bus.document, self.lookup, side=self.side, bus=self.bus)
        self.view = BoardView(self.scene)
        self.scene.moveCommitted.connect(self.on_move_committed)
        self.scene.selectionNetsChanged.connect(self._on_selection_nets_changed)
        self.scene.placementArmed.connect(self._on_placement_armed)
        self.scene.drawArmed.connect(self._on_draw_armed)
        self.scene.conductorDrawn.connect(self._on_conductor_drawn)
        self.scene.netPinsArmed.connect(self._on_net_pins_armed)
        self.scene.netPinsChanged.connect(self._on_net_pins_changed)
        self.scene.netPinRejected.connect(self._on_net_pin_rejected)
        self.scene.netPinsCommitted.connect(self._on_net_pins_committed)
        self.scene.connectArmed.connect(self._on_connect_armed)
        self.scene.connectProgress.connect(self._on_connect_progress)
        self.scene.pinsConnected.connect(self._on_pins_connected)
        self.scene.hoveredHole.connect(self._on_hovered_hole)
        self.scene.componentPlaced.connect(self._on_component_placed)
        self.scene.componentActivated.connect(self.on_component_properties)
        self.view.contextMenuRequested.connect(self._on_board_context_menu)
        self.scene.measureArmed.connect(self._on_measure_armed)
        self.scene.measured.connect(self._on_measured)
        self.scene.cutArmed.connect(self._on_cut_armed)
        self.scene.cutMade.connect(self._on_cut_made)

        # The 2D editor is the application; the 3D view is a panel you open when you want
        # it. See _build_3d_dock for why that is not just a layout preference.
        #
        # It is wrapped rather than set directly so that the update strip can sit above
        # it: a strip in the layout pushes the board down by its own height when there is
        # news and occupies nothing when there is not, where an overlay would cover the
        # top row of holes and a dialog would land on top of whatever was being routed.
        self.update_bar = updater.UpdateBar(self)
        self.update_bar.downloadRequested.connect(self._on_update_download)
        self.update_bar.notesRequested.connect(self._on_update_notes)
        self.update_bar.revealRequested.connect(self._on_update_reveal)
        self.update_bar.cancelRequested.connect(self._on_update_cancel)
        self.update_bar.dismissed.connect(self._on_update_dismissed)
        centre = QWidget(self)
        column = QVBoxLayout(centre)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self.update_bar)
        column.addWidget(self.view, 1)
        self.setCentralWidget(centre)

        # Docks before menus: the View menu offers each dock's own toggleViewAction, so the
        # docks have to exist for the menu to be able to name them.
        self._build_library_dock()
        self._build_nets_dock()
        self._build_3d_dock()
        self._build_guide_dock()
        self._build_schematic_dock()
        self._build_drc_dock()
        # Three panels of the same width on the same edge, so they share it as tabs rather
        # than each getting a third. All start closed; whichever is opened takes the space.
        self.tabifyDockWidget(self.dock_3d, self.dock_guide)
        self.tabifyDockWidget(self.dock_guide, self.dock_schematic)
        self._build_menu()
        self._build_toolbar()
        self._build_status_bar()

        self._subscribe_bus()
        self.on_bus_changed(self.bus.document, None)
        # Last, and after the first repaint: restoring a toggle drives the scene through
        # the same handler the menu item does, and those handlers expect a window that is
        # already built.
        self._restore_session()

        # A window opened on a path -- from the command line, or from the recent list --
        # watches that file from the start, and remembers what it said. Without the text,
        # the first external write would look like a change and the first save like one
        # too.
        if self.current_path is not None:
            text, _problem = read_document_text(self.current_path)
            self._disk_text = text
            self._watch_current_path()

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
        self.dock_3d = QDockWidget(t("3D View"), self)
        self.dock_3d.setObjectName("dock3d")
        self._3d_placeholder = QLabel(t("Opening the 3D view builds it — this takes a moment."))
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
            self._3d_layout.addWidget(self._build_assembly_bar())
            self._3d_stale = False
        except Exception as exc:  # pragma: no cover - environment dependent
            print(f"[3D] Qt/VTK widget unavailable: {exc}", file=sys.stderr)
            self._vtk_renderer = None
            self._3d_placeholder.setText(f"3D view unavailable:\n{exc}")

    # -- assembly playback (PLAN.md D7) --------------------------------------
    #
    # THERE IS NO "ANIMATION MODE". The slider's maximum is the finished board, which is
    # where it sits, so the panel behaves exactly as it did before anyone touches it.
    # Dragging left rewinds the build. A mode would mean a way to be stuck in it, and a
    # second thing to remember to turn off before the view means what it looks like it
    # means.

    #: One step per this many milliseconds while playing. Slow enough to read the caption,
    #: fast enough that a 40-step board is not a chore.
    ASSEMBLY_FRAME_MS = 650

    _assembly_timer: QTimer

    def _build_assembly_bar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 2, 6, 4)

        self.act_play = QPushButton(t("Play"))
        self.act_play.setCheckable(True)
        self.act_play.setToolTip(t("Play the build from here, one step at a time."))
        self.act_play.toggled.connect(self._on_play_toggled)

        self.assembly_slider = QSlider(Qt.Orientation.Horizontal)
        self.assembly_slider.setToolTip(
            t("Drag back to see the board part-way through the build.")
        )
        self.assembly_slider.valueChanged.connect(self._on_assembly_moved)

        self.assembly_label = QLabel()
        self.assembly_label.setMinimumWidth(160)

        row.addWidget(self.act_play)
        row.addWidget(self.assembly_slider, 1)
        row.addWidget(self.assembly_label)

        self._assembly_timer = QTimer(self)
        self._assembly_timer.setInterval(self.ASSEMBLY_FRAME_MS)
        self._assembly_timer.timeout.connect(self._on_assembly_tick)

        self._sync_assembly_range()
        return bar

    def current_guide(self) -> Guide:
        """The build guide for the document as it stands, rebuilt when it changes.

        Cached on the document OBJECT, which is free to compare because documents are
        immutable and every edit produces a new one. Building it costs a couple of
        milliseconds -- it runs DRC and LVS -- which is worth paying only once per edit.

        ONE cache for both readers. The assembly slider and the Build Guide panel are two
        views of the same list, and building it twice would let them disagree about how
        many steps there are while showing the same board.
        """
        if self._assembly_doc is not self.bus.document or self._assembly_guide is None:
            self._assembly_doc = self.bus.document
            self._assembly_guide = build_guide(self.bus.document, self.lookup)
            self._assembly_cached = all_steps(self._assembly_guide)
        return self._assembly_guide

    def _assembly_steps(self) -> tuple[GuideStep, ...]:
        self.current_guide()
        return self._assembly_cached

    def _sync_assembly_range(self) -> None:
        """Fit the slider to the current build and send it to the end.

        Back to the end on every edit, deliberately. A position part-way through a build
        that no longer exists is not a position: adding a part renumbers everything after
        it, so holding the index would show a different moment than the one the user was
        looking at, without saying so.
        """
        steps = self._assembly_steps()
        blocked = self.assembly_slider.blockSignals(True)
        self.assembly_slider.setRange(0, max(1, len(steps)))
        self.assembly_slider.setValue(max(1, len(steps)))
        self.assembly_slider.setEnabled(bool(steps))
        self.assembly_slider.blockSignals(blocked)
        self.act_play.setEnabled(bool(steps))
        self._update_assembly_label()

    def _assembly_index(self) -> int | None:
        """Which step the slider is showing, or ``None`` for "the finished board".

        Asks the slider and nothing else. It deliberately does NOT check whether the 3D
        panel is live: this answer is also what the caption reads, and a caption that
        depends on whether a renderer happens to exist is a caption that lies about where
        the slider is. ``_refresh_3d`` does its own check before rendering anything.
        """
        if self.assembly_slider is None or not self.assembly_slider.isEnabled():
            return None
        return assembly_step_for(self.assembly_slider.value(), self.assembly_slider.maximum())

    def _update_assembly_label(self) -> None:
        steps = self._assembly_cached
        index = self._assembly_index()
        if index is None:
            self.assembly_label.setText(t("Finished board"))
        elif index < 0:
            self.assembly_label.setText(t("Bare board"))
        else:
            self.assembly_label.setText(f"{index + 1}/{len(steps)} · {steps[index].title}")

    def _on_assembly_moved(self, _value: int) -> None:
        self._update_assembly_label()
        self._refresh_3d()

    def _on_play_toggled(self, playing: bool) -> None:
        if playing:
            # From the beginning when the board is already finished, because "play" on a
            # complete board can only sensibly mean "show me how it got there".
            if self.assembly_slider.value() >= self.assembly_slider.maximum():
                self.assembly_slider.setValue(0)
            self._assembly_timer.start()
        else:
            self._assembly_timer.stop()
        self.act_play.setText(t("Pause") if playing else t("Play"))

    def _on_assembly_tick(self) -> None:
        if self.assembly_slider.value() >= self.assembly_slider.maximum():
            self.act_play.setChecked(False)
            return
        self.assembly_slider.setValue(self.assembly_slider.value() + 1)

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
        index = self._assembly_index()
        if index is None:
            document = self.bus.document
            highlight = None
        else:
            steps = self._assembly_steps()
            guide = self._assembly_guide
            assert guide is not None  # _assembly_steps just built it
            document = document_at_step(self.bus.document, guide, index)
            # Nothing is picked out at the bare-board end: there is no step there yet.
            highlight = step_focus(steps[index]) if 0 <= index < len(steps) else None

        view3d.populate_renderer(
            self._vtk_renderer,
            document,
            self.lookup,
            exploded_mm=view3d.EXPLODED_LIFT_MM if self.act_exploded.isChecked() else 0.0,
            highlight=highlight,
        )
        self.vtk_widget.GetRenderWindow().Render()
        self._3d_stale = False

    def on_toggle_exploded(self) -> None:
        """Lift the parts off the board, or set them back down.

        The camera is left where it is on purpose, as everywhere else in this view: a
        person who has just framed the corner they care about does not want exploding the
        board to throw that away. The parts rise in place and the leader lines show where
        each one goes back down to.
        """
        self._refresh_3d()

    def on_reset_3d_camera(self) -> None:
        if self._vtk_renderer is None:
            return
        view3d.apply_default_camera(self._vtk_renderer, flipped=(self.side == "bottom"))
        if self.vtk_widget is not None:
            self.vtk_widget.GetRenderWindow().Render()

    # -- menu ------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu(t("&File"))
        act_new = file_menu.addAction(t("&New Board…"))
        act_new.setShortcut(QKeySequence.StandardKey.New)
        act_new.triggered.connect(self.on_new)
        act_open = file_menu.addAction(t("&Open…"))
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self.on_open)
        # Between Open and Save, where every editor puts it. A perfboard project is worked
        # on across evenings, and hunting the same file out of a directory tree every time
        # is friction the application was adding for no reason.
        self.menu_recent = file_menu.addMenu(t("Open &Recent"))
        self._refresh_recent_menu()
        act_save = file_menu.addAction(t("&Save"))
        act_save.setShortcut(QKeySequence.StandardKey.Save)
        act_save.triggered.connect(self.on_save)
        self.act_save = act_save
        act_save_as = file_menu.addAction(t("Save &As…"))
        act_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        act_save_as.triggered.connect(self.on_save_as)
        # The manual half of the file watcher. A window with unsaved edits is never
        # reloaded behind the user's back, so there has to be a way to say "take the
        # file's version" -- which is what an agent editing the same board needs.
        act_reload = file_menu.addAction(t("Re&load from Disk"))
        act_reload.setShortcut(QKeySequence("F5"))
        act_reload.setToolTip(
            t(
                "Load the file again, discarding what is in this window. The board reloads "
                "itself automatically when it changes on disk and there is nothing unsaved."
            )
        )
        act_reload.triggered.connect(self.on_reload)
        file_menu.addSeparator()
        # Held on the window because the board's own context menu offers it: right-clicking
        # bare board and being told how to change that board is the obvious thing for a
        # right-click there to do.
        act_board = self.act_board_setup = file_menu.addAction(t("&Board Setup…"))
        act_board.setToolTip(
            t(
                "Grid size and substrate. The material is not cosmetic: it decides the iron "
                "temperature the build guide gives and whether the pad-lifting rule applies."
            )
        )
        act_board.triggered.connect(self.on_board_setup)
        act_features = file_menu.addAction(t("Board &Features…"))
        act_features.setToolTip(
            t(
                "Mounting holes and edge-connector fingers. A mounting bore takes the copper "
                "off the pads around it, so DRC treats a pin left there as an error."
            )
        )
        act_features.triggered.connect(self.on_board_features)
        act_import = file_menu.addAction(t("&Import KiCad Netlist…"))
        self.act_import = act_import
        act_import.setShortcut(QKeySequence("Ctrl+I"))
        act_import.triggered.connect(self.on_import_netlist)
        file_menu.addSeparator()
        act_guide = file_menu.addAction(t("Export &Build Guide…"))
        self.act_guide = act_guide
        act_guide.setShortcut(QKeySequence("Ctrl+B"))
        act_guide.setToolTip(
            t(
                "Write the step-by-step soldering guide: one offline HTML file, the wire cut "
                "list and BOM as CSV, and the whole thing as JSON."
            )
        )
        act_guide.triggered.connect(self.on_export_guide)
        act_sheet = file_menu.addAction(t("Export Sc&hematic…"))
        self.act_export_schematic = act_sheet
        act_sheet.setToolTip(
            t(
                "Write the circuit as a sheet: SVG to embed or edit, PDF to print, PNG to "
                "paste into a message asking somebody what is wrong with it."
            )
        )
        act_sheet.triggered.connect(self.on_export_schematic)
        act_pdf = file_menu.addAction(t("Export 1:1 PDF (component + solder side)…"))
        act_pdf.triggered.connect(self.on_export_pdf)
        act_png = file_menu.addAction(t("Export 3D Snapshot PNG…"))
        act_png.triggered.connect(self.on_export_3d_png)
        file_menu.addSeparator()
        act_quit = file_menu.addAction(t("&Quit"))
        # Ctrl+Q rather than StandardKey.Quit: on Windows that standard key resolves to no
        # usable binding at all -- it reports itself as "Exit", a key almost no keyboard
        # has -- so Quit had no shortcut on the platform this is developed on. Qt maps
        # Ctrl to Cmd on macOS, so spelling it out costs nothing there.
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)

        edit_menu = menu.addMenu(t("&Edit"))
        # Held on the window because their enabled state is now kept in step with the
        # history: an Undo that is greyed out says "there is nothing behind you" without
        # anyone having to press it to find out.
        self.act_undo = edit_menu.addAction(t("&Undo"))
        self.act_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self.act_undo.triggered.connect(self.on_undo)
        self.act_redo = edit_menu.addAction(t("&Redo"))
        self.act_redo.setShortcut(QKeySequence("Ctrl+Shift+Z"))
        self.act_redo.triggered.connect(self.on_redo)
        edit_menu.addSeparator()

        # A perfboard project repeats itself in a way a PCB does not -- eight identical
        # channels, the same RC pair at every op-amp -- and until this the only way to
        # build the second one was to place every part again by hand.
        edit_menu.setToolTipsVisible(True)
        self.act_copy = edit_menu.addAction(t("Cop&y"))
        self.act_copy.setShortcut(QKeySequence.StandardKey.Copy)
        self.act_copy.setToolTip(
            t(
                "Put the selected parts and copper on the clipboard as text, so a block can "
                "be pasted into another board, another window, or a bug report."
            )
        )
        self.act_copy.triggered.connect(self.on_copy)
        self.act_paste = edit_menu.addAction(t("&Paste"))
        self.act_paste.setShortcut(QKeySequence.StandardKey.Paste)
        self.act_paste.setToolTip(
            t(
                "Place the clipboard's block under the pointer. New references, no net "
                "claim: a copy of R1 is not R1, and its copper is not on R1's net."
            )
        )
        self.act_paste.triggered.connect(self.on_paste)
        self.act_duplicate = edit_menu.addAction(t("Dupl&icate"))
        self.act_duplicate.setShortcut(QKeySequence("Ctrl+D"))
        self.act_duplicate.setToolTip(
            t(
                "Copy and paste the selection in one step, beside itself and without "
                "touching the clipboard."
            )
        )
        self.act_duplicate.triggered.connect(self.on_duplicate)
        edit_menu.addSeparator()

        # The engine has had component.rotate and component.mirror since the first commit
        # and nothing in the window could reach them, so a part could only ever be placed
        # in the orientation it arrived in. Placing a DIP or an electrolytic without turning
        # it is not a real workflow.
        self.act_rotate_cw = edit_menu.addAction(t("Rotate &Clockwise"))
        self.act_rotate_cw.setShortcut(QKeySequence("R"))
        self.act_rotate_cw.triggered.connect(lambda: self.on_rotate_selection(90))
        self.act_rotate_ccw = edit_menu.addAction(t("Rotate Counter-clock&wise"))
        self.act_rotate_ccw.setShortcut(QKeySequence("Shift+R"))
        self.act_rotate_ccw.triggered.connect(lambda: self.on_rotate_selection(-90))
        self.act_mirror = edit_menu.addAction(t("&Mirror"))
        self.act_mirror.setShortcut(QKeySequence("M"))
        self.act_mirror.triggered.connect(self.on_mirror_selection)
        self.act_lock = edit_menu.addAction(t("Toggle &Lock"))
        self.act_lock.setShortcut(QKeySequence("L"))
        self.act_lock.triggered.connect(self.on_toggle_lock_selection)
        self.act_delete = edit_menu.addAction(t("&Delete"))
        self.act_delete.setShortcut(QKeySequence.StandardKey.Delete)
        self.act_delete.triggered.connect(self.on_delete_selection)
        edit_menu.addSeparator()
        # F2 as well as the double-click, because renaming is what F2 means in every file
        # manager and half the editors -- and because a part small enough to be fiddly to
        # double-click is exactly the one whose value nobody has typed yet.
        self.act_properties = edit_menu.addAction(t("Proper&ties…"))
        self.act_properties.setShortcut(QKeySequence("F2"))
        self.act_properties.setToolTip(
            t(
                "The part's reference and value. The value is what the build guide's bill "
                "of materials groups on, and nothing else in the window can set it."
            )
        )
        self.act_properties.triggered.connect(lambda: self.on_component_properties())

        #: Actions that act on the selection, so they can be greyed out when there is none.
        #: A menu item that silently does nothing is indistinguishable from a broken one.
        # Copy and Duplicate are here and Paste is not: Paste depends on what is on the
        # clipboard, which can change while this window is not looking, and an action
        # greyed out on a stale answer is worse than one that explains itself when pressed.
        self.selection_actions = (
            self.act_rotate_cw,
            self.act_rotate_ccw,
            self.act_mirror,
            self.act_lock,
            self.act_delete,
            self.act_copy,
            self.act_duplicate,
            self.act_properties,
        )
        for action in self.selection_actions:
            action.setEnabled(False)

        draw_menu = menu.addMenu(t("&Draw"))
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
            action = draw_menu.addAction(t(label))
            action.setCheckable(True)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.setToolTip(tip)
            action.triggered.connect(lambda checked, k=kind: self.on_draw_mode(k, checked))
            self.act_draw[kind] = action
        draw_menu.addSeparator()
        # The stripboard edit. It is in Draw because it is a board mode with the same
        # shape as the others -- arm it, click holes, Esc leaves -- even though it is the
        # only one that takes copper away rather than adding it.
        self.act_cut = draw_menu.addAction(t("&Cut Track"))
        self.act_cut.setCheckable(True)
        self.act_cut.setShortcut(QKeySequence("X"))
        self.act_cut.setToolTip(
            t(
                "Break the strip at a hole. The cut is drilled through the pad, so that hole "
                "has nothing to solder to afterwards — click a cut again to take it back."
            )
        )
        self.act_cut.triggered.connect(self.on_cut_mode)
        draw_menu.addSeparator()
        # Not "stop drawing" any more, because Escape has to leave whichever mode you are
        # in -- and placing a part is the one it silently did not. See on_stop_tool.
        act_stop_draw = draw_menu.addAction(t("&Stop the Current Tool"))
        act_stop_draw.setShortcut(QKeySequence("Escape"))
        act_stop_draw.setToolTip(
            t("Leave any board mode: placing a part, drawing a conductor, connecting pins.")
        )
        act_stop_draw.triggered.connect(self.on_stop_tool)

        place_menu = menu.addMenu(t("&Place"))
        self.act_autoplace = place_menu.addAction(t("&Auto-place Board"))
        self.act_autoplace.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self.act_autoplace.setToolTip(
            t(
                "Rearrange the unlocked parts to shorten the connections and make them "
                "solderable as traces rather than wires. Shows the result before applying it."
            )
        )
        self.act_autoplace.triggered.connect(lambda: self.on_autoplace())
        act_reroll = place_menu.addAction(t("&Try Another Arrangement"))
        act_reroll.setToolTip(
            t(
                "Search again from a different seed. Annealing is a random walk, so this is "
                "a real second answer rather than the same one twice."
            )
        )
        act_reroll.triggered.connect(lambda: self.on_autoplace(reroll=True))

        # A net could only ever arrive from a KiCad netlist, which quietly made a
        # schematic capture package a prerequisite for the ratsnest -- and so for
        # autoroute, LVS and the guide's continuity tests. This menu is the same intent,
        # entered by hand: name the net, then click the pins that are on it.
        net_menu = menu.addMenu(t("&Net"))
        net_menu.setToolTipsVisible(True)
        # FIRST, and on the toolbar, because it is the whole job in two clicks. Everything
        # below it exists for the cases this cannot cover -- naming a net before it has
        # pins, saying what it carries, taking a pin off one.
        self.act_connect = net_menu.addAction(t("&Connect Two Pins"))
        self.act_connect.setCheckable(True)
        self.act_connect.setShortcut(QKeySequence("C"))
        self.act_connect.setToolTip(
            t(
                "Click one pin, then another. They end up on the same net: an existing one if "
                "either pin is already on it, or a new one named for you if neither is."
            )
        )
        self.act_connect.triggered.connect(self.on_connect_tool)
        net_menu.addSeparator()
        act_new_net = net_menu.addAction(t("&New Net…"))
        self.act_new_net = act_new_net
        act_new_net.setShortcut(QKeySequence("Ctrl+Shift+N"))
        act_new_net.setToolTip(
            t("Name a net, then click its pins on the board. Nothing here needs KiCad.")
        )
        act_new_net.triggered.connect(self.on_new_net)
        self.act_add_pins = net_menu.addAction(t("&Add Pins to Net"))
        self.act_add_pins.setShortcut(QKeySequence("P"))
        self.act_add_pins.setToolTip(
            t(
                "Click each pin that belongs to the selected net. Right-click or Enter "
                "finishes, and the whole session goes on the history as one step."
            )
        )
        self.act_add_pins.triggered.connect(self.on_add_pins_to_net)
        self.act_finish_pins = net_menu.addAction(t("&Finish Adding Pins"))
        self.act_finish_pins.triggered.connect(self.on_finish_adding_pins)
        self.act_finish_pins.setEnabled(False)
        net_menu.addSeparator()
        self.act_edit_net = net_menu.addAction(t("&Edit Net…"))
        self.act_edit_net.setToolTip(
            t(
                "Name, class, and the current and voltage it carries — which nothing else in "
                "the application can set, and which DRC's capacity and creepage rules need."
            )
        )
        self.act_edit_net.triggered.connect(self.on_edit_net)
        self.act_disconnect_pins = net_menu.addAction(t("&Disconnect Selected Pins"))
        self.act_disconnect_pins.setToolTip(
            t(
                "Take the pins selected in the Nets panel off their net. Expand a net to "
                "see them."
            )
        )
        self.act_disconnect_pins.triggered.connect(self.on_disconnect_pins)
        self.act_delete_net = net_menu.addAction(t("De&lete Net"))
        self.act_delete_net.setToolTip(
            t(
                "Forget what the net was for. Copper already laid for it stays on the board, "
                "and stops being anything re-route or the stale sweep will touch."
            )
        )
        self.act_delete_net.triggered.connect(self.on_delete_net)
        #: Everything that needs a net picked in the Nets panel.
        self.net_actions = (
            self.act_add_pins,
            self.act_edit_net,
            self.act_disconnect_pins,
            self.act_delete_net,
        )
        for action in self.net_actions:
            action.setEnabled(False)

        route_menu = menu.addMenu(t("&Route"))
        self.act_autoroute = route_menu.addAction(t("&Autoroute All Nets"))
        self.act_autoroute.setShortcut(QKeySequence("Ctrl+R"))
        self.act_autoroute.triggered.connect(self.on_autoroute_all)
        self.act_route_selected = route_menu.addAction(t("Route Nets of &Selection"))
        self.act_route_selected.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self.act_route_selected.triggered.connect(self.on_route_selection)
        # HOW the board gets built is the builder's call, not the tool's. The default cost
        # table encodes one opinion and on a populated board it comes out as wire almost
        # everywhere -- 4 traces against 10 wires on the NE555 fixture -- because R5'
        # proximity risk at 12 a hole prices a trace out exactly where traces are wanted.
        # See router.RoutingStyle for the measurements.
        style_menu = route_menu.addMenu(t("&Preferred Connection"))
        style_menu.setToolTipsVisible(True)
        self.act_style: dict[str, QAction] = {}
        for style, label, tip in (
            ("best", t("&Try each and keep the best"),
             "Route the board once with every style, measure what each would cost to "
             "build -- traces, wires, wire length, bridging risk -- and keep the best. "
             "Takes about as long as two ordinary routes; the comparison is reported."),
            ("solder", t("&Solder trace where possible"),
             "Every connection a solder trace can make, IS one -- wire only where a trace "
             "physically cannot get there. A short jumper carries a run over anything it "
             "must cross. On the NE555 fixture: all 14 connections, not one wire."),
            ("balanced", t("&Balanced"),
             "No commitment: weigh each primitive on its own cost and take the cheapest "
             "each time. The default, and what every golden fixture is routed with. On a "
             "populated board this comes out as wire far more often than people expect."),
            ("wire", t("&Wire where possible"),
             "For anyone assembling with wire: every connection a wire can make is a wire, "
             "including the rails. Solder only where a wire cannot reach."),
            ("lead-bend", t("Bend component &legs where possible"),
             "Fold a component's own leg to a nearby hole first, then solder, then wire. "
             "The cheapest connection there is -- no wire to cut, and already soldered at "
             "one end."),
        ):
            action = style_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(style == self._routing_style)
            action.setToolTip(tip)
            action.triggered.connect(lambda _checked, s=style: self.on_routing_style(s))
            self.act_style[style] = action

        route_menu.addSeparator()
        # Rip-up and re-route is a SEPARATE verb from autoroute, and deliberately not what
        # Ctrl+R does: autoroute completes a board, this one discards work to rebuild it.
        # See autoroute.ReroutePlan for the measurement that made it necessary.
        self.act_reroute_all = route_menu.addAction(t("Re-route &Everything"))
        self.act_reroute_all.setToolTip(
            t(
                "Rip up the existing routing and plan it again from nothing. Use this after "
                "moving parts: autoroute only adds, so it leaves the copper laid out for "
                "where things used to be. Hand-drawn copper with no net is never touched."
            )
        )
        self.act_reroute_all.triggered.connect(lambda: self.on_reroute(None))
        self.act_reroute_selected = route_menu.addAction(t("Re-route Nets of Se&lection"))
        self.act_reroute_selected.setShortcut(QKeySequence("Ctrl+Alt+R"))
        self.act_reroute_selected.triggered.connect(self.on_reroute_selection)
        route_menu.addSeparator()
        self.act_clear_strays = route_menu.addAction(t("Remove S&tale Conductors"))
        self.act_clear_strays.triggered.connect(self.on_clear_strays)

        view_menu = menu.addMenu(t("&View"))
        act_flip = view_menu.addAction(t("Flip Board (component / solder side)"))
        self.act_flip = act_flip
        act_flip.setShortcut(QKeySequence("Ctrl+F"))
        act_flip.triggered.connect(self.on_flip_board)
        view_menu.addSeparator()
        act_fit: QAction = view_menu.addAction(t("&Fit Board"))
        self.act_fit = act_fit
        act_fit.setShortcut(QKeySequence("Ctrl+0"))
        act_fit.triggered.connect(self.view.fit_board)
        act_zoom_in = view_menu.addAction(t("Zoom &In"))
        act_zoom_in.setShortcut(QKeySequence.StandardKey.ZoomIn)
        act_zoom_in.triggered.connect(lambda: self.view.zoom_by(1.25))
        act_zoom_out = view_menu.addAction(t("Zoom &Out"))
        act_zoom_out.setShortcut(QKeySequence.StandardKey.ZoomOut)
        act_zoom_out.triggered.connect(lambda: self.view.zoom_by(1 / 1.25))
        view_menu.addSeparator()

        self.act_ratsnest = view_menu.addAction(t("Show &Ratsnest"))
        self.act_ratsnest.setCheckable(True)
        self.act_ratsnest.setChecked(True)
        self.act_ratsnest.setShortcut(QKeySequence("Ctrl+E"))
        self.act_ratsnest.toggled.connect(self.scene.set_show_ratsnest)
        self.act_rulers = view_menu.addAction(t("Show Hole &Addresses"))
        self.act_rulers.setCheckable(True)
        self.act_rulers.setChecked(True)
        self.act_rulers.toggled.connect(self.scene.set_show_rulers)
        # ON by default, because the board is opaque: copper on the far face drawn solid
        # reads as copper in front of you, and that is how a board gets soldered on the
        # wrong side. A toggle rather than a fixed rule because someone tracing a dense
        # solder side may simply want to see it plainly.
        self.act_hatch = view_menu.addAction(t("&Hatch Copper on the Far Side"))
        self.act_hatch.setCheckable(True)
        self.act_hatch.setChecked(True)
        self.act_hatch.setToolTip(
            t(
                "Draw conductors on the face you are NOT looking at as hatched, the way a part "
                "on the far side already is. Turn it off to see them solid."
            )
        )
        self.act_hatch.toggled.connect(self.scene.set_hatch_far_side)

        # Perfboard work is full of distances that have to be right before anything is
        # soldered -- how far apart to bend a resistor's legs, whether a TO-220 clears
        # the capacitor beside it -- and every one of them was countable off the ruler
        # and none of them readable. The tool changes nothing on the board, which is why
        # it is in View rather than Draw.
        self.act_measure = view_menu.addAction(t("Measure &Distance"))
        self.act_measure.setCheckable(True)
        self.act_measure.setShortcut(QKeySequence("Ctrl+M"))
        self.act_measure.setToolTip(
            t(
                "Click two holes. Says how many holes across they are, how far apart in mm, "
                "and how many steps of solder trace it would take to join them — three "
                "different numbers that answer three different questions."
            )
        )
        self.act_measure.toggled.connect(self.on_measure_mode)

        act_go_to = view_menu.addAction(t("&Go to Part…"))
        act_go_to.setShortcut(QKeySequence("Ctrl+G"))
        act_go_to.setToolTip(
            t(
                "Find a part by reference, value or footprint and centre the view on it. "
                "On a dense board there is otherwise no way to answer “where is R37”."
            )
        )
        act_go_to.triggered.connect(self.on_go_to_part)
        view_menu.addSeparator()

        self.act_3d = self.dock_3d.toggleViewAction()
        self.act_3d.setText(t("Show &3D View"))
        self.act_3d.setShortcut(QKeySequence("Ctrl+3"))
        self.act_3d.setToolTip(
            t(
                "Open the 3D board view (Ctrl+3). Closed by default: it is the "
                "most expensive thing in the window to keep up to date."
            )
        )
        view_menu.addAction(self.act_3d)
        self.act_guide_panel = self.dock_guide.toggleViewAction()
        self.act_guide_panel.setText(t("Show &Build Guide"))
        self.act_guide_panel.setShortcut(QKeySequence("Ctrl+4"))
        self.act_guide_panel.setToolTip(
            t(
                "The soldering order, in the window: shortest part first, jumpers before "
                "whatever stands on them, ICs last. Picking a step shows it on the board."
            )
        )
        view_menu.addAction(self.act_guide_panel)
        self.act_schematic = self.dock_schematic.toggleViewAction()
        self.act_schematic.setText(t("Show &Schematic"))
        self.act_schematic.setShortcut(QKeySequence("Ctrl+5"))
        self.act_schematic.setToolTip(
            t(
                "The netlist drawn as a circuit, generated from the document. Clicking a "
                "symbol selects that part on the board; clicking a wire highlights its net."
            )
        )
        view_menu.addAction(self.act_schematic)
        self.act_exploded = view_menu.addAction(t("&Exploded View"))
        self.act_exploded.setCheckable(True)
        self.act_exploded.setToolTip(
            t("Lift every part off the board, with a line down to the holes it goes in.")
        )
        self.act_exploded.toggled.connect(self.on_toggle_exploded)
        act_reset_3d = view_menu.addAction(t("Reset 3D &Camera"))
        act_reset_3d.triggered.connect(self.on_reset_3d_camera)

        view_menu.addSeparator()
        # The interface could be told to speak Turkish only by an environment variable or
        # a command-line flag -- which is to say, not by anybody running the application
        # the way applications are run. The catalogue has existed the whole time.
        language_menu = view_menu.addMenu(t("&Language"))
        self.act_language: dict[str, QAction] = {}
        for code, label in (("en", t("English")), ("tr", t("Turkish"))):
            action = language_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(code == current_language())
            action.triggered.connect(lambda _checked, c=code: self.on_language(c))
            self.act_language[code] = action

        view_menu.addSeparator()
        # Solder mask colour changes nothing about the circuit, so it is a view setting
        # rather than a document field -- see ui/boardcolors.py. Offered because someone
        # matching what is on screen to the board in their hand should be able to.
        colour_menu = view_menu.addMenu(t("Board &Colour"))
        self.act_colour: dict[str, QAction] = {}
        follow = colour_menu.addAction(t("Follow the &material"))
        follow.setCheckable(True)
        follow.setChecked(True)
        follow.setToolTip(
            t(
                "Green for FR-4 and brown for phenolic, which is what those substrates "
                "actually look like."
            )
        )
        follow.triggered.connect(lambda: self.on_board_colour(None))
        self.act_colour[""] = follow
        colour_menu.addSeparator()
        for scheme in BOARD_SCHEMES:
            action = colour_menu.addAction(t(scheme.label))
            action.setCheckable(True)
            action.triggered.connect(lambda _c, k=scheme.key: self.on_board_colour(k))
            self.act_colour[scheme.key] = action

        help_menu = menu.addMenu(t("&Help"))
        # EVERY menu is kept referenced here, and it is not tidiness. QMenuBar.addMenu
        # returns a QMenu that PySide hands to Python to own; with only a local holding it,
        # the garbage collector is free to destroy the C++ menu the moment this method
        # returns, and what is left in the menu bar is an action pointing at nothing. It
        # survived this long because nothing ever walked the menu bar afterwards -- the
        # shortcut card does, and found a destroyed QMenu on the first attempt.
        self._menus = [
            file_menu, self.menu_recent, edit_menu, draw_menu, place_menu, net_menu,
            route_menu, style_menu, view_menu, language_menu, colour_menu, help_menu,
        ]
        act_keys = help_menu.addAction(t("&Keyboard Shortcuts…"))
        act_keys.setShortcut(QKeySequence("F1"))
        act_keys.setToolTip(
            t(
                "Every binding, read off this menu bar — plus the board gestures, which are "
                "on no menu and were previously only in the source."
            )
        )
        act_keys.triggered.connect(self.on_shortcuts)
        help_menu.addSeparator()
        act_updates = help_menu.addAction(t("Check for &Updates…"))
        act_updates.setToolTip(t("Ask GitHub whether a newer release has been published."))
        act_updates.triggered.connect(self.on_check_for_updates)
        self.act_auto_updates = help_menu.addAction(t("Check Automatically at &Startup"))
        self.act_auto_updates.setCheckable(True)
        self.act_auto_updates.setChecked(bool(updater.stored_preference(app_settings())))
        self.act_auto_updates.setToolTip(
            t(
                "Look once a day, as the window opens. Nothing is downloaded or installed "
                "without you asking for it."
            )
        )
        self.act_auto_updates.toggled.connect(self._on_automatic_updates_toggled)
        help_menu.addSeparator()
        act_about = help_menu.addAction(t("&About PerfStudio"))
        act_about.triggered.connect(self.on_about)

    def _build_toolbar(self) -> None:
        """Every tool, on the bar, with a picture and its name under it.

        This replaced a row of words. Words are precise, and they are also a row of eleven
        identical grey rectangles you have to read left to right every time -- so the tools
        were being hunted for in the menus instead, which is the thing a toolbar exists to
        prevent. The icons are drawn in ``icons.py`` and the label stays underneath each
        one: a picture alone would make the specific actions ("flip to the solder side",
        "route only the selected nets") into guesses.

        The four conductor icons deliberately share one drawing and differ only in what
        runs between the two pads, because that difference IS the application.
        """
        bar = QToolBar("Main")
        # Named for the same reason the docks are: restoreState puts an unnamed toolbar
        # back wherever it likes.
        bar.setObjectName("mainToolbar")
        bar.setMovable(False)
        bar.setIconSize(QSize(icons.SIZE, icons.SIZE))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.addToolBar(bar)
        self.toolbar = bar

        # Short labels for the BUTTONS only. Qt draws an action's iconText on a toolbar and
        # its text in a menu, so "Autoroute All Nets" stays exact where there is room for
        # it and the button says "Autoroute" -- without which sixteen tools at full menu
        # length run off the end of a 1600 px window and half of them end up behind the
        # overflow arrow, which is where the menus already were.
        for action, short in (
            (self.act_connect, t("Connect")),
            (self.act_new_net, t("New Net")),
            (self.act_draw["solder-trace"], t("Trace")),
            (self.act_draw["solder-trace-wired"], t("Spine")),
            (self.act_draw["bare-wire"], t("Bare")),
            (self.act_draw["insulated-wire"], t("Insulated")),
            (self.act_draw["top-jumper"], t("Jumper")),
            (self.act_autoplace, t("Auto-place")),
            (self.act_autoroute, t("Autoroute")),
            (self.act_rotate_cw, t("Rotate")),
            (self.act_mirror, t("Mirror")),
            (self.act_delete, t("Delete")),
            (self.act_flip, t("Flip")),
            (self.act_ratsnest, t("Ratsnest")),
            (self.act_3d, t("3D")),
            (self.act_fit, t("Fit")),
        ):
            action.setIconText(short)

        # The document, then what makes a circuit, then what turns it into a board, then
        # how you look at it. Grouped in the order a board is actually built.
        self.act_save.setIcon(icons.icon("save"))
        self.act_undo.setIcon(icons.icon("undo"))
        self.act_redo.setIcon(icons.icon("redo"))
        bar.addAction(self.act_save)
        bar.addAction(self.act_undo)
        bar.addAction(self.act_redo)

        bar.addSeparator()
        # The netlist, which had been the hardest thing in the application to reach: two
        # levels of menu, then a dialog, before a single pin could be joined to anything.
        self.act_connect.setIcon(icons.icon("connect"))
        self.act_new_net.setIcon(icons.icon("new-net"))
        bar.addAction(self.act_connect)
        bar.addAction(self.act_new_net)

        bar.addSeparator()
        for kind in ("solder-trace", "solder-trace-wired", "bare-wire", "insulated-wire",
                     "top-jumper"):
            self.act_draw[kind].setIcon(icons.icon(kind))
            bar.addAction(self.act_draw[kind])

        bar.addSeparator()
        self.act_autoplace.setIcon(icons.icon("autoplace"))
        self.act_autoroute.setIcon(icons.icon("autoroute"))
        bar.addAction(self.act_autoplace)
        bar.addAction(self.act_autoroute)

        bar.addSeparator()
        self.act_rotate_cw.setIcon(icons.icon("rotate"))
        self.act_mirror.setIcon(icons.icon("mirror"))
        self.act_delete.setIcon(icons.icon("delete"))
        bar.addAction(self.act_rotate_cw)
        bar.addAction(self.act_mirror)
        bar.addAction(self.act_delete)

        bar.addSeparator()
        self.act_flip.setIcon(icons.icon("flip"))
        self.act_ratsnest.setIcon(icons.icon("ratsnest"))
        self.act_3d.setIcon(icons.icon("3d"))
        self.act_fit.setIcon(icons.icon("fit"))
        bar.addAction(self.act_flip)
        bar.addAction(self.act_ratsnest)
        bar.addAction(self.act_3d)
        bar.addAction(self.act_fit)

        # The menu entries carry the same pictures. A toolbar that teaches one icon and a
        # menu that shows another teaches nothing.
        self.act_import.setIcon(icons.icon("import"))
        self.act_guide.setIcon(icons.icon("guide"))

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
        self.library_filter.setPlaceholderText(t("Filter parts…  (resistor, dip, 5mm)"))
        self.library_filter.setClearButtonEnabled(True)
        self.library_filter.textChanged.connect(self._refresh_library)
        layout.addWidget(self.library_filter)

        # Typed once and kept, because a board is placed in runs: five 10k resistors, then
        # three 100nF. Naming each part afterwards through its own dialog is the same work
        # done once per part instead of once per run -- and a value nobody types is a bill
        # of materials nobody can order from.
        self.library_value = QLineEdit()
        self.library_value.setPlaceholderText(t("Value for parts placed now…  (10k, 100nF)"))
        self.library_value.setClearButtonEnabled(True)
        self.library_value.setToolTip(
            t(
                "Given to each part as it is placed. Leave it blank and the part is placed "
                "without one; F2 sets it afterwards either way."
            )
        )
        self.library_value.textChanged.connect(self._on_placement_value_changed)
        layout.addWidget(self.library_value)

        self.library_tree = QTreeWidget()
        self.library_tree.setHeaderLabels([t("Part"), t("Pins")])
        self.library_tree.setRootIsDecorated(True)
        self.library_tree.setIconSize(QSize(icons.PART_SIZE, icons.PART_SIZE))
        # Tighter than Qt's default, to pay for the icons: a picture and an indent both
        # come out of the same column, and the name is what gets elided when they win.
        self.library_tree.setIndentation(12)
        header = self.library_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.library_tree.itemSelectionChanged.connect(self._on_library_selection_changed)
        layout.addWidget(self.library_tree)

        self.button_custom_part = QPushButton(t("Custom Part…"))
        self.button_custom_part.setToolTip(
            t(
                "Describe a part the library does not have. It is stored as an identifier "
                "that carries its own dimensions, so it travels with the board."
            )
        )
        self.button_custom_part.clicked.connect(self.on_custom_part)
        layout.addWidget(self.button_custom_part)

        self.label_place_hint = QLabel(t("Pick a part, then click the board. Esc cancels."))
        self.label_place_hint.setWordWrap(True)
        self.label_place_hint.setStyleSheet(f"color: {TEXT_DIM};")
        layout.addWidget(self.label_place_hint)

        dock = QDockWidget(t("Parts"), self)
        # Named because QMainWindow.restoreState matches docks BY objectName and silently
        # drops the ones with none -- a layout that half restores is worse than one that
        # does not, because only half of it looks wrong.
        dock.setObjectName("dockParts")
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.dock_library = dock
        self._refresh_library()

    def custom_footprints(self) -> dict[str, Footprint]:
        """Every part on this board that the library does not have, plus any described in
        this session.

        DERIVED FROM THE DOCUMENT rather than remembered, which is what makes it survive
        opening a board somebody else drew: a custom part is an id that carries its own
        dimensions, so a file using one is already carrying the whole definition and there
        is nothing for this window to have been told. ``_described_here`` only holds the
        ones defined but not yet placed, which the document cannot know about yet.
        """
        document = self.bus.document
        used = {
            *(component.footprint_id for component in document.components),
            *(part.footprint_id for part in document.parts),
        }
        found = dict(self._described_here)
        for footprint_id in used:
            if footprint_id in standard_footprints() or footprint_id in found:
                continue
            footprint = get_footprint(footprint_id)
            if footprint is not None:
                found[footprint_id] = footprint
        return found

    def on_custom_part(self) -> None:
        """Describe a part, then arm it for placement like any other.

        Armed straight away because the reason somebody opens this dialog is that they are
        holding the part -- so the next thing they want is to put it on the board, not to
        find it again in a list.
        """
        dialog = CustomPartDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        footprint = dialog.chosen()
        if footprint is None:
            return
        self._described_here[footprint.id] = footprint
        self.library_filter.clear()
        self._refresh_library()
        self._select_library_footprint(footprint.id)

    def _select_library_footprint(self, footprint_id: str) -> None:
        tree = self.library_tree
        for index in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(index)
            if group is None:
                continue
            for child_index in range(group.childCount()):
                leaf = group.child(child_index)
                if leaf is not None and leaf.data(0, ROLE_FOOTPRINT_ID) == footprint_id:
                    group.setExpanded(True)
                    tree.setCurrentItem(leaf)
                    return

    def _refresh_library(self) -> None:
        needle = self.library_filter.text().strip().lower()
        tree = self.library_tree
        tree.blockSignals(True)
        tree.clear()
        by_archetype: dict[str, list[Footprint]] = {}
        custom = self.custom_footprints()
        for footprint in sorted(standard_footprints().values(), key=lambda f: f.name):
            haystack = f"{footprint.id} {footprint.name} {footprint.body.archetype}".lower()
            if needle and needle not in haystack:
                continue
            by_archetype.setdefault(footprint.body.archetype, []).append(footprint)

        # In a group of their own rather than filed under their archetype, and first. A
        # custom DIP-22 among the library's DIPs is the one part in the dock that cannot be
        # found by knowing what it is, because the only thing that distinguishes it is that
        # the library does not have it.
        if custom:
            ordered = sorted(custom.values(), key=lambda f: f.name)
            group = QTreeWidgetItem([t("this board"), ""])
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            group.setIcon(0, icons.part_icon(ordered[0]))
            tree.addTopLevelItem(group)
            for footprint in ordered:
                leaf = QTreeWidgetItem([footprint.name, str(len(footprint.pins))])
                leaf.setData(0, ROLE_FOOTPRINT_ID, footprint.id)
                leaf.setIcon(0, icons.part_icon(footprint))
                leaf.setToolTip(
                    0, f"{footprint.name}\n{footprint.id} — {len(footprint.pins)} pin(s)"
                )
                group.addChild(leaf)
            group.setExpanded(True)

        for archetype in sorted(by_archetype):
            group = QTreeWidgetItem([archetype.replace("-", " "), ""])
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            # The group takes the picture of its first member, which is the archetype's
            # picture: every part under it is that shape in that colour.
            group.setIcon(0, icons.part_icon(by_archetype[archetype][0]))
            tree.addTopLevelItem(group)
            for footprint in by_archetype[archetype]:
                leaf = QTreeWidgetItem([footprint.name, str(len(footprint.pins))])
                leaf.setData(0, ROLE_FOOTPRINT_ID, footprint.id)
                # In the colours the board draws it in, so finding the part you picked is
                # recognition rather than reading -- see the note in icons.py.
                leaf.setIcon(0, icons.part_icon(footprint))
                # The NAME first, because the column it sits in is the one that gets
                # elided: "Film capa…" in a 300 px dock is the string a tooltip has to
                # finish, and the id alone was no help at all with that.
                leaf.setToolTip(
                    0, f"{footprint.name}\n{footprint.id} — {len(footprint.pins)} pin(s)"
                )
                group.addChild(leaf)
            # Expanded only when the filter has narrowed things down, otherwise the twenty-eight
            # pin headers bury everything else.
            group.setExpanded(bool(needle) or len(by_archetype[archetype]) <= 4)
        tree.blockSignals(False)

    def _on_placement_value_changed(self, text: str) -> None:
        self.scene.placement_value = text.strip()
        # The banner names the part about to be placed, and now its value with it.
        self._refresh_mode_banner()

    def _on_library_selection_changed(self) -> None:
        items = self.library_tree.selectedItems()
        footprint_id = items[0].data(0, ROLE_FOOTPRINT_ID) if items else None
        self.scene.arm_placement(footprint_id)

    def _on_placement_armed(self, footprint_id: str) -> None:
        if not footprint_id:
            self.label_place_hint.setText(t("Pick a part, then click the board. Esc cancels."))
            self.view.viewport().unsetCursor()
            self.library_tree.clearSelection()
            self._refresh_mode_banner()
            return
        footprint = self.lookup(footprint_id)
        name = footprint.name if footprint is not None else footprint_id
        ref = next_reference(self.bus.document, footprint_id)
        described = f"{self.scene.placement_value} {name}" if self.scene.placement_value else name
        self.label_place_hint.setText(
            f"{t('Click a hole to place')} <b>{ref}</b> ({described}). {t('Esc cancels.')}"
        )
        self.view.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self._refresh_mode_banner()

    # -- what mode am I in ----------------------------------------------------
    #
    # Placement, drawing and pin-picking all arm a mode over the board, and until now the
    # only place any of them said so was the status bar -- the bottom edge of a window a
    # metre wide, while the cursor is in the middle of the board. A mode nobody can see is
    # indistinguishable from an application that has stopped responding to clicks, and it
    # is the state in which every click means something other than what it usually means.

    def _refresh_mode_banner(self) -> None:
        """Put the armed mode over the board, or take the banner away.

        Derived from the scene rather than remembered, so the banner cannot disagree with
        what a click will actually do -- the scene is the thing holding the mode.
        """
        self.view.show_mode(self._mode_text())
        # Somebody mid-mode is plainly not stuck, and two blocks of text over one board is
        # one too many.
        self._refresh_empty_hint()

    def _mode_text(self) -> str:
        if self.scene.connect_armed:
            first = self.scene.connect_from()
            if first is None:
                return f"{t('Connecting')}  ·  {t('click the first pin, Esc cancels')}"
            return (
                f"{t('Connecting from')} {first[0]}.{first[1]}  ·  "
                f"{t('click the pin it joins, Esc cancels')}"
            )

        net_id = self.scene.armed_net_id
        if net_id is not None:
            picked = self.scene.picked_pins()
            names = ", ".join(f"{ref}.{pin}" for ref, pin in picked)
            listed = names if picked else t("no pins yet")
            return (
                f"{t('Adding pins to')} {self._net_name(net_id)}: {listed}  ·  "
                f"{t('Enter or right-click finishes, Esc cancels')}"
            )

        footprint_id = self.scene.armed_footprint_id
        if footprint_id:
            footprint = self.lookup(footprint_id)
            name = footprint.name if footprint is not None else footprint_id
            ref = next_reference(self.bus.document, footprint_id)
            value = self.scene.placement_value
            described = f"{value} {name}" if value else name
            return f"{t('Placing')} {ref} ({described})  ·  {t('click a hole, Esc cancels')}"

        kind = self.scene.armed_draw_kind
        if kind:
            two_point = kind in ("bare-wire", "insulated-wire", "top-jumper")
            how = (
                t("click both ends, Esc cancels")
                if two_point
                else t("click each pad, Enter or right-click finishes, Esc cancels")
            )
            return f"{t('Drawing')} {kind.replace('-', ' ')}  ·  {how}"

        if self.scene.cut_armed:
            return f"{t('Cutting tracks')}  ·  {t('click a hole, Esc ends')}"

        if self.scene.measure_armed:
            from_ = self.scene.measure_from()
            where = (
                f"{t('from')} {format_hole(from_)}" if from_ is not None else t("click two holes")
            )
            return f"{t('Measuring')}  ·  {where}, {t('Esc ends')}"

        return ""

    def _remember_a_part_was_placed(self) -> None:
        """Note, once and for good, that this person has placed a part.

        What the blank-board guidance is FOR is the first launch. Repeating it on every
        launch afterwards is the application explaining its own front door to somebody who
        has been through it a hundred times -- and there is nowhere to click it away,
        because the block is transparent to the mouse by design (see ViewOverlay).
        """
        if self._has_placed_a_part:
            return
        self._has_placed_a_part = True
        app_settings().setValue(HAS_PLACED_KEY, True)

    def _refresh_empty_hint(self) -> None:
        """Tell a blank board what to do with itself, the first time round.

        The application opens on an empty 5 x 7 board, and every route, check and export
        needs something on it first. An empty viewport with a full menu bar above it is
        the one screen where a person cannot tell whether they are looking at a tool that
        is ready or one that is broken -- which is a real problem exactly once.
        """
        document = self.bus.document
        if (
            document.components
            or document.conductors
            or self._mode_text()
            or self._has_placed_a_part
        ):
            self.view.set_empty_hint("")
            return
        self.view.set_empty_hint(
            f"<b>{t('Nothing on this board yet.')}</b><br><br>"
            f"{t('Pick a part from the Parts panel and click a hole to place it.')}<br>"
            f"{t('Then Net ▸ New Net… to say what joins what, and Route ▸ Autoroute.')}<br><br>"
            f"{t('An existing circuit comes in through File ▸ Import KiCad Netlist.')}"
        )

    def _on_component_placed(self, result: DispatchResult) -> None:
        if result.ok:
            # Placement stays armed: a board needs several parts, and re-picking from the list
            # between every one of them would be a needless round trip.
            #
            # An overlap is reported rather than prevented: the bus allows two pins in one hole
            # because it is a legal document, and DRC is what objects. Saying only "placed" for
            # something the ghost had just drawn in red would read as approval.
            # A pin with nothing to solder to is said FIRST and said plainly. The board is
            # still legal -- a mounting hole can be added over a part that was already
            # there, so refusing the placement would only make the same board harder to
            # reach -- but DRC calls it an error, and a status line that only said
            # "placed" would be the last chance anybody had to notice.
            if self.scene.last_placement_on_a_dead_hole:
                note = f"  ·  {t('a pin has no pad there — see DRC')}"
            elif self.scene.last_placement_overlapped:
                note = f"  ·  {t('it overlaps an existing pin — see DRC')}"
            else:
                note = ""
            self.statusBar().showMessage(f"{result.description}{note} — Esc to stop placing.", 6000)
            self._on_placement_armed(self.scene.armed_footprint_id or "")
            self._remember_a_part_was_placed()
        else:
            self.statusBar().showMessage(f"Cannot place there: {result.message}", 6000)

    def _build_nets_dock(self) -> None:
        """The netlist, with what each net still needs.

        This is the board's to-do list, and it did not exist before: the only way to find
        out whether a net was finished was to read an LVS message. "To route" is the ratsnest's
        own count, so it reaches zero exactly when the net is closed.
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # The same escape hatch the parts library has, for the same reason: a netlist of
        # any size is mostly nets you are not working on, and "which of these still needs
        # something" is a question you ask about one of them at a time.
        self.nets_filter = QLineEdit()
        self.nets_filter.setPlaceholderText(t("Filter nets…  (gnd, power, U1)"))
        self.nets_filter.setClearButtonEnabled(True)
        self.nets_filter.textChanged.connect(
            lambda _text: self._refresh_nets_panel(self._last_ratsnest)
        )
        layout.addWidget(self.nets_filter)

        self.nets_tree = QTreeWidget()
        self.nets_tree.setHeaderLabels([t("Net"), t("Class"), t("Pins"), t("To route")])
        self.nets_tree.setAlternatingRowColors(True)
        self.nets_tree.setRootIsDecorated(True)
        # The net name absorbs the spare width and the three narrow columns keep their
        # content. Fixed widths pushed "To route" off the edge of the dock -- which is the one
        # column the panel exists for, so it must never be the one that gets clipped.
        header = self.nets_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.nets_tree.itemSelectionChanged.connect(self._on_net_selection_changed)
        self.nets_tree.itemDoubleClicked.connect(self._on_net_double_clicked)
        self.nets_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.nets_tree.customContextMenuRequested.connect(self._on_nets_context_menu)
        layout.addWidget(self.nets_tree)

        dock = QDockWidget(t("Nets"), self)
        dock.setObjectName("dockNets")
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.dock_nets = dock
        # 340 rather than 300: at 300 the parts library elides half its names, and the two
        # left-hand docks share a column.
        self.resizeDocks([dock], [340], Qt.Orientation.Horizontal)

    # -- the schematic, in the window -----------------------------------------
    #
    # THE HALF OF THE TOOL THAT HAD NO PICTURE. The board shows where everything goes and
    # the ratsnest draws the connections still owed, but the CIRCUIT -- the thing the board
    # is a way of building -- was only ever a tree of net names in a dock. LVS could say
    # "VOUT is open" to somebody with no way to look at what VOUT is.
    #
    # It draws the netlist and it does not edit it (PLAN.md D3, and `schematic.py`'s own
    # header). What it does instead is cross-probe: click a symbol and that part is
    # selected on the board, click a wire and its net lights up in both places, and a
    # selection made over there lights up here. Two views of one document, which is worth
    # more on a perfboard than a second editor would be.

    def _build_schematic_dock(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self.schematic_summary = QLabel()
        self.schematic_summary.setWordWrap(True)
        self.schematic_summary.setStyleSheet(f"color: {TEXT_DIM};")
        layout.addWidget(self.schematic_summary)

        self.schematic_view = SchematicView()
        self.schematic_view.partClicked.connect(self._on_schematic_part_clicked)
        self.schematic_view.partActivated.connect(self._on_schematic_part_activated)
        self.schematic_view.netClicked.connect(self._on_schematic_net_clicked)
        self.schematic_view.pinClicked.connect(self._on_schematic_pin_clicked)
        self.schematic_view.cleared.connect(self._on_schematic_cleared)
        layout.addWidget(self.schematic_view, 1)

        # Two rows of buttons rather than a toolbar: this is a dock that can be a third of
        # the window wide, and a toolbar in one elides its way down to icons nobody can
        # tell apart.
        top_row = QHBoxLayout()
        self.act_sch_add = QPushButton(t("Add Part…"))
        self.act_sch_add.setToolTip(
            t("Put a part in the design without deciding where it goes on the board yet.")
        )
        self.act_sch_add.clicked.connect(self.on_schematic_add_part)
        top_row.addWidget(self.act_sch_add)

        self.act_sch_wire = QPushButton(t("Wire"))
        self.act_sch_wire.setCheckable(True)
        self.act_sch_wire.setToolTip(
            t(
                "Click a pin, then the pin it joins. Neither on a net yet? One gets made. "
                "Exactly what the board's connect tool does, because it is the same code."
            )
        )
        self.act_sch_wire.toggled.connect(self.on_schematic_wire_mode)
        top_row.addWidget(self.act_sch_wire)

        self.act_sch_delete = QPushButton(t("Remove"))
        self.act_sch_delete.setToolTip(
            t(
                "Take the selected part out of the design, along with its connections. A "
                "part that is on the board comes off it and stays in the design instead."
            )
        )
        self.act_sch_delete.clicked.connect(self.on_schematic_remove)
        top_row.addWidget(self.act_sch_delete)
        layout.addLayout(top_row)

        bottom_row = QHBoxLayout()
        self.act_sch_place = QPushButton(t("Place on the Board"))
        self.act_sch_place.setToolTip(
            t(
                "Move every part that is only in the design onto the board, in a grid to "
                "drag from. One undo step for the lot. Auto-place (Ctrl+Shift+A) arranges "
                "them properly afterwards."
            )
        )
        self.act_sch_place.clicked.connect(self.on_schematic_place_all)
        bottom_row.addWidget(self.act_sch_place)

        fit = QPushButton(t("Fit the Sheet"))
        fit.setToolTip(
            t(
                "Put the whole schematic back in the panel. The sheet is not re-fitted "
                "when the board changes, so an edit cannot move what you were looking at."
            )
        )
        fit.clicked.connect(self.schematic_view.fit)
        bottom_row.addWidget(fit)

        export_sheet = QPushButton(t("Export…"))
        export_sheet.setToolTip(
            t(
                "Write the sheet beside the document as SVG, PDF and PNG. All three are "
                "drawn from the same file, so they cannot disagree about the circuit."
            )
        )
        export_sheet.clicked.connect(self.on_export_schematic)
        bottom_row.addWidget(export_sheet)
        layout.addLayout(bottom_row)

        dock = QDockWidget(t("Schematic"), self)
        dock.setObjectName("dockSchematic")
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.dock_schematic = dock
        dock.hide()
        dock.visibilityChanged.connect(self._on_schematic_visibility_changed)

    def _on_schematic_visibility_changed(self, visible: bool) -> None:
        if visible and self._schematic_stale:
            self._refresh_schematic_panel()

    def _refresh_schematic_panel(self) -> None:
        """Redraw the sheet, or mark it stale and do nothing.

        ``isHidden()`` rather than ``isVisible()``, for the trap the guide panel documents:
        a widget is "visible" only once every ancestor is, so during construction and in
        any headless run a dock that HAS been shown is not yet visible -- and the panel
        would refuse to fill itself while sitting open in front of the user.
        """
        if not hasattr(self, "schematic_view") or self.dock_schematic.isHidden():
            self._schematic_stale = True
            return
        self._schematic_stale = False
        drawing = build_schematic(self.bus.document, self.lookup)
        self.schematic_view.set_drawing(drawing)
        self._sync_schematic_highlight()

        parts = len(drawing.symbols)
        nets = len(self.bus.document.nets)
        summary = f"{parts} part(s), {nets} net(s)"
        # Counted rather than complained about: while a circuit is being drawn every part
        # is unplaced, so this is a progress line and not a warning. It is also the answer
        # to "have I finished", which is the question the Place button exists for.
        waiting = sum(1 for symbol in drawing.symbols if symbol.unplaced)
        if waiting:
            summary += f"; {waiting} not on the board yet"
        if drawing.rails:
            # Said plainly, because a reader who does not know the convention will look for
            # the ground wires and not find any.
            summary += f"; power and ground drawn as {len(drawing.rails)} rail symbol(s)"
        if drawing.notes:
            summary += "\n" + "\n".join(f"• {note}" for note in drawing.notes[:4])
            if len(drawing.notes) > 4:
                summary += f"\n• …and {len(drawing.notes) - 4} more"
        self.schematic_summary.setText(summary)

    def _sync_schematic_highlight(self) -> None:
        """Light up on the sheet whatever is selected on the board, in the Nets dock, or
        on the sheet itself.

        The third one is why the panel keeps a reference of its own: a part that is not on
        the board cannot be selected on the board, and it is the one somebody drawing a
        circuit is working with.
        """
        if not hasattr(self, "schematic_view"):
            return
        refs = {component.ref for component in self._selected_components()}
        if self._schematic_ref is not None:
            refs.add(self._schematic_ref)
        self.schematic_view.set_highlight(refs, self._selected_net_ids())
        self._refresh_schematic_actions()

    def _refresh_schematic_actions(self) -> None:
        if not hasattr(self, "act_sch_delete"):
            return
        self.act_sch_delete.setEnabled(self._schematic_ref is not None)
        self.act_sch_place.setEnabled(bool(self.bus.document.parts))

    def _on_schematic_cleared(self) -> None:
        self._schematic_ref = None
        self._sync_schematic_highlight()

    def _on_schematic_part_clicked(self, ref: str) -> None:
        self._schematic_ref = ref
        component = next((c for c in self.bus.document.components if c.ref == ref), None)
        if component is None:
            # Not on the board. That is the normal state of a part on a schematic being
            # drawn, so it is selected here and reported rather than treated as a miss.
            self._sync_schematic_highlight()
            in_design = any(part.ref == ref for part in self.bus.document.parts)
            self.statusBar().showMessage(
                f"{ref} is in the design, not on the board yet."
                if in_design
                else f"{ref} is named by a net and is not in the design at all.",
                6000,
            )
            return
        self.go_to_component(component.id)
        self._sync_schematic_highlight()

    def _on_schematic_part_activated(self, ref: str) -> None:
        component = next((c for c in self.bus.document.components if c.ref == ref), None)
        if component is not None:
            self.on_component_properties(component.id)
            return
        part = next((p for p in self.bus.document.parts if p.ref == ref), None)
        if part is not None:
            self.on_schematic_part_properties(part.id)

    # -- drawing the circuit --------------------------------------------------

    def on_schematic_add_part(self) -> None:
        """Put a part in the design. The first half of schematic-first capture.

        Nothing else in the application could do this: every route a part had into a
        document ended in ``component.place``, which needs a hole — so the circuit could
        not be drawn before the layout was, which is the wrong way round and the opposite
        of how every other EDA tool works.
        """
        dialog = AddPartDialog(self.bus.document, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.values()
        if chosen is None:
            return
        ref, value, footprint_id = chosen
        result = self.bus.dispatch(
            "part.add", AddPartPayload(ref=ref, footprint_id=footprint_id, value=value)
        )
        if not result.ok:
            self.statusBar().showMessage(f"[{result.code}] {result.message}", 8000)
            return
        self._schematic_ref = ref
        self._sync_schematic_highlight()
        self.statusBar().showMessage(result.description, 6000)

    def on_schematic_part_properties(self, part_id: str) -> None:
        """The same three fields ``AddPartDialog`` asks for, on a part that exists."""
        part = next((p for p in self.bus.document.parts if p.id == part_id), None)
        if part is None:
            return
        dialog = AddPartDialog(self.bus.document, self)
        dialog.setWindowTitle(t("Edit a Part"))
        dialog.select_footprint(part.footprint_id)
        dialog.ref.setText(part.ref)
        dialog.value.setText(part.value)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.values()
        if chosen is None:
            return
        ref, value, footprint_id = chosen
        result = self.bus.dispatch(
            "part.update",
            UpdatePartPayload(id=part_id, ref=ref, value=value, footprint_id=footprint_id),
        )
        if not result.ok:
            self.statusBar().showMessage(f"[{result.code}] {result.message}", 8000)
            return
        self._schematic_ref = ref
        self._sync_schematic_highlight()

    def on_schematic_wire_mode(self, checked: bool) -> None:
        self.schematic_view.set_wiring(checked)
        if checked:
            self.statusBar().showMessage(
                t("Click a pin, then the pin it joins. Neither on a net yet? One gets made. "
                  "Esc cancels."),
                0,
            )
        else:
            self.statusBar().clearMessage()

    def _on_schematic_pin_clicked(self, ref: str, pin: str) -> None:
        """Take the first pin, or join the second to it.

        The decision about what joining two pins MEANS is ``view2d.join_pins``, shared with
        the board's connect tool. Two surfaces that could disagree about whether a click on
        a rail pin extends the rail or starts a new net would be two different applications
        in one window.
        """
        pending = self.schematic_view.pending_pin
        if pending is None:
            self.schematic_view.set_pending_pin((ref, pin))
            self.statusBar().showMessage(f"From {ref}.{pin} — click the pin it joins.", 0)
            return
        if pending == (ref, pin):
            self.schematic_view.set_pending_pin(None)
            self.statusBar().showMessage(t("Cancelled."), 4000)
            return
        result, refusal = join_pins(self.bus, pending, (ref, pin))
        self.schematic_view.set_pending_pin(None)
        if refusal is not None:
            self.statusBar().showMessage(refusal, 8000)
            return
        if result is not None:
            self.statusBar().showMessage(
                result.description if result.ok else f"[{result.code}] {result.message}", 6000
            )

    def on_schematic_remove(self) -> None:
        """Take the selected part out of the design, or off the board.

        TWO DIFFERENT ACTIONS BEHIND ONE BUTTON, and the difference is which list the part
        is in rather than a mode. A part in the design is deleted outright and its
        connections go with it. A part on the BOARD is unplaced — it goes back to the
        design with its wiring intact, because "I put this in the wrong hole" is what
        somebody clicking Remove on a placed part almost always means, and deleting the
        circuit around it would be a much larger answer than the question.
        """
        ref = self._schematic_ref
        if ref is None:
            return
        component = next((c for c in self.bus.document.components if c.ref == ref), None)
        if component is not None:
            result = self.bus.dispatch("component.unplace", UnplaceComponentPayload(id=component.id))
        else:
            part = next((p for p in self.bus.document.parts if p.ref == ref), None)
            if part is None:
                self.statusBar().showMessage(
                    f"{ref} is only named by a net; edit the net to remove it.", 6000
                )
                return
            result = self.bus.dispatch("part.delete", DeletePartPayload(id=part.id))
        if not result.ok:
            self.statusBar().showMessage(f"[{result.code}] {result.message}", 8000)
            return
        if component is None:
            self._schematic_ref = None
        self._sync_schematic_highlight()
        self.statusBar().showMessage(result.description, 6000)

    def on_schematic_place_all(self) -> None:
        """Move the whole design onto the board. The step this panel exists to lead to.

        A GRID, not an arrangement. Working out where parts should go is
        ``placer.py``'s job and it is a second of simulated annealing; doing it silently
        inside a button called "Place on the Board" would hide the one step of this
        application somebody most wants to watch and re-run. So the parts land somewhere
        obvious and the message says what to press next.
        """
        parts = self.bus.document.parts
        if not parts:
            self.statusBar().showMessage(t("Every part in the design is already on the board."), 6000)
            return
        placements = self._grid_placements(parts)
        if not placements:
            self.statusBar().showMessage(
                t("No room on this board for the parts in the design. Make it bigger, or "
                  "place them one at a time."),
                8000,
            )
            return
        result = self.bus.dispatch(
            "part.place",
            PlacePartsPayload(
                placements=tuple(placements),
                label=f"Place {len(placements)} part(s) from the schematic",
            ),
        )
        if not result.ok:
            self.statusBar().showMessage(f"[{result.code}] {result.message}", 10000)
            return
        left = len(parts) - len(placements)
        note = f"; {left} would not fit" if left else ""
        self.statusBar().showMessage(
            f"{result.description}{note}. Auto-place (Ctrl+Shift+A) arranges them, "
            f"then Ctrl+R routes.",
            12000,
        )
        self._sync_schematic_highlight()

    def _grid_placements(self, parts: Sequence[Any]) -> list[PartPlacement]:
        """Lay parts out left to right in rows, skipping what will not fit.

        The same shape as ``_place_parts_in_grid`` and deliberately not shared with it:
        that one builds ``PlaceComponentPayload`` for parts the document does not have
        yet, this one builds ``PartPlacement`` for parts it does, and the two payloads have
        nothing in common but the arithmetic.
        """
        board = self.bus.document.board
        placements: list[PartPlacement] = []
        col, row, row_height = 1, 1, 0
        for part in sorted(parts, key=lambda p: p.ref):
            footprint = self.lookup(part.footprint_id)
            if footprint is None:
                continue
            width = max((p.d_col for p in footprint.pins), default=0) + 2
            height = max((p.d_row for p in footprint.pins), default=0) + 2
            if col + width >= board.cols:
                col, row = 1, row + row_height + 1
                row_height = 0
            if row + height >= board.rows:
                break  # Out of board; the rest stay in the design and the caller says so.
            placements.append(PartPlacement(id=part.id, anchor=HoleCoord(col=col, row=row)))
            col += width
            row_height = max(row_height, height)
        return placements

    def _on_schematic_net_clicked(self, net_id: str) -> None:
        """Selecting the net in the dock is what lights it up everywhere else.

        Deliberately routed through the Nets panel rather than highlighting three views by
        hand: that panel's selection already drives the board and the schematic, so one
        path means the three cannot end up disagreeing about what is selected.
        """
        self._select_net(net_id)

    # -- the build guide, in the window ---------------------------------------
    #
    # The guide is what this application is FOR: the board on screen is a means to a
    # person at a bench with an iron. Until now the only way to see one was to export four
    # files and go and find them, so the order the tool had worked out -- shortest part
    # first, jumpers before whatever stands on them, ICs last -- was invisible while the
    # board was being designed, which is when it is worth knowing.
    #
    # Closed by default and rebuilt only while open, for the reason the 3D panel is:
    # build_guide runs DRC and LVS, and paying for that on every keystroke to fill a
    # panel nobody has open is the same mistake twice.

    def _build_guide_dock(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self.guide_summary = QLabel()
        self.guide_summary.setWordWrap(True)
        self.guide_summary.setStyleSheet(f"color: {TEXT_DIM};")
        layout.addWidget(self.guide_summary)

        self.guide_tree = QTreeWidget()
        self.guide_tree.setHeaderLabels([t("Step"), t("Where")])
        self.guide_tree.setRootIsDecorated(True)
        header = self.guide_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.guide_tree.itemSelectionChanged.connect(self._on_guide_step_selected)
        layout.addWidget(self.guide_tree)

        export = QPushButton(t("Export the Guide…"))
        export.clicked.connect(self.on_export_guide)
        layout.addWidget(export)

        dock = QDockWidget(t("Build Guide"), self)
        dock.setObjectName("dockGuide")
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.dock_guide = dock
        dock.hide()
        dock.visibilityChanged.connect(self._on_guide_visibility_changed)

    def _on_guide_visibility_changed(self, visible: bool) -> None:
        if visible and self._guide_stale:
            self._refresh_guide_panel()

    def _refresh_guide_panel(self) -> None:
        """Fill the panel from the guide, or mark it stale and do nothing.

        Deliberately keeps NO selection across a rebuild. Adding a part renumbers every
        step after it, so the step at the index you were on is a different step -- and a
        panel that silently moved your place would be worse than one that lost it.
        """
        # isHidden(), never isVisible(): a widget is "visible" only once every ancestor
        # is too, so during construction -- and in any headless run -- a dock that has
        # been shown is not yet visible, and the panel would refuse to fill itself while
        # sitting open in front of the user. The same trap as BoardView._place_overlays.
        if not hasattr(self, "guide_tree") or self.dock_guide.isHidden():
            self._guide_stale = True
            return
        self._guide_stale = False
        guide = self.current_guide()
        steps = self._assembly_cached
        self.guide_summary.setText(describe_guide(guide))

        tree = self.guide_tree
        blocked = tree.blockSignals(True)
        tree.clear()
        index = 0
        for phase in guide.phases:
            if phase.is_empty:
                # The order is physical, not editorial (guide.py), and a board simply may
                # not need a phase. An empty heading reads as a step somebody forgot.
                continue
            head = QTreeWidgetItem([f"{phase.number}. {phase.title}", f"{len(phase.steps)}"])
            head.setFlags(head.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            tree.addTopLevelItem(head)
            for step in phase.steps:
                leaf = QTreeWidgetItem([step.title, step.span])
                # The index into the FLAT list, which is what the assembly slider and
                # document_at_step both count in -- see guide.all_steps.
                leaf.setData(0, ROLE_STEP_INDEX, index)
                head.addChild(leaf)
                index += 1
            for checkpoint in phase.checkpoints:
                mark = QTreeWidgetItem([f"✓ {checkpoint.title}", ""])
                mark.setForeground(0, QColor(OK))
                mark.setFlags(mark.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                head.addChild(mark)
            head.setExpanded(True)
        tree.blockSignals(blocked)
        # Every step accounted for, or the panel and the slider are counting different
        # things and one of them is wrong.
        assert index == len(steps), f"{index} steps in phases, {len(steps)} in the flat list"

    def _on_guide_step_selected(self) -> None:
        """Show the step on the board: its parts selected, its holes brought into view.

        This is the whole reason the guide is worth having in the window rather than in a
        browser. "Fit R7, C7 to C11" is an instruction; the same step with those two pads
        lit up on the board in front of you is an answer.
        """
        items = self.guide_tree.selectedItems()
        if not items:
            return
        index = items[0].data(0, ROLE_STEP_INDEX)
        if index is None:
            return
        steps = self._assembly_cached
        if not 0 <= index < len(steps):
            return
        step = steps[index]
        board, side = self.bus.document.board, self.side
        if isinstance(step, PartStep):
            self.scene.select_components([step.component_id])
            self.view.reveal_holes([hole for _pin, hole in step.pin_holes], board, side)
        else:
            net = next((n for n in self.bus.document.nets if n.name == step.net_name), None)
            self.scene.set_highlighted_nets([net.id] if net is not None else [])
            self.view.reveal_holes(step.path, board, side)
        # The 3D view follows if it is open, so the two panels are one place in the build
        # rather than two. Only when it exists: the slider is built with the 3D widget.
        if self.assembly_slider is not None and self.assembly_slider.isEnabled():
            self.assembly_slider.setValue(index + 1)

    def _build_drc_dock(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # The same escape hatch the parts and nets panels have. A board mid-layout can
        # carry a hundred proximity warnings, and "show me the errors" or "show me R5'"
        # is how anybody reads a list that long.
        self.drc_filter = QLineEdit()
        self.drc_filter.setPlaceholderText(t("Filter findings…  (error, short, R5', C7)"))
        self.drc_filter.setClearButtonEnabled(True)
        self.drc_filter.textChanged.connect(self._on_drc_filter_changed)
        layout.addWidget(self.drc_filter)

        self.drc_tree = QTreeWidget()
        self.drc_tree.setHeaderLabels([t("Rule / Kind"), t("Message")])
        self.drc_tree.setColumnWidth(0, 260)
        self.drc_tree.itemClicked.connect(self._on_drc_item_clicked)
        self.drc_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.drc_tree.customContextMenuRequested.connect(self._on_drc_context_menu)
        layout.addWidget(self.drc_tree)

        dock = QDockWidget(t("DRC / LVS"), self)
        dock.setObjectName("dockDrc")
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        # A clean board is four rows, and this panel was opening a quarter of the window
        # tall to show them -- taken off the board, which is the thing being worked on.
        self.dock_drc = dock
        self.resizeDocks([dock], [190], Qt.Orientation.Vertical)

    def _on_drc_filter_changed(self, _text: str) -> None:
        if self._last_lvs is not None:
            self._refresh_drc_panel(self._last_violations, self._last_lvs)

    # -- right-click ---------------------------------------------------------
    #
    # There was no context menu anywhere in this application. Everything a part can be
    # told to do lived in the menu bar at the top of a window a metre wide, while the part
    # itself was under the pointer in the middle of the board -- so rotating three parts
    # meant three round trips to the Edit menu, and the keyboard shortcuts that avoid that
    # are only discoverable from a card behind F1.
    #
    # Every one of these menus is built from the SAME QAction objects the menu bar holds,
    # never a parallel list. An action greyed out up there is greyed out here, and one
    # that gains a shortcut gains it in both places, because there is only one of it.

    def _on_board_context_menu(self, pos: QPoint) -> None:
        self.board_menu(pos).exec(self.view.viewport().mapToGlobal(pos))

    def board_menu(self, pos: QPoint) -> QMenu:
        """The menu for a right-click on the board, built around what is under it.

        Built and returned rather than shown, so a test can read what a right-click at a
        position would offer without a modal event loop -- ``exec`` on a menu in a
        headless run waits for a click that will never come.
        """
        item = self.scene.component_at(self.view.mapToScene(pos))
        if item is not None and item.comp.id not in self.scene.selected_component_ids():
            # Right-clicking a part nobody selected selects it first, as it does in every
            # editor -- otherwise the menu offers to rotate something else entirely.
            self.scene.select_components([item.comp.id])

        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        if self.scene.selected_component_ids():
            menu.addAction(self.act_properties)
            menu.addSeparator()
            menu.addAction(self.act_rotate_cw)
            menu.addAction(self.act_rotate_ccw)
            menu.addAction(self.act_mirror)
            menu.addAction(self.act_lock)
            menu.addSeparator()
            menu.addAction(self.act_copy)
            menu.addAction(self.act_duplicate)
            menu.addAction(self.act_delete)
            menu.addSeparator()
            menu.addAction(self.act_route_selected)
            menu.addAction(self.act_reroute_selected)
        else:
            # Bare board. Paste lands under the pointer, which is the reason this entry is
            # worth having at all: the keyboard version pastes wherever the pointer happens
            # to be, and here the pointer is demonstrably where the user just clicked.
            menu.addAction(self.act_paste)
            menu.addSeparator()
            menu.addAction(self.act_connect)
            menu.addAction(self.act_new_net)
            menu.addSeparator()
            menu.addAction(self.act_fit)
            menu.addAction(self.act_flip)
            menu.addSeparator()
            menu.addAction(self.act_board_setup)
        return menu

    def _on_nets_context_menu(self, pos: QPoint) -> None:
        self.nets_menu().exec(self.nets_tree.viewport().mapToGlobal(pos))

    def nets_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        menu.addAction(self.act_new_net)
        menu.addSeparator()
        for action in self.net_actions:
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.act_route_selected)
        return menu

    def _on_drc_context_menu(self, pos: QPoint) -> None:
        self.drc_menu(pos).exec(self.drc_tree.viewport().mapToGlobal(pos))

    def drc_menu(self, pos: QPoint) -> QMenu:
        """Copying a finding out is the point: a DRC message is what somebody pastes into
        a forum post asking why their board does not work."""
        menu = QMenu(self)
        item = self.drc_tree.itemAt(pos)
        if item is not None:
            copy_one = menu.addAction(t("&Copy This Finding"))
            copy_one.triggered.connect(
                lambda: QApplication.clipboard().setText(
                    " ".join(part for part in (item.text(0), item.text(1)) if part).strip()
                )
            )
        copy_all = menu.addAction(t("Copy &All Findings"))
        copy_all.triggered.connect(self._copy_all_findings)
        return menu

    def _copy_all_findings(self) -> None:
        lines = [f"{v.severity} {v.rule}: {v.message}" for v in self._last_violations]
        if self._last_lvs is not None:
            lines += [f"lvs {i.kind}: {i.message}" for i in self._last_lvs.issues]
        QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage(
            f"{len(lines)} {t('findings copied to the clipboard')}", 4000
        )

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
        if self.assembly_slider is not None:
            # Before the 3D refresh, so the panel repaints once with the new build rather
            # than once against the old step order and again against the new one.
            self._sync_assembly_range()
        self._refresh_3d()
        # Marks itself stale and returns when the panel is shut, so a closed panel costs
        # nothing -- the guide it would need runs DRC and LVS to build.
        self._refresh_guide_panel()
        self._refresh_schematic_panel()

        self._refresh_title()
        self._refresh_undo_actions()
        self._refresh_empty_hint()
        # The banner names the part about to be placed, and next_reference reads the
        # document to work that out -- so placing R4 has to move the banner on to R5.
        self._refresh_mode_banner()
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

        Kept as well as shown, because Paste lands where the pointer is.
        """
        self._hovered_hole = HoleCoord(col, row)
        board = self.bus.document.board
        inside = 0 <= col < board.cols and 0 <= row < board.rows
        text = format_hole(HoleCoord(col, row)) if inside else "—"
        self.label_hole.setText(
            f'<span style="color:{TEXT_DIM}">{t("hole")}</span> <b>{text}</b>'
        )

    def _refresh_status(self) -> None:
        # The ruler is suppressed on a board that prints its own addresses on the side in
        # view, so the toggle for it is greyed out rather than left as a control that
        # visibly does nothing. Refreshed here because both things that change the answer
        # -- flipping the board and editing the board setup -- already come through.
        if hasattr(self, "act_rulers"):
            readable = self.scene.legend_is_readable()
            self.act_rulers.setEnabled(not readable)
            self.act_rulers.setToolTip(
                t("This board prints its own addresses, so the editor's ruler would repeat them.")
                if readable
                else t("Column letters and row numbers along the edges of the view.")
            )

        # Cutting a track is meaningless on a board that has none, and the board type can
        # change under an open window through Board Setup -- so the tool is greyed out
        # here rather than refusing the click later.
        if hasattr(self, "act_cut"):
            strips = is_stripboard(self.bus.document.board)
            self.act_cut.setEnabled(strips)
            if not strips:
                self.act_cut.setToolTip(
                    t("Only stripboard has tracks to cut. File ▸ Board Setup ▸ Type.")
                )

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

        side = t("component side") if self.side == "top" else f'{t("solder side")} ({t("mirrored")})'
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

    #: LVS issue kinds that mean the board is wired differently from the schematic, as
    #: opposed to merely incomplete. An unrouted net is work left to do; a short is a
    #: board that will not work, and the two should not be the same colour.
    _LVS_WRONG = frozenset({"open", "short", "unplaced-component", "unknown-footprint"})

    def _refresh_drc_panel(self, violations: tuple[DrcViolation, ...], lvs: LvsResult) -> None:
        """Rebuild the findings, keeping the reader's place.

        This ran ``clear()`` and built the whole tree again on EVERY command, which threw
        away the expanded groups and the selected row each time -- so working through a
        rule meant re-expanding it after every edit, which is to say after every attempt
        to fix what the rule was complaining about.
        """
        tree = self.drc_tree
        expanded = self._expanded_drc_keys()
        needle = self.drc_filter.text().strip().lower() if hasattr(self, "drc_filter") else ""
        tree.clear()

        drc_root = QTreeWidgetItem(["DRC", f"{len(violations)} violation(s)"])
        drc_root.setData(0, ROLE_FINDING_KEY, "DRC")
        tree.addTopLevelItem(drc_root)
        by_rule: dict[str, list[DrcViolation]] = {}
        for v in violations:
            if needle and needle not in f"{v.rule} {v.severity} {v.message}".lower():
                continue
            by_rule.setdefault(v.rule, []).append(v)
        for rule in sorted(by_rule):
            items = by_rule[rule]
            severity = items[0].severity
            rule_item = QTreeWidgetItem([f"{rule} ({severity})", f"{len(items)}"])
            # The colour the status bar already uses for the same count, so a warning
            # reads the same whether it is a number on the bar or a row in this tree.
            rule_item.setForeground(0, QColor(ERROR if severity == "error" else WARNING))
            rule_item.setData(0, ROLE_FINDING_KEY, f"drc:{rule}")
            drc_root.addChild(rule_item)
            self._add_drc_findings(rule_item, rule, items, expanded)
            rule_item.setExpanded(f"drc:{rule}" in expanded)
        drc_root.setExpanded("DRC" in expanded or not expanded)

        s = lvs.summary
        lvs_root = QTreeWidgetItem(
            [
                "LVS",
                f"{s.matched_nets}/{s.schematic_nets} matched, {s.opens} open, {s.shorts} short, "
                f"{s.physical_nets} physical nets",
            ]
        )
        lvs_root.setData(0, ROLE_FINDING_KEY, "LVS")
        tree.addTopLevelItem(lvs_root)
        by_kind: dict[str, list[LvsIssue]] = {}
        for iss in lvs.issues:
            if needle and needle not in f"{iss.kind} {iss.message}".lower():
                continue
            by_kind.setdefault(iss.kind, []).append(iss)
        for kind in sorted(by_kind):
            kind_issues = by_kind[kind]
            kind_item = QTreeWidgetItem([kind, f"{len(kind_issues)}"])
            kind_item.setForeground(0, QColor(ERROR if kind in self._LVS_WRONG else WARNING))
            kind_item.setData(0, ROLE_FINDING_KEY, f"lvs:{kind}")
            lvs_root.addChild(kind_item)
            for iss in kind_issues:
                leaf = QTreeWidgetItem(["", iss.message])
                issue_refs = {p.component_ref for p in iss.pins}
                issue_component_ids = tuple(c.id for c in self.bus.document.components if c.ref in issue_refs)
                leaf.setData(0, ROLE_COMPONENT_IDS, issue_component_ids)
                kind_item.addChild(leaf)
            kind_item.setExpanded(f"lvs:{kind}" in expanded)
        lvs_root.setExpanded("LVS" in expanded or not expanded)

    #: Findings of one rule above which they are gathered by the conductor they are
    #: about. Three, because two or three rows are quicker to read than a heading.
    COLLAPSE_FINDINGS_ABOVE = 3

    def _add_drc_findings(
        self,
        rule_item: QTreeWidgetItem,
        rule: str,
        items: list[DrcViolation],
        expanded: set[str],
    ) -> None:
        """A rule's findings, gathered by conductor when there are enough to bury the rest.

        ``solder-trace-proximity`` is why this exists, and it is not the rule being wrong.
        Two traces run side by side for eight pads and it reports one finding per pad, per
        trace: sixteen rows saying the same sentence about the same pair of runs, and the
        panel's other findings scrolled off the bottom. The rule is a WARNING about the
        commonest way a perfboard build fails, and the engine's output is compared
        byte-for-byte against the reference implementation the port is proved against
        (tests/test_drc.py::test_matches_typescript_golden_drc), so the fix belongs here,
        where the problem actually is -- one row per run, opened to the pads underneath.
        """
        by_conductor: dict[str, list[DrcViolation]] = {}
        for violation in items:
            key = violation.conductor_ids[0] if violation.conductor_ids else ""
            by_conductor.setdefault(key, []).append(violation)

        gathered = len(items) > self.COLLAPSE_FINDINGS_ABOVE and any(
            key and len(group) > 1 for key, group in by_conductor.items()
        )
        if not gathered:
            for violation in items:
                rule_item.addChild(self._drc_leaf(violation))
            return

        for key, group in by_conductor.items():
            if not key or len(group) == 1:
                for violation in group:
                    rule_item.addChild(self._drc_leaf(violation))
                continue
            # Named by WHERE it runs rather than by the conductor's id, which is a
            # generated string a user has never seen. The addresses are the vocabulary
            # every other message in this application already speaks.
            holes = tuple(hole for violation in group for hole in violation.holes)
            span = f"{format_hole(group[0].holes[0])}–{format_hole(group[-1].holes[0])}"
            node = QTreeWidgetItem([span, f"{len(group)} {t('pads')}"])
            node.setData(0, ROLE_HOLES, holes)
            node.setData(0, ROLE_FINDING_KEY, f"drc:{rule}:{key}")
            for violation in group:
                node.addChild(self._drc_leaf(violation))
            node.setExpanded(f"drc:{rule}:{key}" in expanded)
            rule_item.addChild(node)

    @staticmethod
    def _drc_leaf(violation: DrcViolation) -> QTreeWidgetItem:
        leaf = QTreeWidgetItem(["", violation.message])
        leaf.setData(0, ROLE_HOLES, violation.holes)
        leaf.setData(0, ROLE_COMPONENT_IDS, violation.component_ids)
        return leaf

    def _expanded_drc_keys(self) -> set[str]:
        """Which groups are open, by NAME rather than by position.

        A rule that gained a violation moves down the tree, and one that lost its last
        one disappears -- so remembering row indices would restore the wrong groups
        exactly when the list changed, which is the only time it matters.
        """
        found: set[str] = set()

        def walk(item: QTreeWidgetItem | None) -> None:
            if item is None:
                return
            key = item.data(0, ROLE_FINDING_KEY)
            if item.isExpanded() and isinstance(key, str):
                found.add(key)
            for index in range(item.childCount()):
                walk(item.child(index))

        for index in range(self.drc_tree.topLevelItemCount()):
            walk(self.drc_tree.topLevelItem(index))
        return found

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
        # selected afterwards. Expansion is kept for the same reason -- a net opened to
        # take a pin off it must not close again the moment the pin is taken off.
        previously = set(self._selected_net_ids())
        expanded = self._expanded_net_ids()
        nodes_by_net = {net.id: net.nodes for net in self.bus.document.nets}
        needle = self.nets_filter.text().strip().lower()
        tree.blockSignals(True)
        tree.clear()
        for entry in nets:
            if needle and not self._net_matches(entry, nodes_by_net.get(entry.net_id, ()), needle):
                continue
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
            unresolved = {(p.component_ref, p.pin) for p in entry.unresolved_pins}
            if entry.unresolved_pins:
                pins = ", ".join(f"{p.component_ref}.{p.pin}" for p in entry.unresolved_pins)
                item.setToolTip(0, f"Not on the board: {pins}")
            # One child per pin the net claims. This is what makes the panel an editor
            # rather than a readout: a pin has to be visible to be selected, and it has
            # to be selectable to be taken off the net.
            for node in nodes_by_net.get(entry.net_id, ()):
                missing = (node.component_ref, node.pin) in unresolved
                pin_row = QTreeWidgetItem(
                    [
                        f"{node.component_ref}.{node.pin}",
                        "",
                        "",
                        "not on the board" if missing else "",
                    ]
                )
                pin_row.setData(0, ROLE_NET_ID, entry.net_id)
                pin_row.setData(0, ROLE_PIN, (node.component_ref, node.pin))
                pin_row.setForeground(0, QColor(WARNING if missing else TEXT_DIM))
                if missing:
                    pin_row.setForeground(3, QColor(WARNING))
                item.addChild(pin_row)
            tree.addTopLevelItem(item)
            item.setExpanded(entry.net_id in expanded)
            if entry.net_id in previously:
                item.setSelected(True)
        tree.blockSignals(False)
        self._refresh_net_actions()

    @staticmethod
    def _net_matches(entry: NetRatsnest, nodes: tuple[NetNode, ...], needle: str) -> bool:
        """Whether a net answers the filter box.

        Matched against the pins as well as the name, because half the time the question
        is "what is U1 pin 3 on" rather than "where is GND" -- and the pin rows are right
        there under the net now.
        """
        haystack = " ".join(
            [entry.net_name, entry.net_class, *(f"{n.component_ref}.{n.pin}" for n in nodes)]
        ).lower()
        return needle in haystack

    def _selected_net_ids(self) -> tuple[NetId, ...]:
        """The nets picked in the panel, deduplicated -- a net row and two of its pin rows
        are one net selected, not three."""
        ids = [
            item.data(0, ROLE_NET_ID)
            for item in self.nets_tree.selectedItems()
            if item.data(0, ROLE_NET_ID)
        ]
        return tuple(dict.fromkeys(ids))

    def _expanded_net_ids(self) -> set[str]:
        tree = self.nets_tree
        return {
            item.data(0, ROLE_NET_ID)
            for index in range(tree.topLevelItemCount())
            if (item := tree.topLevelItem(index)) is not None and item.isExpanded()
        }

    def _selected_pins(self) -> tuple[tuple[NetId, str, str], ...]:
        """(net id, component ref, pin) for every pin row selected in the panel."""
        picked: list[tuple[NetId, str, str]] = []
        for item in self.nets_tree.selectedItems():
            pin = item.data(0, ROLE_PIN)
            net_id = item.data(0, ROLE_NET_ID)
            if pin and net_id:
                picked.append((net_id, pin[0], pin[1]))
        return tuple(picked)

    def _refresh_net_actions(self) -> None:
        """The net actions need a net picked; the finish action needs a session running."""
        has_net = len(self._selected_net_ids()) == 1
        for action in self.net_actions:
            action.setEnabled(has_net)
        self.act_disconnect_pins.setEnabled(bool(self._selected_pins()))
        self.act_finish_pins.setEnabled(self.scene.armed_net_id is not None)

    def _on_net_selection_changed(self) -> None:
        net_ids = self._selected_net_ids()
        self.scene.set_highlighted_nets(net_ids)
        self._sync_schematic_highlight()
        self._refresh_net_actions()
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
        self._sync_schematic_highlight()
        for action in self.selection_actions:
            action.setEnabled(bool(components))
        # ...except that copper on its own is a block worth copying: a length of rail,
        # or the three traces that make an input stage. Rotate and Mirror mean nothing
        # without a part, so they stay off.
        copyable = bool(components) or bool(self.scene.selected_conductor_ids())
        self.act_copy.setEnabled(copyable)
        self.act_duplicate.setEnabled(copyable)
        # Properties edits ONE part: a reference is unique by definition, so a dialog over
        # three of them could only offer the value, and a field that silently overwrites
        # three values with one is not worth the two it destroys.
        self.act_properties.setEnabled(len(components) == 1)

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

    # -- running a planner without freezing the window ----------------------

    def _run_planner(
        self,
        label: str,
        work: Callable[[Callable[[], bool]], Any],
    ) -> Any:
        """Run a planner off the UI thread, with a progress dialog that can cancel it.

        Auto-place takes about a second on eight parts and autoroute about a third of
        one, and both used to happen on the UI thread behind a wait cursor -- so the
        window stopped repainting, stopped moving, and looked hung for exactly as long as
        the useful work took.

        The dialog is indeterminate rather than a percentage, because neither planner can
        honestly report progress: annealing does a fixed number of moves but the answer
        can arrive at any of them, and the router's work depends on what it finds. A fake
        percentage bar would be a lie about something the user is watching closely.

        ``work`` is handed a ``should_stop`` predicate. Cancelling asks the planner to
        stop and return its best result so far rather than discarding it, which for the
        placer means a worse placement, never an invalid one.
        """
        cancelled = False

        def should_stop() -> bool:
            return cancelled

        holder: dict[str, Any] = {}
        error: dict[str, BaseException] = {}

        class _Worker(QThread):
            def run(self) -> None:
                try:
                    holder["result"] = work(should_stop)
                except BaseException as exc:
                    error["exc"] = exc

        worker = _Worker(self)
        progress: QProgressDialog | None = None
        started = time.perf_counter()
        # Disabled rather than merely covered: the dialog only appears after the grace
        # period, and in that window a second Ctrl+R would otherwise re-enter this method
        # while a planner is already running against the same document.
        self.setEnabled(False)
        try:
            worker.start()
            while not worker.isFinished():
                QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
                if progress is None and time.perf_counter() - started > self.PLANNER_GRACE_S:
                    # Only now, so a run that finishes quickly never flashes a dialog: a
                    # window that blinks is worse than one that pauses imperceptibly.
                    progress = QProgressDialog(label, t("Cancel"), 0, 0, self)
                    progress.setWindowTitle(t("Working"))
                    progress.setWindowModality(Qt.WindowModality.WindowModal)
                    progress.setMinimumDuration(0)
                    progress.setAutoClose(False)
                    progress.setAutoReset(False)

                    # The dialog is bound at definition rather than read out of the
                    # enclosing scope. It is created once and this closure is made
                    # immediately after, so the two are the same object either way -- but
                    # a closure over a name reassigned inside a loop is the shape of a
                    # real bug, and writing it the safe way costs nothing and takes the
                    # None-check with it.
                    def on_cancel(dialog: QProgressDialog = progress) -> None:
                        nonlocal cancelled
                        cancelled = True
                        dialog.setLabelText(
                            f"{label}\nStopping, and keeping the best found so far…"
                        )

                    progress.canceled.connect(on_cancel)
                    progress.show()
            worker.wait()
        finally:
            if progress is not None:
                progress.close()
            self.setEnabled(True)

        if "exc" in error:
            raise error["exc"]
        return holder.get("result")

    #: How long a planner may run before a progress dialog appears. Long enough that
    #: anything interactive finishes first, short enough that a real wait is explained.
    PLANNER_GRACE_S = 0.35

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
            self.statusBar().showMessage(t("Nothing to place: the board is empty."), 6000)
            return
        if reroll:
            self._place_seed += 1

        document = self.bus.document
        options = PlacementOptions(seed=self._place_seed)
        t0 = time.perf_counter()
        plan = self._run_planner(
            "Trying arrangements, and routing each one to compare them…",
            lambda should_stop: plan_placement(
                document, self.lookup, options, should_stop=should_stop
            ),
        )
        elapsed = (time.perf_counter() - t0) * 1000
        if plan is None:
            return

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
            QMessageBox.warning(
                self,
                t("Placement refused"), f"[{result.code}] {result.message}")
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
        box.setWindowTitle(t("Apply this placement?"))
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
            self.statusBar().showMessage(t("Select a net in the Nets panel, or a part on the board, then route."), 6000
            )
            return
        self._route(only_net_ids=net_ids)

    def on_routing_style(self, style: str) -> None:
        """Choose which primitive the router reaches for first.

        Only recorded here; it takes effect on the next route. Re-routing the whole board
        the moment a menu item is ticked would discard work the user has not asked to
        lose -- and Route > Re-route Everything is right there for when they do.
        """
        self._routing_style = cast("StylePreference", style)
        for name, action in self.act_style.items():
            action.setChecked(name == style)
        self.statusBar().showMessage(
            f"{t('Preferred connection')}: {self.act_style[style].text().replace('&', '')}"
            f" — {t('applies to the next route')}",
            8000,
        )

    def _autoroute_options(self) -> AutorouteOptions:
        """Options for a single planned route.

        Under "best" the sweep applies each style itself, so this hands it the UNSTYLED
        defaults -- picking one here would prime every variant with another's cost table.
        """
        if self._routing_style == "best":
            return AutorouteOptions()
        return AutorouteOptions(router=options_for_style(self._routing_style))

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
            self.statusBar().showMessage(t("Select a net in the Nets panel, or a part on the board, then re-route."), 6000
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
            self.statusBar().showMessage(t("No netlist imported, so there is nothing to route."), 6000)
            return

        document = self.bus.document
        t0 = time.perf_counter()
        plan = self._run_planner(
            "Ripping up and routing again…",
            lambda _should_stop: plan_reroute(
                document, self.lookup, only_net_ids=only_net_ids,
                options=self._autoroute_options(),
            ),
        )
        elapsed = (time.perf_counter() - t0) * 1000
        if plan is None:
            return

        if plan.is_empty:
            self.statusBar().showMessage(f"Nothing to re-route ({elapsed:.0f} ms)", 6000)
            return

        if plan.remove_ids:
            answer = QMessageBox.question(
                self,
                t("Re-route?"),
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
            QMessageBox.warning(
                self,
                t("Re-route refused"), f"[{result.code}] {result.message}")
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
                self.statusBar().showMessage(t("No stale conductors: every one still connects the net it claims."), 6000
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
            self.statusBar().showMessage(t("No netlist imported, so there is nothing to route."), 6000)
            return

        # A stripboard is a different problem and gets a different planner: its copper is
        # already there, so the work is deciding where to BREAK it and what to link across
        # afterwards. Running the perfboard router on one would lay solder traces along
        # tracks that are already joined, and bare wire across a face that is solid copper.
        if is_stripboard(self.bus.document.board):
            self._route_stripboard(only_net_ids)
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
                t("Re-route the nets whose parts moved?"),
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

        document = self.bus.document
        options = self._autoroute_options()
        sweep = self._routing_style == "best"
        t0 = time.perf_counter()
        if sweep:
            best = self._run_planner(
                "Routing every style…",
                lambda _should_stop: plan_best_autoroute(
                    document, self.lookup, options, only_net_ids=only_net_ids
                ),
            )
            plan = best.plan if best is not None else None
        else:
            best = None
            plan = self._run_planner(
                "Routing…",
                lambda _should_stop: plan_autoroute(
                    document, self.lookup, options, only_net_ids=only_net_ids
                ),
            )
        elapsed = (time.perf_counter() - t0) * 1000
        if plan is None:
            return
        cleared_note = f"  ·  {cleared} stale conductor(s) removed first" if cleared else ""
        # Which style won, and how many it beat. Said in the status line rather than only in
        # the log, because a user who asked the tool to choose is owed the choice it made --
        # otherwise "best" is indistinguishable from the router having a good day.
        style_note = (
            f"  ·  {best.style} beat {best.considered - 1} other style"
            f"{'' if best.considered == 2 else 's'}"
            if best is not None
            else ""
        )

        if plan.is_empty:
            self.statusBar().showMessage(
                f"Nothing to route: {describe_plan(plan)}{cleared_note} ({elapsed:.0f} ms)", 8000
            )
            return

        # The full table goes on the status bar's tooltip: one hover away, and it stays
        # there. The winner was chosen on a judgement about build effort the user is
        # entitled to disagree with, and they cannot disagree with numbers they were never
        # shown -- but a modal dialog after every route would be nagging, and the four
        # styles remain pickable by hand for when they do disagree.
        self.statusBar().setToolTip(describe_best(best) if best is not None else "")

        result = self.bus.dispatch("conductor.addMany", plan.payload())
        if not result.ok:
            QMessageBox.warning(
                self,
                t("Routing refused"), f"[{result.code}] {result.message}"
            )
            return

        # Said after the commit, and said in full: the failures are the part a user must not
        # miss (PLAN.md Sec 13), so they go in the status line and, if there are any, into a
        # dialog that names each one.
        self.statusBar().showMessage(
            f"{describe_plan(plan)}{cleared_note}{style_note}  ({elapsed:.0f} ms)", 0
        )
        self._report_unrouted(plan)

    def _route_stripboard(self, only_net_ids: tuple[NetId, ...] | None) -> None:
        """Cut the tracks this board needs cutting, and link what is left apart.

        One command for both halves, because they are one decision: a board cut apart
        with nothing linking it, or linked with nothing cut, is a state nobody designed
        and either is one Ctrl+Z away if this commits them separately.
        """
        document = self.bus.document
        t0 = time.perf_counter()
        plan = self._run_planner(
            "Planning cuts and links…",
            lambda _should_stop: plan_stripboard(document, self.lookup, only_net_ids),
        )
        elapsed = (time.perf_counter() - t0) * 1000
        if plan is None:
            return

        if plan.is_empty:
            self.statusBar().showMessage(
                f"{describe_strip_plan(plan)} ({elapsed:.0f} ms)", 8000
            )
            self._report_strip_problems(plan)
            return

        result = self.bus.dispatch("stripboard.apply", plan.payload())
        if not result.ok:
            QMessageBox.warning(self, t("Routing refused"), f"[{result.code}] {result.message}")
            return

        self.statusBar().showMessage(
            f"{describe_strip_plan(plan)}  ({elapsed:.0f} ms)", 0
        )
        self._report_strip_problems(plan)

    def _report_strip_problems(self, plan: StripboardPlan) -> None:
        """What it could not do, named. PLAN.md §13's trap is the planner that leaves a
        handful of connections undone and does not say which."""
        if not plan.problems:
            return
        lines = [f"{problem.message}" for problem in plan.problems[:12]]
        if len(plan.problems) > 12:
            lines.append(f"... and {len(plan.problems) - 12} more.")
        QMessageBox.information(
            self,
            t("Some connections could not be made"),
            f"{len(plan.problems)} problem(s):\n\n" + "\n\n".join(lines),
        )

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
            t("Some connections could not be routed"),
            f"{len(failures)} connection(s) were left unrouted:\n\n" + "\n".join(lines),
        )

    # -- edit --------------------------------------------------------------

    def on_undo(self) -> None:
        self.bus.undo()

    def on_redo(self) -> None:
        self.bus.redo()

    def _refresh_undo_actions(self) -> None:
        """Grey out undo and redo when there is nothing behind or ahead, and name what
        they would do.

        The bus has always known both -- the window simply never asked, so the two actions
        were permanently enabled and an undo at the bottom of the stack looked identical to
        one that worked. The label carries the command's own description, which is the same
        string the status bar showed when it ran, so "Undo Place R4" needs no explaining.
        """
        history = self.bus.history()
        self.act_undo.setEnabled(self.bus.can_undo())
        self.act_redo.setEnabled(self.bus.can_redo())
        last = history[-1] if history and self.bus.can_undo() else ""
        self.act_undo.setToolTip(f"{t('Undo')} {last}" if last else t("Nothing to undo"))
        ahead = t("Redo the command you just took back")
        self.act_redo.setToolTip(ahead if self.bus.can_redo() else t("Nothing to redo"))

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

    # -- copy, paste, duplicate ----------------------------------------------
    #
    # The block itself -- what goes on the clipboard, what comes back, and what command
    # it becomes -- is ui/clipboard.py, which imports no Qt and is tested without a
    # display. What is left here is the three things a window has to own: reaching the
    # system clipboard, knowing where the pointer is, and saying what happened.

    def _copied_block(self) -> str:
        """The selection as clipboard text, or "" when nothing is selected."""
        components = [c.id for c in self._selected_components()]
        conductors = self.scene.selected_conductor_ids()
        if not components and not conductors:
            return ""
        return block_to_json(self.bus.document, components, conductors)

    def on_copy(self) -> None:
        text = self._copied_block()
        if not text:
            self.statusBar().showMessage(t("Select a part or a conductor on the board first, then copy it."), 6000
            )
            return
        QApplication.clipboard().setText(text)

        # Read back rather than counted here, because reading back is the path Paste
        # takes: a block that cannot be parsed is one that cannot be pasted, and this is
        # where that shows up rather than three keystrokes later.
        block = block_from_json(text)
        if block is None:
            return
        message = f"Copied {len(block.components)} part(s), {len(block.conductors)} conductor(s)"
        if block.orphaned_lead_bends:
            message += (
                f" — {block.orphaned_lead_bends} lead bend(s) left behind: a bent leg "
                f"belongs to a part that was not in the selection"
            )
        self.statusBar().showMessage(message, 8000)

    def on_paste(self) -> None:
        self._paste_text(QApplication.clipboard().text(), "Paste")

    def on_duplicate(self) -> None:
        """Copy and paste in one keystroke, and deliberately NOT through the clipboard.

        Duplicating a part is a board operation; it has no business throwing away what
        somebody had copied in another application to use in a minute.
        """
        text = self._copied_block()
        if not text:
            self.statusBar().showMessage(t("Select a part or a conductor on the board first, then duplicate it."), 6000
            )
            return
        self._paste_text(text, "Duplicate")

    def _paste_text(self, text: str, what: str) -> None:
        block = block_from_json(text)
        if block is None or block.is_empty:
            self.statusBar().showMessage(t("There is no block on the clipboard. Copy a part or some copper first."), 6000
            )
            return

        # Under the pointer when the pointer is on the board, because that is where a
        # person pasting is looking. Off the board -- the menu was used, or the pointer
        # has not moved yet -- it lands beside where the block was cut from.
        near = self._hovered_hole if self._pointer_is_on_board() else None
        at = paste_position(self.bus.document, block, near)
        paste = paste_payload(self.bus.document, block, at, label=f"{what} at {format_hole(at)}")
        if paste.is_empty:
            self.statusBar().showMessage(t("That block does not fit on this board."), 6000)
            return

        result = self.bus.dispatch("block.place", paste.payload)
        if not result.ok:
            self.statusBar().showMessage(f"[{result.code}] {result.message}", 8000)
            return

        # Selected, so the very next thing -- a drag, R, M -- lands on what was just
        # pasted rather than on whatever happened to be selected when it was copied.
        self.scene.select_components([spec.id for spec in paste.payload.components if spec.id])
        message = (
            f"{what}d {len(paste.payload.components)} part(s) and "
            f"{len(paste.payload.conductors)} conductor(s) at {format_hole(at)}"
        )
        if paste.dropped_conductors:
            message += f" — {paste.dropped_conductors} conductor(s) would have run off the board"
        self.statusBar().showMessage(message, 8000)

    def _pointer_is_on_board(self) -> bool:
        board = self.bus.document.board
        at = self._hovered_hole
        return 0 <= at.col < board.cols and 0 <= at.row < board.rows

    def on_go_to_part(self) -> None:
        """Find a part by name, select it, and put the view on it."""
        components = self.bus.document.components
        if not components:
            self.statusBar().showMessage(t("There are no parts on this board yet."), 6000)
            return
        dialog = GoToPartDialog(components, self.lookup, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen_id()
        if chosen is None:
            return
        self.go_to_component(chosen)

    def go_to_component(self, component_id: str) -> None:
        """Select a part and centre the view on it. Also what the DRC dock wants."""
        component = next((c for c in self.bus.document.components if c.id == component_id), None)
        if component is None:
            return
        self.scene.select_components([component_id])
        self.view.centerOn(
            hole_to_screen(component.anchor, self.bus.document.board, self.side)
        )
        self.statusBar().showMessage(
            f"{component.ref} at {format_hole(component.anchor)}", 6000
        )

    def on_cut_mode(self, checked: bool) -> None:
        """Arm or disarm cutting tracks."""
        self.scene.arm_cutting(checked)
        if checked:
            self.statusBar().showMessage(t("Click a hole to cut the strip there; click a cut again to take it back. "
                "Esc ends."),
                0,
            )
        else:
            self.statusBar().clearMessage()

    def _on_cut_armed(self, on: bool) -> None:
        """Keep the menu in step when the scene ends cutting by itself (Esc, right-click,
        or a board that has no tracks to cut)."""
        was = self.act_cut.blockSignals(True)
        self.act_cut.setChecked(on)
        self.act_cut.blockSignals(was)
        self._refresh_mode_banner()

    def _on_cut_made(self, result: Any) -> None:
        if result is None:
            return
        message = result.description if result.ok else f"[{result.code}] {result.message}"
        self.statusBar().showMessage(message, 6000)

    def on_measure_mode(self, checked: bool) -> None:
        """Arm or disarm the measuring tool."""
        self.scene.arm_measure(checked)
        if not checked:
            self.statusBar().clearMessage()

    def _on_measure_armed(self, on: bool) -> None:
        """Keep the menu in step when the scene ends measuring by itself (Esc, right-click).

        The action is blocked while it is set, because setChecked re-emits toggled and
        would arm the tool again from inside the disarming -- the same reason every other
        mode reports through its own signal rather than the menu keeping its own answer.
        """
        was = self.act_measure.blockSignals(True)
        self.act_measure.setChecked(on)
        self.act_measure.blockSignals(was)
        self._refresh_mode_banner()

    def _on_measured(self, text: str) -> None:
        """The measurement, live, in the status bar. Timeout 0: it stays until it changes,
        because it is being read off the screen while a part is held against the board."""
        if text:
            self.statusBar().showMessage(text, 0)
        else:
            self.statusBar().clearMessage()

    def on_stop_tool(self) -> None:
        """Leave whatever mode the board is in. What Escape does, from anywhere.

        Escape is a WINDOW shortcut, so it fires wherever the focus happens to be -- the
        parts list, a filter box -- and it fires before the scene sees the key at all.
        That is why this exists rather than the scene's key handler being enough: the
        handler was unreachable, so a part armed from the library could not be cancelled
        from the keyboard while the hint under the list said "Esc cancels".

        No unchecking here for the BOARD's modes. Each reports itself through its own
        signal, and the menu and toolbar are already kept in step by those -- doing it
        twice is how the two drift apart. The schematic panel has no such signal: it is a
        plain checkable button, so Escape unchecks it directly, and the toggle handler puts
        the sheet back into panning.
        """
        self.scene.leave_mode()
        if hasattr(self, "act_sch_wire") and self.act_sch_wire.isChecked():
            self.act_sch_wire.setChecked(False)
        self.statusBar().clearMessage()

    def on_draw_mode(self, kind: str, checked: bool) -> None:
        """Arm or disarm a drawing tool. Only one may be armed at a time.

        Only drawing: arming a tool makes the scene disarm the other three board modes
        itself, and leaving every mode at once is on_stop_tool.
        """
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
        self._refresh_mode_banner()

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
                QMessageBox.question(
                self,
                t("Delete conductors"), f"{label}?")
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
                t("Delete parts"),
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

    def on_component_properties(self, component_id: str = "") -> None:
        """Open one part's properties, from a double-click or from the selection.

        Dispatched as a single ``component.update`` even when all three fields changed,
        so OK is one undo step -- and skipped entirely when nothing changed, because an
        undo entry for a dialog somebody opened and closed is an undo entry that lies.
        """
        if component_id:
            component = next(
                (c for c in self.bus.document.components if c.id == component_id), None
            )
        else:
            selected = self._require_selection("edit its properties")
            component = selected[0] if len(selected) == 1 else None
            if selected and component is None:
                self.statusBar().showMessage(
                    t("Properties edits one part at a time — select a single part."), 6000
                )
        if component is None:
            return

        dialog = ComponentDialog(component, self.lookup(component.footprint_id), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        ref, value, locked = dialog.values()
        if not ref:
            QMessageBox.warning(
                self,
                t("A part needs a reference"),
                t("Every part is identified by its reference, so it cannot be blank."),
            )
            return
        if (ref, value, locked) == (component.ref, component.value, component.locked):
            return
        result = self.bus.dispatch(
            "component.update",
            UpdateComponentPayload(id=component.id, ref=ref, value=value, locked=locked),
        )
        if not result.ok:
            # A duplicate reference is the refusal this actually meets, and it is worth a
            # dialog rather than a status line: the edit the user typed is gone otherwise.
            QMessageBox.warning(self, t("Cannot change this part"), f"[{result.code}] {result.message}")
            return
        # Re-selected because the rebuild dropped the selection with the old item, and the
        # part somebody just named is the one they are still working on.
        self.scene.select_components([component.id])

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
        starter = create_starter_document(
            DocumentMeta(name="untitled", created=_now_iso(), modified=_now_iso())
        )
        dialog = BoardSetupDialog(starter.board, self, title=t("New Board"))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        # The fingers and corner holes come with the board, as they do in Board Setup.
        # Without this a preset chosen here produced the grid and none of the product,
        # which is the state `board.applyPreset` exists to make unreachable.
        features = dialog.preset_features()
        document = create_empty_document(
            DocumentMeta(name="untitled", created=_now_iso(), modified=_now_iso()),
            dialog.board(),
        )
        if features is not None:
            connectors, holes = features
            document = dataclasses.replace(
                document, edge_connectors=connectors, mounting_holes=holes
            )
        self.current_path = None
        self._disk_text = None
        self._watch_current_path()
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
        dialog = BoardSetupDialog(self.bus.document.board, self, title=t("Board Setup"))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        board = dialog.board()
        features = dialog.preset_features()
        if board == self.bus.document.board and features is None:
            return
        if features is None:
            result = self.bus.dispatch("board.set", SetBoardPayload(board=board))
        else:
            connectors, holes = features
            result = self.bus.dispatch(
                "board.applyPreset",
                ApplyBoardPresetPayload(
                    board=board,
                    edge_connectors=connectors,
                    mounting_holes=holes,
                    label=f"Use a {board.cols}x{board.rows} {board.material} board",
                ),
            )
        if not result.ok:
            QMessageBox.warning(
                self,
                t("Board not changed"),
                f"[{result.code}] {result.message}\n\nMove or delete whatever is in the way "
                "and try again.",
            )
            return
        self.view.fit_board()

    def on_board_features(self) -> None:
        """Mounting holes and edge connectors.

        No result handling here: the dialog dispatches its own commands as they are made,
        so by the time it closes the board is already whatever the user left it as, and
        the usual bus subscription has redrawn it.
        """
        BoardFeaturesDialog(self.bus, self).exec()

    # -- the files you were last working on -----------------------------------

    #: How many to keep. Eight is one screenful of menu and about as far back as anyone
    #: recognises a file name.
    RECENT_LIMIT = 8

    def _recent_paths(self) -> list[str]:
        stored = app_settings().value(RECENT_FILES_KEY, [])
        # QSettings hands back whatever the platform store round-tripped: a list on most
        # of them, a bare string when exactly one entry was saved, None when the key is
        # missing. All three have to become a list here or the menu builder inherits the
        # problem.
        if isinstance(stored, str):
            return [stored]
        if not isinstance(stored, list):
            return []
        return [str(entry) for entry in stored if entry]

    def _remember_path(self, path: Path) -> None:
        """Put a file at the top of the recent list, dropping any earlier mention of it."""
        resolved = str(path.resolve())
        kept = [entry for entry in self._recent_paths() if entry != resolved]
        app_settings().setValue(
            RECENT_FILES_KEY, [resolved, *kept][: self.RECENT_LIMIT]
        )
        self._refresh_recent_menu()

    def _refresh_recent_menu(self) -> None:
        """Rebuild the submenu from the stored list, skipping files that are gone.

        Skipped rather than shown greyed out: a list of names that no longer open anything
        is a list you stop reading, and the file was moved by the user, not lost by us.
        """
        menu = self.menu_recent
        menu.clear()
        existing = [Path(entry) for entry in self._recent_paths() if Path(entry).is_file()]
        if not existing:
            empty = menu.addAction(t("(nothing yet)"))
            empty.setEnabled(False)
            return
        for index, path in enumerate(existing, start=1):
            # &1..&8: the accelerator is the position, so it is stable while the names
            # underneath it are not.
            action = menu.addAction(f"&{index}  {path.name}")
            action.setToolTip(str(path))
            action.triggered.connect(lambda _checked=False, p=path: self.on_open_recent(p))
        menu.addSeparator()
        menu.addAction(t("&Clear List")).triggered.connect(self.on_clear_recent)

    def on_open_recent(self, path: Path) -> None:
        if not self._offer_to_save():
            return
        self._load_path(path)

    def on_clear_recent(self) -> None:
        app_settings().setValue(RECENT_FILES_KEY, [])
        self._refresh_recent_menu()

    def on_open(self) -> None:
        if not self._offer_to_save():
            return
        start_dir = str(self.current_path.parent) if self.current_path else str(Path.cwd())
        path_str, _ = QFileDialog.getOpenFileName(self, "Open .perf", start_dir, "PerfStudio documents (*.perf)")
        if not path_str:
            return
        self._load_path(Path(path_str))

    # -- the file, watched -----------------------------------------------------
    #
    # PLAN.md §9.3: the project file is agent-friendly precisely so that an agent that
    # only writes files still works. Without this the window is the one participant that
    # does not notice -- a Claude Code session edits the .perf, the board on screen goes
    # quietly stale, and the next save overwrites everything the agent did.
    #
    # A reload is not always the right answer, so the rule is stated rather than guessed:
    # a window with NO unsaved edits reloads itself, and a window with them says the file
    # changed and leaves the decision alone. Losing someone's work to a background event
    # is the one outcome that must not happen.

    #: Editors and agents write in bursts -- truncate, write, rename -- and each step is a
    #: change event. Reloading on the first would read a half-written file.
    _WATCH_SETTLE_MS = 250

    def _watch_current_path(self) -> None:
        """Watch the open document, and only it."""
        watcher = self._file_watcher
        if watcher is None:
            return
        for watched in watcher.files():
            watcher.removePath(watched)
        if self.current_path is not None and self.current_path.exists():
            watcher.addPath(str(self.current_path))

    def _on_file_changed(self, _path: str) -> None:
        # Re-armed on every event: a write that replaces the file rather than editing it
        # in place -- which is what most editors and every atomic writer do -- leaves Qt
        # watching an inode nobody will write to again.
        self._watch_timer.start(self._WATCH_SETTLE_MS)

    def _reload_if_changed(self) -> None:
        path = self.current_path
        if path is None:
            return
        self._watch_current_path()
        text, problem = read_document_text(path)
        if text is None:
            self.statusBar().showMessage(f"{path.name} changed on disk but could not be read: "
                                         f"{problem}", 8000)
            return
        if text == self._disk_text:
            return  # Our own save, or a write that changed nothing.

        if self.is_modified:
            # Not reloaded, and not silently ignored either. The file and the window have
            # both moved and only the person in front of it can say which one is right.
            self.statusBar().showMessage(
                f"{path.name} changed on disk, and this window has unsaved edits. "
                f"File ▸ Reload from Disk to take the file's version.",
                0,
            )
            return
        self._load_path(path, reason=f"Reloaded {path.name}: it changed on disk")

    def on_reload(self) -> None:
        """Take the version on disk, discarding whatever is in the window."""
        path = self.current_path
        if path is None:
            self.statusBar().showMessage(t("This board has never been saved, so there is "
                                         "nothing on disk to reload."), 6000)
            return
        if self.is_modified:
            answer = QMessageBox.question(
                self,
                t("Reload from disk?"),
                f"Discard the unsaved changes in this window and load {path.name} as it is "
                f"on disk?\n\nThis cannot be undone: reloading replaces the document, and "
                f"the undo history with it.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._load_path(path, reason=f"Reloaded {path.name}")

    def _load_path(self, path: Path, reason: str = "") -> None:
        # The file dialog only offers files that exist, but it does not guarantee one is
        # still there, still readable, or actually text by the time it is opened -- and an
        # unhandled read error here would take the whole window down over a bad pick.
        text, problem = read_document_text(path)
        if text is None:
            QMessageBox.critical(
                self,
                t("Open failed"), problem or "Could not read the file.")
            return
        result = persist.deserialize_document(text)
        if not result.ok:
            location = f" (at {result.path})" if result.path else ""
            QMessageBox.critical(
                self,
                t("Open failed"), f"[{result.code}] {result.message}{location}")
            return
        self.current_path = path
        self._disk_text = text
        self.bus = self._new_bus(result.document)
        self._subscribe_bus()
        self.scene.bus = self.bus
        self.on_bus_changed(self.bus.document, None)
        # The viewport is left alone on a reload: somebody watching an agent work is
        # looking at a particular corner of the board, and refitting on every write would
        # snatch it back to the whole board a dozen times a minute.
        if not reason:
            self.view.fit_board()
        note = f" ({len(result.warnings)} warning(s))" if result.warnings else ""
        self.statusBar().showMessage(f"{reason or f'Loaded {path.name}'}{note}", 8000)
        self._mark_saved()
        self._remember_path(path)
        self._watch_current_path()

    # -- nets, entered by hand -----------------------------------------------
    #
    # The other half of the netlist story. Import replaces the whole netlist because that
    # is what re-exporting a schematic means; these edit it, one decision at a time, on a
    # board where the pins are already in front of you.

    def on_new_net(self) -> None:
        """Name a net, then go straight into clicking its pins.

        Arming the pin mode is not a convenience: naming a net and then hunting for the
        command that fills it would be two decisions where the user made one, and an
        empty net is the one state that does nothing for anybody.
        """
        dialog = NetDialog(self, title=t("New Net"))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, net_class, current_a, voltage_v = dialog.values()
        result = self.bus.dispatch(
            "net.add",
            AddNetPayload(
                name=name, net_class=net_class, current_a=current_a, voltage_v=voltage_v
            ),
        )
        if not result.ok:
            self.statusBar().showMessage(f"[{result.code}] {result.message}", 8000)
            return
        net = next((n for n in self.bus.document.nets if n.name == name), None)
        if net is None:  # pragma: no cover - the command just made it
            return
        self._select_net(net.id)
        self.scene.arm_net_pins(net.id)

    # -- the two-click connect tool -------------------------------------------

    def on_connect_tool(self, checked: bool) -> None:
        self.scene.arm_connect(checked)

    def _on_connect_armed(self, on: bool) -> None:
        self.act_connect.setChecked(on)
        self._refresh_mode_banner()
        if on:
            self.statusBar().showMessage(t("Click a pin, then the pin it joins. Neither on a net yet? One gets made. "
                "Esc cancels."),
                0,
            )
        else:
            self.statusBar().clearMessage()

    def _on_connect_progress(self, picked: list[Any]) -> None:
        self._refresh_mode_banner()
        if picked:
            self.statusBar().showMessage(f"From {picked[0]} — click the pin it joins.", 0)

    def _on_pins_connected(self, result: Any) -> None:
        if result is None:
            return
        message = result.description if result.ok else f"[{result.code}] {result.message}"
        self.statusBar().showMessage(message, 6000)

    def on_add_pins_to_net(self) -> None:
        net_id = self._one_selected_net()
        if net_id is None:
            return
        self.scene.arm_net_pins(net_id)

    def on_finish_adding_pins(self) -> None:
        self.scene.commit_net_pins()

    def on_edit_net(self) -> None:
        net_id = self._one_selected_net()
        net = next((n for n in self.bus.document.nets if n.id == net_id), None)
        if net is None:
            return
        dialog = NetDialog(
            self,
            title=t("Edit Net"),
            name=net.name,
            net_class=net.net_class,
            current_a=net.current_a,
            voltage_v=net.voltage_v,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, net_class, current_a, voltage_v = dialog.values()
        # current/voltage are passed straight through, so clearing a field in the dialog
        # clears it on the net. That is what the KEEP sentinel on the payload is for:
        # this caller always knows both values, so it always states both.
        result = self.bus.dispatch(
            "net.update",
            UpdateNetPayload(
                id=net.id,
                name=name,
                net_class=net_class,
                current_a=current_a,
                voltage_v=voltage_v,
            ),
        )
        if not result.ok:
            self.statusBar().showMessage(f"[{result.code}] {result.message}", 8000)

    def on_disconnect_pins(self) -> None:
        """Take the pins selected in the Nets panel off their nets.

        One command per net, so disconnecting three pins of GND is a single undo step
        even when the selection spans two nets.
        """
        picked = self._selected_pins()
        if not picked:
            self.statusBar().showMessage(t("Select the pins to disconnect in the Nets panel — expand a net to see "
                "them."),
                6000,
            )
            return
        by_net: dict[NetId, list[NetNode]] = {}
        for net_id, ref, pin in picked:
            by_net.setdefault(net_id, []).append(NetNode(component_ref=ref, pin=pin))
        results = [
            self.bus.dispatch(
                "net.disconnect", DisconnectPinsPayload(id=net_id, nodes=tuple(nodes))
            )
            for net_id, nodes in by_net.items()
        ]
        self._report_refusals(results, f"Disconnected {len(picked)} pin(s)")

    def on_delete_net(self) -> None:
        net_id = self._one_selected_net()
        net = next((n for n in self.bus.document.nets if n.id == net_id), None)
        if net is None:
            return
        # The copper is the part a person will not expect to survive, so it is said here
        # rather than discovered afterwards.
        freed = sum(1 for c in self.bus.document.conductors if c.net_id == net.id)
        note = (
            f"\n\n{freed} conductor(s) already laid for it stay on the board, and stop "
            f"being anything re-route or the stale sweep will touch."
            if freed
            else ""
        )
        if (
            QMessageBox.question(self, t("Delete net"), f"Delete net {net.name}?{note}")
            != QMessageBox.StandardButton.Yes
        ):
            return
        result = self.bus.dispatch("net.delete", DeleteNetPayload(id=net.id))
        if not result.ok:
            self.statusBar().showMessage(f"[{result.code}] {result.message}", 8000)

    # -- the pin-picking session, reported as it goes -------------------------

    def _on_net_pins_armed(self, net_id: str) -> None:
        self.act_finish_pins.setEnabled(bool(net_id))
        self._refresh_mode_banner()
        if not net_id:
            self.statusBar().clearMessage()
            return
        self.statusBar().showMessage(f"{self._pin_session_prefix(net_id)} — {self.PIN_HINT}", 0)

    def _on_net_pins_changed(self, labels: list[Any]) -> None:
        self._refresh_mode_banner()
        net_id = self.scene.armed_net_id
        if not net_id:
            return
        picked = ", ".join(str(label) for label in labels) if labels else "no pins yet"
        self.statusBar().showMessage(
            f"{self._pin_session_prefix(net_id)}: {picked} — {self.PIN_HINT}", 0
        )

    def _on_net_pin_rejected(self, reason: str) -> None:
        """A refused click keeps the hint beside it rather than replacing it.

        A timed message would expire back to an empty status bar and take the "Enter
        finishes" line with it, in the middle of a session that is still running.
        """
        hint = f" — {self.PIN_HINT}" if self.scene.armed_net_id else ""
        self.statusBar().showMessage(f"{reason}{hint}", 0)

    def _on_net_pins_committed(self, result: Any) -> None:
        if result is None:
            return
        message = (
            result.description if result.ok else f"[{result.code}] {result.message}"
        )
        self.statusBar().showMessage(message, 8000)

    #: Said at every stage of a pin session, because the two ways out of a mode are the
    #: thing a person needs and the thing a mode never says.
    PIN_HINT = "click each pin; Enter or right-click finishes, Esc cancels"

    def _pin_session_prefix(self, net_id: str) -> str:
        return f"Adding pins to {self._net_name(net_id)}"

    def _net_name(self, net_id: str) -> str:
        return next((n.name for n in self.bus.document.nets if n.id == net_id), net_id)

    def _one_selected_net(self) -> NetId | None:
        net_ids = self._selected_net_ids()
        if len(net_ids) != 1:
            self.statusBar().showMessage(t("Select one net in the Nets panel first."), 6000)
            return None
        return net_ids[0]

    def _select_net(self, net_id: NetId) -> None:
        tree = self.nets_tree
        tree.clearSelection()
        for index in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(index)
            if item is not None and item.data(0, ROLE_NET_ID) == net_id:
                item.setSelected(True)
                tree.setCurrentItem(item)
                return

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
        self.import_netlist_from(Path(path_str))

    def import_netlist_from(self, path: Path) -> None:
        """The import itself, without the file dialog in front of it.

        Split out because a netlist can arrive two ways -- chosen from the menu, or
        dropped on the window -- and a drop that imported through a second, similar-looking
        code path would eventually stop reporting the warnings this one does.
        """
        text, problem = read_document_text(path)
        if text is None:
            QMessageBox.critical(
                self,
                t("Import failed"), problem or "Could not read the file.")
            return
        try:
            imported = parse_kicad_netlist(text)
        except ValueError as err:
            QMessageBox.critical(
                self,
                t("Import failed"), f"{path.name}: {err}")
            return

        result = self.bus.dispatch("netlist.import", ImportNetlistPayload(nets=imported.nets))
        if not result.ok:
            QMessageBox.critical(
                self,
                t("Import failed"), f"[{result.code}] {result.message}")
            return

        note = f"Imported {len(imported.nets)} net(s) from {path.name}"
        if imported.warnings:
            shown = "\n".join(f"  • {w}" for w in imported.warnings[:12])
            more = f"\n  … and {len(imported.warnings) - 12} more" if len(imported.warnings) > 12 else ""
            QMessageBox.information(
                self, t("Imported with warnings"), f"{note}, with warnings:\n\n{shown}{more}"
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
            t("Place the missing parts?"),
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

        if not specs:
            return
        # ONE command, which is what the docstring above has always promised and what
        # this could not deliver until block.place existed: dispatched one at a time, a
        # thirty-part netlist took thirty presses of Ctrl+Z to take back, and each press
        # left a board that was half-imported.
        result = self.bus.dispatch(
            "block.place",
            PlaceBlockPayload(
                components=tuple(specs), label=f"Place {len(specs)} imported part(s)"
            ),
        )
        if not result.ok:
            self.statusBar().showMessage(f"[{result.code}] {result.message}", 10000)
            return
        skipped = len(wanted) - len(specs)
        message = f"Placed {len(specs)} part(s)"
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
        box.setWindowTitle(t("Unsaved changes"))
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

    # -- files dropped on the window ------------------------------------------
    #
    # A board and a netlist both arrive as files, and the gesture everyone tries first
    # with a file is to drag it onto the window. Nothing happened: the drop was refused
    # without a word, which reads as the application not accepting that KIND of file
    # rather than not accepting drops at all.

    #: What a dropped file is taken to be, by extension. `.net` is KiCad's exported
    #: netlist, which File ▸ Import already reads.
    DROPPED_DOCUMENTS = (".perf",)
    DROPPED_NETLISTS = (".net",)

    def _dropped_path(self, event: Any) -> Path | None:
        """The one local file being dragged, if it is a kind this window can open."""
        data = event.mimeData()
        if not data.hasUrls():
            return None
        urls = [url for url in data.urls() if url.isLocalFile()]
        if len(urls) != 1:
            # Deliberately not "open the first of them": a drop of five boards is an
            # instruction this window cannot carry out, and picking one at random is a
            # worse answer than declining.
            return None
        path = Path(urls[0].toLocalFile())
        known = self.DROPPED_DOCUMENTS + self.DROPPED_NETLISTS
        return path if path.suffix.lower() in known else None

    def dragEnterEvent(self, event: Any) -> None:
        if self._dropped_path(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: Any) -> None:
        path = self._dropped_path(event)
        if path is None:
            event.ignore()
            return
        event.acceptProposedAction()
        if path.suffix.lower() in self.DROPPED_NETLISTS:
            self.import_netlist_from(path)
            return
        # A board REPLACES what is in this window, so it goes through the same unsaved-work
        # guard the Open menu item does. Losing an hour of layout to a slip of the mouse is
        # exactly the outcome that guard exists for.
        if self._offer_to_save():
            self._load_path(path)

    def closeEvent(self, event: Any) -> None:
        """The last thing standing between an hour of layout and the X button."""
        if self._offer_to_save():
            # AFTER the offer, never before: a close the user backed out of must not
            # record the layout as if they had left.
            self._save_session()
            # A download in flight is abandoned rather than left running into a window
            # that is closing, which is also what removes its half-written .part file --
            # the transfer's own cancel path does that, and nothing else will.
            if self._update_checker is not None:
                self._update_checker.cancel()
            event.accept()
        else:
            event.ignore()

    # -- what the window remembers between runs ------------------------------
    #
    # Every one of these was reset on every launch, and the cost is paid by exactly the
    # people who use the tool most: a maker with a 27" screen re-dragged the DRC panel and
    # re-opened the 3D view every single time, and somebody who had chosen the blue
    # solder mask to match the board in their hand got green again the next morning.

    def _save_session(self) -> None:
        settings = app_settings()
        settings.setValue(GEOMETRY_KEY, self.saveGeometry())
        settings.setValue(WINDOW_STATE_KEY, self.saveState())
        settings.setValue(BOARD_COLOUR_KEY, chosen_board_colour() or "")
        settings.setValue(RATSNEST_KEY, self.act_ratsnest.isChecked())
        settings.setValue(RULERS_KEY, self.act_rulers.isChecked())
        settings.setValue(HATCH_KEY, self.act_hatch.isChecked())
        settings.setValue(ROUTING_STYLE_KEY, self._routing_style)

    def _restore_session(self) -> None:
        """Put the window back where it was, quietly.

        Every toggle is applied by setting the action, which emits ``toggled`` and takes
        the scene with it -- so there is one path from a preference to the board, whether
        the user ticked the menu item or the store did. The routing style is the exception
        and goes in by hand: its handler puts a line in the status bar explaining that the
        change applies to the next route, which is an answer to a question nobody asked
        while the window was still opening.
        """
        settings = app_settings()
        geometry = settings.value(GEOMETRY_KEY)
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = settings.value(WINDOW_STATE_KEY)
        if state is not None:
            self.restoreState(state)
        # ...and then shut the 3D panel regardless of how it was left. Restoring it open
        # would build VTK's whole pipeline during startup -- an OpenGL context and a few
        # thousand actors -- to show a board the user has not looked at yet. Its SIZE and
        # position come back with the rest of the state, so opening it lands where they
        # put it. See _build_3d_dock for the three costs this avoids.
        self.dock_3d.hide()

        colour = settings.value(BOARD_COLOUR_KEY, "")
        if isinstance(colour, str) and colour in self.act_colour:
            self.on_board_colour(colour or None)
        self.act_ratsnest.setChecked(_stored_bool(settings, RATSNEST_KEY, True))
        self.act_rulers.setChecked(_stored_bool(settings, RULERS_KEY, True))
        self.act_hatch.setChecked(_stored_bool(settings, HATCH_KEY, True))

        style = settings.value(ROUTING_STYLE_KEY, self._routing_style)
        if isinstance(style, str) and style in self.act_style:
            self._routing_style = cast("StylePreference", style)
            for name, action in self.act_style.items():
                action.setChecked(name == style)

    def _save_to(self, path: Path) -> None:
        # meta.modified is host-stamped, not part of any command (core has no clock --
        # see persist.py/commands.py) -- so this replaces the document's meta for the
        # SERIALIZED copy only, without pushing that change through the bus.
        doc = self.bus.document
        stamped = dataclasses.replace(doc, meta=dataclasses.replace(doc.meta, modified=_now_iso()))
        text = persist.serialize_document(stamped)
        path.write_text(text, encoding="utf-8")
        # Remembered so the watcher can tell this write from somebody else's: a save
        # changes the file, and a window that reloaded itself after every save would
        # throw away its own undo history for nothing.
        self._disk_text = text
        self._mark_saved()
        self._remember_path(path)
        self._watch_current_path()
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

    def on_board_colour(self, key: str | None) -> None:
        """Recolour the board in both views at once.

        Both, from one scheme, deliberately: a board that was green in the editor and blue
        in the 3D view would undermine the one job the 3D view has, which is letting
        someone check that what they are about to solder is what they meant.
        """
        choose_board_colour(key)
        for name, action in self.act_colour.items():
            action.setChecked(name == (key or ""))
        self.scene.set_document(self.bus.document)
        self._3d_stale = True
        self._refresh_3d()

    def on_language(self, code: str) -> None:
        """Record the interface language. It takes effect at the next start.

        Not applied live, and the reason is worth stating rather than apologising for:
        every label in this window was translated once, as it was built -- menu items,
        tooltips, dock titles, the shortcut card, the empty-board guidance. Re-translating
        them would mean rebuilding the menu bar under somebody's pointer, and the widgets
        a rebuild missed would be exactly the ones nobody would notice had stayed English.
        Saying "next time" is honest; a half-translated window would not be.
        """
        app_settings().setValue(LANGUAGE_KEY, code)
        for name, action in self.act_language.items():
            action.setChecked(name == code)
        QMessageBox.information(
            self,
            t("Language changed"),
            t(
                "The interface is built in one language when the window opens, so the new "
                "one appears the next time PerfStudio starts."
            ),
        )

    def on_export_guide(self) -> None:
        """Write the build guide beside the document, and say what it could not cover.

        Four files rather than one, because they get used in different places: the HTML
        on a phone at the bench, the CSVs in a spreadsheet or an order, the JSON by
        whatever comes next. The 1:1 PDF sheets are a separate export because they are a
        separate thing -- a template you hold against the board, not a document you read.
        """
        base = self.current_path.with_suffix("") if self.current_path else Path.cwd() / "board"
        guide = build_guide(self.bus.document, self.lookup)

        # One 3D render per step, before anything is written. The cursor is the only
        # feedback worth giving: it is well under a second on a board of this size,
        # because the render window is built once and re-actored per step rather than
        # stood up again for each one.
        # ...and only where there is something to render into. On a machine with no
        # offscreen GL -- a VM, a remote session, an old driver -- VTK does not raise,
        # it ends the process, so exporting a guide would take the application down with
        # every unsaved edit in it. A guide without pictures is still a complete guide;
        # losing the board is not recoverable.
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            images = (
                view3d.render_step_images(self.bus.document, guide, self.lookup)
                if view3d.offscreen_gl_available()
                else {}
            )
        finally:
            QApplication.restoreOverrideCursor()

        written: list[Path] = []
        try:
            for suffix, text in (
                ("_guide.html", guide_to_html(guide, images)),
                ("_cut_list.csv", cut_list_to_csv(guide)),
                ("_bom.csv", bom_to_csv(guide)),
                ("_guide.json", guide_to_json(guide)),
            ):
                path = base.with_name(base.name + suffix)
                path.write_text(text, encoding="utf-8")
                written.append(path)
        except OSError as err:
            QMessageBox.critical(
                self,
                t("Export failed"), f"Could not write the guide: {err}")
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
                t("The guide has gaps"),
                f"Written to {written[0].parent}, with {len(guide.warnings)} thing(s) it "
                f"could not cover:\n\n{lines}",
            )
        self._offer_to_open(written)

    def on_export_schematic(self) -> None:
        """Write the circuit as a sheet beside the document: SVG, PDF and PNG.

        Three files for the reason the guide writes four -- they get used in different
        places. The SVG is the drawing itself, vector, editable in any illustration tool and
        embeddable in a page; the PDF is the one you print and put next to the board; the
        PNG is what gets pasted into the message that asks somebody why the circuit does not
        work, which is the single most useful thing to attach to that message.

        ALL THREE COME OUT OF THE SVG (``ui/export_schematic.py``), so they cannot disagree
        about what the circuit is. And the sheet is built here rather than read off the
        panel: the panel may never have been opened, and the export must not depend on
        whether somebody looked at it first.
        """
        base = self.current_path.with_suffix("") if self.current_path else Path.cwd() / "board"
        drawing = build_schematic(self.bus.document, self.lookup)
        if not drawing.symbols:
            QMessageBox.information(
                self, t("Nothing to export"), t("This document has no parts to draw yet.")
            )
            return

        svg = drawing_to_svg(drawing, title=self.bus.document.meta.name)
        written: list[Path] = []
        try:
            sheet = base.with_name(base.name + "_schematic.svg")
            sheet.write_text(svg, encoding="utf-8")
            written.append(sheet)
            written.append(
                svg_to_pdf(
                    svg,
                    base.with_name(base.name + "_schematic.pdf"),
                    title=self.bus.document.meta.name,
                )
            )
            written.append(svg_to_png(svg, base.with_name(base.name + "_schematic.png")))
        except (OSError, SchematicRenderError) as err:
            QMessageBox.critical(
                self, t("Export failed"), f"Could not write the schematic: {err}"
            )
            return

        self.statusBar().showMessage(
            f"{len(drawing.symbols)} part(s) — {written[0].name} and {len(written) - 1} more",
            8000,
        )
        self._offer_to_open(
            written, title=t("Schematic written"), open_label=t("Open the Sheet")
        )

    def _offer_to_open(
        self, written: list[Path], title: str | None = None, open_label: str | None = None
    ) -> None:
        """Several files, and a way to reach them.

        The export used to end at a line in the status bar naming a file in a directory
        the user then had to go and find. The guide is the thing this whole application
        is for -- somebody is standing at a bench about to solder -- so the last step of
        producing it should not be a file-manager expedition. The schematic export borrows
        it with its own two words, because the same objection applies to a sheet.

        ``written[0]`` is the one the Open button opens, so a caller puts the file a person
        would actually read first in the list.
        """
        box = QMessageBox(self)
        box.setWindowTitle(title or t("Build guide written"))
        box.setText(f"<b>{written[0].name}</b>")
        box.setInformativeText(
            t("Written to {folder}, with {count} other files.").format(
                folder=written[0].parent, count=len(written) - 1
            )
        )
        open_it = box.addButton(
            open_label or t("Open the Guide"), QMessageBox.ButtonRole.AcceptRole
        )
        show_it = box.addButton(t("Show the Folder"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(t("Close"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_it:
            # The HTML, which is the one written to be read: offline, on a phone at the
            # bench. Handed to the desktop rather than to a browser by name, because
            # which browser is not this application's business.
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(written[0])))
        elif box.clickedButton() is show_it:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(written[0].parent)))

    def on_shortcuts(self) -> None:
        ShortcutsDialog(self._menus, self).exec()

    def on_about(self) -> None:
        """The version, in a form someone can copy into a bug report.

        Selectable text rather than a picture: the whole point of the line is that it can be
        pasted, and QMessageBox renders it unselectable unless asked.
        """
        box = QMessageBox(self)
        box.setWindowTitle(t("About PerfStudio"))
        box.setText(f"<b>PerfStudio {__version__}</b>")
        box.setInformativeText(
            f"{describe_version()}\n\n"
            "Perfboard layout design, verification and a soldering guide.\n"
            "Apache-2.0 · github.com/medinstech/perfstudio"
        )
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()

    # -- updates -------------------------------------------------------------
    #
    # PLAN.md §14's last unwritten line. Every decision lives in updates.py and every
    # byte on the wire in ui/updater.py; what is left here is the policy a window has to
    # hold -- when to look, who to tell, and what a Hide means.

    def consider_checking_for_updates(self) -> None:
        """The daily check, and the one question that has to be answered before it.

        Called from ``main()`` a moment after the window is on screen, never from the
        constructor: a check is a network request, and a window that makes one while it
        is being built makes one in every test that builds a window.
        """
        settings = app_settings()
        preference = updater.stored_preference(settings)
        if preference is None:
            preference = self._ask_about_updates(settings)
        if not preference:
            return
        if not is_check_due(updater.last_checked(settings), updater.now_iso()):
            return
        self._start_update_check(by_hand=False)

    def _ask_about_updates(self, settings: QSettings) -> bool:
        """Asked once, on the first run, and remembered.

        An update check is a request to GitHub carrying this machine's address, and a
        tool that quietly starts making them has decided something on the user's behalf.
        One sentence, two buttons, and either answer is a menu item away afterwards.
        """
        box = QMessageBox(self)
        box.setWindowTitle(t("Check for updates?"))
        box.setText(t("Should PerfStudio look for new versions?"))
        box.setInformativeText(
            t(
                "It would ask GitHub once a day, as the window opens, and tell you when a "
                "newer release exists. Nothing is downloaded or installed without you "
                "asking for it. Either answer can be changed in the Help menu."
            )
        )
        yes = box.addButton(t("Check for Updates"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(t("Do Not Check"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        enabled = box.clickedButton() is yes
        updater.remember_preference(settings, enabled)
        self.act_auto_updates.setChecked(enabled)
        return enabled

    def on_check_for_updates(self) -> None:
        """Help > Check for Updates. Says something either way, because it was asked."""
        self.statusBar().showMessage(t("Checking for updates…"), 4000)
        self._start_update_check(by_hand=True)

    def _start_update_check(self, *, by_hand: bool) -> None:
        self._update_asked_by_hand = by_hand
        self._checker().check(__version__)

    def _checker(self) -> updater.UpdateChecker:
        """The checker, built on first use and then kept: one object owns the transfer."""
        if self._update_checker is None:
            checker = updater.UpdateChecker(self)
            checker.checked.connect(self._on_update_checked)
            checker.checkFailed.connect(self._on_update_check_failed)
            checker.downloadProgress.connect(self.update_bar.show_progress)
            checker.downloaded.connect(self._on_update_downloaded)
            checker.downloadFailed.connect(self.update_bar.show_failure)
            self._update_checker = checker
        return self._update_checker

    def _on_update_checked(self, release: Release | None) -> None:
        settings = app_settings()
        # Stamped on a finished check and not on a failed one, so that a laptop which was
        # offline this morning looks again this afternoon rather than tomorrow.
        updater.remember_check(settings, updater.now_iso())
        if release is None:
            if self._update_asked_by_hand:
                self.statusBar().showMessage(
                    t("PerfStudio {version} is the newest release.").format(version=__version__),
                    6000,
                )
            return
        # A version somebody pressed Hide on stays hidden -- unless they came from the
        # menu, in which case they have just asked about exactly that version.
        if not self._update_asked_by_hand and release.version == updater.skipped_version(settings):
            return
        self._update_release = release
        self.update_bar.announce(
            release, __version__, downloadable=updater.installable_asset(release) is not None
        )

    def _on_update_check_failed(self, message: str) -> None:
        # Only when somebody asked. An automatic check that could not reach GitHub has
        # found out nothing, and nothing is not worth a strip across somebody's board.
        if self._update_asked_by_hand:
            self.statusBar().showMessage(
                t("Could not check for updates: {reason}").format(reason=message), 8000
            )

    def _on_update_download(self) -> None:
        release = self._update_release
        if release is None:  # pragma: no cover - the button only exists after a check
            return
        asset = updater.installable_asset(release)
        if asset is None:
            # A pip install, or a machine this project publishes no installer for. The
            # release page answers both, and it is where "install from source" is written.
            updater.open_url(release.url)
            return
        self.update_bar.show_progress(0, asset.size)
        self._checker().download(release, asset)

    def _on_update_notes(self) -> None:
        release = self._update_release
        updater.open_url(release.url if release is not None else RELEASES_PAGE_URL)

    def _on_update_reveal(self) -> None:
        if self._downloaded_update is not None:
            updater.open_in_file_manager(self._downloaded_update)

    def _on_update_cancel(self) -> None:
        if self._update_checker is not None:
            self._update_checker.cancel()
        self.update_bar.dismiss()

    def _on_update_downloaded(self, path: str, verified: bool) -> None:
        self._downloaded_update = path
        self.update_bar.show_downloaded(path, verified)

    def _on_update_dismissed(self) -> None:
        if self._update_release is not None:
            updater.remember_skip(app_settings(), self._update_release.version)
        self.update_bar.dismiss()

    def _on_automatic_updates_toggled(self, enabled: bool) -> None:
        updater.remember_preference(app_settings(), enabled)



def _language_argument(argv: list[str]) -> str | None:
    """``--lang tr`` or ``--lang=tr``, or None to work it out from the environment."""
    for index, arg in enumerate(argv):
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1]
        if arg == "--lang" and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _preferred_language(argv: list[str]) -> str | None:
    """The language to start in: the flag, then the variable, then the last choice made.

    Returning None hands the question back to ``set_language``, which reads
    PERFSTUDIO_LANG and then the system locale. That is why the variable is not read
    here: one place reads it, so the two cannot end up disagreeing about precedence.
    The stored choice sits BELOW the variable deliberately -- an environment variable is
    set for this run, and a menu choice was made for every run.
    """
    from_flag = _language_argument(argv)
    if from_flag:
        return from_flag
    if os.environ.get("PERFSTUDIO_LANG"):
        return None
    stored = app_settings().value(LANGUAGE_KEY)
    return stored if isinstance(stored, str) and stored else None


def _apply_application_icon(app: QApplication) -> None:
    """The taskbar and window icon, if the generated mark is on disk.

    Unlike the toolbar icons -- which ui/icons.py draws in code precisely so there are no
    files to find -- the application mark has to exist as a real file for the Windows
    installer and the AppImage's desktop entry to point at, so the one that ships is
    generated by ``tools/make_assets.py`` and committed. This loads that same file rather
    than drawing a second one, so the window, the executable and the Start Menu entry
    cannot disagree.

    Silently skipped when it is missing. A working tree that has never run make_assets.py
    should still start; an icon is not worth a traceback.
    """
    icon_path = Path(__file__).resolve().parent / "assets" / "perfstudio.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))


def main() -> int:
    # Answered before Qt is touched: --version has to work on a machine where the GUI
    # cannot start, since "it will not launch" is exactly when someone is asked which
    # version they have.
    if "--version" in sys.argv or "-V" in sys.argv:
        print(describe_version())
        return 0

    # Before Qt, and before anything else: this is the child process of
    # view3d.offscreen_gl_available, whose entire job is to find out whether VTK can open
    # an offscreen context here -- by crashing where a crash costs nothing if it cannot.
    # Not a documented option; see PROBE_FLAG.
    if view3d.PROBE_FLAG in sys.argv:
        return view3d.probe_offscreen_gl()

    # Chosen before the window is built, because every menu label is translated once at
    # construction. --lang wins over PERFSTUDIO_LANG, which wins over the View menu's own
    # choice, which wins over the system locale. See _preferred_language.
    set_language(_preferred_language(sys.argv))

    if "--headless" in sys.argv:
        # Imported here rather than at module scope: the headless run is a program of its
        # own (ui/headless.py) and a GUI start has no reason to pull it in.
        from .headless import headless

        return headless([a for a in sys.argv[1:] if a != "--headless"])

    app = QApplication(sys.argv)
    _apply_application_icon(app)
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
        document = create_starter_document(
            DocumentMeta(name="untitled", created=_now_iso(), modified=_now_iso())
        )

    window = MainWindow(document, path)
    window.show()
    # After the window is up rather than while it is being built, and through the event
    # loop rather than in line: the first thing somebody should see is their board, and
    # the reply to an update check arrives on the loop this call is scheduled on.
    QTimer.singleShot(UPDATE_CHECK_DELAY_MS, window.consider_checking_for_updates)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
