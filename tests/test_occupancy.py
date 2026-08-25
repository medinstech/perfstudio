"""Tests for the occupancy index (src/perfstudio/occupancy.py).

occupancy.py has no golden fixture of its own -- tools/diffcheck/generate.mjs
dumps physicalNets/drc/lvs/continuity/isolation/routes, but never the raw
OccupancyIndex -- so these are hand-built unit tests, exercising the one
thing that makes this module differ from connectivity.py: a conductor
occupies (and therefore blocks) EVERY hole on its path, not just the
electrical contact points a wire happens to solder down at. See the module
docstrings of occupancy.py and connectivity.py for why that split exists:
registering a wire's pass-over holes as connectivity *nodes* would flood
every consumer with meaningless single-node nets, but the router still needs
to know those holes are physically full.
"""

from __future__ import annotations

from perfstudio.model import (
    Board,
    BoardSide,
    BodySpec,
    ComponentInstance,
    DocumentMeta,
    Footprint,
    FootprintPin,
    HoleCoord,
    PerfDocument,
    Point2,
    Rotation,
    SolderTraceConductor,
    StripConductor,
    WireConductor,
)
from perfstudio.occupancy import (
    FootprintLookup,
    OccupyingPin,
    build_occupancy,
    can_cross_copper,
    stacking_layers,
)

# ---------------------------------------------------------------------------
# Fixture builders -- same shape as test_connectivity.py's, kept independent
# on purpose so the two test files don't depend on each other's scaffolding.
# ---------------------------------------------------------------------------

BOARD = Board(
    type="pad-per-hole",
    cols=40,
    rows=40,
    pitch=2.54,
    thickness=1.6,
    material="FR4",
    pad_diameter=1.9,
    drill_diameter=0.8,
)


def hole(col: int, row: int) -> HoleCoord:
    return HoleCoord(col=col, row=row)


def one_pin_footprint(
    fp_id: str,
    d_col: int = 0,
    d_row: int = 0,
    body_outline: tuple[Point2, ...] = (),
) -> Footprint:
    return Footprint(
        id=fp_id,
        name=fp_id,
        pins=(FootprintPin(number="1", d_col=d_col, d_row=d_row),),
        body_outline=body_outline,
        body_height=0,
        body=BodySpec(archetype="generic-box"),
        lead_diameter=0.5,
        polarized=False,
    )


def make_component(
    comp_id: str,
    ref: str,
    footprint_id: str,
    anchor: HoleCoord,
    rotation: Rotation = 0,
    mirrored: bool = False,
) -> ComponentInstance:
    return ComponentInstance(
        id=comp_id,
        ref=ref,
        value="",
        footprint_id=footprint_id,
        anchor=anchor,
        rotation=rotation,
        mirrored=mirrored,
        locked=False,
    )


def solder_trace(cond_id: str, path: tuple[HoleCoord, ...]) -> SolderTraceConductor:
    return SolderTraceConductor(id=cond_id, path=path, buildup="normal", side="bottom")


def bare_wire(cond_id: str, path: tuple[HoleCoord, ...]) -> WireConductor:
    return WireConductor(id=cond_id, path=path, kind="bare-wire", side="bottom")


def insulated_wire(cond_id: str, path: tuple[HoleCoord, ...]) -> WireConductor:
    return WireConductor(id=cond_id, path=path, kind="insulated-wire", side="bottom")


def top_jumper(cond_id: str, path: tuple[HoleCoord, ...]) -> WireConductor:
    return WireConductor(id=cond_id, path=path, kind="top-jumper", side="top")


def strip_conductor(cond_id: str, path: tuple[HoleCoord, ...]) -> StripConductor:
    return StripConductor(id=cond_id, path=path, side="bottom")


def make_doc(
    components: tuple[ComponentInstance, ...] = (),
    conductors: tuple[SolderTraceConductor | WireConductor | StripConductor, ...] = (),
) -> PerfDocument:
    return PerfDocument(
        meta=DocumentMeta(
            name="t", created="2024-01-01T00:00:00.000Z", modified="2024-01-01T00:00:00.000Z"
        ),
        board=BOARD,
        components=components,
        conductors=conductors,
    )


def make_lookup(footprints: tuple[Footprint, ...]) -> FootprintLookup:
    registry = {fp.id: fp for fp in footprints}
    return registry.get


# ---------------------------------------------------------------------------
# The core distinction: occupancy registers every path hole, not just contacts.
# ---------------------------------------------------------------------------


