"""3D board view on VTK, rewired onto the real engine.

Promoted from ``prototypes/qt/view3d.py``; the three claims it existed to test
(instanced pad rendering, offscreen render for the build guide, and a solder trace
looking different from a wire) are unchanged, only the data source is. The document is
a real ``perfstudio.model.PerfDocument`` and footprints come from an injected
``FootprintLookup`` (``perfstudio.footprints.footprint_lookup()`` in practice) rather
than a JSON sidecar file.

Axis note, unchanged from the prototype: rows grow downward in board space
(screen-like), so 3D uses y = -row*pitch. Looking down +Z then matches the 2D editor's
orientation instead of mirroring it.
"""

from __future__ import annotations

import functools
import math
import subprocess
import sys
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import vtk  # type: ignore[import-untyped]
from vtkmodules.util import numpy_support

from perfstudio.connectivity import FootprintLookup
from perfstudio.geometry import (
    all_pin_holes,
    board_edge_margin_mm,
    board_size_mm,
    column_label,
    edge_finger_rect,
    hole_key,
    holes_without_grid_pad,
    legend_strip_mm,
    mounting_hole_centre_mm,
    pad_extent_mm,
    printed_label_is_clear,
    printed_row_label,
    surviving_finger_holes,
    transform_offset,
    undrilled_holes,
)
from perfstudio.guide import Guide, all_steps, document_at_step, step_focus
from perfstudio.model import (
    Board,
    BoardSide,
    Conductor,
    HoleCoord,
    NetClass,
    PerfDocument,
    Point2,
    contacts_every_path_hole,
)
from perfstudio.occupancy import stacking_layers
from perfstudio.stripboard import cut_holes, segments

from .boardcolors import scheme_for
from .bodies import (
    BodyStyle,
    Surface,
    placement_for,
    polarity_pin_offset,
    resistor_bands,
    style_for,
    surface_for,
)

SUBSTRATE_RGB = {
    "FR4": (0.16, 0.36, 0.21),
    "FR2": (0.62, 0.48, 0.29),
    "FR1": (0.68, 0.55, 0.35),
}
#: Fallback copper. The real colour comes from the board's scheme -- bare copper on a
#: phenolic board, plated gold on a masked one.
PAD_RGB = (0.80, 0.66, 0.32)
#: Solder: dull pewter, and rough. Solder is NOT shiny wire, and PLAN.md Sec 8.3 makes
#: telling the two apart at a glance a requirement of this view rather than a nicety --
#: these two used to be (0.72,0.74,0.77) and (0.85,0.87,0.89), which is the same grey.
SOLDER_RGB = (0.62, 0.63, 0.66)
#: Tinned copper wire: brighter, and specular enough to read as metal.
BARE_RGB = (0.90, 0.92, 0.95)
#: An insulated wire with no net colour of its own.
INSULATED_RGB = (0.45, 0.47, 0.52)
#: The bore through the board. Near black, unlit: it is a hole.
DRILL_RGB = (0.06, 0.06, 0.07)
BODY_RGB = (0.22, 0.22, 0.26)
#: Tinned component lead.
LEAD_RGB = (0.78, 0.80, 0.84)
#: Silkscreen ink. Slightly off-white, because a printed legend never is.
LEGEND_RGB = (0.92, 0.93, 0.95)


def _hex_rgb(value: str | None, fallback: tuple[float, float, float]) -> tuple[float, float, float]:
    if not value or not value.startswith("#") or len(value) != 7:
        return fallback
    return (int(value[1:3], 16) / 255, int(value[3:5], 16) / 255, int(value[5:7], 16) / 255)


def _lit(value: str, factor: float) -> tuple[float, float, float]:
    """One colour a shade lighter or darker, clamped. For a detail that belongs to the
    part it sits on -- a capacitor's crimped rim, the groove scored into its top -- where
    a second colour from the table would read as a second material."""
    return tuple(min(1.0, channel * factor) for channel in _rgb(value))  # type: ignore[return-value]


def _rgb(value: str) -> tuple[float, float, float]:
    """A colour from ui/bodies.py, which is where 2D and 3D agree on what a part looks like."""
    return _hex_rgb(value, BODY_RGB)


def _xy(board: Board, hole: HoleCoord) -> tuple[float, float]:
    return hole.col * board.pitch, -hole.row * board.pitch


def _board_size_mm(board: Board) -> tuple[float, float]:
    """Delegates to geometry rather than repeating the arithmetic: the substrate here and
    the substrate in the 2D editor and on the 1:1 printout have to be the same size, and a
    second copy of the formula is how a board's border silently stops existing in 3D."""
    return board_size_mm(board)


# --------------------------------------------------------------------------- pieces


#: A rectangle in board millimetres: x0, y0, x1, y1, with y increasing upwards as the
#: renderer has it (a row further down the board is a smaller y).
_Rect = tuple[float, float, float, float]


def board_outline_rect(board: Board) -> _Rect:
    """The substrate's own extent. Pure, so it can be asserted directly.

    From ``board_size_mm``, never from the hole span: the two differ by the printed
    border, and a board whose substrate is drawn to the hole span has no border to print
    the row letters on.
    """
    w, h = _board_size_mm(board)
    centre_x = (board.cols - 1) * board.pitch / 2
    centre_y = -(board.rows - 1) * board.pitch / 2
    return (centre_x - w / 2, centre_y - h / 2, centre_x + w / 2, centre_y + h / 2)


def _tile_grid_rect(board: Board) -> _Rect:
    """What the tiles cover: half a pitch beyond the outermost hole centres, all round."""
    half = board.pitch / 2
    return (
        -half,
        -(board.rows - 1) * board.pitch - half,
        (board.cols - 1) * board.pitch + half,
        half,
    )


def _border_rects(board: Board) -> list[_Rect]:
    """The bare strip between the tiles and the board's edge, as up to four rectangles.

    Empty on a flush-cut board, which is the usual case: most stock is cut on the grid and
    ``border_x_mm``/``border_y_mm`` are zero.
    """
    x0, y0, x1, y1 = board_outline_rect(board)
    gx0, gy0, gx1, gy1 = _tile_grid_rect(board)
    # A tolerance, not a bare comparison: a flush-cut board's border is zero and the two
    # rectangles are computed by different routes, so they differ in the last bit and the
    # naive test produces four rectangles a thousandth of a micron wide.
    slop = board.pitch * 1e-6
    rects: list[_Rect] = []
    if gy1 + slop < y1:
        rects.append((x0, gy1, x1, y1))
    if gy0 - slop > y0:
        rects.append((x0, y0, x1, gy0))
    if gx0 - slop > x0:
        rects.append((x0, gy0, gx0, gy1))
    if gx1 + slop < x1:
        rects.append((gx1, gy0, x1, gy1))
    return rects


def _rect_without(rect: _Rect, hole: _Rect) -> list[_Rect]:
    """What is left of one rectangle once another is taken out of it: up to four pieces."""
    x0, y0, x1, y1 = rect
    hx0, hy0, hx1, hy1 = hole
    if hx1 <= x0 or hx0 >= x1 or hy1 <= y0 or hy0 >= y1:
        return [rect]
    pieces: list[_Rect] = []
    if hy1 < y1:
        pieces.append((x0, hy1, x1, y1))
    if hy0 > y0:
        pieces.append((x0, y0, x1, hy0))
    band_y0, band_y1 = max(y0, hy0), min(y1, hy1)
    if hx0 > x0:
        pieces.append((x0, band_y0, hx0, band_y1))
    if hx1 < x1:
        pieces.append((hx1, band_y0, x1, band_y1))
    return pieces


def _rects_without(rects: list[_Rect], holes: list[_Rect]) -> list[_Rect]:
    for hole in holes:
        rects = [piece for rect in rects for piece in _rect_without(rect, hole)]
    return rects


@dataclass(frozen=True, slots=True)
class _Bore:
    """A hole wider than the grid's own, with the patch of plate it is punched in.

    ``covers`` is the set of tile squares the bore reaches into. They are taken out of the
    tiled surface and this one patch is laid over the lot -- the outer boundary of their
    union, with the bore taken out of the middle. A bore that lands on a hole reaches into
    that tile and its four orthogonal neighbours and no further, which is exactly the set
    ``geometry.consumed_holes`` reports the copper gone from: one bore, one answer, in the
    renderer and in DRC.
    """

    x: float
    y: float
    radius: float
    covers: tuple[_Rect, ...]


def _tile_rect(board: Board, col: int, row: int) -> _Rect:
    half = board.pitch / 2
    x, y = _xy(board, HoleCoord(col, row))
    return (x - half, y - half, x + half, y + half)


def _reach(bore_x: float, bore_y: float, angle: float, rects: tuple[_Rect, ...]) -> float:
    """How far a ray from the bore's centre stays inside a union of rectangles.

    Walked as intervals rather than "the furthest rectangle it hits", because a diagonal
    ray out of a cross-shaped patch leaves through the middle tile's corner and must stop
    there -- the arm it would reach next is not connected along that ray.
    """
    dx, dy = math.cos(angle), math.sin(angle)
    spans: list[tuple[float, float]] = []
    for x0, y0, x1, y1 in rects:
        near, far = 0.0, math.inf
        for origin, delta, low, high in ((bore_x, dx, x0, x1), (bore_y, dy, y0, y1)):
            if abs(delta) < 1e-9:
                if not low <= origin <= high:
                    near, far = 1.0, -1.0
                    break
                continue
            first, second = (low - origin) / delta, (high - origin) / delta
            near = max(near, min(first, second))
            far = min(far, max(first, second))
        if far > max(near, 0.0):
            spans.append((max(near, 0.0), far))
    spans.sort()
    reach = 0.0
    for start, end in spans:
        if start <= reach + 1e-9:
            reach = max(reach, end)
    return reach


def _mounting_bores(doc: PerfDocument) -> list[_Bore]:
    board = doc.board
    bores: list[_Bore] = []
    for mount in doc.mounting_holes:
        # mounting_hole_centre_mm, NEVER hole_to_mm(mount.at): the offset is what puts a
        # corner hole in the border, and this view was drawing every one of them back on
        # the grid -- in the middle of four pads that are perfectly intact.
        centre = mounting_hole_centre_mm(mount, board)
        x, y = centre.x, -centre.y
        radius = mount.diameter / 2
        covers = [
            _tile_rect(board, col, row)
            for col in range(board.cols)
            for row in range(board.rows)
            if _overlaps(_tile_rect(board, col, row), x, y, radius)
        ]
        # Whatever of the bore lies OUTSIDE the tiled grid -- a corner hole in the printed
        # border is entirely outside it -- is patched as a rectangle of its own, clipped so
        # it cannot reach a tile the bore never touched. Growing it to whole tiles instead
        # was the first attempt, and it swallowed the neighbouring hole: the pad was still
        # drawn, over solid board, so the board came out with a blind hole beside every
        # screw.
        margin = radius * 0.2
        covers.extend(
            _rect_without(
                (x - radius - margin, y - radius - margin, x + radius + margin, y + radius + margin),
                _tile_grid_rect(board),
            )
        )
        bores.append(_Bore(x=x, y=y, radius=radius, covers=tuple(covers)))
    return bores


def patched_holes(doc: PerfDocument) -> frozenset[str]:
    """Grid positions a mounting bore's patch has taken over, as ``hole_key`` strings.

    The plate has no hole at these -- the patch is solid board from its outer edge to the
    bore -- so nothing may drill one either, or a tube stands in a place with nothing
    around it. A superset of ``geometry.consumed_holes`` and usually the same set: a bore
    that ate a pad necessarily reaches into that tile.
    """
    board = doc.board
    rects = [rect for bore in _mounting_bores(doc) for rect in bore.covers]
    return frozenset(
        hole_key(HoleCoord(col, row))
        for col in range(board.cols)
        for row in range(board.rows)
        for x, y in (_xy(board, HoleCoord(col, row)),)
        if any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in rects)
    )


def _overlaps(rect: _Rect, x: float, y: float, radius: float) -> bool:
    """Whether a circle reaches into a rectangle. The usual nearest-point test."""
    x0, y0, x1, y1 = rect
    near_x = min(max(x, x0), x1)
    near_y = min(max(y, y0), y1)
    return (near_x - x) ** 2 + (near_y - y) ** 2 < radius**2


class _Mesh:
    """Polygons accumulated by hand, for the parts of the plate there is only one of.

    The tiled surface is glyphed and costs nothing per hole; the border, the bore patches
    and the four edges are a handful of polygons each and go into one actor together.
    """

    def __init__(self) -> None:
        self.points = vtk.vtkPoints()
        self.polys = vtk.vtkCellArray()

    def polygon(self, ring: list[tuple[float, float, float]]) -> None:
        first = self.points.GetNumberOfPoints()
        for x, y, z in ring:
            self.points.InsertNextPoint(x, y, z)
        self.polys.InsertNextCell(len(ring))
        for index in range(len(ring)):
            self.polys.InsertCellPoint(first + index)

    def rectangle(self, rect: _Rect, z: float) -> None:
        x0, y0, x1, y1 = rect
        self.polygon([(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)])

    def data(self) -> vtk.vtkPolyData:
        data = vtk.vtkPolyData()
        data.SetPoints(self.points)
        data.SetPolys(self.polys)
        return data


