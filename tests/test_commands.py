"""Tests for the command bus and the standard command set.

Ported from packages/core/src/commands.test.ts, one for one, keeping each test's
intent (see the section comments below, which mirror the original `describe`
blocks). One test is new: dispatching a sequence, round-tripping its journal
through JSON, replaying that, and asserting the same document -- proving the
journal survives the wire format it is meant for (see
``test_journal_survives_a_json_round_trip`` at the bottom).

Why port the tests at all rather than trust the port by inspection: this suite is
the executable specification of command-bus behaviour. A Python reimplementation
that merely "looks equivalent" to command.ts/commands.ts is not verified; one that
reproduces every pinned behaviour -- including the deliberately surprising ones,
like a solder trace refusing a diagonal step while an insulated wire accepts one --
is.
"""

from __future__ import annotations

import dataclasses
import json

from perfstudio.command import (
    CommandBus,
    CommandContext,
    CommandRecord,
    create_id_generator,
    replay,
)
from perfstudio.commands import (
    DEFAULT_BOARD,
    STANDARD_COMMANDS,
    AddConductorPayload,
    AddConductorsPayload,
    AddCutPayload,
    AddNetPayload,
    AddPartPayload,
    ComponentPlacement,
    ConnectPinsPayload,
    DeleteComponentPayload,
    DeleteConductorsPayload,
    DeleteNetPayload,
    DeletePartPayload,
    DisconnectPinsPayload,
    MirrorComponentPayload,
    MoveComponentPayload,
    MoveComponentsPayload,
    NewLeadBendConductor,
    NewSolderTraceConductor,
    NewStripConductor,
    NewWireConductor,
    PartPlacement,
    PlaceComponentPayload,
    PlacePartsPayload,
    ReplaceConductorsPayload,
    RotateComponentPayload,
    SetBoardPayload,
    SetHeightLimitPayload,
    UnplaceComponentPayload,
    UpdateComponentPayload,
    UpdateNetPayload,
    UpdatePartPayload,
    create_document_id_generator,
    create_empty_document,
    create_standard_registry,
)
from perfstudio.model import (
    DocumentMeta,
    HoleCoord,
    Net,
    NetNode,
    PerfDocument,
    SolderTraceConductor,
    SpineSpec,
)
from perfstudio.persist import parse_document_or_throw, serialize_document

META = DocumentMeta(
    name="test",
    created="2026-01-01T00:00:00.000Z",
    modified="2026-01-01T00:00:00.000Z",
)


def new_bus(doc: PerfDocument | None = None) -> CommandBus:
    return CommandBus(
        doc if doc is not None else create_empty_document(META),
        create_standard_registry(),
        CommandContext(next_id=create_id_generator()),
    )


def place_r1(bus: CommandBus, col: int = 2, row: int = 2):
    return bus.dispatch(
        "component.place",
        PlaceComponentPayload(ref="R1", value="10k", footprint_id="r-axial-5", anchor=HoleCoord(col, row)),
    )


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registers_every_standard_command_exactly_once():
    registry = create_standard_registry()
    assert len(registry.types()) == len(STANDARD_COMMANDS)
    unique = {c.type for c in STANDARD_COMMANDS}
    assert len(unique) == len(STANDARD_COMMANDS)


def test_rejects_an_unknown_command_type_without_throwing():
    bus = new_bus()
    result = bus.dispatch("component.teleport", {})
    assert result.ok is False
    assert result.code == "unknown-command"


# ---------------------------------------------------------------------------
# component.place
# ---------------------------------------------------------------------------


def test_places_a_component_and_generates_a_deterministic_id():
    bus = new_bus()
    result = place_r1(bus)
    assert result.ok is True
    assert len(bus.document.components) == 1
    assert bus.document.components[0].id == "cmp-1"
    assert bus.document.components[0].rotation == 0
    assert result.description == "Place R1 at C3"


def test_refuses_a_duplicate_ref():
    bus = new_bus()
    place_r1(bus)
    again = bus.dispatch(
        "component.place",
        PlaceComponentPayload(ref="R1", value="4k7", footprint_id="r-axial-5", anchor=HoleCoord(9, 9)),
    )
    assert again.ok is False
    assert again.code == "duplicate-ref"
    assert len(bus.document.components) == 1


def test_refuses_an_anchor_off_the_board():
    bus = new_bus()
    result = bus.dispatch(
        "component.place",
        PlaceComponentPayload(
            ref="R9", value="1k", footprint_id="r-axial-5", anchor=HoleCoord(DEFAULT_BOARD.cols, 0)
        ),
    )
    assert result.ok is False
    assert result.code == "off-board"


def test_refuses_an_invalid_rotation():
    bus = new_bus()
    result = bus.dispatch(
        "component.place",
        PlaceComponentPayload(
            ref="R2",
            value="1k",
            footprint_id="r-axial-5",
            anchor=HoleCoord(1, 1),
            rotation=45,  # type: ignore[arg-type]  # deliberately invalid, like the TS test
        ),
    )
    assert result.ok is False
    assert result.code == "invalid-rotation"


# ---------------------------------------------------------------------------
# component mutation and locking
# ---------------------------------------------------------------------------


def test_moves_rotates_and_mirrors():
    bus = new_bus()
    place_r1(bus)
    bus.dispatch("component.move", MoveComponentPayload(id="cmp-1", anchor=HoleCoord(5, 6)))
    bus.dispatch("component.rotate", RotateComponentPayload(id="cmp-1", rotation=90))
    bus.dispatch("component.mirror", MirrorComponentPayload(id="cmp-1", mirrored=True))
    c = bus.document.components[0]
    assert c.anchor == HoleCoord(5, 6)
    assert c.rotation == 90
    assert c.mirrored is True


def test_refuses_to_move_a_locked_component():
    bus = new_bus()
    place_r1(bus)
    bus.dispatch("component.update", UpdateComponentPayload(id="cmp-1", locked=True))
    result = bus.dispatch("component.move", MoveComponentPayload(id="cmp-1", anchor=HoleCoord(8, 8)))
    assert result.ok is False
    assert result.code == "component-locked"


# ---------------------------------------------------------------------------
# component.moveMany -- the placer's single undo step
# ---------------------------------------------------------------------------


def place_pair(bus: CommandBus):
    place_r1(bus)
    bus.dispatch(
        "component.place",
        PlaceComponentPayload(ref="R2", value="1k", footprint_id="r-axial-5", anchor=HoleCoord(9, 2)),
    )


def test_moves_several_components_as_one_command():
    bus = new_bus()
    place_pair(bus)
    before = bus.document

    result = bus.dispatch(
        "component.moveMany",
        MoveComponentsPayload(
            placements=(
                ComponentPlacement(id="cmp-1", anchor=HoleCoord(4, 5), rotation=90),
                ComponentPlacement(id="cmp-2", anchor=HoleCoord(12, 7)),
            ),
            label="Auto-place 2 component(s)",
        ),
    )

    assert result.ok, result.message
    by_id = {c.id: c for c in bus.document.components}
    assert by_id["cmp-1"].anchor == HoleCoord(4, 5)
    assert by_id["cmp-1"].rotation == 90
    assert by_id["cmp-2"].anchor == HoleCoord(12, 7)
    # Rotation omitted means "leave it", not "reset to zero".
    assert by_id["cmp-2"].rotation == 0

    bus.undo()
    assert bus.document is before


