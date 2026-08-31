"""Differential tests for the footprint port.

The acceptance criterion for src/perfstudio/footprints.py is that it
reproduces tools/diffcheck/golden/footprints.expected.json -- the registry
dumped from the original TypeScript engine (packages/core/src/footprints.ts)
-- EXACTLY: every id, every pin number and offset, every body-outline point,
every height, every archetype and dims value, and the polarized flag.

The golden file is camelCase (dCol, dRow, bodyOutline, bodyHeight,
leadDiameter); the Python model is snake_case (d_col, d_row, body_outline,
body_height, lead_diameter). This file maps the two explicitly, field by
field, rather than renaming anything in the model.

Comparisons use plain `==`, never `pytest.approx` or a proportional tolerance:
both TypeScript and Python arithmetic run on IEEE-754 doubles, so identical
formulas must produce bit-identical results. A tolerance here would silently
paper over a genuine formula divergence between the two implementations,
which is exactly the class of bug this test exists to catch.

THE ONE EXCEPTION, and it is bounded to a single unit in the last place.

IEEE-754 pins down + - * / exactly, but it does NOT standardise the rounding of
sin and cos. V8 and CPython genuinely disagree at pi/4:

    Node    Math.cos(pi/4) = 0.7071067811865476   bits ...189
            Math.sin(pi/4) = 0.7071067811865475   bits ...188
    Python  math.cos(pi/4) = 0.7071067811865476   bits ...189
            math.sin(pi/4) = 0.7071067811865476   bits ...189

V8's sin and cos disagree with each other; Python's agree. So the eight
footprints with a circular courtyard (four electrolytics, three LEDs, the
potentiometer) land one ULP apart on exactly one vertex of their 24-gon, the
one at theta = pi/4 where cos and sin are mathematically equal.

The gap is 4.44e-16 mm. No decision in this system can turn on it: DRC works
on bounding boxes, rendering rounds to pixels, the PDF rounds to points, and
footprints are a library rather than document data so they never reach the
byte-identical .perf round-trip.

Rather than loosen the comparison, `assert_coord` bounds the difference at TWO
ULPs OF THE SCALE THE VERTEX WAS COMPUTED AT — the centre and the radius that
were added together, not the smaller number that survives their cancellation.
A real formula divergence is orders of magnitude bigger than that and still
fails; so would a third ULP creeping in from somewhere else. The bound is
exactly as wide as the diagnosed problem and no wider. The note above
`MAX_TRIG_PROPAGATION_ULPS` records the second libm — macOS arm64 — that made
the distinction between those two scales matter.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any

import pytest

from perfstudio.footprints import (
    GENERATED_ID_GRAMMAR,
    axial_footprint,
    box_film_capacitor_footprint,
    crystal_hc49_footprint,
    dip_footprint,
    disc_ceramic_footprint,
    footprint_lookup,
    generated_footprint,
    generic_box_footprint,
    get_footprint,
    led_footprint,
    pin_header_footprint,
    potentiometer_footprint,
    radial_electrolytic_footprint,
    relay_footprint,
    screw_terminal_footprint,
    standard_footprints,
    tactile_switch_footprint,
    to92_footprint,
    to220_footprint,
)
from perfstudio.model import STANDARD_PITCH_MM, Footprint


def _ulps_apart(a: float, b: float) -> int:
    """Distance between two doubles counted in representable values.

    0 means bit-identical. 1 means adjacent — no double exists between them, so no
    formula can produce a value in the gap and the difference cannot be meaningful.
    """
    if a == b:
        return 0
    if math.isnan(a) or math.isnan(b) or math.isinf(a) or math.isinf(b):
        return 1 << 62

    def ordered(x: float) -> int:
        bits = struct.unpack("<q", struct.pack("<d", x))[0]
        # Map the sign-magnitude layout onto a monotonic integer ordering.
        return bits if bits >= 0 else (1 << 63) - bits

    return abs(ordered(a) - ordered(b))


# A circle vertex is computed as `centre + radius * cos(theta)`. Two implementations of
# cos disagree by at most one ULP, the multiply carries that relative error through, and
# the addition may round one further away. So two is the most the known artifact can
# produce, and that is the bound — derived from the arithmetic, not fitted to whatever
# made the suite green.
#
# TWO ULPS OF WHAT, THOUGH. This was originally counted on the vertex, which is correct
# until the addition cancels — and on a circle it cancels somewhere by construction.
# `led-3mm` vertex 8 is `1.27 + (-1.385)`: the terms are an order of magnitude larger
# than the 0.115 that survives them, so one ULP at the scale the arithmetic is done at is
# SIXTEEN ULPs at the scale of the result, without any formula having changed. Running
# the suite on macOS arm64 against a golden file generated on x86-64 produced exactly
# that: 16 ULPs on `led-3mm` and 4 on `c-elec-d10-p3`, matching the 16x and 4x
# amplification each vertex's own cancellation predicts, and nothing anywhere else.
#
# So the bound is applied where the error is made rather than where it is read off: two
# ULPs of the largest coordinate the footprint's own arithmetic works at. A genuine
# formula difference — wrong radius, wrong centre, wrong vertex count — is still around
# twelve orders of magnitude larger and still fails loudly.
MAX_TRIG_PROPAGATION_ULPS = 2


def assert_coord(
    footprint_id: str, field: str, actual: float, expected: float, term_scale_mm: float
) -> None:
    """Exact match, or at most the ULPs a trig disagreement can propagate into a sum.

    ``term_scale_mm`` is the magnitude the vertex was computed at — the largest
    coordinate in the same outline, which bounds both the centre and the radius that
    were added to produce this one. See the note above for why the bound cannot be
    counted on the result.
    """
    ulps = _ulps_apart(actual, expected)
    allowed = MAX_TRIG_PROPAGATION_ULPS * math.ulp(term_scale_mm)
    assert abs(actual - expected) <= allowed, (
        f"[{footprint_id}] {field}: {actual!r} != {expected!r} ({ulps} ULPs apart, "
        f"{abs(actual - expected):.3e} mm, bound is {allowed:.3e} mm at a term scale of "
        f"{term_scale_mm} mm). Beyond the bound this is a real formula difference, not a "
        f"trig rounding artifact — do not widen the bound, find the bug."
    )


GOLDEN_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "diffcheck" / "golden" / "footprints.expected.json"
)


def _load_golden() -> dict[str, dict[str, Any]]:
    with GOLDEN_PATH.open(encoding="utf-8") as f:
        return json.load(f)


GOLDEN: dict[str, dict[str, Any]] = _load_golden()
REGISTRY: dict[str, Footprint] = standard_footprints()

EXPECTED_COUNT = 61


# ---------------------------------------------------------------------------
# Registry-shape sanity
# ---------------------------------------------------------------------------


def test_golden_has_expected_footprint_count() -> None:
    assert len(GOLDEN) == EXPECTED_COUNT, (
        f"golden fixture footprints.expected.json has {len(GOLDEN)} entries, expected {EXPECTED_COUNT}"
    )


def test_registry_size_matches_golden() -> None:
    assert len(REGISTRY) == len(GOLDEN), (
        f"standard_footprints() has {len(REGISTRY)} entries, golden has {len(GOLDEN)}"
    )


def test_registry_ids_match_golden_exactly() -> None:
    actual_ids = set(REGISTRY.keys())
    expected_ids = set(GOLDEN.keys())
    missing = expected_ids - actual_ids
    extra = actual_ids - expected_ids
    assert not missing, f"ids present in golden but missing from ported registry: {sorted(missing)}"
    assert not extra, f"ids present in ported registry but not in golden: {sorted(extra)}"


# ---------------------------------------------------------------------------
# Full field-by-field diff against the golden registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("footprint_id", sorted(GOLDEN.keys()))
def test_footprint_matches_golden_field_by_field(footprint_id: str) -> None:
    expected = GOLDEN[footprint_id]
    actual = REGISTRY.get(footprint_id)
    assert actual is not None, f"[{footprint_id}] missing from ported registry (expected it to exist)"

    assert actual.id == expected["id"], (
        f"[{footprint_id}] field 'id': {actual.id!r} != {expected['id']!r}"
    )
    assert actual.name == expected["name"], (
        f"[{footprint_id}] field 'name': {actual.name!r} != {expected['name']!r}"
    )

    expected_pins = expected["pins"]
    assert len(actual.pins) == len(expected_pins), (
        f"[{footprint_id}] field 'pins': length {len(actual.pins)} != {len(expected_pins)}"
    )
    for i, (ap, ep) in enumerate(zip(actual.pins, expected_pins, strict=True)):
        assert ap.number == ep["number"], (
            f"[{footprint_id}] pins[{i}].number: {ap.number!r} != {ep['number']!r}"
        )
        assert ap.d_col == ep["dCol"], (
            f"[{footprint_id}] pins[{i}].dCol: {ap.d_col!r} != {ep['dCol']!r}"
        )
        assert ap.d_row == ep["dRow"], (
            f"[{footprint_id}] pins[{i}].dRow: {ap.d_row!r} != {ep['dRow']!r}"
        )
        expected_pin_name = ep.get("name")
        assert ap.name == expected_pin_name, (
            f"[{footprint_id}] pins[{i}].name: {ap.name!r} != {expected_pin_name!r}"
        )

    expected_outline = expected["bodyOutline"]
    assert len(actual.body_outline) == len(expected_outline), (
        f"[{footprint_id}] field 'bodyOutline': length {len(actual.body_outline)} != {len(expected_outline)}"
    )
    # The scale the outline's own arithmetic is done at: every centre and radius that
    # produced a vertex is bounded by the largest coordinate the outline reaches.
    term_scale = max(
        (abs(c) for p in actual.body_outline for c in (p.x, p.y)),
        default=1.0,
    )
    for i, (ao, eo) in enumerate(zip(actual.body_outline, expected_outline, strict=True)):
        assert_coord(footprint_id, f"bodyOutline[{i}].x", ao.x, eo["x"], term_scale)
        assert_coord(footprint_id, f"bodyOutline[{i}].y", ao.y, eo["y"], term_scale)

    assert actual.body_height == expected["bodyHeight"], (
        f"[{footprint_id}] field 'bodyHeight': {actual.body_height!r} != {expected['bodyHeight']!r}"
    )

    expected_body = expected["body"]
    assert actual.body.archetype == expected_body["archetype"], (
        f"[{footprint_id}] field 'body.archetype': {actual.body.archetype!r} != {expected_body['archetype']!r}"
    )

    expected_dims = expected_body["dims"]
    actual_dim_keys = set(actual.body.dims.keys())
    expected_dim_keys = set(expected_dims.keys())
    assert actual_dim_keys == expected_dim_keys, (
        f"[{footprint_id}] field 'body.dims' keys: {sorted(actual_dim_keys)} != {sorted(expected_dim_keys)}"
    )
    for key, expected_value in expected_dims.items():
        actual_value = actual.body.dims[key]
        assert actual_value == expected_value, (
            f"[{footprint_id}] body.dims[{key!r}]: {actual_value!r} != {expected_value!r}"
        )

    assert actual.lead_diameter == expected["leadDiameter"], (
        f"[{footprint_id}] field 'leadDiameter': {actual.lead_diameter!r} != {expected['leadDiameter']!r}"
    )
    assert actual.polarized == expected["polarized"], (
        f"[{footprint_id}] field 'polarized': {actual.polarized!r} != {expected['polarized']!r}"
    )


# ---------------------------------------------------------------------------
# Targeted regressions for the two documented traps
# ---------------------------------------------------------------------------


def test_dip8_pin_numbering_runs_down_then_up() -> None:
    """DIP-8: pin 1 at (0,0); numbering runs DOWN the left column then back UP the
    right column, so pin 8 ends up directly across from pin 1 (same row), and pin
    5 is the diagonal opposite (bottom of the right column).
    """
    fp = dip_footprint(pin_count=8, id="dip-8-check", name="DIP-8 check")
    by_number = {p.number: p for p in fp.pins}

    assert (by_number["1"].d_col, by_number["1"].d_row) == (0, 0)
    assert (by_number["4"].d_col, by_number["4"].d_row) == (0, 3), "pin 4: bottom of left column"
    assert (by_number["5"].d_col, by_number["5"].d_row) == (3, 3), "pin 5: diagonal opposite of pin 1"
    assert (by_number["8"].d_col, by_number["8"].d_row) == (3, 0), "pin 8: directly across from pin 1"


def test_body_outline_encloses_every_pin_for_every_footprint() -> None:
    """bodyOutline doubles as the courtyard for overlap DRC, so its bounding box
    must contain every pin position (converted to mm) for every footprint in the
    standard registry.
    """
    offenders: list[str] = []
    for footprint_id, fp in REGISTRY.items():
        xs = [p.x for p in fp.body_outline]
        ys = [p.y for p in fp.body_outline]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        for pin in fp.pins:
            px = pin.d_col * STANDARD_PITCH_MM
            py = pin.d_row * STANDARD_PITCH_MM
            if not (min_x <= px <= max_x and min_y <= py <= max_y):
                offenders.append(f"{footprint_id}: pin {pin.number} at ({px}, {py}) outside outline bbox")
    assert not offenders, "pins outside body_outline bounding box:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def test_get_footprint_returns_matching_id() -> None:
    fp = get_footprint("dip-8")
    assert fp is not None
    assert fp.id == "dip-8"


def test_get_footprint_unknown_id_returns_none() -> None:
    assert get_footprint("does-not-exist") is None


def test_footprint_lookup_is_equivalent_to_get_footprint() -> None:
    lookup = footprint_lookup()
    assert lookup("to220") is get_footprint("to220")
    assert lookup("does-not-exist") is None


def test_standard_footprints_is_cached_across_calls() -> None:
    assert standard_footprints() is standard_footprints()


def test_registry_has_no_duplicate_ids_by_construction() -> None:
    # dict construction itself can't produce duplicates, but this documents the
    # invariant the TS source enforces explicitly (throws on a duplicate id).
    assert len(REGISTRY) == len(set(REGISTRY.keys()))


# ---------------------------------------------------------------------------
# Argument validation, ported from the TS generators' thrown errors
# ---------------------------------------------------------------------------


def test_dip_footprint_rejects_odd_pin_count() -> None:
    with pytest.raises(ValueError):
        dip_footprint(pin_count=7)


def test_dip_footprint_rejects_too_few_pins() -> None:
    with pytest.raises(ValueError):
        dip_footprint(pin_count=2)


def test_pin_header_footprint_rejects_non_positive_cols() -> None:
    with pytest.raises(ValueError):
        pin_header_footprint(rows=1, cols=0)


def test_screw_terminal_footprint_rejects_too_few_ways() -> None:
    with pytest.raises(ValueError):
        screw_terminal_footprint(ways=1)


# ---------------------------------------------------------------------------
# Every generator is directly callable and returns a Footprint (defaults path,
# not exercised by the standard registry since it always passes explicit
# id/name -- this covers the "no id/name given" branch of each function).
# ---------------------------------------------------------------------------


def test_every_generator_is_callable_with_only_required_params() -> None:
    generators_and_calls = [
        lambda: axial_footprint(span_holes=4, body_length_mm=6.3, body_diameter_mm=2.3),
        lambda: radial_electrolytic_footprint(pitch_holes=2, can_diameter_mm=5, can_height_mm=7),
        lambda: disc_ceramic_footprint(pitch_holes=2, body_diameter_mm=5),
        lambda: box_film_capacitor_footprint(
            pitch_holes=2, body_length_mm=7, body_width_mm=4, body_height_mm=6
        ),
        lambda: dip_footprint(pin_count=8),
        lambda: to92_footprint(),
        lambda: to220_footprint(),
        lambda: led_footprint(diameter_mm=5),
        lambda: pin_header_footprint(rows=2, cols=4),
        lambda: screw_terminal_footprint(ways=2),
        lambda: potentiometer_footprint(),
        lambda: tactile_switch_footprint(),
        lambda: crystal_hc49_footprint(),
        lambda: relay_footprint(),
    ]
    for call in generators_and_calls:
        fp = call()
        assert isinstance(fp, Footprint)
        assert fp.id
        assert fp.name
        assert len(fp.pins) > 0
        assert len(fp.body_outline) > 0
# ---------------------------------------------------------------------------
# Footprints the library does not have: the id IS the definition
# ---------------------------------------------------------------------------
#
# Sixty-one footprints is a good library and not every part anybody owns, and a part the
# library does not have could not be placed at all. A custom footprint is asked for by an
# id that carries its own parameters, so nothing new is stored: the .perf format does not
# move, the fifteen golden fixtures above are untouched, and a board mailed to a stranger
# opens with the same part on it.
#
# The property everything here rests on is that the mapping is ONE-TO-ONE IN BOTH
# DIRECTIONS. A generator writes an id that reproduces it, and an id builds a footprint
# whose own id is what was asked for. Either half failing puts a footprint in a document
# under a name that is not its own.


def _generated_examples() -> list[Footprint]:
    """One of every family, built by calling the generator with no id at all.

    Deliberately built rather than listed: these are the ids the generators write FOR
    THEMSELVES, so the round trip below is testing the two halves against each other and
    not against a list somebody kept in step by hand.
    """
    return [
        generic_box_footprint(cols=4, rows=2, col_step=1, row_step=3, width_mm=15, depth_mm=10, height_mm=8),
        generic_box_footprint(cols=6, rows=1, width_mm=20.32, depth_mm=6.5, height_mm=3),
        generic_box_footprint(cols=1, rows=1, width_mm=5, depth_mm=5, height_mm=5),
        generic_box_footprint(cols=8, rows=2, col_step=2, row_step=4, width_mm=40, depth_mm=12, height_mm=9.5),
        dip_footprint(pin_count=22),
        dip_footprint(pin_count=28, wide=True),
        pin_header_footprint(rows=1, cols=40),
        pin_header_footprint(rows=2, cols=13),
        screw_terminal_footprint(ways=6),
        axial_footprint(span_holes=8, body_length_mm=12, body_diameter_mm=5),
        axial_footprint(span_holes=5, body_length_mm=7.5, body_diameter_mm=3.2, polarized=True),
        radial_electrolytic_footprint(pitch_holes=5, can_diameter_mm=16, can_height_mm=25),
        disc_ceramic_footprint(pitch_holes=2, body_diameter_mm=7, body_thickness_mm=3),
        box_film_capacitor_footprint(pitch_holes=3, body_length_mm=10, body_width_mm=5, body_height_mm=8),
        led_footprint(diameter_mm=10),
    ]


@pytest.mark.parametrize("footprint", _generated_examples(), ids=lambda f: f.id)
def test_the_id_a_generator_writes_for_itself_rebuilds_it(footprint: Footprint) -> None:
    """Field for field, not "looks similar".

    This is the whole feature in one assertion. The id in a .perf file is all anybody has;
    if reading it back produces a different part, the board on the screen and the board the
    document describes are two different boards.
    """
    rebuilt = get_footprint(footprint.id)
    assert rebuilt is not None, f"{footprint.id} does not parse back"
    assert rebuilt == footprint


@pytest.mark.parametrize("footprint_id", sorted(standard_footprints()))
def test_the_registry_always_wins_over_the_grammar(footprint_id: str) -> None:
    """`dip-8` is the library's DIP-8, not something rebuilt from its own name.

    Several registry ids are spelled exactly like a parametric one, which is how they came
    to be spelled at all. If the grammar could shadow one, a board saved before a part
    joined the library would open as a different part afterwards -- and the difference would
    be a lead diameter or a body height nobody would look for.
    """
    assert get_footprint(footprint_id) is standard_footprints()[footprint_id]


@pytest.mark.parametrize(
    "spelling",
    [
        "dip-08",                    # a leading zero
        "led-05mm",
        "c-elec-d8.0-p3-h11",        # a trailing zero
        "box-2x1-p01-r1-5x5x5",
        "box-4x2-p1-r3-15.00x10x8",
        "hdr-1x040",
    ],
)
def test_one_spelling_per_part(spelling: str) -> None:
    """Refused rather than accepted-and-renamed.

    Every parse ends by rebuilding the id and comparing it, so a spelling the generators
    would never write is not a footprint. Without that check `dip-08` builds a DIP-8 and the
    document holds a part whose own id is `dip-8`, which is a footprint that disagrees with
    the name it is stored under -- and two ids that mean one part.
    """
    assert get_footprint(spelling) is None


@pytest.mark.parametrize(
    "nonsense",
    [
        "",
        "nonsense",
        "dip",
        "dip-7",                     # a DIP has an even pin count
        "dip-8-oops",                # the pattern is anchored
        "screw-terminal-1",          # one way is not a terminal block
        "led-4mm",                   # no height is recorded for a 4 mm LED
        "box-0x2-p1-r1-5x5x5",
        "box-999x999-p1-r1-5x5x5",   # a million pins
        "box-4x2-p1-r99-5x5x5",
        "box-4x2-p1-r1-5x5x9999",
        "axial-3h-0x2",
        "c-elec-d16-p5",             # the height is not optional
        "hdr-1x40x2",
    ],
)
def test_an_id_that_means_nothing_is_nothing_rather_than_an_error(nonsense: str) -> None:
    """`None`, never a raise. An unknown footprint is a case every consumer already
    handles -- the guide warns, the schematic dashes the symbol, the 2D view falls back to a
    plain pad -- and a raise would turn a typo in a hand-edited file into a crash on load."""
    assert get_footprint(nonsense) is None
    assert generated_footprint(nonsense) is None


def test_a_generated_box_anchors_pin_one_and_numbers_straight_across() -> None:
    """The anchor convention every footprint here keeps, and the numbering this one has to
    choose. Straight across is what a module's silkscreen does; a DIP goes anti-clockwise,
    and `dip_footprint` is what to use when that is the answer."""
    box = generic_box_footprint(cols=3, rows=2, row_step=4, width_mm=10, depth_mm=12, height_mm=5)

    assert [(p.number, p.d_col, p.d_row) for p in box.pins] == [
        ("1", 0, 0), ("2", 1, 0), ("3", 2, 0),
        ("4", 0, 4), ("5", 1, 4), ("6", 2, 4),
    ]


def test_a_single_line_of_pins_has_exactly_one_spelling() -> None:
    """With one row there is no row spacing to describe, so it is forced to 1 rather than
    left free -- otherwise `box-6x1-p1-r1-...` and `box-6x1-p1-r9-...` are the same part
    under two names, and a document could hold either."""
    for row_step in (1, 2, 7):
        box = generic_box_footprint(
            cols=6, rows=1, row_step=row_step, width_mm=15, depth_mm=5, height_mm=4
        )
        assert box.id == "box-6x1-p1-r1-15x5x4"
    for col_step in (1, 3, 9):
        column = generic_box_footprint(
            cols=1, rows=4, col_step=col_step, width_mm=5, depth_mm=12, height_mm=4
        )
        assert column.id == "box-1x4-p1-r1-5x12x4"


@pytest.mark.parametrize("footprint", _generated_examples(), ids=lambda f: f.id)
def test_a_generated_footprint_is_as_complete_as_a_library_one(footprint: Footprint) -> None:
    """Everything downstream assumes these invariants of registry footprints, and a
    generated one reaches exactly the same code: DRC, the placer, the guide, both
    renderers."""
    numbers = [pin.number for pin in footprint.pins]
    assert len(set(numbers)) == len(numbers), "pin numbers must be unique"
    assert numbers[0] == "1"
    assert (footprint.pins[0].d_col, footprint.pins[0].d_row) == (0, 0), "pin 1 is the anchor"
    assert len(footprint.body_outline) >= 4
    assert footprint.body_height > 0
    assert footprint.lead_diameter > 0
    assert footprint.name

    # The courtyard is the generous of the two shapes DRC has, so it must contain the pins.
    xs = [point.x for point in footprint.body_outline]
    ys = [point.y for point in footprint.body_outline]
    for pin in footprint.pins:
        assert min(xs) <= pin.d_col * STANDARD_PITCH_MM <= max(xs)
        assert min(ys) <= pin.d_row * STANDARD_PITCH_MM <= max(ys)


def test_the_grammar_names_every_family_the_parser_accepts() -> None:
    """`GENERATED_ID_GRAMMAR` is the only thing that tells a person or an agent what to
    type, so a family added to the parser and left out of it is a feature nobody can reach.
    The same shape of check as the MCP tool table's."""
    from perfstudio.footprints import _GENERATED

    for pattern, _ in _GENERATED:
        prefix = pattern.pattern.lstrip("^").split("(")[0].split("\\")[0]
        assert prefix and prefix in GENERATED_ID_GRAMMAR, (
            f"the grammar does not mention {prefix!r}"
        )


