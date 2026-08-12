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

import math
from dataclasses import dataclass
from typing import Any

import vtk  # type: ignore[import-untyped]

from perfstudio.connectivity import FootprintLookup
from perfstudio.geometry import (
    all_pin_holes,
    board_edge_margin_mm,
    board_size_mm,
    column_label,
    consumed_holes,
    edge_connector_holes,
    edge_finger_rect,
    hole_key,
    holes_without_grid_pad,
    legend_strip_mm,
    pad_extent_mm,
    printed_row_label,
    transform_offset,
)
from perfstudio.model import (
    Board,
    BoardSide,
    Conductor,
    HoleCoord,
    NetClass,
    PerfDocument,
    contacts_every_path_hole,
)

from .boardcolors import scheme_for
from .bodies import BodyStyle, placement_for, polarity_pin_offset, style_for

SUBSTRATE_RGB = {
    "FR4": (0.16, 0.36, 0.21),
    "FR2": (0.62, 0.48, 0.29),
    "FR1": (0.68, 0.55, 0.35),
}
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


def build_substrate(board: Board) -> vtk.vtkActor:
    w, h = _board_size_mm(board)
    cube = vtk.vtkCubeSource()
    cube.SetXLength(w)
    cube.SetYLength(h)
    cube.SetZLength(board.thickness)
    cube.SetCenter(
        (board.cols - 1) * board.pitch / 2,
        -(board.rows - 1) * board.pitch / 2,
        -board.thickness / 2,
    )
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(cube.GetOutputPort())
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*scheme_for(board.material).rgb)
    return actor


def pad_z(board: Board, side: BoardSide) -> float:
    """Where one face's copper sits, in board z. Pure, so it can be asserted directly."""
    return 0.05 if side == "top" else -board.thickness - 0.05


def conductor_z(cond: Conductor, board: Board, stack: int = 0) -> float:
    """Where one conductor sits, in board z.

    Split out from ``build_conductor`` because the interesting property is arithmetic and
    testing it through VTK means reaching into an unexecuted pipeline, which segfaults.

    ``stack`` separates conductors sharing a layer. Everything on the solder side used to
    sit at one z per ``layer_z``, so two crossing bare wires were drawn INTERSECTING --
    occupying the same space, which is not a thing wire does and looked like a modelling
    error because it was one.
    """
    if cond.side == "bottom":
        # The substrate spans z = -thickness .. 0, so solder-side conductors must sit
        # BELOW -thickness or they end up buried inside the board.
        return -board.thickness - 0.5 - 0.4 * cond.layer_z - 0.08 * stack
    return 0.45 + 0.08 * stack


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
    segments = 24
    outer = _stadium_contour(extent_x, extent_y, segments)
    n = len(outer)
    drill_r = board.drill_diameter / 2

    points = vtk.vtkPoints()
    for x, y in outer:
        points.InsertNextPoint(x, y, 0.0)
    for i in range(n):
        angle = 2 * math.pi * i / n
        points.InsertNextPoint(drill_r * math.cos(angle), drill_r * math.sin(angle), 0.0)

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


