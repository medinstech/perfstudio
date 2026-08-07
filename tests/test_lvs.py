"""Tests for the LVS engine (src/perfstudio/lvs.py).

Two layers, in order of importance:

1. Golden differential tests: every board document under
   tools/diffcheck/golden/*.perf, run through run_lvs(), continuity_checks() and
   isolation_checks(), must reproduce the `lvs`, `continuity` and `isolation` keys
   of its *.expected.json byte-for-byte (same issue kinds, ordering, pins, net
   names, physical net ids). Those fixtures were dumped from the original
   TypeScript engine (packages/core/src/lvs.ts) by tools/diffcheck/generate.mjs,
   so a match here is proof of equivalence, not just "my own idea of correct".

   The `lvs.issues` entries in the fixtures only carry {kind, netNames, pins,
   physicalNetIds} -- `message` and `conductorIds` are not part of the diff
   contract, so the jsonable conversion below deliberately omits them too.

2. Hand-built unit tests translated from packages/core/src/lvs.test.ts, isolating
   specific semantics (short vs. open vs. unrouted-net, the GND/V+ critical-short
   message, unplaced-component / unknown-footprint, determinism) that the golden
   fixtures happen not to exercise (none of the 15 golden cases contains a short
   or an unplaced component).

The golden-loading helpers below deliberately duplicate the approach in
tests/test_connectivity.py's private `_load_golden`, extended to also parse the
`nets` section that LVS (but not connectivity) needs. This is minimal, private
test scaffolding -- persist.py (being written separately) will supersede it, and
nothing outside this test file should depend on it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from perfstudio.connectivity import FootprintLookup, PhysicalPinRef
from perfstudio.footprints import footprint_lookup
from perfstudio.lvs import (
    ContinuityCheck,
    IsolationCheck,
    LvsIssue,
    LvsResult,
    LvsSummary,
    continuity_checks,
    isolation_checks,
    run_lvs,
)
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
    Net,
    NetClass,
    NetNode,
    PerfDocument,
    SolderTraceConductor,
    SpineSpec,
    StripConductor,
    WireConductor,
)

# ---------------------------------------------------------------------------
# Golden fixtures: minimal *.perf reader (board, components, conductors, nets).
#
# Scaffolding only -- see module docstring. Parses exactly the subset of the
# wire format run_lvs / continuity_checks / isolation_checks consume; cuts and
# meta beyond what PerfDocument requires are left at their dataclass defaults.
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


def _component(raw: dict[str, Any]) -> ComponentInstance:
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


def _conductor(raw: dict[str, Any]) -> Conductor:
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


def _net_node(raw: dict[str, Any]) -> NetNode:
    return NetNode(component_ref=raw["componentRef"], pin=raw["pin"])


def _net(raw: dict[str, Any]) -> Net:
    return Net(
        id=raw["id"],
        name=raw["name"],
        nodes=tuple(_net_node(n) for n in raw["nodes"]),
        net_class=raw["class"],
        current_a=raw.get("currentA"),
        voltage_v=raw.get("voltageV"),
    )


def _document(raw: dict[str, Any]) -> PerfDocument:
    meta_raw = raw["meta"]
    return PerfDocument(
        meta=DocumentMeta(
            name=meta_raw["name"], created=meta_raw["created"], modified=meta_raw["modified"]
        ),
        board=_board(raw["board"]),
        components=tuple(_component(c) for c in raw.get("components", [])),
        conductors=tuple(_conductor(c) for c in raw.get("conductors", [])),
        nets=tuple(_net(n) for n in raw.get("nets", [])),
        format_version=raw.get("formatVersion", 1),
    )


def _load_golden(name: str) -> tuple[PerfDocument, dict[str, Any]]:
    """Load one golden case: the board document and its full expected.json."""
    doc = _document(json.loads((GOLDEN_DIR / f"{name}.perf").read_text(encoding="utf-8")))
    expected = json.loads((GOLDEN_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))
    return doc, expected


#: The real, verified standard footprint registry -- not a re-parse of
#: footprints.expected.json, per the task's instruction to reuse it directly.
_FOOTPRINT_LOOKUP: FootprintLookup = footprint_lookup()


def _pin_to_jsonable(p: PhysicalPinRef) -> dict[str, str]:
    return {"componentRef": p.component_ref, "pin": p.pin}


def _issue_to_jsonable(issue: LvsIssue) -> dict[str, Any]:
    # Deliberately omits `message` and `conductorIds`: the golden fixtures' `lvs.issues`
    # entries only carry {kind, netNames, pins, physicalNetIds} -- see module docstring.
    return {
        "kind": issue.kind,
        "netNames": list(issue.net_names),
        "pins": [_pin_to_jsonable(p) for p in issue.pins],
        "physicalNetIds": list(issue.physical_net_ids),
    }


def _lvs_to_jsonable(result: LvsResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "summary": {
            "schematicNets": result.summary.schematic_nets,
            "physicalNets": result.summary.physical_nets,
            "matchedNets": result.summary.matched_nets,
            "opens": result.summary.opens,
            "shorts": result.summary.shorts,
        },
        "issues": [_issue_to_jsonable(i) for i in result.issues],
    }


def _continuity_to_jsonable(checks: tuple[ContinuityCheck, ...]) -> list[dict[str, Any]]:
    return [{"a": _pin_to_jsonable(c.a), "b": _pin_to_jsonable(c.b), "netName": c.net_name} for c in checks]


def _isolation_to_jsonable(checks: tuple[IsolationCheck, ...]) -> list[dict[str, Any]]:
    return [
        {"a": _pin_to_jsonable(c.a), "b": _pin_to_jsonable(c.b), "netA": c.net_a, "netB": c.net_b}
        for c in checks
    ]


# ---------------------------------------------------------------------------
# THE deliverable: exact match against the TypeScript engine's output.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_name", GOLDEN_CASE_NAMES)
def test_matches_typescript_golden_lvs_continuity_isolation(case_name: str) -> None:
    doc, expected = _load_golden(case_name)

    lvs_result = run_lvs(doc, _FOOTPRINT_LOOKUP)
    continuity_result = continuity_checks(doc)
    isolation_result = isolation_checks(doc)

    assert _lvs_to_jsonable(lvs_result) == expected["lvs"], f"lvs mismatch for golden case {case_name!r}"
    assert _continuity_to_jsonable(continuity_result) == expected["continuity"], (
        f"continuity mismatch for golden case {case_name!r}"
    )
    assert _isolation_to_jsonable(isolation_result) == expected["isolation"], (
        f"isolation mismatch for golden case {case_name!r}"
    )


def test_golden_case_count_is_fifteen() -> None:
    """Sanity check on the fixture set itself: 15 documents, as the task specifies."""
    perf_files = sorted(GOLDEN_DIR.glob("*.perf"))
    assert len(perf_files) == 15
    assert {p.stem for p in perf_files} == set(GOLDEN_CASE_NAMES)


# ---------------------------------------------------------------------------
# Unit-test fixture builders, translated from lvs.test.ts.
# ---------------------------------------------------------------------------


def hole(col: int, row: int) -> HoleCoord:
    return HoleCoord(col=col, row=row)


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


def one_pin_footprint(fp_id: str) -> Footprint:
    """A footprint with a single pin at the anchor (dCol=0, dRow=0)."""
    return Footprint(
        id=fp_id,
        name=fp_id,
        pins=(FootprintPin(number="1", d_col=0, d_row=0),),
        body_outline=(),
        body_height=0,
        body=BodySpec(archetype="generic-box"),
        lead_diameter=0.5,
        polarized=False,
    )


def make_component(comp_id: str, ref: str, footprint_id: str, anchor: HoleCoord) -> ComponentInstance:
    return ComponentInstance(
        id=comp_id, ref=ref, value="", footprint_id=footprint_id, anchor=anchor, locked=False
    )


def solder_trace(cond_id: str, path: tuple[HoleCoord, ...]) -> SolderTraceConductor:
    return SolderTraceConductor(id=cond_id, path=path, buildup="normal", side="bottom")


def net_node(component_ref: str, pin: str) -> NetNode:
    return NetNode(component_ref=component_ref, pin=pin)


def net(net_id: str, name: str, net_class: NetClass, nodes: tuple[NetNode, ...]) -> Net:
    return Net(id=net_id, name=name, nodes=nodes, net_class=net_class)


def make_doc(
    components: tuple[ComponentInstance, ...] = (),
    conductors: tuple[Conductor, ...] = (),
    nets: tuple[Net, ...] = (),
) -> PerfDocument:
    return PerfDocument(
        meta=DocumentMeta(
            name="test", created="2024-01-01T00:00:00.000Z", modified="2024-01-01T00:00:00.000Z"
        ),
        board=BOARD,
        components=components,
        conductors=conductors,
        nets=nets,
    )


def make_lookup(footprints: tuple[Footprint, ...]) -> FootprintLookup:
    registry = {fp.id: fp for fp in footprints}
    return registry.get


# ---------------------------------------------------------------------------
# run_lvs unit tests
# ---------------------------------------------------------------------------


def test_reports_ok_for_a_correctly_wired_two_component_net() -> None:
    a, b = hole(0, 0), hole(1, 0)
    fp = one_pin_footprint("fp1")
    doc = make_doc(
        components=(make_component("c1", "R1", "fp1", a), make_component("c2", "R2", "fp1", b)),
        conductors=(solder_trace("t1", (a, b)),),
        nets=(net("n1", "NET1", "signal", (net_node("R1", "1"), net_node("R2", "1"))),),
    )
    lookup = make_lookup((fp,))

    result = run_lvs(doc, lookup)

    assert result.ok is True
    assert result.issues == ()
    assert result.summary == LvsSummary(
        schematic_nets=1, physical_nets=1, matched_nets=1, opens=0, shorts=0
    )


def test_reports_unrouted_net_when_none_of_a_nets_pins_are_connected() -> None:
    a, b = hole(0, 0), hole(5, 5)
    fp = one_pin_footprint("fp1")
    doc = make_doc(
        components=(make_component("c1", "R1", "fp1", a), make_component("c2", "R2", "fp1", b)),
        nets=(net("n1", "NET1", "signal", (net_node("R1", "1"), net_node("R2", "1"))),),
    )
    lookup = make_lookup((fp,))

    result = run_lvs(doc, lookup)

    assert result.ok is False
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.kind == "unrouted-net"
    assert issue.net_names == ("NET1",)
    assert len(issue.physical_net_ids) == 2
    assert sorted(f"{p.component_ref}.{p.pin}" for p in issue.pins) == ["R1.1", "R2.1"]
    # summary.opens covers under-connection of BOTH shapes, so an unrouted net still counts.
    assert result.summary.opens == 1
    assert result.summary.matched_nets == 0


def test_reports_open_when_a_net_is_partly_wired() -> None:
    a, b, c = hole(0, 0), hole(1, 0), hole(8, 8)  # b joined to a; c left stranded
    fp = one_pin_footprint("fp1")
    doc = make_doc(
        components=(
            make_component("c1", "R1", "fp1", a),
            make_component("c2", "R2", "fp1", b),
            make_component("c3", "R3", "fp1", c),
        ),
        conductors=(solder_trace("t1", (a, b)),),
        nets=(net("n1", "NET1", "signal", (net_node("R1", "1"), net_node("R2", "1"), net_node("R3", "1"))),),
    )
    lookup = make_lookup((fp,))

    result = run_lvs(doc, lookup)

    assert result.ok is False
    opens = [i for i in result.issues if i.kind == "open"]
    assert len(opens) == 1
    assert opens[0].net_names == ("NET1",)
    # Two groups: {R1,R2} joined, {R3} alone. This is what distinguishes open from unrouted.
    assert len(opens[0].physical_net_ids) == 2
    assert result.summary.opens == 1
    assert result.summary.matched_nets == 0


def test_fresh_import_reports_one_consistent_kind_regardless_of_net_size() -> None:
    # The bug this guards: keying the kind off pin count made a 2-pin net say 'open'
    # and a 3-pin net say 'unrouted' for the identical "nothing wired yet" situation.
    fp = one_pin_footprint("fp1")
    doc = make_doc(
        components=(
            make_component("c1", "R1", "fp1", hole(0, 0)),
            make_component("c2", "R2", "fp1", hole(2, 0)),
            make_component("c3", "R3", "fp1", hole(4, 0)),
            make_component("c4", "R4", "fp1", hole(6, 0)),
            make_component("c5", "R5", "fp1", hole(8, 0)),
        ),
        nets=(
            net("n1", "TWO_PIN", "signal", (net_node("R1", "1"), net_node("R2", "1"))),
            net("n2", "THREE_PIN", "signal", (net_node("R3", "1"), net_node("R4", "1"), net_node("R5", "1"))),
        ),
    )

    result = run_lvs(doc, make_lookup((fp,)))
    kinds = {i.kind for i in result.issues}
    assert kinds == {"unrouted-net"}
    assert result.summary.opens == 2


def test_reports_a_single_short_issue_naming_both_nets_for_gnd_vplus_bridge() -> None:
    g1, g2 = hole(0, 0), hole(1, 0)
    p1, p2 = hole(2, 0), hole(3, 0)  # p1 adjacent to g2 -- the accidental bridge lands here
    fp = one_pin_footprint("fp1")
    doc = make_doc(
        components=(
            make_component("c1", "G1", "fp1", g1),
            make_component("c2", "G2", "fp1", g2),
            make_component("c3", "P1", "fp1", p1),
            make_component("c4", "P2", "fp1", p2),
        ),
        conductors=(
            solder_trace("t1", (g1, g2)),
            solder_trace("t2", (p1, p2)),
            solder_trace("bridge", (g2, p1)),  # accidental bridge between the GND and V+ rails
        ),
        nets=(
            net("gnd", "GND", "ground", (net_node("G1", "1"), net_node("G2", "1"))),
            net("vplus", "V+", "power", (net_node("P1", "1"), net_node("P2", "1"))),
        ),
    )
    lookup = make_lookup((fp,))

    result = run_lvs(doc, lookup)

    assert result.ok is False
    shorts = [i for i in result.issues if i.kind == "short"]
    assert len(shorts) == 1
    assert shorts[0].net_names == ("GND", "V+")
    assert "GND" in shorts[0].message
    assert "V+" in shorts[0].message
    assert "CRITICAL SHORT" in shorts[0].message
    assert [i for i in result.issues if i.kind == "open"] == []
    assert result.summary.shorts == 1


def test_reports_both_open_and_short_when_a_net_is_split_and_bridged() -> None:
    u1, u2 = hole(0, 0), hole(1, 0)
    u4 = hole(2, 0)  # AUX component, adjacent to u2 -- the accidental bridge
    u3 = hole(10, 10)  # CLK's third pin, left isolated -- the "open" half
    fp = one_pin_footprint("fp1")
    doc = make_doc(
        components=(
            make_component("c1", "U1", "fp1", u1),
            make_component("c2", "U2", "fp1", u2),
            make_component("c3", "U3", "fp1", u3),
            make_component("c4", "U4", "fp1", u4),
        ),
        conductors=(solder_trace("t1", (u1, u2, u4)),),  # connects CLK's U1-U2 but runs into AUX's U4
        nets=(
            net("clk", "CLK", "signal", (net_node("U1", "1"), net_node("U2", "1"), net_node("U3", "1"))),
            net("aux", "AUX", "signal", (net_node("U4", "1"),)),
        ),
    )
    lookup = make_lookup((fp,))

    result = run_lvs(doc, lookup)

    assert result.ok is False
    open_issue = next((i for i in result.issues if i.kind == "open"), None)
    short_issue = next((i for i in result.issues if i.kind == "short"), None)
    assert open_issue is not None
    assert open_issue.net_names == ("CLK",)
    assert short_issue is not None
    assert short_issue.net_names == ("AUX", "CLK")
    assert result.summary.matched_nets == 0


def test_reports_a_floating_conductor_with_no_component_pin() -> None:
    a, b = hole(20, 20), hole(21, 20)
    doc = make_doc(conductors=(solder_trace("stray", (a, b)),))
    lookup = make_lookup(())

    result = run_lvs(doc, lookup)

    assert result.ok is False
    issue = next(i for i in result.issues if i.kind == "floating-conductor")
    assert issue.pins == ()
    assert issue.conductor_ids == ("stray",)


def test_reports_an_unplaced_component_referenced_by_a_schematic_net() -> None:
    doc = make_doc(nets=(net("missing", "MISSING", "signal", (net_node("U99", "1"),)),))
    lookup = make_lookup(())

    result = run_lvs(doc, lookup)

    assert result.ok is False
    issue = next(i for i in result.issues if i.kind == "unplaced-component")
    assert issue.net_names == ("MISSING",)
    assert issue.pins == (PhysicalPinRef(component_ref="U99", pin="1"),)


def test_reports_unknown_footprint_for_unresolvable_footprint_id() -> None:
    a = hole(0, 0)
    doc = make_doc(
        components=(make_component("c1", "U1", "does-not-exist", a),),
        nets=(net("n1", "NET1", "signal", (net_node("U1", "1"),)),),
    )
    lookup = make_lookup(())  # empty: nothing resolves

    result = run_lvs(doc, lookup)

    issue = next(i for i in result.issues if i.kind == "unknown-footprint")
    assert issue.net_names == ("NET1",)
    assert issue.pins == (PhysicalPinRef(component_ref="U1", pin="1"),)


def test_single_pin_schematic_net_produces_no_issues() -> None:
    a = hole(0, 0)
    fp = one_pin_footprint("fp1")
    doc = make_doc(
        components=(make_component("c1", "TP1", "fp1", a),),
        nets=(net("tp", "TESTPOINT", "signal", (net_node("TP1", "1"),)),),
    )
    result = run_lvs(doc, make_lookup((fp,)))

    assert result.ok is True
    assert result.issues == ()


def test_deterministic_across_repeated_runs_and_input_reordering() -> None:
    a, b, c = hole(0, 0), hole(1, 0), hole(5, 5)
    fp = one_pin_footprint("fp1")
    components = (
        make_component("c1", "R1", "fp1", a),
        make_component("c2", "R2", "fp1", b),
        make_component("c3", "R3", "fp1", c),
    )
    conductors: tuple[Conductor, ...] = (solder_trace("t1", (a, b)),)
    nets = (
        net("n1", "NET1", "signal", (net_node("R1", "1"), net_node("R2", "1"))),
        net("n2", "NET2", "signal", (net_node("R3", "1"),)),
    )
    doc = make_doc(components, conductors, nets)
    lookup = make_lookup((fp,))

    first = run_lvs(doc, lookup)
    second = run_lvs(doc, lookup)
    assert second == first

    reordered_doc = make_doc(tuple(reversed(components)), conductors, tuple(reversed(nets)))
    reordered = run_lvs(reordered_doc, lookup)
    assert reordered == first


# ---------------------------------------------------------------------------
# continuity_checks unit tests
# ---------------------------------------------------------------------------


def test_continuity_checks_return_spanning_chain_for_four_pin_net() -> None:
    doc = make_doc(
        nets=(
            net(
                "n1",
                "BUS",
                "signal",
                (net_node("U1", "1"), net_node("U2", "2"), net_node("U3", "3"), net_node("U4", "4")),
            ),
        )
    )

    checks = continuity_checks(doc)

    assert len(checks) == 3
    assert all(c.net_name == "BUS" for c in checks)
    # Spanning chain: each check's `a` matches the previous check's `b`.
    for i in range(1, len(checks)):
        assert checks[i].a == checks[i - 1].b


def test_continuity_checks_produce_none_for_single_pin_or_empty_nets() -> None:
    doc = make_doc(
        nets=(
            net("n1", "SINGLE", "signal", (net_node("U1", "1"),)),
            net("n2", "EMPTY", "signal", ()),
        )
    )
    assert continuity_checks(doc) == ()


# ---------------------------------------------------------------------------
# isolation_checks unit tests
# ---------------------------------------------------------------------------


def test_isolation_checks_prioritise_a_power_ground_pair() -> None:
    doc = make_doc(
        nets=(
            net("sig", "SIG", "signal", (net_node("U1", "1"),)),
            net("gnd", "GND", "ground", (net_node("U2", "1"),)),
            net("vcc", "V+", "power", (net_node("U3", "1"),)),
        )
    )

    checks = isolation_checks(doc)

    assert len(checks) > 0
    first = checks[0]
    assert sorted((first.net_a, first.net_b)) == ["GND", "V+"]


def test_isolation_checks_are_capped_at_forty() -> None:
    # 15 nets -> C(15, 2) = 105 candidate pairs, comfortably over the cap.
    nets = tuple(net(f"n{i}", f"NET{i}", "signal", (net_node(f"U{i}", "1"),)) for i in range(15))
    doc = make_doc(nets=nets)

    checks = isolation_checks(doc)

    assert len(checks) == 40
