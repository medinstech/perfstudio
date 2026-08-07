"""PerfStudio desktop application: the real engine behind the prototype's window.

Promoted from ``prototypes/qt/main.py``. Everything the prototype only gestured at in
its status-bar comment -- "(engine would now re-run DRC and re-route the nets it
touches)" -- actually happens here: every mutation goes through a
``perfstudio.command.CommandBus`` (never a direct write to the document), and every
successful command re-runs ``run_drc``/``run_lvs`` and repaints from the bus's own
document.

    python -m perfstudio.ui.main                 launch the app (blank document)
    python -m perfstudio.ui.main path/to.perf     launch the app, opening a document
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
from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from perfstudio import persist
from perfstudio.command import CommandBus, CommandContext, DispatchResult, HistoryEntry, create_id_generator
from perfstudio.commands import create_empty_document, create_standard_registry
from perfstudio.drc import DrcViolation, run_drc
from perfstudio.footprints import footprint_lookup
from perfstudio.geometry import board_size_mm, hole_span_mm
from perfstudio.lvs import LvsIssue, LvsResult, run_lvs
from perfstudio.model import BoardSide, DocumentMeta, PerfDocument

from . import view3d
from .export_pdf import export_pdf, verify_scale
from .view2d import BoardScene, BoardView

ROLE_HOLES = int(Qt.ItemDataRole.UserRole) + 1
ROLE_COMPONENT_IDS = int(Qt.ItemDataRole.UserRole) + 2


def _now_iso() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


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
        self.bus = CommandBus(document, create_standard_registry(), CommandContext(next_id=create_id_generator()))
        self._last_drc_ms = 0.0
        self._last_violations: tuple[DrcViolation, ...] = ()
        self._last_lvs: LvsResult | None = None
        self._vtk_renderer: Any = None

        self.setWindowTitle("PerfStudio")
        self.resize(1500, 950)

        self.scene = BoardScene(self.bus.document, self.lookup, side=self.side, bus=self.bus)
        self.view = BoardView(self.scene)
        self.scene.moveCommitted.connect(self.on_move_committed)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.view)

        right = QWidget()
        layout = QVBoxLayout(right)
        layout.setContentsMargins(0, 0, 0, 0)
        # Typed Any, not QWidget | None: the real runtime type is a
        # QVTKRenderWindowInteractor (VTK ships no stubs), which has methods (like
        # GetRenderWindow) QWidget itself does not declare.
        self.vtk_widget: Any = self._make_3d(right)
        if self.vtk_widget is not None:
            layout.addWidget(self.vtk_widget)
        else:
            layout.addWidget(QLabel("VTK Qt widget unavailable — see --headless output"))
        splitter.addWidget(right)
        splitter.setSizes([900, 600])
        self.setCentralWidget(splitter)

        self._build_menu()
        self._build_drc_dock()
        self.setStatusBar(QStatusBar())

        self.bus.subscribe(self.on_bus_changed)
        self.on_bus_changed(self.bus.document, None)

    # -- 3D widget -----------------------------------------------------------

    def _make_3d(self, parent: QWidget) -> Any:
        try:
            from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor

            widget = QVTKRenderWindowInteractor(parent)  # type: ignore[no-untyped-call]
            ren, _stats = view3d.build_renderer(self.bus.document, self.lookup, flipped=(self.side == "bottom"))
            widget.GetRenderWindow().AddRenderer(ren)  # type: ignore[no-untyped-call]
            widget.Initialize()
            widget.Start()
            self._vtk_renderer = ren
            return widget
        except Exception as exc:  # pragma: no cover - environment dependent
            print(f"[3D] Qt/VTK widget unavailable: {exc}", file=sys.stderr)
            self._vtk_renderer = None
            return None

    def _refresh_3d(self) -> None:
        if self.vtk_widget is None:
            return
        ren, _stats = view3d.build_renderer(self.bus.document, self.lookup, flipped=(self.side == "bottom"))
        rw = self.vtk_widget.GetRenderWindow()
        if self._vtk_renderer is not None:
            rw.RemoveRenderer(self._vtk_renderer)
        rw.AddRenderer(ren)
        self._vtk_renderer = ren
        rw.Render()

    # -- menu ------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
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

        view_menu = menu.addMenu("&View")
        act_flip = view_menu.addAction("Flip Board (component / solder side)")
        act_flip.triggered.connect(self.on_flip_board)
        act_fit: QAction = view_menu.addAction("Fit")
        act_fit.triggered.connect(
            lambda: self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        )

    def _build_drc_dock(self) -> None:
        self.drc_tree = QTreeWidget()
        self.drc_tree.setHeaderLabels(["Rule / Kind", "Message"])
        self.drc_tree.itemClicked.connect(self._on_drc_item_clicked)
        dock = QDockWidget("DRC / LVS", self)
        dock.setWidget(self.drc_tree)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

    # -- the one repaint path: every successful command, undo and redo funnels here --

    def on_bus_changed(self, document: PerfDocument, entry: HistoryEntry | None) -> None:
        self.scene.set_document(document)

        t0 = time.perf_counter()
        violations = run_drc(document, self.lookup)
        self._last_drc_ms = (time.perf_counter() - t0) * 1000
        self._last_violations = tuple(violations)
        self.scene.set_violations(violations)

        self._last_lvs = run_lvs(document, self.lookup)
        self._refresh_drc_panel(self._last_violations, self._last_lvs)
        self._refresh_3d()

        errors = sum(1 for v in violations if v.severity == "error")
        warns = sum(1 for v in violations if v.severity == "warning")
        desc = entry.description if entry is not None else "Updated"
        s = self._last_lvs.summary
        self.statusBar().showMessage(
            f"{desc}  ·  DRC {errors} error(s), {warns} warning(s) ({self._last_drc_ms:.1f} ms)  ·  "
            f"LVS {s.matched_nets}/{s.schematic_nets} nets matched, {s.opens} open, {s.shorts} short"
        )

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

    # -- edit --------------------------------------------------------------

    def on_undo(self) -> None:
        self.bus.undo()

    def on_redo(self) -> None:
        self.bus.redo()

    def on_flip_board(self) -> None:
        self.side = "bottom" if self.side == "top" else "top"
        self.scene.set_side(self.side)
        self._refresh_3d()
        self.statusBar().showMessage(f"Viewing {self.side} side")

    # -- file ----------------------------------------------------------------

    def on_open(self) -> None:
        start_dir = str(self.current_path.parent) if self.current_path else str(Path.cwd())
        path_str, _ = QFileDialog.getOpenFileName(self, "Open .perf", start_dir, "PerfStudio documents (*.perf)")
        if not path_str:
            return
        self._load_path(Path(path_str))

    def _load_path(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        result = persist.deserialize_document(text)
        if not result.ok:
            location = f" (at {result.path})" if result.path else ""
            QMessageBox.critical(self, "Open failed", f"[{result.code}] {result.message}{location}")
            return
        self.current_path = path
        self.bus = CommandBus(result.document, create_standard_registry(), CommandContext(next_id=create_id_generator()))
        self.bus.subscribe(self.on_bus_changed)
        self.scene.bus = self.bus
        self.on_bus_changed(self.bus.document, None)
        note = f" ({len(result.warnings)} warning(s))" if result.warnings else ""
        self.statusBar().showMessage(f"Loaded {path.name}{note}")

    def on_save(self) -> None:
        if self.current_path is None:
            self.on_save_as()
            return
        self._save_to(self.current_path)

    def on_save_as(self) -> None:
        default = str(self.current_path) if self.current_path else str(Path.cwd() / "board.perf")
        path_str, _ = QFileDialog.getSaveFileName(self, "Save As", default, "PerfStudio documents (*.perf)")
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix != ".perf":
            path = path.with_suffix(".perf")
        self.current_path = path
        self._save_to(path)

    def _save_to(self, path: Path) -> None:
        # meta.modified is host-stamped, not part of any command (core has no clock --
        # see persist.py/commands.py) -- so this replaces the document's meta for the
        # SERIALIZED copy only, without pushing that change through the bus.
        doc = self.bus.document
        stamped = dataclasses.replace(doc, meta=dataclasses.replace(doc.meta, modified=_now_iso()))
        path.write_text(persist.serialize_document(stamped), encoding="utf-8")
        self.statusBar().showMessage(f"Saved {path}")

    def on_export_pdf(self) -> None:
        base = self.current_path.with_suffix("") if self.current_path else Path.cwd() / "board"
        doc = self.bus.document
        top_scene = BoardScene(doc, self.lookup, side="top")
        bottom_scene = BoardScene(doc, self.lookup, side="bottom")
        p1 = export_pdf(doc.board, top_scene, base.with_name(base.name + "_component_side.pdf"))
        p2 = export_pdf(doc.board, bottom_scene, base.with_name(base.name + "_solder_side.pdf"), mirrored=True)
        self.statusBar().showMessage(f"Exported {p1.name} and {p2.name}")

    def on_export_3d_png(self) -> None:
        out = self.current_path.with_suffix(".png") if self.current_path else Path.cwd() / "board_3d.png"
        view3d.render_offscreen(self.bus.document, self.lookup, str(out), flipped=(self.side == "bottom"))
        self.statusBar().showMessage(f"Exported {out}")


# ---------------------------------------------------------------------------
# Headless entry point
# ---------------------------------------------------------------------------


def headless(argv: list[str]) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    lookup = footprint_lookup()

    positional = [a for a in argv if not a.startswith("--")]
    perf_path = Path(positional[0]) if positional else _find_repo_root() / "tools" / "diffcheck" / "golden" / "dense.perf"

    out_dir = Path.cwd() / "headless_out"
    out_dir.mkdir(exist_ok=True)

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
    px_per_mm = 12
    img_w, img_h = int(w_mm * px_per_mm), int(h_mm * px_per_mm)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32)
    image.fill(QColor("#15161a"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    source = QRectF(-board.pitch / 2, -board.pitch / 2, w_mm, h_mm)
    t0 = time.perf_counter()
    scene.render(painter, QRectF(0, 0, img_w, img_h), source)
    t_2d = (time.perf_counter() - t0) * 1000
    painter.end()
    out_2d = out_dir / "out_2d.png"
    image.save(str(out_2d))
    print(f"\n2D render    {t_2d:6.1f} ms   -> {out_2d}")
    if scene.pad_grid is not None:
        print(f"pads painted {scene.pad_grid.drawn} of {board.cols * board.rows} (Qt culls the rest)")

    # --- 1:1 scale verification ---
    check = verify_scale(scene, board)
    print(
        f"\n1:1 check    {check.span_holes} holes at {check.dpi:.0f} dpi: "
        f"expected {check.expected_px:.3f} px, measured {check.measured_px:.3f} px, "
        f"error {check.error_mm * 1000:.2f} um  -> {'PASS' if check.ok else 'FAIL'}"
    )

    bottom_scene = BoardScene(doc, lookup, side="bottom")
    pdf_component = export_pdf(board, scene, out_dir / "board_1to1_component_side.pdf")
    pdf_solder = export_pdf(board, bottom_scene, out_dir / "board_1to1_solder_side.pdf", mirrored=True)
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
    if "--headless" in sys.argv:
        return headless([a for a in sys.argv[1:] if a != "--headless"])

    app = QApplication(sys.argv)
    argv_paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    path: Path | None
    if argv_paths:
        path = Path(argv_paths[0])
        result = persist.deserialize_document(path.read_text(encoding="utf-8"))
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