def test_the_batch_label_is_what_the_undo_stack_shows():
    bus = new_bus()
    place_pair(bus)
    bus.dispatch(
        "component.moveMany",
        MoveComponentsPayload(
            placements=(ComponentPlacement(id="cmp-1", anchor=HoleCoord(4, 5)),),
            label="Auto-place 1 component(s)",
        ),
    )
    assert bus.history()[-1] == "Auto-place 1 component(s)"


def test_falls_back_to_a_count_when_the_batch_has_no_label():
    bus = new_bus()
    place_pair(bus)
    bus.dispatch(
        "component.moveMany",
        MoveComponentsPayload(placements=(ComponentPlacement(id="cmp-1", anchor=HoleCoord(4, 5)),)),
    )
    assert bus.history()[-1] == "Move 1 component(s)"


def test_refuses_an_empty_batch_of_placements():
    bus = new_bus()
    place_r1(bus)
    result = bus.dispatch("component.moveMany", MoveComponentsPayload(placements=()))
    assert result.ok is False
    assert result.code == "nothing-to-move"


def test_a_locked_member_refuses_the_whole_batch():
    """All-or-nothing, like the batch conductor commands: a half-applied placement leaves
    the board in an arrangement the optimiser never proposed and nobody chose."""
    bus = new_bus()
    place_pair(bus)
    bus.dispatch("component.update", UpdateComponentPayload(id="cmp-2", locked=True))
    before = bus.document

    result = bus.dispatch(
        "component.moveMany",
        MoveComponentsPayload(
            placements=(
                ComponentPlacement(id="cmp-1", anchor=HoleCoord(4, 5)),
                ComponentPlacement(id="cmp-2", anchor=HoleCoord(12, 7)),
            )
        ),
    )

    assert result.ok is False
    assert result.code == "component-locked"
    assert bus.document is before


def test_an_off_board_member_refuses_the_whole_batch():
    bus = new_bus()
    place_pair(bus)
    before = bus.document

    result = bus.dispatch(
        "component.moveMany",
        MoveComponentsPayload(
            placements=(
                ComponentPlacement(id="cmp-1", anchor=HoleCoord(4, 5)),
                ComponentPlacement(id="cmp-2", anchor=HoleCoord(-3, 7)),
            )
        ),
    )

    assert result.ok is False
    assert bus.document is before


def test_the_same_component_twice_in_one_batch_is_an_error():
    """Applying the last one silently would hide a caller bug behind a plausible board."""
    bus = new_bus()
    place_pair(bus)

    result = bus.dispatch(
        "component.moveMany",
        MoveComponentsPayload(
            placements=(
                ComponentPlacement(id="cmp-1", anchor=HoleCoord(4, 5)),
                ComponentPlacement(id="cmp-1", anchor=HoleCoord(6, 5)),
            )
        ),
    )

    assert result.ok is False
    assert result.code == "duplicate-component"


def test_an_unknown_id_refuses_the_batch_without_raising():
    bus = new_bus()
    place_r1(bus)
    result = bus.dispatch(
        "component.moveMany",
        MoveComponentsPayload(placements=(ComponentPlacement(id="cmp-99", anchor=HoleCoord(4, 5)),)),
    )
    assert result.ok is False


def test_an_invalid_rotation_in_a_batch_is_refused():
    bus = new_bus()
    place_r1(bus)
    result = bus.dispatch(
        "component.moveMany",
        MoveComponentsPayload(
            placements=(ComponentPlacement(id="cmp-1", anchor=HoleCoord(4, 5), rotation=45),)
        ),
    )
    assert result.ok is False


def test_deleting_a_component_also_removes_its_lead_bends_but_keeps_other_routing():
    bus = new_bus()
    place_r1(bus)
    bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewLeadBendConductor(
                path=(HoleCoord(2, 2), HoleCoord(4, 2)),
                component_id="cmp-1",
                pin_number="1",
            )
        ),
    )
    bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewWireConductor(
                path=(HoleCoord(10, 10), HoleCoord(14, 10)),
                kind="bare-wire",
            )
        ),
    )
    assert len(bus.document.conductors) == 2

    bus.dispatch("component.delete", DeleteComponentPayload(id="cmp-1"))
    assert len(bus.document.components) == 0
    assert len(bus.document.conductors) == 1
    assert bus.document.conductors[0].kind == "bare-wire"


# ---------------------------------------------------------------------------
# conductor.add
# ---------------------------------------------------------------------------


def test_accepts_an_orthogonal_solder_trace():
    bus = new_bus()
    result = bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewSolderTraceConductor(
                path=(HoleCoord(1, 1), HoleCoord(2, 1), HoleCoord(3, 1)),
                buildup="normal",
            )
        ),
    )
    assert result.ok is True
    c = bus.document.conductors[0]
    assert isinstance(c, SolderTraceConductor)
    assert c.kind == "solder-trace"
    assert c.buildup == "normal"


def test_refuses_a_solder_trace_with_a_diagonal_step():
    bus = new_bus()
    result = bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewSolderTraceConductor(
                path=(HoleCoord(1, 1), HoleCoord(2, 2)),
                buildup="normal",
            )
        ),
    )
    assert result.ok is False
    assert result.code == "non-orthogonal-path"


def test_allows_a_diagonal_insulated_wire_which_is_physically_fine():
    bus = new_bus()
    result = bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewWireConductor(
                path=(HoleCoord(1, 1), HoleCoord(9, 7)),
                kind="insulated-wire",
                layer_z=1,
            )
        ),
    )
    assert result.ok is True


def test_refuses_a_lead_bend_referencing_a_component_that_does_not_exist():
    bus = new_bus()
    result = bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewLeadBendConductor(
                path=(HoleCoord(1, 1), HoleCoord(2, 1)),
                component_id="nope",
                pin_number="1",
            )
        ),
    )
    assert result.ok is False
    assert result.code == "component-not-found"


def test_refuses_a_path_that_leaves_the_board():
    bus = new_bus()
    result = bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewWireConductor(
                path=(HoleCoord(0, 0), HoleCoord(DEFAULT_BOARD.cols + 5, 0)),
                kind="bare-wire",
            )
        ),
    )
    assert result.ok is False
    assert result.code == "off-board"


# ---------------------------------------------------------------------------
# conductor.addMany
# ---------------------------------------------------------------------------


def two_traces() -> tuple[NewSolderTraceConductor, NewSolderTraceConductor]:
    return (
        NewSolderTraceConductor(path=(HoleCoord(2, 2), HoleCoord(3, 2), HoleCoord(4, 2))),
        NewSolderTraceConductor(path=(HoleCoord(2, 6), HoleCoord(3, 6))),
    )


def test_adds_every_conductor_as_a_single_undo_step():
    """The reason this command exists: a planner's output is one thing the user accepted,
    so it has to be one thing they can take back."""
    bus = new_bus()

    result = bus.dispatch("conductor.addMany", AddConductorsPayload(conductors=two_traces()))

    assert result.ok is True
    assert len(bus.document.conductors) == 2
    assert len(bus.journal()) == 1

    bus.undo()

    assert bus.document.conductors == ()


def test_uses_the_supplied_label_as_the_undo_description():
    bus = new_bus()

    result = bus.dispatch(
        "conductor.addMany",
        AddConductorsPayload(conductors=two_traces(), label="Autoroute 2 nets"),
    )

    assert result.description == "Autoroute 2 nets"
    assert bus.history() == ("Autoroute 2 nets",)


