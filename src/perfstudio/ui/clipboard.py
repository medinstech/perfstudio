"""Copying a block of board and pasting it back.

A perfboard project is repetitive in a way a PCB is not: eight identical channels, four
identical input stages, the same RC pair at every op-amp. Before this, the only way to
build the second one was to place every part again by hand.

WHAT A BLOCK IS: some parts and the copper between them, which is what
``commands.PlaceBlockPayload`` places in ONE command. That matters more here than it
looks -- a paste that arrived as two commands would put a state on the undo stack nobody
chose, the parts down with their wiring gone, one Ctrl+Z from a board that looks finished
and is not.

WHY JSON ON THE SYSTEM CLIPBOARD rather than a variable on the window: it crosses
documents and crosses two running copies of the application, which is exactly the
"channel 1 into this other board" case. It is also readable, so a block can be pasted
into a bug report, and this project already keeps its documents in diffable JSON for the
same reason.

NO WIDGETS HERE. The window owns the actual clipboard, the pointer and the status bar;
this module is text in, command payload out, so the interesting half is tested by calling
it -- the same split ``mcp/session.py`` and ``mcp/server.py`` have. (It reaches into
``view2d`` for ``next_reference``, which is where the naming rule lives and is a fact
about documents rather than about drawing.)

THREE THINGS ARE DELIBERATELY NOT CARRIED OVER:

  * **The net claim.** Copper is copied without its ``net_id``. A copy of R1 is not R1,
    so its copper is not on R1's net -- and claiming otherwise would tell LVS the new
    block is wired to a schematic that has never heard of it. Unclaimed copper is also
    the one kind rip-up and the stale-conductor cleanup both promise never to touch.
  * **The lock.** A pasted part is one you are still positioning.
  * **A lead bend whose part was not copied.** It is a leg of that part, and a copy of it
    would be a leg of nothing. The caller is told how many were left behind rather than
    finding out from DRC.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any, TypeGuard, cast

from perfstudio.commands import (
    NewConductor,
    NewLeadBendConductor,
    NewSolderTraceConductor,
    NewStripConductor,
    NewWireConductor,
    PlaceBlockPayload,
    PlaceComponentPayload,
    create_document_id_generator,
)
from perfstudio.geometry import is_inside_board
from perfstudio.model import (
    ComponentInstance,
    Conductor,
    HoleCoord,
    LeadBendConductor,
    PerfDocument,
    Rotation,
    SolderTraceConductor,
    StripConductor,
    WireConductor,
)

from .view2d import next_reference

#: What the clipboard text announces itself as. Anything else on the clipboard -- a URL,
#: a line of code, a screenshot -- is not a block, and Paste must say so rather than
#: raising out of json.loads with a message about column 1.
CLIPBOARD_KIND = "perfstudio-block"

#: Bumped only if the shape below stops being readable by an older build. Nothing has
#: needed it yet; it exists because this text outlives the session that produced it.
CLIPBOARD_VERSION = 1

#: A block with nothing to say about where it came from starts at the top-left. Hoisted
#: out of the dataclass below because a constructor call in a field default is a trap in
#: general -- it is evaluated once at class definition -- even where the value is frozen
#: and sharing it is harmless.
BLOCK_ORIGIN = HoleCoord(0, 0)


@dataclass(frozen=True, slots=True)
class Block:
    """A copied piece of board, positioned relative to its own top-left corner.

    Relative, so where it was copied from stops mattering the moment it is on the
    clipboard: a block cut from the bottom-right of one board pastes onto the top-left of
    another, and the same text pastes at any offset.
    """

    components: tuple[ComponentInstance, ...]
    conductors: tuple[Conductor, ...]
    #: Where it was cut from, which is only ever a DEFAULT: it is where a paste lands
    #: when nothing better has been pointed at, so Ctrl+C Ctrl+V puts the copy beside
    #: the original instead of in the corner of the board.
    origin: HoleCoord = BLOCK_ORIGIN
    #: Lead bends dropped on the way in because their part was not in the selection.
    orphaned_lead_bends: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.components and not self.conductors

    def size(self) -> tuple[int, int]:
        """Width and height in holes, for a caller deciding whether it will fit."""
        cols, rows = _extent(self.components, self.conductors)
        return cols, rows


def _is_int(value: Any) -> TypeGuard[int]:
    """A real integer. ``bool`` is a subclass of ``int`` in Python and is not one here --
    the same check the commands make of their own payloads, for the same reason."""
    return isinstance(value, int) and not isinstance(value, bool)


def _hole_of(value: Any) -> HoleCoord | None:
    if not isinstance(value, dict):
        return None
    col, row = value.get("col"), value.get("row")
    if not _is_int(col) or not _is_int(row):
        return None
    return HoleCoord(col, row)


def _extent(
    components: Sequence[ComponentInstance], conductors: Sequence[Conductor]
) -> tuple[int, int]:
    holes = [c.anchor for c in components] + [h for c in conductors for h in c.path]
    if not holes:
        return 0, 0
    return (
        max(h.col for h in holes) - min(h.col for h in holes) + 1,
        max(h.row for h in holes) - min(h.row for h in holes) + 1,
    )


def _origin(
    components: Sequence[ComponentInstance], conductors: Sequence[Conductor]
) -> HoleCoord:
    """The block's own top-left, which every coordinate in it is written relative to.

    The anchors and the copper both count: a solder trace running left of the leftmost
    part is part of the block, and measuring from the parts alone would push it off the
    board's left edge on paste.
    """
    holes = [c.anchor for c in components] + [h for c in conductors for h in c.path]
    if not holes:
        return BLOCK_ORIGIN
    return HoleCoord(min(h.col for h in holes), min(h.row for h in holes))


# ---------------------------------------------------------------------------
# Board -> text
# ---------------------------------------------------------------------------


def block_to_json(
    doc: PerfDocument,
    component_ids: Iterable[str],
    conductor_ids: Iterable[str] = (),
) -> str:
    """The selected parts and copper as clipboard text.

    Document order, not selection order, so copying the same block twice by two different
    routes produces the same text -- which is what makes this diffable and testable.
    """
    wanted_components = set(component_ids)
    wanted_conductors = set(conductor_ids)
    components = tuple(c for c in doc.components if c.id in wanted_components)
    conductors = tuple(c for c in doc.conductors if c.id in wanted_conductors)

    refs = {c.id: c.ref for c in components}
    kept: list[Conductor] = []
    orphans = 0
    for conductor in conductors:
        if isinstance(conductor, LeadBendConductor) and conductor.component_id not in refs:
            orphans += 1
            continue
        kept.append(conductor)

    origin = _origin(components, kept)
    payload = {
        "kind": CLIPBOARD_KIND,
        "version": CLIPBOARD_VERSION,
        "from": {"col": origin.col, "row": origin.row},
        "orphanedLeadBends": orphans,
        "components": [
            {
                "ref": c.ref,
                "value": c.value,
                "footprintId": c.footprint_id,
                "at": _relative(c.anchor, origin),
                "rotation": c.rotation,
                "mirrored": c.mirrored,
            }
            for c in components
        ],
        "conductors": [_conductor_to_json(c, origin, refs) for c in kept],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _relative(hole: HoleCoord, origin: HoleCoord) -> dict[str, int]:
    return {"col": hole.col - origin.col, "row": hole.row - origin.row}


def _conductor_to_json(
    conductor: Conductor, origin: HoleCoord, refs: dict[str, str]
) -> dict[str, Any]:
    """One conductor, minus its id and its net claim.

    A lead bend names its part by REFERENCE rather than by id: ids are generated per
    document and mean nothing in the one this is pasted into, while "R4" is the same part
    in the block's own terms.
    """
    out: dict[str, Any] = {
        "kind": conductor.kind,
        "side": conductor.side,
        "path": [_relative(h, origin) for h in conductor.path],
        "layerZ": conductor.layer_z,
    }
    if isinstance(conductor, SolderTraceConductor):
        out["buildup"] = conductor.buildup
        if conductor.spine is not None:
            out["spine"] = {
                "material": conductor.spine.material,
                "gauge": conductor.spine.gauge,
            }
    elif isinstance(conductor, WireConductor):
        if conductor.gauge_awg is not None:
            out["gaugeAwg"] = conductor.gauge_awg
        if conductor.color is not None:
            out["color"] = conductor.color
    elif isinstance(conductor, LeadBendConductor):
        out["componentRef"] = refs[conductor.component_id]
        out["pinNumber"] = conductor.pin_number
    return out


# ---------------------------------------------------------------------------
# Text -> block
# ---------------------------------------------------------------------------


def block_from_json(text: str) -> Block | None:
    """Read clipboard text back, or None if it is not a block.

    None rather than an exception because the clipboard usually holds something else
    entirely -- a URL, a line of code, whatever was copied last -- and "there is no block
    on the clipboard" is an ordinary answer to Paste, not an error to report.

    Anything malformed INSIDE a block is skipped rather than failing the paste: the parts
    that survived are still the block the user copied, and refusing all of them over one
    bad member is the less useful of the two answers.
    """
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("kind") != CLIPBOARD_KIND:
        return None

    components: list[ComponentInstance] = []
    for index, item in enumerate(raw.get("components") or []):
        parsed = _component_from_json(item, index)
        if parsed is not None:
            components.append(parsed)

    by_ref = {c.ref: c.id for c in components}
    conductors: list[Conductor] = []
    orphans = raw.get("orphanedLeadBends")
    for index, item in enumerate(raw.get("conductors") or []):
        parsed_conductor = _conductor_from_json(item, index, by_ref)
        if parsed_conductor is not None:
            conductors.append(parsed_conductor)

    return Block(
        components=tuple(components),
        conductors=tuple(conductors),
        origin=_hole_of(raw.get("from")) or BLOCK_ORIGIN,
        orphaned_lead_bends=orphans if _is_int(orphans) else 0,
    )


def _component_from_json(item: Any, index: int) -> ComponentInstance | None:
    if not isinstance(item, dict):
        return None
    at = _hole_of(item.get("at"))
    ref, footprint_id = item.get("ref"), item.get("footprintId")
    if at is None or not isinstance(ref, str) or not isinstance(footprint_id, str):
        return None
    rotation = item.get("rotation", 0)
    if rotation not in (0, 90, 180, 270):
        rotation = 0
    value = item.get("value")
    return ComponentInstance(
        # The id is the block's own, and is thrown away on paste: it exists so a lead
        # bend inside the block has something to point at before the document does.
        id=f"block-cmp-{index}",
        ref=ref,
        value=value if isinstance(value, str) else "",
        footprint_id=footprint_id,
        anchor=at,
        rotation=cast(Rotation, rotation),
        mirrored=bool(item.get("mirrored")),
        locked=False,
    )


def _conductor_from_json(item: Any, index: int, by_ref: dict[str, str]) -> Conductor | None:
    if not isinstance(item, dict):
        return None
    path = tuple(h for h in (_hole_of(p) for p in item.get("path") or []) if h is not None)
    if len(path) != len(item.get("path") or []) or len(path) < 2:
        return None

    kind = item.get("kind")
    side = item.get("side")
    layer_z = item.get("layerZ", 0)
    if not _is_int(layer_z):
        layer_z = 0
    id_ = f"block-cond-{index}"

    if kind in ("solder-trace", "solder-trace-wired"):
        buildup = item.get("buildup")
        return SolderTraceConductor(
            id=id_,
            kind=kind,
            path=path,
            buildup=cast(Any, buildup if buildup in ("light", "normal", "heavy") else "normal"),
            spine=None,
            layer_z=layer_z,
        )
    if kind in ("bare-wire", "insulated-wire", "top-jumper"):
        gauge = item.get("gaugeAwg")
        color = item.get("color")
        return WireConductor(
            id=id_,
            kind=kind,
            path=path,
            side=cast(Any, side if side in ("top", "bottom") else "bottom"),
            gauge_awg=gauge if isinstance(gauge, int) and not isinstance(gauge, bool) else None,
            color=color if isinstance(color, str) else None,
            layer_z=layer_z,
        )
    if kind == "lead-bend":
        ref, pin = item.get("componentRef"), item.get("pinNumber")
        owner = by_ref.get(ref) if isinstance(ref, str) else None
        if owner is None or not isinstance(pin, str):
            return None
        return LeadBendConductor(
            id=id_, path=path, component_id=owner, pin_number=pin, layer_z=layer_z
        )
    if kind == "strip":
        return StripConductor(id=id_, path=path, layer_z=layer_z)
    return None


# ---------------------------------------------------------------------------
# Block -> a command the bus will take
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Paste:
    """A block turned into one command, and what to say about it afterwards."""

    payload: PlaceBlockPayload
    at: HoleCoord
    #: Conductors left out because the offset would have put them off the board.
    dropped_conductors: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.payload.components and not self.payload.conductors


def paste_position(doc: PerfDocument, block: Block, near: HoleCoord | None = None) -> HoleCoord:
    """Where a paste lands: beside what is already there rather than on top of it.

    Stepping diagonally one hole at a time from the requested corner until no part's
    anchor is already sitting there. Counted from the DOCUMENT rather than from a paste
    counter for the reason ``next_reference`` is: a hidden counter disagrees with the
    board after an undo, and nobody can see why.

    Falls back to the requested corner once the block would run off the board -- the
    command refuses an off-board anchor with a message that names the part, which is a
    better answer than this function quietly walking a block into the far corner.
    """
    board = doc.board
    cols, rows = block.size()
    start = near if near is not None else block.origin
    taken = {(c.anchor.col, c.anchor.row) for c in doc.components}
    anchors = [c.anchor for c in block.components]

    at = start
    for _ in range(max(board.cols, board.rows)):
        if not is_inside_board(HoleCoord(at.col + cols - 1, at.row + rows - 1), board):
            return start
        if not any((at.col + a.col, at.row + a.row) in taken for a in anchors):
            return at
        at = HoleCoord(at.col + 1, at.row + 1)
    return start


def paste_payload(doc: PerfDocument, block: Block, at: HoleCoord, label: str = "") -> Paste:
    """One ``block.place`` command that puts this block on this board at this hole.

    References are minted from the document one at a time -- R4, R5, R6 -- rather than
    kept from the block, because the originals are still on the board this is being
    pasted onto. Ids come from a generator seeded from the document, the same one the bus
    would have used, so a pasted lead bend can name the part it belongs to before that
    part has been placed.

    Copper that would land off the board is dropped rather than refused: the parts are
    what the user is placing, and a block pasted near an edge with one trace hanging over
    it is still the paste they asked for. The count comes back so it can be said out loud.
    """
    next_id = create_document_id_generator(doc)
    placements: list[PlaceComponentPayload] = []
    ids_by_block_id: dict[str, str] = {}
    # The document does not have the new refs until the command lands, so the running
    # tally of what this block has already claimed is kept here. Without it a block of
    # three resistors would ask for R4 three times and the bus would refuse the block.
    claimed = doc
    for component in block.components:
        ref = next_reference(claimed, component.footprint_id)
        id_ = next_id("cmp")
        ids_by_block_id[component.id] = id_
        placed = replace(
            component,
            id=id_,
            ref=ref,
            anchor=HoleCoord(at.col + component.anchor.col, at.row + component.anchor.row),
        )
        claimed = replace(claimed, components=(*claimed.components, placed))
        placements.append(
            PlaceComponentPayload(
                ref=ref,
                value=component.value,
                footprint_id=component.footprint_id,
                anchor=placed.anchor,
                rotation=component.rotation,
                mirrored=component.mirrored,
                id=id_,
            )
        )

    conductors: list[NewConductor] = []
    dropped = 0
    for conductor in block.conductors:
        path = tuple(
            HoleCoord(at.col + h.col, at.row + h.row) for h in conductor.path
        )
        if not all(is_inside_board(h, doc.board) for h in path):
            dropped += 1
            continue
        spec = _new_conductor(conductor, path, ids_by_block_id)
        if spec is None:
            dropped += 1
            continue
        conductors.append(spec)

    return Paste(
        payload=PlaceBlockPayload(
            components=tuple(placements),
            conductors=tuple(conductors),
            label=label or _describe(len(placements), len(conductors)),
        ),
        at=at,
        dropped_conductors=dropped,
    )


def _describe(parts: int, copper: int) -> str:
    if parts and copper:
        return f"Paste {parts} part(s) and {copper} connection(s)"
    if parts:
        return f"Paste {parts} part(s)"
    return f"Paste {copper} connection(s)"


def _new_conductor(
    conductor: Conductor, path: tuple[HoleCoord, ...], ids_by_block_id: dict[str, str]
) -> NewConductor | None:
    """The clipboard's conductor as a new one, translated, and with no net claim."""
    if isinstance(conductor, SolderTraceConductor):
        return NewSolderTraceConductor(
            path=path, kind=conductor.kind, buildup=conductor.buildup, spine=conductor.spine
        )
    if isinstance(conductor, WireConductor):
        return NewWireConductor(
            path=path,
            kind=conductor.kind,
            side=conductor.side,
            gauge_awg=conductor.gauge_awg,
            color=conductor.color,
        )
    if isinstance(conductor, LeadBendConductor):
        owner = ids_by_block_id.get(conductor.component_id)
        return (
            None
            if owner is None
            else NewLeadBendConductor(
                path=path, component_id=owner, pin_number=conductor.pin_number
            )
        )
    if isinstance(conductor, StripConductor):
        return NewStripConductor(path=path)
    return None