def test_bare_wire_occupies_every_hole_on_its_path_not_just_its_endpoints() -> None:
    a, b, c = hole(0, 0), hole(1, 0), hole(2, 0)
    doc = make_doc(conductors=(bare_wire("w1", (a, b, c)),))
    index = build_occupancy(doc, make_lookup(()))

    # Unlike connectivity's contact-only nodes, ALL THREE holes are occupied.
    assert index.conductors_at(a, "bottom") == ("w1",)
    assert index.conductors_at(b, "bottom") == ("w1",)
    assert index.conductors_at(c, "bottom") == ("w1",)
    assert set(index.occupied_holes()) == {a, b, c}


def test_solder_trace_and_strip_occupy_every_hole_on_their_path() -> None:
    path = tuple(hole(i, 0) for i in range(4))
    doc = make_doc(
        conductors=(solder_trace("t1", path), strip_conductor("s1", (hole(0, 5), hole(1, 5))))
    )
    index = build_occupancy(doc, make_lookup(()))

    for h in path:
        assert index.conductors_at(h, "bottom") == ("t1",)
    assert index.conductors_at(hole(0, 5), "bottom") == ("s1",)
    assert index.conductors_at(hole(1, 5), "bottom") == ("s1",)


def test_conductors_at_is_keyed_by_side_independently() -> None:
    a = hole(0, 0)
    doc = make_doc(
        conductors=(top_jumper("j1", (a, hole(1, 0))), solder_trace("t1", (a, hole(0, 1))))
    )
    index = build_occupancy(doc, make_lookup(()))

    assert index.conductors_at(a, "top") == ("j1",)
    assert index.conductors_at(a, "bottom") == ("t1",)


def test_hole_touched_by_multiple_conductors_lists_all_of_them() -> None:
    a, b = hole(0, 0), hole(5, 0)
    doc = make_doc(conductors=(bare_wire("w1", (a, b)), bare_wire("w2", (hole(0, 0), hole(0, 3)))))
    index = build_occupancy(doc, make_lookup(()))

    assert index.conductors_at(a, "bottom") == ("w1", "w2")


def test_unoccupied_hole_reports_nothing_and_is_not_blocked() -> None:
    doc = make_doc(conductors=(bare_wire("w1", (hole(0, 0), hole(1, 0))),))
    index = build_occupancy(doc, make_lookup(()))

    far = hole(30, 30)
    assert index.conductors_at(far, "bottom") == ()
    assert index.conductors_at(far, "top") == ()
    assert index.is_copper_blocked(far, "bottom") is False
    assert index.pin_at(far) is None
    assert index.body_covers(far) is None


# ---------------------------------------------------------------------------
# Copper blocking: which conductor kinds a router must treat as walls.
# ---------------------------------------------------------------------------


def test_solder_trace_bare_wire_and_strip_block_crossing() -> None:
    a, b = hole(0, 0), hole(1, 0)
    doc = make_doc(
        conductors=(
            solder_trace("t1", (a, b)),
            bare_wire("w1", (hole(2, 0), hole(3, 0))),
            strip_conductor("s1", (hole(4, 0), hole(5, 0))),
        )
    )
    index = build_occupancy(doc, make_lookup(()))

    assert index.is_copper_blocked(a, "bottom") is True
    assert index.is_copper_blocked(hole(2, 0), "bottom") is True
    assert index.is_copper_blocked(hole(4, 0), "bottom") is True


def test_insulated_wire_and_top_jumper_do_not_block_crossing() -> None:
    doc = make_doc(
        conductors=(
            insulated_wire("iw1", (hole(0, 0), hole(1, 0))),
            top_jumper("j1", (hole(2, 0), hole(3, 0))),
        )
    )
    index = build_occupancy(doc, make_lookup(()))

    assert index.is_copper_blocked(hole(0, 0), "bottom") is False
    assert index.is_copper_blocked(hole(2, 0), "top") is False
    # They still occupy the hole -- merely crossable, not absent.
    assert index.conductors_at(hole(0, 0), "bottom") == ("iw1",)


def test_can_cross_copper_matches_is_copper_blocked_kinds() -> None:
    assert can_cross_copper("insulated-wire") is True
    assert can_cross_copper("top-jumper") is True
    assert can_cross_copper("solder-trace") is False
    assert can_cross_copper("solder-trace-wired") is False
    assert can_cross_copper("bare-wire") is False
    assert can_cross_copper("lead-bend") is False
    assert can_cross_copper("strip") is False


# ---------------------------------------------------------------------------
# Component pins and bodies.
# ---------------------------------------------------------------------------