def test_falls_back_to_a_count_when_no_label_is_given():
    bus = new_bus()

    result = bus.dispatch("conductor.addMany", AddConductorsPayload(conductors=two_traces()))

    assert result.description == "Add 2 conductor(s)"


def test_refuses_an_empty_batch_rather_than_putting_a_no_op_on_the_undo_stack():
    bus = new_bus()

    result = bus.dispatch("conductor.addMany", AddConductorsPayload(conductors=()))

    assert result.ok is False
    assert result.code == "nothing-to-add"
    assert bus.journal() == ()


def test_a_bad_member_refuses_the_whole_batch_and_leaves_nothing_behind():
    """All or nothing. A half-applied plan is worse than a rejected one, because the user
    cannot tell which half they got."""
    bus = new_bus()
    good, _ = two_traces()
    diagonal = NewSolderTraceConductor(path=(HoleCoord(2, 2), HoleCoord(3, 3)))

    result = bus.dispatch("conductor.addMany", AddConductorsPayload(conductors=(good, diagonal)))

    assert result.ok is False
    assert result.code == "non-orthogonal-path"
    assert bus.document.conductors == ()


def test_supplied_ids_are_used_so_a_batch_replays_reproducibly():
    bus = new_bus()

    result = bus.dispatch(
        "conductor.addMany",
        AddConductorsPayload(conductors=two_traces(), ids=("rail-1", "rail-2")),
    )

    assert result.ok is True
    assert [c.id for c in bus.document.conductors] == ["rail-1", "rail-2"]


def test_the_wrong_number_of_ids_is_an_error_not_a_partial_mapping():
    bus = new_bus()

    result = bus.dispatch(
        "conductor.addMany", AddConductorsPayload(conductors=two_traces(), ids=("only-one",))
    )

    assert result.ok is False
    assert result.code == "id-count-mismatch"


def test_a_duplicate_id_within_one_batch_is_caught():
    bus = new_bus()

    result = bus.dispatch(
        "conductor.addMany", AddConductorsPayload(conductors=two_traces(), ids=("same", "same"))
    )

    assert result.ok is False
    assert result.code == "duplicate-id"


def test_a_batch_is_validated_exactly_as_a_single_add_is():
    """The batch exists to save undo entries, not to skip checks -- so a path that
    conductor.add refuses must be refused here with the same code."""
    off_board = NewSolderTraceConductor(path=(HoleCoord(0, 0), HoleCoord(-1, 0)))
    single = new_bus().dispatch("conductor.add", AddConductorPayload(conductor=off_board))
    batch = new_bus().dispatch("conductor.addMany", AddConductorsPayload(conductors=(off_board,)))

    assert single.ok is batch.ok is False
    assert single.code == batch.code


# ---------------------------------------------------------------------------
# Id generation against a document that already has ids
# ---------------------------------------------------------------------------


def test_a_loaded_document_can_be_edited_without_colliding_with_its_own_ids():
    """A bare create_id_generator() restarts at zero, so the next conductor.add on a
    document whose conductors are already cond-1.. would be refused as a duplicate. Any
    host that opens a file and then edits it needs create_document_id_generator."""
    loaded = new_bus()
    loaded.dispatch("conductor.addMany", AddConductorsPayload(conductors=two_traces()))
    document = loaded.document
    assert [c.id for c in document.conductors] == ["cond-1", "cond-2"]

    naive = CommandBus(
        document, create_standard_registry(), CommandContext(next_id=create_id_generator())
    )
    seeded = CommandBus(
        document,
        create_standard_registry(),
        CommandContext(next_id=create_document_id_generator(document)),
    )
    another = NewSolderTraceConductor(path=(HoleCoord(2, 9), HoleCoord(3, 9)))

    assert naive.dispatch("conductor.add", AddConductorPayload(conductor=another)).code == "duplicate-id"
    assert seeded.dispatch("conductor.add", AddConductorPayload(conductor=another)).ok is True
    assert seeded.document.conductors[-1].id == "cond-3"


def test_the_seeded_generator_counts_each_prefix_separately():
    bus = new_bus()
    place_r1(bus)
    bus.dispatch("conductor.addMany", AddConductorsPayload(conductors=two_traces()))
    next_id = create_document_id_generator(bus.document)

    assert next_id("cond") == "cond-3"
    assert next_id("cmp") == "cmp-2"
    assert next_id("cut") == "cut-1"  # No cuts in the document, so this prefix starts fresh.


def test_the_seeded_generator_ignores_ids_it_could_not_have_produced():
    """Only `prefix-<digits>` ids can collide with the generator. A hand-written or
    imported id is left alone rather than guessed at."""
    bus = new_bus()
    bus.dispatch(
        "conductor.addMany",
        AddConductorsPayload(conductors=two_traces(), ids=("gnd-rail", "vcc-rail")),
    )

    assert create_document_id_generator(bus.document)("cond") == "cond-1"


# ---------------------------------------------------------------------------
# board.set
# ---------------------------------------------------------------------------


def test_refuses_a_shrink_that_would_strand_a_placed_component():
    bus = new_bus()
    bus.dispatch(
        "component.place",
        PlaceComponentPayload(ref="U1", value="NE555", footprint_id="dip-8", anchor=HoleCoord(50, 30)),
    )
    result = bus.dispatch(
        "board.set", SetBoardPayload(board=dataclasses.replace(DEFAULT_BOARD, cols=20, rows=20))
    )
    assert result.ok is False
    assert result.code == "would-strand-component"
    assert "U1" in result.message


def test_refuses_a_drill_diameter_that_is_not_smaller_than_the_pad():
    bus = new_bus()
    result = bus.dispatch(
        "board.set",
        SetBoardPayload(board=dataclasses.replace(DEFAULT_BOARD, pad_diameter=1.0, drill_diameter=1.0)),
    )
    assert result.ok is False
    assert result.code == "invalid-board"


# ---------------------------------------------------------------------------
# cut.add
# ---------------------------------------------------------------------------


def test_is_refused_on_a_pad_per_hole_board():
    bus = new_bus()
    result = bus.dispatch("cut.add", AddCutPayload(at=HoleCoord(3, 3)))
    assert result.ok is False
    assert result.code == "not-stripboard"


def test_is_allowed_on_stripboard():
    stripboard = dataclasses.replace(DEFAULT_BOARD, type="stripboard", strip_axis="horizontal")
    bus = new_bus(create_empty_document(META, stripboard))
    result = bus.dispatch("cut.add", AddCutPayload(at=HoleCoord(3, 3)))
    assert result.ok is True
    assert len(bus.document.cuts) == 1


# ---------------------------------------------------------------------------
# The guarantees the command bus exists to provide
# ---------------------------------------------------------------------------


def test_undo_restores_the_exact_previous_document_by_reference():
    bus = new_bus()
    before = bus.document
    place_r1(bus)
    after = bus.document
    assert after is not before

    bus.undo()
    assert bus.document is before  # identity, not just deep equality

    bus.redo()
    assert bus.document is after


def test_a_failed_command_leaves_history_untouched():
    bus = new_bus()
    place_r1(bus)
    history_before = len(bus.history())
    bus.dispatch("component.move", MoveComponentPayload(id="does-not-exist", anchor=HoleCoord(1, 1)))
    assert len(bus.history()) == history_before
    assert bus.can_redo() is False


