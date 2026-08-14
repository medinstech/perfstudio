"""Visual regression for the 2D board render (PLAN.md §10).

WHAT THIS CATCHES AND WHY THERE WAS NOTHING. The headless run has always produced PNGs
and CI has always kept them as artefacts, but the only thing asserted about one was that
it began with the PNG magic bytes -- so a render that lost every pad, drew the board
inside out or came out blank passed the build. Somebody had to open the artefact and
look, which is exactly the thing nobody does on a green tick.

WHY NOT A PIXEL DIFF against a stored PNG, which is the obvious answer: antialiasing,
font rasterisation and Qt's own version move individual pixels across platforms, and this
project runs its suite on three. A per-pixel golden would fail on macOS for reasons that
have nothing to do with the board, and a test that fails for the wrong reason gets
switched off -- the same argument the repository makes about linting.

WHAT IS COMPARED INSTEAD is a coarse signature: the MEAN COLOUR of each cell of a 6 x 6
grid, plus how much of the picture is not background and how many distinct colours it
uses. Averaging is what makes it both stable and sharp -- an antialiased edge moves a
cell mean by a fraction of one level out of 255, while the failures that matter move it
by twenty or more. Measured on this board: re-rendering it is 0.0, rendering the OTHER
FACE is 27.5, and rendering it with every part and conductor removed is 22.6, against a
tolerance of 3.

The first thing tried was the fraction of each cell covered in ink, and it is recorded
here because it looked reasonable and was nearly useless: a perfboard is mostly board, so
"not background" is about 80% of every cell whether or not anything is on it, and losing
every part on the board moved a cell by 2.6 points against a tolerance of 2. A signature
has to be sensitive to what is ON the board, not to where the board is.

NO TEXT IS IN THE PICTURE. The rulers are turned off, exactly as the 1:1 PDF export turns
them off, because a font database is the one part of this that genuinely differs between
platforms -- Qt's offscreen plugin ships none at all on Windows.

To re-bless after a deliberate change: run this file with PERFSTUDIO_BLESS_RENDER=1 and
commit the diff, having looked at the render first.

The signature was blessed on Windows and the suite runs on three platforms. If another
one lands a little outside the tolerance for reasons that are plainly not the board -- a
different Qt raster engine version, say -- the honest fixes in order of preference are:
widen TOLERANCE with the measurement written down, or key the stored signatures by
platform. Deleting the test is not one of them; it is the only thing that looks at what
was drawn.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from perfstudio import persist
from perfstudio.footprints import footprint_lookup
from perfstudio.model import BoardSide, PerfDocument
from perfstudio.ui.view2d import BoardScene

GOLDEN_DIR = pathlib.Path(__file__).resolve().parents[1] / "tools" / "diffcheck" / "golden"
SIGNATURES = pathlib.Path(__file__).resolve().parent / "render_signatures.json"

#: Cells per side. Six is enough to localise a failure ("the bottom third is empty")
#: without making any one cell so small that antialiasing dominates it.
GRID = 6

#: Render size, fixed so the signature means the same thing every time -- and divisible
#: by GRID, so every cell holds exactly the same number of pixels and a cell fraction
#: cannot come out fractionally over 1.
WIDTH, HEIGHT = 900, 600

#: How far a cell's mean colour may move, per channel out of 255, before it is a
#: regression. Three is an order of magnitude above any antialiasing difference and an
#: order of magnitude below the smallest real failure measured (see the docstring).
TOLERANCE = 3.0

#: The overall ink fraction is a blunter instrument and gets a blunter band: it is here
#: to catch a blank or half-drawn image, not to police the drawing.
INK_TOLERANCE = 0.05

BACKGROUND = QColor("#12131a")


def _document(name: str) -> PerfDocument:
    result = persist.deserialize_document((GOLDEN_DIR / f"{name}.perf").read_text(encoding="utf-8"))
    assert result.ok, f"{name}.perf failed to load: {result}"
    return result.document


def _render(doc: PerfDocument, side: BoardSide) -> QImage:
    """The board, and nothing that is not the board.

    Rulers and the ratsnest off: the first draws text and the second draws what is NOT
    built yet, and neither is what this test is about.
    """
    scene = BoardScene(doc, footprint_lookup(), side=side, show_ratsnest=False, show_rulers=False)
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32)
    image.fill(BACKGROUND)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    scene.render(painter, QRectF(0, 0, WIDTH, HEIGHT), scene.sceneRect())
    painter.end()
    return image


def _signature(image: QImage) -> dict[str, object]:
    """The mean colour of each cell, how much is not background, and how many colours.

    One pass over the pixels for all three. ``pixelColor`` is slow enough that a second
    pass would be noticeable on a 900 x 600 image, and there is no reason to make one.
    """
    background = (BACKGROUND.red(), BACKGROUND.green(), BACKGROUND.blue())
    cell_w, cell_h = image.width() // GRID, image.height() // GRID
    sums = [[0, 0, 0] for _ in range(GRID * GRID)]
    colours: set[int] = set()
    ink = 0

    for y in range(image.height()):
        row_cell = min(GRID - 1, y // cell_h) * GRID
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            rgb = (colour.red(), colour.green(), colour.blue())
            cell = sums[row_cell + min(GRID - 1, x // cell_w)]
            cell[0] += rgb[0]
            cell[1] += rgb[1]
            cell[2] += rgb[2]
            if max(abs(a - b) for a, b in zip(rgb, background, strict=True)) <= 6:
                continue
            ink += 1
            # Quantised to 5 bits a channel: enough to tell copper from substrate from a
            # part body, coarse enough that a shading difference is not a new colour.
            colours.add(((rgb[0] >> 3) << 10) | ((rgb[1] >> 3) << 5) | (rgb[2] >> 3))

    per_cell = cell_w * cell_h
    return {
        "size": [image.width(), image.height()],
        "ink": round(ink / (image.width() * image.height()), 4),
        "cells": [[round(channel / per_cell, 2) for channel in cell] for cell in sums],
        "colours": len(colours),
    }


def _load_signatures() -> dict[str, dict[str, object]]:
    if not SIGNATURES.exists():
        return {}
    loaded: dict[str, dict[str, object]] = json.loads(SIGNATURES.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication(["perfstudio-render-tests"])
    yield app


CASES = [("dense", "top"), ("dense", "bottom"), ("ne555", "top"), ("ne555", "bottom")]


@pytest.mark.parametrize(("name", "side"), CASES)
def test_the_board_still_renders_the_way_it_did(name: str, side: str) -> None:
    signature = _signature(_render(_document(name), side))  # type: ignore[arg-type]
    key = f"{name}:{side}"

    if os.environ.get("PERFSTUDIO_BLESS_RENDER"):
        stored = _load_signatures()
        stored[key] = signature
        SIGNATURES.write_text(
            json.dumps(dict(sorted(stored.items())), indent=2) + "\n", encoding="utf-8"
        )
        pytest.skip(f"blessed {key}")

    expected = _load_signatures().get(key)
    assert expected is not None, (
        f"no stored signature for {key}. Run with PERFSTUDIO_BLESS_RENDER=1 to write one, "
        f"after looking at the render."
    )
    assert signature["size"] == expected["size"]
    assert abs(float(signature["ink"]) - float(expected["ink"])) < INK_TOLERANCE, (  # type: ignore[arg-type]
        f"{key}: the amount of board drawn moved"
    )
    got_cells = signature["cells"]
    want_cells = expected["cells"]
    assert isinstance(got_cells, list) and isinstance(want_cells, list)
    drift, worst_cell = max(
        (max(abs(float(a) - float(b)) for a, b in zip(got, want, strict=True)), index)
        for index, (got, want) in enumerate(zip(got_cells, want_cells, strict=True))
    )
    assert drift < TOLERANCE, (
        f"{key}: cell {worst_cell} (row {worst_cell // GRID}, column {worst_cell % GRID}) "
        f"changed colour by {drift:.1f} of 255 — got {got_cells[worst_cell]}, "
        f"want {want_cells[worst_cell]}"
    )
    # A wide band rather than an equality: the exact count depends on how many antialiased
    # in-between shades a renderer produces, but losing the copper or the part bodies
    # halves it.
    assert 0.6 <= float(signature["colours"]) / float(expected["colours"]) <= 1.6, (  # type: ignore[arg-type]
        f"{key}: {signature['colours']} distinct colours against {expected['colours']}"
    )


def test_the_two_faces_of_a_board_do_not_render_the_same(qapp) -> None:
    """The failure this rules out is the one that would pass every assertion above: a
    render that ignores which side it was asked for. The solder side is where the copper
    is and is a different picture, which is the whole reason it is a first-class view."""
    doc = _document("dense")

    top = _signature(_render(doc, "top"))
    bottom = _signature(_render(doc, "bottom"))

    assert top["cells"] != bottom["cells"]


def test_the_signature_notices_a_board_that_lost_its_contents(qapp) -> None:
    """The proof that the tolerance is worth something. A signature nobody has shown to
    fail is a signature that might be comparing nothing to nothing -- so this takes every
    part and every conductor off the board and measures how far that moves it."""
    doc = _document("dense")
    full = _signature(_render(doc, "top"))
    stripped = _signature(_render(dataclasses.replace(doc, components=(), conductors=()), "top"))

    drift = max(
        max(abs(float(a) - float(b)) for a, b in zip(got, want, strict=True))
        for got, want in zip(full["cells"], stripped["cells"], strict=True)  # type: ignore[arg-type]
    )

    assert drift > TOLERANCE * 5, f"an empty board is only {drift:.1f} from a full one"


def test_a_render_is_not_blank(qapp) -> None:
    """The one that would have caught every rendering catastrophe this file exists for,
    stated on its own so a failure says 'blank' rather than 'cell 14 moved'."""
    signature = _signature(_render(_document("ne555"), "top"))

    assert float(signature["ink"]) > 0.2  # type: ignore[arg-type]
    assert int(signature["colours"]) > 10  # type: ignore[arg-type]
