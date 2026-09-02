"""Tests for perfstudio.parsers.sexpr and perfstudio.parsers.kicad.

`NE555_ASTABLE_NETLIST` below is a byte-for-byte port of the TypeScript fixture at
packages/parsers/src/__fixtures__/ne555-astable.ts, which
tools/diffcheck/golden/ne555.perf was itself generated from (via
tools/diffcheck/generate.mjs). The assertions here check the importer's output
against what that TS fixture's own doc comment says it is deliberately exercising:
net-code sort order, mixed `(ref X)` / `(ref "X")` quoting, and the
"unconnected-(U1-Pad4)" pseudo-net being dropped with a warning. The resulting net
names/classes/membership are cross-checked against tools/diffcheck/golden/ne555.perf,
which is the same circuit run all the way through the real engine.
"""

from __future__ import annotations

import pathlib

import pytest

from perfstudio.parsers.kicad import ImportedComponent, infer_net_class, parse_kicad_netlist
from perfstudio.parsers.sexpr import SExprSyntaxError, parse_sexpr

# ---------------------------------------------------------------------------
# Fixture: ported verbatim (modulo JS-template-literal -> Python-string syntax) from
# packages/parsers/src/__fixtures__/ne555-astable.ts
# ---------------------------------------------------------------------------
#
# Circuit: classic 555 astable oscillator.
#   - U1: NE555 timer
#   - R1, R2: timing resistors (charge / discharge path)
#   - C1: timing capacitor
#   - C2: control-voltage pin (5) bypass capacitor
#   - R3: current-limiting resistor for the output LED
#   - LED1: output indicator LED
#   - J1: 2-pin power input connector
#
# Two things are deliberate, to give the importer something to report on:
#   - Net codes are out of numeric order in the source text (net "GND" is code "3" and
#     appears first), to exercise "sort nets by numeric code".
#   - U1 pin 4 (RESET) is wired to its own "unconnected-(U1-Pad4)" pseudo-net, the way
#     KiCad emits an unwired pin, purely to exercise the importer's unconnected-net
#     handling. (A real board would tie RESET to VCC.)
#
# Ref quoting is deliberately mixed between `(ref U1)` and `(ref "R1")` forms, matching
# the inconsistency between KiCad versions that the importer must tolerate.
NE555_ASTABLE_NETLIST = """
(export (version "E")
  (design
    (source "/home/user/ne555_astable/ne555_astable.kicad_sch")
    (date "2026-01-01T00:00:00")
    (tool "Eeschema 8.0.0")
    (sheet (number "1") (name "/") (tstamps "/")
      (title_block
        (title "NE555 Astable")
        (company "")
        (rev "")
        (date "")
        (source "ne555_astable.kicad_sch"))))
  (components
    (comp (ref U1)
      (value "NE555")
      (footprint "Package_DIP:DIP-8_W7.62mm")
      (libsource (lib "Timer") (part "NE555") (description "Single 555 timer"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000001"))
    (comp (ref "R1")
      (value "10k")
      (footprint "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal")
      (libsource (lib "Device") (part "R") (description "Resistor"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000002"))
    (comp (ref "R2")
      (value "100k")
      (footprint "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal")
      (libsource (lib "Device") (part "R") (description "Resistor"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000003"))
    (comp (ref C1)
      (value "10uF")
      (footprint "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm")
      (libsource (lib "Device") (part "C_Polarized") (description "Polarized capacitor"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000004"))
    (comp (ref "C2")
      (value "0.01uF")
      (footprint "Capacitor_THT:C_Disc_D3.0mm_W2.0mm_P2.50mm")
      (libsource (lib "Device") (part "C") (description "Unpolarized capacitor"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000005"))
    (comp (ref "R3")
      (value "330")
      (footprint "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal")
      (libsource (lib "Device") (part "R") (description "Resistor"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000006"))
    (comp (ref LED1)
      (value "LED")
      (footprint "LED_THT:LED_D5.0mm")
      (libsource (lib "Device") (part "LED") (description "Light emitting diode"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000007"))
    (comp (ref "J1")
      (value "Conn_01x02")
      (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
      (libsource (lib "Connector_Generic") (part "Conn_01x02") (description "Generic connector, 2 pins"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000008")))
  (nets
    (net (code "3") (name "GND")
      (node (ref U1) (pin "1") (pinfunction "GND") (pintype "power_in"))
      (node (ref C1) (pin "2") (pinfunction "~") (pintype "passive"))
      (node (ref "C2") (pin "2") (pinfunction "~") (pintype "passive"))
      (node (ref LED1) (pin "2") (pinfunction "K") (pintype "passive"))
      (node (ref "J1") (pin "2") (pinfunction "Pin_2") (pintype "passive")))
    (net (code "1") (name "VCC")
      (node (ref U1) (pin "8") (pinfunction "VCC") (pintype "power_in"))
      (node (ref "R1") (pin "1") (pinfunction "~") (pintype "passive"))
      (node (ref "J1") (pin "1") (pinfunction "Pin_1") (pintype "passive")))
    (net (code "5") (name "THRESH")
      (node (ref U1) (pin "2") (pinfunction "TRIG") (pintype "input"))
      (node (ref U1) (pin "6") (pinfunction "THR") (pintype "input"))
      (node (ref "R2") (pin "2") (pinfunction "~") (pintype "passive"))
      (node (ref C1) (pin "1") (pinfunction "~") (pintype "passive")))
    (net (code "8") (name "unconnected-(U1-Pad4)")
      (node (ref U1) (pin "4") (pinfunction "RESET") (pintype "input")))
    (net (code "2") (name "OUT")
      (node (ref U1) (pin "3") (pinfunction "OUT") (pintype "output"))
      (node (ref "R3") (pin "1") (pinfunction "~") (pintype "passive")))
    (net (code "7") (name "CTRL")
      (node (ref U1) (pin "5") (pinfunction "CV") (pintype "passive"))
      (node (ref "C2") (pin "1") (pinfunction "~") (pintype "passive")))
    (net (code "6") (name "DISCH")
      (node (ref U1) (pin "7") (pinfunction "DISCH") (pintype "output"))
      (node (ref "R1") (pin "2") (pinfunction "~") (pintype "passive"))
      (node (ref "R2") (pin "1") (pinfunction "~") (pintype "passive")))
    (net (code "4") (name "LED_A")
      (node (ref "R3") (pin "2") (pinfunction "~") (pintype "passive"))
      (node (ref LED1) (pin "1") (pinfunction "A") (pintype "passive")))))
"""

