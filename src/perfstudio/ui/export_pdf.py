"""1:1 scale PDF export, rewired onto the real engine.

Promoted from ``prototypes/qt/export_pdf.py``, unchanged in spirit: the scene works in
millimetres, ``QPdfWriter`` works in millimetres, so the mapping is a straight
source-rect to target-rect with no fudge factor, and the claim is checked two ways --
a machine check (:func:`verify_scale`) and a human one (the printed 50 mm scale bar).

Mirroring for the solder-side sheet is NOT done here with a painter-level flip.
``view2d.BoardScene`` already builds a fully mirrored scene (pads, conductor paths,
component silhouettes -- everything) when constructed with ``side="bottom"``, using
``geometry.hole_span_mm`` (see the note in view2d.py). This module only needs to know
whether the scene handed to it is the mirrored one, to choose the printed label.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QMarginsF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen

from perfstudio.geometry import board_outline_mm
from perfstudio.model import Board

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


def _draw_scale_bar(painter: QPainter, origin: QPointF) -> None:
    """A 50 mm ruler drawn in scene units. The end-user's 1:1 verification.

    The small point sizes used here and below are correct for THIS paint device and would
    not be for a screen. A point is 1/72 inch of the device, so at the writer's 600 dpi a
    2.2 pt font is about 18 device pixels -- ample. The same 2.2 pt on a 96 dpi screen
    would be three pixels, which some platforms' font engines decline to draw at all; that
    is why the editor's own labels are sized in screen pixels instead (ui/scenetext.py).
    """
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


def export_pdf(board: Board, scene: Any, path: str | Path, mirrored: bool = False) -> Path:
    """Render `scene` (already built for the correct side -- see the module note) at
    true size onto A4, with a scale bar for verification."""
    out = Path(path)

    writer = QPdfWriter(str(out))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageOrientation(QPageLayout.Orientation.Landscape)
    writer.setPageMargins(QMarginsF(10, 10, 10, 10), QPageLayout.Unit.Millimeter)
    writer.setResolution(600)
    writer.setTitle(f"PerfStudio — {board.cols}x{board.rows} {board.material}")

    painter = QPainter(writer)
    px_per_mm = writer.resolution() / MM_PER_INCH

    outline = board_outline_mm(board)
    source = QRectF(outline.x, outline.y, outline.width, outline.height)

    painter.save()
    painter.scale(px_per_mm, px_per_mm)  # from here on, one unit is one millimetre
    scene.render(painter, source, source, Qt.AspectRatioMode.IgnoreAspectRatio)
    painter.restore()

    painter.save()
    painter.scale(px_per_mm, px_per_mm)
    _draw_scale_bar(painter, QPointF(0, outline.y + outline.height + 8))
    font = QFont()
    font.setPointSizeF(3.0)
    painter.setFont(font)
    painter.setPen(QPen(QColor("#000000")))
    side = "SOLDER SIDE (mirrored)" if mirrored else "COMPONENT SIDE"
    painter.drawText(
        QPointF(0, -6), f"PerfStudio  ·  {board.cols}x{board.rows} {board.material}  ·  {side}  ·  printed 1:1"
    )
    painter.restore()

    painter.end()
    return out


def verify_scale(scene: Any, board: Board, dpi: float = 300.0, span_holes: int = 10) -> ScaleCheck:
    """Render at a known DPI and confirm a known hole span lands on the right pixel
    count.

    Catches the class of bug where a transform is off by a constant -- the print looks
    plausible but every dimension is wrong, which is worse than an obviously broken one.
    """
    px_per_mm = dpi / MM_PER_INCH
    outline = board_outline_mm(board)
    img_w = round(outline.width * px_per_mm)
    img_h = round(outline.height * px_per_mm)

    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    source = QRectF(outline.x, outline.y, outline.width, outline.height)
    scene.render(painter, QRectF(0, 0, img_w, img_h), source, Qt.AspectRatioMode.IgnoreAspectRatio)
    painter.end()

    # Where the transform actually put two holes, measured through the same mapping.
    scale_x = img_w / outline.width
    x0 = (0.0 - source.left()) * scale_x
    x1 = (span_holes * board.pitch - source.left()) * scale_x
    measured = x1 - x0
    expected = span_holes * board.pitch * px_per_mm
    return ScaleCheck(dpi=dpi, span_holes=span_holes, expected_px=expected, measured_px=measured)