def _patch(mesh: _Mesh, bore: _Bore, z: float) -> None:
    """The plate around one bore: the outline of the tiles it took, minus the bore."""
    corners = [
        (corner_x, corner_y)
        for x0, y0, x1, y1 in bore.covers
        for corner_x, corner_y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    ]
    # The corners of the patch are sample points, or its outline would be cut across and
    # leave a gap against the tiles beside it.
    angles = sorted(
        {math.atan2(cy - bore.y, cx - bore.x) % (2 * math.pi) for cx, cy in corners}
        | {2 * math.pi * index / TILE_SIDES for index in range(TILE_SIDES)}
    )
    outer: list[tuple[float, float]] = []
    inner: list[tuple[float, float]] = []
    for angle in angles:
        reach = max(_reach(bore.x, bore.y, angle, bore.covers), bore.radius)
        dx, dy = math.cos(angle), math.sin(angle)
        outer.append((bore.x + reach * dx, bore.y + reach * dy))
        inner.append((bore.x + bore.radius * dx, bore.y + bore.radius * dy))
    for index in range(len(angles)):
        following = (index + 1) % len(angles)
        mesh.polygon(
            [
                (*outer[index], z),
                (*outer[following], z),
                (*inner[following], z),
                (*inner[index], z),
            ]
        )


#: Facets round a hole punched in the board itself, and round the bore's wall. A multiple
#: of four, and the tile is sampled from a corner, so all four corners of a tile are
#: vertices -- a contour that cut them off would leave a pinhole in the board at the
#: corner of every tile.
TILE_SIDES = 24

#: How much wider the hole's wall is than the hole punched in the surface. The wall then
#: sits just BEHIND the rim rather than exactly on it, so no sliver of background can show
#: through the seam between the two.
WALL_OVERSIZE_MM = 0.01


def _tile_with_hole(board: Board) -> vtk.vtkPolyData:
    """One pitch square of substrate with its hole taken out of the middle.

    Tiles are exactly a pitch across, so neighbours share an edge exactly and the tiled
    surface is watertight.
    """
    half = board.pitch / 2
    radius = board.drill_diameter / 2
    angles = [math.pi / 4 + 2 * math.pi * index / TILE_SIDES for index in range(TILE_SIDES)]
    outer: list[tuple[float, float]] = []
    inner: list[tuple[float, float]] = []
    for angle in angles:
        dx, dy = math.cos(angle), math.sin(angle)
        reach = half / max(abs(dx), abs(dy))
        outer.append((reach * dx, reach * dy))
        inner.append((radius * math.cos(angle), radius * math.sin(angle)))
    return _annulus(outer, inner)


def _solid_tile(board: Board) -> vtk.vtkPolyData:
    """The same square with nothing taken out, for a position that was never drilled: an
    edge-connector finger is a solid contact on solid board."""
    half = board.pitch / 2
    mesh = _Mesh()
    mesh.rectangle((-half, -half, half, half), 0.0)
    return mesh.data()


def _glyphed(points: vtk.vtkPoints, source: vtk.vtkPolyData) -> vtk.vtkActor:
    data = vtk.vtkPolyData()
    data.SetPoints(points)
    glyph = vtk.vtkGlyph3DMapper()
    glyph.SetInputData(data)
    glyph.SetSourceData(source)
    glyph.SetOrient(False)
    glyph.SetScaling(False)
    actor = vtk.vtkActor()
    actor.SetMapper(glyph)
    return actor


def build_substrate(doc: PerfDocument) -> list[vtk.vtkActor]:
    """The board itself, WITH ITS HOLES IN IT.

    It used to be one solid cube, and every hole on it was faked by laying a dark cylinder
    over the top. A dark disc on green reads as a mark printed on the board rather than as
    something you can push a lead through -- and on a mounting bore, which has no pad ring
    around it to explain the darkness, it read as a sticker. The fake was chosen because a
    boolean subtraction per hole is thousands of them on a real board, which remains true.
    This is neither: the face is ONE TILE, a pitch square with its hole taken out, glyphed
    at every hole. Both faces come out of the same glyph, so a 945-hole board costs one
    source and two actors rather than 1890 subtractions.

    Three actors: the drilled tiles, the few undrilled ones, and one mesh holding the
    printed border, the patch around each mounting bore and the four edges.
    """
    board = doc.board
    top, bottom = 0.0, -board.thickness
    bores = _mounting_bores(doc)
    patched = [rect for bore in bores for rect in bore.covers]
    undrilled = undrilled_holes(doc)

    drilled_at = vtk.vtkPoints()
    solid_at = vtk.vtkPoints()
    for col in range(board.cols):
        for row in range(board.rows):
            x, y = _xy(board, HoleCoord(col, row))
            if any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in patched):
                continue  # A bore took this tile; its patch covers the ground instead.
            target = solid_at if hole_key(HoleCoord(col, row)) in undrilled else drilled_at
            for z in (top, bottom):
                target.InsertNextPoint(x, y, z)

    mesh = _Mesh()
    for z in (top, bottom):
        for rect in _rects_without(_border_rects(board), patched):
            mesh.rectangle(rect, z)
        for bore in bores:
            _patch(mesh, bore, z)
    x0, y0, x1, y1 = board_outline_rect(board)
    for (ax, ay), (bx, by) in (
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    ):
        mesh.polygon([(ax, ay, top), (bx, by, top), (bx, by, bottom), (ax, ay, bottom)])

    rgb = scheme_for(board.material).rgb
    actors: list[vtk.vtkActor] = []
    for points, source in (
        (drilled_at, _tile_with_hole(board)),
        (solid_at, _solid_tile(board)),
    ):
        if points.GetNumberOfPoints() == 0:
            continue
        actors.append(_glyphed(points, source))
    edges = vtk.vtkActor()
    edge_mapper = vtk.vtkPolyDataMapper()
    edge_mapper.SetInputData(mesh.data())
    edges.SetMapper(edge_mapper)
    actors.append(edges)
    for actor in actors:
        actor.GetProperty().SetColor(*rgb)
    return actors


#: How far each face's copper stands off the substrate: enough that a flat pad never
#: z-fights the board it lies on, small enough to be invisible.
PAD_LIFT_MM = 0.05

#: How far the dark bore stops SHORT of each face's copper.
#:
#: It used to overshoot by 0.10 mm instead, and a hole that stands proud of its own pad is
#: not a hole. At any grazing angle -- which is most of them, since this view is orbited --
#: every bore showed a black cap standing above the copper and occluding the pads on the
#: rows behind it, so a board read as a grid of black buttons rather than as one with
#: holes drilled through it. Below the copper on both faces, the ring is the topmost thing
#: at every hole, which is what makes it read as a hole.
BORE_UNDER_PAD_MM = 0.015

#: How far a trimmed lead stands proud of the solder-side copper. Enough to see that
#: something came through the hole, not enough to look like a board nobody has cut the
#: legs off yet.
LEAD_TRIM_MM = 0.07


#: How tight the highlight on the board's own copper is.
#:
#: VTK's DEFAULT IS 1.0, which is not a highlight at all: it adds the specular term flat
#: across the whole surface, so a pad carrying 0.4 of it rendered a fifth brighter than its
#: own colour and CLIPPED -- a pad measured (255, 255, 125) against the very same
#: `#c8a951` the 2D view paints from the same table. Clipping does not merely shift the
#: hue, it flattens the shading off the copper, which is why the 3D board came out a grid
#: of flat yellow rings while the 2D one looked like metal. Measured, not chosen: at this
#: power the pad renders within a few levels of the 2D view's.
COPPER_SPECULAR_POWER = 30.0


def pad_z(board: Board, side: BoardSide) -> float:
    """Where one face's copper sits, in board z. Pure, so it can be asserted directly."""
    return PAD_LIFT_MM if side == "top" else -board.thickness - PAD_LIFT_MM


def bore_span_z(board: Board) -> tuple[float, float]:
    """The dark bore's top and bottom, in board z. Pure, for the reason ``pad_z`` is.

    Between the two faces' copper and never past it, while still standing clear of the
    substrate's own faces: the bore has to be visible through the pad's hole from either
    side without standing above the metal around it.
    """
    return (
        pad_z(board, "top") - BORE_UNDER_PAD_MM,
        pad_z(board, "bottom") + BORE_UNDER_PAD_MM,
    )


#: What a conductor measures on the board, in mm.
#:
#: A SOLDER RUN is two numbers, because it is not a uniform thing: a mound on each pad it
#: is soldered to, and a narrower bridge of solder between them. That narrowing is not
#: decoration -- it is what makes the joints countable, and counting joints along a run
#: against the real board is exactly what somebody following the build guide does.
#: 1.2 mm across a joint sits inside a 1.9 mm pad and leaves the copper ring showing;
#: 0.72 mm across the bridge still spans the 0.64 mm gap to the next pad, which is what a
#: run is for.
#:
#: The tube was 0.34 mm at a constant radius with a 1.9x SPHERE dropped on every pad. Under
#: half a real run, thinner than its own joints, and two primitives meeting in a hard
#: crease all the way round -- so it read as balls threaded on a stick, a molecular model
#: rather than a length of solder. It is one varying-radius surface now; see
#: ``_trace_swell``.
TRACE_JOINT_RADIUS_MM = 0.60
TRACE_WAIST_RATIO = 0.60

#: 24 AWG hookup wire over the sleeve, and tinned copper for a bare link or a spine.
BARE_WIRE_RADIUS_MM = 0.30
INSULATED_RADIUS_MM = 0.55

#: A wire's two ends get a solder fillet and its length gets none, which is the distinction
#: ``contacts_every_path_hole`` draws and the single most important thing this view says.
#: Modest, because a joint on the end of a wire is a fillet around it, not a bead on it.
BEAD_RATIO_WIRE = 1.20

#: Facets round a drilled hole and round a pad's outline. Twelve made every hole on the
#: board a visible dodecagon as soon as anyone looked closely -- and a perfboard is mostly
#: holes, so that one number set how machine-made the whole thing looked. Both are ONE
#: glyphed source instanced at every hole, so the cost is per board and not per hole.
BORE_SIDES = 28
PAD_SEGMENTS = 40

#: Facets round a tube and round a fillet. Ten and twelve were enough at the whole-board
#: zoom the default camera gives and visibly polygonal as soon as anyone looked closely at
#: a joint -- which is the thing they are most likely to want to look closely at.
TUBE_SIDES = 20
BEAD_RESOLUTION = 20

#: How far one stacking level lifts a conductor clear of the one it crosses.
#:
#: DERIVED FROM THE RADII ABOVE, not chosen: the worst pair that can actually cross is an
#: insulated wire over a solder run, and their tubes stop overlapping when their centres
#: are the sum of the two radii apart. Anything less does not fix the thing stacking
#: exists for -- which is what the 0.08 mm this used to be did not, being a tenth of what
#: two tubes needed, while the offset still accumulated: the level was a running index
#: over every conductor on the board, putting the last one on the dense fixture 4.47 mm
#: off a board 1.6 mm thick. It bought levitation and no clearance.
#: ``occupancy.stacking_layers`` now lifts only what actually crosses something, which is
#: what makes a step this size affordable.
STACK_STEP_MM = INSULATED_RADIUS_MM + TRACE_JOINT_RADIUS_MM + 0.15


def conductor_radius(cond: Conductor) -> float:
    """The tube one conductor is drawn as. Read by ``build_conductor`` and by
    :func:`conductor_z`, which needs it to rest the tube ON the copper rather than near
    it."""
    if contacts_every_path_hole(cond):
        return TRACE_JOINT_RADIUS_MM  # the widest it gets, which is what has to clear
    if cond.kind in ("insulated-wire", "top-jumper"):
        return INSULATED_RADIUS_MM
    return BARE_WIRE_RADIUS_MM


def conductor_z(cond: Conductor, board: Board, stack: int = 0) -> float:
    """Where one conductor sits, in board z.

    Split out from ``build_conductor`` because the interesting property is arithmetic and
    testing it through VTK means reaching into an unexecuted pipeline, which segfaults.

    RESTING ON THE COPPER, not near it. The height used to be a constant 0.5 mm clear of
    the substrate, which is 0.45 from the pad surface -- and with a 0.34 mm trace radius
    that leaves 0.11 mm of daylight between a solder run and the pad it is supposedly
    soldered to. Small, and visible as a shadow line under every run: it read as floating,
    which is the one thing solder does not do. Taking the radius off the PAD plane makes
    the tube tangent to the copper by construction, for a wire lying on the board as much
    as for a run fused to it, and takes a magic number out of the file.

    The beads at each joint are deliberately larger than that and so reach into the
    substrate. That is correct: a fillet wicks into the hole, the board is opaque, and
    what shows on the surface is the dome.

    ``stack`` is the conductor's level from ``occupancy.stacking_layers`` -- how many
    conductors it has to pass over. Everything on the solder side used to sit at one z per
    ``layer_z``, so two crossing bare wires were drawn INTERSECTING: occupying the same
    space, which is not a thing wire does and looked like a modelling error because it was
    one. See :data:`STACK_STEP_MM` for why the first attempt at fixing that did not.
    """
    # ``stack`` is the WHOLE answer, from ``occupancy.stacking_layers``, which already has
    # the document's own ``layer_z`` as its floor. Adding layer_z again here is what put
    # conductors the stacker had deliberately separated back at one height -- one at
    # layer_z 1 and stack 0, the other at layer_z 0 and stack 1 -- and drew them straight
    # through each other on four of the fifteen golden fixtures.
    lift = STACK_STEP_MM * stack
    # A RUN IS IN THE SURFACE; A WIRE IS ON IT. Solder wets the copper and stands as a
    # half-round ridge over it, so a run's centreline is the pad plane itself and only its
    # outer half shows -- which is also why a joint swells concentrically out of it instead
    # of hanging off its back. A wire lies on top of the board and touches it along one
    # line, so its centreline is a radius clear.
    #
    # The distinction is the one this view exists to make (PLAN.md Sec 8.3), and it is now
    # in the geometry rather than only in the colour.
    standoff = 0.0 if contacts_every_path_hole(cond) else conductor_radius(cond)
    if cond.side == "bottom":
        return pad_z(board, "bottom") - standoff - lift
    return pad_z(board, "top") + standoff + lift


