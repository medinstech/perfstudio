"""Editor labels drawn at a constant on-screen size, positioned by scene coordinates.

THE PROBLEM. The 2D scene works in millimetres -- one scene unit is one mm, which is what
makes the 1:1 PDF exact -- and the view is scaled anywhere from 1.5 to 90 pixels per mm.
Text sized in scene units inherits that: a label that reads well at 12 px/mm is an
illegible smear at 2 and a wall of letters at 60. Ruler labels and component references are
annotation, not artwork on the board, so they should hold one comfortable size while the
board zooms underneath them -- which is how every editor with an axis ruler behaves.

There is a second, sharper reason not to size them in millimetres. A point is 1/72 inch OF
THE PAINT DEVICE, so a "2 pt" label is about 18 pixels on the PDF writer's 600 dpi page
and under 3 on a 96 dpi screen. At that size some platforms' font engines draw nothing at
all and report no error -- the offscreen Qt platform used for headless rendering is one of
them, which means a millimetre-sized label can vanish from CI output while looking fine on
a developer's screen. Sizing in device pixels puts every label well clear of that floor.

So: the position comes from the scene (the transform maps it), and the size does not (the
transform is reset before drawing). ``export_pdf`` deliberately does NOT use this -- printed
text has to scale with the page, and at 600 dpi ordinary point sizes work correctly.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QPainter

#: pixel size -> font. Rulers draw dozens of labels per repaint, so building a QFont and
#: its metrics every time is worth avoiding.
_CACHE: dict[tuple[int, bool], QFont] = {}


def label_font(pixel_size: int, bold: bool = False) -> QFont:
    key = (pixel_size, bold)
    cached = _CACHE.get(key)
    if cached is None:
        cached = QFont()
        cached.setPixelSize(pixel_size)
        cached.setBold(bold)
        _CACHE[key] = cached
    return cached


def draw_label(
    painter: QPainter,
    anchor: QPointF,
    text: str,
    pixel_size: int,
    alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
    bold: bool = False,
    offset: QPointF | None = None,
) -> None:
    """Draw ``text`` at a fixed ``pixel_size``, anchored at the scene point ``anchor``.

    ``offset`` shifts the label in DEVICE pixels after positioning, which is how a drop
    shadow or a few pixels of clearance stay constant instead of growing with zoom.
    """
    transform = painter.transform()
    device = transform.map(anchor)
    if offset is not None:
        device += offset

    font = label_font(pixel_size, bold)
    metrics = QFontMetricsF(font)
    width = metrics.horizontalAdvance(text)
    height = metrics.height()

    # A box around the anchor big enough for the text, then aligned inside it. Simpler and
    # more predictable than juggling baselines per alignment case.
    box = QRectF(device.x() - width, device.y() - height, width * 2, height * 2)

    painter.save()
    painter.resetTransform()
    painter.setFont(font)
    painter.drawText(box, int(alignment), text)
    painter.restore()


def label_extent_mm(pixel_size: int, scale: float) -> float:
    """How many millimetres of scene a ``pixel_size`` label occupies at ``scale`` px/mm.

    Items need this for ``boundingRect``: Qt uses the bounding rect to decide what to
    repaint, so a label drawn outside its item's rect can leave debris behind when the view
    scrolls. Since these labels do not shrink with the board, the scene-space room they need
    GROWS as the view zooms out -- the opposite of the usual intuition, and the reason this
    is computed rather than guessed at.
    """
    return pixel_size / max(scale, 0.01)
