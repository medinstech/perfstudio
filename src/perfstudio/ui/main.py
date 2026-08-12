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
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QEventLoop, QRectF, QSettings, QSize, Qt, QThread, QTimer
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter
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
    QMainWindow,
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
    describe_reroute,
    plan_autoroute,
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
    ApplyBoardPresetPayload,
    DeleteComponentPayload,
    DeleteConductorsPayload,
    DeleteEdgeConnectorPayload,
    DeleteMountingHolePayload,
    DeleteNetPayload,
    DisconnectPinsPayload,
    ImportNetlistPayload,
    MirrorComponentPayload,
    PlaceComponentPayload,
    RotateComponentPayload,
    SetBoardPayload,
    SetHeightLimitPayload,
    UpdateComponentPayload,
    UpdateNetPayload,
    create_document_id_generator,
    create_empty_document,
    create_standard_registry,
    create_starter_document,
)
from perfstudio.drc import DrcViolation, run_drc
from perfstudio.footprints import footprint_lookup, standard_footprints
from perfstudio.geometry import (
    STANDARD_PRESETS,
    BoardPreset,
    board_edge_margin_mm,
    board_from_preset,
    board_outline_mm,
    edge_connector_holes,
    format_hole,
    hole_span_mm,
    pad_edge_gap_mm,
    pad_extent_mm,
    preset_edge_connectors,
    preset_mounting_holes,
)
from perfstudio.guide import Guide, GuideStep, all_steps, build_guide, document_at_step, step_focus
from perfstudio.guide import describe as describe_guide
from perfstudio.guide_export import bom_to_csv, cut_list_to_csv, guide_to_html, guide_to_json
from perfstudio.lvs import LvsIssue, LvsResult, run_lvs, stale_conductor_ids
from perfstudio.model import (
    Board,
    BoardEdge,
    BoardLabels,
    BoardMaterial,
    BoardSide,
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
from perfstudio.version import __version__
from perfstudio.version import describe as describe_version

from . import icons, view3d
from .boardcolors import SCHEMES as BOARD_SCHEMES
from .boardcolors import choose as choose_board_colour
from .export_pdf import export_pdf, verify_scale
from .i18n import set_language, t
from .theme import ERROR, OK, STYLESHEET, TEXT_DIM, WARNING
from .view2d import RULER_MARGIN_MM, BoardScene, BoardView, next_reference


#: Where the recent-file list is kept between runs. A function rather than a constant so a
#: test can point it at a temporary file instead of the real user store -- a test suite has
#: no business writing into somebody's registry, and one that did would also make the
#: recent-files tests depend on whatever ran before them.
def recent_files_settings() -> QSettings:
    return QSettings("PerfStudio", "PerfStudio")


RECENT_FILES_KEY = "recentFiles"

ROLE_HOLES = int(Qt.ItemDataRole.UserRole) + 1
ROLE_COMPONENT_IDS = int(Qt.ItemDataRole.UserRole) + 2
ROLE_NET_ID = int(Qt.ItemDataRole.UserRole) + 3
ROLE_FOOTPRINT_ID = int(Qt.ItemDataRole.UserRole) + 4
#: (component ref, pin number) on a pin row under a net. The row also carries
#: ROLE_NET_ID, so selecting a pin highlights its net exactly as selecting the net does.
ROLE_PIN = int(Qt.ItemDataRole.UserRole) + 5


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
        self.pad_shape.currentIndexChanged.connect(self._update_enabled)
        self.legend.toggled.connect(self._update_enabled)
        self._pitch = board.pitch
        self._pad_diameter = board.pad_diameter
        self._update_note()

        form = QFormLayout()
        form.addRow(t("Board"), self.preset)
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
        return dataclasses.replace(
            self._board,
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
            "Wakes DRC's current-capacity rule and picks the wire gauge on the build "
            "guide's cut list. Nothing else in the application can set it."
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
            "Wakes DRC's creepage rule above the mains threshold. A -12 V rail is an "
            "ordinary value here, which is why it needs its own tick rather than a zero."
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
        #: Which primitive the router should reach for first. See router.RoutingStyle.
        self._routing_style: RoutingStyle = "balanced"

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
        self.scene.netPinsArmed.connect(self._on_net_pins_armed)
        self.scene.netPinsChanged.connect(self._on_net_pins_changed)
        self.scene.netPinRejected.connect(self._on_net_pin_rejected)
        self.scene.netPinsCommitted.connect(self._on_net_pins_committed)
        self.scene.connectArmed.connect(self._on_connect_armed)
        self.scene.connectProgress.connect(self._on_connect_progress)
        self.scene.pinsConnected.connect(self._on_pins_connected)
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
        self.dock_3d = QDockWidget(t("3D View"), self)
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

    def _assembly_steps(self) -> tuple[GuideStep, ...]:
        """The build order for the document as it stands, rebuilt when it changes.

        Cached on the document OBJECT, which is free to compare because documents are
        immutable and every edit produces a new one. Building it costs a couple of
        milliseconds -- it runs DRC and LVS -- which is worth paying only once per edit.
        """
        if self._assembly_doc is not self.bus.document:
            self._assembly_doc = self.bus.document
            self._assembly_guide = build_guide(self.bus.document, self.lookup)
            self._assembly_cached = all_steps(self._assembly_guide)
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
        file_menu.addSeparator()
        act_board = file_menu.addAction(t("&Board Setup…"))
        act_board.setToolTip(
            "Grid size and substrate. The material is not cosmetic: it decides the iron "
            "temperature the build guide gives and whether the pad-lifting rule applies."
        )
        act_board.triggered.connect(self.on_board_setup)
        act_features = file_menu.addAction(t("Board &Features…"))
        act_features.setToolTip(
            "Mounting holes and edge-connector fingers. A mounting bore takes the copper "
            "off the pads around it, so DRC treats a pin left there as an error."
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
            "Write the step-by-step soldering guide: one offline HTML file, the wire cut "
            "list and BOM as CSV, and the whole thing as JSON."
        )
        act_guide.triggered.connect(self.on_export_guide)
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
        act_stop_draw = draw_menu.addAction(t("&Stop Drawing"))
        act_stop_draw.setShortcut(QKeySequence("Escape"))
        act_stop_draw.triggered.connect(lambda: self.on_draw_mode("", False))

        place_menu = menu.addMenu(t("&Place"))
        self.act_autoplace = place_menu.addAction(t("&Auto-place Board"))
        self.act_autoplace.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self.act_autoplace.setToolTip(
            "Rearrange the unlocked parts to shorten the connections and make them "
            "solderable as traces rather than wires. Shows the result before applying it."
        )
        self.act_autoplace.triggered.connect(lambda: self.on_autoplace())
        act_reroll = place_menu.addAction(t("&Try Another Arrangement"))
        act_reroll.setToolTip(
            "Search again from a different seed. Annealing is a random walk, so this is "
            "a real second answer rather than the same one twice."
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
            "Click one pin, then another. They end up on the same net: an existing one if "
            "either pin is already on it, or a new one named for you if neither is."
        )
        self.act_connect.triggered.connect(self.on_connect_tool)
        net_menu.addSeparator()
        act_new_net = net_menu.addAction(t("&New Net…"))
        self.act_new_net = act_new_net
        act_new_net.setShortcut(QKeySequence("Ctrl+Shift+N"))
        act_new_net.setToolTip(
            "Name a net, then click its pins on the board. Nothing here needs KiCad."
        )
        act_new_net.triggered.connect(self.on_new_net)
        self.act_add_pins = net_menu.addAction(t("&Add Pins to Net"))
        self.act_add_pins.setShortcut(QKeySequence("P"))
        self.act_add_pins.setToolTip(
            "Click each pin that belongs to the selected net. Right-click or Enter "
            "finishes, and the whole session goes on the history as one step."
        )
        self.act_add_pins.triggered.connect(self.on_add_pins_to_net)
        self.act_finish_pins = net_menu.addAction(t("&Finish Adding Pins"))
        self.act_finish_pins.triggered.connect(self.on_finish_adding_pins)
        self.act_finish_pins.setEnabled(False)
        net_menu.addSeparator()
        self.act_edit_net = net_menu.addAction(t("&Edit Net…"))
        self.act_edit_net.setToolTip(
            "Name, class, and the current and voltage it carries — which nothing else in "
            "the application can set, and which DRC's capacity and creepage rules need."
        )
        self.act_edit_net.triggered.connect(self.on_edit_net)
        self.act_disconnect_pins = net_menu.addAction(t("&Disconnect Selected Pins"))
        self.act_disconnect_pins.setToolTip(
            "Take the pins selected in the Nets panel off their net. Expand a net to "
            "see them."
        )
        self.act_disconnect_pins.triggered.connect(self.on_disconnect_pins)
        self.act_delete_net = net_menu.addAction(t("De&lete Net"))
        self.act_delete_net.setToolTip(
            "Forget what the net was for. Copper already laid for it stays on the board, "
            "and stops being anything re-route or the stale sweep will touch."
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
            ("solder", t("&Solder trace where possible"),
             "Solder wherever solder reaches, with a short jumper only where it must cross. "
             "On the NE555 fixture this routes all 14 connections without a single wire."),
            ("balanced", t("&Balanced"),
             "Weigh each primitive on its own cost. The default, and what every golden "
             "fixture is routed with."),
            ("wire", t("&Wire where possible"),
             "For anyone who would rather cut and dress wire than drag solder along a row."),
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
            "Rip up the existing routing and plan it again from nothing. Use this after "
            "moving parts: autoroute only adds, so it leaves the copper laid out for "
            "where things used to be. Hand-drawn copper with no net is never touched."
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
        view_menu.addSeparator()

        self.act_3d = self.dock_3d.toggleViewAction()
        self.act_3d.setText(t("Show &3D View"))
        self.act_3d.setShortcut(QKeySequence("Ctrl+3"))
        self.act_3d.setToolTip("Open the 3D board view (Ctrl+3). Closed by default: it is the "
                              "most expensive thing in the window to keep up to date.")
        view_menu.addAction(self.act_3d)
        self.act_exploded = view_menu.addAction(t("&Exploded View"))
        self.act_exploded.setCheckable(True)
        self.act_exploded.setToolTip(
            t("Lift every part off the board, with a line down to the holes it goes in.")
        )
        self.act_exploded.toggled.connect(self.on_toggle_exploded)
        act_reset_3d = view_menu.addAction(t("Reset 3D &Camera"))
        act_reset_3d.triggered.connect(self.on_reset_3d_camera)

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
            "Green for FR-4 and brown for phenolic, which is what those substrates "
            "actually look like."
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
            route_menu, style_menu, view_menu, colour_menu, help_menu,
        ]
        act_keys = help_menu.addAction(t("&Keyboard Shortcuts…"))
        act_keys.setShortcut(QKeySequence("F1"))
        act_keys.setToolTip(
            "Every binding, read off this menu bar — plus the board gestures, which are "
            "on no menu and were previously only in the source."
        )
        act_keys.triggered.connect(self.on_shortcuts)
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

        dock = QDockWidget(t("Parts"), self)
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

    def _on_library_selection_changed(self) -> None:
        items = self.library_tree.selectedItems()
        footprint_id = items[0].data(0, ROLE_FOOTPRINT_ID) if items else None
        self.scene.arm_placement(footprint_id)

    def _on_placement_armed(self, footprint_id: str) -> None:
        if not footprint_id:
            self.label_place_hint.setText("Pick a part, then click the board. Esc cancels.")
            self.view.viewport().unsetCursor()
            self.library_tree.clearSelection()
            self._refresh_mode_banner()
            return
        footprint = self.lookup(footprint_id)
        name = footprint.name if footprint is not None else footprint_id
        ref = next_reference(self.bus.document, footprint_id)
        self.label_place_hint.setText(f"Click a hole to place <b>{ref}</b> ({name}). Esc cancels.")
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
            return f"{t('Placing')} {ref} ({name})  ·  {t('click a hole, Esc cancels')}"

        kind = self.scene.armed_draw_kind
        if kind:
            two_point = kind in ("bare-wire", "insulated-wire", "top-jumper")
            how = (
                t("click both ends, Esc cancels")
                if two_point
                else t("click each pad, Enter or right-click finishes, Esc cancels")
            )
            return f"{t('Drawing')} {kind.replace('-', ' ')}  ·  {how}"

        return ""

    def _refresh_empty_hint(self) -> None:
        """Tell a blank board what to do with itself.

        The application opens on an empty 5 x 7 board, and every route, check and export
        needs something on it first. An empty viewport with a full menu bar above it is
        the one screen where a person cannot tell whether they are looking at a tool that
        is ready or one that is broken.
        """
        document = self.bus.document
        if document.components or document.conductors or self._mode_text():
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
        self.nets_tree.setHeaderLabels(["Net", "Class", "Pins", "Left"])
        self.nets_tree.setAlternatingRowColors(True)
        self.nets_tree.setRootIsDecorated(True)
        # The net name absorbs the spare width and the three narrow columns keep their
        # content. Fixed widths pushed "Left" off the edge of the dock -- which is the one
        # column the panel exists for, so it must never be the one that gets clipped.
        header = self.nets_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.nets_tree.itemSelectionChanged.connect(self._on_net_selection_changed)
        self.nets_tree.itemDoubleClicked.connect(self._on_net_double_clicked)
        layout.addWidget(self.nets_tree)

        dock = QDockWidget(t("Nets"), self)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.dock_nets = dock
        # 340 rather than 300: at 300 the parts library elides half its names, and the two
        # left-hand docks share a column.
        self.resizeDocks([dock], [340], Qt.Orientation.Horizontal)

    def _build_drc_dock(self) -> None:
        self.drc_tree = QTreeWidget()
        self.drc_tree.setHeaderLabels(["Rule / Kind", "Message"])
        self.drc_tree.setColumnWidth(0, 260)
        self.drc_tree.itemClicked.connect(self._on_drc_item_clicked)
        dock = QDockWidget(t("DRC / LVS"), self)
        dock.setWidget(self.drc_tree)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        # A clean board is four rows, and this panel was opening a quarter of the window
        # tall to show them -- taken off the board, which is the thing being worked on.
        self.dock_drc = dock
        self.resizeDocks([dock], [190], Qt.Orientation.Vertical)

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
        """
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
                "This board prints its own addresses, so the editor's ruler would repeat them."
                if readable
                else "Column letters and row numbers along the edges of the view."
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
                except BaseException as exc:  # noqa: BLE001 - re-raised on the UI thread
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

                    def on_cancel() -> None:
                        nonlocal cancelled
                        cancelled = True
                        if progress is not None:
                            progress.setLabelText(
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
            self.statusBar().showMessage("Nothing to place: the board is empty.", 6000)
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
            self.statusBar().showMessage(
                "Select a net in the Nets panel, or a part on the board, then route.", 6000
            )
            return
        self._route(only_net_ids=net_ids)

    def on_routing_style(self, style: str) -> None:
        """Choose which primitive the router reaches for first.

        Only recorded here; it takes effect on the next route. Re-routing the whole board
        the moment a menu item is ticked would discard work the user has not asked to
        lose -- and Route > Re-route Everything is right there for when they do.
        """
        self._routing_style = cast(RoutingStyle, style)
        for name, action in self.act_style.items():
            action.setChecked(name == style)
        self.statusBar().showMessage(
            f"{t('Preferred connection')}: {self.act_style[style].text().replace('&', '')}"
            f" — {t('applies to the next route')}",
            8000,
        )

    def _autoroute_options(self) -> AutorouteOptions:
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

        document = self.bus.document
        t0 = time.perf_counter()
        plan = self._run_planner(
            "Routing…",
            lambda _should_stop: plan_autoroute(
                document, self.lookup, self._autoroute_options(), only_net_ids=only_net_ids
            ),
        )
        elapsed = (time.perf_counter() - t0) * 1000
        if plan is None:
            return
        cleared_note = f"  ·  {cleared} stale conductor(s) removed first" if cleared else ""

        if plan.is_empty:
            self.statusBar().showMessage(
                f"Nothing to route: {describe_plan(plan)}{cleared_note} ({elapsed:.0f} ms)", 8000
            )
            return

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

    def on_draw_mode(self, kind: str, checked: bool) -> None:
        """Arm or disarm a drawing tool. Only one may be armed at a time.

        Escape is bound to this with an empty kind, and Escape has to mean "leave the mode
        I am in" whichever one that is -- a window shortcut fires before the scene sees the
        key at all, so ending a pin-picking session belongs here rather than only in the
        scene's own key handler.
        """
        wanted = kind if checked and kind else ""
        for name, action in self.act_draw.items():
            action.setChecked(name == wanted)
        if not wanted:
            self.scene.arm_net_pins(None)
            self.scene.arm_connect(False)
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
        stored = recent_files_settings().value(RECENT_FILES_KEY, [])
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
        recent_files_settings().setValue(
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
        recent_files_settings().setValue(RECENT_FILES_KEY, [])
        self._refresh_recent_menu()

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
        self.bus = self._new_bus(result.document)
        self._subscribe_bus()
        self.scene.bus = self.bus
        self.on_bus_changed(self.bus.document, None)
        self.view.fit_board()
        note = f" ({len(result.warnings)} warning(s))" if result.warnings else ""
        self.statusBar().showMessage(f"Loaded {path.name}{note}", 8000)
        self._mark_saved()
        self._remember_path(path)

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
            self.statusBar().showMessage(
                "Click a pin, then the pin it joins. Neither on a net yet? One gets made. "
                "Esc cancels.",
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
            self.statusBar().showMessage(
                "Select the pins to disconnect in the Nets panel — expand a net to see "
                "them.",
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
            self.statusBar().showMessage("Select one net in the Nets panel first.", 6000)
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
        path = Path(path_str)
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
        self._remember_path(path)
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
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            images = view3d.render_step_images(self.bus.document, guide, self.lookup)
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
    outline = board_outline_mm(board)
    w_mm, h_mm = outline.width, outline.height
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
    source = QRectF(outline.x - margin, outline.y - margin, src_w, src_h)
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
    t0 = time.perf_counter()
    images = view3d.render_step_images(doc, guide, lookup)
    t_shots = (time.perf_counter() - t0) * 1000
    html = guide_to_html(guide, images)
    (out_dir / "guide.html").write_text(html, encoding="utf-8")
    (out_dir / "guide.json").write_text(guide_to_json(guide), encoding="utf-8")
    (out_dir / "cut_list.csv").write_text(cut_list_to_csv(guide), encoding="utf-8")
    (out_dir / "bom.csv").write_text(bom_to_csv(guide), encoding="utf-8")
    print(f"\nbuild guide  {describe_guide(guide)}")
    print(f"             {guide.part_steps} part step(s), {guide.conductor_steps} connection(s), "
          f"{len(guide.cut_list)} wire(s) -> guide.html")
    # The weight is printed because the images are inlined: the guide has to stay a file
    # somebody opens on a phone, and this is the number that would quietly stop being
    # true.
    print(f"step images  {t_shots:6.1f} ms   {len(images)} render(s), "
          f"guide.html is {len(html.encode('utf-8')) // 1024} KB")
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


def _language_argument(argv: list[str]) -> str | None:
    """``--lang tr`` or ``--lang=tr``, or None to work it out from the environment."""
    for index, arg in enumerate(argv):
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1]
        if arg == "--lang" and index + 1 < len(argv):
            return argv[index + 1]
    return None


def main() -> int:
    # Answered before Qt is touched: --version has to work on a machine where the GUI
    # cannot start, since "it will not launch" is exactly when someone is asked which
    # version they have.
    if "--version" in sys.argv or "-V" in sys.argv:
        print(describe_version())
        return 0

    # Chosen before the window is built, because every menu label is translated once at
    # construction. --lang wins over PERFSTUDIO_LANG, which wins over the system locale.
    set_language(_language_argument(sys.argv))

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
        document = create_starter_document(
            DocumentMeta(name="untitled", created=_now_iso(), modified=_now_iso())
        )

    window = MainWindow(document, path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
