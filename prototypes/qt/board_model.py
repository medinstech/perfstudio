"""Read-only view of a .perf document.

This is NOT a port of the engine. It is the smallest set of dataclasses needed to draw
a board that the TypeScript engine produced. The whole point of this prototype is to
evaluate Qt and VTK as a UI stack; duplicating engine logic here would prove nothing
and would immediately start drifting.

Geometry conventions are copied from packages/core/src/geometry.ts and nowhere else:
  hole {col,row} -> mm is (col*pitch, row*pitch), hole 0,0 at the origin
  board size is cols*pitch  (substrate, half a pitch past the outer holes)
  hole span is (cols-1)*pitch  (first to last hole centre — what mirroring uses)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

PITCH_DEFAULT = 2.54


# --------------------------------------------------------------------------- board


@dataclass(frozen=True)
class Board:
    type: str
    cols: int
    rows: int
    pitch: float
    thickness: float
    material: str
    pad_diameter: float
    drill_diameter: float

    @property
    def size_mm(self) -> tuple[float, float]:
        """Physical substrate: half a pitch past the outermost hole centres."""
        return self.cols * self.pitch, self.rows * self.pitch

    @property
    def hole_span_mm(self) -> tuple[float, float]:
        """First hole centre to last hole centre. Different from size_mm on purpose."""
        return max(0, self.cols - 1) * self.pitch, max(0, self.rows - 1) * self.pitch

    def hole_to_mm(self, col: int, row: int) -> tuple[float, float]:
        return col * self.pitch, row * self.pitch


def hole_ref(col: int, row: int) -> str:
    """Spreadsheet-style hole address, the language the soldering guide speaks."""
    letters = ""
    n = col + 1
    while n > 0:
        n -= 1
        letters = chr(65 + n % 26) + letters
        n //= 26
    return f"{letters}{row + 1}"


# ----------------------------------------------------------------------- footprints


@dataclass(frozen=True)
class Pin:
    number: str
    d_col: int
    d_row: int


@dataclass(frozen=True)
class Footprint:
    id: str
    name: str
    pins: tuple[Pin, ...]
    body_outline: tuple[tuple[float, float], ...]
    body_height: float
    archetype: str
    dims: dict[str, float]
    polarized: bool


# ----------------------------------------------------------------------- components


@dataclass
class Component:
    id: str
    ref: str
    value: str
    footprint_id: str
    col: int
    row: int
    rotation: int
    mirrored: bool
    locked: bool

    def transform_offset(self, x: float, y: float) -> tuple[float, float]:
        """Mirror about the vertical axis first, then rotate clockwise in 90 deg steps.

        Copied from core's transformOffset. If this ever disagrees with the engine, a
        part is drawn in one place and wired in another, so it is worth stating loudly.
        """
        if self.mirrored:
            x = -x
        for _ in range((self.rotation // 90) % 4):
            x, y = -y, x
        return x + 0.0, y + 0.0

    def pin_holes(self, fp: Footprint) -> list[tuple[Pin, int, int]]:
        out = []
        for pin in fp.pins:
            dx, dy = self.transform_offset(pin.d_col, pin.d_row)
            out.append((pin, self.col + int(dx), self.row + int(dy)))
        return out

    def outline_mm(self, fp: Footprint, board: Board) -> list[tuple[float, float]]:
        ax, ay = board.hole_to_mm(self.col, self.row)
        pts = []
        for px, py in fp.body_outline:
            dx, dy = self.transform_offset(px, py)
            pts.append((ax + dx, ay + dy))
        return pts


# ----------------------------------------------------------------------- conductors


@dataclass(frozen=True)
class Conductor:
    id: str
    kind: str
    path: tuple[tuple[int, int], ...]
    side: str
    layer_z: int
    buildup: str | None = None
    spine: dict[str, Any] | None = None
    gauge_awg: int | None = None
    color: str | None = None

    @property
    def contacts_every_hole(self) -> bool:
        """A solder trace is soldered at every pad it crosses; a wire only at its ends.

        This is the central distinction in the whole data model, so the 3D and 2D views
        both have to render it differently — beads all along a trace, fillets only at a
        wire's endpoints.
        """
        return self.kind in ("solder-trace", "solder-trace-wired", "strip")


@dataclass(frozen=True)
class Net:
    id: str
    name: str
    net_class: str
    nodes: tuple[tuple[str, str], ...]


# ------------------------------------------------------------------------- document


@dataclass
class Document:
    board: Board
    components: list[Component]
    conductors: list[Conductor]
    nets: list[Net]
    footprints: dict[str, Footprint] = field(default_factory=dict)

    def footprint(self, fid: str) -> Footprint | None:
        return self.footprints.get(fid)

    def component_by_ref(self, ref: str) -> Component | None:
        return next((c for c in self.components if c.ref == ref), None)


def _footprint(raw: dict[str, Any]) -> Footprint:
    return Footprint(
        id=raw["id"],
        name=raw["name"],
        pins=tuple(Pin(p["number"], p["dCol"], p["dRow"]) for p in raw["pins"]),
        body_outline=tuple((p["x"], p["y"]) for p in raw["bodyOutline"]),
        body_height=raw["bodyHeight"],
        archetype=raw["body"]["archetype"],
        dims=dict(raw["body"].get("dims", {})),
        polarized=raw.get("polarized", False),
    )


def load(perf_path: str | Path, footprints_path: str | Path) -> Document:
    perf = json.loads(Path(perf_path).read_text(encoding="utf-8"))
    fps_raw = json.loads(Path(footprints_path).read_text(encoding="utf-8"))

    b = perf["board"]
    board = Board(
        type=b["type"],
        cols=b["cols"],
        rows=b["rows"],
        pitch=b.get("pitch", PITCH_DEFAULT),
        thickness=b.get("thickness", 1.6),
        material=b.get("material", "FR4"),
        pad_diameter=b.get("padDiameter", 1.9),
        drill_diameter=b.get("drillDiameter", 1.0),
    )

    components = [
        Component(
            id=c["id"],
            ref=c["ref"],
            value=c["value"],
            footprint_id=c["footprintId"],
            col=c["anchor"]["col"],
            row=c["anchor"]["row"],
            rotation=c.get("rotation", 0),
            mirrored=c.get("mirrored", False),
            locked=c.get("locked", False),
        )
        for c in perf["components"]
    ]

    conductors = [
        Conductor(
            id=c["id"],
            kind=c["kind"],
            path=tuple((h["col"], h["row"]) for h in c["path"]),
            side=c["side"],
            layer_z=c.get("layerZ", 0),
            buildup=c.get("buildup"),
            spine=c.get("spine"),
            gauge_awg=c.get("gaugeAwg"),
            color=c.get("color"),
        )
        for c in perf["conductors"]
    ]

    nets = [
        Net(
            id=n["id"],
            name=n["name"],
            net_class=n.get("class", "signal"),
            nodes=tuple((nd["componentRef"], nd["pin"]) for nd in n["nodes"]),
        )
        for n in perf.get("nets", [])
    ]

    return Document(
        board=board,
        components=components,
        conductors=conductors,
        nets=nets,
        footprints={k: _footprint(v) for k, v in fps_raw.items() if v},
    )


def net_of_pin(doc: Document) -> dict[tuple[str, str], Net]:
    return {(ref, pin): n for n in doc.nets for (ref, pin) in n.nodes}


def iter_pin_positions(doc: Document) -> Iterable[tuple[Component, Pin, int, int]]:
    for comp in doc.components:
        fp = doc.footprint(comp.footprint_id)
        if fp is None:
            continue
        for pin, col, row in comp.pin_holes(fp):
            yield comp, pin, col, row
