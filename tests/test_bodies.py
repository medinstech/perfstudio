"""Tests for parametric component bodies (src/perfstudio/ui/bodies.py).

The load-bearing test here is the courtyard invariant: every one of the sixty-one registry
footprints must produce a body that fits inside its own ``body_outline``. That single check
is what keeps the dimension table honest, because the registry names its dimensions from each
package's own datasheet and those names do NOT agree with each other about orientation -- a
relay's "length" is its x extent, a DIP's is its y extent. It is also what caught the relay
and the sideways TO-220 while this was being written.

The rest pin down the details that make a part identifiable rather than merely present: real
dimensions instead of the padded courtyard, a body centred on its own pins, leads only where
a lead is actually visible, and a polarity mark on everything that has one.
"""

from __future__ import annotations

import pytest

from perfstudio.footprints import get_footprint, standard_footprints
from perfstudio.model import BodyArchetype
from perfstudio.ui.bodies import (
    BODY_STYLES,
    leads_for,
    parse_resistance,
    placement_for,
    polarity_pin_offset,
    resistance_bands,
    resistor_bands,
    style_for,
    surface_for,
)

PITCH = 2.54
ALL_FOOTPRINTS = standard_footprints()


def _courtyard(footprint_id: str) -> tuple[float, float, float, float]:
    footprint = ALL_FOOTPRINTS[footprint_id]
    xs = [p.x for p in footprint.body_outline]
    ys = [p.y for p in footprint.body_outline]
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("footprint_id", sorted(ALL_FOOTPRINTS))
def test_every_body_fits_inside_its_own_courtyard(footprint_id: str) -> None:
    """A body larger than the courtyard DRC checks would mean the drawing and the rules
    disagree about how much board a part occupies -- and the drawing would be the wrong one,
    since the courtyard is deliberately the generous of the two."""
    footprint = ALL_FOOTPRINTS[footprint_id]
    if not footprint.body_outline:
        pytest.skip("no courtyard to compare against")
    placement = placement_for(footprint, PITCH)
    min_x, min_y, max_x, max_y = _courtyard(footprint_id)

    assert placement.centre_x - placement.size_x / 2 >= min_x - 0.01
    assert placement.centre_x + placement.size_x / 2 <= max_x + 0.01
    assert placement.centre_y - placement.size_y / 2 >= min_y - 0.01
    assert placement.centre_y + placement.size_y / 2 <= max_y + 0.01


@pytest.mark.parametrize("footprint_id", sorted(ALL_FOOTPRINTS))
def test_every_body_is_centred_on_its_own_pins(footprint_id: str) -> None:
    """One rule for every archetype: the pin centroid. It is the midpoint of a two-lead part,
    the centre of a DIP's pin rectangle and the middle pin of a TO-220, so no archetype needs
    a table of offsets that could drift out of step with the registry."""
    footprint = ALL_FOOTPRINTS[footprint_id]
    placement = placement_for(footprint, PITCH)
    xs = [pin.d_col * PITCH for pin in footprint.pins]
    ys = [pin.d_row * PITCH for pin in footprint.pins]

    assert placement.centre_x == pytest.approx((min(xs) + max(xs)) / 2)
    assert placement.centre_y == pytest.approx((min(ys) + max(ys)) / 2)


@pytest.mark.parametrize("footprint_id", sorted(ALL_FOOTPRINTS))
def test_no_body_is_degenerate(footprint_id: str) -> None:
    """Some registry entries record a zero dimension -- a 1x1 header has length 0.0 -- and a
    zero-sized body is invisible rather than small."""
    placement = placement_for(ALL_FOOTPRINTS[footprint_id], PITCH)

    assert placement.size_x > 0
    assert placement.size_y > 0
    assert placement.height > 0


# ---------------------------------------------------------------------------
# The body is not the courtyard
# ---------------------------------------------------------------------------


def test_a_resistor_body_is_its_real_length_not_its_courtyard() -> None:
    """The specific mistake that made every part a blob: r-axial-3's courtyard spans 10.16 mm
    while the resistor itself is 5 mm long."""
    footprint = get_footprint("r-axial-3")
    assert footprint is not None

    placement = placement_for(footprint, PITCH)

    assert placement.size_x == pytest.approx(5.0)
    assert placement.size_y == pytest.approx(2.0)
    min_x, _min_y, max_x, _max_y = _courtyard("r-axial-3")
    assert max_x - min_x == pytest.approx(10.16)  # ...and the courtyard is twice that long.


def test_a_to220_is_wider_than_it_is_deep() -> None:
    """Its three pins run along the 10 mm face. Read as (length, width) from key names, this
    came out 5x10 -- the package turned sideways."""
    footprint = get_footprint("to220")
    assert footprint is not None

    placement = placement_for(footprint, PITCH)

    assert placement.size_x == pytest.approx(10.0)
    assert placement.size_y == pytest.approx(4.6)