def test_pin_at_reports_the_occupying_pin_regardless_of_side() -> None:
    a = hole(3, 3)
    fp = one_pin_footprint("fp1")
    doc = make_doc(components=(make_component("c1", "U1", "fp1", a),))
    index = build_occupancy(doc, make_lookup((fp,)))

    pin = index.pin_at(a)
    assert pin == OccupyingPin(component_id="c1", component_ref="U1", pin="1")


def test_body_covers_uses_bounding_box_and_clips_to_board() -> None:
    # Outline spans x in [-3.0, 2.5] mm and y in [-0.1, 0.1] mm at pitch 2.54.
    # col range: ceil(-3.0/2.54)=-1 .. floor(2.5/2.54)=0, but col -1 is off-board
    # (board starts at col 0), so only col 0 should end up covered.
    outline = (
        Point2(x=-3.0, y=-0.1),
        Point2(x=2.5, y=-0.1),
        Point2(x=2.5, y=0.1),
        Point2(x=-3.0, y=0.1),
    )
    fp = one_pin_footprint("fp1", body_outline=outline)
    doc = make_doc(components=(make_component("c1", "U1", "fp1", hole(0, 0)),))
    index = build_occupancy(doc, make_lookup((fp,)))

    assert index.body_covers(hole(0, 0)) == "c1"
    assert index.body_covers(hole(-1, 0)) is None  # off-board: never registered
    assert index.body_covers(hole(1, 0)) is None  # outside the outline's bounding box


def test_footprint_with_empty_body_outline_covers_nothing() -> None:
    fp = one_pin_footprint("fp1", body_outline=())
    doc = make_doc(components=(make_component("c1", "U1", "fp1", hole(2, 2)),))
    index = build_occupancy(doc, make_lookup((fp,)))

    assert index.body_covers(hole(2, 2)) is None


def test_unknown_footprint_is_skipped_without_raising() -> None:
    doc = make_doc(components=(make_component("c1", "X1", "does-not-exist", hole(0, 0)),))

    index = build_occupancy(doc, make_lookup(()))  # must not raise

    assert index.pin_at(hole(0, 0)) is None
    assert index.body_covers(hole(0, 0)) is None
    assert index.occupied_holes() == ()


# ---------------------------------------------------------------------------
# occupied_holes(): full, deduplicated, sorted inventory.
# ---------------------------------------------------------------------------


def test_occupied_holes_is_sorted_and_deduplicated_across_sources() -> None:
    fp = one_pin_footprint("fp1")
    # The pin hole (5, 5) is also visited by a wire endpoint -- must appear once.
    doc = make_doc(
        components=(make_component("c1", "U1", "fp1", hole(5, 5)),),
        conductors=(
            bare_wire("w1", (hole(5, 5), hole(0, 9))),
            solder_trace("t1", (hole(2, 0), hole(3, 0))),
        ),
    )
    index = build_occupancy(doc, make_lookup((fp,)))

    holes = index.occupied_holes()
    assert holes == tuple(sorted(holes, key=lambda h: (h.col, h.row)))
    assert len(holes) == len(set(holes))
    assert set(holes) == {hole(5, 5), hole(0, 9), hole(2, 0), hole(3, 0)}


def test_build_occupancy_is_deterministic_across_repeats_and_reordering() -> None:
    fp = one_pin_footprint("fp1")
    components = (
        make_component("c1", "U1", "fp1", hole(0, 0)),
        make_component("c2", "U2", "fp1", hole(5, 5)),
    )
    conductors = (
        bare_wire("w1", (hole(0, 0), hole(9, 9))),
        solder_trace("t1", (hole(1, 1), hole(1, 2))),
    )
    doc = make_doc(components, conductors)
    lookup = make_lookup((fp,))

    first = build_occupancy(doc, lookup)
    second = build_occupancy(doc, lookup)
    assert first.occupied_holes() == second.occupied_holes()

    reordered_doc = make_doc(tuple(reversed(components)), tuple(reversed(conductors)))
    reordered = build_occupancy(reordered_doc, lookup)
    assert reordered.occupied_holes() == first.occupied_holes()
    first_at_origin = first.conductors_at(hole(0, 0), "bottom")
    assert reordered.conductors_at(hole(0, 0), "bottom") == first_at_origin


# ---------------------------------------------------------------------------
# Stacking: which conductors have to pass over which
# ---------------------------------------------------------------------------
#
# The renderers ask this, but the question is not a rendering one: it is "do these two
# pieces of copper occupy the same space", which is what this module is for. Both views
# read the answer, so a wire drawn passing over another in 3D is the one drawn over it
# in 2D -- and neither can invent its own opinion.


