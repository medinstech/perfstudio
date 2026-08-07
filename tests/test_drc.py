"""Tests for the design rule checker (src/perfstudio/drc.py).

Two layers, mirroring test_connectivity.py's structure and priorities:

1. Golden differential tests: every board document under
   tools/diffcheck/golden/*.perf, run through run_drc(), must reproduce the
   `drc` array of its *.expected.json byte-for-byte on the fields the golden
   data actually captures -- rule, severity, holes, componentIds, conductorIds,
   in the exact order the TypeScript engine (packages/core/src/drc.ts) emitted
   them. `message` is deliberately excluded: generate.mjs does not capture it
   (see tools/diffcheck/generate.mjs), so it is not part of the acceptance
   criterion. A match here is proof of equivalence with the original engine,
   not just "my own idea of correct".

2. Hand-built unit tests translated from packages/core/src/drc.test.ts. These
   matter more here than they did for connectivity: of the 12 rules, the 15
   golden fixtures only ever exercise 7 (component-body-overlap,
   component-off-board, duplicate-pin-hole, pad-lifting-risk,
   pin-not-connected, solder-trace-proximity, solder-trace-too-long) --
   crossing-conductors, solder-trace-invalid-path, current-capacity,
   creepage-clearance and lead-bend-too-long never fire in any of the 15
   fixtures. The unit tests below are what actually exercises those five.

The `_load_golden` helper is deliberately minimal, private test scaffolding,
copied from test_connectivity.py's approach and extended to also parse `nets`
(drc.py's current-capacity/creepage-clearance/pin-not-connected rules need
them, unlike connectivity.py). It is NOT the real persistence layer --
persist.py (being written separately) will supersede it, and nothing outside
this test file should depend on it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from perfstudio.drc import (
    DEFAULT_DRC_OPTIONS,
    DrcOptions,
    DrcViolation,
    run_drc,
)
from perfstudio.footprints import footprint_lookup
from perfstudio.model import (
    Board,
    BoardSide,
    Conductor,
    ComponentInstance,
    DocumentMeta,
    HoleCoord,
    LeadBendConductor,
    Net,
    NetNode,
    PerfDocument,
    Rotation,
    SolderBuildup,
    SolderTraceConductor,
    SpineSpec,
    StripConductor,
    WireConductor,
)

# ---------------------------------------------------------------------------
# Golden fixtures: minimal *.perf reader (board, components, conductors, nets).
#
# Scaffolding only -- see module docstring. Parses exactly the subset of the
# wire format drc.py consumes; footprints come from the real, verified
# perfstudio.footprints.footprint_lookup() registry, not a fixture file, since
# every golden .perf's footprintId is one of the standard library's ids.
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
        nodes=tuple(_net_node(n) for n in raw.get("nodes", [])),
        net_class=raw.get("class", "signal"),
        current_a=raw.get("currentA"),
        voltage_v=raw.get("voltageV"),
    )


def _document(raw: dict[str, Any]) -> PerfDocument:
    """Load a golden .perf exactly as it sits on disk, order and all.

    An earlier version of this loader re-sorted the arrays back into id-creation order,
    to work around the generator having computed its expected output from the in-memory
    document while writing the .perf sorted. That inconsistency has been fixed at source
    (tools/diffcheck/generate.mjs now describes the reloaded document), so the loader
    must NOT reorder anything: the whole point is to run DRC on the same bytes a real
    user's project file would give it.
    """
    meta_raw = raw["meta"]
    components = [_component(c) for c in raw.get("components", [])]
    conductors = [_conductor(c) for c in raw.get("conductors", [])]
    nets = [_net(n) for n in raw.get("nets", [])]
    return PerfDocument(
        meta=DocumentMeta(
            name=meta_raw["name"], created=meta_raw["created"], modified=meta_raw["modified"]
        ),
        board=_board(raw["board"]),
        components=tuple(components),
        conductors=tuple(conductors),
        nets=tuple(nets),
        format_version=raw.get("formatVersion", 1),
    )


def _load_golden(name: str) -> tuple[PerfDocument, list[dict[str, Any]]]:
    """Load one golden case: the board document and its expected drc array."""
    doc = _document(json.loads((GOLDEN_DIR / f"{name}.perf").read_text(encoding="utf-8")))
    expected = json.loads((GOLDEN_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))
    return doc, expected["drc"]


_FOOTPRINT_LOOKUP = footprint_lookup()


def _violation_to_jsonable(v: DrcViolation) -> dict[str, Any]:
    return {
        "rule": v.rule,
        "severity": v.severity,
        "holes": [{"col": h.col, "row": h.row} for h in v.holes],
        "componentIds": list(v.component_ids),
        "conductorIds": list(v.conductor_ids),
    }


# ---------------------------------------------------------------------------
# THE deliverable: exact match against the TypeScript engine's output.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_name", GOLDEN_CASE_NAMES)
def test_matches_typescript_golden_drc(case_name: str) -> None:
    doc, expected_violations = _load_golden(case_name)

    actual_violations = [_violation_to_jsonable(v) for v in run_drc(doc, _FOOTPRINT_LOOKUP)]

    if actual_violations != expected_violations:
        actual_rules = [v["rule"] for v in actual_violations]
        expected_rules = [v["rule"] for v in expected_violations]
        diff_lines = []
        max_len = max(len(actual_violations), len(expected_violations))
        for i in range(max_len):
            a = actual_violations[i] if i < len(actual_violations) else None
            e = expected_violations[i] if i < len(expected_violations) else None
            if a != e:
                diff_lines.append(f"  [{i}] got={a!r}\n       want={e!r}")
        detail = "\n".join(diff_lines)
        pytest.fail(
            f"drc mismatch for golden case {case_name!r}: "
            f"got {len(actual_violations)} violations (rules={actual_rules}), "
            f"expected {len(expected_violations)} (rules={expected_rules}).\n{detail}"
        )


def test_golden_case_count_is_fifteen() -> None:
    """Sanity check on the fixture set itself: 15 documents, as the task specifies."""
    perf_files = sorted(GOLDEN_DIR.glob("*.perf"))
    assert len(perf_files) == 15
    assert {p.stem for p in perf_files} == set(GOLDEN_CASE_NAMES)


def test_golden_fixtures_exercise_seven_of_twelve_rules() -> None:
    """Documents which rules the golden data actually covers, so the gap is
    visible rather than silently assumed. The other five (crossing-conductors,
    solder-trace-invalid-path, current-capacity, creepage-clearance,
    lead-bend-too-long) are covered by the unit tests below instead.
    """
    seen_rules: set[str] = set()
    for case_name in GOLDEN_CASE_NAMES:
        _doc, expected = _load_golden(case_name)
        seen_rules.update(v["rule"] for v in expected)

    assert seen_rules == {
        "component-body-overlap",
        "component-off-board",
        "duplicate-pin-hole",
        "pad-lifting-risk",
        "pin-not-connected",
        "solder-trace-proximity",
        "solder-trace-too-long",
    }


# ---------------------------------------------------------------------------
# Unit-test fixture builders, translated from drc.test.ts.
# ---------------------------------------------------------------------------


def hole(col: int, row: int) -> HoleCoord:
    return HoleCoord(col=col, row=row)


def board(**overrides: Any) -> Board:
    base: dict[str, Any] = dict(
        type="pad-per-hole",
        cols=40,
        rows=40,
        pitch=2.54,
        thickness=1.6,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=0.8,
    )
    base.update(overrides)
    return Board(**base)


BOARD = board()


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


def solder_trace(
    cond_id: str,
    path: tuple[HoleCoord, ...],
    *,
    buildup: SolderBuildup = "normal",
    net_id: str | None = None,
    spine: SpineSpec | None = None,
) -> SolderTraceConductor:
    return SolderTraceConductor(
        id=cond_id, path=path, buildup=buildup, spine=spine, net_id=net_id, side="bottom"
    )


def solder_trace_wired(
    cond_id: str,
    path: tuple[HoleCoord, ...],
    spine: SpineSpec,
    *,
    buildup: SolderBuildup = "normal",
    net_id: str | None = None,
) -> SolderTraceConductor:
    return SolderTraceConductor(
        id=cond_id,
        path=path,
        buildup=buildup,
        spine=spine,
        net_id=net_id,
        kind="solder-trace-wired",
        side="bottom",
    )


def bare_wire(cond_id: str, path: tuple[HoleCoord, ...], side: BoardSide = "bottom") -> WireConductor:
    return WireConductor(id=cond_id, path=path, kind="bare-wire", side=side)


def lead_bend(
    cond_id: str, path: tuple[HoleCoord, ...], component_id: str = "cmp-x", pin_number: str = "1"
) -> LeadBendConductor:
    return LeadBendConductor(
        id=cond_id, path=path, component_id=component_id, pin_number=pin_number
    )


def make_doc(
    *,
    board_: Board = BOARD,
    components: tuple[ComponentInstance, ...] = (),
    conductors: tuple[Conductor, ...] = (),
    nets: tuple[Net, ...] = (),
) -> PerfDocument:
    return PerfDocument(
        meta=DocumentMeta(
            name="test", created="2024-01-01T00:00:00.000Z", modified="2024-01-01T00:00:00.000Z"
        ),
        board=board_,
        components=components,
        conductors=conductors,
        nets=nets,
    )


def by_rule(violations: list[DrcViolation], rule: str) -> list[DrcViolation]:
    return [v for v in violations if v.rule == rule]


# ---------------------------------------------------------------------------
# Rule 4: crossing-conductors -- not exercised by any golden fixture.
# ---------------------------------------------------------------------------


def test_crossing_conductors_flags_two_bare_wires_crossing_off_endpoint() -> None:
    # w1 runs A-B-C horizontally; w2 runs D-B-E vertically. Both pass over B
    # without terminating there, so B is not a registered contact for either --
    # an accidental crossing, and thus a short.
    a, b, c = hole(0, 2), hole(2, 2), hole(4, 2)
    d, e = hole(2, 0), hole(2, 4)
    doc = make_doc(conductors=(bare_wire("w1", (a, b, c)), bare_wire("w2", (d, b, e))))

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "crossing-conductors")
    assert len(violations) == 1
    assert violations[0].severity == "error"
    assert violations[0].holes == (b,)
    assert violations[0].conductor_ids == ("w1", "w2")


def test_crossing_conductors_does_not_flag_a_genuine_endpoint_junction() -> None:
    a, b, c = hole(0, 2), hole(2, 2), hole(4, 2)
    doc = make_doc(conductors=(bare_wire("w1", (a, b)), bare_wire("w2", (b, c))))

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "crossing-conductors")
    assert violations == []


def test_crossing_conductors_does_not_flag_two_solder_traces_sharing_a_pad() -> None:
    # Two solder traces sharing a hole are automatically the same physical net
    # (connectivity.py rule b), so the rule correctly never fires here.
    a, b, d = hole(0, 2), hole(2, 2), hole(2, 0)
    doc = make_doc(
        conductors=(
            solder_trace("t1", (a, hole(1, 2), b)),
            solder_trace("t2", (d, hole(2, 1), b)),
        )
    )

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "crossing-conductors")
    assert violations == []


# ---------------------------------------------------------------------------
# Rule 5: solder-trace-invalid-path -- not exercised by any golden fixture.
# ---------------------------------------------------------------------------


def test_solder_trace_invalid_path_flags_a_diagonal_step() -> None:
    doc = make_doc(conductors=(solder_trace("t1", (hole(0, 0), hole(1, 1))),))

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "solder-trace-invalid-path")
    assert len(violations) == 1
    assert violations[0].severity == "error"


def test_solder_trace_invalid_path_does_not_flag_a_valid_chain() -> None:
    doc = make_doc(conductors=(solder_trace("t1", (hole(0, 0), hole(1, 0), hole(2, 0))),))

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "solder-trace-invalid-path")
    assert violations == []


# ---------------------------------------------------------------------------
# Rule 6: solder-trace-proximity -- THE headline rule; golden data does
# exercise it, but these pin down the exact semantics per drc.test.ts.
# ---------------------------------------------------------------------------


def test_solder_trace_proximity_flags_once_for_a_different_net_neighbour() -> None:
    fp = _FOOTPRINT_LOOKUP("r-axial-4")
    assert fp is not None
    doc = make_doc(
        components=(make_component("c1", "U1", "r-axial-4", hole(2, 1)),),
        conductors=(solder_trace("t1", (hole(1, 2), hole(2, 2), hole(3, 2))),),
    )

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "solder-trace-proximity")
    assert len(violations) == 1
    assert violations[0].severity == "warning"
    assert violations[0].holes == (hole(2, 2), hole(2, 1))


def test_solder_trace_proximity_flags_nothing_for_a_same_net_neighbour() -> None:
    doc = make_doc(
        components=(make_component("c1", "U1", "r-axial-4", hole(2, 1)),),
        conductors=(
            solder_trace("t1", (hole(1, 2), hole(2, 2), hole(3, 2))),
            bare_wire("w1", (hole(2, 1), hole(1, 2))),  # bridges U1.1 onto the trace's net
        ),
    )

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "solder-trace-proximity")
    assert violations == []


def test_solder_trace_proximity_flags_nothing_for_an_empty_neighbour() -> None:
    doc = make_doc(conductors=(solder_trace("t1", (hole(1, 2), hole(2, 2), hole(3, 2))),))

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "solder-trace-proximity")
    assert violations == []


def test_solder_trace_proximity_uses_board_pitch_minus_pad_diameter_for_the_gap() -> None:
    """The rule computes the gap from board.pitch - board.pad_diameter rather
    than hardcoding 0.6 mm, so it stays correct for any board -- verify the
    computed gap changes when the board's geometry does, via a board whose
    pitch/pad combination is not the 2.54/1.9 default.
    """
    wide_board = board(pitch=5.0, pad_diameter=2.0)  # gap = 3.0 mm, not ~0.64 mm
    doc = make_doc(
        board_=wide_board,
        components=(make_component("c1", "U1", "r-axial-4", hole(2, 1)),),
        conductors=(solder_trace("t1", (hole(1, 2), hole(2, 2), hole(3, 2))),),
    )

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "solder-trace-proximity")
    assert len(violations) == 1
    assert "3.00 mm" in violations[0].message


# ---------------------------------------------------------------------------
# Rule 9: current-capacity -- not exercised by any golden fixture.
# ---------------------------------------------------------------------------


def test_current_capacity_flags_inadequate_cross_section() -> None:
    # light buildup = 0.15mm^2 -> capacity = 0.15 * 5 A/mm^2 = 0.75A at defaults.
    net = Net(id="n1", name="VBUS", nodes=(), net_class="power", current_a=3)
    path = (hole(0, 0), hole(1, 0), hole(2, 0))
    doc = make_doc(nets=(net,), conductors=(solder_trace("t1", path, buildup="light", net_id="n1"),))

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "current-capacity")
    assert len(violations) == 1
    assert violations[0].severity == "warning"
    assert "mOhm" in violations[0].message
    assert "mV" in violations[0].message


def test_current_capacity_does_not_flag_adequate_cross_section() -> None:
    # heavy buildup = 0.6mm^2 -> capacity = 0.6 * 5 A/mm^2 = 3A; net draws 0.5A.
    net = Net(id="n1", name="VBUS", nodes=(), net_class="power", current_a=0.5)
    path = (hole(0, 0), hole(1, 0), hole(2, 0))
    doc = make_doc(nets=(net,), conductors=(solder_trace("t1", path, buildup="heavy", net_id="n1"),))

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "current-capacity")
    assert violations == []


def test_current_capacity_wired_spine_reduces_reported_resistance() -> None:
    net_hot = Net(id="n1", name="VBUS", nodes=(), net_class="power", current_a=10)
    path = (hole(0, 0), hole(1, 0), hole(2, 0), hole(3, 0))
    pure_doc = make_doc(
        nets=(net_hot,), conductors=(solder_trace("t1", path, buildup="normal", net_id="n1"),)
    )
    wired_doc = make_doc(
        nets=(net_hot,),
        conductors=(
            solder_trace_wired(
                "t1", path, SpineSpec(material="tinned-copper", gauge=0.6), buildup="normal", net_id="n1"
            ),
        ),
    )

    import re

    def extract_m_ohm(msg: str) -> float:
        m = re.search(r"~([\d.]+) mOhm", msg)
        assert m is not None
        return float(m.group(1))

    pure_msg = by_rule(run_drc(pure_doc, _FOOTPRINT_LOOKUP), "current-capacity")[0].message
    wired_msg = by_rule(run_drc(wired_doc, _FOOTPRINT_LOOKUP), "current-capacity")[0].message
    assert extract_m_ohm(wired_msg) < extract_m_ohm(pure_msg)


# ---------------------------------------------------------------------------
# Rule 10: creepage-clearance -- not exercised by any golden fixture.
# ---------------------------------------------------------------------------


def test_creepage_flags_high_voltage_conductor_next_to_a_different_net() -> None:
    net = Net(id="n1", name="MAINS", nodes=(), net_class="power", voltage_v=400)
    doc = make_doc(
        components=(make_component("c1", "U1", "r-axial-4", hole(2, 1)),),
        nets=(net,),
        conductors=(solder_trace("t1", (hole(1, 2), hole(2, 2), hole(3, 2)), net_id="n1"),),
    )

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "creepage-clearance")
    assert len(violations) == 1
    assert violations[0].severity == "warning"


def test_creepage_does_not_flag_low_voltage_conductor() -> None:
    net = Net(id="n1", name="SIGNAL", nodes=(), net_class="signal", voltage_v=12)
    doc = make_doc(
        components=(make_component("c1", "U1", "r-axial-4", hole(2, 1)),),
        nets=(net,),
        conductors=(solder_trace("t1", (hole(1, 2), hole(2, 2), hole(3, 2)), net_id="n1"),),
    )

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "creepage-clearance")
    assert violations == []


# ---------------------------------------------------------------------------
# Rule 11: lead-bend-too-long -- not exercised by any golden fixture.
# ---------------------------------------------------------------------------


def test_lead_bend_too_long_flags_a_long_bend() -> None:
    doc = make_doc(conductors=(lead_bend("lb1", (hole(0, 0), hole(6, 0)), "c1", "1"),))  # 6 > 4

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "lead-bend-too-long")
    assert len(violations) == 1
    assert violations[0].severity == "warning"
    assert violations[0].component_ids == ("c1",)


def test_lead_bend_too_long_does_not_flag_a_short_bend() -> None:
    doc = make_doc(conductors=(lead_bend("lb1", (hole(0, 0), hole(2, 0)), "c1", "1"),))  # 2 <= 4

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "lead-bend-too-long")
    assert violations == []


# ---------------------------------------------------------------------------
# Rule 12: pin-not-connected
# ---------------------------------------------------------------------------


def test_pin_not_connected_flags_a_fully_isolated_pin() -> None:
    net = Net(id="n1", name="NET1", nodes=(NetNode(component_ref="R1", pin="1"),), net_class="signal")
    doc = make_doc(components=(make_component("c1", "R1", "r-axial-4", hole(5, 5)),), nets=(net,))

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "pin-not-connected")
    assert len(violations) == 1
    assert violations[0].severity == "warning"
    assert violations[0].holes == (hole(5, 5),)


def test_pin_not_connected_does_not_flag_a_pin_with_any_conductor_attached() -> None:
    net = Net(id="n1", name="NET1", nodes=(NetNode(component_ref="R1", pin="1"),), net_class="signal")
    doc = make_doc(
        components=(make_component("c1", "R1", "r-axial-4", hole(5, 5)),),
        nets=(net,),
        conductors=(bare_wire("w1", (hole(5, 5), hole(6, 5))),),
    )

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "pin-not-connected")
    assert violations == []


def test_pin_not_connected_does_not_flag_a_pin_joined_to_another_pin() -> None:
    net = Net(
        id="n1",
        name="NET1",
        nodes=(NetNode(component_ref="R1", pin="1"), NetNode(component_ref="R2", pin="1")),
        net_class="signal",
    )
    doc = make_doc(
        components=(
            make_component("c1", "R1", "r-axial-4", hole(5, 5)),
            make_component("c2", "R2", "r-axial-4", hole(6, 5)),
        ),
        nets=(net,),
        conductors=(solder_trace("t1", (hole(5, 5), hole(6, 5))),),
    )

    violations = by_rule(run_drc(doc, _FOOTPRINT_LOOKUP), "pin-not-connected")
    assert violations == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_run_drc_is_deterministic_and_stably_sorted_across_repeated_runs() -> None:
    net = Net(
        id="n1", name="VBUS", nodes=(NetNode(component_ref="R1", pin="1"),), net_class="power", current_a=3
    )
    doc = make_doc(
        board_=board(material="FR2"),
        components=(
            make_component("c1", "R1", "r-axial-4", hole(0, 0)),
            make_component("c2", "R2", "r-axial-4", hole(1, 0)),  # overlaps c1
            make_component("c3", "U1", "r-axial-4", hole(2, 1)),
        ),
        nets=(net,),
        conductors=(
            solder_trace(
                "t1",
                tuple(hole(i, 2) for i in range(1, 8)),
                buildup="light",
                net_id="n1",
            ),
        ),
    )

    first = run_drc(doc, _FOOTPRINT_LOOKUP)
    second = run_drc(doc, _FOOTPRINT_LOOKUP)
    assert second == first
    assert len(first) > 1  # sanity: this fixture exercises multiple rules

    # Stably sorted: rule ids must be non-decreasing across the whole output.
    for prev, cur in zip(first, first[1:]):
        assert prev.rule <= cur.rule

    third = run_drc(doc, _FOOTPRINT_LOOKUP)
    assert third == first


def test_default_drc_options_is_a_stable_fully_populated_defaults_object() -> None:
    assert DEFAULT_DRC_OPTIONS.pad_lifting_max_solder_trace_pads == 6
    assert DEFAULT_DRC_OPTIONS.solder_trace_feasibility_max_pads == 6
    assert DEFAULT_DRC_OPTIONS.creepage_voltage_threshold_v == 300
    assert DEFAULT_DRC_OPTIONS.max_lead_bend_holes == 4
    assert isinstance(DEFAULT_DRC_OPTIONS, DrcOptions)