def test_a_dip_runs_along_its_pin_columns() -> None:
    """A DIP-8's pin block is square, so no span comparison can decide its orientation. Its
    length lies along the four-pin columns and its rowSpacing across them."""
    footprint = get_footprint("dip-8")
    assert footprint is not None

    placement = placement_for(footprint, PITCH)

    assert placement.axis == "y"
    assert placement.size_y == pytest.approx(9.82)
    assert placement.size_x == pytest.approx(7.62)


def test_a_two_row_header_is_two_holes_deep() -> None:
    """The registry records a header's width as 0.0 because the moulding is one hole wide per
    ROW, so the body has to come from the pins rather than from dims."""
    single = get_footprint("hdr-1x4")
    double = get_footprint("hdr-2x5")
    assert single is not None and double is not None

    assert placement_for(single, PITCH).size_y == pytest.approx(PITCH)
    assert placement_for(double, PITCH).size_y == pytest.approx(2 * PITCH)


def test_a_disc_ceramic_is_a_slab_from_above_not_a_circle() -> None:
    """It stands on edge: the disc face is what you see from the side. Drawing a circle in the
    top view would claim it covers twice the board it does."""
    footprint = get_footprint("c-disc-p2")
    assert footprint is not None

    placement = placement_for(footprint, PITCH)

    assert placement.silhouette == "rounded"
    assert placement.size_x == pytest.approx(5.0)
    assert placement.size_y == pytest.approx(2.5)


def test_pitch_is_honoured_rather_than_assumed() -> None:
    """Board.pitch is a field. A body placed on an assumed 2.54 would drift off its own pins
    on any board that sets it differently."""
    footprint = get_footprint("r-axial-3")
    assert footprint is not None

    standard = placement_for(footprint, 2.54)
    metric = placement_for(footprint, 2.0)

    assert metric.centre_x == pytest.approx(standard.centre_x * 2.0 / 2.54)
    # The body's own size comes from dims in millimetres, so it does not scale with pitch.
    assert metric.size_x == pytest.approx(standard.size_x)


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


def test_an_axial_part_has_a_lead_at_each_end() -> None:
    footprint = get_footprint("r-axial-5")
    assert footprint is not None
    placement = placement_for(footprint, PITCH)

    leads = leads_for(footprint, placement, PITCH)

    assert len(leads) == 2
    # Each lead starts on a pin and ends on the body edge.
    half = placement.size_x / 2
    for lead in leads:
        assert abs(lead.to_x - placement.centre_x) == pytest.approx(half)


def test_a_part_whose_body_covers_its_pins_has_no_visible_leads() -> None:
    for footprint_id in ("dip-8", "hdr-1x4", "c-elec-d5-p2"):
        footprint = ALL_FOOTPRINTS[footprint_id]
        placement = placement_for(footprint, PITCH)

        assert leads_for(footprint, placement, PITCH) == (), footprint_id


# ---------------------------------------------------------------------------
# Polarity
# ---------------------------------------------------------------------------


def test_polarized_parts_report_their_keyed_pin() -> None:
    for footprint_id in ("d-do41", "led-5mm", "c-elec-d5-p2"):
        footprint = ALL_FOOTPRINTS[footprint_id]

        assert polarity_pin_offset(footprint, PITCH) is not None, footprint_id


def test_a_dip_is_keyed_even_though_the_registry_calls_it_unpolarized() -> None:
    """`polarized` records ELECTRICAL polarity, which a DIP does not have -- and fitting one
    backwards still destroys it, so its pin-1 dot has to be drawn anyway."""
    footprint = get_footprint("dip-8")
    assert footprint is not None
    assert footprint.polarized is False

    assert polarity_pin_offset(footprint, PITCH) == (0.0, 0.0)


def test_a_plain_resistor_is_not_keyed() -> None:
    footprint = get_footprint("r-axial-3")
    assert footprint is not None

    assert polarity_pin_offset(footprint, PITCH) is None


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


def test_every_archetype_has_a_style() -> None:
    """A missing entry would silently fall back to grey, which is the look this replaced."""
    declared = set(BodyArchetype.__args__)  # type: ignore[attr-defined]

    assert declared == set(BODY_STYLES)


def test_a_diode_and_a_resistor_do_not_look_alike() -> None:
    """They share the axial-cylinder archetype and are nothing like each other to look at."""
    resistor = get_footprint("r-axial-3")
    diode = get_footprint("d-do41")
    assert resistor is not None and diode is not None

    assert style_for(resistor).fill != style_for(diode).fill