GOLDEN_NE555 = pathlib.Path(__file__).resolve().parent.parent / "tools" / "diffcheck" / "golden" / "ne555.perf"

# ---------------------------------------------------------------------------
# sexpr.py
# ---------------------------------------------------------------------------


def test_parse_sexpr_atoms_and_lists() -> None:
    assert parse_sexpr("(a b (c d) e)") == [["a", "b", ["c", "d"], "e"]]


def test_parse_sexpr_quoted_string_escapes() -> None:
    assert parse_sexpr(r'("a\"b" "c\\d" "e\nf")') == [['a"b', "c\\d", "e\nf"]]


def test_parse_sexpr_mixed_quoting_same_result() -> None:
    """`(ref U1)` and `(ref "U1")` must parse to the identical structure -- this is
    exactly what lets the importer ignore KiCad's inconsistent quoting.
    """
    assert parse_sexpr("(ref U1)") == parse_sexpr('(ref "U1")')


def test_parse_sexpr_unbalanced_parens_raises_with_offset() -> None:
    with pytest.raises(SExprSyntaxError):
        parse_sexpr("(a (b)")


def test_parse_sexpr_unexpected_close_raises() -> None:
    with pytest.raises(SExprSyntaxError):
        parse_sexpr("(a))")


def test_parse_sexpr_unterminated_string_raises() -> None:
    with pytest.raises(SExprSyntaxError):
        parse_sexpr('("unterminated')