def build_drills(board: Board, consumed: frozenset[str] = frozenset()) -> vtk.vtkActor:
    """The holes, as dark cylinders straight through the board.

    THE BOARD HAD NO HOLES FROM UNDERNEATH. The substrate is one cube and the pads sat
    only on top, so turning the board over showed a blank green slab -- on the very view
    whose job is to check the solder side.

    Cut rather than drawn would be the obvious fix and is not affordable: a boolean
    subtraction per hole is thousands of them on a real board. A near-black cylinder
    spanning the full thickness and a little beyond reads as a hole from either face, at
    the cost of one glyphed source -- so the instancing claim survives intact. Nobody
    looking at a perfboard from 200 mm away can tell the difference, and the alternative
    is a board with no holes in it.
    """
    points = vtk.vtkPoints()
    for col in range(board.cols):
        for row in range(board.rows):
            # A mounting bore is a bigger hole in the same place, drawn by
            # `build_mounting_holes`. Leaving this one in as well puts a 1 mm cylinder
            # inside a 3.2 mm one, which z-fights along its whole length.
            if consumed and hole_key(HoleCoord(col, row)) in consumed:
                continue
            x, y = _xy(board, HoleCoord(col, row))
            points.InsertNextPoint(x, y, -board.thickness / 2)
    data = vtk.vtkPolyData()
    data.SetPoints(points)

    bore = vtk.vtkCylinderSource()
    bore.SetRadius(board.drill_diameter / 2)
    # Slightly longer than the board so its caps never z-fight with the pads at either
    # face, which shows up as a flickering speckle across the whole grid.
    bore.SetHeight(board.thickness + 0.3)
    bore.SetResolution(12)
    # vtkCylinderSource stands along Y; the board's thickness is along Z.
    upright = vtk.vtkTransform()
    upright.RotateX(90)
    turn = vtk.vtkTransformPolyDataFilter()
    turn.SetTransform(upright)
    turn.SetInputConnection(bore.GetOutputPort())

    glyph = vtk.vtkGlyph3DMapper()
    glyph.SetInputData(data)
    glyph.SetSourceConnection(turn.GetOutputPort())
    glyph.SetOrient(False)
    glyph.SetScaling(False)

    actor = vtk.vtkActor()
    actor.SetMapper(glyph)
    actor.GetProperty().SetColor(*DRILL_RGB)
    actor.GetProperty().SetSpecular(0.0)
    actor.GetProperty().SetAmbient(0.25)
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
    actor.GetProperty().SetColor(*PAD_RGB)
    actor.GetProperty().SetSpecular(0.4)
    return actor


def build_mounting_holes(doc: PerfDocument) -> list[vtk.vtkActor]:
    """The screw bores, straight through the board.

    Same trick as :func:`build_drills` -- a dark cylinder rather than a boolean
    subtraction from the substrate -- and for the same reason, except that here there are
    four of them rather than thousands, so the cost was never the argument. Consistency
    is: a mounting hole that was cut properly would look different from every other hole
    on the board, which would read as the two being different kinds of thing.
    """
    board = doc.board
    if not doc.mounting_holes:
        return []
    actors: list[vtk.vtkActor] = []
    for mount in doc.mounting_holes:
        bore = vtk.vtkCylinderSource()
        bore.SetRadius(mount.diameter / 2)
        bore.SetHeight(board.thickness + 0.3)
        bore.SetResolution(28)
        x, y = _xy(board, mount.at)
        actor = vtk.vtkActor()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(bore.GetOutputPort())
        actor.SetMapper(mapper)
        # vtkCylinderSource stands along Y; the board's thickness is along Z.
        actor.SetOrientation(90.0, 0.0, 0.0)
        actor.SetPosition(x, y, -board.thickness / 2)
        actor.GetProperty().SetColor(*DRILL_RGB)
        actor.GetProperty().SetSpecular(0.0)
        actor.GetProperty().SetAmbient(0.25)
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
            for hole in edge_connector_holes(connector, board):
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
            actor.GetProperty().SetColor(*PAD_RGB)
            actor.GetProperty().SetSpecular(0.4)
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
    #: World positions of every pin, and of the polarity pin if the part has one.
    pins: tuple[tuple[float, float], ...]
    polarity: tuple[float, float] | None

    @property
    def along(self) -> float:
        return self.size_x if self.axis == "x" else self.size_y

    @property
    def across(self) -> float:
        return self.size_y if self.axis == "x" else self.size_x


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
        pins=tuple(
            (hole.col * board.pitch, -hole.row * board.pitch)
            for _pin, hole in all_pin_holes(comp, fp)
        ),
        polarity=to_world(*polarity_local) if polarity_local is not None else None,
    )


def _lead_pieces(body: _WorldBody, radius: float = 0.28) -> list[_Piece]:
    """Tinned wire from each pin to the body edge, for parts whose body is shorter than
    their lead span. This is most of what makes a resistor read as a resistor."""
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
    return pieces