def test_a_material_is_shaded_from_its_own_flags() -> None:
    """``metallic`` and ``lens`` were documented as shading hints for both views and read by
    almost neither -- the HC-49 crystal hardcoded its own metal shading while carrying the
    flag that says so, and no LED was ever lit like a lens. Two sources for one fact."""
    crystal = surface_for(BODY_STYLES["crystal-hc49"])
    led = surface_for(BODY_STYLES["led-round"])
    plastic = surface_for(BODY_STYLES["dip"])

    assert crystal.specular > plastic.specular
    assert led.specular > crystal.specular  # A lens transmits; metal only reflects.
    assert led.sheen > crystal.sheen > plastic.sheen


# ---------------------------------------------------------------------------
# The resistor colour code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "ohms"),
    [
        ("470", 470.0),
        ("470R", 470.0),
        ("4R7", 4.7),
        ("10k", 10_000.0),
        ("4k7", 4_700.0),
        ("2.2k", 2_200.0),
        ("1M", 1_000_000.0),
        ("2M2", 2_200_000.0),
        ("100k", 100_000.0),
        ("330 ohm", 330.0),
        ("1kΩ", 1_000.0),
    ],
)
def test_a_resistance_is_read_the_way_a_schematic_writes_it(value: str, ohms: float) -> None:
    """The unit letter stands in for the decimal point -- `4k7` is 4.7 k -- which is why this
    cannot be a float() call."""
    assert parse_resistance(value) == pytest.approx(ohms)


@pytest.mark.parametrize(
    "value", ["100nF", "10uF", "0.01uF", "10uH", "2A", "NE555", "LED", "Conn_01x02", "", "  ", "v12"]
)
def test_anything_that_is_not_a_resistance_gets_no_bands(value: str) -> None:
    """THE load-bearing test here. A wrong band is worse than no band: somebody would read it
    and fit the wrong part. Every one of these appears as a `value` in the golden fixtures, and
    a parser that merely stripped letters would decode most of them as resistances."""
    assert parse_resistance(value) is None


@pytest.mark.parametrize(
    ("ohms", "expected"),
    [
        (10_000.0, ("brown", "black", "orange")),
        (4_700.0, ("yellow", "violet", "red")),
        (470.0, ("yellow", "violet", "brown")),
        (330.0, ("orange", "orange", "brown")),
        (1_000_000.0, ("brown", "black", "green")),
        (47.0, ("yellow", "violet", "black")),
        (4.7, ("yellow", "violet", "gold")),
    ],
)
def test_the_bands_match_the_real_colour_code(ohms: float, expected: tuple[str, ...]) -> None:
    """Checked against the code printed on real parts: 10k is brown-black-orange, and a
    resistor that reads any other way on screen is worse than an unmarked one."""
    names = {
        "#141519": "black",
        "#6b4423": "brown",
        "#c62f2a": "red",
        "#e2701f": "orange",
        "#efc430": "yellow",
        "#3c8f45": "green",
        "#2062c4": "blue",
        "#7a3fa3": "violet",
        "#8f959d": "grey",
        "#f2f4f8": "white",
        "#c9a227": "gold",
        "#c6cad1": "silver",
    }
    bands = resistance_bands(ohms)
    assert bands is not None

    assert tuple(names[c] for c in bands[:3]) == expected
    assert names[bands[3]] == "gold"  # The E24 tolerance band, and the one that says which
    #                                   end to start reading from.


def test_a_resistor_is_banded_and_a_diode_is_not() -> None:
    """They share the axial-cylinder archetype. A diode carries a cathode stripe, and bands
    painted on top of it would say something the part does not."""
    resistor = get_footprint("r-axial-4")
    diode = get_footprint("d-do41")
    assert resistor is not None and diode is not None

    assert resistor_bands(resistor, "10k") is not None
    assert resistor_bands(diode, "10k") is None


def test_a_part_that_is_not_axial_is_never_banded() -> None:
    for footprint_id in ("dip-8", "c-elec-d5-p2", "led-5mm"):
        assert resistor_bands(ALL_FOOTPRINTS[footprint_id], "10k") is None, footprint_id


def test_an_explicit_colour_overrides_the_fill_and_nothing_else() -> None:
    """A caller setting a colour is saying "this part is green", not asking for a whole new
    palette -- the edge and accent exist to stay legible against the fill."""
    import dataclasses

    footprint = get_footprint("dip-8")
    assert footprint is not None
    base = style_for(footprint)
    recoloured = dataclasses.replace(
        footprint, body=dataclasses.replace(footprint.body, color="#00ff00")
    )

    style = style_for(recoloured)

    assert style.fill == "#00ff00"
    assert (style.edge, style.accent) == (base.edge, base.accent)