def test_parse_sexpr_does_not_hang_at_end_of_input() -> None:
    """Regression guard: whitespace-skipping must terminate at end-of-input rather
    than looping forever (a Python port of a `ch === ' ' || ...` boundary check is
    easy to get wrong via `ch in " \\t\\n"`, under which the empty end-of-input marker
    is trivially "in" the whitespace string).
    """
    assert parse_sexpr("   ") == []
    assert parse_sexpr("(a)   ") == [["a"]]


# ---------------------------------------------------------------------------
# infer_net_class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["GND", "gnd", "AGND", "DGND", "VSS", "0"])
def test_infer_net_class_ground(name: str) -> None:
    assert infer_net_class(name) == "ground"


@pytest.mark.parametrize("name", ["VCC", "vcc", "VDD", "+5V", "+3V3", "-12V", "VBAT1"])
def test_infer_net_class_power(name: str) -> None:
    assert infer_net_class(name) == "power"


@pytest.mark.parametrize("name", ["OUT", "CTRL", "THRESH", "DISCH", "LED_A", "SDA", "SCL"])
def test_infer_net_class_signal(name: str) -> None:
    assert infer_net_class(name) == "signal"


# ---------------------------------------------------------------------------
# parse_kicad_netlist -- against the NE555 fixture
# ---------------------------------------------------------------------------


def test_no_export_form_raises() -> None:
    with pytest.raises(ValueError, match="export"):
        parse_kicad_netlist("(not-a-netlist (foo bar))")


def test_ne555_component_count_and_fields() -> None:
    result = parse_kicad_netlist(NE555_ASTABLE_NETLIST)
    assert len(result.components) == 8
    assert result.components[0] == ImportedComponent(
        ref="U1", value="NE555", footprint="Package_DIP:DIP-8_W7.62mm", lib_part="NE555"
    )
    by_ref = {c.ref: c for c in result.components}
    assert set(by_ref) == {"U1", "R1", "R2", "C1", "C2", "R3", "LED1", "J1"}
    assert by_ref["R1"].value == "10k"
    assert by_ref["R2"].value == "100k"
    assert by_ref["J1"].footprint == "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
    assert by_ref["J1"].lib_part == "Conn_01x02"


def test_ne555_unconnected_pseudo_net_is_dropped_with_warning() -> None:
    result = parse_kicad_netlist(NE555_ASTABLE_NETLIST)
    assert all(not net.name.startswith("unconnected-") for net in result.nets)
    assert any("unconnected" in w.lower() for w in result.warnings), result.warnings


def test_ne555_net_count_after_dropping_unconnected() -> None:
    # 8 nets in the source; "unconnected-(U1-Pad4)" is dropped -> 7 remain.
    result = parse_kicad_netlist(NE555_ASTABLE_NETLIST)
    assert len(result.nets) == 7


def test_ne555_nets_sorted_by_numeric_code_ascending() -> None:
    """GND is net code "3" and appears first in the source text; VCC is code "1" and
    appears second. The importer must still emit ascending-by-code order.
    """
    result = parse_kicad_netlist(NE555_ASTABLE_NETLIST)
    # Codes: VCC=1, OUT=2, GND=3, LED_A=4, THRESH=5, DISCH=6, CTRL=7 (code 8,
    # "unconnected-(U1-Pad4)", is dropped). Matches tools/diffcheck/golden/ne555.perf.
    assert [n.id for n in result.nets] == ["net-1", "net-2", "net-3", "net-4", "net-5", "net-6", "net-7"]
    assert [n.name for n in result.nets] == ["VCC", "OUT", "GND", "LED_A", "THRESH", "DISCH", "CTRL"]


