"""Draw the application icon, and write the files a packager needs.

    python tools/make_assets.py

Writes `src/perfstudio/ui/assets/`: `perfstudio.png` (256 px, for the AppImage and the
desktop entry) and `perfstudio.ico` (for the Windows executable and installer).

WHY THIS IS A SCRIPT AND NOT A PAIR OF FILES SOMEBODY DREW. `ui/icons.py` already argues
the case for the toolbar: an icon drawn in code cannot fall out of step with the palette
around it, and there is no licence to track. The application mark is the same argument
with one extra constraint -- Windows wants an `.ico`, the AppImage wants a `.png`, and a
`.desktop` file wants the basename to match. Those are real files that a build reads, so
they are generated once and committed, rather than being drawn once and slowly becoming
a picture of an older version of the product.

WHAT THE MARK IS. A corner of perfboard: the substrate, a three-by-three of plated pads,
and a solder trace joining two of them. Every colour comes from `ui.boardcolors`, so the
icon is the same green and the same gold as the board in the editor. The trace is the
point -- a grid of holes alone is a picture of a blank board, and this application is
about what gets joined to what.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from perfstudio.ui.boardcolors import BY_KEY  # noqa: E402

OUT_DIR = REPO_ROOT / "src" / "perfstudio" / "ui" / "assets"

#: The FR-4 board, because it is the one on the front page and the one most people have.
SCHEME = BY_KEY["green"]

#: Sizes inside an .ico. 16 and 32 are the ones actually seen -- the taskbar, the title
#: bar and Explorer's list view -- and they are also where a detailed drawing turns to
#: mud, which is why the mark is three pads and not a whole board.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw(size: int) -> QImage:
    """The mark, at one size, drawn in a unit box scaled to ``size``."""
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Every measurement below is a fraction of the icon, so one drawing serves 16 px and
    # 256 px alike -- the same trick ui/icons.py plays with its 100 x 100 box.
    u = size / 100.0

    # --- substrate ---------------------------------------------------------
    # Inset slightly: a mark that runs to the very edge of its box looks larger than its
    # neighbours in a taskbar, where everything else is drawn with a margin.
    board = QRectF(6 * u, 6 * u, 88 * u, 88 * u)
    painter.setPen(QPen(QColor(SCHEME.edge), max(1.0, 2 * u)))
    painter.setBrush(QBrush(QColor(SCHEME.fill)))
    painter.drawRoundedRect(board, 12 * u, 12 * u)

    # --- the solder trace, under the pads it joins -------------------------
    # Drawn first so the pads sit on top of it, which is the physical order: solder is
    # dragged across pads that are already there.
    #
    # The pads are drawn well short of touching for this reason alone: at a radius that
    # fills the pitch they meet, the trace is completely buried, and the mark becomes a
    # grid of holes -- a picture of a blank board, which is the one thing it must not be.
    trace = QColor(SCHEME.pad_sheen)
    painter.setPen(QPen(trace, 11 * u, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(QPointF(28 * u, 72 * u), QPointF(72 * u, 72 * u))

    # --- the pads ----------------------------------------------------------
    ring = QColor(SCHEME.pad_ring)
    for row in (28.0, 50.0, 72.0):
        for col in (28.0, 50.0, 72.0):
            centre = QPointF(col * u, row * u)
            radius = 8.5 * u

            # A radial gradient rather than a flat fill: a pad is a dome of solder over
            # copper, and the catch of light across it is what makes it read as metal
            # instead of a yellow dot. The same three colours the 2D view uses.
            gradient = QRadialGradient(
                centre + QPointF(-3 * u, -3 * u), radius * 1.4, centre + QPointF(-3 * u, -3 * u)
            )
            gradient.setColorAt(0.0, QColor(SCHEME.pad_sheen))
            gradient.setColorAt(0.55, QColor(SCHEME.pad))
            gradient.setColorAt(1.0, ring)
            painter.setBrush(QBrush(gradient))
            painter.setPen(QPen(ring, max(0.75, 1.5 * u)))
            painter.drawEllipse(centre, radius, radius)

            # The hole. Every pad on a perfboard has one, and leaving it out is the
            # difference between a perfboard and a pad of stickers -- but below about
            # 24 px it closes up into a dark smudge that only makes the pad look dirty,
            # so it is dropped rather than drawn badly.
            if size >= 24:
                painter.setBrush(QBrush(QColor(SCHEME.edge)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(centre, 4 * u, 4 * u)

    painter.end()
    return image


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # QImage needs no window, but Qt's imaging still wants an application object.
    app = QApplication.instance() or QApplication(sys.argv[:1])
    assert app is not None

    png = OUT_DIR / "perfstudio.png"
    draw(256).save(str(png))

    # QImage cannot write a multi-size .ico, and Qt's ICO *writer* is not built into
    # every PySide6 wheel -- so the container is assembled here from PNG-compressed
    # frames, which is what a modern .ico is and what Windows has read since Vista.
    ico = OUT_DIR / "perfstudio.ico"
    _write_ico(ico, [draw(n) for n in ICO_SIZES])

    for path in (png, ico):
        print(f"{path.relative_to(REPO_ROOT)}  {path.stat().st_size // 1024} KB")
    return 0


def _write_ico(path: Path, images: list[QImage]) -> None:
    """Write a PNG-compressed .ico by hand.

    The format is a six-byte header, a sixteen-byte directory entry per image, then the
    image data. A 256-pixel image records its width and height as 0, which is the
    convention that let the format grow past a byte.
    """
    import struct
    from io import BytesIO

    from PySide6.QtCore import QBuffer, QByteArray

    payloads: list[bytes] = []
    for image in images:
        buffer_data = QByteArray()
        buffer = QBuffer(buffer_data)
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        buffer.close()
        payloads.append(bytes(buffer_data.data()))

    out = BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(images)))  # reserved, type 1 = icon, count
    offset = 6 + 16 * len(images)
    for image, payload in zip(images, payloads, strict=True):
        side = 0 if image.width() >= 256 else image.width()
        out.write(
            struct.pack(
                "<BBBBHHII",
                side, side,   # width, height (0 means 256)
                0,            # palette size; 0 for a truecolour image
                0,            # reserved
                1,            # colour planes
                32,           # bits per pixel
                len(payload),
                offset,
            )
        )
        offset += len(payload)
    for payload in payloads:
        out.write(payload)

    path.write_bytes(out.getvalue())


if __name__ == "__main__":
    raise SystemExit(main())