def _axial_pieces(body: _WorldBody) -> list[_Piece]:
    """A cylinder lying on the board along its leads, with a band at the marked end.

    Covers resistors and DO-41/DO-35 diodes, which share this archetype and are told apart
    by polarity: a diode's band is its cathode stripe, and it is the difference between a
    working circuit and a dead one.
    """
    radius = body.across / 2
    z = radius + _LIFT
    orientation = _ALONG_X if body.axis == "x" else _ALONG_Y
    pieces = [
        _Piece(
            source=_cylinder(radius, body.along),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, z),
            orientation=orientation,
            specular=0.3,
        )
    ]
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
        )
    ]
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
    return pieces


def _disc_pieces(body: _WorldBody) -> list[_Piece]:
    """A ceramic disc standing on edge: a cylinder whose axis lies ACROSS the leads."""
    diameter = body.along
    thickness = body.across
    orientation = _ALONG_Y if body.axis == "x" else _ALONG_X
    return [
        _Piece(
            source=_cylinder(diameter / 2, thickness, resolution=24),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, diameter / 2 + _LIFT),
            orientation=orientation,
            specular=0.15,
        )
    ] + _lead_pieces(body)


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
        dot_r = min(body.size_x, body.size_y) * 0.09
        # Pulled in from the corner so the dot sits on the package rather than over its edge.
        dot_x = body.x + (body.polarity[0] - body.x) * 0.62
        dot_y = body.y + (body.polarity[1] - body.y) * 0.62
        pieces.append(
            _Piece(
                source=_cylinder(dot_r, 0.3, resolution=14),
                rgb=_rgb(body.style.accent),
                position=(dot_x, dot_y, body.height + _LIFT),
                orientation=_ALONG_Z,
                specular=0.1,
            )
        )
    return pieces


def _to92_pieces(body: _WorldBody) -> list[_Piece]:
    """A small oval case: one cylinder, squashed by a non-uniform actor scale."""
    radius = max(body.size_x, body.size_y) / 2
    squash = min(body.size_x, body.size_y) / max(body.size_x, body.size_y)
    scale = (1.0, squash, 1.0) if body.size_x >= body.size_y else (squash, 1.0, 1.0)
    return [
        _Piece(
            source=_cylinder(radius, body.height, resolution=22),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, body.height / 2 + _LIFT),
            orientation=_ALONG_Z,
            scale=scale,
            specular=0.15,
        )
    ] + _lead_pieces(body)


def _to220_pieces(body: _WorldBody) -> list[_Piece]:
    """Plastic case with the metal tab above it -- the tab is what decides whether the part
    clears its neighbours and whether it can be bolted to a heatsink."""
    plastic_h = body.height * 0.62
    tab_h = body.height - plastic_h
    tab_thickness = body.across * 0.35
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
            position=(body.x, body.y, plastic_h + tab_h / 2 + _LIFT),
            specular=0.7,
            specular_power=40.0,
        ),
    ]


def _led_pieces(body: _WorldBody) -> list[_Piece]:
    """A cylindrical lens with a domed top, lit like a lens rather than a case."""
    radius = min(body.size_x, body.size_y) / 2
    barrel_h = max(body.height - radius, radius * 0.4)
    lens = _rgb(body.style.fill)
    pieces = [
        _Piece(
            source=_cylinder(radius, barrel_h, resolution=24),
            rgb=lens,
            position=(body.x, body.y, barrel_h / 2 + _LIFT),
            orientation=_ALONG_Z,
            specular=0.8,
            specular_power=45.0,
        ),
        _Piece(
            source=_sphere(radius),
            rgb=lens,
            position=(body.x, body.y, barrel_h + _LIFT),
            specular=0.9,
            specular_power=60.0,
        ),
        # The flange at the base is the flat that marks the cathode on a real LED.
        _Piece(
            source=_cylinder(radius * 1.12, radius * 0.22, resolution=24),
            rgb=lens,
            position=(body.x, body.y, radius * 0.11 + _LIFT),
            orientation=_ALONG_Z,
            specular=0.5,
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
                position=(pin_x, pin_y, body.height + _LIFT),
                orientation=_ALONG_Z,
                specular=0.7,
                specular_power=35.0,
            )
        )
    return pieces


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
    ]