def test_ne555_net_class_inference() -> None:
    result = parse_kicad_netlist(NE555_ASTABLE_NETLIST)
    by_name = {n.name: n for n in result.nets}
    assert by_name["GND"].net_class == "ground"
    assert by_name["VCC"].net_class == "power"
    for signal_name in ("OUT", "THRESH", "CTRL", "DISCH", "LED_A"):
        assert by_name[signal_name].net_class == "signal", signal_name


def test_ne555_net_node_membership() -> None:
    result = parse_kicad_netlist(NE555_ASTABLE_NETLIST)
    by_name = {n.name: n for n in result.nets}
    gnd_pins = {(node.component_ref, node.pin) for node in by_name["GND"].nodes}
    assert gnd_pins == {("U1", "1"), ("C1", "2"), ("C2", "2"), ("LED1", "2"), ("J1", "2")}
    vcc_pins = {(node.component_ref, node.pin) for node in by_name["VCC"].nodes}
    assert vcc_pins == {("U1", "8"), ("R1", "1"), ("J1", "1")}


@pytest.mark.skipif(not GOLDEN_NE555.exists(), reason="golden ne555.perf not present")
def test_ne555_matches_golden_perf_net_structure() -> None:
    """Cross-check against tools/diffcheck/golden/ne555.perf: the same NE555 fixture,
    run all the way through the real (TypeScript) engine and serialized. Its `nets`
    array's id/name/class/node set must match what this importer produces directly
    from the raw netlist text.
    """
    from perfstudio import persist

    golden_doc = persist.parse_document_or_throw(GOLDEN_NE555.read_text(encoding="utf-8"))
    golden_by_name = {n.name: n for n in golden_doc.nets}

    imported = parse_kicad_netlist(NE555_ASTABLE_NETLIST)
    assert {n.name for n in imported.nets} == set(golden_by_name)

    for net in imported.nets:
        golden_net = golden_by_name[net.name]
        assert net.net_class == golden_net.net_class, net.name
        imported_pins = {(node.component_ref, node.pin) for node in net.nodes}
        golden_pins = {(node.component_ref, node.pin) for node in golden_net.nodes}
        assert imported_pins == golden_pins, net.name



# ---------------------------------------------------------------------------
# What the importer says about a file it cannot use, or only half can
# ---------------------------------------------------------------------------


def test_a_truncated_netlist_is_a_value_error_the_importers_catch() -> None:
    """Both the window and the MCP server catch ValueError around the import; a syntax
    error that was not one escaped both as a traceback."""
    with pytest.raises(ValueError):
        parse_kicad_netlist('(export (version "E")')


def test_a_schematic_handed_to_the_importer_is_named_as_such() -> None:
    """Feeding the importer the schematic instead of the netlist exported from it is the
    first-time mistake, and "no export form" did not say which file to use instead."""
    with pytest.raises(ValueError, match="schematic"):
        parse_kicad_netlist('(kicad_sch (version 20231120) (generator "eeschema"))')


def test_two_nets_with_one_code_are_disambiguated_with_a_warning() -> None:
    """Refusing the whole import over one collision left the user no way forward."""
    result = parse_kicad_netlist(
        """
        (export (version "E")
          (components (comp (ref "R1") (value "1k") (footprint "R_Axial")))
          (nets
            (net (code "1") (name "A") (node (ref "R1") (pin "1")) (node (ref "R1") (pin "2")))
            (net (code "1") (name "B") (node (ref "R1") (pin "1")) (node (ref "R1") (pin "2")))))
        """
    )
    ids = [net.id for net in result.nets]
    assert len(ids) == len(set(ids)) == 2
    assert any("shares its code" in w for w in result.warnings), result.warnings


def test_a_component_without_a_footprint_is_warned_about() -> None:
    """A missing footprint is what forces the host to guess one, which is the step the
    import dialog asks the user to check -- so it needs something to point at."""
    result = parse_kicad_netlist(
        '(export (version "E") (components (comp (ref "R1") (value "1k"))) (nets))'
    )
    assert result.components[0].footprint is None
    assert any("R1" in w and "footprint" in w for w in result.warnings), result.warnings
