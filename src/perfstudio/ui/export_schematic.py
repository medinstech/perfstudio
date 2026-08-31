"""PDF and PNG of the schematic sheet, both made out of the SVG.

THIS FILE RENDERS NOTHING. ``schematic_export.drawing_to_svg`` draws the sheet; everything
here hands that string to Qt's SVG renderer and asks it to paginate or rasterise. A
second painter over ``SchematicDrawing`` here would be a second chance for the printed
sheet and the emailed PNG to disagree about what the circuit is, and a sheet that says two
different things is worse than no export at all.

WHAT THAT BUYS AND WHAT IT COSTS. It buys one drawing, testable without Qt, with a golden
file (``tests/schematic_golden/*.svg``) that pins what all three formats say. It costs
Qt's SVG support being SVG Tiny 1.2 -- no ``dominant-baseline``, no CSS -- which is why
``schematic_export`` computes text baselines itself instead of asking for them. That
constraint lives in the writer, next to the code it constrains.

NOT 1:1, AND THE CONTRAST WITH ``export_pdf.py`` IS THE POINT. That module exists to print
a board template you tape to a physical board, so its whole job is that 50 mm on the page
is 50 mm on a ruler, checked by a machine and by a printed scale bar. A schematic is a
drawing of a circuit and has no true size; asking a reader to measure it would be asking
about a number that means nothing. So this fits the sheet to the page and keeps quiet
about millimetres.

THE OFFSCREEN FONT TRAP. Qt's offscreen platform plugin ships no font database on Windows,
so a sheet exported from a headless run there comes out with every wire, symbol and glyph
correct and not one word of text -- silently, with no error. It is the same hazard
``tests/test_ui.py`` guards with ``skipif``. Anything asserting on exported TEXT has to
skip where ``QFontDatabase.families()`` is empty; anything asserting on the drawing does
not.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QMarginsF, QRectF
from PySide6.QtGui import QColor, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter
from PySide6.QtSvg import QSvgRenderer

#: Blank border left around the sheet on a printed page, in millimetres. The sheet already
#: carries its own ``schematic.MARGIN_MM``; this is the part a printer cannot reach.
PAGE_MARGIN_MM = 8.0

#: Pixels per millimetre of sheet for a PNG, matching the MCP board renderer's default so
#: the two pictures of one document come back at comparable sizes.
DEFAULT_PX_PER_MM = 12.0

MM_PER_INCH = 25.4


class SchematicRenderError(RuntimeError):
    """The SVG could not be parsed. Only reachable if the writer emitted something invalid,
    which is a bug here rather than bad input -- there is no user-supplied SVG on this
    path -- so it is raised loudly instead of returning a half-drawn page."""


def _renderer(svg: str) -> QSvgRenderer:
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise SchematicRenderError("the generated schematic SVG did not parse")
    return renderer


def _sheet_mm(renderer: QSvgRenderer) -> tuple[float, float]:
    """The sheet's size in millimetres, from the viewBox rather than ``defaultSize``.

    ``defaultSize`` is pixels at Qt's assumed 90 dpi, so it has already rounded and already
    chosen a resolution. The viewBox is the user-unit box the writer emitted, and every user
    unit in that file is one millimetre by construction -- the same scene unit
    ``ui/view2d.py`` uses, which is what lets both of them avoid a scale factor.
    """
    box = renderer.viewBoxF()
    return box.width(), box.height()


def _fitted(target: QRectF, aspect: float) -> QRectF:
    """``target`` shrunk to ``aspect`` (width / height) and centred inside it.

    Done here rather than with ``setAspectRatioMode`` because the default is
    ``IgnoreAspectRatio``: forgetting the call does not fail, it silently prints a
    schematic stretched to the shape of the paper, which reads as a slightly wrong drawing
    rather than as a bug.
    """
    if aspect <= 0 or target.width() <= 0 or target.height() <= 0:
        return target
    width, height = target.width(), target.height()
    if width / height > aspect:
        width = height * aspect
    else:
        height = width / aspect
    return QRectF(
        target.x() + (target.width() - width) / 2,
        target.y() + (target.height() - height) / 2,
        width,
        height,
    )


def svg_to_pdf(
    svg: str,
    path: str | Path,
    title: str = "",
    page_size: QPageSize.PageSizeId = QPageSize.PageSizeId.A4,
    resolution: int = 600,
) -> Path:
    """Write the sheet to a one-page PDF, fitted to the page and centred.

    Orientation follows the sheet rather than a convention: a tall circuit on a landscape
    page wastes half the paper and halves the text, and which way round a schematic runs is
    decided by the layout, not by the printer.
    """
    out = Path(path)
    renderer = _renderer(svg)
    sheet_w, sheet_h = _sheet_mm(renderer)

    writer = QPdfWriter(str(out))
    writer.setPageSize(QPageSize(page_size))
    writer.setPageOrientation(
        QPageLayout.Orientation.Landscape
        if sheet_w > sheet_h
        else QPageLayout.Orientation.Portrait
    )
    writer.setPageMargins(
        QMarginsF(PAGE_MARGIN_MM, PAGE_MARGIN_MM, PAGE_MARGIN_MM, PAGE_MARGIN_MM),
        QPageLayout.Unit.Millimeter,
    )
    writer.setResolution(resolution)
    if title:
        writer.setTitle(title)

    painter = QPainter(writer)
    page = writer.pageLayout().paintRectPixels(resolution)
    renderer.render(
        painter,
        _fitted(QRectF(page), sheet_w / sheet_h if sheet_h else 1.0),
    )
    painter.end()
    return out


def svg_to_image(
    svg: str,
    px_per_mm: float = DEFAULT_PX_PER_MM,
    background: str = "#ffffff",
) -> QImage:
    """Rasterise the sheet at ``px_per_mm`` pixels per millimetre of sheet.

    Opaque rather than transparent: the SVG paints its own background over the whole
    viewBox, and a PNG whose corners were transparent anyway would go dark the moment it
    was pasted into a message thread in dark mode.

    ``Format_ARGB32`` IS THE POINT OF THIS FUNCTION, not an incidental choice. Painting text
    onto ``Format_RGB32`` on Windows gets ClearType -- subpixel antialiasing, which draws
    black text as orange and blue pixels because it is exploiting the stripe order of one
    particular monitor. In a file that is not a rendering trick, it is wrong data: it
    survives into the print, into the resize, and onto every screen with a different
    subpixel layout. An image with an alpha channel cannot be rendered that way, so Qt falls
    back to plain greyscale antialiasing, which is what an exported picture should have. The
    image is filled opaque and stays opaque; the alpha channel is there to change how the
    text is drawn. ``test_an_exported_png_has_no_subpixel_colour_fringes`` measures it.

    Separate from :func:`svg_to_png` because the MCP server wants the pixels and not a
    file: it hands an agent a picture over the protocol, and a temporary file on the way
    would be a filesystem round trip in the one part of this codebase that has no business
    touching the disk.
    """
    renderer = _renderer(svg)
    sheet_w, sheet_h = _sheet_mm(renderer)
    image = QImage(
        max(1, round(sheet_w * px_per_mm)),
        max(1, round(sheet_h * px_per_mm)),
        QImage.Format.Format_ARGB32,
    )
    image.fill(QColor(background))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, image.width(), image.height()))
    painter.end()
    return image


def svg_to_png(
    svg: str,
    path: str | Path,
    px_per_mm: float = DEFAULT_PX_PER_MM,
    background: str = "#ffffff",
) -> Path:
    """:func:`svg_to_image`, written to a file."""
    out = Path(path)
    if not svg_to_image(svg, px_per_mm=px_per_mm, background=background).save(str(out)):
        raise SchematicRenderError(f"could not write {out}")
    return out


__all__ = [
    "DEFAULT_PX_PER_MM",
    "PAGE_MARGIN_MM",
    "SchematicRenderError",
    "svg_to_image",
    "svg_to_pdf",
    "svg_to_png",
]
