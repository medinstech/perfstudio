"""Differential proof for the occupancy index.

Occupancy was the one engine module ported without a golden fixture behind it, which
mattered because the router depends on it and because the TypeScript source it was
ported from is about to be deleted — after that, the proof could never be produced.
tools/diffcheck/generate.mjs now dumps it, and this asserts the Python port reproduces
it across all fifteen boards.

The distinction being pinned here is the one that makes occupancy a separate module at
all: connectivity says what is electrically joined, and a wire contacts only its two
endpoints. Occupancy says what is physically in the way, and that same wire lies across
every hole on its path. If the two ever collapsed into one another, the router would
either refuse legal routes or lay copper straight through a wire.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from perfstudio.footprints import footprint_lookup
from perfstudio.occupancy import build_occupancy

from .test_drc import GOLDEN_CASE_NAMES, GOLDEN_DIR, _document


def _load(name: str) -> tuple[Any, list[dict[str, Any]]]:
    doc = _document(json.loads((GOLDEN_DIR / f"{name}.perf").read_text(encoding="utf-8")))
    expected = json.loads((GOLDEN_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))
    return doc, expected["occupancy"]


@pytest.mark.parametrize("name", GOLDEN_CASE_NAMES)
def test_matches_typescript_golden_occupancy(name: str) -> None:
    doc, expected = _load(name)
    occ = build_occupancy(doc, footprint_lookup())

    holes = occ.occupied_holes()
    assert len(holes) == len(expected), (
        f"[{name}] occupied hole count {len(holes)} != {len(expected)}"
    )

    for i, (hole, want) in enumerate(zip(holes, expected, strict=True)):
        where = f"[{name}] occupancy[{i}] at ({hole.col},{hole.row})"
        assert {"col": hole.col, "row": hole.row} == want["hole"], (
            f"{where}: hole ordering diverges — expected {want['hole']}"
        )

        pin = occ.pin_at(hole)
        got_pin = None if pin is None else {"componentRef": pin.component_ref, "pin": pin.pin}
        assert got_pin == want["pin"], f"{where}: pin {got_pin} != {want['pin']}"

        assert list(occ.conductors_at(hole, "bottom")) == want["bottom"], (
            f"{where}: bottom conductors {list(occ.conductors_at(hole, 'bottom'))} != {want['bottom']}"
        )
        assert list(occ.conductors_at(hole, "top")) == want["top"], (
            f"{where}: top conductors {list(occ.conductors_at(hole, 'top'))} != {want['top']}"
        )
        assert occ.is_copper_blocked(hole, "bottom") == want["blockedBottom"], (
            f"{where}: blockedBottom {occ.is_copper_blocked(hole, 'bottom')} != {want['blockedBottom']}"
        )
        assert occ.is_copper_blocked(hole, "top") == want["blockedTop"], (
            f"{where}: blockedTop {occ.is_copper_blocked(hole, 'top')} != {want['blockedTop']}"
        )
        assert occ.body_covers(hole) == want["bodyCovers"], (
            f"{where}: bodyCovers {occ.body_covers(hole)} != {want['bodyCovers']}"
        )


def test_the_fixtures_actually_exercise_occupancy() -> None:
    """Guard against a green suite that proves nothing.

    A fixture set where every board happened to be empty would pass the test above
    without checking anything, so assert the golden data really does contain pins,
    conductors on both sides, blocked copper and covered bodies.
    """
    seen = {"pin": 0, "bottom": 0, "top": 0, "blocked": 0, "body": 0}
    for name in GOLDEN_CASE_NAMES:
        _, expected = _load(name)
        for rec in expected:
            seen["pin"] += rec["pin"] is not None
            seen["bottom"] += bool(rec["bottom"])
            seen["top"] += bool(rec["top"])
            seen["blocked"] += rec["blockedBottom"] or rec["blockedTop"]
            seen["body"] += rec["bodyCovers"] is not None
    for key, count in seen.items():
        assert count > 0, f"golden occupancy data never exercises {key!r} — the proof is hollow"


def test_occupancy_and_connectivity_disagree_exactly_where_they_should() -> None:
    """The reason occupancy is a separate module, checked on the real boards.

    A solder trace CONTACTS every pad along its path, so connectivity joins them all
    and occupancy blocks them all — the two agree. A wire contacts only its endpoints,
    so connectivity joins just those two while occupancy still blocks every hole the
    conductor declares. Both facts have to be true at once, and this asserts it over
    all fifteen boards rather than on a hand-built toy.
    """
    from perfstudio.connectivity import extract_physical_nets
    from perfstudio.model import contacts_every_path_hole

    lookup = footprint_lookup()
    checked_traces = 0
    checked_wires = 0

    for name in GOLDEN_CASE_NAMES:
        doc, _ = _load(name)
        occ = build_occupancy(doc, lookup)
        net_of_hole: dict[tuple[int, int], str] = {}
        for net in extract_physical_nets(doc, lookup):
            for node in net.nodes:
                net_of_hole[(node.hole.col, node.hole.row)] = net.id

        for cond in doc.conductors:
            for hole in cond.path:
                assert cond.id in occ.conductors_at(hole, cond.side), (
                    f"[{name}] {cond.id} declares ({hole.col},{hole.row}) but occupancy misses it"
                )

            if contacts_every_path_hole(cond):
                ids = {net_of_hole.get((h.col, h.row)) for h in cond.path}
                assert len(ids) == 1 and None not in ids, (
                    f"[{name}] solder trace {cond.id} should join every pad it crosses, got {ids}"
                )
                checked_traces += 1
            elif len(cond.path) == 2:
                a, b = cond.path
                assert net_of_hole.get((a.col, a.row)) == net_of_hole.get((b.col, b.row)), (
                    f"[{name}] wire {cond.id} should join its two endpoints"
                )
                checked_wires += 1

    assert checked_traces > 0 and checked_wires > 0, (
        f"fixtures stopped covering both cases: {checked_traces} traces, {checked_wires} wires"
    )


def test_known_gap_occupancy_does_not_model_a_straight_run_geometrically() -> None:
    """Documents a real limitation, faithfully carried over from the TypeScript engine.

    Occupancy registers the holes a conductor DECLARES in its path. A straight wire's
    path is just its two endpoints, so the holes it physically flies over in between are
    not registered here — the router computes those itself when it checks whether a bare
    run is clear.

    That is a genuine gap, not a porting error: both implementations behave this way, so
    the golden fixtures agree. It is pinned here so the behaviour is a decision on the
    record rather than a surprise, and so that closing it later is a deliberate change
    with a failing test to announce it.
    """
    doc, _ = _load("dense")
    occ = build_occupancy(doc, footprint_lookup())
    long_wires = [
        c
        for c in doc.conductors
        if len(c.path) == 2
        and abs(c.path[0].col - c.path[1].col) + abs(c.path[0].row - c.path[1].row) > 4
    ]
    assert long_wires, "fixture no longer contains a long straight wire"

    wire = long_wires[0]
    a, b = wire.path
    midpoint_col = (a.col + b.col) // 2
    midpoint_row = (a.row + b.row) // 2
    from perfstudio.model import HoleCoord

    midpoint = HoleCoord(midpoint_col, midpoint_row)
    if midpoint not in (a, b):
        assert wire.id not in occ.conductors_at(midpoint, wire.side), (
            "occupancy now models straight runs geometrically — that is an improvement, "
            "but it diverges from the TypeScript engine the golden fixtures came from. "
            "Regenerate the fixtures deliberately rather than deleting this test."
        )