def test_dispatching_after_an_undo_clears_the_redo_stack():
    bus = new_bus()
    place_r1(bus)
    bus.undo()
    assert bus.can_redo() is True
    bus.dispatch(
        "component.place",
        PlaceComponentPayload(ref="R2", value="1k", footprint_id="r-axial-5", anchor=HoleCoord(7, 7)),
    )
    assert bus.can_redo() is False


# ---------------------------------------------------------------------------
# deterministic replay
# ---------------------------------------------------------------------------


def _dispatch_representative_sequence(bus: CommandBus) -> None:
    """Places two components, moves one, and routes a solder-trace-wired conductor
    with a spine -- enough variety (optional defaults, an update, a nested union
    member, a nested dataclass) to exercise replay and JSON round-tripping alike."""
    place_r1(bus)
    bus.dispatch(
        "component.place",
        PlaceComponentPayload(ref="C1", value="100nF", footprint_id="c-disc-1", anchor=HoleCoord(6, 2)),
    )
    bus.dispatch("component.move", MoveComponentPayload(id="cmp-1", anchor=HoleCoord(3, 4)))
    bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewSolderTraceConductor(
                path=(HoleCoord(3, 8), HoleCoord(4, 8), HoleCoord(5, 8)),
                buildup="heavy",
                spine=SpineSpec(material="tinned-copper", gauge=0.6),
                kind="solder-trace-wired",
            )
        ),
    )


def test_replaying_a_journal_onto_a_fresh_document_reproduces_it_exactly():
    bus = new_bus()
    _dispatch_representative_sequence(bus)

    journal = bus.journal()
    assert len(journal) == 4

    # Fresh id generator: ids must come out the same, which is the whole point.
    replayed = replay(
        create_empty_document(META),
        journal,
        create_standard_registry(),
        CommandContext(next_id=create_id_generator()),
    )

    assert replayed.ok is True
    assert replayed.document == bus.document


def test_replay_surfaces_the_first_failing_command_rather_than_continuing():
    registry = create_standard_registry()
    journal = (
        CommandRecord(
            type="component.place",
            payload=PlaceComponentPayload(ref="R1", value="1k", footprint_id="f", anchor=HoleCoord(1, 1)),
        ),
        CommandRecord(
            type="component.move",
            payload=MoveComponentPayload(id="nope", anchor=HoleCoord(2, 2)),
        ),
    )
    result = replay(
        create_empty_document(META), journal, registry, CommandContext(next_id=create_id_generator())
    )
    assert result.ok is False
    assert result.code == "component-not-found"


# ---------------------------------------------------------------------------
# immutability
# ---------------------------------------------------------------------------


def test_never_mutates_the_previous_document():
    bus = new_bus()
    before = bus.document
    snapshot = dataclasses.asdict(before)
    place_r1(bus)
    bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewWireConductor(path=(HoleCoord(1, 1), HoleCoord(5, 1)), kind="bare-wire")
        ),
    )
    assert dataclasses.asdict(before) == snapshot


# ---------------------------------------------------------------------------
# journal as a wire format (new: not in commands.test.ts)
# ---------------------------------------------------------------------------

_NEW_CONDUCTOR_KIND_TO_CLASS = {
    "solder-trace": NewSolderTraceConductor,
    "solder-trace-wired": NewSolderTraceConductor,
    "bare-wire": NewWireConductor,
    "insulated-wire": NewWireConductor,
    "top-jumper": NewWireConductor,
    "lead-bend": NewLeadBendConductor,
    "strip": NewStripConductor,
}


def _hole_from_json(d: dict) -> HoleCoord:
    return HoleCoord(col=d["col"], row=d["row"])


def _path_from_json(items: list) -> tuple[HoleCoord, ...]:
    return tuple(_hole_from_json(h) for h in items)


def _new_conductor_from_json(d: dict):
    cls = _NEW_CONDUCTOR_KIND_TO_CLASS[d["kind"]]
    data = dict(d)
    data["path"] = _path_from_json(data["path"])
    if "spine" in data and data["spine"] is not None:
        data["spine"] = SpineSpec(**data["spine"])
    return cls(**data)


def _payload_from_json(type_: str, d: dict):
    """Minimal decoder covering the command types _dispatch_representative_sequence
    uses. Demonstrates the round trip; it is not a general-purpose journal codec."""
    data = dict(d)
    if type_ == "component.place":
        data["anchor"] = _hole_from_json(data["anchor"])
        return PlaceComponentPayload(**data)
    if type_ == "component.move":
        data["anchor"] = _hole_from_json(data["anchor"])
        return MoveComponentPayload(**data)
    if type_ == "conductor.add":
        data["conductor"] = _new_conductor_from_json(data["conductor"])
        return AddConductorPayload(**data)
    raise NotImplementedError(f"no JSON decoder wired up for {type_!r} in this test")


def test_journal_survives_a_json_round_trip():
    """The journal is a wire format: it must survive being written out as JSON by
    one process (e.g. an MCP server) and read back by another (e.g. a CLI replay)."""
    bus = new_bus()
    _dispatch_representative_sequence(bus)
    journal = bus.journal()

    wire = json.dumps([dataclasses.asdict(record) for record in journal])
    raw = json.loads(wire)

    decoded = tuple(
        CommandRecord(type=entry["type"], payload=_payload_from_json(entry["type"], entry["payload"]))
        for entry in raw
    )

    replayed = replay(
        create_empty_document(META),
        decoded,
        create_standard_registry(),
        CommandContext(next_id=create_id_generator()),
    )

    assert replayed.ok is True
    assert replayed.document == bus.document


def test_a_negative_off_board_move_is_refused_not_raised() -> None:
    """dispatch() must never raise for bad input, including at negative coordinates.

    The off-board message names the offending hole, and an earlier version formatted it
    with the strict `coord_to_hole_ref`, which rejects negative columns by design. So a
    component dragged off the LEFT edge crashed inside the error formatter instead of
    being refused -- the checker failing on exactly the case it exists to report.

    `geometry.format_hole` exists for this, and its docstring warns about this mistake.
    Dragging a part past the left or top edge is an ordinary thing a user does, so this
    is pinned here for both axes as well as the positive overflow that always worked.
    """
    bus = new_bus()
    place_r1(bus)
    before = bus.document

    for anchor in (HoleCoord(-1, 5), HoleCoord(5, -3), HoleCoord(-4, -4), HoleCoord(999, 5)):
        result = bus.dispatch("component.move", MoveComponentPayload(id="cmp-1", anchor=anchor))
        assert result.ok is False
        assert result.code == "off-board"
        assert result.message  # a usable message, not an empty string
        assert bus.document is before, "a refused move must leave the document untouched"


# ---------------------------------------------------------------------------
# conductor.deleteMany
# ---------------------------------------------------------------------------


def test_deletes_every_conductor_as_a_single_undo_step():
    """Clearing the copper a moved part left behind is one decision to the user; it should take
    one Ctrl+Z, not one per conductor."""
    bus = new_bus()
    bus.dispatch("conductor.addMany", AddConductorsPayload(conductors=two_traces()))
    ids = tuple(c.id for c in bus.document.conductors)

    result = bus.dispatch(
        "conductor.deleteMany", DeleteConductorsPayload(ids=ids, label="Remove 2 stale conductor(s)")
    )

    assert result.ok is True
    assert result.description == "Remove 2 stale conductor(s)"
    assert bus.document.conductors == ()

    bus.undo()

    assert len(bus.document.conductors) == 2


