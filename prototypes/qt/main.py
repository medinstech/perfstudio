"""PerfStudio Qt/VTK prototype.

Throwaway. It exists to answer three questions before we commit to a stack:
  1. Does QGraphicsView feel right for a grid CAD editor, or are we fighting it?
  2. Does VTK carry the 3D requirement on ordinary hardware?
  3. Does Qt really give us a true 1:1 printable sheet?

It reads a .perf file produced by the existing TypeScript engine. It does NOT port the
engine, and it must not start to.

    python main.py            launch the app
    python main.py --headless render 2D/3D/PDF to files and print the measurements
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QAction, QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QMainWindow,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import board_model  # noqa: E402
from export_pdf import export_pdf, verify_scale  # noqa: E402
from view2d import BoardScene, BoardView  # noqa: E402

PERF = HERE / "sample.perf"
FOOTPRINTS = HERE / "footprints.json"


def load_document() -> board_model.Document:
    return board_model.load(PERF, FOOTPRINTS)


class MainWindow(QMainWindow):
    def __init__(self, doc: board_model.Document) -> None:
        super().__init__()
        self.doc = doc
        self.setWindowTitle("PerfStudio — Qt/VTK prototype")
        self.resize(1500, 950)

        self.scene = BoardScene(doc)
        self.view = BoardView(self.scene)
        self.scene.componentMoved.connect(self.on_component_moved)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.view)

        right = QWidget()
        layout = QVBoxLayout(right)
        layout.setContentsMargins(0, 0, 0, 0)
        self.vtk_widget = self._make_3d(right)
        if self.vtk_widget is not None:
            layout.addWidget(self.vtk_widget)
        else:
            layout.addWidget(QLabel("VTK Qt widget unavailable — see --headless output"))
        splitter.addWidget(right)
        splitter.setSizes([900, 600])
        self.setCentralWidget(splitter)

        bar = QToolBar("main")
        self.addToolBar(bar)
        act_pdf = QAction("Export 1:1 PDF", self)
        act_pdf.triggered.connect(self.on_export_pdf)
        bar.addAction(act_pdf)
        act_fit = QAction("Fit", self)
        act_fit.triggered.connect(lambda: self.view.fitInView(self.scene.sceneRect(),
                                                             Qt.AspectRatioMode.KeepAspectRatio))
        bar.addAction(act_fit)

        self.setStatusBar(QStatusBar())
        b = doc.board
        w, h = b.size_mm
        self.statusBar().showMessage(
            f"{b.cols}x{b.rows} {b.material}  ·  {w:.1f} x {h:.1f} mm  ·  "
            f"{len(doc.components)} parts  ·  {len(doc.conductors)} conductors  ·  drag a part to move it"
        )

    def _make_3d(self, parent: QWidget):  # noqa: ANN202
        try:
            from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
            import view3d

            widget = QVTKRenderWindowInteractor(parent)
            ren, _ = view3d.build_renderer(self.doc)
            widget.GetRenderWindow().AddRenderer(ren)
            widget.Initialize()
            widget.Start()
            return widget
        except Exception as exc:  # pragma: no cover - environment dependent
            print(f"[3D] Qt/VTK widget unavailable: {exc}", file=sys.stderr)
            return None

    def on_component_moved(self, ref: str, col: int, row: int) -> None:
        self.statusBar().showMessage(
            f"{ref} moved to {board_model.hole_ref(col, row)}  "
            f"(engine would now re-run DRC and re-route the nets it touches)"
        )

    def on_export_pdf(self) -> None:
        out = HERE / "board_1to1.pdf"
        export_pdf(self.doc, self.scene, out)
        self.statusBar().showMessage(f"wrote {out}")


def headless() -> int:
    app = QApplication(sys.argv)
    doc = load_document()
    board = doc.board
    scene = BoardScene(doc)

    print(f"board        {board.cols}x{board.rows} {board.material}")
    w, h = board.size_mm
    sw, sh = board.hole_span_mm
    print(f"substrate    {w:.2f} x {h:.2f} mm   (hole span {sw:.2f} x {sh:.2f} mm)")
    print(f"parts        {len(doc.components)}   conductors {len(doc.conductors)}")

    # --- 2D render ---
    px_per_mm = 12
    img_w, img_h = int(w * px_per_mm), int(h * px_per_mm)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32)
    image.fill(QColor("#15161a"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    source = QRectF(-board.pitch / 2, -board.pitch / 2, w, h)
    t0 = time.perf_counter()
    scene.render(painter, QRectF(0, 0, img_w, img_h), source)
    t_2d = (time.perf_counter() - t0) * 1000
    painter.end()
    image.save(str(HERE / "out_2d.png"))
    print(f"\n2D render    {t_2d:6.1f} ms   -> out_2d.png ({img_w}x{img_h})")
    print(f"pads painted {scene.pad_grid.drawn} of {board.cols * board.rows} (Qt culls the rest)")

    # --- 1:1 scale verification ---
    check = verify_scale(scene, board)
    print(
        f"\n1:1 check    {check.span_holes} holes at {check.dpi:.0f} dpi: "
        f"expected {check.expected_px:.3f} px, measured {check.measured_px:.3f} px, "
        f"error {check.error_mm * 1000:.2f} um  -> {'PASS' if check.ok else 'FAIL'}"
    )

    pdf = export_pdf(doc, scene, HERE / "board_1to1.pdf")
    pdf_m = export_pdf(doc, scene, HERE / "board_1to1_solder_side.pdf", mirrored=True)
    print(f"PDF          {pdf.stat().st_size / 1024:.0f} KB -> {pdf.name}")
    print(f"PDF mirrored {pdf_m.stat().st_size / 1024:.0f} KB -> {pdf_m.name}")

    # --- 3D render, offscreen: the build-guide image path ---
    try:
        import view3d

        t0 = time.perf_counter()
        stats = view3d.render_offscreen(doc, str(HERE / "out_3d.png"))
        t_3d = (time.perf_counter() - t0) * 1000
        print(f"\n3D offscreen {t_3d:6.1f} ms   -> out_3d.png")
        print(f"actors       {stats['actors']} total for {stats['pads']} pads (instanced)")
        view3d.render_offscreen(doc, str(HERE / "out_3d_solder.png"), flipped=True)
        print("             out_3d_solder.png (flipped to the solder side)")
    except Exception as exc:
        print(f"\n3D FAILED: {exc}")
        return 1

    del app
    return 0


def main() -> int:
    if "--headless" in sys.argv:
        return headless()
    app = QApplication(sys.argv)
    window = MainWindow(load_document())
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