def wire(cid: str, path: tuple[HoleCoord, ...], side: BoardSide = "bottom") -> WireConductor:
    return WireConductor(id=cid, kind="bare-wire", side=side, path=path)


def run(cid: str, path: tuple[HoleCoord, ...]) -> SolderTraceConductor:
    return SolderTraceConductor(id=cid, kind="solder-trace", side="bottom", path=path)


def test_a_board_where_nothing_crosses_lies_completely_flat() -> None:
    """The property the running index this replaced could not have: a board with nothing
    to step over is drawn with nothing stepped over. It lifted every conductor past every
    earlier one, which on the dense fixture came to 4.47 mm off a board 1.6 mm thick."""
    doc = make_doc(
        conductors=(
            wire("w1", (hole(1, 1), hole(8, 1))),
            wire("w2", (hole(1, 3), hole(8, 3))),
            wire("w3", (hole(1, 5), hole(8, 5))),
        )
    )

    assert set(stacking_layers(doc).values()) == {0}


def test_two_wires_crossing_are_not_left_in_the_same_space() -> None:
    doc = make_doc(
        conductors=(wire("w1", (hole(1, 1), hole(8, 8))), wire("w2", (hole(8, 1), hole(1, 8)))),
    )

    layers = stacking_layers(doc)

    assert layers["w1"] != layers["w2"]


def test_two_wires_meeting_at_a_pad_stay_level_with_each_other() -> None:
    """A shared endpoint is a junction, not a crossing -- two wires of one net soldered
    into the same pad. Lifting one off the other would draw a deliberate joint as an
    accident. `geometry.segments_touch` already draws this line and DRC reads the same
    one, which is why this asks it rather than re-deciding."""
    doc = make_doc(
        conductors=(wire("w1", (hole(1, 1), hole(5, 5))), wire("w2", (hole(5, 5), hole(9, 1)))),
    )

    assert set(stacking_layers(doc).values()) == {0}


def test_a_solder_trace_never_leaves_the_pads_and_the_wire_goes_over_it() -> None:
    """A trace IS the copper: it is soldered at every pad it touches, so it cannot pass
    over anything. Two traces crossing is a short, which is DRC's business and not a
    question about drawing -- so both stay down and the picture shows what it is."""
    doc = make_doc(
        conductors=(
            wire("w1", (hole(4, 1), hole(4, 8))),
            run("t1", tuple(hole(c, 4) for c in range(1, 9))),
            run("t2", tuple(hole(c, 6) for c in range(1, 9))),
        )
    )

    layers = stacking_layers(doc)

    assert layers["t1"] == 0 and layers["t2"] == 0
    assert layers["w1"] > 0, "the wire crosses both runs, so it has to clear them"


def test_the_two_faces_are_stacked_independently() -> None:
    """Copper on one face cannot collide with copper on the other -- there is 1.6 mm of
    board in between."""
    doc = make_doc(
        conductors=(
            wire("w1", (hole(1, 1), hole(8, 8))),
            wire("w2", (hole(8, 1), hole(1, 8)), side="top"),
        )
    )

    assert set(stacking_layers(doc).values()) == {0}


def test_only_what_crosses_is_lifted_and_only_past_what_it_crosses() -> None:
    """Three wires over one: the crossers each need to clear the run underneath, and
    nothing needs to clear a wire it never meets."""
    doc = make_doc(
        conductors=(
            wire("base", (hole(1, 4), hole(9, 4))),
            wire("a", (hole(2, 1), hole(2, 8))),
            wire("b", (hole(5, 1), hole(5, 8))),
            wire("far", (hole(1, 9), hole(9, 9))),
        )
    )

    layers = stacking_layers(doc)

    assert layers["base"] == 0
    assert layers["a"] == 1 and layers["b"] == 1, "each clears the base, not each other"
    assert layers["far"] == 0, "it crosses nothing, so it is not lifted"


def test_the_same_board_stacks_the_same_way_twice() -> None:
    """Greedy in document order and nothing else, so the picture does not shuffle between
    two renders of one board."""
    doc = make_doc(
        conductors=(
            wire("w1", (hole(1, 1), hole(8, 8))),
            wire("w2", (hole(8, 1), hole(1, 8))),
            wire("w3", (hole(1, 4), hole(9, 4))),
        )
    )

    assert stacking_layers(doc) == stacking_layers(doc)
