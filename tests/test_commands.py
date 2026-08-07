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
    AddCutPayload,
    DeleteComponentPayload,
    MirrorComponentPayload,
    MoveComponentPayload,
    NewLeadBendConductor,
    NewSolderTraceConductor,
    NewStripConductor,
    NewWireConductor,
    PlaceComponentPayload,
    RotateComponentPayload,
    SetBoardPayload,
    UpdateComponentPayload,
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
