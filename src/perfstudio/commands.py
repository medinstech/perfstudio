"""The standard command set.

Every mutation of a PerfDocument is one of these. The GUI, the CLI, the MCP server
and a replayed journal all go through the same list, which is what keeps undo/redo,
macro recording and agent-driven editing consistent with each other (PLAN.md Sec 8.1).

DIVISION OF RESPONSIBILITY -- worth being precise about, because the temptation is to
validate everything here:

  Commands enforce DOCUMENT INTEGRITY. Ids are unique, references resolve, paths lie
  on the board, and the invariants declared in model.py hold. A document that fails
  any of these is not a document, so these are hard errors and the mutation is
  refused (a handler raises CommandError; CommandBus.dispatch catches it and turns it
  into a structured DispatchResult -- it never propagates as an exception).

  DRC reports DESIGN QUALITY. Overlapping bodies, solder-trace proximity risk,
  inadequate current capacity. These are all legal documents that describe a board
  you probably do not want to build, so they are reported, not refused.

That split is why commands need geometry but not the footprint library: whether a
part's pins land somewhere sensible is a design question, and DRC owns it.

Nothing here touches ``meta.modified``. Core is deterministic and has no clock; the
host stamps timestamps when it saves.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .command import CommandContext, CommandDefinition, CommandError, CommandRegistry
from .geometry import coord_to_hole_ref, is_inside_board, validate_orthogonal_chain
from .model import (
    DOCUMENT_FORMAT_VERSION,
    VALID_ROTATIONS,
    Board,
    BoardSide,
    ComponentId,
    ComponentInstance,
    Conductor,
    ConductorId,
    ConductorKind,
    DocumentMeta,
    HoleCoord,
    LeadBendConductor,
    Net,
    PerfDocument,
    Rotation,
    SolderBuildup,
    SolderTraceConductor,
    SpineSpec,
    StripConductor,
    TrackCut,
    WireConductor,
)

# ---------------------------------------------------------------------------
# Document construction
# ---------------------------------------------------------------------------

DEFAULT_BOARD = Board(
    type="pad-per-hole",
    cols=60,
    rows=40,
    pitch=2.54,
    thickness=1.6,
    material="FR4",
    pad_diameter=1.9,
    drill_diameter=1.0,
)


def create_empty_document(meta: DocumentMeta, board: Board = DEFAULT_BOARD) -> PerfDocument:
    """A blank document.

    ``meta.created``/``meta.modified`` are supplied by the caller because core must
    not read a clock -- see the module note above.
    """
    return PerfDocument(
        meta=meta,
        board=board,
        components=(),
        conductors=(),
        cuts=(),
        nets=(),
        format_version=DOCUMENT_FORMAT_VERSION,
    )


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------


def _is_plain_int(value: object) -> bool:
    """True integers only -- ``bool`` is a subclass of ``int`` in Python and must not
    silently pass an integer check."""
    return isinstance(value, int) and not isinstance(value, bool)


def require_component(doc: PerfDocument, id_: ComponentId) -> ComponentInstance:
    for c in doc.components:
        if c.id == id_:
            return c
    raise CommandError("component-not-found", f'No component with id "{id_}".')


def require_conductor(doc: PerfDocument, id_: ConductorId) -> Conductor:
    for c in doc.conductors:
        if c.id == id_:
            return c
    raise CommandError("conductor-not-found", f'No conductor with id "{id_}".')


def assert_rotation(rotation: int) -> None:
    if rotation not in VALID_ROTATIONS:
        raise CommandError(
            "invalid-rotation", f"Rotation must be 0, 90, 180 or 270; got {rotation}."
        )


def assert_hole_on_board(hole: HoleCoord, board: Board, what: str) -> None:
    if not _is_plain_int(hole.col) or not _is_plain_int(hole.row):
        raise CommandError("invalid-hole", f"{what} must have integer col/row.")
    if not is_inside_board(hole, board):
        raise CommandError(
            "off-board",
            f"{what} {coord_to_hole_ref(hole)} is outside the {board.cols}x{board.rows} board.",
        )


def assert_valid_path(path: tuple[HoleCoord, ...], kind: ConductorKind, board: Board) -> None:
    """Path checks common to every conductor: on the board, non-empty, and -- for
    solder traces -- an unbroken chain of orthogonal neighbours, since solder cannot
    reliably span a diagonal gap (model.py conductor path invariant, PLAN.md Sec 4.6)."""
    if len(path) < 2:
        raise CommandError("path-too-short", "A conductor path needs at least 2 holes.")
    for hole in path:
        assert_hole_on_board(hole, board, "Conductor path hole")
    if kind in ("solder-trace", "solder-trace-wired", "strip"):
        check = validate_orthogonal_chain(path)
        if not check.ok:
            raise CommandError("non-orthogonal-path", check.reason)


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

# TypeScript expresses "a Conductor variant without its id" with a single mapped
# conditional type (`WithoutId<T>`) that distributes over the union. Python dataclasses
# have no equivalent, so each Conductor variant gets an explicit New* counterpart here,
# field-for-field identical minus ``id``. `_finalize_conductor` below is what turns one
# of these into the real, id-bearing Conductor once a command has decided on an id.


@dataclass(frozen=True, slots=True)
class NewSolderTraceConductor:
    path: tuple[HoleCoord, ...]
    buildup: SolderBuildup = "normal"
    spine: SpineSpec | None = None
    net_id: str | None = None
    layer_z: int = 0
    kind: Literal["solder-trace", "solder-trace-wired"] = "solder-trace"
    side: Literal["bottom"] = "bottom"


@dataclass(frozen=True, slots=True)
class NewWireConductor:
    path: tuple[HoleCoord, ...]
    kind: Literal["bare-wire", "insulated-wire", "top-jumper"] = "bare-wire"
    side: BoardSide = "bottom"
    gauge_awg: int | None = None
    color: str | None = None
    net_id: str | None = None
    layer_z: int = 0


@dataclass(frozen=True, slots=True)
class NewLeadBendConductor:
    path: tuple[HoleCoord, ...]
    component_id: ComponentId
    pin_number: str
    net_id: str | None = None
    layer_z: int = 0
    kind: Literal["lead-bend"] = "lead-bend"
    side: Literal["bottom"] = "bottom"


@dataclass(frozen=True, slots=True)
class NewStripConductor:
    path: tuple[HoleCoord, ...]
    net_id: str | None = None
    layer_z: int = 0
    kind: Literal["strip"] = "strip"
    side: BoardSide = "bottom"


NewConductor: TypeAlias = (
    NewSolderTraceConductor | NewWireConductor | NewLeadBendConductor | NewStripConductor
)


def _finalize_conductor(spec: NewConductor, id_: ConductorId) -> Conductor:
    data = {f.name: getattr(spec, f.name) for f in dataclasses.fields(spec)}
    if isinstance(spec, NewSolderTraceConductor):
        return SolderTraceConductor(id=id_, **data)
    if isinstance(spec, NewWireConductor):
        return WireConductor(id=id_, **data)
    if isinstance(spec, NewLeadBendConductor):
        return LeadBendConductor(id=id_, **data)
    if isinstance(spec, NewStripConductor):
        return StripConductor(id=id_, **data)
    raise CommandError("invalid-conductor-kind", f"Unrecognised new-conductor spec: {spec!r}")


@dataclass(frozen=True, slots=True)
class PlaceComponentPayload:
    ref: str
    value: str
    footprint_id: str
    anchor: HoleCoord
    rotation: Rotation | None = None
    mirrored: bool | None = None
    #: Supply to make placement reproducible (e.g. netlist import); otherwise generated.
    id: ComponentId | None = None


@dataclass(frozen=True, slots=True)
class MoveComponentPayload:
    id: ComponentId
    anchor: HoleCoord


@dataclass(frozen=True, slots=True)
class RotateComponentPayload:
    id: ComponentId
    rotation: Rotation


@dataclass(frozen=True, slots=True)
class MirrorComponentPayload:
    id: ComponentId
    mirrored: bool


@dataclass(frozen=True, slots=True)
class UpdateComponentPayload:
    id: ComponentId
    ref: str | None = None
    value: str | None = None
    locked: bool | None = None


@dataclass(frozen=True, slots=True)
class DeleteComponentPayload:
    id: ComponentId


@dataclass(frozen=True, slots=True)
class AddConductorPayload:
    conductor: NewConductor
    id: ConductorId | None = None


@dataclass(frozen=True, slots=True)
class SetConductorPathPayload:
    id: ConductorId
    path: tuple[HoleCoord, ...]


@dataclass(frozen=True, slots=True)
class DeleteConductorPayload:
    id: ConductorId


@dataclass(frozen=True, slots=True)
class SetBoardPayload:
    board: Board


@dataclass(frozen=True, slots=True)
class ImportNetlistPayload:
    nets: tuple[Net, ...]


@dataclass(frozen=True, slots=True)
class AddCutPayload:
    at: HoleCoord
    id: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteCutPayload:
    id: str


# ---------------------------------------------------------------------------
# Component commands
# ---------------------------------------------------------------------------


class _PlaceComponent:
    type = "component.place"

    def apply(
        self, doc: PerfDocument, p: PlaceComponentPayload, ctx: CommandContext
    ) -> PerfDocument:
        assert_hole_on_board(p.anchor, doc.board, f"Anchor for {p.ref}")
        rotation: Rotation = p.rotation if p.rotation is not None else 0
        assert_rotation(rotation)

        if any(c.ref == p.ref for c in doc.components):
            raise CommandError("duplicate-ref", f'A component with ref "{p.ref}" already exists.')
        id_ = p.id if p.id is not None else ctx.next_id("cmp")
        if any(c.id == id_ for c in doc.components):
            raise CommandError("duplicate-id", f'A component with id "{id_}" already exists.')

        component = ComponentInstance(
            id=id_,
            ref=p.ref,
            value=p.value,
            footprint_id=p.footprint_id,
            anchor=p.anchor,
            rotation=rotation,
            mirrored=p.mirrored if p.mirrored is not None else False,
            locked=False,
        )
        return dataclasses.replace(doc, components=doc.components + (component,))

    def describe(self, p: PlaceComponentPayload, doc: PerfDocument) -> str:
        return f"Place {p.ref} at {coord_to_hole_ref(p.anchor)}"


class _MoveComponent:
    type = "component.move"

    def apply(self, doc: PerfDocument, p: MoveComponentPayload, ctx: CommandContext) -> PerfDocument:
        existing = require_component(doc, p.id)
        if existing.locked:
            raise CommandError("component-locked", f"{existing.ref} is locked.")
        assert_hole_on_board(p.anchor, doc.board, f"Anchor for {existing.ref}")
        components = tuple(
            dataclasses.replace(c, anchor=p.anchor) if c.id == p.id else c for c in doc.components
        )
        return dataclasses.replace(doc, components=components)

    def describe(self, p: MoveComponentPayload, doc: PerfDocument) -> str:
        c = next((x for x in doc.components if x.id == p.id), None)
        ref = c.ref if c is not None else p.id
        return f"Move {ref} to {coord_to_hole_ref(p.anchor)}"


class _RotateComponent:
    type = "component.rotate"

    def apply(
        self, doc: PerfDocument, p: RotateComponentPayload, ctx: CommandContext
    ) -> PerfDocument:
        existing = require_component(doc, p.id)
        if existing.locked:
            raise CommandError("component-locked", f"{existing.ref} is locked.")
        assert_rotation(p.rotation)
        components = tuple(
            dataclasses.replace(c, rotation=p.rotation) if c.id == p.id else c
            for c in doc.components
        )
        return dataclasses.replace(doc, components=components)

    def describe(self, p: RotateComponentPayload, doc: PerfDocument) -> str:
        c = next((x for x in doc.components if x.id == p.id), None)
        ref = c.ref if c is not None else p.id
        return f"Rotate {ref} to {p.rotation} degrees"


class _MirrorComponent:
    type = "component.mirror"

    def apply(
        self, doc: PerfDocument, p: MirrorComponentPayload, ctx: CommandContext
    ) -> PerfDocument:
        existing = require_component(doc, p.id)
        if existing.locked:
            raise CommandError("component-locked", f"{existing.ref} is locked.")
        components = tuple(
            dataclasses.replace(c, mirrored=p.mirrored) if c.id == p.id else c
            for c in doc.components
        )
        return dataclasses.replace(doc, components=components)

    def describe(self, p: MirrorComponentPayload, doc: PerfDocument) -> str:
        c = next((x for x in doc.components if x.id == p.id), None)
        ref = c.ref if c is not None else p.id
        return f"{'Mirror' if p.mirrored else 'Unmirror'} {ref}"


class _UpdateComponent:
    type = "component.update"

    def apply(
        self, doc: PerfDocument, p: UpdateComponentPayload, ctx: CommandContext
    ) -> PerfDocument:
        existing = require_component(doc, p.id)
        if p.ref is not None and p.ref != existing.ref:
            if any(c.ref == p.ref for c in doc.components):
                raise CommandError(
                    "duplicate-ref", f'A component with ref "{p.ref}" already exists.'
                )
        components = tuple(
            dataclasses.replace(
                c,
                ref=p.ref if p.ref is not None else c.ref,
                value=p.value if p.value is not None else c.value,
                locked=p.locked if p.locked is not None else c.locked,
            )
            if c.id == p.id
            else c
            for c in doc.components
        )
        return dataclasses.replace(doc, components=components)

    def describe(self, p: UpdateComponentPayload, doc: PerfDocument) -> str:
        c = next((x for x in doc.components if x.id == p.id), None)
        ref = c.ref if c is not None else p.id
        return f"Update {ref}"


class _DeleteComponent:
    type = "component.delete"

    def apply(
        self, doc: PerfDocument, p: DeleteComponentPayload, ctx: CommandContext
    ) -> PerfDocument:
        existing = require_component(doc, p.id)
        if existing.locked:
            raise CommandError("component-locked", f"{existing.ref} is locked.")
        # Lead bends belong to the component; nothing else can own them, so they go
        # too. Wires and traces are deliberately left in place: they may still be
        # wanted, and silently deleting a user's routing is worse than leaving
        # something dangling for DRC and LVS to point at.
        return dataclasses.replace(
            doc,
            components=tuple(c for c in doc.components if c.id != p.id),
            conductors=tuple(
                c
                for c in doc.conductors
                if not (c.kind == "lead-bend" and c.component_id == p.id)  # type: ignore[union-attr]
            ),
        )

    def describe(self, p: DeleteComponentPayload, doc: PerfDocument) -> str:
        c = next((x for x in doc.components if x.id == p.id), None)
        ref = c.ref if c is not None else p.id
        return f"Delete {ref}"


# ---------------------------------------------------------------------------
# Conductor commands
# ---------------------------------------------------------------------------


class _AddConductor:
    type = "conductor.add"

    def apply(self, doc: PerfDocument, p: AddConductorPayload, ctx: CommandContext) -> PerfDocument:
        spec = p.conductor
        assert_valid_path(spec.path, spec.kind, doc.board)

        if isinstance(spec, NewLeadBendConductor):
            require_component(doc, spec.component_id)
        if spec.kind in ("solder-trace", "solder-trace-wired") and spec.side != "bottom":
            raise CommandError("invalid-side", "Solder traces exist on the solder side only.")

        id_ = p.id if p.id is not None else ctx.next_id("cond")
        if any(c.id == id_ for c in doc.conductors):
            raise CommandError("duplicate-id", f'A conductor with id "{id_}" already exists.')

        conductor = _finalize_conductor(spec, id_)
        return dataclasses.replace(doc, conductors=doc.conductors + (conductor,))

    def describe(self, p: AddConductorPayload, doc: PerfDocument) -> str:
        path = p.conductor.path
        span = f" {coord_to_hole_ref(path[0])} to {coord_to_hole_ref(path[-1])}" if path else ""
        return f"Add {p.conductor.kind}{span}"


class _SetConductorPath:
    type = "conductor.setPath"

    def apply(
        self, doc: PerfDocument, p: SetConductorPathPayload, ctx: CommandContext
    ) -> PerfDocument:
        existing = require_conductor(doc, p.id)
        assert_valid_path(p.path, existing.kind, doc.board)
        conductors = tuple(
            dataclasses.replace(c, path=p.path) if c.id == p.id else c for c in doc.conductors
        )
        return dataclasses.replace(doc, conductors=conductors)

    def describe(self, p: SetConductorPathPayload, doc: PerfDocument) -> str:
        return f"Reroute conductor {p.id}"


class _DeleteConductor:
    type = "conductor.delete"

    def apply(
        self, doc: PerfDocument, p: DeleteConductorPayload, ctx: CommandContext
    ) -> PerfDocument:
        require_conductor(doc, p.id)  # Raises if it does not exist.
        return dataclasses.replace(
            doc, conductors=tuple(c for c in doc.conductors if c.id != p.id)
        )

    def describe(self, p: DeleteConductorPayload, doc: PerfDocument) -> str:
        c = next((x for x in doc.conductors if x.id == p.id), None)
        kind = c.kind if c is not None else "conductor"
        return f"Delete {kind} {p.id}"


# ---------------------------------------------------------------------------
# Board, netlist and cuts
# ---------------------------------------------------------------------------


class _SetBoard:
    type = "board.set"

    def apply(self, doc: PerfDocument, p: SetBoardPayload, ctx: CommandContext) -> PerfDocument:
        b = p.board
        if not _is_plain_int(b.cols) or not _is_plain_int(b.rows) or b.cols < 1 or b.rows < 1:
            raise CommandError("invalid-board", "Board must have at least 1 column and 1 row.")
        if not (b.pitch > 0) or not (b.pad_diameter > 0) or not (b.drill_diameter > 0):
            raise CommandError(
                "invalid-board", "Pitch and pad/drill diameters must be positive."
            )
        if b.drill_diameter >= b.pad_diameter:
            raise CommandError("invalid-board", "Drill diameter must be smaller than the pad.")

        # Shrinking the board could strand parts outside it. Refuse rather than
        # silently dropping the user's work, and name the first offender so the
        # message is useful.
        stranded_component = next(
            (c for c in doc.components if not is_inside_board(c.anchor, b)), None
        )
        if stranded_component is not None:
            raise CommandError(
                "would-strand-component",
                f"{stranded_component.ref} at {coord_to_hole_ref(stranded_component.anchor)} "
                f"would fall outside a {b.cols}x{b.rows} board.",
            )
        for cond in doc.conductors:
            stranded = next((h for h in cond.path if not is_inside_board(h, b)), None)
            if stranded is not None:
                raise CommandError(
                    "would-strand-conductor",
                    f"Conductor {cond.id} passes through {coord_to_hole_ref(stranded)}, "
                    f"outside a {b.cols}x{b.rows} board.",
                )

        return dataclasses.replace(doc, board=b)

    def describe(self, p: SetBoardPayload, doc: PerfDocument) -> str:
        return f"Set board to {p.board.cols}x{p.board.rows} {p.board.material}"


class _ImportNetlist:
    type = "netlist.import"

    def apply(
        self, doc: PerfDocument, p: ImportNetlistPayload, ctx: CommandContext
    ) -> PerfDocument:
        seen: set[str] = set()
        for net in p.nets:
            if net.id in seen:
                raise CommandError("duplicate-net-id", f'Duplicate net id "{net.id}".')
            seen.add(net.id)
        # Replaces the schematic intent wholesale. Placement and routing are
        # untouched: re-importing after a schematic edit must not throw away the
        # board.
        return dataclasses.replace(doc, nets=tuple(p.nets))

    def describe(self, p: ImportNetlistPayload, doc: PerfDocument) -> str:
        return f"Import netlist ({len(p.nets)} nets)"


class _AddCut:
    type = "cut.add"

    def apply(self, doc: PerfDocument, p: AddCutPayload, ctx: CommandContext) -> PerfDocument:
        if doc.board.type != "stripboard":
            raise CommandError("not-stripboard", "Track cuts only apply to stripboard.")
        assert_hole_on_board(p.at, doc.board, "Cut")
        id_ = p.id if p.id is not None else ctx.next_id("cut")
        if any(c.id == id_ for c in doc.cuts):
            raise CommandError("duplicate-id", f'A cut with id "{id_}" already exists.')
        cut = TrackCut(id=id_, at=p.at)
        return dataclasses.replace(doc, cuts=doc.cuts + (cut,))

    def describe(self, p: AddCutPayload, doc: PerfDocument) -> str:
        return f"Cut track at {coord_to_hole_ref(p.at)}"


class _DeleteCut:
    type = "cut.delete"

    def apply(self, doc: PerfDocument, p: DeleteCutPayload, ctx: CommandContext) -> PerfDocument:
        if not any(c.id == p.id for c in doc.cuts):
            raise CommandError("cut-not-found", f'No cut with id "{p.id}".')
        return dataclasses.replace(doc, cuts=tuple(c for c in doc.cuts if c.id != p.id))

    def describe(self, p: DeleteCutPayload, doc: PerfDocument) -> str:
        return f"Remove cut {p.id}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

place_component: CommandDefinition[PlaceComponentPayload] = _PlaceComponent()
move_component: CommandDefinition[MoveComponentPayload] = _MoveComponent()
rotate_component: CommandDefinition[RotateComponentPayload] = _RotateComponent()
mirror_component: CommandDefinition[MirrorComponentPayload] = _MirrorComponent()
update_component: CommandDefinition[UpdateComponentPayload] = _UpdateComponent()
delete_component: CommandDefinition[DeleteComponentPayload] = _DeleteComponent()
add_conductor: CommandDefinition[AddConductorPayload] = _AddConductor()
set_conductor_path: CommandDefinition[SetConductorPathPayload] = _SetConductorPath()
delete_conductor: CommandDefinition[DeleteConductorPayload] = _DeleteConductor()
set_board: CommandDefinition[SetBoardPayload] = _SetBoard()
import_netlist: CommandDefinition[ImportNetlistPayload] = _ImportNetlist()
add_cut: CommandDefinition[AddCutPayload] = _AddCut()
delete_cut: CommandDefinition[DeleteCutPayload] = _DeleteCut()

STANDARD_COMMANDS: tuple[CommandDefinition[object], ...] = (
    place_component,
    move_component,
    rotate_component,
    mirror_component,
    update_component,
    delete_component,
    add_conductor,
    set_conductor_path,
    delete_conductor,
    set_board,
    import_netlist,
    add_cut,
    delete_cut,
)


def create_standard_registry() -> CommandRegistry:
    """A registry with every standard command registered."""
    registry = CommandRegistry()
    for definition in STANDARD_COMMANDS:
        registry.register(definition)
    return registry
