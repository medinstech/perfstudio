"""Tests for copying a block of board and pasting it back (ui/clipboard.py).

The failure this file is mostly about is a quiet one: a paste that looks right and is
wired wrong. A copy of R1 is not R1, so its copper is not on R1's net; a pasted lead bend
must belong to the part that was pasted with it and not to the original; and the whole
thing has to arrive as ONE command, because a paste that undid in two steps would leave a
board with the parts down and the wiring gone -- a state nobody chose.

Runs offscreen like tests/test_ui.py: ui/clipboard.py holds no widgets, but it reaches
into view2d for the reference-naming rule, and importing that imports Qt.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from perfstudio.command import CommandBus, CommandContext
from perfstudio.commands import create_document_id_generator, create_standard_registry
from perfstudio.model import (
    Board,
    ComponentInstance,
    DocumentMeta,
    HoleCoord,
    LeadBendConductor,
    PerfDocument,
    SolderTraceConductor,
    WireConductor,
)
from perfstudio.ui.clipboard import (
    CLIPBOARD_KIND,
    block_from_json,
    block_to_json,
    paste_payload,
    paste_position,
)

BOARD = Board(type="pad-per-hole", cols=24, rows=18, pitch=2.54, thickness=1.6, material="FR4",
              pad_diameter=1.9, drill_diameter=1.0)


def _doc(
    components: tuple[ComponentInstance, ...] = (),
    conductors: tuple[object, ...] = (),
) -> PerfDocument:
    return PerfDocument(
        meta=DocumentMeta(name="test", created="2026-01-01T00:00:00Z", modified="2026-01-01T00:00:00Z"),
        board=BOARD,
        components=components,
        conductors=tuple(conductors),  # type: ignore[arg-type]
        nets=(),
    )


def _resistor(id_: str, ref: str, at: HoleCoord, value: str = "10k") -> ComponentInstance:
    return ComponentInstance(
        id=id_, ref=ref, value=value, footprint_id="r-axial-4", anchor=at,
        rotation=0, mirrored=False, locked=False,
    )


def _bus(doc: PerfDocument) -> CommandBus:
    """Seeded from the document, as MainWindow._new_bus is: a generator that restarts at
    zero would refuse the first edit to a board whose conductors are already cond-1.."""
    return CommandBus(
        doc,
        create_standard_registry(),
        CommandContext(next_id=create_document_id_generator(doc)),
    )


# ---------------------------------------------------------------------------
# What goes on the clipboard
# ---------------------------------------------------------------------------


def test_a_block_is_written_relative_to_its_own_corner() -> None:
    """Where it was copied from stops mattering the moment it is on the clipboard: the
    same text has to paste at any offset, onto any board."""
    doc = _doc((_resistor("cmp-1", "R1", HoleCoord(7, 5)), _resistor("cmp-2", "R2", HoleCoord(9, 6))))

    raw = json.loads(block_to_json(doc, ["cmp-1", "cmp-2"]))

    assert raw["kind"] == CLIPBOARD_KIND
    assert [c["at"] for c in raw["components"]] == [{"col": 0, "row": 0}, {"col": 2, "row": 1}]
    # ...and where it came from, which is only ever the default landing place.
    assert raw["from"] == {"col": 7, "row": 5}


def test_the_corner_counts_the_copper_as_well_as_the_parts() -> None:
    """A trace running left of the leftmost part is part of the block. Measuring the
    corner from the parts alone pushes it off the board's left edge on paste."""
    doc = _doc(
        (_resistor("cmp-1", "R1", HoleCoord(5, 5)),),
        (SolderTraceConductor(id="cond-1", path=(HoleCoord(2, 5), HoleCoord(3, 5))),),
    )

    raw = json.loads(block_to_json(doc, ["cmp-1"], ["cond-1"]))

    assert raw["from"] == {"col": 2, "row": 5}
    assert raw["components"][0]["at"] == {"col": 3, "row": 0}
    assert raw["conductors"][0]["path"][0] == {"col": 0, "row": 0}


def test_only_what_was_selected_is_copied() -> None:
    doc = _doc((_resistor("cmp-1", "R1", HoleCoord(1, 1)), _resistor("cmp-2", "R2", HoleCoord(4, 1))))

    raw = json.loads(block_to_json(doc, ["cmp-2"]))

    assert [c["ref"] for c in raw["components"]] == ["R2"]


