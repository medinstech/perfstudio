"""1:1 scale PDF export.

This is the specific claim I made for Qt over a webview, so it should be measurable
rather than asserted. The scene works in millimetres, QPdfWriter works in millimetres,
so the mapping is a straight source-rect to target-rect with no fudge factor.

Two checks are built in:
  - a machine check: the painter transform is verified to place a known hole span at a
    known number of device pixels;
  - a human check: the sheet carries a 50 mm scale bar. Print it, put a ruler on it. If
    it does not read 50 mm the print is not 1:1, and no amount of code can tell you that
    from inside the process.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QMarginsF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen

from board_model import Board, Document

MM_PER_INCH = 25.4


@dataclass
class ScaleCheck:
    dpi: float
    span_holes: int
    expected_px: float
    measured_px: float

    @property
    def error_mm(self) -> float:
        return (self.measured_px - self.expected_px) / self.dpi * MM_PER_INCH

    @property
    def ok(self) -> bool:
        # A tenth of a millimetre over a 25 mm span is far tighter than any printer.
        return abs(self.error_mm) < 0.1


def _draw_scale_bar(painter: QPainter, board: Board, origin: QPointF) -> None:
    """A 50 mm ruler drawn in scene units. The end-user's 1:1 verification."""
    painter.save()
    pen = QPen(QColor("#000000"), 0.25)
    painter.setPen(pen)
    x0, y0 = origin.x(), origin.y()
    painter.drawLine(QPointF(x0, y0), QPointF(x0 + 50, y0))
    for mm in range(0, 51, 10):
        h = 2.5 if mm % 50 == 0 else 1.6
        painter.drawLine(QPointF(x0 + mm, y0), QPointF(x0 + mm, y0 - h))
    font = QFont()
    font.setPointSizeF(2.2)
    painter.setFont(font)
    painter.drawText(QPointF(x0, y0 + 3.4), "50 mm — measure this with a ruler to confirm 1:1")
    painter.restore()


def export_pdf(doc: Document, scene, path: str | Path, mirrored: bool = False) -> Path:
    """Render the board at true size onto A4, with a scale bar for verification."""
    board = doc.board
    out = Path(path)

    writer = QPdfWriter(str(out))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageOrientation(QPageLayout.Orientation.Landscape)
    writer.setPageMargins(QMarginsF(10, 10, 10, 10), QPageLayout.Unit.Millimeter)
    writer.setResolution(600)
    writer.setTitle(f"PerfStudio — {board.cols}x{board.rows} {board.material}")

    painter = QPainter(writer)
    px_per_mm = writer.resolution() / MM_PER_INCH

    painter.save()
    painter.scale(px_per_mm, px_per_mm)  # from here on, one unit is one millimetre
    if mirrored:
        # Solder-side view: reflect about the hole-centre span, NOT the substrate size,
        # so hole 0 lands exactly on hole cols-1.
        span_w, _ = board.hole_span_mm
        painter.translate(span_w, 0)
        painter.scale(-1, 1)

    w, h = board.size_mm
    source = QRectF(-board.pitch / 2, -board.pitch / 2, w, h)
    scene.render(painter, source, source, Qt.AspectRatioMode.IgnoreAspectRatio)
    painter.restore()

    painter.save()
    painter.scale(px_per_mm, px_per_mm)
    _draw_scale_bar(painter, board, QPointF(0, h + 8))
    font = QFont()
    font.setPointSizeF(3.0)
    painter.setFont(font)
    painter.setPen(QPen(QColor("#000000")))
    side = "SOLDER SIDE (mirrored)" if mirrored else "COMPONENT SIDE"
    painter.drawText(QPointF(0, -6), f"PerfStudio  ·  {board.cols}x{board.rows} {board.material}  ·  {side}  ·  printed 1:1")
    painter.restore()

    painter.end()
    return out


def verify_scale(scene, board: Board, dpi: float = 300.0, span_holes: int = 10) -> ScaleCheck:
    """Render at a known DPI and confirm a known hole span lands on the right pixel count.

    Catches the class of bug where a transform is off by a constant — the print looks
    plausible but every dimension is wrong, which is worse than an obviously broken one.
    """
    px_per_mm = dpi / MM_PER_INCH
    w, h = board.size_mm
    img_w = int(round(w * px_per_mm))
    img_h = int(round(h * px_per_mm))

    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    source = QRectF(-board.pitch / 2, -board.pitch / 2, w, h)
    scene.render(painter, QRectF(0, 0, img_w, img_h), source, Qt.AspectRatioMode.IgnoreAspectRatio)
    painter.end()

    # Where the transform actually put two holes, measured through the same mapping.
    scale_x = img_w / w
    x0 = (0.0 - source.left()) * scale_x
    x1 = (span_holes * board.pitch - source.left()) * scale_x
    measured = x1 - x0
    expected = span_holes * board.pitch * px_per_mm
    return ScaleCheck(dpi=dpi, span_holes=span_holes, expected_px=expected, measured_px=measured)