def test_an_unknown_id_refuses_the_whole_batch():
    """All or nothing: a partly-applied cleanup leaves the user unable to tell what went."""
    bus = new_bus()
    bus.dispatch("conductor.addMany", AddConductorsPayload(conductors=two_traces()))

    result = bus.dispatch(
        "conductor.deleteMany", DeleteConductorsPayload(ids=("cond-1", "nope"))
    )

    assert result.ok is False
    assert result.code == "conductor-not-found"
    assert len(bus.document.conductors) == 2


def test_an_empty_delete_batch_is_refused():
    bus = new_bus()

    result = bus.dispatch("conductor.deleteMany", DeleteConductorsPayload(ids=()))

    assert result.ok is False
    assert result.code == "nothing-to-delete"
    assert bus.journal() == ()


# ---------------------------------------------------------------------------
# conductor.replace -- rip-up and re-route as one step
# ---------------------------------------------------------------------------


def _two_traces(bus: CommandBus):
    bus.dispatch(
        "conductor.addMany",
        AddConductorsPayload(
            conductors=(
                NewSolderTraceConductor(path=(HoleCoord(2, 2), HoleCoord(3, 2))),
                NewSolderTraceConductor(path=(HoleCoord(5, 5), HoleCoord(6, 5))),
            )
        ),
    )
    return [c.id for c in bus.document.conductors]


def test_replaces_conductors_in_one_command():
    bus = new_bus()
    first, second = _two_traces(bus)
    before = bus.document

    result = bus.dispatch(
        "conductor.replace",
        ReplaceConductorsPayload(
            remove_ids=(first,),
            conductors=(NewSolderTraceConductor(path=(HoleCoord(8, 8), HoleCoord(9, 8))),),
            label="Re-route GND",
        ),
    )

    assert result.ok, result.message
    ids = [c.id for c in bus.document.conductors]
    assert first not in ids
    assert second in ids
    assert len(ids) == 2
    assert bus.history()[-1] == "Re-route GND"

    bus.undo()
    assert bus.document is before


def test_replace_is_all_or_nothing():
    """A half-applied re-route leaves a net ripped up with nothing put back -- a board
    the planner never proposed and nobody chose."""
    bus = new_bus()
    first, _second = _two_traces(bus)
    before = bus.document

    result = bus.dispatch(
        "conductor.replace",
        ReplaceConductorsPayload(
            remove_ids=(first,),
            # Diagonal: refused by exactly the same check conductor.add applies.
            conductors=(NewSolderTraceConductor(path=(HoleCoord(8, 8), HoleCoord(9, 9))),),
        ),
    )

    assert result.ok is False
    assert bus.document is before


def test_replace_refuses_an_id_that_is_not_there():
    bus = new_bus()
    _two_traces(bus)
    result = bus.dispatch(
        "conductor.replace",
        ReplaceConductorsPayload(
            remove_ids=("cond-99",),
            conductors=(NewSolderTraceConductor(path=(HoleCoord(8, 8), HoleCoord(9, 8))),),
        ),
    )
    assert result.ok is False
    assert result.code == "conductor-not-found"


def test_replace_refuses_a_no_op():
    bus = new_bus()
    result = bus.dispatch(
        "conductor.replace", ReplaceConductorsPayload(remove_ids=(), conductors=())
    )
    assert result.ok is False
    assert result.code == "nothing-to-do"


def test_replace_can_reuse_a_hole_the_removed_conductor_occupied():
    """The new conductors are validated against the document AFTER the removals, which is
    the whole reason this is one command and not two."""
    bus = new_bus()
    first, _second = _two_traces(bus)

    result = bus.dispatch(
        "conductor.replace",
        ReplaceConductorsPayload(
            remove_ids=(first,),
            conductors=(NewSolderTraceConductor(path=(HoleCoord(2, 2), HoleCoord(2, 3))),),
        ),
    )

    assert result.ok, result.message


# ---------------------------------------------------------------------------
# height-limit.set
# ---------------------------------------------------------------------------


def test_setting_a_height_limit_records_what_it_did():
    bus = new_bus()

    result = bus.dispatch("height-limit.set", SetHeightLimitPayload(height_limit_mm=22.0))

    assert result.ok, result.message
    assert bus.document.height_limit_mm == 22.0
    assert result.description == "Limit build height to 22 mm"


def test_clearing_a_height_limit_is_a_separate_undo_step():
    """None is not zero: it removes the constraint rather than setting an impossible one,
    and it goes on the history like any other edit so it can be taken back."""
    bus = new_bus()
    bus.dispatch("height-limit.set", SetHeightLimitPayload(height_limit_mm=22.0))

    result = bus.dispatch("height-limit.set", SetHeightLimitPayload(height_limit_mm=None))

    assert result.ok, result.message
    assert bus.document.height_limit_mm is None
    assert result.description == "Remove the build height limit"

    bus.undo()
    assert bus.document.height_limit_mm == 22.0


def test_a_height_limit_of_zero_or_less_is_refused():
    """Nothing fits under it, so every part on the board would be reported. Refused at
    the bus rather than reported by DRC, because it is not a document anybody meant."""
    bus = new_bus()

    for value in (0.0, -5.0):
        result = bus.dispatch("height-limit.set", SetHeightLimitPayload(height_limit_mm=value))
        assert result.ok is False
        assert result.code == "invalid-height-limit"
        assert bus.document.height_limit_mm is None


# ---------------------------------------------------------------------------
# net.add / net.update / net.delete / net.connect / net.disconnect
#
# Before these existed, netlist.import was the only way a net could enter a document,
# which made a schematic capture package a prerequisite for the ratsnest, and so for
# autoroute, LVS and the guide's continuity tests. These pin what the hand-entered
# path may and may not do.
# ---------------------------------------------------------------------------


def test_adds_a_net_with_a_generated_id():
    bus = new_bus()

    result = bus.dispatch("net.add", AddNetPayload(name="GND", net_class="ground"))

    assert result.ok, result.message
    assert [(n.id, n.name, n.net_class) for n in bus.document.nets] == [("net-1", "GND", "ground")]
    assert result.description == "Add ground net GND"


def test_a_net_can_be_declared_with_its_pins_in_one_step():
    bus = new_bus()

    result = bus.dispatch(
        "net.add",
        AddNetPayload(
            name="GND", net_class="ground", nodes=(NetNode("U1", "8"), NetNode("C2", "2"))
        ),
    )

    assert result.ok, result.message
    assert bus.document.nets[0].nodes == (NetNode("U1", "8"), NetNode("C2", "2"))
    assert result.description == "Add ground net GND with 2 pin(s)"


def test_a_generated_net_id_cannot_collide_with_one_already_in_the_document():
    """The counterpart of the conductor case: a file whose nets are already net-1..net-2
    must not have the next net.add refused as a duplicate."""
    doc = dataclasses.replace(
        create_empty_document(META),
        nets=(Net(id="net-1", name="GND", nodes=()), Net(id="net-2", name="+5V", nodes=())),
    )
    bus = CommandBus(
        doc,
        create_standard_registry(),
        CommandContext(next_id=create_document_id_generator(doc)),
    )

    result = bus.dispatch("net.add", AddNetPayload(name="OUT"))

    assert result.ok, result.message
    assert bus.document.nets[-1].id == "net-3"