def _crystal_pieces(body: _WorldBody) -> list[_Piece]:
    """An HC-49 can: a flattened metal cylinder, shaded as metal."""
    radius = max(body.size_x, body.size_y) / 2
    squash = min(body.size_x, body.size_y) / max(body.size_x, body.size_y)
    scale = (1.0, squash, 1.0) if body.size_x >= body.size_y else (squash, 1.0, 1.0)
    return [
        _Piece(
            source=_cylinder(radius, body.height, resolution=24),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, body.height / 2 + _LIFT),
            orientation=_ALONG_Z,
            scale=scale,
            specular=0.85,
            specular_power=50.0,
        )
    ] + _lead_pieces(body)


def _box_pieces(body: _WorldBody) -> list[_Piece]:
    """Plain case: film capacitors, relays, and anything without its own archetype."""
    return [
        _Piece(
            source=_box(body.size_x, body.size_y, body.height),
            rgb=_rgb(body.style.fill),
            position=(body.x, body.y, body.height / 2 + _LIFT),
            specular=0.35 if body.style.metallic else 0.15,
        )
    ] + _lead_pieces(body)


_BUILDERS: dict[str, Any] = {
    "axial-cylinder": _axial_pieces,
    "radial-electrolytic": _can_pieces,
    "disc-ceramic": _disc_pieces,
    "box-film": _box_pieces,
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
        glyph.SetSourceConnection(piece.source.GetOutputPort())
        glyph.SetOrient(False)
        glyph.SetScaling(False)
        actor.SetMapper(glyph)
    else:
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(piece.source.GetOutputPort())
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
    z = conductor_z(cond, board, stack)
    points = vtk.vtkPoints()
    line = vtk.vtkPolyLine()
    line.GetPointIds().SetNumberOfIds(len(cond.path))
    for i, hole in enumerate(cond.path):
        x, y = _xy(board, hole)
        points.InsertNextPoint(x, y, z)
        line.GetPointIds().SetId(i, i)
    cells = vtk.vtkCellArray()
    cells.InsertNextCell(line)
    poly = vtk.vtkPolyData()
    poly.SetPoints(points)
    poly.SetLines(cells)

    is_trace = contacts_every_path_hole(cond)
    insulated = cond.kind in ("insulated-wire", "top-jumper")
    # A solder trace is a low bulging ridge along the pads; a wire is a round section
    # standing off the board. Drawn at their real proportions rather than one being a
    # fatter version of the other, because "which of these is solder" is the question this
    # view exists to answer (PLAN.md Sec 8.3).
    radius = 0.34 if is_trace else (0.42 if insulated else 0.30)
    tube = vtk.vtkTubeFilter()
    tube.SetInputData(poly)
    tube.SetRadius(radius)
    tube.SetNumberOfSides(10)
    tube.CappingOn()

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(tube.GetOutputPort())
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
        # Solder is dull and slightly rough. Making it shiny is what made it look like
        # wire, which is the one thing it must not look like.
        actor.GetProperty().SetSpecular(0.25)
        actor.GetProperty().SetSpecularPower(8.0)
        actor.GetProperty().SetDiffuse(0.85)
    elif insulated:
        actor.GetProperty().SetSpecular(0.35)
        actor.GetProperty().SetSpecularPower(25.0)
    else:
        actor.GetProperty().SetSpecular(0.9)
        actor.GetProperty().SetSpecularPower(60.0)
    actors = [actor]

    # The distinction that matters: a trace is soldered at EVERY pad it crosses, a wire
    # only at its two ends. Render exactly that -- driven by the same
    # `contacts_every_path_hole` predicate the connectivity engine itself uses.
    bead_at = cond.path if is_trace else (cond.path[0], cond.path[-1])
    bead_pts = vtk.vtkPoints()
    for hole in bead_at:
        x, y = _xy(board, hole)
        bead_pts.InsertNextPoint(x, y, z)
    bead_data = vtk.vtkPolyData()
    bead_data.SetPoints(bead_pts)
    sphere = vtk.vtkSphereSource()
    # A trace bulges noticeably at every pad -- that chain of beads IS the silhouette of
    # a solder run. A wire only has a fillet where it is soldered, at its two ends.
    sphere.SetRadius(radius * (1.9 if is_trace else 1.3))
    sphere.SetThetaResolution(12)
    sphere.SetPhiResolution(12)
    bead_glyph = vtk.vtkGlyph3DMapper()
    bead_glyph.SetInputData(bead_data)
    bead_glyph.SetSourceConnection(sphere.GetOutputPort())
    bead_glyph.SetOrient(False)
    bead_glyph.SetScaling(False)
    beads = vtk.vtkActor()
    beads.SetMapper(bead_glyph)
    beads.GetProperty().SetColor(*SOLDER_RGB)
    beads.GetProperty().SetSpecular(0.3)
    beads.GetProperty().SetSpecularPower(10.0)
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


def populate_renderer(
    ren: vtk.vtkRenderer, doc: PerfDocument, lookup: FootprintLookup
) -> dict[str, int]:
    """Rebuild the board's actors in an EXISTING renderer, leaving the camera alone.

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

    ren.AddActor(build_substrate(board))
    # Copper face by face, because a single-sided board genuinely has none on top: the
    # component side is bare phenolic with drilled holes, which is most of what makes
    # those boards look and solder differently.
    for face in ("top", "bottom"):
        if board.single_sided and face == "top":
            continue
        ren.AddActor(build_pads(board, face, holes_without_grid_pad(doc, face)))
    ren.AddActor(build_drills(board, consumed_holes(doc)))
    for actor in build_legend(doc):
        ren.AddActor(actor)
    for actor in build_edge_connectors(doc):
        ren.AddActor(actor)
    for actor in build_mounting_holes(doc):
        ren.AddActor(actor)
    for comp in doc.components:
        for actor in build_component(lookup, comp, board):
            ren.AddActor(actor)
    net_class_by_id = {net.id: net.net_class for net in doc.nets}
    signal_index = {
        net.id: index
        for index, net in enumerate(n for n in doc.nets if n.net_class == "signal")
    }
    for stack, cond in enumerate(doc.conductors):
        for actor in build_conductor(
            cond,
            board,
            stack=stack,
            net_class=net_class_by_id.get(cond.net_id or ""),
            signal_index=signal_index.get(cond.net_id or "", 0),
        ):
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
    doc: PerfDocument, lookup: FootprintLookup, flipped: bool = False
) -> tuple[vtk.vtkRenderer, dict[str, int]]:
    """A renderer with the board in it, framed and lit. For a first build or a one-off
    offscreen render; an interactive view refreshes with :func:`populate_renderer`."""
    ren = vtk.vtkRenderer()
    ren.SetBackground(0.09, 0.09, 0.11)
    stats = populate_renderer(ren, doc, lookup)
    apply_default_camera(ren, flipped)

    # TWO lights, above and below. With only the upper one, flipping to the solder side
    # showed an almost black board: the substrate's underside faced away from the only light
    # in the scene, so the view whose entire purpose is checking the side you actually solder
    # was the one you could not see. The lower light is dimmer -- a board's solder side is
    # genuinely less brightly lit than its component side, and matching that keeps the two
    # sides visually distinguishable instead of identically flat.
    key = vtk.vtkLight()
    key.SetPosition(80, 60, 120)
    key.SetIntensity(0.9)
    ren.AddLight(key)

    fill = vtk.vtkLight()
    fill.SetPosition(-60, -40, -140)
    fill.SetIntensity(0.7)
    ren.AddLight(fill)
    return ren, stats


def trackball_style() -> vtk.vtkInteractorStyleTrackballCamera:
    """Drag-to-orbit, and stop when the pointer stops.

    Set explicitly because VTK's default is ``vtkInteractorStyleSwitch``, which can start in
    joystick mode: there, holding the button keeps the camera turning at a speed set by how
    far the pointer is from centre, so a small drag sends the board spinning. That reads as a
    broken control rather than a different one, and it is not something a user would think
    to go looking for a setting to change.
    """
    return vtk.vtkInteractorStyleTrackballCamera()


def render_offscreen(
    doc: PerfDocument,
    lookup: FootprintLookup,
    path: str,
    width: int = 1400,
    height: int = 950,
    flipped: bool = False,
) -> dict[str, int]:
    """Headless render. This is the path the build guide's step images would use."""
    ren, stats = build_renderer(doc, lookup, flipped=flipped)
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
