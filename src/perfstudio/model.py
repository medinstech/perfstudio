"""PerfStudio domain model.

Ported from the original TypeScript engine. Two rules govern this file:

1. The document is IMMUTABLE. Every dataclass is frozen; mutations happen only by
   dispatching a Command (see command.py), which builds a new document with
   ``dataclasses.replace``. Nothing writes to a document in place.

2. The JSON wire format must stay byte-identical to what the TypeScript engine
   produced. Field names here are Python-cased, but persist.py maps them to the exact
   camelCase keys the .perf format uses. That is what lets the port be verified
   differentially against the old implementation rather than merely "tested".

The one idea worth carrying over above all others: a perfboard connection is not one
thing. There are six physically distinct ways to join two points, each with its own
constraints, costs and failure modes, and modelling that difference is what this whole
project rests on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

Mm: TypeAlias = float

#: Standard perfboard hole pitch.
STANDARD_PITCH_MM: Mm = 2.54

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class HoleCoord:
    """A hole on the board grid, 0-indexed from the top-left.

    ``col`` grows to the right and ``row`` grows downward, matching both the canvas
    convention and the way a board reads when held component-side up.
    """

    col: int
    row: int


#: Human-facing hole address: column letters plus a 1-indexed row, e.g. "A1", "AC12".
#: This is the language the soldering guide speaks, so it is a first-class concept
#: rather than a formatting detail.
HoleRef: TypeAlias = str


@dataclass(frozen=True, slots=True)
class Point2:
    x: Mm
    y: Mm


#: Which physical face of the board something lives on.
#: 'top' is the component side, 'bottom' the solder side.
BoardSide: TypeAlias = Literal["top", "bottom"]

Rotation: TypeAlias = Literal[0, 90, 180, 270]

VALID_ROTATIONS: tuple[Rotation, ...] = (0, 90, 180, 270)

# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------

BoardType: TypeAlias = Literal["pad-per-hole", "stripboard", "plain"]

#: Substrate material. Not cosmetic: FR-2 phenolic paper (cheap "pertinaks") lifts pads
#: under sustained heat far more readily than FR-4, which bounds how long a pure solder
#: trace may be.
BoardMaterial: TypeAlias = Literal["FR4", "FR2", "FR1"]


@dataclass(frozen=True, slots=True)
class Board:
    type: BoardType
    cols: int
    rows: int
    pitch: Mm
    thickness: Mm
    material: BoardMaterial
    pad_diameter: Mm
    drill_diameter: Mm
    #: Stripboard only: the axis the copper strips run along.
    strip_axis: Literal["horizontal", "vertical"] | None = None


# ---------------------------------------------------------------------------
# Footprints and component bodies
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FootprintPin:
    number: str
    #: Hole offset from the component anchor, in grid steps (not mm).
    d_col: int
    d_row: int
    name: str | None = None


BodyArchetype: TypeAlias = Literal[
    "axial-cylinder",  # resistors, DO-41 diodes
    "radial-electrolytic",
    "disc-ceramic",
    "box-film",
    "dip",
    "to92",
    "to220",
    "led-round",
    "pin-header",
    "screw-terminal",
    "potentiometer",
    "tactile-switch",
    "crystal-hc49",
    "relay-box",
    "generic-box",
]


@dataclass(frozen=True, slots=True)
class BodySpec:
    """Parameters for procedurally generating the 3D body.

    We generate roughly 25 body archetypes rather than shipping a mesh library, which
    keeps assets at zero, guarantees the 3D body agrees with the footprint, and avoids
    inheriting a share-alike asset licence.
    """

    archetype: BodyArchetype
    #: Archetype-specific dimensions in mm, e.g. {"length": 6.3, "diameter": 2.4}.
    dims: dict[str, Mm] = field(default_factory=dict)
    color: str | None = None


@dataclass(frozen=True, slots=True)
class Footprint:
    id: str
    name: str
    pins: tuple[FootprintPin, ...]
    #: Body outline in mm relative to the anchor hole centre. Used for courtyard DRC.
    body_outline: tuple[Point2, ...]
    #: Height above the board surface, for clearance DRC and 3D.
    body_height: Mm
    body: BodySpec
    lead_diameter: Mm
    polarized: bool


# ---------------------------------------------------------------------------
# Component instances
# ---------------------------------------------------------------------------

ComponentId: TypeAlias = str


@dataclass(frozen=True, slots=True)
class ComponentInstance:
    id: ComponentId
    #: Designator, e.g. "R1". Matches the schematic netlist.
    ref: str
    value: str
    footprint_id: str
    #: Hole the footprint's origin sits on.
    anchor: HoleCoord
    rotation: Rotation = 0
    mirrored: bool = False
    locked: bool = False


# ---------------------------------------------------------------------------
# Conductors — the heart of the model
# ---------------------------------------------------------------------------

ConductorKind: TypeAlias = Literal[
    "lead-bend",  # a component lead bent to reach a nearby hole; effectively free
    "solder-trace",  # TR "lehim yolu": adjacent pads joined with solder alone
    "solder-trace-wired",  # the same, over a tinned-wire or lead-offcut spine
    "bare-wire",  # bare/tinned wire on the solder side; cannot cross other copper
    "insulated-wire",  # may cross other conductors, at a preparation cost
    "top-jumper",  # insulated jumper routed over the component side
    "strip",  # stripboard's pre-existing copper strip (v2)
]

ConductorId: TypeAlias = str

#: How much solder has been built up, which sets the effective cross-section.
SolderBuildup: TypeAlias = Literal["light", "normal", "heavy"]


@dataclass(frozen=True, slots=True)
class SpineSpec:
    material: Literal["tinned-copper", "lead-offcut"]
    gauge: Mm


@dataclass(frozen=True, slots=True)
class SolderTraceConductor:
    """Pads joined with solder, optionally over a wire spine.

    INVARIANT: consecutive entries in ``path`` must be 4-neighbours (orthogonally
    adjacent). Solder cannot reliably span a diagonal gap — at 2.54 mm pitch the
    orthogonal pad-edge gap is around 0.6 mm, while the diagonal one is nearer 1.7 mm.
    """

    id: ConductorId
    path: tuple[HoleCoord, ...]
    buildup: SolderBuildup = "normal"
    spine: SpineSpec | None = None
    net_id: NetId | None = None
    layer_z: int = 0
    kind: Literal["solder-trace", "solder-trace-wired"] = "solder-trace"
    side: Literal["bottom"] = "bottom"


@dataclass(frozen=True, slots=True)
class WireConductor:
    id: ConductorId
    path: tuple[HoleCoord, ...]
    kind: Literal["bare-wire", "insulated-wire", "top-jumper"] = "bare-wire"
    side: BoardSide = "bottom"
    gauge_awg: int | None = None
    #: Insulation colour, used by the cut list and the guide's colour convention.
    color: str | None = None
    net_id: NetId | None = None
    layer_z: int = 0


@dataclass(frozen=True, slots=True)
class LeadBendConductor:
    id: ConductorId
    path: tuple[HoleCoord, ...]
    component_id: ComponentId
    pin_number: str
    net_id: NetId | None = None
    layer_z: int = 0
    kind: Literal["lead-bend"] = "lead-bend"
    side: Literal["bottom"] = "bottom"


@dataclass(frozen=True, slots=True)
class StripConductor:
    id: ConductorId
    path: tuple[HoleCoord, ...]
    net_id: NetId | None = None
    layer_z: int = 0
    kind: Literal["strip"] = "strip"
    side: BoardSide = "bottom"


Conductor: TypeAlias = (
    SolderTraceConductor | WireConductor | LeadBendConductor | StripConductor
)


@dataclass(frozen=True, slots=True)
class TrackCut:
    """Stripboard track cut (v2)."""

    id: str
    at: HoleCoord


# ---------------------------------------------------------------------------
# Nets
# ---------------------------------------------------------------------------

NetId: TypeAlias = str

NetClass: TypeAlias = Literal["power", "ground", "signal"]


@dataclass(frozen=True, slots=True, order=True)
class NetNode:
    """A node in the schematic netlist: one pin of one component."""

    component_ref: str
    pin: str


@dataclass(frozen=True, slots=True)
class Net:
    """A net as declared by the schematic.

    This is the *intent*; what the board actually connects is derived from the
    conductors by the connectivity engine. Comparing the two is LVS.
    """

    id: NetId
    name: str
    nodes: tuple[NetNode, ...]
    net_class: NetClass = "signal"
    #: Expected current, if the user supplied it. Drives current-capacity DRC.
    current_a: float | None = None
    #: Nominal voltage, if supplied. Drives creepage DRC.
    voltage_v: float | None = None


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------

#: Bumped whenever a migration is needed. Persisted in the project file.
DOCUMENT_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class DocumentMeta:
    name: str
    #: ISO 8601. Set by the host, never by the engine, which must stay deterministic.
    created: str
    modified: str


@dataclass(frozen=True, slots=True)
class PerfDocument:
    meta: DocumentMeta
    board: Board
    components: tuple[ComponentInstance, ...] = ()
    conductors: tuple[Conductor, ...] = ()
    cuts: tuple[TrackCut, ...] = ()
    #: Schematic intent, imported from a netlist. Empty until a netlist is loaded.
    nets: tuple[Net, ...] = ()
    format_version: int = DOCUMENT_FORMAT_VERSION


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def is_solder_trace(c: Conductor) -> bool:
    return c.kind in ("solder-trace", "solder-trace-wired")


def is_wire(c: Conductor) -> bool:
    return c.kind in ("bare-wire", "insulated-wire", "top-jumper")


def is_lead_bend(c: Conductor) -> bool:
    return c.kind == "lead-bend"


def is_crossing_blocked(c: Conductor) -> bool:
    """Conductors occupying the copper plane, which therefore cannot cross one another.

    Insulated wire and top jumpers are excluded: they may pass over.
    """
    return c.kind in ("solder-trace", "solder-trace-wired", "bare-wire", "lead-bend", "strip")


def contacts_every_path_hole(c: Conductor) -> bool:
    """Whether every hole along ``path`` is an electrical contact.

    A solder trace is soldered down at each pad it crosses. A wire is soldered only at
    its two endpoints and merely passes over the holes between. This single distinction
    is the crux of the connectivity engine, and getting it wrong silently produces a
    board that is wired differently from what the screen shows.
    """
    return c.kind in ("solder-trace", "solder-trace-wired", "strip")
