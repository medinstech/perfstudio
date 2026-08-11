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

from dataclasses import dataclass
from typing import Any

import vtk  # type: ignore[import-untyped]

from perfstudio.connectivity import FootprintLookup
from perfstudio.geometry import all_pin_holes, transform_offset
from perfstudio.model import Board, Conductor, HoleCoord, PerfDocument, contacts_every_path_hole

from .bodies import BodyStyle, placement_for, polarity_pin_offset, style_for

SUBSTRATE_RGB = {
    "FR4": (0.16, 0.36, 0.21),
    "FR2": (0.62, 0.48, 0.29),
    "FR1": (0.68, 0.55, 0.35),
}
PAD_RGB = (0.80, 0.66, 0.32)
SOLDER_RGB = (0.72, 0.74, 0.77)
BARE_RGB = (0.85, 0.87, 0.89)
BODY_RGB = (0.22, 0.22, 0.26)
#: Tinned component lead.
LEAD_RGB = (0.78, 0.80, 0.84)


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

    # An annulus, not a disc: a pad with no hole in it makes the board read as a dotted
    # sheet rather than as perfboard, and the hole is the entire point of the part. Still one
    # source glyphed over every position, so the instancing claim is unaffected.
    ring = vtk.vtkDiskSource()
    ring.SetOuterRadius(board.pad_diameter / 2)
    ring.SetInnerRadius(board.drill_diameter / 2)
    ring.SetCircumferentialResolution(16)
    ring.SetRadialResolution(1)
    # vtkDiskSource lies in the XY plane already, which is the plane the board is in.
    tf = vtk.vtkTransformPolyDataFilter()
    tf.SetTransform(vtk.vtkTransform())
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
    ren.AddActor(build_pads(board))
    for comp in doc.components:
        for actor in build_component(lookup, comp, board):
            ren.AddActor(actor)
    for cond in doc.conductors:
        for actor in build_conductor(cond, board):
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