def test_a_net_name_is_stripped_and_must_be_unique():
    """The name is the handle everything outside the engine uses -- the MCP server
    resolves a net by it, DRC and LVS quote it -- so two of them is not a document."""
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="  GND  "))
    assert bus.document.nets[0].name == "GND"

    clash = bus.dispatch("net.add", AddNetPayload(name="GND"))
    assert clash.ok is False
    assert clash.code == "duplicate-net-name"
    assert len(bus.document.nets) == 1


def test_a_nameless_net_is_refused():
    bus = new_bus()
    result = bus.dispatch("net.add", AddNetPayload(name="   "))
    assert result.ok is False
    assert result.code == "invalid-net-name"


def test_a_pin_may_only_be_on_one_net():
    """Refused rather than moved: moving it would rewrite a net the command did not name."""
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="GND", nodes=(NetNode("U1", "8"),)))

    stolen = bus.dispatch("net.add", AddNetPayload(name="+5V", nodes=(NetNode("U1", "8"),)))

    assert stolen.ok is False
    assert stolen.code == "pin-in-another-net"
    assert "GND" in stolen.message
    assert len(bus.document.nets) == 1


def test_a_net_may_name_a_pin_that_is_not_on_the_board_yet():
    """Deliberate. Importing a netlist and then placing what it asks for is a workflow
    this application already offers, so a node with nothing behind it yet is a perfectly
    good document; the ratsnest reports it as an unresolved pin and LVS raises it."""
    bus = new_bus()

    result = bus.dispatch("net.add", AddNetPayload(name="GND", nodes=(NetNode("U99", "1"),)))

    assert result.ok, result.message
    assert bus.document.nets[0].nodes == (NetNode("U99", "1"),)


def test_connecting_several_pins_is_one_undo_step():
    """A click-a-pin session commits once: an undo that leaves three of five pins
    attached is a state nobody asked for."""
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="GND", net_class="ground"))

    result = bus.dispatch(
        "net.connect",
        ConnectPinsPayload(id="net-1", nodes=(NetNode("U1", "8"), NetNode("C2", "2"))),
    )

    assert result.ok, result.message
    assert len(bus.document.nets[0].nodes) == 2
    assert result.description == "Connect 2 pins to GND"

    bus.undo()
    assert bus.document.nets[0].nodes == ()


def test_connecting_a_pin_the_net_already_has_is_refused():
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="GND", nodes=(NetNode("U1", "8"),)))

    again = bus.dispatch("net.connect", ConnectPinsPayload(id="net-1", nodes=(NetNode("U1", "8"),)))

    assert again.ok is False
    assert again.code == "duplicate-pin"
    assert len(bus.document.nets[0].nodes) == 1


def test_the_same_pin_twice_in_one_batch_is_refused():
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="GND"))

    result = bus.dispatch(
        "net.connect",
        ConnectPinsPayload(id="net-1", nodes=(NetNode("U1", "8"), NetNode("U1", "8"))),
    )

    assert result.ok is False
    assert result.code == "duplicate-pin"
    assert bus.document.nets[0].nodes == ()


def test_connecting_to_a_net_that_is_not_there_names_the_ones_that_are():
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="GND"))

    result = bus.dispatch(
        "net.connect", ConnectPinsPayload(id="net-7", nodes=(NetNode("U1", "8"),))
    )

    assert result.ok is False
    assert result.code == "net-not-found"
    assert "GND" in result.message


def test_an_empty_connect_batch_is_refused():
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="GND"))

    result = bus.dispatch("net.connect", ConnectPinsPayload(id="net-1", nodes=()))

    assert result.ok is False
    assert result.code == "empty-batch"


def test_disconnecting_a_pin_leaves_the_rest_alone():
    bus = new_bus()
    bus.dispatch(
        "net.add",
        AddNetPayload(name="GND", nodes=(NetNode("U1", "8"), NetNode("C2", "2"))),
    )

    result = bus.dispatch(
        "net.disconnect", DisconnectPinsPayload(id="net-1", nodes=(NetNode("U1", "8"),))
    )

    assert result.ok, result.message
    assert bus.document.nets[0].nodes == (NetNode("C2", "2"),)
    assert result.description == "Disconnect U1.8 from GND"


def test_disconnecting_a_pin_the_net_does_not_have_is_refused():
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="GND", nodes=(NetNode("U1", "8"),)))

    result = bus.dispatch(
        "net.disconnect", DisconnectPinsPayload(id="net-1", nodes=(NetNode("U1", "9"),))
    )

    assert result.ok is False
    assert result.code == "pin-not-on-net"
    assert bus.document.nets[0].nodes == (NetNode("U1", "8"),)


def test_renaming_a_net_says_so_in_the_history():
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="N$1"))

    result = bus.dispatch("net.update", UpdateNetPayload(id="net-1", name="GND"))

    assert result.ok, result.message
    assert bus.document.nets[0].name == "GND"
    assert result.description == "Rename net N$1 to GND"


def test_a_net_can_be_renamed_to_the_name_it_already_has():
    """The uniqueness check must ignore the net being renamed, or a no-op rename -- which
    is what re-typing a name into a dialog is -- would be refused as a clash with itself."""
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="GND"))

    result = bus.dispatch("net.update", UpdateNetPayload(id="net-1", name="GND"))

    assert result.ok, result.message


def test_a_nets_current_and_voltage_can_be_stated_and_cleared():
    """Nothing else in the application can set these, and DRC's current-capacity and
    creepage rules and the guide's wire gauge are all silent without them."""
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="+12V", net_class="power"))

    stated = bus.dispatch("net.update", UpdateNetPayload(id="net-1", current_a=2.5, voltage_v=12.0))
    assert stated.ok, stated.message
    assert (bus.document.nets[0].current_a, bus.document.nets[0].voltage_v) == (2.5, 12.0)

    # KEEP is the default, so an update about the name alone leaves both where they were.
    renamed = bus.dispatch("net.update", UpdateNetPayload(id="net-1", name="+12V rail"))
    assert renamed.ok, renamed.message
    assert (bus.document.nets[0].current_a, bus.document.nets[0].voltage_v) == (2.5, 12.0)

    cleared = bus.dispatch("net.update", UpdateNetPayload(id="net-1", current_a=None))
    assert cleared.ok, cleared.message
    assert bus.document.nets[0].current_a is None
    assert bus.document.nets[0].voltage_v == 12.0


def test_a_negative_current_is_refused_and_a_negative_voltage_is_not():
    """-12 V rails exist; -2 A reaches the capacity rule as a wire gauge nobody can cut."""
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="-12V", net_class="power"))

    bad = bus.dispatch("net.update", UpdateNetPayload(id="net-1", current_a=-2.0))
    assert bad.ok is False
    assert bad.code == "invalid-current"

    fine = bus.dispatch("net.update", UpdateNetPayload(id="net-1", voltage_v=-12.0))
    assert fine.ok, fine.message
    assert bus.document.nets[0].voltage_v == -12.0


