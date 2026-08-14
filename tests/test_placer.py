"""Tests for the placement optimiser (src/perfstudio/placer.py).

Four things have to hold, and they are what this file is organised around:

1. DETERMINISM (PLAN.md Sec 6.3, non-negotiable). Same document, same seed, same
   placement. Without it a user cannot tell an improvement from noise, and the whole
   golden-fixture approach the rest of this engine rests on stops working here.

2. THE RESULT IS LEGAL. Everything the annealer proposes is on the board, no two pins
   share a hole, and no courtyards overlap -- checked against DRC itself rather than
   against the placer's own opinion of what those words mean, because a placer that
   agrees only with itself is how you ship a layout that will not build.

3. THE DELTA ARITHMETIC IS EXACT. The annealer evaluates moves incrementally, which is
   what makes it affordable; an error there is invisible (it just produces a slightly
   wrong answer, quietly, forever). test_local_delta_matches_a_full_recompute is the
   guard, and it is the load-bearing test in this file.

4. IT ACTUALLY HELPS. The point of the module is fewer insulated wires, so the last
   section routes real fixtures before and after and compares. This is PLAN.md Sec 6.3's
   whole justification made into an assertion.

Most tests here run a deliberately tiny anneal (few iterations, one restart), because
they are testing behaviour rather than quality. The three that measure quality say so
and pay for the full default run.
"""

from __future__ import annotations

import dataclasses
import random
from pathlib import Path

import pytest