def _stadium_contour(extent_x: float, extent_y: float, count: int) -> list[tuple[float, float]]:
    """Points around a stadium: a rectangle capped with a semicircle at each end.

    That is what an oblong pad is -- not an ellipse, which is what scaling a disc would
    give and what the copper visibly is not. A round pad falls out of the same code with
    a zero-length straight section, so there is one contour routine rather than two.
    """
    radius = min(extent_x, extent_y) / 2
    half = (max(extent_x, extent_y) - min(extent_x, extent_y)) / 2
    vertical = extent_y >= extent_x
    per_arc = max(3, count // 2)
    points: list[tuple[float, float]] = []
    for cap in (1.0, -1.0):
        for i in range(per_arc):
            # Each arc runs a half turn; the straight sides are the polygon edges between
            # the end of one arc and the start of the next, so they need no points of
            # their own.
            angle = math.pi * i / (per_arc - 1)
            dx = radius * math.cos(angle) * cap
            dy = radius * math.sin(angle) * cap
            if vertical:
                points.append((dx, dy + cap * half))
            else:
                points.append((dy + cap * half, dx))
    return points


def _pad_annulus(board: Board) -> vtk.vtkPolyData:
    """One pad as a flat ring: a stadium (or circle) outline with the drill punched out.

    Built by hand rather than with ``vtkDiskSource`` because that source only makes
    circles, and an oblong pad is the shape the board actually has. Still ONE source,
    glyphed at every hole, so the instanced-rendering claim this view exists to prove is
    unaffected.
    """
    extent_x, extent_y = pad_extent_mm(board)
    outer = _stadium_contour(extent_x, extent_y, PAD_SEGMENTS)
    n = len(outer)
    drill_r = board.drill_diameter / 2
    inner = [
        (drill_r * math.cos(2 * math.pi * i / n), drill_r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]
    return _annulus(outer, inner)


def _annulus(
    outer: list[tuple[float, float]], inner: list[tuple[float, float]]
) -> vtk.vtkPolyData:
    """A flat ring between two closed contours, paired point by point.

    Every punched surface here is one of these -- a pad, a tile of substrate, the patch of
    board around a mounting bore -- so the winding and the pairing are decided once rather
    than three times. The two contours need the same number of points and nothing else:
    the pad's outer contour is a stadium walked by arc and its inner one a circle walked by
    angle, and the quads between them are none the worse for it.
    """
    n = len(outer)
    points = vtk.vtkPoints()
    for x, y in (*outer, *inner):
        points.InsertNextPoint(x, y, 0.0)
    polys = vtk.vtkCellArray()
    for i in range(n):
        j = (i + 1) % n
        polys.InsertNextCell(4)
        for index in (i, j, n + j, n + i):
            polys.InsertCellPoint(index)
    data = vtk.vtkPolyData()
    data.SetPoints(points)
    data.SetPolys(polys)
    return data


def _hole_wall(board: Board, radius: float) -> vtk.vtkPolyData:
    """The inside of one hole: a tube through the board, open at both ends.

    OPEN is the point. A capped cylinder is a plug -- it was the plug this view used to
    fake every hole with, and you cannot see through a plug. With the surface punched
    (see :func:`build_substrate`) this is the wall the drill left, and a hole shows what
    is behind the board, which is what a hole does.
    """
    wall = vtk.vtkCylinderSource()
    wall.SetRadius(radius + WALL_OVERSIZE_MM)
    wall.SetHeight(board.thickness)
    wall.SetResolution(BORE_SIDES)
    wall.CappingOff()
    wall.Update()
    upright = vtk.vtkTransform()
    upright.RotateX(90)  # vtkCylinderSource stands along Y; thickness is along Z.
    turn = vtk.vtkTransformPolyDataFilter()
    turn.SetTransform(upright)
    turn.SetInputData(wall.GetOutput())
    turn.Update()
    return turn.GetOutput()


def build_drills(board: Board, consumed: frozenset[str] = frozenset()) -> vtk.vtkActor:
    """Every hole's wall, in one instanced actor.

    THE BOARD HAD NO HOLES FROM UNDERNEATH before any of this: the substrate was one cube
    and the pads sat only on top, so turning the board over showed a blank green slab, on
    the very view whose job is to check the solder side. The first answer was a dark
    cylinder laid over the surface, which is what this replaces.
    """
    points = vtk.vtkPoints()
    for col in range(board.cols):
        for row in range(board.rows):
            # A mounting bore is a bigger hole in the same place, walled by
            # `build_mounting_holes`. Leaving this one in as well puts a 1 mm tube inside
            # a 3.2 mm one, which z-fights along its whole length.
            if consumed and hole_key(HoleCoord(col, row)) in consumed:
                continue
            x, y = _xy(board, HoleCoord(col, row))
            points.InsertNextPoint(x, y, -board.thickness / 2)

    actor = _glyphed(points, _hole_wall(board, board.drill_diameter / 2))
    prop = actor.GetProperty()
    # The cut edge of the laminate, in shadow: darker than the face, and the same hue --
    # a hole in a brown phenolic board is not the same colour as one in green FR-4.
    prop.SetColor(*(channel * 0.55 for channel in scheme_for(board.material).rgb))
    prop.SetSpecular(0.0)
    prop.SetAmbient(0.15)
    return actor


def build_pads(
    board: Board, side: BoardSide = "top", consumed: frozenset[str] = frozenset()
) -> vtk.vtkActor:
    """Every pad on one face, in one instanced actor. The scalability claim, tested.

    Called for BOTH faces. The boards this is modelled on are plated through-hole with an
    annular ring on each side, which is also why the solder side is somewhere you can
    solder at all -- and until now the underside had no copper on it whatsoever.

    An annulus, not a disc: a pad with no hole in it makes the board read as a dotted
    sheet rather than as perfboard, and the hole is the entire point of the part.
    """
    z = pad_z(board, side)
    points = vtk.vtkPoints()
    for col in range(board.cols):
        for row in range(board.rows):
            if consumed and hole_key(HoleCoord(col, row)) in consumed:
                continue  # A mounting bore took this pad's copper away.
            x, y = _xy(board, HoleCoord(col, row))
            points.InsertNextPoint(x, y, z)
    data = vtk.vtkPolyData()
    data.SetPoints(points)

    glyph = vtk.vtkGlyph3DMapper()
    glyph.SetInputData(data)
    glyph.SetSourceData(_pad_annulus(board))
    glyph.SetOrient(False)
    glyph.SetScaling(False)

    actor = vtk.vtkActor()
    actor.SetMapper(glyph)
    actor.GetProperty().SetColor(*scheme_for(board.material).pad_rgb)
    actor.GetProperty().SetSpecular(0.4)
    actor.GetProperty().SetSpecularPower(COPPER_SPECULAR_POWER)
    return actor


def build_strips(doc: PerfDocument) -> list[vtk.vtkActor]:
    """The copper a stripboard came with, as one bar per uncut run.

    On the solder side only, because that is the only side it is on. Without it the 3D
    view of a stripboard shows a grid of separate pads -- which is a picture of a
    different board, and this view exists to be checked against the real one.

    One thin box per segment rather than per hole: a 30 x 20 board has 20 of them against
    600 pads, and the strip has to read as one continuous piece of copper anyway.
    """
    runs = segments(doc)
    if not runs:
        return []
    board = doc.board
    extent_x, extent_y = pad_extent_mm(board)
    z = pad_z(board, "bottom")
    actors: list[vtk.vtkActor] = []
    for run in runs:
        first_x, first_y = _xy(board, run.holes[0])
        last_x, last_y = _xy(board, run.holes[-1])
        bar = vtk.vtkCubeSource()
        bar.SetXLength(abs(last_x - first_x) + extent_x)
        bar.SetYLength(abs(last_y - first_y) + extent_y)
        # Thin enough to read as foil rather than as a rail standing off the board, and
        # thick enough that the renderer does not fight the substrate for the same plane.
        bar.SetZLength(0.06)
        actor = vtk.vtkActor()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(bar.GetOutputPort())
        actor.SetMapper(mapper)
        actor.SetPosition((first_x + last_x) / 2, (first_y + last_y) / 2, z)
        actor.GetProperty().SetColor(*scheme_for(board.material).pad_rgb)
        actor.GetProperty().SetSpecular(0.4)
        actor.GetProperty().SetSpecularPower(COPPER_SPECULAR_POWER)
        actors.append(actor)
    return actors


def build_mounting_holes(doc: PerfDocument) -> list[vtk.vtkActor]:
    """The screw bores' walls. The plate around them is punched by :func:`build_substrate`.

    A mounting hole is where the old fake was worst. Every other hole had a copper ring
    round it, which explained the darkness in the middle; a bore has its copper taken away,
    so a dark disc lying on bare green read as a sticker on the board rather than a hole
    through it. It is punched now, like every other hole and by the same machinery, which
    is also what stops the two reading as different kinds of thing.
    """
    board = doc.board
    if not doc.mounting_holes:
        return []
    actors: list[vtk.vtkActor] = []
    for bore in _mounting_bores(doc):
        points = vtk.vtkPoints()
        points.InsertNextPoint(bore.x, bore.y, -board.thickness / 2)
        actor = _glyphed(points, _hole_wall(board, bore.radius))
        prop = actor.GetProperty()
        prop.SetColor(*(channel * 0.55 for channel in scheme_for(board.material).rgb))
        prop.SetSpecular(0.0)
        prop.SetAmbient(0.15)
        actors.append(actor)
    return actors


def build_edge_connectors(doc: PerfDocument) -> list[vtk.vtkActor]:
    """Connector fingers, as thin copper plates lying on the board's faces."""
    board = doc.board
    actors: list[vtk.vtkActor] = []
    for connector in doc.edge_connectors:
        faces: tuple[BoardSide, ...] = (
            ("top", "bottom") if connector.face == "both" else (connector.face,)
        )
        for face in faces:
            if board.single_sided and face == "top":
                continue  # No copper on the component side at all -- see Board.single_sided.
            append = vtk.vtkAppendPolyData()
            # Only the fingers a bore has not drilled through -- see
            # geometry.surviving_finger_holes, which view2d asks the same question of.
            for hole in surviving_finger_holes(doc, connector):
                rect = edge_finger_rect(connector, hole, board)
                plate = vtk.vtkCubeSource()
                plate.SetXLength(rect.width)
                plate.SetYLength(rect.height)
                plate.SetZLength(0.05)
                # Board y runs downward while 3D y runs up, so the rect's y interval is
                # negated -- the same flip `_xy` applies to every hole in this file.
                plate.SetCenter(
                    rect.x + rect.width / 2,
                    -(rect.y + rect.height / 2),
                    pad_z(board, face),
                )
                plate.Update()
                append.AddInputData(plate.GetOutput())
            if append.GetNumberOfInputConnections(0) == 0:
                continue
            append.Update()
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(append.GetOutputPort())
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*scheme_for(board.material).pad_rgb)
            actor.GetProperty().SetSpecular(0.4)
            actor.GetProperty().SetSpecularPower(COPPER_SPECULAR_POWER)
            actors.append(actor)
    return actors


def build_legend(doc: PerfDocument) -> list[vtk.vtkActor]:
    """The addresses printed on the substrate, as flat text on the board's faces.

    Every label of one face goes into ONE actor. A vtkVectorText per label would be a
    hundred actors on a modest board and several hundred on a real one, which is the same
    mistake the pad grid exists to avoid -- and a legend is not worth more actors than
    the copper.
    """
    labels = doc.board.labels
    if labels is None:
        return []
    board = doc.board
    # The strip of bare substrate between the outermost pads and the board edge -- NOT the
    # whole margin, which the pads eat half their extent of. Same reasoning, and the same
    # arithmetic, as view2d.BoardLegendItem._free_strip_mm: the two views have to print the
    # legend in the same place or the 3D board stops matching the one being edited.
    # Asked of the DOCUMENT, not the board: an edge carrying connector fingers has only
    # whatever inset those fingers left, and the 2D legend measures it the same way.
    strip_x = legend_strip_mm(doc, "horizontal")
    strip_y = legend_strip_mm(doc, "vertical")
    margin_x = board_edge_margin_mm(board, "horizontal")
    margin_y = board_edge_margin_mm(board, "vertical")
    height = min(1.15, min(strip_x, strip_y) * 0.6)
    faces: tuple[BoardSide, ...] = (
        ("top", "bottom") if labels.face == "both" else (labels.face,)
    )

    actors: list[vtk.vtkActor] = []
    for face in faces:
        append = vtk.vtkAppendPolyData()
        # (text, x, y, widest it may be) -- the width limits differ between the two runs
        # because their free axes are swapped, exactly as in the 2D legend.
        # 3D y runs up where board rows run down, so the top border is at POSITIVE y here.
        #
        # Measured IN FROM THE BOARD EDGE, exactly as view2d does and for the same reason:
        # out from the pad puts the column letters on top of the connector fingers, which
        # reach most of the way to the edge on the board this application opens on.
        span_w = (board.cols - 1) * board.pitch
        span_h = (board.rows - 1) * board.pitch
        column_ys = [margin_y - strip_y / 2]
        row_xs = [-(margin_x - strip_x / 2)]
        if labels.all_edges:
            column_ys.append(-span_h - margin_y + strip_y / 2)
            row_xs.append(span_w + margin_x - strip_x / 2)
        # Row numbers are turned on their side, as they are on the real boards and in the
        # 2D view: the strip beside a row is narrow across and a whole pitch deep, so a
        # turned number fits where an upright one has to shrink.
        entries: list[tuple[str, float, float, float, float]] = [
            (column_label(col), col * board.pitch, y, board.pitch * 0.9, 0.0)
            for col in range(board.cols)
            for y in column_ys
        ]
        entries += [
            (printed_row_label(row, labels), x, -row * board.pitch, board.pitch * 0.9, 90.0)
            for row in range(board.rows)
            for x in row_xs
        ]
        for text, x, y, max_width, rotate in entries:
            # Not printed where a bore was drilled -- the same question view2d asks, in
            # the same units: board millimetres with y growing DOWN, so 3D's y is negated
            # on the way in. A turned number's box is turned with it.
            box = (height, max_width) if rotate else (max_width, height)
            if not printed_label_is_clear(doc, Point2(x, -y), box[0], box[1]):
                continue
            vector = vtk.vtkVectorText()
            vector.SetText(text)
            vector.Update()
            bounds = vector.GetOutput().GetBounds()
            # vtkVectorText is roughly one unit tall and starts at the origin, so it is
            # scaled to the wanted cap height and then centred on its own bounds.
            scale = height
            text_width = max(bounds[1] - bounds[0], 1e-6)
            if text_width * scale > max_width:
                scale = max_width / text_width
            transform = vtk.vtkTransform()
            transform.Translate(x, y, 0.0)
            if rotate:
                transform.RotateZ(rotate)
            transform.Scale(scale, scale, scale)
            transform.Translate(
                -(bounds[0] + bounds[1]) / 2, -(bounds[2] + bounds[3]) / 2, 0.0
            )
            placed = vtk.vtkTransformPolyDataFilter()
            placed.SetTransform(transform)
            placed.SetInputData(vector.GetOutput())
            placed.Update()
            append.AddInputData(placed.GetOutput())
        append.Update()

        printed = append.GetOutputPort()
        if face == "bottom":
            # Ink on the underside, being looked at from underneath. Both the glyphs and
            # their positions have to reflect, or turning the board over in 3D shows the
            # legend written backwards -- and this is the one view where the text is a
            # physical object being seen directly rather than an annotation drawn over a
            # picture. (view2d deliberately does the opposite for the face it is seeing
            # THROUGH the board: reversed 1 mm text is noise, and what the reader wants
            # off a label there is the address, not the reflection.)
            #
            # Reflected about the HOLE SPAN, the axis view2d.hole_to_screen mirrors about,
            # so both views agree which hole a label belongs to.
            flip = vtk.vtkTransform()
            flip.Translate(span_w, 0.0, 0.0)
            flip.Scale(-1.0, 1.0, 1.0)
            mirrored = vtk.vtkTransformPolyDataFilter()
            mirrored.SetTransform(flip)
            mirrored.SetInputConnection(append.GetOutputPort())
            mirrored.Update()
            printed = mirrored.GetOutputPort()

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(printed)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        # Just clear of the substrate, on whichever face carries the print. Silkscreen is
        # ink: unlit and matte, so it does not catch highlights the way copper does.
        actor.SetPosition(0.0, 0.0, 0.02 if face == "top" else -board.thickness - 0.02)
        actor.GetProperty().SetColor(*LEGEND_RGB)
        actor.GetProperty().SetAmbient(0.6)
        actor.GetProperty().SetSpecular(0.0)
        actors.append(actor)
    return actors


# ------------------------------------------------------- parametric component bodies
#
# One solid per archetype, built from the dimensions already in the footprint registry
# (PLAN.md D6: parametric generation, no mesh library, no share-alike asset licence).
# Previously every part was one grey cube sized from its COURTYARD -- which is padded well
# beyond the physical part -- so a resistor, a DIP and a 10 mm electrolytic were the same
# oversized block. See ui/bodies.py for where the real dimensions come from.


@dataclass(frozen=True, slots=True)
class _Piece:
    """One solid of one component. Positioned by the actor, not baked into the source.

    VTK applies an actor's scale, then its orientation, then its position, so every source
    below is built centred on the origin and placed afterwards. That is what lets a single
    cylinder source serve as an upright can, a lying resistor body and -- squashed by a
    non-uniform scale -- an oval crystal can.
    """

    source: Any
    rgb: tuple[float, float, float]
    position: tuple[float, float, float]
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    #: Euler angles in degrees, as VTK's actor orientation.
    orientation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    specular: float = 0.25
    specular_power: float = 20.0
    opacity: float = 1.0
    #: When set, the source is glyphed at each of these world positions in ONE actor and
    #: ``position`` is ignored. For repeated identical solids -- a header's pins -- where an
    #: actor each would cost more than the whole instanced pad grid.
    instances: tuple[tuple[float, float, float], ...] = ()


#: vtkCylinderSource points along +Y. These turn it along each world axis.
_ALONG_X = (0.0, 0.0, 90.0)
_ALONG_Y = (0.0, 0.0, 0.0)
_ALONG_Z = (90.0, 0.0, 0.0)


def _upright_scale(scale: tuple[float, float, float]) -> tuple[float, float, float]:
    """A world scale rewritten for a cylinder stood upright by ``_ALONG_Z``.

    VTK scales BEFORE it orients, and that turn maps the source's own z onto world y -- so
    a scale written to flatten the world y of an upright can flattens its LENGTH instead.
    The crystal came out a quarter too short with its domed cap floating in the air above
    it, which is what measuring the actors' bounds says and what squinting at the render
    did not.
    """
    return (scale[0], 1.0, scale[1])

#: Component bodies sit this far above the board so they never z-fight with the pads.
_LIFT = 0.12


def _box(x: float, y: float, z: float) -> Any:
    cube = vtk.vtkCubeSource()
    cube.SetXLength(x)
    cube.SetYLength(y)
    cube.SetZLength(z)
    cube.SetCenter(0.0, 0.0, 0.0)
    return cube


def _cylinder(radius: float, height: float, resolution: int = 24) -> Any:
    cyl = vtk.vtkCylinderSource()
    cyl.SetRadius(radius)
    cyl.SetHeight(height)
    cyl.SetResolution(resolution)
    cyl.SetCenter(0.0, 0.0, 0.0)
    cyl.CappingOn()
    return cyl


def _upright_cylinder(radius: float, height: float, resolution: int = 12) -> Any:
    """A cylinder standing along Z, ready to be glyphed at every pin of one component.

    The turn is baked into the SOURCE rather than set on the actor because the instanced
    path in ``_actor_for`` has no actor to turn -- one glyph mapper draws every copy, and
    ``SetOrient(False)`` means each copy arrives exactly as the source was built.

    ``SetInputData`` and not ``SetInputConnection``: a connection keeps a RAW pointer back
    to the algorithm that produced it, and the cylinder here is a local that dies with
    this function. Connecting one segfaults the interpreter outright -- measured, not
    feared. Handing over the computed polydata takes a real reference to it and leaves no
    producer to outlive.
    """
    cylinder = _cylinder(radius, height, resolution)
    cylinder.Update()
    upright = vtk.vtkTransform()
    upright.RotateX(90)
    turn = vtk.vtkTransformPolyDataFilter()
    turn.SetTransform(upright)
    turn.SetInputData(cylinder.GetOutput())
    turn.Update()
    return turn


def _d_prism(radius: float, flat: float, height: float, resolution: int = 22) -> vtk.vtkPolyData:
    """A cylinder with ONE side flattened, standing along Z: the shape of a TO-92.

    The flat is not decoration -- it is the only thing on the package that says which way
    round the three legs go, and squashing a cylinder to fake it gives an ellipse, which
    has two flats and marks nothing. Built as a profile rather than as a source because
    VTK has no source for it.
    """
    limit = math.asin(min(max(flat / radius, -1.0), 1.0))
    span = math.pi + 2 * limit
    profile = [
        (
            radius * math.cos(math.pi - limit + span * index / (resolution - 1)),
            radius * math.sin(math.pi - limit + span * index / (resolution - 1)),
        )
        for index in range(resolution)
    ]
    mesh = _Mesh()
    top, bottom = height / 2, -height / 2
    mesh.polygon([(x, y, top) for x, y in profile])
    mesh.polygon([(x, y, bottom) for x, y in reversed(profile)])
    for index in range(len(profile)):
        (ax, ay), (bx, by) = profile[index], profile[(index + 1) % len(profile)]
        mesh.polygon([(ax, ay, bottom), (bx, by, bottom), (bx, by, top), (ax, ay, top)])
    return mesh.data()


def _sphere(radius: float, resolution: int = 20) -> Any:
    sphere = vtk.vtkSphereSource()
    sphere.SetRadius(radius)
    sphere.SetThetaResolution(resolution)
    sphere.SetPhiResolution(resolution)
    sphere.SetCenter(0.0, 0.0, 0.0)
    return sphere


@dataclass(frozen=True, slots=True)
class _WorldBody:
    """A component's body in 3D world space, with its transform already applied."""

    x: float
    y: float
    size_x: float
    size_y: float
    height: float
    axis: str
    style: BodyStyle
    #: The board's own thickness. A body knows where its pins are; without this it does
    #: not know how deep their holes go, and a lead cannot be drawn through one.
    thickness: float
    #: World positions of every pin, and of the polarity pin if the part has one.
    pins: tuple[tuple[float, float], ...]
    polarity: tuple[float, float] | None
    #: The printed colour code, for a resistor whose value could be decoded. Empty for
    #: everything else -- see ``bodies.resistor_bands``, which refuses to guess.
    bands: tuple[str, ...] = ()

    @property
    def along(self) -> float:
        return self.size_x if self.axis == "x" else self.size_y

    @property
    def across(self) -> float:
        return self.size_y if self.axis == "x" else self.size_x

    @property
    def lead_bottom_z(self) -> float:
        """Where a lead ends: just past the solder-side copper, as a trimmed one does.

        Not at the copper: an end coplanar with the pad would z-fight it, and a lead you
        cannot see from underneath is one the solder side has no evidence of.
        """
        return -self.thickness - PAD_LIFT_MM - LEAD_TRIM_MM

    @property
    def surface(self) -> Surface:
        """How this part's material catches light. One table, two renderers: the 2D view
        derives its highlight from the same call."""
        return surface_for(self.style)


def _world_body(lookup: FootprintLookup, comp: Any, board: Board) -> _WorldBody | None:
    fp = lookup(comp.footprint_id)
    if fp is None:
        return None
    placement = placement_for(fp, board.pitch)

    def to_world(local_x: float, local_y: float) -> tuple[float, float]:
        tx, ty = transform_offset(local_x, local_y, comp.rotation, comp.mirrored)
        return (
            comp.anchor.col * board.pitch + tx,
            -(comp.anchor.row * board.pitch + ty),
        )

    x, y = to_world(placement.centre_x, placement.centre_y)
    # A quarter turn swaps which world axis each extent lies along; the extents themselves
    # are unchanged, and a sign flip from mirroring cannot affect a length.
    swapped = comp.rotation in (90, 270)
    size_x = placement.size_y if swapped else placement.size_x
    size_y = placement.size_x if swapped else placement.size_y
    axis = placement.axis
    if swapped:
        axis = "y" if axis == "x" else "x"

    polarity_local = polarity_pin_offset(fp, board.pitch)
    return _WorldBody(
        x=x,
        y=y,
        size_x=size_x,
        size_y=size_y,
        height=placement.height,
        axis=axis,
        style=style_for(fp),
        thickness=board.thickness,
        pins=tuple(
            (hole.col * board.pitch, -hole.row * board.pitch)
            for _pin, hole in all_pin_holes(comp, fp)
        ),
        polarity=to_world(*polarity_local) if polarity_local is not None else None,
        # From the document's own value, so the bands cannot disagree with the netlist.
        bands=resistor_bands(fp, comp.value) or (),
    )


def _through_hole_pieces(
    body: _WorldBody,
    top_z: float,
    radius: float = 0.28,
    blade: tuple[float, float] | None = None,
) -> list[_Piece]:
    """The part of every lead that goes down its hole, in ONE instanced actor.

    THE LEADS USED TO STOP IN MID-AIR. A resistor's wire ran horizontally to the pin
    position and ended there, a hand's breadth above the board at this scale, and a DIP
    had no pins at all -- so every part hovered over the holes it is supposed to be
    soldered into, which is the one thing this view exists to show. The lead now turns
    down at the pin, disappears into the hole (the bore is opaque, as a board is) and
    reappears trimmed on the solder side.

    Instanced rather than an actor per pin, for the reason the pad grid is: a 2x20 header
    has forty pins, and forty actors for one connector would cost more than every pad on
    the board.
    """
    bottom = body.lead_bottom_z
    height = top_z - bottom
    if height <= 0 or not body.pins:
        return []
    # ``blade`` is a flat pin rather than a round lead: a DIP and a header are stamped from
    # sheet, and a round pin on a DIP is the detail that makes a rendered package look like
    # a toy. A box needs no turning, so the instanced path takes it as it is.
    return [
        _Piece(
            source=_box(blade[0], blade[1], height)
            if blade is not None
            else _upright_cylinder(radius, height),
            rgb=LEAD_RGB,
            position=(0.0, 0.0, 0.0),
            specular=0.6,
            specular_power=30.0,
            instances=tuple((pin_x, pin_y, bottom + height / 2) for pin_x, pin_y in body.pins),
        )
    ]


def _lead_pieces(body: _WorldBody, radius: float = 0.28) -> list[_Piece]:
    """Tinned wire from each pin to the body edge, and down through the hole from there.

    This is most of what makes a resistor read as a resistor: the horizontal run says the
    part is standing on its own leads, and the bend down at each end says which holes
    those leads are in.
    """
    pieces: list[_Piece] = []
    half = body.along / 2
    z = min(body.across, 1.4) / 2 + _LIFT
    for pin_x, pin_y in body.pins:
        if body.axis == "x":
            offset = pin_x - body.x
            if abs(offset) <= half + 0.05:
                continue
            run = abs(offset) - half
            centre = body.x + (half + run / 2) * (1 if offset > 0 else -1)
            pieces.append(
                _Piece(
                    source=_cylinder(radius, run, resolution=10),
                    rgb=LEAD_RGB,
                    position=(centre, pin_y, z),
                    orientation=_ALONG_X,
                    specular=0.6,
                )
            )
        else:
            offset = pin_y - body.y
            if abs(offset) <= half + 0.05:
                continue
            run = abs(offset) - half
            centre = body.y + (half + run / 2) * (1 if offset > 0 else -1)
            pieces.append(
                _Piece(
                    source=_cylinder(radius, run, resolution=10),
                    rgb=LEAD_RGB,
                    position=(pin_x, centre, z),
                    orientation=_ALONG_Y,
                    specular=0.6,
                )
            )
    # The drop is added for EVERY pin, including the ones with no horizontal run: a pin
    # under its own body still has to reach the hole it is in. It starts at the TOP of the
    # horizontal run rather than at its centreline, so its flat cap is buried inside that
    # tube and the corner reads as a bend rather than as two pieces meeting.
    return pieces + _through_hole_pieces(body, z + radius, radius)


def _axial_pieces(body: _WorldBody) -> list[_Piece]:
    """A cylinder lying on the board along its leads, with a band at the marked end.

    Covers resistors and DO-41/DO-35 diodes, which share this archetype and are told apart
    by polarity: a diode's band is its cathode stripe, and it is the difference between a
    working circuit and a dead one.
    """
    radius = body.across / 2
    z = radius + _LIFT
    orientation = _ALONG_X if body.axis == "x" else _ALONG_Y
    surface = body.surface
    # A resistor's body is not a tin can: it is moulded with a shoulder at each end, and a
    # flat-ended cylinder is the single thing that made these read as machined blanks. The
    # barrel is shortened by what the two domes add back, so the part still measures the
    # length its footprint says it does.
    dome = min(radius * 0.55, body.along * 0.16)
    pieces = [
        _Piece(
            source=_cylinder(radius, body.along - 2 * dome),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, z),
            orientation=orientation,
            specular=surface.specular,
            specular_power=surface.specular_power,
        )
    ]
    for end in (-1.0, 1.0):
        along = 1.0 if body.axis == "x" else 0.0
        pieces.append(
            _Piece(
                source=_sphere(radius),
                rgb=_rgb(body.style.fill),
                position=_offset_along(body, end * (body.along / 2 - dome), z),
                # Squashed along the part's own axis, so the end is a shoulder rather than
                # a ball stuck on the end of a tube.
                scale=(
                    (dome / radius, 1.0, 1.0) if along else (1.0, dome / radius, 1.0)
                ),
                specular=surface.specular,
                specular_power=surface.specular_power,
            )
        )

    # The printed colour code, as rings standing a hair proud of the body. Same layout as
    # the 2D view draws (bodies.resistor_bands is the shared source): three bands in the
    # near half and the tolerance band at the far end, because that asymmetry is what says
    # which way round to read them.
    for index, colour in enumerate(body.bands):
        fraction = 0.16 + index * 0.15 if index < len(body.bands) - 1 else 0.80
        pieces.append(
            _Piece(
                source=_cylinder(radius * 1.03, body.along * 0.11, resolution=20),
                rgb=_rgb(colour),
                position=_offset_along(body, (fraction - 0.5) * body.along, z),
                orientation=orientation,
                specular=surface.specular * 0.6,
            )
        )

    if body.polarity is not None:
        # A band at the end nearest the marked pin, standing very slightly proud so it is
        # visible against the body rather than fighting it for the same pixels.
        band_width = max(body.along * 0.16, 0.5)
        offset = (body.along / 2 - band_width) * _towards(body, body.polarity)
        pieces.append(
            _Piece(
                source=_cylinder(radius * 1.04, band_width),
                rgb=_rgb(body.style.accent),
                position=_offset_along(body, offset, z),
                orientation=orientation,
                specular=0.2,
            )
        )
    return pieces + _lead_pieces(body)