def test_a_custom_part_can_actually_be_built() -> None:
    """The end of the road, and the reason the feature exists.

    A footprint that parses is not the point -- a part that can be placed, wired, checked
    and SOLDERED is. This puts a part the library has never heard of on a board through the
    command bus and asks the build guide about it, because `unknown-footprint` is exactly
    the warning a user with an odd part used to get and had no way to answer.
    """
    from perfstudio.command import CommandBus, CommandContext, create_id_generator
    from perfstudio.commands import PlaceComponentPayload, create_standard_registry
    from perfstudio.guide import build_guide
    from perfstudio.model import Board, DocumentMeta, HoleCoord, PerfDocument

    board = Board(
        type="pad-per-hole", cols=30, rows=20, pitch=2.54, thickness=1.6,
        material="FR4", pad_diameter=1.9, drill_diameter=0.8,
    )
    document = PerfDocument(
        meta=DocumentMeta(name="odd part", created="", modified=""), board=board
    )
    bus = CommandBus(
        document,
        create_standard_registry(),
        CommandContext(next_id=create_id_generator()),
    )
    result = bus.dispatch(
        "component.place",
        PlaceComponentPayload(
            ref="M1",
            value="sensor",
            footprint_id="box-4x2-p1-r3-15x10x8",
            anchor=HoleCoord(col=4, row=4),
        ),
    )
    assert result.ok, result.message

    guide = build_guide(bus.document, footprint_lookup())
    assert not [w for w in guide.warnings if w.code == "unknown-footprint"], guide.warnings
    assert guide.part_steps == 1
    steps = [step for phase in guide.phases for step in phase.steps]
    assert any("M1" in step.title for step in steps), [s.title for s in steps]
    assert any("M1" in line.ref_designators for line in guide.bom)
