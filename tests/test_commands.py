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
    ComponentPlacement,
    DeleteComponentPayload,
    DeleteConductorsPayload,
    MirrorComponentPayload,
    MoveComponentPayload,
    MoveComponentsPayload,
    NewLeadBendConductor,
    NewSolderTraceConductor,
    NewStripConductor,
    NewWireConductor,
    PlaceComponentPayload,
    ReplaceConductorsPayload,
    RotateComponentPayload,
    SetBoardPayload,
    UpdateComponentPayload,
    create_document_id_generator,
    create_empty_document,
    create_standard_registry,
)
from perfstudio.model import (
    DocumentMeta,
    HoleCoord,
    PerfDocument,
    SolderTraceConductor,
    SpineSpec,
)

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
