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
import math
from dataclasses import dataclass
from typing import Any, Literal

from .command import (
    CommandContext,
    CommandDefinition,
    CommandError,
    CommandRegistry,
    NextId,
    create_id_generator,
)
from .geometry import (
    STANDARD_PRESETS,
    board_edge_margin_mm,
    board_from_preset,
    default_finger_length_mm,
    edge_connector_holes,
    format_hole,
    is_inside_board,
    preset_edge_connectors,
    preset_mounting_holes,
    validate_orthogonal_chain,
)
from .model import (
    DOCUMENT_FORMAT_VERSION,
    VALID_ROTATIONS,
    Board,
    BoardEdge,
    BoardFace,
    BoardSide,
    ComponentId,
    ComponentInstance,
    Conductor,
    ConductorId,
    ConductorKind,
    DocumentMeta,
    EdgeConnector,
    HoleCoord,
    LeadBendConductor,
    Mm,
    MountingHole,
    Net,
    NetClass,
    NetId,
    NetNode,
    PerfDocument,
    Rotation,
    SchematicPart,
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


def create_document_id_generator(doc: PerfDocument) -> NextId:
    """An id source that cannot collide with ids already present in ``doc``.

    A bare ``create_id_generator()`` restarts every counter at zero, which is correct for
    a blank document and WRONG for a loaded one: a file whose conductors are already
    ``cond-1``..``cond-40`` would make the very next ``conductor.add`` fail with
    ``duplicate-id``. Any host that opens a document and then edits it needs this instead.

    Only ids of the shape ``prefix-<digits>`` are read, since those are the ones this
    generator can collide with. Ids from elsewhere -- a netlist's own net names, a
    hand-written id in a project file -- are left alone rather than guessed at.
    """
    highest: dict[str, int] = {}

    def note(id_: str) -> None:
        prefix, separator, suffix = id_.rpartition("-")
        if separator and prefix and suffix.isdigit():
            highest[prefix] = max(highest.get(prefix, 0), int(suffix))

    for component in doc.components:
        note(component.id)
    for conductor in doc.conductors:
        note(conductor.id)
    for cut in doc.cuts:
        note(cut.id)
    # Nets are here because ``net.add`` generates ids the same way the others do. A net
    # named by a netlist ("+5V", "N$3") almost never has this shape and is simply not
    # noted, which is the behaviour described above rather than an exception to it.
    for net in doc.nets:
        note(net.id)

    return create_id_generator(initial=highest)


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


#: The board a new document opens on: the 5 x 7 cm double-sided one, which is the board
#: most people have a stack of.
STARTER_PRESET_NAME = "5 x 7 cm"


def create_starter_document(meta: DocumentMeta) -> PerfDocument:
    """A new document on a board somebody actually sells.

    ``create_empty_document`` gives a bare grid, which is the right *engine* default and
    the wrong thing to open the application on. ``DEFAULT_BOARD`` has no border, so there
    is nowhere on it to print an address: the editor falls back to drawing its own ruler
    outside the board, and the first board a user sees is one that does not exist,
    addressed by an annotation instead of by its own ink -- and carrying nothing at all
    in the 3D view or the 1:1 printout, which have no ruler to fall back on.

    A preset is a PRODUCT, so this returns the whole of one: the grid, the printed
    legend, a finger strip down each of the two edges with room for one, and a screw hole
    in each corner. Built here rather than dispatched as ``board.applyPreset`` because
    there is no history yet to put it in -- a new document should not open with an undo
    step already on the stack.
    """
    preset = next(
        p for p in STANDARD_PRESETS if p.name == STARTER_PRESET_NAME and not p.single_sided
    )
    board = board_from_preset(preset, DEFAULT_BOARD)
    return dataclasses.replace(
        create_empty_document(meta, board),
        edge_connectors=preset_edge_connectors(preset, board),
        mounting_holes=preset_mounting_holes(preset, board),
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


def require_part(doc: PerfDocument, id_: ComponentId) -> SchematicPart:
    for part in doc.parts:
        if part.id == id_:
            return part
    raise CommandError("part-not-found", f'No schematic part with id "{id_}".')


def assert_ref_free(doc: PerfDocument, ref: str, *, ignoring: ComponentId | None = None) -> None:
    """A reference is unique across the BOARD and the DESIGN, not within either one.

    The two lists are the whole reason ``SchematicPart`` is separate from
    ``ComponentInstance``, and one reference naming a thing in each is the way that
    separation would go wrong: every net node is a ``(ref, pin)`` pair, so two R4s means
    a netlist that cannot say which one it wired. Checked in one helper for that reason,
    and the refusal names which side already has it, because "place it" and "rename it"
    are different fixes.
    """
    for component in doc.components:
        if component.ref == ref and component.id != ignoring:
            raise CommandError("duplicate-ref", f'A component with ref "{ref}" is on the board.')
    for part in doc.parts:
        if part.ref == ref and part.id != ignoring:
            raise CommandError(
                "duplicate-ref",
                f'A schematic part with ref "{ref}" is already in the design. '
                f"Place that one instead, or give this a different reference.",
            )


def rename_in_nets(doc: PerfDocument, old_ref: str, new_ref: str) -> tuple[Net, ...]:
    """Carry a part's connections across a rename, or refuse the rename.

    RENAMING TAKES THE WIRING WITH IT. It did not always: a rename used to leave the nets
    naming the old reference, so R1 wired into six nets and then relabelled R7 came out
    the other side connected to nothing, and the properties dialog had to carry a tooltip
    warning people to rename before importing rather than after. That is a wart, not a
    decision -- a reference is the only name a net has for a part, so renaming the part
    IS renaming what the net points at.

    The one thing it must not do is merge two parts by accident. If some net already names
    ``new_ref`` on a pin this part also has, carrying the rename would put one pin on two
    nets, which is the invariant ``assert_pins_free`` exists to hold. That is refused, and
    the refusal names the pins.
    """
    if old_ref == new_ref:
        return doc.nets
    moving = {node.pin for net in doc.nets for node in net.nodes if node.component_ref == old_ref}
    if not moving:
        return doc.nets
    taken = {node.pin for net in doc.nets for node in net.nodes if node.component_ref == new_ref}
    collides = sorted(moving & taken)
    if collides:
        raise CommandError(
            "pin-collision",
            f'Renaming to "{new_ref}" would put {", ".join(f"{new_ref}.{pin}" for pin in collides)} '
            f"on two nets at once, because something already wired those pins under that "
            f"reference.",
        )
    return tuple(
        dataclasses.replace(
            net,
            nodes=tuple(
                dataclasses.replace(node, component_ref=new_ref)
                if node.component_ref == old_ref
                else node
                for node in net.nodes
            ),
        )
        if any(node.component_ref == old_ref for node in net.nodes)
        else net
        for net in doc.nets
    )


def require_conductor(doc: PerfDocument, id_: ConductorId) -> Conductor:
    for c in doc.conductors:
        if c.id == id_:
            return c
    raise CommandError("conductor-not-found", f'No conductor with id "{id_}".')


def require_net(doc: PerfDocument, id_: NetId) -> Net:
    """The net with this id, or a refusal naming the ones there are.

    Nets are the one thing in this document a user addresses by NAME -- the MCP server
    resolves ``"GND"``, DRC and LVS print it -- so a miss lists the names rather than the
    ids, which nobody has ever typed.
    """
    for net in doc.nets:
        if net.id == id_:
            return net
    known = ", ".join(n.name for n in doc.nets) or "(this board has no nets)"
    raise CommandError("net-not-found", f'No net with id "{id_}". Nets on this board: {known}.')


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
            f"{what} {format_hole(hole)} is outside the {board.cols}x{board.rows} board.",
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


def assert_net_name_free(doc: PerfDocument, name: str, ignoring: NetId | None = None) -> str:
    """Check a net name and give it back stripped, or refuse it.

    Uniqueness is integrity rather than taste here, because a net's NAME is its handle
    everywhere outside the engine: the MCP server resolves ``"GND"`` to a net by it, DRC
    and LVS quote it in every message, the build guide prints it on the wire list. Two
    nets called GND make all of those ambiguous and leave one of the pair unaddressable.
    """
    cleaned = name.strip()
    if not cleaned:
        raise CommandError("invalid-net-name", "A net needs a name.")
    for net in doc.nets:
        if net.id != ignoring and net.name == cleaned:
            raise CommandError("duplicate-net-name", f'A net called "{cleaned}" already exists.')
    return cleaned


def assert_electrical(current_a: float | None, voltage_v: float | None) -> None:
    """The two numbers a net may declare about itself.

    Both are optional and both stay refusable: a net carrying 0 A is what ``None`` says,
    and a negative or non-finite one would reach the current-capacity rule as a wire gauge
    nobody can cut. A negative VOLTAGE is ordinary (-12 V rails exist), so only the
    non-finite case is refused there.
    """
    if current_a is not None and not (math.isfinite(current_a) and current_a > 0):
        raise CommandError(
            "invalid-current",
            f"A net's declared current must be a positive number of amps; got {current_a}. "
            f"Pass null to say nothing about it instead.",
        )
    if voltage_v is not None and not math.isfinite(voltage_v):
        raise CommandError(
            "invalid-voltage", f"A net's declared voltage must be a real number; got {voltage_v}."
        )


def assert_pins_free(
    doc: PerfDocument, nodes: tuple[NetNode, ...], joining: NetId | None
) -> tuple[NetNode, ...]:
    """Check pins about to join a net and give them back stripped, or refuse them.

    A pin belongs to exactly one net -- that is what a net is -- so one already claimed is
    refused rather than quietly moved. Moving it would rewrite a net the user did not
    name in a command about a different net, and the disconnect is one call away.

    DELIBERATELY NOT CHECKED: whether the component exists, or whether its footprint has
    that pin. A netlist routinely names parts that are not on the board yet -- importing
    one and then placing what it asks for is a workflow this application already offers --
    so a node with nothing behind it yet is a perfectly good document. ``ratsnest`` reports
    it as an unresolved pin and LVS raises it, which is where it belongs.
    """
    owner: dict[tuple[str, str], Net] = {}
    for net in doc.nets:
        for node in net.nodes:
            owner[(node.component_ref, node.pin)] = net

    cleaned: list[NetNode] = []
    seen: set[tuple[str, str]] = set()
    for node in nodes:
        ref = node.component_ref.strip()
        pin = node.pin.strip()
        if not ref or not pin:
            raise CommandError(
                "invalid-pin", "A net node needs both a component reference and a pin number."
            )
        key = (ref, pin)
        if key in seen:
            raise CommandError("duplicate-pin", f"{ref}.{pin} is listed twice in the same request.")
        seen.add(key)

        holder = owner.get(key)
        if holder is not None and holder.id == joining:
            raise CommandError("duplicate-pin", f'{ref}.{pin} is already on net "{holder.name}".')
        if holder is not None:
            raise CommandError(
                "pin-in-another-net",
                f'{ref}.{pin} already belongs to net "{holder.name}". Disconnect it from '
                f"there first -- a pin can only be on one net.",
            )
        cleaned.append(NetNode(component_ref=ref, pin=pin))

    return tuple(cleaned)


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


type NewConductor = (
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
class ComponentPlacement:
    """Where one component ends up. ``rotation`` of None leaves it as it is."""

    id: ComponentId
    anchor: HoleCoord
    rotation: Rotation | None = None


@dataclass(frozen=True, slots=True)
class MoveComponentsPayload:
    """Several components repositioned as ONE command, so a placer is one undo step.

    The counterpart of ``conductor.addMany`` for placement. An annealing run moves most
    of the board at once and the result only makes sense as a whole -- dispatched part
    by part it would bury the undo stack and let a single Ctrl+Z leave the board in a
    state the optimiser never proposed and nobody chose.
    """

    placements: tuple[ComponentPlacement, ...]
    #: Undo-stack label; without it the entry reads as a bare count.
    label: str | None = None


@dataclass(frozen=True, slots=True)
class PlaceBlockPayload:
    """Several parts AND the copper between them, placed as ONE command.

    What a paste is. A block of perfboard -- an input stage, one channel of eight -- is
    parts and the connections that make them a circuit, and splitting it into
    ``component.placeMany`` followed by ``conductor.addMany`` would put a state on the
    undo stack that nobody ever chose: the parts down with their wiring gone, one Ctrl+Z
    from a board that looks finished and is not.

    Conductors are prepared against the document the components have ALREADY joined, so
    a pasted lead bend can name a part that did not exist when the payload was built.
    All-or-nothing like every other batch here: a duplicate ref or a path off the board
    refuses the whole block rather than leaving half of it down.
    """

    components: tuple[PlaceComponentPayload, ...] = ()
    conductors: tuple[NewConductor, ...] = ()
    #: Optional explicit ids for the conductors, for reproducible replay. Components
    #: carry their own ``id`` field, so this is only needed for the copper.
    conductor_ids: tuple[ConductorId, ...] | None = None
    #: Undo-stack label. A batch cannot tell what it is from its contents, and "Paste 4
    #: part(s)" is a better thing to read than "Place a block of 4 and 3".
    label: str | None = None


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
class UnplaceComponentPayload:
    """Take a part off the board and keep it in the design. The inverse of ``part.place``."""

    id: ComponentId


@dataclass(frozen=True, slots=True)
class AddPartPayload:
    """A part the design has and the board does not yet.

    No anchor and no rotation: those are the questions placement answers, and asking them
    here would be the thing this command exists to avoid.
    """

    ref: str
    footprint_id: str
    value: str = ""
    id: ComponentId | None = None


@dataclass(frozen=True, slots=True)
class UpdatePartPayload:
    id: ComponentId
    #: ``None`` leaves the field alone. A changed ``ref`` carries the part's nets with it.
    ref: str | None = None
    value: str | None = None
    footprint_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeletePartPayload:
    id: ComponentId


@dataclass(frozen=True, slots=True)
class PartPlacement:
    """Where one schematic part is going."""

    id: ComponentId
    anchor: HoleCoord
    rotation: Rotation | None = None
    mirrored: bool | None = None


@dataclass(frozen=True, slots=True)
class PlacePartsPayload:
    """One command however many parts, because placing a whole schematic is one decision.

    A thirty-part circuit dispatched one at a time takes thirty presses of Ctrl+Z to take
    back, and each press leaves a board that is half laid out -- the same reason
    ``block.place`` exists.
    """

    placements: tuple[PartPlacement, ...]
    label: str | None = None


@dataclass(frozen=True, slots=True)
class AddConductorPayload:
    conductor: NewConductor
    id: ConductorId | None = None


@dataclass(frozen=True, slots=True)
class AddConductorsPayload:
    """Several conductors as ONE command, so a planner's output is one undo step.

    Autorouting a board produces dozens of conductors that only make sense together;
    dispatched one at a time they would bury the undo stack and let a user tear the
    result in half with a single Ctrl+Z. Each conductor is still validated exactly as
    ``conductor.add`` validates it, and the whole batch is refused if any member is
    invalid -- a half-applied plan is worse than a rejected one.
    """

    conductors: tuple[NewConductor, ...]
    #: Optional explicit ids, one per conductor, for reproducible replay. Generated when
    #: omitted; supplying the wrong number is an error rather than a silent partial map.
    ids: tuple[ConductorId, ...] | None = None
    #: Undo-stack label. A batch command cannot tell what it is from its contents, and
    #: "Autoroute 6 nets" is a far better thing to read on the undo stack than
    #: "Add 14 conductors", so the caller that knows says so.
    label: str | None = None


@dataclass(frozen=True, slots=True)
class SetConductorPathPayload:
    id: ConductorId
    path: tuple[HoleCoord, ...]


@dataclass(frozen=True, slots=True)
class DeleteConductorPayload:
    id: ConductorId


@dataclass(frozen=True, slots=True)
class DeleteConductorsPayload:
    """Several conductors as ONE command, so a cleanup is one undo step.

    Counterpart to ``AddConductorsPayload``. Clearing the copper a moved part left behind is a
    single decision to the user, and dispatching it one conductor at a time would take as many
    Ctrl+Z presses to reverse as there were conductors.
    """

    ids: tuple[ConductorId, ...]
    #: Undo-stack label; without it the entry reads as a bare count.
    label: str | None = None


@dataclass(frozen=True, slots=True)
class ReplaceConductorsPayload:
    """Remove some conductors and add others, as ONE command.

    What rip-up-and-reroute needs, and the reason it is a single command rather than a
    delete followed by an add: those two are one decision to the user, and splitting them
    means a single Ctrl+Z leaves the board with a net ripped up and nothing put back --
    a state the planner never proposed and nobody chose.
    """

    remove_ids: tuple[ConductorId, ...]
    conductors: tuple[NewConductor, ...]
    #: Optional explicit ids for the new conductors, for reproducible replay.
    ids: tuple[ConductorId, ...] | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class SetBoardPayload:
    board: Board


@dataclass(frozen=True, slots=True)
class SetHeightLimitPayload:
    """``None`` clears the limit, which is a different thing from a limit of zero."""

    height_limit_mm: Mm | None


@dataclass(frozen=True, slots=True)
class ApplyBoardPresetPayload:
    """A whole board product: the stock, and the features it is sold with.

    Separate from ``board.set`` because a preset is not just a size. Choosing "5 x 7 cm,
    double-sided" replaces the grid AND the oblong finger strips down two of its edges AND
    the screw hole in each corner, because that is what arrives in the envelope. Doing it
    as four commands would put four entries in the history for one decision, and leave the
    board describable as a product nobody sells halfway through the undo stack.
    """

    board: Board
    edge_connectors: tuple[EdgeConnector, ...] = ()
    mounting_holes: tuple[MountingHole, ...] = ()
    label: str | None = None


@dataclass(frozen=True, slots=True)
class ImportNetlistPayload:
    nets: tuple[Net, ...]


class _Keep:
    """Sentinel for ``net.update``: leave this field exactly as it is.

    ``None`` cannot carry that meaning here, because for ``current_a`` and ``voltage_v``
    None IS a value -- "this net declares no current" is what silences the current-capacity
    rule. A payload that used None for both would be unable to express one of them.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "KEEP"


KEEP = _Keep()


@dataclass(frozen=True, slots=True)
class AddNetPayload:
    """Declare a net the schematic (or the user) says should exist.

    ``nodes`` is optional because both orders of work are real: name the net and then
    click its pins on the board, or state the whole thing at once from an agent.
    """

    name: str
    net_class: NetClass = "signal"
    nodes: tuple[NetNode, ...] = ()
    #: Expected current. Nothing else in the application can set this, and DRC's
    #: current-capacity rule and the guide's wire-gauge choice are silent without it.
    current_a: float | None = None
    #: Nominal voltage, which is what wakes the creepage rule.
    voltage_v: float | None = None
    id: NetId | None = None


@dataclass(frozen=True, slots=True)
class UpdateNetPayload:
    """Rename a net, reclassify it, or state its current/voltage.

    ``None`` means "leave alone" for name and class, which cannot legitimately BE None.
    For the two electrical fields it means "clear it", and leaving them alone is ``KEEP``.
    """

    id: NetId
    name: str | None = None
    net_class: NetClass | None = None
    current_a: float | None | _Keep = KEEP
    voltage_v: float | None | _Keep = KEEP


@dataclass(frozen=True, slots=True)
class DeleteNetPayload:
    id: NetId


@dataclass(frozen=True, slots=True)
class ConnectPinsPayload:
    """Add pins to a net. Plural, so a click-a-pin session is ONE undo step.

    The counterpart of ``conductor.addMany``: "GND is these five pins" is one decision,
    and an undo that leaves three of them attached is a state nobody asked for.
    """

    id: NetId
    nodes: tuple[NetNode, ...]
    label: str | None = None


@dataclass(frozen=True, slots=True)
class DisconnectPinsPayload:
    id: NetId
    nodes: tuple[NetNode, ...]
    label: str | None = None


@dataclass(frozen=True, slots=True)
class AddCutPayload:
    at: HoleCoord
    id: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyStripboardPlanPayload:
    """Track cuts AND the links that replace them, as ONE command.

    They are one decision. On stripboard a cut takes away a connection the board was
    providing and a link puts back the one the circuit wanted; committing them separately
    would put a state on the undo stack nobody designed -- a board cut apart with nothing
    linking it, or, one Ctrl+Z the other way, a board linked with nothing cut, which is a
    short across two nets.

    All-or-nothing like every other batch here: a cut off the board or a link the router
    should never have produced refuses the whole plan rather than leaving half of it down.
    """

    cuts: tuple[HoleCoord, ...] = ()
    conductors: tuple[NewConductor, ...] = ()
    #: Optional explicit ids, for reproducible replay. Generated when omitted.
    cut_ids: tuple[str, ...] | None = None
    conductor_ids: tuple[ConductorId, ...] | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteCutPayload:
    id: str


@dataclass(frozen=True, slots=True)
class AddMountingHolePayload:
    at: HoleCoord
    offset_x_mm: Mm = 0.0
    offset_y_mm: Mm = 0.0
    diameter: Mm = 3.2
    head_diameter: Mm = 6.0
    id: str | None = None


@dataclass(frozen=True, slots=True)
class AddMountingHolesPayload:
    """Several mounting holes as ONE undo step.

    The counterpart of ``conductor.addMany``, and here for the same reason: "put a hole in
    each corner" is one decision, and a single Ctrl+Z that leaves three of the four
    drilled is a state nobody asked for.
    """

    ats: tuple[HoleCoord, ...]
    #: Millimetre (dx, dy) per hole, so a batch of corner holes can each be pushed out
    #: into its OWN corner of the border. One shared offset would send all four the same
    #: way. None means every hole sits on its grid position.
    offsets: tuple[tuple[Mm, Mm], ...] | None = None
    diameter: Mm = 3.2
    head_diameter: Mm = 6.0
    ids: tuple[str, ...] | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteMountingHolePayload:
    id: str


@dataclass(frozen=True, slots=True)
class AddEdgeConnectorPayload:
    edge: BoardEdge
    start: int
    count: int
    finger_width: Mm = 2.0
    #: None asks for the length the board implies -- see ``geometry.default_finger_length_mm``.
    finger_length: Mm | None = None
    face: BoardFace = "bottom"
    id: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteEdgeConnectorPayload:
    id: str


# ---------------------------------------------------------------------------
# Component commands
# ---------------------------------------------------------------------------


def _prepare_component(
    doc: PerfDocument,
    p: PlaceComponentPayload,
    ctx: CommandContext,
    taken_refs: set[str],
    taken_ids: set[ComponentId],
) -> ComponentInstance:
    """Validate one placement against the document and give it its id.

    Shared by ``component.place`` and ``block.place`` for the reason
    ``_prepare_conductor`` is shared by the single and batch conductor adds: a batch
    exists to save undo entries, never to reach a weaker set of checks. The two ``taken``
    sets accumulate across a block, which is what catches a caller placing two parts
    called R1 in one payload -- the document alone cannot see that.
    """
    # A reference is how every net node names a part, so a part with none is a part the
    # wiring can never reach -- and a non-string one breaks the serializer on the way out.
    if not isinstance(p.ref, str) or not p.ref.strip():
        raise CommandError("invalid-ref", "A component needs a non-empty reference.")
    assert_hole_on_board(p.anchor, doc.board, f"Anchor for {p.ref}")
    rotation: Rotation = p.rotation if p.rotation is not None else 0
    assert_rotation(rotation)

    if p.ref in taken_refs:
        raise CommandError("duplicate-ref", f'A component with ref "{p.ref}" already exists.')
    # ...and against the design as well, which the caller's set cannot see. A reference
    # names one thing across both lists; see `assert_ref_free`.
    for part in doc.parts:
        if part.ref == p.ref:
            raise CommandError(
                "duplicate-ref",
                f'"{p.ref}" is a schematic part waiting to be placed. Use part.place to put '
                f"that one on the board rather than creating a second part with its name.",
            )
    taken_refs.add(p.ref)
    id_ = p.id if p.id is not None else ctx.next_id("cmp")
    if id_ in taken_ids:
        raise CommandError("duplicate-id", f'A component with id "{id_}" already exists.')
    taken_ids.add(id_)

    return ComponentInstance(
        id=id_,
        ref=p.ref,
        value=p.value,
        footprint_id=p.footprint_id,
        anchor=p.anchor,
        rotation=rotation,
        mirrored=p.mirrored if p.mirrored is not None else False,
        locked=False,
    )


class _PlaceComponent:
    type = "component.place"

    def apply(
        self, doc: PerfDocument, p: PlaceComponentPayload, ctx: CommandContext
    ) -> PerfDocument:
        component = _prepare_component(
            doc, p, ctx, {c.ref for c in doc.components}, {c.id for c in doc.components}
        )
        return dataclasses.replace(doc, components=(*doc.components, component))

    def describe(self, p: PlaceComponentPayload, doc: PerfDocument) -> str:
        return f"Place {p.ref} at {format_hole(p.anchor)}"


class _PlaceBlock:
    type = "block.place"

    def apply(self, doc: PerfDocument, p: PlaceBlockPayload, ctx: CommandContext) -> PerfDocument:
        if not p.components and not p.conductors:
            raise CommandError(
                "nothing-to-place",
                "block.place needs a part or a conductor; an empty block would put a "
                "no-op on the undo stack.",
            )
        if p.conductor_ids is not None and len(p.conductor_ids) != len(p.conductors):
            raise CommandError(
                "id-count-mismatch",
                f"Got {len(p.conductor_ids)} id(s) for {len(p.conductors)} conductor(s).",
            )

        taken_refs = {c.ref for c in doc.components}
        taken_ids = {c.id for c in doc.components}
        placed = tuple(
            _prepare_component(doc, spec, ctx, taken_refs, taken_ids) for spec in p.components
        )

        # The parts join the document BEFORE the copper is checked against it, because a
        # lead bend names the component it belongs to and that component is in this same
        # block. Checking the copper first would refuse every block that carried one.
        with_parts = dataclasses.replace(doc, components=doc.components + placed)

        taken_conductor_ids = _existing_conductor_ids(with_parts)
        prepared: list[Conductor] = []
        for index, spec in enumerate(p.conductors):
            id_ = p.conductor_ids[index] if p.conductor_ids is not None else ctx.next_id("cond")
            prepared.append(_prepare_conductor(with_parts, spec, id_, taken_conductor_ids))

        # All-or-nothing: everything above is validated before the document below changes,
        # so a bad member raises out and the caller's document is untouched.
        return dataclasses.replace(
            with_parts, conductors=with_parts.conductors + tuple(prepared)
        )

    def describe(self, p: PlaceBlockPayload, doc: PerfDocument) -> str:
        if p.label:
            return p.label
        return f"Place {len(p.components)} part(s) and {len(p.conductors)} connection(s)"


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
        return f"Move {ref} to {format_hole(p.anchor)}"


class _MoveComponents:
    type = "component.moveMany"

    def apply(
        self, doc: PerfDocument, p: MoveComponentsPayload, ctx: CommandContext
    ) -> PerfDocument:
        if not p.placements:
            raise CommandError(
                "nothing-to-move",
                "component.moveMany needs at least one placement; an empty batch would "
                "put a no-op on the undo stack.",
            )

        seen: set[ComponentId] = set()
        updates: dict[ComponentId, ComponentPlacement] = {}
        for placement in p.placements:
            existing = require_component(doc, placement.id)
            if placement.id in seen:
                # Two placements for one component is a caller bug, and applying the last
                # one silently would hide it behind a plausible-looking board.
                raise CommandError(
                    "duplicate-component",
                    f"{existing.ref} appears twice in one component.moveMany.",
                )
            seen.add(placement.id)
            if existing.locked:
                raise CommandError("component-locked", f"{existing.ref} is locked.")
            assert_hole_on_board(placement.anchor, doc.board, f"Anchor for {existing.ref}")
            if placement.rotation is not None:
                assert_rotation(placement.rotation)
            updates[placement.id] = placement

        # All-or-nothing, like the batch conductor commands: everything above is checked
        # before anything below changes, so a bad member leaves the document untouched.
        def placed(c: ComponentInstance) -> ComponentInstance:
            update = updates.get(c.id)
            if update is None:
                return c
            rotation = update.rotation if update.rotation is not None else c.rotation
            return dataclasses.replace(c, anchor=update.anchor, rotation=rotation)

        return dataclasses.replace(doc, components=tuple(placed(c) for c in doc.components))

    def describe(self, p: MoveComponentsPayload, doc: PerfDocument) -> str:
        if p.label:
            return p.label
        return f"Move {len(p.placements)} component(s)"


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
        ref = p.ref if p.ref is not None else existing.ref
        if ref != existing.ref:
            assert_ref_free(doc, ref, ignoring=p.id)
        # The rename carries the wiring; `rename_in_nets` says why, and refuses rather
        # than merging two parts when the new reference is already wired.
        nets = rename_in_nets(doc, existing.ref, ref)
        components = tuple(
            dataclasses.replace(
                c,
                ref=ref,
                value=p.value if p.value is not None else c.value,
                locked=p.locked if p.locked is not None else c.locked,
            )
            if c.id == p.id
            else c
            for c in doc.components
        )
        return dataclasses.replace(doc, components=components, nets=nets)

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
                if not (c.kind == "lead-bend" and c.component_id == p.id)
            ),
        )

    def describe(self, p: DeleteComponentPayload, doc: PerfDocument) -> str:
        c = next((x for x in doc.components if x.id == p.id), None)
        ref = c.ref if c is not None else p.id
        return f"Delete {ref}"


# ---------------------------------------------------------------------------
# Schematic parts: the design before the board
# ---------------------------------------------------------------------------
#
# THE OTHER ORDER OF WORK, AND THE ONE EVERY OTHER EDA TOOL USES. Until these existed a
# part could only enter a document by being put on the board, which made "lay out the
# circuit and then work out where it goes" impossible: you had to decide a hole for a
# resistor before you had decided the resistor. These five commands and `component.unplace`
# are the schematic half -- add a part, wire it (`net.connect` already does not care
# whether a part is on the board), and place it when the circuit is settled.
#
# Placement is a MOVE between two lists rather than a flag, for the reason set out on
# `model.SchematicPart`: everything downstream of the board reads `doc.components` and is
# right to assume every entry has a position.


class _AddPart:
    type = "part.add"

    def apply(self, doc: PerfDocument, p: AddPartPayload, ctx: CommandContext) -> PerfDocument:
        ref = p.ref.strip()
        if not ref:
            raise CommandError("empty-ref", "A part needs a reference designator.")
        assert_ref_free(doc, ref)
        id_ = p.id if p.id is not None else ctx.next_id("part")
        if any(part.id == id_ for part in doc.parts) or any(c.id == id_ for c in doc.components):
            raise CommandError("duplicate-id", f'Something with id "{id_}" already exists.')
        part = SchematicPart(
            id=id_, ref=ref, value=p.value, footprint_id=p.footprint_id
        )
        return dataclasses.replace(doc, parts=(*doc.parts, part))

    def describe(self, p: AddPartPayload, doc: PerfDocument) -> str:
        value = f" {p.value}" if p.value else ""
        return f"Add {p.ref.strip()}{value} to the schematic"


class _UpdatePart:
    type = "part.update"

    def apply(self, doc: PerfDocument, p: UpdatePartPayload, ctx: CommandContext) -> PerfDocument:
        existing = require_part(doc, p.id)
        ref = existing.ref if p.ref is None else p.ref.strip()
        if not ref:
            raise CommandError("empty-ref", "A part needs a reference designator.")
        if ref != existing.ref:
            assert_ref_free(doc, ref, ignoring=p.id)
        nets = rename_in_nets(doc, existing.ref, ref)
        parts = tuple(
            dataclasses.replace(
                part,
                ref=ref,
                value=p.value if p.value is not None else part.value,
                footprint_id=(
                    p.footprint_id if p.footprint_id is not None else part.footprint_id
                ),
            )
            if part.id == p.id
            else part
            for part in doc.parts
        )
        return dataclasses.replace(doc, parts=parts, nets=nets)

    def describe(self, p: UpdatePartPayload, doc: PerfDocument) -> str:
        part = next((x for x in doc.parts if x.id == p.id), None)
        return f"Update {part.ref if part is not None else p.id}"


class _DeletePart:
    type = "part.delete"

    def apply(self, doc: PerfDocument, p: DeletePartPayload, ctx: CommandContext) -> PerfDocument:
        existing = require_part(doc, p.id)
        # ITS CONNECTIONS GO WITH IT, and this is the one place that differs from
        # `component.delete` on purpose. Deleting a component takes it OFF THE BOARD; the
        # schematic still asks for it, and LVS is right to report the gap. Deleting a
        # schematic part means the design does not have it, so a net still naming its pins
        # would be asking for a part nothing in the document has ever heard of.
        nets = tuple(
            dataclasses.replace(
                net, nodes=tuple(n for n in net.nodes if n.component_ref != existing.ref)
            )
            if any(n.component_ref == existing.ref for n in net.nodes)
            else net
            for net in doc.nets
        )
        return dataclasses.replace(
            doc, parts=tuple(part for part in doc.parts if part.id != p.id), nets=nets
        )

    def describe(self, p: DeletePartPayload, doc: PerfDocument) -> str:
        part = next((x for x in doc.parts if x.id == p.id), None)
        if part is None:
            return f"Delete {p.id}"
        wired = sum(
            1 for net in doc.nets for node in net.nodes if node.component_ref == part.ref
        )
        if wired:
            return f"Delete {part.ref} and its {wired} connection(s)"
        return f"Delete {part.ref}"


def _placed_from(part: SchematicPart, spec: PartPlacement, doc: PerfDocument) -> ComponentInstance:
    assert_hole_on_board(spec.anchor, doc.board, f"Anchor for {part.ref}")
    rotation: Rotation = spec.rotation if spec.rotation is not None else 0
    assert_rotation(rotation)
    # The SAME id, deliberately. Nothing outside this document refers to it, but the
    # journal and the undo history do, and a part that changed identity when it was
    # placed would make a replayed journal describe two different things.
    return ComponentInstance(
        id=part.id,
        ref=part.ref,
        value=part.value,
        footprint_id=part.footprint_id,
        anchor=spec.anchor,
        rotation=rotation,
        mirrored=spec.mirrored if spec.mirrored is not None else False,
        locked=False,
    )


class _PlaceParts:
    type = "part.place"

    def apply(self, doc: PerfDocument, p: PlacePartsPayload, ctx: CommandContext) -> PerfDocument:
        if not p.placements:
            raise CommandError(
                "nothing-to-place",
                "part.place needs at least one part; an empty batch would put a no-op on "
                "the undo stack.",
            )
        seen: set[ComponentId] = set()
        placed: list[ComponentInstance] = []
        for spec in p.placements:
            if spec.id in seen:
                raise CommandError("duplicate-id", f'Part "{spec.id}" is placed twice in one batch.')
            seen.add(spec.id)
            placed.append(_placed_from(require_part(doc, spec.id), spec, doc))
        return dataclasses.replace(
            doc,
            components=doc.components + tuple(placed),
            parts=tuple(part for part in doc.parts if part.id not in seen),
        )

    def describe(self, p: PlacePartsPayload, doc: PerfDocument) -> str:
        if p.label:
            return p.label
        by_id = {part.id: part for part in doc.parts}
        if len(p.placements) == 1:
            spec = p.placements[0]
            part = by_id.get(spec.id)
            return f"Place {part.ref if part is not None else spec.id} at {format_hole(spec.anchor)}"
        return f"Place {len(p.placements)} part(s) on the board"


class _UnplaceComponent:
    type = "component.unplace"

    def apply(
        self, doc: PerfDocument, p: UnplaceComponentPayload, ctx: CommandContext
    ) -> PerfDocument:
        existing = require_component(doc, p.id)
        if existing.locked:
            raise CommandError("component-locked", f"{existing.ref} is locked.")
        # Its lead bends go, exactly as they do on `component.delete`: a lead bend is a
        # length of the part's own leg, and the part is no longer on the board. Wires and
        # traces stay, because they may still be wanted and silently deleting somebody's
        # routing is worse than leaving something for DRC and LVS to point at. THE NETS
        # ARE UNTOUCHED -- that is the whole difference from deleting it.
        return dataclasses.replace(
            doc,
            components=tuple(c for c in doc.components if c.id != p.id),
            conductors=tuple(
                c
                for c in doc.conductors
                if not (c.kind == "lead-bend" and c.component_id == p.id)
            ),
            parts=(
                *doc.parts,
                SchematicPart(
                    id=existing.id,
                    ref=existing.ref,
                    value=existing.value,
                    footprint_id=existing.footprint_id,
                ),
            ),
        )

    def describe(self, p: UnplaceComponentPayload, doc: PerfDocument) -> str:
        c = next((x for x in doc.components if x.id == p.id), None)
        return f"Take {c.ref if c is not None else p.id} off the board"


# ---------------------------------------------------------------------------
# Conductor commands
# ---------------------------------------------------------------------------


def _prepare_conductor(
    doc: PerfDocument,
    spec: NewConductor,
    id_: ConductorId,
    taken_ids: set[ConductorId],
) -> Conductor:
    """Validate one new-conductor spec against the document and give it its id.

    Shared by ``conductor.add`` and ``conductor.addMany`` so a batch cannot drift into a
    weaker set of checks than a single add -- the batch exists to save undo entries, not
    to skip validation. ``taken_ids`` accumulates across a batch, which is what catches a
    caller supplying the same id twice within one payload.
    """
    assert_valid_path(spec.path, spec.kind, doc.board)

    if isinstance(spec, NewLeadBendConductor):
        require_component(doc, spec.component_id)
    if spec.kind in ("solder-trace", "solder-trace-wired") and spec.side != "bottom":
        raise CommandError("invalid-side", "Solder traces exist on the solder side only.")

    if id_ in taken_ids:
        raise CommandError("duplicate-id", f'A conductor with id "{id_}" already exists.')
    taken_ids.add(id_)

    return _finalize_conductor(spec, id_)


def _existing_conductor_ids(doc: PerfDocument) -> set[ConductorId]:
    return {c.id for c in doc.conductors}


class _AddConductor:
    type = "conductor.add"

    def apply(self, doc: PerfDocument, p: AddConductorPayload, ctx: CommandContext) -> PerfDocument:
        id_ = p.id if p.id is not None else ctx.next_id("cond")
        conductor = _prepare_conductor(doc, p.conductor, id_, _existing_conductor_ids(doc))
        return dataclasses.replace(doc, conductors=(*doc.conductors, conductor))

    def describe(self, p: AddConductorPayload, doc: PerfDocument) -> str:
        path = p.conductor.path
        span = f" {format_hole(path[0])} to {format_hole(path[-1])}" if path else ""
        return f"Add {p.conductor.kind}{span}"


class _AddConductors:
    type = "conductor.addMany"

    def apply(
        self, doc: PerfDocument, p: AddConductorsPayload, ctx: CommandContext
    ) -> PerfDocument:
        if not p.conductors:
            raise CommandError(
                "nothing-to-add",
                "conductor.addMany needs at least one conductor; an empty batch would "
                "put a no-op on the undo stack.",
            )
        if p.ids is not None and len(p.ids) != len(p.conductors):
            raise CommandError(
                "id-count-mismatch",
                f"Got {len(p.ids)} id(s) for {len(p.conductors)} conductor(s).",
            )

        taken = _existing_conductor_ids(doc)
        prepared: list[Conductor] = []
        for index, spec in enumerate(p.conductors):
            id_ = p.ids[index] if p.ids is not None else ctx.next_id("cond")
            prepared.append(_prepare_conductor(doc, spec, id_, taken))

        # All-or-nothing: every spec is validated above before the document changes, so a
        # bad member raises out of the loop and the caller's document is untouched.
        return dataclasses.replace(doc, conductors=doc.conductors + tuple(prepared))

    def describe(self, p: AddConductorsPayload, doc: PerfDocument) -> str:
        if p.label:
            return p.label
        return f"Add {len(p.conductors)} conductor(s)"


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


class _DeleteConductors:
    type = "conductor.deleteMany"

    def apply(
        self, doc: PerfDocument, p: DeleteConductorsPayload, ctx: CommandContext
    ) -> PerfDocument:
        if not p.ids:
            raise CommandError(
                "nothing-to-delete",
                "conductor.deleteMany needs at least one id; an empty batch would put a no-op "
                "on the undo stack.",
            )
        present = {c.id for c in doc.conductors}
        missing = [id_ for id_ in p.ids if id_ not in present]
        if missing:
            # All or nothing, like the batch add: a partly-applied cleanup leaves the user
            # unable to tell what was removed.
            raise CommandError(
                "conductor-not-found", f"No conductor with id(s) {', '.join(sorted(missing))}."
            )
        doomed = set(p.ids)
        return dataclasses.replace(
            doc, conductors=tuple(c for c in doc.conductors if c.id not in doomed)
        )

    def describe(self, p: DeleteConductorsPayload, doc: PerfDocument) -> str:
        if p.label:
            return p.label
        return f"Delete {len(p.ids)} conductor(s)"


class _ReplaceConductors:
    type = "conductor.replace"

    def apply(
        self, doc: PerfDocument, p: ReplaceConductorsPayload, ctx: CommandContext
    ) -> PerfDocument:
        if not p.remove_ids and not p.conductors:
            raise CommandError(
                "nothing-to-do",
                "conductor.replace needs something to remove or something to add.",
            )
        if p.ids is not None and len(p.ids) != len(p.conductors):
            raise CommandError(
                "id-count-mismatch",
                f"Got {len(p.ids)} id(s) for {len(p.conductors)} conductor(s).",
            )

        present = {c.id for c in doc.conductors}
        missing = [id_ for id_ in p.remove_ids if id_ not in present]
        if missing:
            raise CommandError(
                "conductor-not-found", f"No conductor with id(s) {', '.join(sorted(missing))}."
            )

        doomed = set(p.remove_ids)
        kept = tuple(c for c in doc.conductors if c.id not in doomed)

        # Validated against the document AFTER the removals, which is the board the new
        # conductors will actually live on -- and which is the point of doing both in one
        # command rather than two.
        reduced = dataclasses.replace(doc, conductors=kept)
        taken = {c.id for c in kept}
        prepared: list[Conductor] = []
        for index, spec in enumerate(p.conductors):
            id_ = p.ids[index] if p.ids is not None else ctx.next_id("cond")
            prepared.append(_prepare_conductor(reduced, spec, id_, taken))

        return dataclasses.replace(doc, conductors=kept + tuple(prepared))

    def describe(self, p: ReplaceConductorsPayload, doc: PerfDocument) -> str:
        if p.label:
            return p.label
        return f"Replace {len(p.remove_ids)} conductor(s) with {len(p.conductors)}"


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
        if b.pad_shape == "oblong" and (b.pad_length is None or b.pad_length <= b.pad_diameter):
            raise CommandError(
                "invalid-board",
                f"An oblong pad needs a pad length longer than its {b.pad_diameter} mm width; "
                f"got {b.pad_length}. A pad that is as long as it is wide is a round pad.",
            )
        if b.border_x_mm < 0 or b.border_y_mm < 0:
            raise CommandError("invalid-board", "A board border cannot be negative.")
        if b.labels is not None and b.labels.row_digits < 1:
            raise CommandError(
                "invalid-board", "A printed row label cannot be narrower than one digit."
            )

        # Shrinking the board could strand parts outside it. Refuse rather than
        # silently dropping the user's work, and name EVERY offender: naming the first
        # meant one refusal per part, each discovered only after the previous one had
        # been moved.
        stranded_components = [c for c in doc.components if not is_inside_board(c.anchor, b)]
        if stranded_components:
            listed = ", ".join(
                f"{c.ref} at {format_hole(c.anchor)}" for c in stranded_components[:8]
            )
            more = f" and {len(stranded_components) - 8} more" if len(stranded_components) > 8 else ""
            raise CommandError(
                "would-strand-component",
                f"{listed}{more} would fall outside a {b.cols}x{b.rows} board.",
            )
        for cond in doc.conductors:
            stranded = next((h for h in cond.path if not is_inside_board(h, b)), None)
            if stranded is not None:
                raise CommandError(
                    "would-strand-conductor",
                    f"Conductor {cond.id} passes through {format_hole(stranded)}, "
                    f"outside a {b.cols}x{b.rows} board.",
                )
        for cut in doc.cuts:
            # A cut off the board is not harmless: it survives the shrink unseen and, if
            # the board is grown again, breaks a strip nobody remembers cutting.
            if not is_inside_board(cut.at, b):
                raise CommandError(
                    "would-strand-cut",
                    f"The track cut at {format_hole(cut.at)} would fall outside a "
                    f"{b.cols}x{b.rows} board.",
                )
        for mount in doc.mounting_holes:
            if not is_inside_board(mount.at, b):
                raise CommandError(
                    "would-strand-mounting-hole",
                    f"Mounting hole {mount.id} at {format_hole(mount.at)} would fall outside a "
                    f"{b.cols}x{b.rows} board.",
                )
        for connector in doc.edge_connectors:
            # Checked against the NEW board, so a run that would hang off the shortened
            # edge -- or that is no longer against an edge at all -- is caught here rather
            # than silently drawing half a connector.
            last = connector.start + max(0, connector.count) - 1
            limit = b.cols if connector.edge in ("top", "bottom") else b.rows
            if last >= limit:
                raise CommandError(
                    "would-strand-edge-connector",
                    f"Edge connector {connector.id} runs to index {last} along the "
                    f"{connector.edge} edge, past the {limit} available on a {b.cols}x{b.rows} "
                    f"board.",
                )
            if connector.finger_width >= b.pitch:
                raise CommandError(
                    "would-merge-edge-connector",
                    f"Edge connector {connector.id} has {connector.finger_width} mm fingers, "
                    f"which at a {b.pitch} mm pitch would touch each other.",
                )

        return dataclasses.replace(doc, board=b)

    def describe(self, p: SetBoardPayload, doc: PerfDocument) -> str:
        return f"Set board to {p.board.cols}x{p.board.rows} {p.board.material}"


class _ApplyBoardPreset:
    """Replace the board and the features that belong to it, all or nothing.

    Reuses ``board.set``'s own validation for the board itself -- the stranding checks
    matter more here than anywhere, since a preset can shrink the grid by a lot -- and
    then swaps the mechanical features wholesale rather than merging. Merging is the wrong
    idea: the fingers and corner holes of the board being replaced belong to that board.
    A part the user placed is NOT touched, which is why the stranding checks still run.
    """

    type = "board.applyPreset"

    def apply(
        self, doc: PerfDocument, p: ApplyBoardPresetPayload, ctx: CommandContext
    ) -> PerfDocument:
        # Validated against the new board with the OLD features cleared, so a run of
        # fingers along the previous board's longer edge cannot fail a resize it has no
        # part in.
        stripped = dataclasses.replace(doc, edge_connectors=(), mounting_holes=())
        resized = _SetBoard().apply(stripped, SetBoardPayload(board=p.board), ctx)

        seen: set[str] = set()
        for connector in p.edge_connectors:
            if connector.id in seen:
                raise CommandError("duplicate-id", f'Duplicate edge connector id "{connector.id}".')
            seen.add(connector.id)
        seen.clear()
        for mount in p.mounting_holes:
            if mount.id in seen:
                raise CommandError("duplicate-id", f'Duplicate mounting hole id "{mount.id}".')
            seen.add(mount.id)
            assert_hole_on_board(mount.at, p.board, "Mounting hole")

        return dataclasses.replace(
            resized, edge_connectors=tuple(p.edge_connectors), mounting_holes=tuple(p.mounting_holes)
        )

    def describe(self, p: ApplyBoardPresetPayload, doc: PerfDocument) -> str:
        if p.label:
            return p.label
        return f"Use a {p.board.cols}x{p.board.rows} {p.board.material} board"


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


# ---------------------------------------------------------------------------
# Net commands: the schematic's intent, stated by hand
# ---------------------------------------------------------------------------
#
# For a long time ``netlist.import`` was the ONLY way a net could enter a document, which
# quietly made KiCad a prerequisite for the whole tool. Nobody reaches for a schematic
# capture package to wire up four parts on a scrap of perfboard, and without a net there
# is no ratsnest, so autoroute, LVS and the guide's continuity tests all had nothing to
# work from. These five commands are the same intent, entered by hand.
#
# The import stays as it is: it REPLACES the netlist wholesale, because that is what
# re-exporting from a schematic means. These edit it in place.


class _AddNet:
    type = "net.add"

    def apply(self, doc: PerfDocument, p: AddNetPayload, ctx: CommandContext) -> PerfDocument:
        name = assert_net_name_free(doc, p.name)
        if p.net_class not in ("power", "ground", "signal"):
            raise CommandError(
                "invalid-net-class",
                f'A net is "power", "ground" or "signal"; got "{p.net_class}".',
            )
        assert_electrical(p.current_a, p.voltage_v)
        nodes = assert_pins_free(doc, p.nodes, joining=None)

        id_ = p.id if p.id is not None else ctx.next_id("net")
        if any(n.id == id_ for n in doc.nets):
            raise CommandError("duplicate-id", f'A net with id "{id_}" already exists.')

        net = Net(
            id=id_,
            name=name,
            nodes=nodes,
            net_class=p.net_class,
            current_a=p.current_a,
            voltage_v=p.voltage_v,
        )
        return dataclasses.replace(doc, nets=(*doc.nets, net))

    def describe(self, p: AddNetPayload, doc: PerfDocument) -> str:
        pins = f" with {len(p.nodes)} pin(s)" if p.nodes else ""
        return f"Add {p.net_class} net {p.name.strip()}{pins}"


class _UpdateNet:
    type = "net.update"

    def apply(self, doc: PerfDocument, p: UpdateNetPayload, ctx: CommandContext) -> PerfDocument:
        existing = require_net(doc, p.id)
        name = existing.name if p.name is None else assert_net_name_free(doc, p.name, ignoring=p.id)
        net_class = existing.net_class if p.net_class is None else p.net_class
        if net_class not in ("power", "ground", "signal"):
            raise CommandError(
                "invalid-net-class", f'A net is "power", "ground" or "signal"; got "{net_class}".'
            )
        current_a = existing.current_a if isinstance(p.current_a, _Keep) else p.current_a
        voltage_v = existing.voltage_v if isinstance(p.voltage_v, _Keep) else p.voltage_v
        assert_electrical(current_a, voltage_v)

        nets = tuple(
            dataclasses.replace(
                net,
                name=name,
                net_class=net_class,
                current_a=current_a,
                voltage_v=voltage_v,
            )
            if net.id == p.id
            else net
            for net in doc.nets
        )
        return dataclasses.replace(doc, nets=nets)

    def describe(self, p: UpdateNetPayload, doc: PerfDocument) -> str:
        existing = next((n for n in doc.nets if n.id == p.id), None)
        was = existing.name if existing is not None else p.id
        if p.name is not None and p.name.strip() != was:
            return f"Rename net {was} to {p.name.strip()}"
        if p.net_class is not None:
            return f"Make {was} a {p.net_class} net"
        return f"Update net {was}"


class _DeleteNet:
    """Forget what a net was FOR. The copper laid for it stays on the board.

    A conductor's ``net_id`` is a claim on an intent -- "I am part of this net's routing"
    -- and that claim is the one reference in the document that would be left dangling by
    a delete, so it is cleared here in the same step. The consequence is deliberate and
    worth stating: that copper becomes indistinguishable from hand-drawn work, which is
    exactly what re-route and the stale-conductor sweep both promise never to touch. There
    is no longer an intent to route it against, so nothing may act on it unasked.
    """

    type = "net.delete"

    def apply(self, doc: PerfDocument, p: DeleteNetPayload, ctx: CommandContext) -> PerfDocument:
        require_net(doc, p.id)
        conductors = tuple(
            dataclasses.replace(c, net_id=None) if c.net_id == p.id else c for c in doc.conductors
        )
        return dataclasses.replace(
            doc, nets=tuple(n for n in doc.nets if n.id != p.id), conductors=conductors
        )

    def describe(self, p: DeleteNetPayload, doc: PerfDocument) -> str:
        existing = next((n for n in doc.nets if n.id == p.id), None)
        name = existing.name if existing is not None else p.id
        freed = sum(1 for c in doc.conductors if c.net_id == p.id)
        if freed:
            return f"Delete net {name} ({freed} conductor(s) keep their copper)"
        return f"Delete net {name}"


class _ConnectPins:
    type = "net.connect"

    def apply(self, doc: PerfDocument, p: ConnectPinsPayload, ctx: CommandContext) -> PerfDocument:
        require_net(doc, p.id)
        if not p.nodes:
            raise CommandError(
                "empty-batch",
                "net.connect needs at least one pin; an empty batch would put a no-op in "
                "the undo history.",
            )
        nodes = assert_pins_free(doc, p.nodes, joining=p.id)
        nets = tuple(
            dataclasses.replace(net, nodes=net.nodes + nodes) if net.id == p.id else net
            for net in doc.nets
        )
        return dataclasses.replace(doc, nets=nets)

    def describe(self, p: ConnectPinsPayload, doc: PerfDocument) -> str:
        if p.label:
            return p.label
        existing = next((n for n in doc.nets if n.id == p.id), None)
        name = existing.name if existing is not None else p.id
        if len(p.nodes) == 1:
            node = p.nodes[0]
            return f"Connect {node.component_ref.strip()}.{node.pin.strip()} to {name}"
        return f"Connect {len(p.nodes)} pins to {name}"


class _DisconnectPins:
    type = "net.disconnect"

    def apply(
        self, doc: PerfDocument, p: DisconnectPinsPayload, ctx: CommandContext
    ) -> PerfDocument:
        existing = require_net(doc, p.id)
        if not p.nodes:
            raise CommandError(
                "empty-batch",
                "net.disconnect needs at least one pin; an empty batch would put a no-op "
                "in the undo history.",
            )
        wanted = {(node.component_ref.strip(), node.pin.strip()) for node in p.nodes}
        held = {(node.component_ref, node.pin) for node in existing.nodes}
        missing = sorted(f"{ref}.{pin}" for ref, pin in wanted - held)
        if missing:
            raise CommandError(
                "pin-not-on-net",
                f'Net "{existing.name}" does not have pin(s) {", ".join(missing)}.',
            )

        kept = tuple(
            node for node in existing.nodes if (node.component_ref, node.pin) not in wanted
        )
        nets = tuple(
            dataclasses.replace(net, nodes=kept) if net.id == p.id else net for net in doc.nets
        )
        return dataclasses.replace(doc, nets=nets)

    def describe(self, p: DisconnectPinsPayload, doc: PerfDocument) -> str:
        if p.label:
            return p.label
        existing = next((n for n in doc.nets if n.id == p.id), None)
        name = existing.name if existing is not None else p.id
        if len(p.nodes) == 1:
            node = p.nodes[0]
            return f"Disconnect {node.component_ref.strip()}.{node.pin.strip()} from {name}"
        return f"Disconnect {len(p.nodes)} pins from {name}"


class _AddCut:
    type = "cut.add"

    def apply(self, doc: PerfDocument, p: AddCutPayload, ctx: CommandContext) -> PerfDocument:
        if doc.board.type != "stripboard":
            raise CommandError(
                "not-stripboard",
                f"This board is {doc.board.type}, which has no tracks to cut.",
            )
        assert_hole_on_board(p.at, doc.board, "Cut")
        id_ = p.id if p.id is not None else ctx.next_id("cut")
        if any(c.id == id_ for c in doc.cuts):
            raise CommandError("duplicate-id", f'A cut with id "{id_}" already exists.')
        cut = TrackCut(id=id_, at=p.at)
        return dataclasses.replace(doc, cuts=(*doc.cuts, cut))

    def describe(self, p: AddCutPayload, doc: PerfDocument) -> str:
        return f"Cut track at {format_hole(p.at)}"


class _ApplyStripboardPlan:
    type = "stripboard.apply"

    def apply(
        self, doc: PerfDocument, p: ApplyStripboardPlanPayload, ctx: CommandContext
    ) -> PerfDocument:
        if doc.board.type != "stripboard":
            raise CommandError(
                "not-stripboard",
                f"This board is {doc.board.type}, which has no tracks to cut or link.",
            )
        if not p.cuts and not p.conductors:
            raise CommandError(
                "nothing-to-apply",
                "stripboard.apply needs a cut or a link; an empty plan would put a no-op "
                "on the undo stack.",
            )
        if p.cut_ids is not None and len(p.cut_ids) != len(p.cuts):
            raise CommandError(
                "id-count-mismatch", f"Got {len(p.cut_ids)} id(s) for {len(p.cuts)} cut(s)."
            )
        if p.conductor_ids is not None and len(p.conductor_ids) != len(p.conductors):
            raise CommandError(
                "id-count-mismatch",
                f"Got {len(p.conductor_ids)} id(s) for {len(p.conductors)} conductor(s).",
            )

        taken_cut_ids = {cut.id for cut in doc.cuts}
        cut_at = {(cut.at.col, cut.at.row) for cut in doc.cuts}
        cuts: list[TrackCut] = []
        for index, at in enumerate(p.cuts):
            assert_hole_on_board(at, doc.board, "Cut")
            if (at.col, at.row) in cut_at:
                # Cutting a hole that is already cut is a no-op the caller did not mean:
                # the plan was made against a different board than the one in front of it.
                raise CommandError(
                    "duplicate-cut", f"The track at {format_hole(at)} is already cut."
                )
            cut_at.add((at.col, at.row))
            id_ = p.cut_ids[index] if p.cut_ids is not None else ctx.next_id("cut")
            if id_ in taken_cut_ids:
                raise CommandError("duplicate-id", f'A cut with id "{id_}" already exists.')
            taken_cut_ids.add(id_)
            cuts.append(TrackCut(id=id_, at=at))

        # The cuts join the document before the copper is checked against it, so a link
        # is validated against the board it will actually be soldered to.
        with_cuts = dataclasses.replace(doc, cuts=doc.cuts + tuple(cuts))

        taken_conductor_ids = _existing_conductor_ids(with_cuts)
        prepared: list[Conductor] = []
        for index, spec in enumerate(p.conductors):
            id_ = p.conductor_ids[index] if p.conductor_ids is not None else ctx.next_id("cond")
            prepared.append(_prepare_conductor(with_cuts, spec, id_, taken_conductor_ids))

        return dataclasses.replace(
            with_cuts, conductors=with_cuts.conductors + tuple(prepared)
        )

    def describe(self, p: ApplyStripboardPlanPayload, doc: PerfDocument) -> str:
        if p.label:
            return p.label
        return f"Cut {len(p.cuts)} track(s) and fit {len(p.conductors)} link(s)"


class _DeleteCut:
    type = "cut.delete"

    def apply(self, doc: PerfDocument, p: DeleteCutPayload, ctx: CommandContext) -> PerfDocument:
        if not any(c.id == p.id for c in doc.cuts):
            raise CommandError("cut-not-found", f'No cut with id "{p.id}".')
        return dataclasses.replace(doc, cuts=tuple(c for c in doc.cuts if c.id != p.id))

    def describe(self, p: DeleteCutPayload, doc: PerfDocument) -> str:
        return f"Remove cut {p.id}"


class _AddMountingHole:
    """Drill a screw hole through the board.

    Notice what is NOT checked here. A mounting bore destroys the copper on its own pad
    and on its orthogonal neighbours, so drilling one under a placed component leaves
    pins sitting on pads that no longer exist -- and this command allows it. That is the
    division this module's header sets out: the result is still a perfectly well-formed
    document describing a board somebody has made a mistake on, which is DRC's subject
    (``mounting-hole-conflict``), not a command's. Refusing it here would also make the
    obvious order of work -- place the holes, then move the parts off them -- impossible.
    """

    type = "mounting-hole.add"

    def apply(
        self, doc: PerfDocument, p: AddMountingHolePayload, ctx: CommandContext
    ) -> PerfDocument:
        assert_hole_on_board(p.at, doc.board, "Mounting hole")
        if not (p.diameter > 0):
            raise CommandError(
                "invalid-mounting-hole", "A mounting hole needs a positive diameter."
            )
        if p.head_diameter < p.diameter:
            raise CommandError(
                "invalid-mounting-hole",
                f"A screw head ({p.head_diameter} mm) cannot be smaller than the hole it goes "
                f"through ({p.diameter} mm).",
            )
        id_ = p.id if p.id is not None else ctx.next_id("mh")
        if any(m.id == id_ for m in doc.mounting_holes):
            raise CommandError("duplicate-id", f'A mounting hole with id "{id_}" already exists.')
        if any(m.at == p.at for m in doc.mounting_holes):
            raise CommandError(
                "duplicate-mounting-hole",
                f"There is already a mounting hole at {format_hole(p.at)}.",
            )
        hole = MountingHole(
            id=id_,
            at=p.at,
            offset_x_mm=p.offset_x_mm,
            offset_y_mm=p.offset_y_mm,
            diameter=p.diameter,
            head_diameter=p.head_diameter,
        )
        return dataclasses.replace(doc, mounting_holes=(*doc.mounting_holes, hole))

    def describe(self, p: AddMountingHolePayload, doc: PerfDocument) -> str:
        return f"Drill {p.diameter} mm mounting hole at {format_hole(p.at)}"


class _AddMountingHoles:
    type = "mounting-hole.addMany"

    def apply(
        self, doc: PerfDocument, p: AddMountingHolesPayload, ctx: CommandContext
    ) -> PerfDocument:
        if not p.ats:
            raise CommandError("nothing-to-add", "No mounting holes were given.")
        if p.ids is not None and len(p.ids) != len(p.ats):
            raise CommandError(
                "id-count-mismatch",
                f"{len(p.ids)} id(s) were supplied for {len(p.ats)} mounting hole(s).",
            )
        if p.offsets is not None and len(p.offsets) != len(p.ats):
            raise CommandError(
                "offset-count-mismatch",
                f"{len(p.offsets)} offset(s) were supplied for {len(p.ats)} mounting hole(s).",
            )
        if not (p.diameter > 0):
            raise CommandError(
                "invalid-mounting-hole", "A mounting hole needs a positive diameter."
            )
        if p.head_diameter < p.diameter:
            raise CommandError(
                "invalid-mounting-hole",
                f"A screw head ({p.head_diameter} mm) cannot be smaller than the hole it goes "
                f"through ({p.diameter} mm).",
            )

        # All-or-nothing: validated in full before anything is added, so a rejected batch
        # leaves the document exactly as it was rather than half-drilled.
        taken_ids = {m.id for m in doc.mounting_holes}
        taken_holes = {(m.at.col, m.at.row) for m in doc.mounting_holes}
        added: list[MountingHole] = []
        for index, at in enumerate(p.ats):
            assert_hole_on_board(at, doc.board, "Mounting hole")
            if (at.col, at.row) in taken_holes:
                raise CommandError(
                    "duplicate-mounting-hole",
                    f"There is already a mounting hole at {format_hole(at)}.",
                )
            taken_holes.add((at.col, at.row))
            id_ = p.ids[index] if p.ids is not None else ctx.next_id("mh")
            if id_ in taken_ids:
                raise CommandError(
                    "duplicate-id", f'A mounting hole with id "{id_}" already exists.'
                )
            taken_ids.add(id_)
            offset = p.offsets[index] if p.offsets is not None else (0.0, 0.0)
            added.append(
                MountingHole(
                    id=id_,
                    at=at,
                    offset_x_mm=offset[0],
                    offset_y_mm=offset[1],
                    diameter=p.diameter,
                    head_diameter=p.head_diameter,
                )
            )
        return dataclasses.replace(doc, mounting_holes=doc.mounting_holes + tuple(added))

    def describe(self, p: AddMountingHolesPayload, doc: PerfDocument) -> str:
        if p.label:
            return p.label
        return f"Drill {len(p.ats)} mounting holes"


class _DeleteMountingHole:
    type = "mounting-hole.delete"

    def apply(
        self, doc: PerfDocument, p: DeleteMountingHolePayload, ctx: CommandContext
    ) -> PerfDocument:
        if not any(m.id == p.id for m in doc.mounting_holes):
            raise CommandError("mounting-hole-not-found", f'No mounting hole with id "{p.id}".')
        return dataclasses.replace(
            doc, mounting_holes=tuple(m for m in doc.mounting_holes if m.id != p.id)
        )

    def describe(self, p: DeleteMountingHolePayload, doc: PerfDocument) -> str:
        return f"Remove mounting hole {p.id}"


class _AddEdgeConnector:
    type = "edge-connector.add"

    def apply(
        self, doc: PerfDocument, p: AddEdgeConnectorPayload, ctx: CommandContext
    ) -> PerfDocument:
        board = doc.board
        if not _is_plain_int(p.start) or not _is_plain_int(p.count) or p.start < 0 or p.count < 1:
            raise CommandError(
                "invalid-edge-connector",
                "An edge connector needs a non-negative start and at least one finger.",
            )
        limit = board.cols if p.edge in ("top", "bottom") else board.rows
        if p.start + p.count > limit:
            raise CommandError(
                "invalid-edge-connector",
                f"A {p.count}-finger run starting at index {p.start} runs past the {limit} "
                f"positions along the {p.edge} edge.",
            )
        # Both of these are integrity, not taste. A finger as wide as the pitch is not a
        # finger, it is one piece of copper across two nets; a finger longer than the
        # pitch reaches the next hole in and joins two rows. The model has no way to
        # express either, so it must not be able to hold one.
        if not (0 < p.finger_width < board.pitch):
            raise CommandError(
                "invalid-edge-connector",
                f"Finger width must be positive and under the {board.pitch} mm pitch, else "
                f"neighbouring fingers touch; got {p.finger_width} mm.",
            )
        # Measured from the board's edge, so both bounds come from where the first hole
        # actually sits: a finger shorter than the margin never reaches its own hole, and
        # one longer than half a pitch past it starts eating the next row's pad. On a
        # flush-cut board those work out to "more than half a pitch, at most a pitch".
        margin = board_edge_margin_mm(board)
        longest = default_finger_length_mm(board)
        length = p.finger_length if p.finger_length is not None else longest
        if not (margin < length <= longest):
            raise CommandError(
                "invalid-edge-connector",
                f"Finger length is measured in from the board edge and must be over "
                f"{margin:g} mm (to reach its own hole) and at most {longest:g} mm (before it "
                f"reaches the next hole in); got {length:g} mm.",
            )

        id_ = p.id if p.id is not None else ctx.next_id("ec")
        if any(e.id == id_ for e in doc.edge_connectors):
            raise CommandError("duplicate-id", f'An edge connector with id "{id_}" already exists.')

        connector = EdgeConnector(
            id=id_,
            edge=p.edge,
            start=p.start,
            count=p.count,
            finger_width=p.finger_width,
            finger_length=p.finger_length,
            face=p.face,
        )
        # Two fingers on one hole is one pad claimed twice, and everything downstream --
        # the renderer, the gap maths -- would silently use whichever came first.
        taken = {
            (h.col, h.row)
            for existing in doc.edge_connectors
            for h in edge_connector_holes(existing, board)
        }
        clash = next(
            (h for h in edge_connector_holes(connector, board) if (h.col, h.row) in taken), None
        )
        if clash is not None:
            raise CommandError(
                "overlapping-edge-connector",
                f"Another edge connector already has a finger at {format_hole(clash)}.",
            )
        return dataclasses.replace(doc, edge_connectors=(*doc.edge_connectors, connector))

    def describe(self, p: AddEdgeConnectorPayload, doc: PerfDocument) -> str:
        return f"Add {p.count}-finger edge connector on the {p.edge} edge"


class _DeleteEdgeConnector:
    type = "edge-connector.delete"

    def apply(
        self, doc: PerfDocument, p: DeleteEdgeConnectorPayload, ctx: CommandContext
    ) -> PerfDocument:
        if not any(e.id == p.id for e in doc.edge_connectors):
            raise CommandError("edge-connector-not-found", f'No edge connector with id "{p.id}".')
        return dataclasses.replace(
            doc, edge_connectors=tuple(e for e in doc.edge_connectors if e.id != p.id)
        )

    def describe(self, p: DeleteEdgeConnectorPayload, doc: PerfDocument) -> str:
        return f"Remove edge connector {p.id}"


class _SetHeightLimit:
    type = "height-limit.set"

    def apply(
        self, doc: PerfDocument, p: SetHeightLimitPayload, ctx: CommandContext
    ) -> PerfDocument:
        if p.height_limit_mm is not None and not (p.height_limit_mm > 0):
            raise CommandError(
                "invalid-height-limit",
                f"A height limit must be a positive number of millimetres; got "
                f"{p.height_limit_mm}. Pass null to remove the limit instead.",
            )
        return dataclasses.replace(doc, height_limit_mm=p.height_limit_mm)

    def describe(self, p: SetHeightLimitPayload, doc: PerfDocument) -> str:
        if p.height_limit_mm is None:
            return "Remove the build height limit"
        return f"Limit build height to {p.height_limit_mm:g} mm"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

place_component: CommandDefinition[PlaceComponentPayload] = _PlaceComponent()
place_block: CommandDefinition[PlaceBlockPayload] = _PlaceBlock()
move_component: CommandDefinition[MoveComponentPayload] = _MoveComponent()
move_components: CommandDefinition[MoveComponentsPayload] = _MoveComponents()
rotate_component: CommandDefinition[RotateComponentPayload] = _RotateComponent()
mirror_component: CommandDefinition[MirrorComponentPayload] = _MirrorComponent()
update_component: CommandDefinition[UpdateComponentPayload] = _UpdateComponent()
delete_component: CommandDefinition[DeleteComponentPayload] = _DeleteComponent()
unplace_component: CommandDefinition[UnplaceComponentPayload] = _UnplaceComponent()
add_part: CommandDefinition[AddPartPayload] = _AddPart()
update_part: CommandDefinition[UpdatePartPayload] = _UpdatePart()
delete_part: CommandDefinition[DeletePartPayload] = _DeletePart()
place_parts: CommandDefinition[PlacePartsPayload] = _PlaceParts()
add_conductor: CommandDefinition[AddConductorPayload] = _AddConductor()
add_conductors: CommandDefinition[AddConductorsPayload] = _AddConductors()
set_conductor_path: CommandDefinition[SetConductorPathPayload] = _SetConductorPath()
delete_conductor: CommandDefinition[DeleteConductorPayload] = _DeleteConductor()
delete_conductors: CommandDefinition[DeleteConductorsPayload] = _DeleteConductors()
replace_conductors: CommandDefinition[ReplaceConductorsPayload] = _ReplaceConductors()
set_board: CommandDefinition[SetBoardPayload] = _SetBoard()
apply_board_preset: CommandDefinition[ApplyBoardPresetPayload] = _ApplyBoardPreset()
import_netlist: CommandDefinition[ImportNetlistPayload] = _ImportNetlist()
add_net: CommandDefinition[AddNetPayload] = _AddNet()
update_net: CommandDefinition[UpdateNetPayload] = _UpdateNet()
delete_net: CommandDefinition[DeleteNetPayload] = _DeleteNet()
connect_pins: CommandDefinition[ConnectPinsPayload] = _ConnectPins()
disconnect_pins: CommandDefinition[DisconnectPinsPayload] = _DisconnectPins()
add_cut: CommandDefinition[AddCutPayload] = _AddCut()
apply_stripboard_plan: CommandDefinition[ApplyStripboardPlanPayload] = _ApplyStripboardPlan()
delete_cut: CommandDefinition[DeleteCutPayload] = _DeleteCut()
add_mounting_hole: CommandDefinition[AddMountingHolePayload] = _AddMountingHole()
add_mounting_holes: CommandDefinition[AddMountingHolesPayload] = _AddMountingHoles()
delete_mounting_hole: CommandDefinition[DeleteMountingHolePayload] = _DeleteMountingHole()
add_edge_connector: CommandDefinition[AddEdgeConnectorPayload] = _AddEdgeConnector()
delete_edge_connector: CommandDefinition[DeleteEdgeConnectorPayload] = _DeleteEdgeConnector()
set_height_limit: CommandDefinition[SetHeightLimitPayload] = _SetHeightLimit()

# Typed with Any because CommandDefinition's payload is contravariant, so a specific
# command is deliberately NOT assignable to CommandDefinition[object]. See the note on
# TPayload in command.py.
STANDARD_COMMANDS: tuple[CommandDefinition[Any], ...] = (
    place_component,
    place_block,
    move_component,
    move_components,
    rotate_component,
    mirror_component,
    update_component,
    delete_component,
    unplace_component,
    add_part,
    update_part,
    delete_part,
    place_parts,
    add_conductor,
    add_conductors,
    set_conductor_path,
    delete_conductor,
    delete_conductors,
    replace_conductors,
    set_board,
    apply_board_preset,
    import_netlist,
    add_net,
    update_net,
    delete_net,
    connect_pins,
    disconnect_pins,
    add_cut,
    apply_stripboard_plan,
    delete_cut,
    add_mounting_hole,
    add_mounting_holes,
    delete_mounting_hole,
    add_edge_connector,
    delete_edge_connector,
    set_height_limit,
)


def create_standard_registry() -> CommandRegistry:
    """A registry with every standard command registered."""
    registry = CommandRegistry()
    for definition in STANDARD_COMMANDS:
        registry.register(definition)
    return registry
