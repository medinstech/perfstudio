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

from typing import Any

import vtk  # type: ignore[import-untyped]

from perfstudio.connectivity import FootprintLookup
from perfstudio.geometry import transform_offset
from perfstudio.model import Board, Conductor, HoleCoord, PerfDocument, contacts_every_path_hole

SUBSTRATE_RGB = {
    "FR4": (0.16, 0.36, 0.21),
    "FR2": (0.62, 0.48, 0.29),
    "FR1": (0.68, 0.55, 0.35),
}
PAD_RGB = (0.80, 0.66, 0.32)
SOLDER_RGB = (0.72, 0.74, 0.77)
BARE_RGB = (0.85, 0.87, 0.89)
BODY_RGB = (0.22, 0.22, 0.26)


def _hex_rgb(value: str | None, fallback: tuple[float, float, float]) -> tuple[float, float, float]:
    if not value or not value.startswith("#") or len(value) != 7:
        return fallback
    return (int(value[1:3], 16) / 255, int(value[3:5], 16) / 255, int(value[5:7], 16) / 255)


def _xy(board: Board, hole: HoleCoord) -> tuple[float, float]:
    return hole.col * board.pitch, -hole.row * board.pitch


def _board_size_mm(board: Board) -> tuple[float, float]:
    return board.cols * board.pitch, board.rows * board.pitch


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
    actor.GetProperty().SetColor(*SUBSTRATE_RGB.get(board.material, SUBSTRATE_RGB["FR4"]))
    return actor


def build_pads(board: Board) -> vtk.vtkActor:
    """Every pad in one instanced actor. This is the scalability claim, tested."""
    points = vtk.vtkPoints()
    for col in range(board.cols):
        for row in range(board.rows):
            x, y = _xy(board, HoleCoord(col, row))
            points.InsertNextPoint(x, y, 0.05)
    data = vtk.vtkPolyData()
    data.SetPoints(points)

    ring = vtk.vtkCylinderSource()
    ring.SetRadius(board.pad_diameter / 2)
    ring.SetHeight(0.1)
    ring.SetResolution(14)
    # vtkCylinderSource points along +Y; stand it up along +Z.
    rotate = vtk.vtkTransform()
    rotate.RotateX(90)
    tf = vtk.vtkTransformPolyDataFilter()
    tf.SetTransform(rotate)
    tf.SetInputConnection(ring.GetOutputPort())

    glyph = vtk.vtkGlyph3DMapper()
    glyph.SetInputData(data)
    glyph.SetSourceConnection(tf.GetOutputPort())
    glyph.SetOrient(False)
    glyph.SetScaling(False)

    actor = vtk.vtkActor()
    actor.SetMapper(glyph)
    actor.GetProperty().SetColor(*PAD_RGB)
    actor.GetProperty().SetSpecular(0.4)
    return actor


def build_component(lookup: FootprintLookup, comp: Any, board: Board) -> vtk.vtkActor | None:
    fp = lookup(comp.footprint_id)
    if fp is None:
        return None
    anchor_x, anchor_y = comp.anchor.col * board.pitch, comp.anchor.row * board.pitch
    xs: list[float] = []
    ys: list[float] = []
    for pt in fp.body_outline:
        tx, ty = transform_offset(pt.x, pt.y, comp.rotation, comp.mirrored)
        xs.append(anchor_x + tx)
        ys.append(anchor_y + ty)
    cx = (min(xs) + max(xs)) / 2
    cy = -(min(ys) + max(ys)) / 2
    height = max(fp.body_height, 1.0)

    cube = vtk.vtkCubeSource()
    cube.SetXLength(max(max(xs) - min(xs), 1.0))
    cube.SetYLength(max(max(ys) - min(ys), 1.0))
    cube.SetZLength(height)
    cube.SetCenter(cx, cy, height / 2 + 0.2)

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(cube.GetOutputPort())
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*BODY_RGB)
    return actor


def build_conductor(cond: Conductor, board: Board) -> list[vtk.vtkActor]:
    # The substrate spans z = -thickness .. 0, so solder-side conductors must sit BELOW
    # -thickness or they end up buried inside the board.
    if cond.side == "bottom":
        z = -board.thickness - 0.5 - 0.4 * cond.layer_z
    else:
        z = 0.45
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
    radius = 0.55 if is_trace else 0.28
    tube = vtk.vtkTubeFilter()
    tube.SetInputData(poly)
    tube.SetRadius(radius)
    tube.SetNumberOfSides(10)
    tube.CappingOn()

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(tube.GetOutputPort())
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    rgb = _hex_rgb(getattr(cond, "color", None), SOLDER_RGB if is_trace else BARE_RGB)
    actor.GetProperty().SetColor(*rgb)
    actor.GetProperty().SetSpecular(0.6 if is_trace else 0.3)
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
    sphere.SetRadius(radius * 1.45)
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
    beads.GetProperty().SetSpecular(0.8)
    actors.append(beads)
    return actors


# --------------------------------------------------------------------------- scene


def build_renderer(
    doc: PerfDocument, lookup: FootprintLookup, flipped: bool = False
) -> tuple[vtk.vtkRenderer, dict[str, int]]:
    board = doc.board
    ren = vtk.vtkRenderer()
    ren.SetBackground(0.09, 0.09, 0.11)

    stats = {"actors": 0, "pads": board.cols * board.rows}
    ren.AddActor(build_substrate(board))
    ren.AddActor(build_pads(board))
    for comp in doc.components:
        actor = build_component(lookup, comp, board)
        if actor:
            ren.AddActor(actor)
    for cond in doc.conductors:
        for actor in build_conductor(cond, board):
            ren.AddActor(actor)
    stats["actors"] = ren.GetActors().GetNumberOfItems()

    ren.ResetCamera()
    cam = ren.GetActiveCamera()
    if flipped:
        cam.Elevation(180)
    cam.Elevation(-32)
    cam.Azimuth(18)
    cam.Zoom(1.35)
    ren.ResetCameraClippingRange()

    light = vtk.vtkLight()
    light.SetPosition(80, 60, 120)
    light.SetIntensity(0.9)
    ren.AddLight(light)
    return ren, stats


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