def _can_pieces(body: _WorldBody) -> list[_Piece]:
    """An upright cylinder: electrolytic can or potentiometer.

    An electrolytic gets its polarity stripe down the side nearest the negative lead. On a
    real part that stripe is the only thing distinguishing the two ends, and reversing an
    electrolytic is the classic way to make one vent.
    """
    radius = min(body.size_x, body.size_y) / 2
    pieces = [
        _Piece(
            source=_cylinder(radius, body.height, resolution=28),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, body.height / 2 + _LIFT),
            orientation=_ALONG_Z,
            specular=0.35,
        ),
        # The crimped rim at the top, where the sleeve is folded over the can. A thin
        # bright ring and nothing more: this was a WHITE DISC across the whole top, and
        # the two capacitors on a board then read as screw heads -- the first person to
        # see it called them mounting holes.
        _Piece(
            source=_cylinder(radius, 0.22, resolution=28),
            rgb=_lit(body.style.fill, 1.5),
            position=(body.x, body.y, body.height + _LIFT - 0.11),
            orientation=_ALONG_Z,
            specular=0.55,
            specular_power=35.0,
        ),
        # The top itself is the sleeve, as it is on the real part.
        _Piece(
            source=_cylinder(radius * 0.93, 0.24, resolution=28),
            rgb=_lit(body.style.fill, 1.12),
            position=(body.x, body.y, body.height + _LIFT - 0.1),
            orientation=_ALONG_Z,
            specular=0.3,
        ),
    ]
    # The vent, scored into that top rather than printed on it: two shallow grooves, which
    # is what a radial can carries and what it splits along when one lets go.
    for across in (False, True):
        pieces.append(
            _Piece(
                source=(
                    _box(radius * 1.45, radius * 0.1, 0.1)
                    if across
                    else _box(radius * 0.1, radius * 1.45, 0.1)
                ),
                rgb=_lit(body.style.fill, 0.55),
                position=(body.x, body.y, body.height + _LIFT - 0.02),
                specular=0.05,
            )
        )
    if body.polarity is not None:
        # The stripe marks the end AWAY from pin 1: pin 1 is the positive lead, so the printed
        # band belongs on the negative side.
        #
        # Thin RADIALLY and wide tangentially, sitting just inside the can's surface, so it
        # reads as printing on the side. A square slab, which is what this was, stuck out of
        # the cylinder as a separate bolted-on block.
        direction = -_towards(body, body.polarity)
        thickness = radius * 0.22
        tangential = radius * 1.05
        source = (
            _box(thickness, tangential, body.height * 0.9)
            if body.axis == "x"
            else _box(tangential, thickness, body.height * 0.9)
        )
        pieces.append(
            _Piece(
                source=source,
                rgb=_rgb(body.style.accent),
                position=_offset_along(
                    body, direction * (radius - thickness * 0.45), body.height / 2 + _LIFT
                ),
                specular=0.1,
            )
        )
    # Under the can, so only the hole and the solder side ever show them -- which is
    # exactly where a radial capacitor's legs are.
    return pieces + _through_hole_pieces(body, _LIFT + 0.15)


