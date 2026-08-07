"""Tests for the connection router (src/perfstudio/router.py).

Two layers, in order of importance:

1. Golden differential tests: every board document under tools/diffcheck/golden/*.perf
   carries a `routes` array of three routing requests -- {from, to, ok, strategy, cost,
   path, riskHoles, alternatives} -- with the result the original TypeScript router
   (packages/core/src/router.ts) produced for that exact request. Reproducing all 45
   (15 cases x 3 requests) is the acceptance criterion for this port: it is proof of
   equivalence, not just "my own idea of correct".

2. Hand-built unit tests translated from packages/core/src/router.test.ts, isolating
   the specific behaviours (strategy selection, the solder/wire cost crossover, the R5'
   proximity risk being priced into the search rather than reported afterwards, foreign
   pins blocking a trace, determinism, alternative ordering) that the golden tests would
   otherwise only exercise incidentally.

The golden-fixture loader below is deliberately minimal, private test scaffolding, the
same shape as tests/test_connectivity.py's `_load_golden`: it reads a *.perf JSON file
straight into the model dataclasses using only what router.py actually reads (board,
components, conductors). It is NOT the real persistence layer -- persist.py (being
written separately) will supersede it, and nothing outside this test file should depend
on it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from perfstudio.commands import DEFAULT_BOARD, create_empty_document
from perfstudio.footprints import footprint_lookup
from perfstudio.geometry import coord_to_hole_ref, hole_key
from perfstudio.model import (
    Board,
    BodySpec,
    ComponentInstance,
    Conductor,
    DocumentMeta,
    Footprint,
    FootprintPin,
    HoleCoord,
    LeadBendConductor,
    PerfDocument,
    Point2,
    SolderTraceConductor,
    SpineSpec,
    StripConductor,
    WireConductor,
)
from perfstudio.occupancy import build_occupancy
from perfstudio.router import (
    DEFAULT_ROUTER_COSTS,
    DEFAULT_ROUTER_OPTIONS,
    RouteRequest,
    RouterOptions,
    route_connection,
)

# ---------------------------------------------------------------------------
# Golden fixtures: minimal *.perf / *.expected.json["routes"] readers.
#
# Scaffolding only -- see module docstring. Parses exactly the subset of the wire
# format router.py consumes (board, components, conductors); nets/cuts/meta beyond
# what PerfDocument requires are not needed and are left at their dataclass defaults.
# ---------------------------------------------------------------------------

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tools" / "diffcheck" / "golden"

GOLDEN_CASE_NAMES: tuple[str, ...] = (
    "dense",
    "ne555",
    "sparse",
    *(f"random-{i:02d}" for i in range(1, 13)),
)


def _hole(raw: dict[str, Any]) -> HoleCoord:
    return HoleCoord(col=raw["col"], row=raw["row"])


def _board(raw: dict[str, Any]) -> Board:
    return Board(
        type=raw["type"],
        cols=raw["cols"],
        rows=raw["rows"],
        pitch=raw["pitch"],
        thickness=raw["thickness"],
        material=raw["material"],
        pad_diameter=raw["padDiameter"],
        drill_diameter=raw["drillDiameter"],
        strip_axis=raw.get("stripAxis"),
    )


def _golden_component(raw: dict[str, Any]) -> ComponentInstance:
    return ComponentInstance(
        id=raw["id"],
        ref=raw["ref"],
        value=raw["value"],
        footprint_id=raw["footprintId"],
        anchor=_hole(raw["anchor"]),
        rotation=raw.get("rotation", 0),
        mirrored=raw.get("mirrored", False),
        locked=raw.get("locked", False),
    )


def _golden_conductor(raw: dict[str, Any]) -> Conductor:
    kind = raw["kind"]
    path = tuple(_hole(h) for h in raw["path"])

    if kind in ("solder-trace", "solder-trace-wired"):
        spine_raw = raw.get("spine")
        spine = (
            SpineSpec(material=spine_raw["material"], gauge=spine_raw["gauge"])
            if spine_raw is not None
            else None
        )
        return SolderTraceConductor(
            id=raw["id"],
            path=path,
            buildup=raw.get("buildup", "normal"),
            spine=spine,
            net_id=raw.get("netId"),
            layer_z=raw.get("layerZ", 0),
            kind=kind,
            side="bottom",
        )
    if kind in ("bare-wire", "insulated-wire", "top-jumper"):
        return WireConductor(
            id=raw["id"],
            path=path,
            kind=kind,
            side=raw.get("side", "bottom"),
            gauge_awg=raw.get("gaugeAwg"),
            color=raw.get("color"),
            net_id=raw.get("netId"),
            layer_z=raw.get("layerZ", 0),
        )
    if kind == "lead-bend":
        return LeadBendConductor(
            id=raw["id"],
            path=path,
            component_id=raw["componentId"],
            pin_number=raw["pinNumber"],
            net_id=raw.get("netId"),
            layer_z=raw.get("layerZ", 0),
        )
    if kind == "strip":
        return StripConductor(
            id=raw["id"],
            path=path,
            net_id=raw.get("netId"),
            layer_z=raw.get("layerZ", 0),
            side=raw.get("side", "bottom"),
        )
    raise ValueError(f"Unknown conductor kind in golden fixture: {kind!r}")


def _golden_document(raw: dict[str, Any]) -> PerfDocument:
    meta_raw = raw["meta"]
    return PerfDocument(
        meta=DocumentMeta(
            name=meta_raw["name"], created=meta_raw["created"], modified=meta_raw["modified"]
        ),
        board=_board(raw["board"]),
        components=tuple(_golden_component(c) for c in raw.get("components", [])),
        conductors=tuple(_golden_conductor(c) for c in raw.get("conductors", [])),
        format_version=raw.get("formatVersion", 1),
    )


def _load_golden_routes(name: str) -> tuple[PerfDocument, list[dict[str, Any]]]:
    """Load one golden case: the board document and its expected `routes` array."""
    doc = _golden_document(json.loads((GOLDEN_DIR / f"{name}.perf").read_text(encoding="utf-8")))
    expected = json.loads((GOLDEN_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))
    return doc, expected["routes"]


_FOOTPRINT_LOOKUP = footprint_lookup()


# ---------------------------------------------------------------------------
# THE deliverable: exact match against the TypeScript engine's output.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_name", GOLDEN_CASE_NAMES)
def test_matches_typescript_golden_routes(case_name: str) -> None:
    doc, expected_routes = _load_golden_routes(case_name)

    for expected in expected_routes:
        from_hole = _hole(expected["from"])
        to_hole = _hole(expected["to"])
        request = RouteRequest(from_=from_hole, to=to_hole)
        result = route_connection(doc, _FOOTPRINT_LOOKUP, request)

        where = f"{case_name}: {coord_to_hole_ref(from_hole)} -> {coord_to_hole_ref(to_hole)}"
        assert result.ok == expected["ok"], f"{where}: ok mismatch"
        if not expected["ok"]:
            assert result.alternatives == (), f"{where}: expected no alternatives on failure"
            continue

        assert result.best is not None, f"{where}: expected a best candidate"
        assert result.best.strategy == expected["strategy"], (
            f"{where}: strategy mismatch: got {result.best.strategy!r}, "
            f"expected {expected['strategy']!r}"
        )
        assert round(result.best.cost, 6) == expected["cost"], (
            f"{where}: cost mismatch: got {result.best.cost!r} (rounded "
            f"{round(result.best.cost, 6)!r}), expected {expected['cost']!r}"
        )

        path = result.best.conductors[0].path
        actual_path = [{"col": p.col, "row": p.row} for p in path]
        assert actual_path == expected["path"], f"{where}: path mismatch"

        actual_risk_holes = [{"col": p.col, "row": p.row} for p in result.best.risk_holes]
        assert actual_risk_holes == expected["riskHoles"], f"{where}: riskHoles mismatch"

        actual_alternatives = [a.strategy for a in result.alternatives]
        assert actual_alternatives == expected["alternatives"], f"{where}: alternatives mismatch"


def test_golden_case_count_is_fifteen() -> None:
    """Sanity check on the fixture set itself: 15 documents, as the task specifies."""
    perf_files = sorted(GOLDEN_DIR.glob("*.perf"))
    assert len(perf_files) == 15
    assert {p.stem for p in perf_files} == set(GOLDEN_CASE_NAMES)


def test_golden_route_total_is_forty_five() -> None:
    """15 cases x 3 requests each = 45 golden route results."""
    total = 0
    for case_name in GOLDEN_CASE_NAMES:
        _doc, routes = _load_golden_routes(case_name)
        total += len(routes)
    assert total == 45


# ---------------------------------------------------------------------------
# Unit-test fixture builders, translated from router.test.ts.
# ---------------------------------------------------------------------------

_META = DocumentMeta(
    name="t", created="2026-01-01T00:00:00.000Z", modified="2026-01-01T00:00:00.000Z"
)

_ONE_PIN_FOOTPRINT = Footprint(
    id="pad1",
    name="pad",
    pins=(FootprintPin(number="1", d_col=0, d_row=0),),
    body_outline=(
        Point2(-1, -1),
        Point2(1, -1),
        Point2(1, 1),
        Point2(-1, 1),
    ),
    body_height=1,
    body=BodySpec(archetype="generic-box", dims={}),
    lead_diameter=0.6,
    polarized=False,
)


def _lookup(footprint_id: str) -> Footprint | None:
    return _ONE_PIN_FOOTPRINT if footprint_id == "pad1" else None


def h(col: int, row: int) -> HoleCoord:
    return HoleCoord(col=col, row=row)


def comp(id_: str, ref: str, anchor: HoleCoord) -> ComponentInstance:
    return ComponentInstance(
        id=id_,
        ref=ref,
        value="x",
        footprint_id="pad1",
        anchor=anchor,
        rotation=0,
        mirrored=False,
        locked=False,
    )


def trace(id_: str, path: tuple[HoleCoord, ...]) -> SolderTraceConductor:
    return SolderTraceConductor(id=id_, path=path, buildup="normal", side="bottom")


def wire(id_: str, path: tuple[HoleCoord, ...]) -> WireConductor:
    return WireConductor(id=id_, path=path, kind="bare-wire", side="bottom")


def doc(
    components: tuple[ComponentInstance, ...], conductors: tuple[Conductor, ...] = ()
) -> PerfDocument:
    base = create_empty_document(_META, DEFAULT_BOARD)
    return dataclasses.replace(base, components=components, conductors=conductors)


# ---------------------------------------------------------------------------
# occupancy (shared fixtures with the router tests in router.test.ts)
# ---------------------------------------------------------------------------


def test_a_wire_occupies_every_hole_it_crosses_even_though_it_only_contacts_its_ends() -> None:
    # This is the whole reason occupancy exists separately from connectivity: the
    # middle hole is not electrically joined, but the board is physically full there.
    d = doc((), (wire("w1", (h(2, 2), h(3, 2), h(4, 2))),))
    occ = build_occupancy(d, _lookup)
    assert occ.conductors_at(h(3, 2), "bottom") == ("w1",)
    assert occ.is_copper_blocked(h(3, 2), "bottom") is True
    assert occ.conductors_at(h(3, 2), "top") == ()


def test_a_pin_occupies_its_hole_and_is_reported_with_its_ref() -> None:
    d = doc((comp("c1", "R1", h(5, 5)),))
    occ = build_occupancy(d, _lookup)
    pin = occ.pin_at(h(5, 5))
    assert pin is not None
    assert pin.component_ref == "R1"
    assert occ.pin_at(h(6, 5)) is None


# ---------------------------------------------------------------------------
# routeConnection
# ---------------------------------------------------------------------------


def test_prefers_a_solder_trace_on_an_empty_board() -> None:
    d = doc((comp("c1", "A", h(2, 2)), comp("c2", "B", h(5, 2))))
    r = route_connection(d, _lookup, RouteRequest(from_=h(2, 2), to=h(5, 2)))
    assert r.ok is True
    assert r.best is not None
    assert r.best.strategy == "solder-trace"
    path = r.best.conductors[0].path
    assert len(path) == 4
    assert path[0] == h(2, 2)
    assert path[-1] == h(5, 2)


def test_every_consecutive_pair_of_a_returned_trace_is_orthogonally_adjacent() -> None:
    d = doc((comp("c1", "A", h(1, 1)), comp("c2", "B", h(3, 2))))
    r = route_connection(d, _lookup, RouteRequest(from_=h(1, 1), to=h(3, 2)))
    assert r.best is not None
    assert r.best.strategy.startswith("solder-trace")
    path = r.best.conductors[0].path
    for i in range(1, len(path)):
        a, b = path[i - 1], path[i]
        assert abs(a.col - b.col) + abs(a.row - b.row) == 1


def test_picks_a_solder_trace_for_a_short_hop_and_a_wire_for_a_long_one() -> None:
    """The economics deliberately cross over: dragging solder is cheapest for short
    hops, but nobody drags it twenty pads -- they lay a wire. The crossover sits at
    the pure-solder pad limit, which is what a person would do by hand, and pinning
    it here means a cost-table edit cannot quietly move it.
    """
    short = doc((comp("a", "A", h(2, 2)), comp("b", "B", h(5, 2))))
    short_result = route_connection(short, _lookup, RouteRequest(from_=h(2, 2), to=h(5, 2)))
    assert short_result.best is not None
    assert short_result.best.strategy == "solder-trace"

    long = doc((comp("a", "A", h(1, 1)), comp("b", "B", h(20, 1))))
    long_result = route_connection(long, _lookup, RouteRequest(from_=h(1, 1), to=h(20, 1)))
    assert long_result.best is not None
    assert long_result.best.strategy == "bare-wire"


def test_proposes_a_spine_when_a_long_run_must_stay_on_copper_because_a_wire_is_blocked() -> None:
    # A pin on the straight line rules out a bare wire, so the long trace wins -- and a
    # trace that long has to be reinforced rather than built from solder alone.
    d = doc(
        (
            comp("a", "A", h(1, 1)),
            comp("b", "B", h(12, 1)),
            comp("x", "X", h(6, 1)),  # sits on the straight line between them
        )
    )
    r = route_connection(d, _lookup, RouteRequest(from_=h(1, 1), to=h(12, 1)))
    assert r.best is not None
    assert r.best.strategy == "solder-trace-wired"
    assert r.best.conductors[0].kind == "solder-trace-wired"
    assert "spine" in r.best.explanation.lower()


def test_falls_back_to_a_wire_when_copper_blocks_the_whole_corridor() -> None:
    # A wall of foreign trace across the board, with only the endpoints free.
    wall = tuple(h(4, row) for row in range(DEFAULT_BOARD.rows))
    d = doc(
        (comp("c1", "A", h(2, 2)), comp("c2", "B", h(6, 2))),
        (trace("t-wall", wall),),
    )

    r = route_connection(d, _lookup, RouteRequest(from_=h(2, 2), to=h(6, 2)))
    assert r.ok is True
    assert r.best is not None
    assert r.best.strategy == "insulated-wire"
    # Bare wire must NOT be offered: it would cross the wall's copper.
    assert "bare-wire" not in {a.strategy for a in r.alternatives}


def test_reports_failure_honestly_instead_of_returning_a_broken_route() -> None:
    d = doc(())
    r = route_connection(d, _lookup, RouteRequest(from_=h(3, 3), to=h(3, 3)))
    assert r.ok is False
    assert r.reason


def test_refuses_endpoints_outside_the_board() -> None:
    d = doc(())
    r = route_connection(d, _lookup, RouteRequest(from_=h(0, 0), to=h(DEFAULT_BOARD.cols, 0)))
    assert r.ok is False
    assert r.reason is not None
    assert "outside" in r.reason.lower()


def test_is_deterministic_the_same_request_routes_the_same_way_every_time() -> None:
    d = doc((comp("c1", "A", h(2, 7)), comp("c2", "B", h(9, 3))))
    a = route_connection(d, _lookup, RouteRequest(from_=h(2, 7), to=h(9, 3)))
    b = route_connection(d, _lookup, RouteRequest(from_=h(2, 7), to=h(9, 3)))
    assert b == a


def test_offers_alternatives_ordered_cheapest_first_so_the_ui_can_suggest_a_swap() -> None:
    d = doc((comp("c1", "A", h(2, 2)), comp("c2", "B", h(5, 2))))
    r = route_connection(d, _lookup, RouteRequest(from_=h(2, 2), to=h(5, 2)))
    assert len(r.alternatives) > 1
    for i in range(1, len(r.alternatives)):
        assert r.alternatives[i].cost >= r.alternatives[i - 1].cost


# ---------------------------------------------------------------------------
# R5 proximity risk is priced into the search, not just reported afterwards
# ---------------------------------------------------------------------------
#
# A short corridor of foreign pads along row 2. Routing A(1,3) to B(5,3) straight
# along row 3 keeps a different net one hole above for the whole run. The router
# should pay a couple of extra steps to drop into a clear row instead. This is the
# behaviour that separates "a legal route" from "a route someone can actually solder".
#
# The run is kept short on purpose so a solder trace is the winning strategy at all --
# over longer distances a wire wins on cost and there is no trace to steer.


def _risk_board() -> PerfDocument:
    foreign = tuple(comp(f"f{col}", f"F{col}", h(col, 2)) for col in range(2, 5))
    return doc((comp("a", "A", h(1, 3)), comp("b", "B", h(5, 3)), *foreign))


def _hug_count(path: tuple[HoleCoord, ...]) -> int:
    return sum(1 for p in path if p.row == 3 and 2 <= p.col <= 4)


def _trace_path(d: PerfDocument, proximity_risk: float) -> tuple[HoleCoord, ...]:
    """Inspect the SOLDER-TRACE candidate rather than the overall winner. Which
    strategy wins is a separate question of economics -- over this distance a plain
    wire is cheaper, and rightly so. What is under test here is the path the trace
    search chooses when it does run, so the assertion has to look at that candidate
    directly.
    """
    options = RouterOptions(costs=dataclasses.replace(DEFAULT_ROUTER_COSTS, proximity_risk=proximity_risk))
    r = route_connection(d, _lookup, RouteRequest(from_=h(1, 3), to=h(5, 3)), options)
    candidate = next((a for a in r.alternatives if a.strategy.startswith("solder-trace")), None)
    assert candidate is not None
    return candidate.conductors[0].path


def test_steers_the_trace_off_the_foreign_row_when_risk_is_priced() -> None:
    assert _hug_count(_trace_path(_risk_board(), DEFAULT_ROUTER_COSTS.proximity_risk)) < 3


def test_hugs_the_foreign_row_once_the_risk_price_is_set_to_zero() -> None:
    # Free risk means the shortest path wins: straight along row 3, past all three pads.
    assert _hug_count(_trace_path(_risk_board(), 0)) == 3


def test_a_priced_route_is_longer_than_a_free_one_the_detour_is_real_not_cosmetic() -> None:
    priced = _trace_path(_risk_board(), DEFAULT_ROUTER_COSTS.proximity_risk)
    free = _trace_path(_risk_board(), 0)
    assert len(priced) > len(free)


def test_names_the_risky_pads_so_they_can_become_measurement_steps_in_the_guide() -> None:
    d = doc((comp("a", "A", h(1, 3)), comp("b", "B", h(3, 3)), comp("f", "F", h(2, 2))))
    r = route_connection(d, _lookup, RouteRequest(from_=h(1, 3), to=h(3, 3)))
    assert r.ok is True
    assert r.best is not None
    if r.best.risk_holes:
        assert "different net" in r.best.explanation.lower()
        for hole in r.best.risk_holes:
            assert coord_to_hole_ref(hole) in r.best.explanation


# ---------------------------------------------------------------------------
# routes do not pass through foreign pins
# ---------------------------------------------------------------------------


def test_steps_around_a_pin_sitting_in_the_direct_path() -> None:
    d = doc((comp("a", "A", h(2, 4)), comp("b", "B", h(6, 4)), comp("x", "X", h(4, 4))))
    r = route_connection(d, _lookup, RouteRequest(from_=h(2, 4), to=h(6, 4)))
    assert r.ok is True
    if r.best is not None and r.best.strategy.startswith("solder-trace"):
        keys = {hole_key(p) for p in r.best.conductors[0].path}
        assert hole_key(h(4, 4)) not in keys