def test_a_lead_bend_whose_part_was_not_copied_is_left_behind_and_counted() -> None:
    """It is a leg of that part. Copied on its own it would be a leg of nothing, and the
    count is reported rather than left for DRC to discover."""
    doc = _doc(
        (_resistor("cmp-1", "R1", HoleCoord(1, 1)), _resistor("cmp-2", "R2", HoleCoord(6, 1))),
        (
            LeadBendConductor(id="cond-1", path=(HoleCoord(1, 1), HoleCoord(2, 1)),
                              component_id="cmp-1", pin_number="1"),
            LeadBendConductor(id="cond-2", path=(HoleCoord(6, 1), HoleCoord(7, 1)),
                              component_id="cmp-2", pin_number="1"),
        ),
    )

    raw = json.loads(block_to_json(doc, ["cmp-1"], ["cond-1", "cond-2"]))

    assert len(raw["conductors"]) == 1
    assert raw["conductors"][0]["componentRef"] == "R1"
    assert raw["orphanedLeadBends"] == 1


def test_the_clipboard_text_is_readable_json() -> None:
    """It is pasted into bug reports and read in diffs; this project keeps its documents
    that way for the same reason."""
    doc = _doc((_resistor("cmp-1", "R1", HoleCoord(1, 1)),))

    text = block_to_json(doc, ["cmp-1"])

    assert "\n" in text and '  "kind"' in text


# ---------------------------------------------------------------------------
# What comes back off it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "https://example.com/",
        "not json at all",
        '{"kind": "something-else", "components": []}',
        "[1, 2, 3]",
    ],
)
def test_anything_that_is_not_a_block_reads_as_no_block(text: str) -> None:
    """The clipboard usually holds something else entirely. "There is nothing to paste"
    is an ordinary answer, not an error to report."""
    assert block_from_json(text) is None


def test_a_round_trip_keeps_what_matters_about_a_part() -> None:
    doc = _doc((
        ComponentInstance(id="cmp-1", ref="U1", value="NE555", footprint_id="dip-8",
                          anchor=HoleCoord(3, 3), rotation=270, mirrored=True, locked=True),
    ))

    block = block_from_json(block_to_json(doc, ["cmp-1"]))

    assert block is not None
    part = block.components[0]
    assert (part.ref, part.value, part.footprint_id) == ("U1", "NE555", "dip-8")
    assert (part.rotation, part.mirrored) == (270, True)
    # ...but not the lock. A pasted part is one you are still positioning.
    assert part.locked is False


def test_one_bad_member_does_not_lose_the_rest_of_the_block() -> None:
    """The parts that survived are still the block the user copied."""
    text = json.dumps({
        "kind": CLIPBOARD_KIND,
        "components": [
            {"ref": "R1", "footprintId": "r-axial-4", "at": {"col": 0, "row": 0}},
            {"ref": "R2"},  # no footprint, no position
        ],
        "conductors": [{"kind": "bare-wire", "path": [{"col": 0, "row": 0}]}],  # one hole
    })

    block = block_from_json(text)

    assert block is not None
    assert [c.ref for c in block.components] == ["R1"]
    assert block.conductors == ()


# ---------------------------------------------------------------------------
# Where it lands
# ---------------------------------------------------------------------------


def test_a_paste_steps_clear_of_what_is_already_there() -> None:
    """Pasted exactly on top of the original, a block is invisible and the two are
    impossible to tell apart -- which is how a duplicate gets soldered twice."""
    doc = _doc((_resistor("cmp-1", "R1", HoleCoord(4, 4)),))
    block = block_from_json(block_to_json(doc, ["cmp-1"]))
    assert block is not None

    assert paste_position(doc, block) == HoleCoord(5, 5)


def test_a_paste_lands_where_it_is_asked_to_when_that_hole_is_free() -> None:
    doc = _doc((_resistor("cmp-1", "R1", HoleCoord(4, 4)),))
    block = block_from_json(block_to_json(doc, ["cmp-1"]))
    assert block is not None

    assert paste_position(doc, block, HoleCoord(10, 2)) == HoleCoord(10, 2)


def test_a_block_that_would_run_off_the_board_is_left_where_it_was_asked_for() -> None:
    """Stepping clear has to stop at the edge. Walking the block into the far corner
    instead would be a worse answer arrived at silently -- and the command's refusal,
    which names the part and the hole, is a better one."""
    corner = HoleCoord(BOARD.cols - 2, BOARD.rows - 2)
    doc = _doc(
        (_resistor("cmp-1", "R1", corner),),
        (WireConductor(id="cond-1", kind="bare-wire",
                       path=(corner, HoleCoord(BOARD.cols - 1, BOARD.rows - 1))),),
    )
    block = block_from_json(block_to_json(doc, ["cmp-1"], ["cond-1"]))
    assert block is not None

    # One step down-right and the block's own far corner is off the board.
    assert paste_position(doc, block) == corner


# ---------------------------------------------------------------------------
# The command it turns into
# ---------------------------------------------------------------------------