def _disc_pieces(body: _WorldBody) -> list[_Piece]:
    """A ceramic disc standing on edge -- a LENS, not a coin.

    The dipped case is thicker in the middle and thins to a rounded edge, and a flat
    cylinder with a sharp rim was the single thing that made these read as washers stood
    up on the board. A sphere squashed across the leads is the shape, and costs the same
    one solid.
    """
    diameter = body.along
    thickness = body.across
    squash = max(thickness / diameter, 0.08)
    return [
        _Piece(
            source=_sphere(diameter / 2, resolution=24),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, diameter / 2 + _LIFT),
            # Flattened across the leads: the disc's faces look sideways, which is how one
            # is fitted and why two of them side by side need the room they do.
            scale=(1.0, squash, 1.0) if body.axis == "x" else (squash, 1.0, 1.0),
            specular=0.15,
        ),
        *_lead_pieces(body),
    ]


def _film_pieces(body: _WorldBody) -> list[_Piece]:
    """A box film capacitor: a slab with ROUNDED ENDS, which is what a dipped case is.

    A bare cuboid was the worst model in the library -- an orange brick sitting on the
    board with nothing about it that said capacitor. The ends are half-round in plan, so
    the case is a stadium prism: one box and two upright cylinders.
    """
    surface = body.surface
    fill = _rgb(body.style.fill)
    radius = body.across / 2
    middle = max(body.along - 2 * radius, body.along * 0.1)
    pieces = [
        _Piece(
            source=(
                _box(middle, body.across, body.height)
                if body.axis == "x"
                else _box(body.across, middle, body.height)
            ),
            rgb=fill,
            position=(body.x, body.y, body.height / 2 + _LIFT),
            specular=surface.specular,
            specular_power=surface.specular_power,
        )
    ]
    for end in (-1.0, 1.0):
        pieces.append(
            _Piece(
                source=_cylinder(radius, body.height, resolution=20),
                rgb=fill,
                position=_offset_along(
                    body, end * (body.along / 2 - radius), body.height / 2 + _LIFT
                ),
                orientation=_ALONG_Z,
                specular=surface.specular,
                specular_power=surface.specular_power,
            )
        )
    return pieces + _lead_pieces(body)


def _dip_pieces(body: _WorldBody) -> list[_Piece]:
    """A plastic package with a pin-1 dot, which is the only thing telling you which way
    round the chip goes."""
    pieces = [
        _Piece(
            source=_box(body.size_x, body.size_y, body.height),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, body.height / 2 + _LIFT),
            specular=0.12,
        )
    ]
    if body.polarity is not None:
        dot_r = min(body.size_x, body.size_y) * 0.055
        # Pulled in from the corner so the dot sits on the package rather than over its edge.
        dot_x = body.x + (body.polarity[0] - body.x) * 0.62
        dot_y = body.y + (body.polarity[1] - body.y) * 0.62
        pieces.append(
            _Piece(
                # A DIMPLE pressed into the plastic, which is what it is: the accent
                # colour at nearly a tenth of the package made it a headlamp, and the one
                # marking on the part came out looking like a component of its own.
                source=_cylinder(dot_r, 0.24, resolution=14),
                rgb=_lit(body.style.fill, 0.55),
                position=(dot_x, dot_y, body.height + _LIFT - 0.06),
                orientation=_ALONG_Z,
                specular=0.05,
            )
        )
        # AND the notch at the pin-1 end, which is the marking people actually use: the
        # dot goes under a label often enough that a chip is oriented by the semicircle
        # moulded into the end of the package. Cut into the end rather than printed on it,
        # so it reads from the side as well as from above.
        notch_r = min(body.across * 0.22, body.along * 0.12)
        pieces.append(
            _Piece(
                source=_cylinder(notch_r, body.height * 0.9, resolution=18),
                # Darker than the package, or a notch cut into black plastic is invisible
                # against black plastic -- which is what the first attempt drew.
                rgb=_lit(body.style.fill, 0.45),
                position=_offset_along(
                    body,
                    _towards(body, body.polarity) * body.along / 2,
                    body.height / 2 + _LIFT,
                ),
                orientation=_ALONG_Z,
                specular=0.02,
            )
        )
    # From half way up the package, because a DIP's rows are wider than its body: the pins
    # run down the OUTSIDE of the two long sides, which is what they do on the real part
    # and what tells you at a glance which way the package is turned. Flat, because a DIP's
    # pins are stamped from sheet and a round one reads as a model of a chip.
    blade = (0.5, 0.26) if body.axis == "x" else (0.26, 0.5)
    return pieces + _through_hole_pieces(body, body.height / 2 + _LIFT, blade=blade)


def _to92_pieces(body: _WorldBody) -> list[_Piece]:
    """The D-shaped case, with the flat face the legs are read against.

    It was a squashed cylinder, which is an ellipse: no flat, so nothing on the part said
    which way round it goes -- and getting a transistor round the wrong way is the classic
    way to spend an evening. The flat faces the row of pins, as it does on the real part.
    """
    radius = max(body.size_x, body.size_y) / 2
    return [
        _Piece(
            # The chord sits where the case's own DEPTH puts it: a flat at half the depth
            # shaves a sliver off a cylinder and reads as no flat at all, which is what
            # the first attempt drew.
            source=_d_prism(
                radius,
                max(min(body.size_x, body.size_y) - radius, radius * 0.25),
                body.height,
            ),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, body.height / 2 + _LIFT),
            # The profile is built with its flat towards +y; a part whose pins run along y
            # wants it towards +x instead.
            orientation=(0.0, 0.0, 0.0) if body.axis == "x" else (0.0, 0.0, 90.0),
            specular=0.15,
        ),
        *_lead_pieces(body),
    ]


def _to220_pieces(body: _WorldBody) -> list[_Piece]:
    """Plastic case with the metal tab above it -- the tab is what decides whether the part
    clears its neighbours and whether it can be bolted to a heatsink."""
    plastic_h = body.height * 0.62
    tab_h = body.height - plastic_h
    # The tab is a sheet of metal the plastic is moulded AROUND, so it is thin and it is
    # flush with the back face -- not a slab the width of the package sitting on top of it,
    # which is what this was and what made a TO-220 read as a two-tone brick. Which face is
    # the back does not matter electrically; that it has one does, because that is the side
    # a heatsink bolts to.
    tab_thickness = min(body.across * 0.22, 1.4)
    back = (body.across - tab_thickness) / 2
    offset_x, offset_y = (0.0, back) if body.axis == "x" else (back, 0.0)
    hole_r = min(body.across, body.along) * 0.16
    return [
        _Piece(
            source=_box(body.size_x, body.size_y, plastic_h),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, plastic_h / 2 + _LIFT),
            specular=0.12,
        ),
        _Piece(
            source=(
                _box(body.size_x, tab_thickness, tab_h)
                if body.axis == "x"
                else _box(tab_thickness, body.size_y, tab_h)
            ),
            rgb=_rgb(body.style.accent),
            position=(body.x + offset_x, body.y + offset_y, plastic_h + tab_h / 2 + _LIFT),
            specular=0.7,
            specular_power=40.0,
        ),
        # The bolt hole, as a dark disc through the tab rather than a hole cut in it: this
        # is a 3 mm feature on a vertical face, where the board's own holes are the surface
        # you spend the whole time looking at. It says the part can be bolted down, which
        # is the fact a height check cares about.
        _Piece(
            source=_cylinder(hole_r, tab_thickness * 1.4, resolution=16),
            rgb=_lit(body.style.accent, 0.3),
            position=(
                body.x + offset_x,
                body.y + offset_y,
                plastic_h + tab_h * 0.62 + _LIFT,
            ),
            orientation=_ALONG_Y if body.axis == "x" else _ALONG_X,
            specular=0.1,
        ),
        *_through_hole_pieces(body, _LIFT + 0.15),
    ]