def test_deleting_a_net_leaves_its_copper_and_releases_the_claim_on_it():
    """The copper is physical and stays. Its net_id is a reference, and a reference to a
    net that is gone is exactly what commands exist to prevent."""
    bus = new_bus()
    bus.dispatch("net.add", AddNetPayload(name="GND", net_class="ground"))
    bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewSolderTraceConductor(
                path=(HoleCoord(1, 1), HoleCoord(2, 1)), net_id="net-1"
            )
        ),
    )

    result = bus.dispatch("net.delete", DeleteNetPayload(id="net-1"))

    assert result.ok, result.message
    assert bus.document.nets == ()
    assert len(bus.document.conductors) == 1
    assert bus.document.conductors[0].net_id is None
    assert result.description == "Delete net GND (1 conductor(s) keep their copper)"

    bus.undo()
    assert bus.document.conductors[0].net_id == "net-1"


def test_a_hand_built_netlist_survives_a_save_and_a_load():
    """The .perf format has always carried nets; what was missing was any way to write
    one without KiCad. This is the feature, end to end."""
    bus = new_bus()
    bus.dispatch(
        "net.add", AddNetPayload(name="GND", net_class="ground", nodes=(NetNode("U1", "8"),))
    )
    bus.dispatch("net.add", AddNetPayload(name="+5V", net_class="power"))
    bus.dispatch("net.update", UpdateNetPayload(id="net-2", current_a=0.5, voltage_v=5.0))
    bus.dispatch("net.connect", ConnectPinsPayload(id="net-2", nodes=(NetNode("U1", "16"),)))

    reloaded = parse_document_or_throw(serialize_document(bus.document))

    assert reloaded.nets == bus.document.nets


# ---------------------------------------------------------------------------
# Schematic parts: the design before the board
# ---------------------------------------------------------------------------
#
# The other order of work, and the one every other EDA tool uses. Until these commands
# existed, every route a part had into a document ended in `component.place`, which needs
# a hole -- so the circuit could not be drawn before the layout was.


def add_part(bus: CommandBus, ref: str = "R1", footprint_id: str = "r-axial-3", value: str = "10k"):
    return bus.dispatch(
        "part.add", AddPartPayload(ref=ref, footprint_id=footprint_id, value=value)
    )


def test_a_part_can_enter_the_design_without_a_place_on_the_board():
    """The whole point. A hole is a decision about layout, and the circuit comes first."""
    bus = new_bus()

    result = add_part(bus)

    assert result.ok, result.message
    assert bus.document.components == ()
    assert len(bus.document.parts) == 1
    part = bus.document.parts[0]
    assert (part.ref, part.value, part.footprint_id) == ("R1", "10k", "r-axial-3")


def test_a_reference_is_unique_across_the_board_and_the_design():
    """One namespace, not two. Every net node is a (ref, pin) pair, so two R1s would be a
    netlist that cannot say which one it wired -- which is why this is checked in both
    directions rather than within each list."""
    bus = new_bus()
    place_r1(bus)

    refused = add_part(bus, ref="R1")

    assert not refused.ok
    assert refused.code == "duplicate-ref"

    bus_two = new_bus()
    add_part(bus_two, ref="R1")
    also_refused = place_r1(bus_two)
    assert not also_refused.ok
    assert also_refused.code == "duplicate-ref"
    # The refusal points at the fix rather than merely naming the clash.
    assert "part.place" in also_refused.message


def test_wiring_a_part_that_is_not_on_the_board_is_ordinary():
    """`assert_pins_free` deliberately does not check that a component exists, and this is
    the workflow that depends on it: draw the circuit, then place it."""
    bus = new_bus()
    add_part(bus, ref="R1")
    add_part(bus, ref="R2")

    result = bus.dispatch(
        "net.add",
        AddNetPayload(name="OUT", nodes=(NetNode("R1", "2"), NetNode("R2", "1"))),
    )

    assert result.ok, result.message
    assert len(bus.document.nets[0].nodes) == 2


def test_renaming_a_part_carries_its_wiring_with_it():
    """A reference is the only name a net has for a part, so renaming the part IS renaming
    what the net points at. It did not always do this: R1 wired into six nets and then
    relabelled came out the other side connected to nothing."""
    bus = new_bus()
    add_part(bus, ref="R1")
    bus.dispatch("net.add", AddNetPayload(name="OUT", nodes=(NetNode("R1", "2"),)))

    result = bus.dispatch(
        "part.update", UpdatePartPayload(id=bus.document.parts[0].id, ref="R7")
    )

    assert result.ok, result.message
    assert bus.document.nets[0].nodes == (NetNode("R7", "2"),)


def test_renaming_a_placed_component_carries_its_wiring_too():
    """The same rule on the board, because it is the same wart. The properties dialog used
    to have to warn people to rename before importing a netlist rather than after."""
    bus = new_bus()
    place_r1(bus)
    bus.dispatch("net.add", AddNetPayload(name="OUT", nodes=(NetNode("R1", "2"),)))

    result = bus.dispatch(
        "component.update", UpdateComponentPayload(id=bus.document.components[0].id, ref="R7")
    )

    assert result.ok, result.message
    assert bus.document.nets[0].nodes == (NetNode("R7", "2"),)


def test_a_rename_that_would_put_one_pin_on_two_nets_is_refused():
    """The one thing carrying the wiring must not do. R9.2 is already wired, so renaming
    R1 to R9 would merge them -- and a pin belongs to exactly one net."""
    bus = new_bus()
    add_part(bus, ref="R1")
    bus.dispatch("net.add", AddNetPayload(name="A", nodes=(NetNode("R1", "2"),)))
    bus.dispatch("net.add", AddNetPayload(name="B", nodes=(NetNode("R9", "2"),)))

    refused = bus.dispatch(
        "part.update", UpdatePartPayload(id=bus.document.parts[0].id, ref="R9")
    )

    assert not refused.ok
    assert refused.code == "pin-collision"
    assert "R9.2" in refused.message


def test_placing_parts_moves_them_onto_the_board_in_one_step():
    """One command however many parts: a thirty-part circuit dispatched one at a time takes
    thirty presses of Ctrl+Z, each leaving a board that is half laid out."""
    bus = new_bus()
    add_part(bus, ref="R1")
    add_part(bus, ref="R2")
    ids = [part.id for part in bus.document.parts]

    result = bus.dispatch(
        "part.place",
        PlacePartsPayload(
            placements=(
                PartPlacement(id=ids[0], anchor=HoleCoord(2, 2)),
                PartPlacement(id=ids[1], anchor=HoleCoord(2, 6), rotation=90),
            )
        ),
    )

    assert result.ok, result.message
    assert bus.document.parts == ()
    assert [c.ref for c in bus.document.components] == ["R1", "R2"]
    assert bus.document.components[1].rotation == 90
    # The value and the footprint come across; nothing has to be typed twice.
    assert bus.document.components[0].value == "10k"

    bus.undo()
    assert len(bus.document.parts) == 2 and bus.document.components == ()


def test_a_placed_part_keeps_its_identity():
    """The journal and the undo history refer to it by id, and a part that changed identity
    when it was placed would make a replayed journal describe two different things."""
    bus = new_bus()
    add_part(bus, ref="R1")
    part_id = bus.document.parts[0].id

    bus.dispatch(
        "part.place",
        PlacePartsPayload(placements=(PartPlacement(id=part_id, anchor=HoleCoord(2, 2)),)),
    )

    assert bus.document.components[0].id == part_id


