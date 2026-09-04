"""The schematic, exported: the SVG writer, and the PDF and PNG made out of it.

Two halves, and the split is the architecture. ``perfstudio.schematic_export`` is an engine
module -- pure, no Qt -- so most of what matters here is asserted by reading a string, which
is what lets a whole sheet be frozen in ``tests/schematic_golden/*.svg`` next to the text
dumps ``test_schematic.py`` already keeps. ``perfstudio.ui.export_schematic`` only asks Qt
to paginate or rasterise that string, so the tests for it are about the three things Qt can
get wrong on the way out: the size, the aspect ratio and the colour of antialiased text.

Re-bless the goldens with ``PERFSTUDIO_BLESS_SCHEMATIC=1`` -- the same switch that blesses
the text dumps, deliberately, because they describe one drawing and blessing half of it
would leave the two disagreeing -- AFTER READING THE DIFF. A readable diff is the point.

THE FONT TRAP APPLIES HERE. Qt's offscreen platform ships no font database on Windows, so
an exported sheet comes back with every line correct and no text at all, silently. Anything
below that asserts on rendered TEXT goes through ``_needs_text``; anything that asserts on
the drawing does not, and runs everywhere.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ElementTree
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QFontDatabase, QImage
from PySide6.QtWidgets import QApplication

from perfstudio import persist
from perfstudio.footprints import footprint_lookup
from perfstudio.model import Board, DocumentMeta, Net, NetNode, PerfDocument
from perfstudio.schematic import (
    SchematicDrawing,
    build_schematic,
    no_connect_arms,
    rail_glyph_bars,
)
from perfstudio.schematic_export import PAPER, SheetInk, drawing_to_svg
from perfstudio.ui.export_schematic import (
    SchematicRenderError,
    _fitted,
    svg_to_pdf,
    svg_to_png,
)
from perfstudio.version import __version__

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = ROOT / "tools" / "diffcheck" / "golden"
EXAMPLES_DIR = ROOT / "examples"
EXPECTED_DIR = Path(__file__).resolve().parent / "schematic_golden"
REGISTRY = footprint_lookup()

#: Every board in the repository. The structural claims below are claims about all inputs.
ALL_BOARDS = sorted(GOLDEN_DIR.glob("*.perf")) + sorted(EXAMPLES_DIR.glob("*.perf"))

#: The two sheets ``test_schematic.py`` freezes, frozen again in the format that gets
#: printed. Between them they cover nine of the ten symbol kinds and all three net classes.
FROZEN = ("ne555-astable", "lm317-supply")

SVG_NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication(["perfstudio-schematic-export-tests"])


def _needs_text(app: QApplication) -> None:
    """Skip where Qt has no fonts, which is not a failure of anything being tested."""
    assert app is not None
    if not QFontDatabase.families():
        pytest.skip("this Qt platform has no font database, so no text can be rendered")


def load(path: Path) -> PerfDocument:
    result = persist.deserialize_document(path.read_text(encoding="utf-8"))
    assert result.ok, result.message
    return result.document


def drawing_for(path: Path) -> SchematicDrawing:
    return build_schematic(load(path), REGISTRY)


def svg_for(stem: str, title: str | None = None) -> str:
    return drawing_to_svg(drawing_for(EXAMPLES_DIR / f"{stem}.perf"), title=title)


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda p: p.stem)
def test_every_board_in_the_repository_writes_well_formed_xml(path: Path) -> None:
    """An SVG that does not parse is not a picture, it is a file that renders as nothing --
    and it renders as nothing in a viewer with no error, which is the failure this catches
    on nineteen boards rather than on a chosen one."""
    root = ElementTree.fromstring(drawing_to_svg(drawing_for(path), title=path.stem))
    assert root.tag == f"{SVG_NS}svg"


def test_an_empty_document_still_writes_a_sheet_rather_than_a_broken_one() -> None:
    """A new document has no parts, and the export button is reachable before the first one
    is added. A zero-width viewBox is not a valid SVG, so the sheet gets a floor."""
    document = PerfDocument(
        meta=DocumentMeta(name="empty", created="", modified=""),
        board=Board(
            type="pad-per-hole",
            cols=10,
            rows=10,
            pitch=2.54,
            thickness=1.6,
            material="FR4",
            pad_diameter=1.9,
            drill_diameter=0.8,
        ),
    )
    root = ElementTree.fromstring(drawing_to_svg(build_schematic(document, REGISTRY)))
    box = [float(value) for value in (root.get("viewBox") or "").split()]
    assert box[2] > 0 and box[3] > 0


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda p: p.stem)
def test_nothing_on_the_sheet_is_dropped_on_the_way_out(path: Path) -> None:
    """Every wire, rail, junction, symbol shape and label reaches the file.

    Counted rather than eyeballed because the way an exporter goes wrong is by quietly
    handling one kind of thing and not another -- the sheet still looks like a schematic,
    and one net simply is not on it.
    """
    drawing = drawing_for(path)
    root = ElementTree.fromstring(drawing_to_svg(drawing, title=path.stem))

    polylines = sum(1 for _ in root.iter(f"{SVG_NS}polyline"))
    circles = sum(1 for _ in root.iter(f"{SVG_NS}circle"))
    polygons = sum(1 for _ in root.iter(f"{SVG_NS}polygon"))
    texts = sum(1 for _ in root.iter(f"{SVG_NS}text"))
    groups = sum(1 for _ in root.iter(f"{SVG_NS}g"))

    shape_kinds = [shape.kind for symbol in drawing.symbols for shape in symbol.shapes]
    assert polylines == (
        len(drawing.wires) + len(drawing.rails) + shape_kinds.count("polyline")
    )
    assert polygons == shape_kinds.count("polygon")
    assert circles == len(drawing.junctions) + shape_kinds.count("circle")
    # The sheet's title, every label, and one line per note.
    assert texts == 1 + len(drawing.labels) + len(drawing.notes)
    # One wrapper for the strokes, one per symbol.
    assert groups == 1 + len(drawing.symbols)


def test_the_rail_glyphs_are_the_ones_the_layout_made_room_for() -> None:
    """The bars in the file are exactly ``schematic.rail_glyph_bars``, to the coordinate.

    ONE FACT, TWO RENDERERS, and this is the exporter's half of it: the panel paints the
    same function's output. If either grew its own copy of the glyph geometry, the printed
    sheet and the screen would disagree about which rail is which -- and the wider of the
    two would be drawing bars through wires the LAYOUT cleared room for on the strength of
    ``RAIL_GLYPH_MM``, which is measured by ``test_nothing_is_drawn_through_a_rail_glyph``.
    """
    drawing = drawing_for(EXAMPLES_DIR / "ne555-astable.perf")
    assert drawing.rails, "this fixture is chosen for having rails"
    root = ElementTree.fromstring(drawing_to_svg(drawing))

    drawn = {
        (
            round(float(line.get("x1") or 0), 3),
            round(float(line.get("y1") or 0), 3),
            round(float(line.get("x2") or 0), 3),
            round(float(line.get("y2") or 0), 3),
        )
        for line in root.iter(f"{SVG_NS}line")
    }
    bars = {
        (round(a.x, 3), round(a.y, 3), round(b.x, 3), round(b.y, 3))
        for rail in drawing.rails
        for a, b in rail_glyph_bars(rail)
    }
    crosses = {
        (round(a.x, 3), round(a.y, 3), round(b.x, 3), round(b.y, 3))
        for mark in drawing.no_connects
        for a, b in no_connect_arms(mark)
    }
    # Both directions, which is what makes this more than a subset check: every bar the
    # layout cleared room for is in the file, and every straight line in the file is one
    # the layout owns the coordinates of. `<line>` is shared with the no-connect cross,
    # which is the other shape `schematic.py` hands out rather than letting a renderer
    # invent -- so it is named here instead of loosening the assertion to a subset.
    assert bars <= drawn, "a rail bar the layout made room for is missing from the file"
    assert drawn == bars | crosses, "the file draws a line the layout did not place"


def test_the_notes_are_printed_with_the_sheet() -> None:
    """A part nothing defines is a hole in the design. The panel says so in its summary;
    the exported sheet has to say so too, or somebody prints the drawing, takes it to the
    bench and builds a circuit whose defects were on the screen they walked away from."""
    document = PerfDocument(
        meta=DocumentMeta(name="missing", created="", modified=""),
        board=Board(
            type="pad-per-hole",
            cols=20,
            rows=20,
            pitch=2.54,
            thickness=1.6,
            material="FR4",
            pad_diameter=1.9,
            drill_diameter=0.8,
        ),
        nets=(
            Net(
                id="net-1",
                name="OUT",
                nodes=(NetNode(component_ref="U9", pin="1"), NetNode(component_ref="R4", pin="2")),
            ),
        ),
    )
    drawing = build_schematic(document, REGISTRY)
    assert drawing.notes
    svg = drawing_to_svg(drawing)
    for note in drawing.notes:
        assert note in svg


def test_a_part_nothing_defines_survives_a_monochrome_print() -> None:
    """Dashed AND coloured, not either one.

    The sheet is black on white by default precisely so it can be photocopied, so a defect
    marked only by a colour is a defect that disappears the first time the drawing is
    printed on the machine at work.
    """
    document = PerfDocument(
        meta=DocumentMeta(name="missing", created="", modified=""),
        board=Board(
            type="pad-per-hole",
            cols=20,
            rows=20,
            pitch=2.54,
            thickness=1.6,
            material="FR4",
            pad_diameter=1.9,
            drill_diameter=0.8,
        ),
        nets=(
            Net(
                id="net-1",
                name="OUT",
                nodes=(NetNode(component_ref="U9", pin="1"), NetNode(component_ref="R4", pin="2")),
            ),
        ),
    )
    drawing = build_schematic(document, REGISTRY)
    assert any(symbol.undefined for symbol in drawing.symbols)
    svg = drawing_to_svg(drawing)
    assert "stroke-dasharray" in svg
    assert PAPER.undefined in svg


def test_the_sheet_is_black_on_white_unless_a_caller_says_otherwise() -> None:
    """The default has no colour in it at all except the one defect marker.

    Not a style preference: the rail glyphs already tell a reader which rail sinks and which
    sources (``viewsch._rail_glyph`` says why), so colour would be a second, redundant
    channel that half the destinations -- a printer, a photocopier, a projector -- throw
    away.
    """
    svg = svg_for("ne555-astable")
    drawing = drawing_for(EXAMPLES_DIR / "ne555-astable.perf")
    assert not any(symbol.undefined for symbol in drawing.symbols)
    colours = {
        value
        for element in ElementTree.fromstring(svg).iter()
        for key, value in element.items()
        if key in ("stroke", "fill") and value != "none"
    }
    for colour in colours:
        assert colour.startswith("#")
        red, green, blue = colour[1:3], colour[3:5], colour[5:7]
        assert red == green == blue, f"{colour} is not a grey"


def test_a_caller_that_wants_colour_can_have_it() -> None:
    """``SheetInk`` keeps the three net classes as separate fields for exactly this. The
    default being monochrome is a decision about paper, not a limit of the writer."""
    drawing = drawing_for(EXAMPLES_DIR / "ne555-astable.perf")
    svg = drawing_to_svg(drawing, ink=SheetInk(power="#e0a33c", ground="#8f97a8"))
    assert "#e0a33c" in svg and "#8f97a8" in svg


def test_writing_the_same_drawing_twice_gives_the_same_bytes() -> None:
    """The whole point of freezing a sheet. ``schematic.py`` breaks every tie by reference
    and net id so the layout cannot rearrange itself between runs; an exporter that iterated
    a set somewhere would put that back."""
    drawing = drawing_for(EXAMPLES_DIR / "lm317-supply.perf")
    assert drawing_to_svg(drawing, title="x") == drawing_to_svg(drawing, title="x")


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda p: p.stem)
def test_no_coordinate_is_written_as_negative_zero(path: Path) -> None:
    """``-0`` and ``0`` are the same point and would be a golden diff nobody caused -- the
    kind that appears on one platform's libm and not another's."""
    svg = drawing_to_svg(drawing_for(path), title=path.stem)
    assert '"-0"' not in svg
    assert "-0," not in svg
    assert ",-0 " not in svg


@pytest.mark.parametrize("path", ALL_BOARDS, ids=lambda p: p.stem)
def test_the_view_box_holds_the_whole_sheet(path: Path) -> None:
    """A viewBox short of the drawing crops it, silently, at the edge -- and the edge is
    where the outermost column of symbols is."""
    drawing = drawing_for(path)
    root = ElementTree.fromstring(drawing_to_svg(drawing, title=path.stem))
    x, y, width, height = (float(value) for value in (root.get("viewBox") or "").split())
    assert x <= 0 and y <= 0
    assert width >= drawing.width and y + height >= drawing.height


def test_the_version_that_wrote_it_is_in_the_file() -> None:
    """An exported sheet outlives the session that produced it, and the first question
    about one that looks wrong is which build drew it. Same reasoning as
    ``guide_to_json``'s generator line, and it is substituted out of the goldens below for
    the same reason too."""
    assert f"PerfStudio {__version__}" in svg_for("ne555-astable")


def test_the_title_is_the_callers_and_is_escaped() -> None:
    """The document name reaches the file as text, and a document may be called anything --
    including something with an ampersand in it, which would end the XML document there."""
    drawing = drawing_for(EXAMPLES_DIR / "ne555-astable.perf")
    svg = drawing_to_svg(drawing, title='R&D "rev 2" <draft>')
    root = ElementTree.fromstring(svg)
    titles = [element.text for element in root.iter(f"{SVG_NS}title")]
    assert 'R&D "rev 2" <draft>' in titles


# ---------------------------------------------------------------------------
# The whole sheet, frozen
# ---------------------------------------------------------------------------

#: How many lines of a golden diff to print. Enough to see what moved.
DIFF_LINES = 40


@pytest.mark.parametrize("stem", FROZEN)
def test_the_svg_is_the_svg_that_was_blessed(stem: str) -> None:
    import difflib

    produced = svg_for(stem, title=stem).replace(__version__, "{VERSION}")
    expected_path = EXPECTED_DIR / f"{stem}.svg"

    if os.environ.get("PERFSTUDIO_BLESS_SCHEMATIC"):
        EXPECTED_DIR.mkdir(exist_ok=True)
        # An explicit LF, the way `test_guide_golden` already writes its own. The
        # repository is LF everywhere, and a bless run on Windows would otherwise put
        # CRLF into a file every other platform leaves alone.
        expected_path.write_text(produced, encoding="utf-8", newline="\n")
        pytest.skip(f"blessed {expected_path.name}")

    assert expected_path.exists(), (
        f"{expected_path} is missing. Run with PERFSTUDIO_BLESS_SCHEMATIC=1 to create it."
    )
    expected = expected_path.read_text(encoding="utf-8")
    if produced != expected:
        diff = list(
            difflib.unified_diff(
                expected.splitlines(), produced.splitlines(), "blessed", "produced", lineterm=""
            )
        )
        shown = "\n".join(diff[:DIFF_LINES])
        more = f"\n... and {len(diff) - DIFF_LINES} more lines" if len(diff) > DIFF_LINES else ""
        pytest.fail(f"{expected_path.name} changed:\n{shown}{more}")


# ---------------------------------------------------------------------------
# PDF and PNG, which are the SVG through Qt
# ---------------------------------------------------------------------------


def test_fitted_keeps_the_aspect_ratio_and_centres_what_is_left() -> None:
    """Qt's default is ``IgnoreAspectRatio``, which does not fail -- it prints a schematic
    stretched to the shape of the paper, which reads as a slightly wrong drawing rather
    than as a bug. So the fit is arithmetic here and this is what checks it."""
    page = QRectF(0, 0, 200, 100)

    wide = _fitted(page, 4.0)  # wider than the page: pinned by width
    assert wide.width() == pytest.approx(200)
    assert wide.height() == pytest.approx(50)
    assert wide.center().y() == pytest.approx(page.center().y())

    tall = _fitted(page, 0.5)  # taller than the page: pinned by height
    assert tall.height() == pytest.approx(100)
    assert tall.width() == pytest.approx(50)
    assert tall.center().x() == pytest.approx(page.center().x())


def test_the_png_is_the_size_the_sheet_asked_for(qapp: QApplication, tmp_path: Path) -> None:
    drawing = drawing_for(EXAMPLES_DIR / "ne555-astable.perf")
    # Untitled, so the sheet is exactly the drawing: the band a title needs is part of the
    # viewBox rather than something the renderer adds, and this keeps the sum to one term.
    out = svg_to_png(drawing_to_svg(drawing), tmp_path / "sheet.png", px_per_mm=6.0)

    image = QImage(str(out))
    assert not image.isNull()
    assert image.width() == round(drawing.width * 6.0)
    assert image.height() == round(drawing.height * 6.0)


def test_the_png_actually_contains_the_drawing(qapp: QApplication, tmp_path: Path) -> None:
    """A blank white page is what a broken renderer produces, and it is also what a
    successful export of nothing produces, so the two have to be told apart."""
    svg = svg_for("ne555-astable", title="ne555")
    image = QImage(str(svg_to_png(svg, tmp_path / "sheet.png", px_per_mm=6.0)))
    dark = sum(
        1
        for y in range(0, image.height(), 3)
        for x in range(0, image.width(), 3)
        if image.pixelColor(x, y).lightness() < 128
    )
    assert dark > 200, "the sheet came out blank"


def test_an_exported_png_has_no_subpixel_colour_fringes(
    qapp: QApplication, tmp_path: Path
) -> None:
    """Black text must come out grey, not orange and blue.

    Painting text onto ``Format_RGB32`` on Windows gets ClearType: subpixel antialiasing,
    which is a rendering trick that exploits the stripe order of one particular monitor. In
    a FILE it is wrong data -- it survives the print, the resize, and the reader whose
    screen is laid out the other way round. ``svg_to_png`` asks for an alpha channel to
    force greyscale antialiasing instead, and this is the measurement that says so; it is
    also why the test is worth having, since nothing about the code says "no ClearType".
    """
    _needs_text(qapp)
    image = QImage(str(svg_to_png(svg_for("ne555-astable", title="ne555"), tmp_path / "s.png")))
    assert not image.isNull()
    worst = 0
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            channels = (colour.red(), colour.green(), colour.blue())
            worst = max(worst, max(channels) - min(channels))
    assert worst == 0, f"a pixel is {worst} levels off neutral: text was drawn with subpixels"


def test_the_pdf_is_a_pdf_and_takes_its_orientation_from_the_sheet(
    qapp: QApplication, tmp_path: Path
) -> None:
    """A tall circuit on a landscape page wastes half the paper and halves the text. Which
    way round a schematic runs is decided by the layout, not by a convention.

    The two sheets are BUILT here rather than loaded. This test used to take its tall one
    from `lm317-supply`, whose eleven parts all hung off U1 and were drawn in a single
    ten-row column; capping the height of a layer made that sheet landscape like every
    other real circuit, and the fixture guard fired -- correctly, and it is why the guard
    was there. What is under test is `svg_to_pdf` reading a shape and picking a page, so
    the shape is the input, and no layout change can take the tall case away again.
    """
    tall = SchematicDrawing(width=100.0, height=200.0)
    wide = SchematicDrawing(width=200.0, height=100.0)

    tall_pdf = svg_to_pdf(drawing_to_svg(tall), tmp_path / "tall.pdf")
    wide_pdf = svg_to_pdf(drawing_to_svg(wide), tmp_path / "wide.pdf")
    for path in (tall_pdf, wide_pdf):
        assert path.read_bytes()[:5] == b"%PDF-"

    # A4 either way round, so the page whose media box is wider is the landscape one.
    assert _media_box(wide_pdf)[0] > _media_box(wide_pdf)[1]
    assert _media_box(tall_pdf)[0] < _media_box(tall_pdf)[1]


def _media_box(path: Path) -> tuple[float, float]:
    """The first ``/MediaBox [a b c d]`` in the file, as (width, height) in points."""
    import re

    match = re.search(rb"/MediaBox\s*\[([^\]]*)\]", path.read_bytes())
    assert match is not None, f"{path.name} has no MediaBox"
    numbers = [float(value) for value in match.group(1).split()]
    return numbers[2] - numbers[0], numbers[3] - numbers[1]


def test_a_broken_svg_is_refused_rather_than_half_drawn(
    qapp: QApplication, tmp_path: Path
) -> None:
    """Nothing on this path takes an SVG from a user, so an unparseable one is a bug in the
    writer. It is raised rather than swallowed: a blank page written successfully is the
    outcome that gets noticed after the thing is printed."""
    with pytest.raises(SchematicRenderError):
        svg_to_png("<svg><this is not xml", tmp_path / "no.png")