def _led_pieces(body: _WorldBody) -> list[_Piece]:
    """A cylindrical lens with a domed top, lit like a lens rather than a case."""
    radius = min(body.size_x, body.size_y) / 2
    barrel_h = max(body.height - radius, radius * 0.4)
    lens = _rgb(body.style.fill)
    # From ``style.lens`` rather than from numbers written here: the flag was documented as
    # a shading hint for both views and read by neither, so a lens was lit like a slightly
    # glossy plastic case. A LED that does not look lit does not look like a LED.
    surface = body.surface
    pieces = [
        _Piece(
            source=_cylinder(radius, barrel_h, resolution=24),
            rgb=lens,
            position=(body.x, body.y, barrel_h / 2 + _LIFT),
            orientation=_ALONG_Z,
            specular=surface.specular,
            specular_power=surface.specular_power,
        ),
        _Piece(
            source=_sphere(radius),
            rgb=lens,
            position=(body.x, body.y, barrel_h + _LIFT),
            specular=surface.specular,
            specular_power=surface.specular_power,
        ),
        # The flange at the base is the flat that marks the cathode on a real LED.
        _Piece(
            source=_cylinder(radius * 1.12, radius * 0.22, resolution=24),
            rgb=lens,
            position=(body.x, body.y, radius * 0.11 + _LIFT),
            orientation=_ALONG_Z,
            specular=surface.specular * 0.6,
        ),
    ]
    return pieces + _lead_pieces(body)


def _header_pieces(body: _WorldBody) -> list[_Piece]:
    """Black moulding with a gold pin standing up over each hole.

    The pins are INSTANCED into one glyph rather than given an actor each. A 2x20 header has
    forty of them, and a board with a few such headers would otherwise add more actors than
    the entire 2400-pad grid does -- the grid is instanced for exactly this reason.
    """
    moulding_h = min(body.height * 0.3, 2.6)
    pin_h = body.height - moulding_h
    return [
        _Piece(
            source=_box(body.size_x, body.size_y, moulding_h),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, moulding_h / 2 + _LIFT),
            specular=0.1,
        ),
        _Piece(
            source=_box(0.64, 0.64, pin_h),
            rgb=_rgb(body.style.accent),
            position=(0.0, 0.0, 0.0),
            specular=0.75,
            specular_power=40.0,
            instances=tuple(
                (pin_x, pin_y, moulding_h + pin_h / 2 + _LIFT) for pin_x, pin_y in body.pins
            ),
        ),
        # The same pin continues below the moulding and through the board, which is what
        # is soldered -- the part standing above it is only the half you can see. Square,
        # because that is what the pin above the moulding already is.
        *_through_hole_pieces(body, _LIFT + 0.15, blade=(0.64, 0.64)),
    ]


def _screw_terminal_pieces(body: _WorldBody) -> list[_Piece]:
    """A block with a screw head per way, so the wire entries are where they look."""
    pieces = [
        _Piece(
            source=_box(body.size_x, body.size_y, body.height),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, body.height / 2 + _LIFT),
            specular=0.15,
        )
    ]
    head_r = min(body.across * 0.28, 1.6)
    for pin_x, pin_y in body.pins:
        pieces.append(
            _Piece(
                source=_cylinder(head_r, 0.5, resolution=14),
                rgb=_rgb(body.style.accent),
                # Sunk into the top rather than sitting on it, so the block is exactly as
                # tall as its footprint says -- which is the number the height rule and
                # the case check are both working from.
                position=(pin_x, pin_y, body.height + _LIFT - 0.25),
                orientation=_ALONG_Z,
                specular=0.7,
                specular_power=35.0,
            )
        )
    return pieces + _through_hole_pieces(body, _LIFT + 0.15)


def _pot_pieces(body: _WorldBody) -> list[_Piece]:
    """A round body with the adjustment shaft on top, which is what has to stay reachable."""
    radius = min(body.size_x, body.size_y) / 2
    body_h = body.height * 0.72
    shaft_h = body.height - body_h
    return [
        _Piece(
            source=_cylinder(radius, body_h, resolution=28),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, body_h / 2 + _LIFT),
            orientation=_ALONG_Z,
            specular=0.2,
        ),
        _Piece(
            source=_cylinder(radius * 0.28, shaft_h, resolution=18),
            rgb=_rgb(body.style.accent),
            position=(body.x, body.y, body_h + shaft_h / 2 + _LIFT),
            orientation=_ALONG_Z,
            specular=0.6,
            specular_power=35.0,
        ),
        *_through_hole_pieces(body, _LIFT + 0.15),
    ]


def _switch_pieces(body: _WorldBody) -> list[_Piece]:
    """A case with the button proud of it, so its travel is visible in a height check."""
    case_h = body.height * 0.7
    button_h = body.height - case_h
    return [
        _Piece(
            source=_box(body.size_x, body.size_y, case_h),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, case_h / 2 + _LIFT),
            specular=0.12,
        ),
        _Piece(
            source=_cylinder(min(body.size_x, body.size_y) * 0.22, button_h, resolution=18),
            rgb=_rgb(body.style.accent),
            position=(body.x, body.y, case_h + button_h / 2 + _LIFT),
            orientation=_ALONG_Z,
            specular=0.3,
        ),
        *_through_hole_pieces(body, _LIFT + 0.15),
    ]


def _crystal_pieces(body: _WorldBody) -> list[_Piece]:
    """An HC-49 can: a flattened metal cylinder, shaded as metal.

    The shading comes from ``style.metallic`` via ``body.surface`` rather than from numbers
    written here. It used to be hardcoded, which meant the one archetype in the registry
    that is literally a metal can was ignoring its own metallic flag -- two sources of
    truth for one fact, and the sort of drift bodies.py exists to prevent.
    """
    radius = max(body.size_x, body.size_y) / 2
    squash = min(body.size_x, body.size_y) / max(body.size_x, body.size_y)
    scale = (1.0, squash, 1.0) if body.size_x >= body.size_y else (squash, 1.0, 1.0)
    surface = body.surface
    # The can is drawn short of its full height and domed over, because a real HC-49 is
    # closed with a pressed cap and a flat-topped tube reads as a slug of metal. The lip
    # near the base is where the can is welded to its header, and it is the detail that
    # says which way up the part goes.
    # Shallow: a can closed with a pressed cap has a rounded shoulder, and a dome the
    # height of its own radius turns the part into a bullet.
    dome = min(radius * squash * 0.55, body.height * 0.16)
    barrel = body.height - dome
    return [
        _Piece(
            source=_cylinder(radius, barrel, resolution=24),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, barrel / 2 + _LIFT),
            orientation=_ALONG_Z,
            scale=_upright_scale(scale),
            specular=surface.specular,
            specular_power=surface.specular_power,
        ),
        _Piece(
            source=_sphere(radius, resolution=24),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, barrel + _LIFT),
            scale=(scale[0], scale[1], dome / radius),
            specular=surface.specular,
            specular_power=surface.specular_power,
        ),
        _Piece(
            source=_cylinder(radius * 1.06, 0.35, resolution=24),
            rgb=_lit(body.style.fill, 0.8),
            position=(body.x, body.y, 0.35 / 2 + _LIFT),
            orientation=_ALONG_Z,
            scale=_upright_scale(scale),
            specular=surface.specular * 0.7,
        ),
        *_lead_pieces(body),
    ]


def _box_pieces(body: _WorldBody) -> list[_Piece]:
    """Plain case: film capacitors, relays, and anything without its own archetype."""
    surface = body.surface
    return [
        _Piece(
            source=_box(body.size_x, body.size_y, body.height),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, body.height / 2 + _LIFT),
            specular=surface.specular,
            specular_power=surface.specular_power,
        ),
        *_lead_pieces(body),
    ]


_BUILDERS: dict[str, Any] = {
    "axial-cylinder": _axial_pieces,
    "radial-electrolytic": _can_pieces,
    "disc-ceramic": _disc_pieces,
    "box-film": _film_pieces,
    "dip": _dip_pieces,
    "to92": _to92_pieces,
    "to220": _to220_pieces,
    "led-round": _led_pieces,
    "pin-header": _header_pieces,
    "screw-terminal": _screw_terminal_pieces,
    "potentiometer": _pot_pieces,
    "tactile-switch": _switch_pieces,
    "crystal-hc49": _crystal_pieces,
    "relay-box": _box_pieces,
    "generic-box": _box_pieces,
}


def _towards(body: _WorldBody, point: tuple[float, float]) -> float:
    """+1 or -1: which way along the body's axis ``point`` lies."""
    delta = point[0] - body.x if body.axis == "x" else point[1] - body.y
    return 1.0 if delta >= 0 else -1.0


def _offset_along(body: _WorldBody, offset: float, z: float) -> tuple[float, float, float]:
    if body.axis == "x":
        return (body.x + offset, body.y, z)
    return (body.x, body.y + offset, z)


def build_component(lookup: FootprintLookup, comp: Any, board: Board) -> list[vtk.vtkActor]:
    """Every solid making up one placed component.

    A list rather than a single actor because real parts are not one colour: a TO-220 is
    black plastic with a bright metal tab, a pin header is black moulding with gold pins, and
    an electrolytic has a printed stripe. Collapsing those into one actor is what made the
    3D view look like a board full of identical blocks.
    """
    body = _world_body(lookup, comp, board)
    if body is None:
        return []
    footprint = lookup(comp.footprint_id)
    assert footprint is not None  # _world_body already returned None otherwise.
    builder = _BUILDERS.get(footprint.body.archetype, _box_pieces)
    return [_actor_for(piece) for piece in builder(body)]


def _attach(mapper: Any, source: Any, *, glyph: bool = False) -> None:
    """Hand a mapper either a VTK source or a finished polydata.

    Most pieces are sources with a pipeline behind them, and a few are polydata built here
    -- the D-shaped TO-92 profile, the punched tile -- which have no output port to
    connect. One place knows the difference rather than every builder.
    """
    if hasattr(source, "GetOutputPort"):
        if glyph:
            mapper.SetSourceConnection(source.GetOutputPort())
        else:
            mapper.SetInputConnection(source.GetOutputPort())
    elif glyph:
        mapper.SetSourceData(source)
    else:
        mapper.SetInputData(source)


def _actor_for(piece: _Piece) -> vtk.vtkActor:
    actor = vtk.vtkActor()
    if piece.instances:
        points = vtk.vtkPoints()
        for x, y, z in piece.instances:
            points.InsertNextPoint(x, y, z)
        data = vtk.vtkPolyData()
        data.SetPoints(points)
        glyph = vtk.vtkGlyph3DMapper()
        glyph.SetInputData(data)
        _attach(glyph, piece.source, glyph=True)
        glyph.SetOrient(False)
        glyph.SetScaling(False)
        actor.SetMapper(glyph)
    else:
        mapper = vtk.vtkPolyDataMapper()
        _attach(mapper, piece.source)
        actor.SetMapper(mapper)
        actor.SetScale(*piece.scale)
        actor.SetOrientation(*piece.orientation)
        actor.SetPosition(*piece.position)
    prop = actor.GetProperty()
    prop.SetColor(*piece.rgb)
    prop.SetSpecular(piece.specular)
    prop.SetSpecularPower(piece.specular_power)
    prop.SetOpacity(piece.opacity)
    return actor


def _conductor_centreline(
    cond: Conductor,
    board: Board,
    run_z: float,
    joint_z: float,
    is_trace: bool,
) -> list[tuple[float, float, float]]:
    """The path a conductor's tube actually follows, in board mm.

    A SOLDER RUN LIES FLAT. It is fused to the copper along its whole length, so its
    centreline is the hole centres at one height and nothing else.

    A WIRE GOES INTO ITS HOLES. It was drawn as a stick floating parallel to the board and
    stopping in mid-air above each pad, with the fillet drawn at the stick's height rather
    than at the pad -- so a wire neither entered the board nor touched what it was soldered
    to. Real wire is bent down at each end and the joint is made where it meets the copper.
    So each end drops from the run height to the pad, over a short horizontal run-in: long
    enough to read as a bend rather than a staple, and never more than a fraction of the
    segment it is bending within, so a two-hole link does not turn into a V.

    This is also what keeps a wire lifted over an obstacle attached to the board at all --
    at one stacking level its ends are more than a bead's width above the pads.
    """
    flat = [(*_xy(board, hole), run_z) for hole in cond.path]
    if is_trace:
        # A point at every pad and one between each pair, so `_trace_swell` has somewhere
        # to bring the radius back down. Nothing else about a run's path moves: it is fused
        # to the copper along its whole length and goes exactly where the pads are.
        if len(flat) < 2:
            return flat
        woven: list[tuple[float, float, float]] = [flat[0]]
        for previous, point in pairwise(flat):
            woven.append(((previous[0] + point[0]) / 2, (previous[1] + point[1]) / 2, run_z))
            woven.append(point)
        return woven
    if len(flat) < 2:
        return flat

    drop = abs(run_z - joint_z)
    out: list[tuple[float, float, float]] = []
    for end, inward in ((0, 1), (-1, -2)):
        x, y, _ = flat[end]
        ix, iy, _ = flat[inward]
        span = math.hypot(ix - x, iy - y)
        # SHORT, and kept near the pad. A bend as long as it is deep reads nicely and
        # sweeps a long way: a wire coming down from two stacking levels ramped almost
        # four millimetres across the board, through whatever was lying under it -- which
        # is two of the fifteen golden fixtures. Held inside a third of a pitch, the
        # descent stays over its own pad and its neighbours, where the only other thing
        # is something soldered to the same hole. Never longer than the drop either, or a
        # wire lying flat on the board acquires a bend it does not need.
        run_in = min(drop, board.pitch / 3, span * 0.35)
        t = (run_in / span) if span else 0.0
        elbow = (x + (ix - x) * t, y + (iy - y) * t, run_z)
        out = (
            [(x, y, joint_z), elbow, *flat[1:-1]]
            if end == 0
            else [*out, elbow, (x, y, joint_z)]
        )
    return out