def test_placing_a_part_off_the_board_is_refused_and_nothing_moves():
    bus = new_bus()
    add_part(bus, ref="R1")

    refused = bus.dispatch(
        "part.place",
        PlacePartsPayload(
            placements=(PartPlacement(id=bus.document.parts[0].id, anchor=HoleCoord(999, 999)),)
        ),
    )

    assert not refused.ok
    assert len(bus.document.parts) == 1
    assert bus.document.components == ()


def test_unplacing_keeps_the_part_and_its_wiring():
    """Putting a part in the wrong hole must not delete the circuit around it. The inverse
    of part.place, and the whole difference from component.delete."""
    bus = new_bus()
    place_r1(bus)
    bus.dispatch("net.add", AddNetPayload(name="OUT", nodes=(NetNode("R1", "2"),)))

    result = bus.dispatch(
        "component.unplace", UnplaceComponentPayload(id=bus.document.components[0].id)
    )

    assert result.ok, result.message
    assert bus.document.components == ()
    assert [(p.ref, p.value, p.footprint_id) for p in bus.document.parts] == [
        ("R1", "10k", "r-axial-5")
    ]
    assert bus.document.nets[0].nodes == (NetNode("R1", "2"),)


def test_unplacing_takes_the_lead_bends_with_it_and_leaves_the_routing():
    """Exactly what component.delete does, and for the same reason: a lead bend is a length
    of the part's own leg, while somebody's routing is not this command's to throw away."""
    bus = new_bus()
    place_r1(bus)
    component_id = bus.document.components[0].id
    bus.dispatch(
        "conductor.add",
        AddConductorPayload(
            conductor=NewLeadBendConductor(
                path=(HoleCoord(2, 2), HoleCoord(3, 2)),
                component_id=component_id,
                pin_number="1",
            )
        ),
    )
    bus.dispatch(
        "conductor.add",
        AddConductorPayload(conductor=NewWireConductor(path=(HoleCoord(8, 8), HoleCoord(9, 8)))),
    )

    bus.dispatch("component.unplace", UnplaceComponentPayload(id=component_id))

    assert [c.kind for c in bus.document.conductors] == ["bare-wire"]


def test_a_locked_part_is_not_unplaced():
    bus = new_bus()
    place_r1(bus)
    component_id = bus.document.components[0].id
    bus.dispatch("component.update", UpdateComponentPayload(id=component_id, locked=True))

    refused = bus.dispatch("component.unplace", UnplaceComponentPayload(id=component_id))

    assert not refused.ok
    assert refused.code == "component-locked"


def test_deleting_a_part_takes_its_connections_with_it():
    """The one place this differs from component.delete on purpose. Deleting a COMPONENT
    takes it off the board and the schematic still asks for it -- LVS is right to report
    the gap. Deleting a schematic PART means the design does not have it, so a net still
    naming its pins would be asking for something nothing has heard of."""
    bus = new_bus()
    add_part(bus, ref="R1")
    add_part(bus, ref="R2")
    bus.dispatch(
        "net.add", AddNetPayload(name="OUT", nodes=(NetNode("R1", "2"), NetNode("R2", "1")))
    )

    result = bus.dispatch("part.delete", DeletePartPayload(id=bus.document.parts[0].id))

    assert result.ok, result.message
    assert result.description == "Delete R1 and its 1 connection(s)"
    assert bus.document.nets[0].nodes == (NetNode("R2", "1"),)


def test_a_whole_circuit_drawn_before_the_board_survives_a_save_and_a_load():
    """The feature, end to end: parts and nets with nothing placed, written out and read
    back. Fifteen golden fixtures have no `parts` key at all, so the array is omitted when
    empty -- this is the other half of that."""
    bus = new_bus()
    add_part(bus, ref="U1", footprint_id="dip-8", value="NE555")
    add_part(bus, ref="R1", footprint_id="r-axial-3", value="10k")
    bus.dispatch(
        "net.add",
        AddNetPayload(name="DISCH", nodes=(NetNode("U1", "7"), NetNode("R1", "2"))),
    )

    text = serialize_document(bus.document)
    reloaded = parse_document_or_throw(text)

    assert reloaded == bus.document
    assert '"parts"' in text
    # ...and a board with nothing in the design does not gain the key.
    assert '"parts"' not in serialize_document(create_empty_document(META))



# ---------------------------------------------------------------------------
# The bus never raises, and board.set names everything it refuses over
# ---------------------------------------------------------------------------


def test_a_payload_of_the_wrong_shape_is_refused_rather_than_raised():
    """"Never raises; callers branch on ok" has to hold for an agent handing the bus a
    dict where a dataclass was expected -- a traceback lands in somebody else's process."""
    bus = new_bus()
    result = bus.dispatch("component.place", {"ref": "R1", "footprint_id": "r-axial-5"})
    assert result.ok is False
    assert result.code == "invalid-payload"
    assert "component.place" in result.message
    assert bus.journal() == ()


def test_a_shrink_names_every_stranded_part_at_once():
    """Naming the first meant one refusal per part, each discovered only after the
    previous one had been moved."""
    bus = new_bus()
    for ref, col in (("R1", 30), ("R2", 40), ("R3", 50)):
        bus.dispatch(
            "component.place",
            PlaceComponentPayload(ref=ref, value="10k", footprint_id="r-axial-5", anchor=HoleCoord(col, 5)),
        )
    result = bus.dispatch(
        "board.set", SetBoardPayload(board=dataclasses.replace(DEFAULT_BOARD, cols=20, rows=20))
    )
    assert result.ok is False
    assert result.code == "would-strand-component"
    assert all(ref in result.message for ref in ("R1", "R2", "R3")), result.message


def test_a_shrink_refuses_to_strand_a_track_cut():
    """A cut off the board survives the shrink unseen and, if the board is grown again,
    breaks a strip nobody remembers cutting."""
    bus = new_bus()
    bus.dispatch(
        "board.set",
        SetBoardPayload(board=dataclasses.replace(DEFAULT_BOARD, type="stripboard", strip_axis="horizontal")),
    )
    assert bus.dispatch("cut.add", AddCutPayload(at=HoleCoord(15, 15))).ok
    result = bus.dispatch(
        "board.set",
        SetBoardPayload(
            board=dataclasses.replace(
                DEFAULT_BOARD, type="stripboard", strip_axis="horizontal", cols=5, rows=5
            )
        ),
    )
    assert result.ok is False
    assert result.code == "would-strand-cut"
    assert "P16" in result.message


def test_a_component_needs_a_reference():
    """Every net node names a part by reference; a part with none is one the wiring can
    never reach."""
    bus = new_bus()
    result = bus.dispatch(
        "component.place",
        PlaceComponentPayload(ref="", value="10k", footprint_id="r-axial-5", anchor=HoleCoord(2, 2)),
    )
    assert result.ok is False
    assert result.code == "invalid-ref"


def test_the_undo_stack_is_bounded():
    bus = new_bus()
    limit = CommandBus.UNDO_LIMIT
    for index in range(limit + 5):
        assert bus.dispatch(
            "net.add", AddNetPayload(name=f"N{index}", net_class="signal")
        ).ok
    assert len(bus.journal()) == limit
    # The OLDEST entries went, so the most recent edit is still the first to undo.
    assert bus.history()[-1].endswith(f"N{limit + 4}")


def test_cutting_a_pad_per_hole_board_says_what_the_board_is():
    bus = new_bus()
    result = bus.dispatch("cut.add", AddCutPayload(at=HoleCoord(3, 3)))
    assert result.ok is False
    assert "pad-per-hole" in result.message