from perfstudio import persist
from perfstudio.autoroute import plan_autoroute
from perfstudio.command import CommandBus, CommandContext
from perfstudio.commands import create_document_id_generator, create_standard_registry
from perfstudio.connectivity import FootprintLookup
from perfstudio.drc import run_drc
from perfstudio.footprints import footprint_lookup
from perfstudio.geometry import all_pin_holes, is_inside_board
from perfstudio.model import (
    Board,
    BodyArchetype,
    BodySpec,
    ComponentInstance,
    DocumentMeta,
    Footprint,
    FootprintPin,
    HoleCoord,
    Net,
    NetClass,
    NetNode,
    PerfDocument,
    Point2,
)
from perfstudio.placer import (
    DEFAULT_PLACEMENT_OPTIONS,
    PlacementOptions,
    PlacementWeights,
    _anneal,
    _build_nets,
    _build_parts,
    _initial_state,
    _propose,
    _Scorer,
    _settle_rotations,
    describe,
    plan_placement,
    summarize_changes,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tools" / "diffcheck" / "golden"


def golden_document(name: str) -> PerfDocument:
    """A golden fixture as it sits on disk. Used here as a realistic board, not as a
    differential reference -- nothing in this file compares against frozen output."""
    result = persist.deserialize_document((GOLDEN_DIR / f"{name}.perf").read_text(encoding="utf-8"))
    assert result.ok, result.message
    return result.document

BOARD = Board(
    type="pad-per-hole",
    cols=24,
    rows=16,
    pitch=2.54,
    thickness=1.6,
    material="FR4",
    pad_diameter=1.9,
    drill_diameter=0.8,
)

#: Small and fast: these tests check behaviour, not annealing quality.
QUICK = PlacementOptions(iterations=400, restarts=1, score_with_router=False)


def hole(col: int, row: int) -> HoleCoord:
    return HoleCoord(col=col, row=row)


def footprint(
    fp_id: str,
    offsets: tuple[tuple[int, int], ...],
    archetype: BodyArchetype = "generic-box",
    body_mm: float = 0.0,
) -> Footprint:
    """A footprint with one pin per offset and an optional square courtyard.

    ``body_mm`` of 0 means no outline at all, which is what most tests want: it takes
    the overlap term out of the picture so the test is about the thing it names.
    """
    outline: tuple[Point2, ...] = ()
    if body_mm > 0:
        half = body_mm / 2
        outline = (
            Point2(-half, -half),
            Point2(half, -half),
            Point2(half, half),
            Point2(-half, half),
        )
    return Footprint(
        id=fp_id,
        name=fp_id,
        pins=tuple(
            FootprintPin(number=str(index + 1), d_col=d_col, d_row=d_row)
            for index, (d_col, d_row) in enumerate(offsets)
        ),
        body_outline=outline,
        body_height=0,
        body=BodySpec(archetype=archetype),
        lead_diameter=0.5,
        polarized=False,
    )


ONE_PIN = footprint("fp1", ((0, 0),))
TWO_PIN = footprint("fp2", ((0, 0), (2, 0)))
BOXED = footprint("boxed", ((0, 0), (2, 0)), body_mm=6.0)
TERMINAL = footprint("term", ((0, 0), (1, 0)), archetype="screw-terminal")
HOT = footprint("hot", ((0, 0),), archetype="to220")
DELICATE = footprint("delicate", ((0, 0),), archetype="radial-electrolytic")

LOOKUP: FootprintLookup = {
    f.id: f for f in (ONE_PIN, TWO_PIN, BOXED, TERMINAL, HOT, DELICATE)
}.get


def component(
    ref: str, footprint_id: str, anchor: HoleCoord, *, locked: bool = False, rotation: int = 0
) -> ComponentInstance:
    return ComponentInstance(
        id=f"cmp-{ref}",
        ref=ref,
        value="",
        footprint_id=footprint_id,
        anchor=anchor,
        rotation=rotation,  # type: ignore[arg-type]
        locked=locked,
    )


def net(net_id: str, name: str, net_class: NetClass, pins: tuple[tuple[str, str], ...]) -> Net:
    return Net(
        id=net_id,
        name=name,
        nodes=tuple(NetNode(component_ref=ref, pin=pin) for ref, pin in pins),
        net_class=net_class,
    )


def make_doc(
    components: tuple[ComponentInstance, ...] = (),
    nets: tuple[Net, ...] = (),
    board: Board = BOARD,
) -> PerfDocument:
    return PerfDocument(
        meta=DocumentMeta(
            name="test", created="2024-01-01T00:00:00.000Z", modified="2024-01-01T00:00:00.000Z"
        ),
        board=board,
        components=components,
        nets=nets,
    )


def anchors(doc: PerfDocument) -> dict[str, tuple[HoleCoord, int]]:
    return {c.ref: (c.anchor, int(c.rotation)) for c in doc.components}


def commit(doc: PerfDocument, payload: object) -> PerfDocument:
    """Dispatch a plan through a real bus, the way a host does."""
    bus = CommandBus(
        doc, create_standard_registry(), CommandContext(next_id=create_document_id_generator(doc))
    )
    result = bus.dispatch("component.moveMany", payload)
    assert result.ok, result.message
    return bus.document


# ---------------------------------------------------------------------------
# 1. Determinism
# ---------------------------------------------------------------------------


def test_the_same_seed_gives_the_same_placement() -> None:
    doc = make_doc(
        components=tuple(component(f"R{i}", "fp2", hole(2 + i * 3, 2)) for i in range(6)),
        nets=(net("n1", "SIG", "signal", tuple((f"R{i}", "1") for i in range(6))),),
    )

    first = plan_placement(doc, LOOKUP, QUICK)
    second = plan_placement(doc, LOOKUP, QUICK)

    assert first.changes == second.changes
    assert first.after == second.after


def test_a_different_seed_explores_differently() -> None:
    """Not a correctness requirement -- a demonstration that the seed is the only thing
    driving the search, which is what makes restarts worth anything."""
    doc = make_doc(
        components=tuple(component(f"R{i}", "fp2", hole(2 + i * 3, 2)) for i in range(8)),
        nets=(net("n1", "SIG", "signal", tuple((f"R{i}", "1") for i in range(8))),),
    )

    results = {
        tuple(
            (c.ref, c.to_anchor, c.to_rotation)
            for c in plan_placement(doc, LOOKUP, dataclasses.replace(QUICK, seed=seed)).changes
        )
        for seed in range(4)
    }
    assert len(results) > 1


def test_restarts_do_not_change_a_run_of_one() -> None:
    """The winning restart's seed is reported, and replaying it alone reproduces the plan.

    This is what makes a result reportable: "PerfStudio 0.4.0, seed 2" is enough for
    someone else to get the same board.
    """
    doc = make_doc(
        components=tuple(component(f"R{i}", "fp2", hole(2 + i * 3, 2 + i)) for i in range(5)),
        nets=(net("n1", "SIG", "signal", tuple((f"R{i}", "1") for i in range(5))),),
    )

    many = plan_placement(
        doc, LOOKUP, PlacementOptions(iterations=300, restarts=3, score_with_router=False)
    )
    assert many.seed is not None
    replay = plan_placement(
        doc,
        LOOKUP,
        PlacementOptions(iterations=300, restarts=1, score_with_router=False, seed=many.seed),
    )
    assert replay.changes == many.changes


# ---------------------------------------------------------------------------
# 2. The result is legal
# ---------------------------------------------------------------------------


def test_every_pin_stays_on_the_board() -> None:
    doc = make_doc(
        components=tuple(component(f"R{i}", "fp2", hole(2 + i * 3, 2)) for i in range(6)),
        nets=(net("n1", "SIG", "signal", tuple((f"R{i}", "1") for i in range(6))),),
    )

    plan = plan_placement(doc, LOOKUP, QUICK)

    for placed in plan.document.components:
        fp = LOOKUP(placed.footprint_id)
        assert fp is not None
        for _pin, at in all_pin_holes(placed, fp):
            assert is_inside_board(at, plan.document.board), f"{placed.ref} pin at {at}"


def test_locked_components_never_move() -> None:
    doc = make_doc(
        components=(
            component("J1", "fp2", hole(10, 8), locked=True),
            component("R1", "fp2", hole(2, 2)),
            component("R2", "fp2", hole(20, 12)),
        ),
        nets=(net("n1", "SIG", "signal", (("J1", "1"), ("R1", "1"), ("R2", "1"))),),
    )

    plan = plan_placement(doc, LOOKUP, QUICK)

    assert plan.locked == 1
    assert all(change.ref != "J1" for change in plan.changes)
    after = {c.ref: c.anchor for c in plan.document.components}
    assert after["J1"] == hole(10, 8)


def test_a_board_of_locked_parts_produces_no_plan() -> None:
    doc = make_doc(
        components=(
            component("J1", "fp2", hole(4, 4), locked=True),
            component("J2", "fp2", hole(12, 4), locked=True),
        )
    )

    plan = plan_placement(doc, LOOKUP, QUICK)

    assert plan.is_empty
    assert plan.movable == 0
    assert plan.document is doc
    assert "no change" in describe(plan) or plan.iterations == 0


def test_it_separates_overlapping_bodies() -> None:
    doc = make_doc(
        components=(
            component("U1", "boxed", hole(6, 6)),
            component("U2", "boxed", hole(6, 6)),
        )
    )

    assert plan_placement(doc, LOOKUP, QUICK).before.overlap_pairs == 1
    plan = plan_placement(doc, LOOKUP, PlacementOptions(restarts=2, score_with_router=False))

    assert plan.after.overlap_pairs == 0
    assert plan.after.is_legal


def test_overlap_is_counted_by_exactly_the_predicate_drc_uses() -> None:
    """The reason ``overlap_pairs`` exists next to ``overlap_mm2``.

    An annealer minimising area alone packs parts until their courtyards overlap by a
    floating-point residue -- 7e-14 mm^2, which the area term prices at nothing and DRC
    calls an error. The two must agree, so this pins them together on real footprints.
    """
    registry = footprint_lookup()
    doc = golden_document("dense")
    plan = plan_placement(
        doc, registry, PlacementOptions(iterations=3000, restarts=2, score_with_router=False)
    )

    overlaps = [v for v in run_drc(plan.document, registry) if v.rule == "component-body-overlap"]
    assert len(overlaps) == plan.after.overlap_pairs


def test_it_clears_the_drc_errors_it_is_responsible_for() -> None:
    """The dense fixture starts with six overlapping pairs. A placer that cannot fix
    that is not doing the job the user pressed the button for."""
    registry = footprint_lookup()
    doc = golden_document("dense")

    before = [v for v in run_drc(doc, registry) if v.severity == "error"]
    plan = plan_placement(doc, registry)
    after = [v for v in run_drc(plan.document, registry) if v.severity == "error"]

    assert len(before) == 6
    assert after == []


# ---------------------------------------------------------------------------
# 3. The delta arithmetic is exact
# ---------------------------------------------------------------------------


def _scorer_for(doc: PerfDocument, lookup: FootprintLookup, weights: PlacementWeights):
    parts = _build_parts(doc, lookup)
    nets, nets_of = _build_nets(doc, parts)
    state = _initial_state(doc, parts)
    scorer = _Scorer(
        board_pitch=doc.board.pitch,
        board_cols=doc.board.cols,
        board_rows=doc.board.rows,
        weights=weights,
        nets=nets,
        nets_of=nets_of,
    )
    return state, scorer


def test_local_delta_matches_a_full_recompute() -> None:
    """THE load-bearing test here.

    The annealer never recomputes the whole cost: it scores only the terms involving the
    parts a move touches and adds the difference. That is what makes 40000 moves
    affordable in Python, and it is also completely silent when wrong -- a mis-scoped
    local term does not crash, it just quietly optimises the wrong function forever. So
    every move type is played against a full recompute, on a board with courtyards, nets,
    a heat pair and an edge-seeking part, so every term is live.
    """
    weights = PlacementWeights()
    doc = make_doc(
        components=(
            component("U1", "boxed", hole(4, 4)),
            component("U2", "boxed", hole(10, 4)),
            component("Q1", "hot", hole(6, 9)),
            component("C1", "delicate", hole(8, 9)),
            component("J1", "term", hole(14, 6)),
            component("R1", "fp2", hole(18, 10)),
        ),
        nets=(
            net("n1", "GND", "ground", (("U1", "1"), ("C1", "1"), ("R1", "1"), ("J1", "1"))),
            net("n2", "SIG", "signal", (("U2", "2"), ("Q1", "1"), ("R1", "2"))),
        ),
    )
    state, scorer = _scorer_for(doc, LOOKUP, weights)
    movable = list(range(len(state.parts)))
    rng = random.Random(4242)

    checked = 0
    for _ in range(400):
        proposal = _propose(rng, state, movable, 5, DEFAULT_PLACEMENT_OPTIONS)
        if proposal is None:
            continue
        positions, placements = proposal

        full_before = scorer.full(state).total(weights)
        local_before = scorer.local(state, positions)
        collisions_before = state.collisions
        snapshot = tuple((state.col[p], state.row[p], state.rot[p]) for p in positions)

        for position, (col, row, rot) in zip(positions, placements, strict=True):
            state.set_placement(position, col, row, rot)

        tracked = (scorer.local(state, positions) - local_before) + weights.collision * (
            state.collisions - collisions_before
        )
        actual = scorer.full(state).total(weights) - full_before
        assert tracked == pytest.approx(actual, abs=1e-9), (
            f"incremental delta {tracked} disagrees with a full recompute {actual}"
        )

        for position, (col, row, rot) in zip(positions, snapshot, strict=True):
            state.set_placement(position, col, row, rot)
        checked += 1

    assert checked > 200, "the move generator refused too many proposals to prove anything"


def test_collision_bookkeeping_survives_a_move_and_its_undo() -> None:
    """The one term that is not local is tracked on the state, so it has to be exactly
    reversible -- a leak here would drift over tens of thousands of moves."""
    doc = make_doc(
        components=(
            component("R1", "fp2", hole(4, 4)),
            component("R2", "fp2", hole(9, 9)),
        )
    )
    state, _scorer = _scorer_for(doc, LOOKUP, PlacementWeights())
    assert state.collisions == 0

    state.set_placement(1, 4, 4, 0)  # Directly on top of R1: both pins collide.
    assert state.collisions == 2

    state.set_placement(1, 9, 9, 0)
    assert state.collisions == 0
    assert all(count == 1 for count in state.hole_count.values())


def test_the_cost_never_gets_worse() -> None:
    """Every restart keeps the best state it saw, so no plan can be worse than the input.

    A placer that can hand back a worse board than it was given is one nobody will press
    twice.
    """
    registry = footprint_lookup()
    doc = golden_document("sparse")

    plan = plan_placement(doc, registry, PlacementOptions(iterations=800, restarts=2))

    assert plan.after.total(plan.weights) <= plan.before.total(plan.weights) + 1e-9


# ---------------------------------------------------------------------------
# Cost terms, individually
# ---------------------------------------------------------------------------


def test_a_connector_is_pulled_towards_an_edge() -> None:
    doc = make_doc(components=(component("J1", "term", hole(12, 8)),))

    plan = plan_placement(doc, LOOKUP, PlacementOptions(iterations=2000, restarts=1,
                                                        score_with_router=False))

    after = plan.document.components[0].anchor
    to_edge = min(after.col, BOARD.cols - 1 - after.col, after.row, BOARD.rows - 1 - after.row)
    assert to_edge == 0, f"a screw terminal should reach the board edge, landed at {after}"


def test_an_electrolytic_moves_away_from_a_to220() -> None:
    doc = make_doc(
        components=(
            component("Q1", "hot", hole(12, 8), locked=True),
            component("C1", "delicate", hole(13, 8)),
        )
    )

    plan = plan_placement(doc, LOOKUP, PlacementOptions(iterations=2000, restarts=1,
                                                        score_with_router=False))

    assert plan.before.heat_mm > 0
    assert plan.after.heat_mm == 0


def test_alignment_rewards_pins_that_share_a_row() -> None:
    """PLAN.md Sec 6.2: a net whose pins share one row can be picked up by a single
    solder-trace rail. HPWL alone cannot see the difference, which is the whole reason
    this term exists."""
    weights = PlacementWeights()
    spread = make_doc(
        components=(
            component("R1", "fp1", hole(4, 2)),
            component("R2", "fp1", hole(8, 6)),
            component("R3", "fp1", hole(12, 10)),
        ),
        nets=(net("n1", "GND", "ground", (("R1", "1"), ("R2", "1"), ("R3", "1"))),),
    )
    in_a_row = make_doc(
        components=(
            component("R1", "fp1", hole(4, 6)),
            component("R2", "fp1", hole(8, 6)),
            component("R3", "fp1", hole(12, 6)),
        ),
        nets=(net("n1", "GND", "ground", (("R1", "1"), ("R2", "1"), ("R3", "1"))),),
    )

    spread_state, spread_scorer = _scorer_for(spread, LOOKUP, weights)
    row_state, row_scorer = _scorer_for(in_a_row, LOOKUP, weights)

    assert row_scorer.full(row_state).alignment_mm == 0.0
    assert spread_scorer.full(spread_state).alignment_mm > 0.0


@pytest.mark.parametrize("fixture", ["dense", "random-11"])
def test_the_placer_stops_turning_parts_for_nothing(fixture: str, monkeypatch) -> None:
    """The annealer accepts any move whose delta is <= 0, and a rotation's delta is
    exactly zero for every part the cost cannot tell apart turned -- one on no net, or
    one whose courtyard is square. So it turned parts for no reason: on the dense fixture
    it turned 11, and 5 of those cost 0.00 to turn back.

    That is not free to whoever is holding the iron. Every rotation is an orientation to
    get right at the bench and a polarity line in the build guide, so when the tool has no
    preference, the user's own orientation is the one to keep.

    Measured against the same search with the tidy-up disabled, because that is the claim:
    fewer parts turned, and the router no worse off for it.
    """
    from perfstudio import placer as placer_module

    registry = footprint_lookup()
    doc = dataclasses.replace(golden_document(fixture), conductors=())
    options = PlacementOptions(seed=0)

    monkeypatch.setattr(placer_module, "_settle_rotations", lambda *args, **kwargs: None)
    churned = plan_placement(doc, registry, options)
    monkeypatch.undo()
    settled = plan_placement(doc, registry, options)

    turned = sum(1 for c in settled.changes if c.rotated)
    turned_before = sum(1 for c in churned.changes if c.rotated)
    assert turned < turned_before, f"{turned} turned, was {turned_before}"

    # ...and it bought that with nothing. The router is the arbiter that chose this board,
    # so it is the one that has to agree the tidy-up was free.
    assert plan_autoroute(settled.document, registry).summary.total_cost <= (
        plan_autoroute(churned.document, registry).summary.total_cost
    )


def test_settling_a_rotation_never_makes_the_placement_worse() -> None:
    """It only ever hands an orientation back, and only when the total does not go up --
    so it is a tidy-up, not a second optimiser with an opinion of its own."""
    registry = footprint_lookup()
    doc = dataclasses.replace(golden_document("dense"), conductors=())
    options = PlacementOptions(seed=0, iterations=3000, restarts=1, score_with_router=False)

    parts = _build_parts(doc, registry)
    state = _initial_state(doc, parts)
    nets, nets_of = _build_nets(doc, parts)
    scorer = _Scorer(
        board_pitch=doc.board.pitch,
        board_cols=doc.board.cols,
        board_rows=doc.board.rows,
        weights=options.weights,
        nets=nets,
        nets_of=nets_of,
    )
    movable = [position for position, part in enumerate(state.parts) if part.movable]
    original = tuple(state.rot)
    _anneal(state, scorer, movable, doc, options, options.iterations or 0, options.seed)
    before = scorer.full(state).total(options.weights)
    turned_before = sum(1 for p in movable if state.rot[p] != original[p])

    _settle_rotations(state, scorer, movable, original, options)

    after = scorer.full(state).total(options.weights)
    turned_after = sum(1 for p in movable if state.rot[p] != original[p])
    assert after <= before
    assert turned_after <= turned_before


def test_a_net_reaching_fewer_than_two_placed_pins_is_ignored() -> None:
    """A net naming a part that is not on the board constrains nothing about placement.
    LVS is what reports it; the placer must not crash on it or invent a position."""
    doc = make_doc(
        components=(component("R1", "fp2", hole(4, 4)),),
        nets=(
            net("n1", "SIG", "signal", (("R1", "1"), ("MISSING", "3"))),
            net("n2", "NC", "signal", (("R1", "9"),)),
        ),
    )

    plan = plan_placement(doc, LOOKUP, QUICK)

    assert plan.before.hpwl_mm == 0.0
    assert plan.after.is_legal


def test_a_component_with_an_unknown_footprint_is_left_alone() -> None:
    """Skipped exactly as connectivity, DRC and LVS skip it: moving a part whose size and
    pins are unknown would be moving it blind."""
    doc = make_doc(
        components=(
            component("R1", "fp2", hole(4, 4)),
            component("X9", "no-such-footprint", hole(9, 9)),
        ),
        nets=(net("n1", "SIG", "signal", (("R1", "1"), ("R1", "2"))),),
    )

    plan = plan_placement(doc, LOOKUP, QUICK)

    assert all(change.ref != "X9" for change in plan.changes)
    assert {c.ref: c.anchor for c in plan.document.components}["X9"] == hole(9, 9)


def test_an_empty_document_is_handled() -> None:
    plan = plan_placement(make_doc(), LOOKUP, QUICK)
    assert plan.is_empty
    assert plan.movable == 0


# ---------------------------------------------------------------------------
# It is a proposal, and it commits as one step
# ---------------------------------------------------------------------------


def test_planning_does_not_touch_the_input_document() -> None:
    doc = make_doc(
        components=tuple(component(f"R{i}", "fp2", hole(2 + i * 3, 2)) for i in range(5)),
        nets=(net("n1", "SIG", "signal", tuple((f"R{i}", "1") for i in range(5))),),
    )
    before = anchors(doc)

    plan = plan_placement(doc, LOOKUP, QUICK)

    assert anchors(doc) == before
    assert plan.document is not doc


def test_the_payload_reproduces_the_preview_exactly() -> None:
    """The preview is what DRC, LVS and the 2D view are shown; committing has to produce
    that document and not a similar one."""
    doc = make_doc(
        components=tuple(component(f"R{i}", "fp2", hole(2 + i * 3, 2)) for i in range(5)),
        nets=(net("n1", "SIG", "signal", tuple((f"R{i}", "1") for i in range(5))),),
    )

    plan = plan_placement(doc, LOOKUP, QUICK)
    committed = commit(doc, plan.payload())

    assert committed.components == plan.document.components


def test_the_whole_placement_is_one_undo_step() -> None:
    doc = make_doc(
        components=tuple(component(f"R{i}", "fp2", hole(2 + i * 3, 2)) for i in range(5)),
        nets=(net("n1", "SIG", "signal", tuple((f"R{i}", "1") for i in range(5))),),
    )
    plan = plan_placement(doc, LOOKUP, QUICK)
    assert not plan.is_empty

    bus = CommandBus(
        doc, create_standard_registry(), CommandContext(next_id=create_document_id_generator(doc))
    )
    assert bus.dispatch("component.moveMany", plan.payload()).ok
    assert bus.document.components != doc.components

    bus.undo()

    assert bus.document.components == doc.components


def test_the_undo_entry_says_what_it_did() -> None:
    doc = make_doc(
        components=tuple(component(f"R{i}", "fp2", hole(2 + i * 3, 2)) for i in range(4)),
        nets=(net("n1", "SIG", "signal", tuple((f"R{i}", "1") for i in range(4))),),
    )
    plan = plan_placement(doc, LOOKUP, QUICK)

    bus = CommandBus(
        doc, create_standard_registry(), CommandContext(next_id=create_document_id_generator(doc))
    )
    bus.dispatch("component.moveMany", plan.payload())

    assert "Auto-place" in bus.history()[-1]


def test_summarize_changes_speaks_hole_addresses() -> None:
    doc = make_doc(
        components=(component("R1", "fp2", hole(2, 2)), component("R2", "fp2", hole(18, 12))),
        nets=(net("n1", "SIG", "signal", (("R1", "1"), ("R2", "1"))),),
    )
    plan = plan_placement(doc, LOOKUP, QUICK)

    lines = summarize_changes(plan)
    assert lines
    assert all("->" in line for line in lines)
    assert not any("HoleCoord" in line for line in lines)


# ---------------------------------------------------------------------------
# 4. It actually helps -- the reason the module exists
# ---------------------------------------------------------------------------


def _routing(doc: PerfDocument, lookup: FootprintLookup) -> tuple[int, float, int]:
    """(insulated wires, total cost, unrouted) for a dry-run autoroute."""
    plan = plan_autoroute(doc, lookup)
    insulated = sum(
        1 for outcome in plan.nets for link in outcome.routed if link.strategy == "insulated-wire"
    )
    return insulated, plan.summary.total_cost, plan.summary.links_unrouted


@pytest.mark.parametrize("fixture", ["ne555", "sparse"])
def test_placement_makes_the_board_cheaper_to_route(fixture: str) -> None:
    """The claim PLAN.md Sec 6.3 is written to justify, measured rather than asserted.

    Conductors are stripped first: the question is what the board would cost to route
    from scratch at each placement, and leaving the fixture's own routing in would have
    the two runs answering different questions.
    """
    registry = footprint_lookup()
    doc = dataclasses.replace(golden_document(fixture), conductors=())

    plan = plan_placement(doc, registry)

    before_insulated, before_cost, _ = _routing(doc, registry)
    after_insulated, after_cost, after_unrouted = _routing(plan.document, registry)

    assert after_cost < before_cost
    assert after_insulated <= before_insulated
    assert after_unrouted == 0


def test_it_recovers_a_grid_import_which_is_the_case_it_exists_for() -> None:
    """A netlist import drops parts in a grid, because it has nowhere better to put them.

    That grid is the worst realistic starting point and the one every user of the import
    path actually gets: on NE555 it needs 7 insulated wires and leaves 2 connections the
    router cannot make at all. This is the measurement that says the button is worth
    pressing.
    """
    registry = footprint_lookup()
    doc = dataclasses.replace(golden_document("ne555"), conductors=())
    gridded = dataclasses.replace(
        doc,
        components=tuple(
            dataclasses.replace(c, anchor=hole(2 + (i // 6) * 6, 2 + (i % 6) * 4), rotation=0)
            for i, c in enumerate(doc.components)
        ),
    )

    before_insulated, before_cost, before_unrouted = _routing(gridded, registry)
    plan = plan_placement(gridded, registry)
    after_insulated, after_cost, after_unrouted = _routing(plan.document, registry)

    assert before_unrouted > 0 and after_unrouted == 0
    assert after_insulated < before_insulated
    assert after_cost < before_cost
    assert plan.after.is_legal


def test_describe_leads_with_what_the_user_gets() -> None:
    registry = footprint_lookup()
    doc = dataclasses.replace(golden_document("sparse"), conductors=())

    line = describe(plan_placement(doc, registry, PlacementOptions(iterations=800, restarts=1)))

    assert "part(s) placed" in line
    assert "mm less connection length" in line