def _trace_swell(cond: Conductor, centreline: list[tuple[float, float, float]]) -> list[float]:
    """How wide the run is at each point of its centreline, as a multiple of the ridge.

    ``_conductor_centreline`` gives a solder run one point per pad, and a tube through
    those is a constant ridge with no joints in it. The midpoints inserted here are what
    lets the radius come back DOWN between pads: full width where it is soldered, drawn in
    between, which is the silhouette of a run of solder along a row of pads and the reason
    somebody can count the joints on one.
    """
    del cond
    joint = 1.0 / TRACE_WAIST_RATIO
    return [joint if index % 2 == 0 else 1.0 for index in range(len(centreline))]


def build_conductor(
    cond: Conductor,
    board: Board,
    stack: int = 0,
    net_class: NetClass | None = None,
    signal_index: int = 0,
) -> list[vtk.vtkActor]:
    """One conductor's solids.

    ``stack`` separates conductors that share a layer. Everything on the solder side used
    to sit at one z per ``layer_z``, so two bare wires crossing were drawn INTERSECTING --
    occupying the same space, which is not a thing wire does and looked like a modelling
    error because it was one. A small per-conductor offset makes one pass over the other,
    which is what the board actually looks like.
    """
    is_trace = contacts_every_path_hole(cond)
    z = conductor_z(cond, board, stack)
    joint_z = pad_z(board, cond.side)
    centreline = _conductor_centreline(cond, board, z, joint_z, is_trace)
    swell = _trace_swell(cond, centreline) if is_trace else None

    points = vtk.vtkPoints()
    line = vtk.vtkPolyLine()
    line.GetPointIds().SetNumberOfIds(len(centreline))
    for i, (x, y, pz) in enumerate(centreline):
        points.InsertNextPoint(x, y, pz)
        line.GetPointIds().SetId(i, i)
    cells = vtk.vtkCellArray()
    cells.InsertNextCell(line)
    poly = vtk.vtkPolyData()
    poly.SetPoints(points)
    poly.SetLines(cells)
    if swell is not None:
        widths = vtk.vtkDoubleArray()
        widths.SetName("swell")
        for value in swell:
            widths.InsertNextValue(value)
        poly.GetPointData().SetScalars(widths)

    insulated = cond.kind in ("insulated-wire", "top-jumper")
    radius = conductor_radius(cond)
    tube = vtk.vtkTubeFilter()
    tube.SetInputData(poly)
    # VTK scales the radius by scalar/min(scalar), so the radius set here is the value at
    # the NARROWEST point -- the bridge between two pads, for a run.
    tube.SetRadius(radius * TRACE_WAIST_RATIO if swell is not None else radius)
    tube.SetNumberOfSides(TUBE_SIDES)
    tube.CappingOn()
    if swell is not None:
        # ONE SURFACE for a solder run, swelling at each pad and drawing in between it.
        # The joints used to be spheres dropped on top of a constant tube, which meets it
        # in a hard crease all the way round and reads as a bead threaded on a wire -- two
        # objects where the board has one. A run of solder is a single fillet, and varying
        # the tube's own radius is what says so.
        tube.SetVaryRadiusToVaryRadiusByScalar()
        tube.SetRadiusFactor(1.0 / TRACE_WAIST_RATIO)

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(tube.GetOutputPort())
    # The swell scalars are geometry, not data. Left visible, VTK colour-maps them and a
    # run of solder comes out as a blue-to-cyan rainbow.
    mapper.ScalarVisibilityOff()
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    if is_trace:
        fallback = SOLDER_RGB
    elif insulated:
        fallback = _insulation_rgb(net_class, signal_index)
    else:
        fallback = BARE_RGB
    rgb = _hex_rgb(getattr(cond, "color", None), fallback)
    actor.GetProperty().SetColor(*rgb)
    if is_trace:
        # Solder is metal, and it is ROUGH metal: a broad soft sheen rather than the tight
        # glint tinned wire gives. Making it shiny is what once made it look like wire,
        # which is the one thing it must not look like -- but leaving it at a matte 0.25
        # with no ambient is what made it look like grey plumbing, which is not better.
        # A little ambient so a fillet turned away from the lamp is still a fillet.
        actor.GetProperty().SetSpecular(0.42)
        actor.GetProperty().SetSpecularPower(16.0)
        actor.GetProperty().SetDiffuse(0.8)
        actor.GetProperty().SetAmbient(0.16)
    elif insulated:
        actor.GetProperty().SetSpecular(0.35)
        actor.GetProperty().SetSpecularPower(25.0)
        actor.GetProperty().SetAmbient(0.14)
    else:
        # Tinned copper: a tight bright glint, which is exactly what solder must not have.
        actor.GetProperty().SetSpecular(0.9)
        actor.GetProperty().SetSpecularPower(60.0)
        actor.GetProperty().SetAmbient(0.12)
    actors = [actor]

    # The distinction that matters: a trace is soldered at EVERY pad it crosses, a wire
    # only at its two ends. Render exactly that -- driven by the same
    # `contacts_every_path_hole` predicate the connectivity engine itself uses.
    #
    # A run's joints ALONG its length are in the tube above, because a run of solder is one
    # piece of metal and spheres dropped on it meet it in a crease. Its two ENDS still need
    # a solid: a tube's cap is a flat disc, which at a corner shows as a sliced-off face.
    # At exactly the radius the tube already has there, a sphere is tangent to it and the
    # seam does not exist.
    #
    # A wire's two are proud of it, and have to be: they are SOLDER, on the ends of
    # something that is not, and a silver fillet at each end of a coloured sleeve is how
    # you see where a wire is actually attached.
    bead_pts = vtk.vtkPoints()
    for hole in (cond.path[0], cond.path[-1]):
        x, y = _xy(board, hole)
        # AT THE PAD, not at the conductor. A fillet is centred on the copper and wicks
        # into the hole; drawn at a lifted wire's own height it would hang in the air above
        # the pad it is supposedly made on.
        bead_pts.InsertNextPoint(x, y, joint_z)
    bead_data = vtk.vtkPolyData()
    bead_data.SetPoints(bead_pts)
    sphere = vtk.vtkSphereSource()
    sphere.SetRadius(radius if is_trace else radius * BEAD_RATIO_WIRE)
    sphere.SetThetaResolution(BEAD_RESOLUTION)
    sphere.SetPhiResolution(BEAD_RESOLUTION)
    bead_glyph = vtk.vtkGlyph3DMapper()
    bead_glyph.SetInputData(bead_data)
    bead_glyph.SetSourceConnection(sphere.GetOutputPort())
    bead_glyph.SetOrient(False)
    bead_glyph.SetScaling(False)
    beads = vtk.vtkActor()
    beads.SetMapper(bead_glyph)
    beads.GetProperty().SetColor(*SOLDER_RGB)
    # The same material as the run it swells out of -- a joint and the solder leading into
    # it are one piece of metal, and two finishes would draw a seam that is not there.
    beads.GetProperty().SetSpecular(0.42)
    beads.GetProperty().SetSpecularPower(16.0)
    beads.GetProperty().SetDiffuse(0.8)
    beads.GetProperty().SetAmbient(0.16)
    actors.append(beads)
    return actors


def _insulation_rgb(net_class: NetClass | None, signal_index: int) -> tuple[float, float, float]:
    """An insulated wire's colour, from the same convention the 2D view and the cut list
    use -- so a wire is the same colour on screen, in 3D and on the list someone works
    from."""
    from .view2d import insulation_color

    colour = insulation_color(net_class, signal_index)
    return (colour.redF(), colour.greenF(), colour.blueF())


# --------------------------------------------------------------------------- scene


#: How far a part rises off the board in the exploded view, in mm. Well clear of the
#: tallest thing in the registry (a 20 mm TO-220), so no part floats inside its
#: neighbour, and the leads stay pointing at the holes they came out of.
EXPLODED_LIFT_MM: float = 26.0

#: What a dimmed actor's colour is multiplied by. Enough that the part a step is about is
#: unmistakable in a thumbnail; not so far that the rest of the board stops being legible
#: context, because a step image that does not show WHERE is not worth printing.
DIM_FACTOR: float = 0.42


#: Leader lines in the exploded view: thin, unlit, and darker than any part, so they
#: read as annotation rather than as wire.
LEADER_RGB: tuple[float, float, float] = (0.42, 0.45, 0.50)


def build_drop_lines(
    lookup: FootprintLookup, doc: PerfDocument, lift: float
) -> vtk.vtkActor | None:
    """A line from every lifted part down to the holes it drops into.

    Without these a vertical explosion is ambiguous, and measurably so: a part over the
    MIDDLE of the board projects onto the board from the standard three-quarter viewpoint
    and reads as sitting on it, while one near an edge reads as floating. Same lift, two
    different apparent meanings, decided by nothing but where the part happens to be.

    Lines fix it at any lift, and they are what the view is for (PLAN.md D7): the question
    an exploded view answers is not "what is on this board" â€” the assembled view answers
    that â€” but "which holes does THIS go in", and a leader line is the answer drawn.

    One actor for every line on the board. A part with no known footprint contributes
    nothing, as everywhere else.
    """
    if lift <= 0:
        return None

    points = vtk.vtkPoints()
    lines = vtk.vtkCellArray()
    for comp in doc.components:
        footprint = lookup(comp.footprint_id)
        if footprint is None:
            continue
        for _pin, hole in all_pin_holes(comp, footprint):
            x, y = _xy(doc.board, hole)
            first = points.InsertNextPoint(x, y, 0.0)
            second = points.InsertNextPoint(x, y, lift)
            lines.InsertNextCell(2)
            lines.InsertCellPoint(first)
            lines.InsertCellPoint(second)

    if points.GetNumberOfPoints() == 0:
        return None

    data = vtk.vtkPolyData()
    data.SetPoints(points)
    data.SetLines(lines)
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(data)
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    prop = actor.GetProperty()
    prop.SetColor(*LEADER_RGB)
    prop.SetLineWidth(1.0)
    prop.SetAmbient(1.0)
    prop.SetDiffuse(0.0)
    return actor


def _lift(actor: vtk.vtkActor, dz: float) -> vtk.vtkActor:
    """Raise one actor off the board. Added to its position rather than assigned: a
    glyphed piece bakes its instances into the points and leaves the actor at the origin,
    while a solid piece has already been positioned."""
    if dz:
        x, y, z = actor.GetPosition()
        actor.SetPosition(x, y, z + dz)
    return actor


def _dim(actor: vtk.vtkActor) -> vtk.vtkActor:
    """Push an actor back so something else can come forward. Keeps its hue -- a dimmed
    resistor still reads as a resistor -- and drops the specular, since a highlight on a
    part that is not the subject is exactly what the eye goes to."""
    prop = actor.GetProperty()
    prop.SetColor(*(channel * DIM_FACTOR for channel in prop.GetColor()))
    prop.SetSpecular(0.0)
    return actor


#: What the subject of a step is tinted towards. A fixed colour rather than "the part's
#: own, but brighter": raising the brightness of a BLACK DIP against parts dimmed to
#: near-black leaves the two indistinguishable, which is exactly what the first attempt
#: produced. A step image has one job, and it cannot depend on the part having a light
#: colour to begin with.
HIGHLIGHT_RGB: tuple[float, float, float] = (0.44, 0.72, 1.0)

#: How far towards it. Short of 1 so the shape still shades and reads as a solid object
#: rather than a flat silhouette.
HIGHLIGHT_MIX: float = 0.75


def _pick_out(actor: vtk.vtkActor) -> vtk.vtkActor:
    """The one thing this step is about. The caption names the part; this says WHERE."""
    prop = actor.GetProperty()
    prop.SetColor(
        *(
            channel * (1 - HIGHLIGHT_MIX) + target * HIGHLIGHT_MIX
            for channel, target in zip(prop.GetColor(), HIGHLIGHT_RGB, strict=True)
        )
    )
    prop.SetAmbient(0.45)
    prop.SetSpecular(0.15)
    prop.SetSpecularPower(COPPER_SPECULAR_POWER)
    return actor