def test_pasted_parts_get_free_references_counted_from_the_board() -> None:
    """R1 and R2 are still on the board this is being pasted onto, and the batch has to
    see the names it has already claimed within itself -- otherwise three resistors all
    ask to be R3 and the bus refuses the whole block."""
    doc = _doc((_resistor("cmp-1", "R1", HoleCoord(1, 1)), _resistor("cmp-2", "R2", HoleCoord(4, 1))))
    block = block_from_json(block_to_json(doc, ["cmp-1", "cmp-2"]))
    assert block is not None

    paste = paste_payload(doc, block, HoleCoord(1, 8))

    assert [spec.ref for spec in paste.payload.components] == ["R3", "R4"]


def test_pasted_copper_carries_no_net_claim() -> None:
    """A copy of R1 is not R1. Copper that claimed R1's net would tell LVS this block is
    wired to a schematic that has never heard of it -- and unclaimed copper is the one
    kind rip-up and the stale-conductor cleanup both promise never to touch."""
    doc = _doc(
        (_resistor("cmp-1", "R1", HoleCoord(1, 1)),),
        (WireConductor(id="cond-1", kind="bare-wire", path=(HoleCoord(1, 1), HoleCoord(1, 4)),
                       net_id="net-1"),),
    )
    block = block_from_json(block_to_json(doc, ["cmp-1"], ["cond-1"]))
    assert block is not None

    paste = paste_payload(doc, block, HoleCoord(6, 1))

    assert [spec.net_id for spec in paste.payload.conductors] == [None]


def test_a_pasted_lead_bend_belongs_to_the_pasted_part() -> None:
    """The whole point of doing this in one command: the copper is prepared against a
    document the new parts have already joined, so the bend can name one of them."""
    doc = _doc(
        (_resistor("cmp-1", "R1", HoleCoord(2, 2)),),
        (LeadBendConductor(id="cond-1", path=(HoleCoord(2, 2), HoleCoord(3, 2)),
                           component_id="cmp-1", pin_number="1"),),
    )
    block = block_from_json(block_to_json(doc, ["cmp-1"], ["cond-1"]))
    assert block is not None

    paste = paste_payload(doc, block, HoleCoord(8, 8))
    bend = paste.payload.conductors[0]

    assert bend.component_id == paste.payload.components[0].id  # type: ignore[union-attr]
    assert bend.component_id != "cmp-1"

    # ...and the bus accepts it, which is the assertion that actually proves the order.
    bus = _bus(doc)
    result = bus.dispatch("block.place", paste.payload)
    assert result.ok, result.message
    pasted = bus.document.conductors[-1]
    assert pasted.component_id == bus.document.components[-1].id  # type: ignore[union-attr]


def test_a_paste_is_one_undo_step() -> None:
    """Two commands would put a state on the undo stack nobody chose: the parts down and
    the wiring gone, one Ctrl+Z from a board that looks finished and is not."""
    doc = _doc(
        (_resistor("cmp-1", "R1", HoleCoord(2, 2)), _resistor("cmp-2", "R2", HoleCoord(5, 2))),
        (SolderTraceConductor(id="cond-1", path=(HoleCoord(3, 2), HoleCoord(4, 2))),),
    )
    block = block_from_json(block_to_json(doc, ["cmp-1", "cmp-2"], ["cond-1"]))
    assert block is not None
    bus = _bus(doc)

    bus.dispatch("block.place", paste_payload(doc, block, HoleCoord(2, 9)).payload)
    assert len(bus.document.components) == 4
    assert len(bus.document.conductors) == 2

    bus.undo()

    assert len(bus.document.components) == 2
    assert len(bus.document.conductors) == 1


def test_copper_that_would_land_off_the_board_is_dropped_and_counted() -> None:
    """The parts are what the user is placing. A block pasted near an edge with one trace
    hanging over it is still the paste they asked for -- but they are told."""
    doc = _doc(
        (_resistor("cmp-1", "R1", HoleCoord(1, 1)),),
        (WireConductor(id="cond-1", kind="bare-wire",
                       path=(HoleCoord(1, 1), HoleCoord(6, 1))),),
    )
    block = block_from_json(block_to_json(doc, ["cmp-1"], ["cond-1"]))
    assert block is not None

    paste = paste_payload(doc, block, HoleCoord(BOARD.cols - 3, 1))

    assert paste.dropped_conductors == 1
    assert paste.payload.conductors == ()
    assert len(paste.payload.components) == 1


def test_the_undo_label_says_what_it_was_rather_than_how_many() -> None:
    doc = _doc((_resistor("cmp-1", "R1", HoleCoord(1, 1)),))
    block = block_from_json(block_to_json(doc, ["cmp-1"]))
    assert block is not None
    bus = _bus(doc)

    bus.dispatch("block.place", paste_payload(doc, block, HoleCoord(5, 5), label="Paste at F6").payload)

    assert bus.history()[-1] == "Paste at F6"