def populate_renderer(
    ren: vtk.vtkRenderer,
    doc: PerfDocument,
    lookup: FootprintLookup,
    *,
    exploded_mm: float = 0.0,
    highlight: str | None = None,
) -> dict[str, int]:
    """Rebuild the board's actors in an EXISTING renderer, leaving the camera alone.

    ``exploded_mm`` lifts every part off the board, so the holes each one drops into are
    visible at once (PLAN.md D7). ``highlight`` is a component or conductor id â€” the value
    ``guide.step_focus`` returns â€” and dims everything else, which is what turns a frame
    of the assembly sequence into an illustration of one step.

    The BOARD is never dimmed, only the other parts and the copper. A step card says which
    holes a part goes in, and a reader who cannot see the holes has been given a picture
    of the answer with the question rubbed out.

    This separation is the whole point. The interactive view is refreshed after every
    command, and refreshing used to mean constructing a fresh renderer -- which meant
    ``ResetCamera`` plus a fixed elevation and azimuth. So the 3D viewpoint silently
    snapped back to its default the moment the user did anything: rotate the board, nudge a
    part, and the rotation was gone. The camera belongs to the person looking through it,
    and only an explicit request (opening the view, flipping the board, "Reset View") may
    move it -- see :func:`apply_default_camera`.
    """
    board = doc.board
    # Actors and 2D props only. Lights are not view props, so they survive -- which is what
    # makes this safe to call repeatedly without re-adding a light every time.
    ren.RemoveAllViewProps()

    for actor in build_substrate(doc):
        ren.AddActor(actor)
    # The board's own copper goes down before the pads, so a stripboard reads as strips
    # with holes in them rather than as a grid of islands that happen to line up.
    for actor in build_strips(doc):
        ren.AddActor(actor)
    # Copper face by face, because a single-sided board genuinely has none on top: the
    # component side is bare phenolic with drilled holes, which is most of what makes
    # those boards look and solder differently.
    for face in ("top", "bottom"):
        if board.single_sided and face == "top":
            continue
        ren.AddActor(build_pads(board, face, holes_without_grid_pad(doc, face) | cut_holes(doc)))
    # A finger has no bore, so it gets no wall either -- and neither does a position a
    # mounting bore's patch has taken over, where the plate is solid.
    ren.AddActor(build_drills(board, patched_holes(doc) | undrilled_holes(doc)))
    for actor in build_legend(doc):
        ren.AddActor(actor)
    for actor in build_edge_connectors(doc):
        ren.AddActor(actor)
    for actor in build_mounting_holes(doc):
        ren.AddActor(actor)
    leaders = build_drop_lines(lookup, doc, exploded_mm)
    if leaders is not None:
        ren.AddActor(leaders)
    for comp in doc.components:
        subject = highlight is not None and comp.id == highlight
        for actor in build_component(lookup, comp, board):
            _lift(actor, exploded_mm)
            if highlight is not None:
                (_pick_out if subject else _dim)(actor)
            ren.AddActor(actor)
    net_class_by_id = {net.id: net.net_class for net in doc.nets}
    signal_index = {
        net.id: index
        for index, net in enumerate(n for n in doc.nets if n.net_class == "signal")
    }
    # How high each conductor has to sit to clear what it crosses -- from the engine, so
    # 2D and 3D cannot disagree about which wire passes over which. Not a running index:
    # see occupancy.stacking_layers.
    layers = stacking_layers(doc)
    for cond in doc.conductors:
        subject = highlight is not None and cond.id == highlight
        for actor in build_conductor(
            cond,
            board,
            stack=layers.get(cond.id, cond.layer_z),
            net_class=net_class_by_id.get(cond.net_id or ""),
            signal_index=signal_index.get(cond.net_id or "", 0),
        ):
            if highlight is not None:
                (_pick_out if subject else _dim)(actor)
            ren.AddActor(actor)

    ren.ResetCameraClippingRange()
    return {"actors": ren.GetActors().GetNumberOfItems(), "pads": board.cols * board.rows}


def apply_default_camera(ren: vtk.vtkRenderer, flipped: bool = False) -> None:
    """Frame the board from the standard three-quarter viewpoint.

    Called when the view is first shown, when the board is flipped, and by "Reset Camera" --
    never as a side effect of the document changing.

    The orientation is set ABSOLUTELY before the three-quarter tilt is applied, because
    ``Elevation`` and ``Azimuth`` are relative and ``ResetCamera`` preserves the current
    direction of view. Without the reset to a known axis, calling this after the user had
    orbited would re-frame the board while keeping their accumulated rotation and then tilt
    a further 32 degrees from wherever that left it -- so "Reset Camera" would move the view
    without resetting it, and would land somewhere different every time it was pressed.
    These three values are vtkCamera's own defaults, so a first call is unaffected.
    """
    cam = ren.GetActiveCamera()
    cam.SetPosition(0.0, 0.0, 1.0)
    cam.SetFocalPoint(0.0, 0.0, 0.0)
    cam.SetViewUp(0.0, 1.0, 0.0)

    ren.ResetCamera()
    if flipped:
        cam.Elevation(180)
    cam.Elevation(-32)
    cam.Azimuth(18)
    cam.Zoom(1.35)
    ren.ResetCameraClippingRange()


def build_renderer(
    doc: PerfDocument,
    lookup: FootprintLookup,
    flipped: bool = False,
    *,
    exploded_mm: float = 0.0,
    highlight: str | None = None,
) -> tuple[vtk.vtkRenderer, dict[str, int]]:
    """A renderer with the board in it, framed and lit. For a first build or a one-off
    offscreen render; an interactive view refreshes with :func:`populate_renderer`."""
    ren = vtk.vtkRenderer()
    ren.SetBackground(0.09, 0.09, 0.11)
    stats = populate_renderer(ren, doc, lookup, exploded_mm=exploded_mm, highlight=highlight)
    apply_default_camera(ren, flipped)

    apply_default_lighting(ren)
    return ren, stats


def apply_default_lighting(ren: vtk.vtkRenderer) -> None:
    """Two lights, and they travel WITH THE CAMERA.

    They used to be nailed to world positions, one above the board and one below. That
    was already the second attempt -- with only the upper one, flipping to the solder side
    showed an almost black board -- and it was still wrong in the same way, just less
    obviously: the lower light was the deliberately dimmer FILL, so the face you turn the
    board over to inspect was the one lit by the weaker lamp, at a fixed angle that no
    longer had anything to do with where you were looking from. Solder came out a flat
    dark grey with no highlight on it, which is why a run of it read as grey plumbing
    rather than as metal, and why turning the board could take a conductor into shadow for
    no reason a viewer could see.

    A camera light is positioned relative to the viewpoint, so whichever face is towards
    you is the lit one, at a constant angle, however the board is turned -- which is also
    what somebody bent over a board with a lamp on the bench actually has. The key is
    offset up and to the left rather than dead-on: a headlight flattens everything it
    lights, and the shape of a solder fillet is the thing this view is for.
    """
    for existing in list(ren.GetLights()):
        ren.RemoveLight(existing)

    key = vtk.vtkLight()
    key.SetLightTypeToCameraLight()
    key.SetPosition(-0.45, 0.55, 1.0)  # relative to the camera, in its own frame
    key.SetFocalPoint(0.0, 0.0, 0.0)
    key.SetIntensity(0.95)
    ren.AddLight(key)

    fill = vtk.vtkLight()
    fill.SetLightTypeToCameraLight()
    fill.SetPosition(0.6, -0.4, 0.7)
    fill.SetFocalPoint(0.0, 0.0, 0.0)
    fill.SetIntensity(0.45)
    ren.AddLight(fill)


def trackball_style() -> vtk.vtkInteractorStyleTrackballCamera:
    """Drag-to-orbit, and stop when the pointer stops.

    Set explicitly because VTK's default is ``vtkInteractorStyleSwitch``, which can start in
    joystick mode: there, holding the button keeps the camera turning at a speed set by how
    far the pointer is from centre, so a small drag sends the board spinning. That reads as a
    broken control rather than a different one, and it is not something a user would think
    to go looking for a setting to change.
    """
    return vtk.vtkInteractorStyleTrackballCamera()


# ---------------------------------------------------------------------------
# Is there anything to render into
# ---------------------------------------------------------------------------

#: The argv flag that turns a run of this application into the probe below. It is not a
#: user-facing option and is not documented as one: it exists because a frozen build has
#: no separate Python to spawn, so the only interpreter available to ask the question in
#: a *different process* is this application itself.
PROBE_FLAG = "--probe-offscreen-gl"


def probe_offscreen_gl() -> int:
    """Open an offscreen window, render one frame, and report by exit status.

    Called in the child process; ``main`` routes ``PROBE_FLAG`` here before it touches
    Qt. The parent never calls this directly, because the failure it is looking for
    cannot be caught in the process it happens in.
    """
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.SetSize(16, 16)
    win.AddRenderer(vtk.vtkRenderer())
    win.Render()
    return 0


@functools.cache
def offscreen_gl_available() -> bool:
    """Whether this machine can render offscreen at all -- asked in a child process.

    VTK DOES NOT RAISE WHEN THERE IS NO USABLE OpenGL BEHIND AN OFFSCREEN WINDOW. It
    ends the process: on Windows an access violation, elsewhere an abort. So the
    ``except Exception`` around every render in this application, and the promise it
    encodes -- that a guide with no pictures is still a complete guide -- is unreachable
    in exactly the case it was written for. A virtual machine, a remote desktop session
    or an old driver does not raise; it takes the whole application down mid-export.

    The only way to catch that is to spend the crash somewhere it costs nothing, which
    means another process, which is what this is. One spawn per run, cached: the answer
    cannot change while the application is open.

    Timeouts and OSErrors answer False. A machine slow enough to take three minutes over
    a 16x16 frame is not one to render 29 step images on either.
    """
    if getattr(sys, "frozen", False):
        # A frozen build IS the interpreter, so it probes by running itself. sys.argv[0]
        # is not usable here -- it is the launcher script under some spawn methods.
        command = [sys.executable, PROBE_FLAG]
    else:
        command = [sys.executable, "-m", "perfstudio.ui.main", PROBE_FLAG]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - machine-specific
        return False
    return completed.returncode == 0


def render_offscreen(
    doc: PerfDocument,
    lookup: FootprintLookup,
    path: str,
    width: int = 1400,
    height: int = 950,
    flipped: bool = False,
    *,
    exploded_mm: float = 0.0,
    highlight: str | None = None,
) -> dict[str, int]:
    """Headless render. This is the path the build guide's step images take.

    Pair it with ``guide.document_at_step`` and ``guide.step_focus`` and one call is one
    step card's illustration: the board as it stands at that point in the build, with the
    thing that step asks for picked out of it.
    """
    ren, stats = build_renderer(
        doc, lookup, flipped=flipped, exploded_mm=exploded_mm, highlight=highlight
    )
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.AddRenderer(ren)
    win.SetSize(width, height)
    win.Render()

    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(win)
    w2i.Update()
    writer = vtk.vtkPNGWriter()
    writer.SetFileName(path)
    writer.SetInputConnection(w2i.GetOutputPort())
    writer.Write()
    return stats


def step_is_solder_side(doc: PerfDocument, focus: str) -> bool:
    """Which face a step's work happens on, given what it is about.

    A part goes in from the component side. A connection is made on whichever face its
    conductor lies on, which for everything except a top jumper is the solder side --
    so this is the difference between illustrating a step and photographing the back of
    the board it is behind.
    """
    for conductor in doc.conductors:
        if conductor.id == focus:
            return conductor.side == "bottom"
    return False


#: JPEG quality for the step images, measured on ``dense.perf`` at 560x370 (33 steps):
#:
#:     PNG        135.6 KB/image   guide.html 6378 KB
#:     JPEG q90    61.8 KB/image
#:     JPEG q82    47.0 KB/image   guide.html 2070 KB
#:     JPEG q70    35.6 KB/image
#:
#: The guide is one self-contained file meant to open on a phone, so its size is a
#: feature and not a detail -- and these are photographs of a lit 3D scene, the exact
#: content PNG is worst at. q82 keeps the pin-1 marks and the highlight colour clean;
#: below about q70 the JPEG rings around the thin leader lines in the exploded shots.
#: JPEG rather than WebP because this has to survive PyInstaller: vtkJPEGWriter is
#: linked into VTK, while Qt's WebP writer is an image-format plugin that has to be
#: collected into the bundle, and a missing plugin fails at the user's machine.
STEP_IMAGE_JPEG_QUALITY = 82


def render_step_images(
    doc: PerfDocument,
    guide: Guide,
    lookup: FootprintLookup,
    width: int = 560,
    height: int = 370,
) -> dict[str, bytes]:
    """One picture per build step (PLAN.md Â§7.2), keyed by ``guide.step_focus``.

    JPEG bytes rather than files, because that is what ``guide_export.guide_to_html``
    takes: it base64s them into the document, so the finished guide cannot acquire a
    dependency on a folder beside it. Base64 costs a third on top, which is the other
    reason the format matters here (see ``STEP_IMAGE_JPEG_QUALITY``).

    FROM THE SIDE THE WORK IS DONE ON. Most connections are made on the solder side, and
    photographed from the component side they are behind 1.6 mm of board -- the first
    version of this produced fourteen pictures of a board with nothing happening in them.
    So there are two cameras, and a step is shot from whichever face its subject is on,
    which is also the face the builder is looking at when they do it.

    Within a face the camera is framed on the FINISHED board and then left alone. Framing
    each step on its own contents would zoom in hard on the first part and back out as
    the board filled, so flipping through the guide would read as a series of unrelated
    photographs rather than one board being built.

    One render window per face, re-actored per step -- which is what ``populate_renderer``
    exists for, and is the difference between half a second and a minute.
    """
    steps = all_steps(guide)
    if not steps:
        return {}

    windows: dict[bool, tuple[vtk.vtkRenderer, vtk.vtkRenderWindow]] = {}

    def window_for(flipped: bool) -> tuple[vtk.vtkRenderer, vtk.vtkRenderWindow]:
        if flipped not in windows:
            ren, _stats = build_renderer(doc, lookup, flipped=flipped)
            win = vtk.vtkRenderWindow()
            win.SetOffScreenRendering(1)
            win.AddRenderer(ren)
            win.SetSize(width, height)
            windows[flipped] = (ren, win)
        return windows[flipped]

    images: dict[str, bytes] = {}
    for index, step in enumerate(steps):
        focus = step_focus(step)
        ren, win = window_for(step_is_solder_side(doc, focus))
        populate_renderer(
            ren, document_at_step(doc, guide, index), lookup, highlight=focus
        )
        win.Render()
        grab = vtk.vtkWindowToImageFilter()
        grab.SetInput(win)
        grab.Update()
        writer = vtk.vtkJPEGWriter()
        writer.SetQuality(STEP_IMAGE_JPEG_QUALITY)
        writer.WriteToMemoryOn()
        writer.SetInputConnection(grab.GetOutputPort())
        writer.Write()
        jpeg = numpy_support.vtk_to_numpy(writer.GetResult())  # type: ignore[no-untyped-call]
        images[focus] = bytes(jpeg.tobytes())
    return images
