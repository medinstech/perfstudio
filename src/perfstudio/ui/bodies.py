"""What each component archetype looks like, shared by the 2D and 3D views.

WHY THIS EXISTS AT ALL. The footprint library already describes every part properly:
``BodySpec.archetype`` says what kind of thing it is and ``BodySpec.dims`` gives its real
millimetre dimensions, for fifteen archetypes across sixty-one footprints. Both renderers
threw all of it away -- 3D extruded one grey cube per part and 2D filled one pale polygon --
so a DIP-8, a 10 mm electrolytic and a quarter-watt resistor were the same anonymous blob in
both views. Nothing was missing from the data; it simply was not being read.

THE OUTLINE IS NOT THE BODY. This is the specific mistake that made everything look wrong.
``Footprint.body_outline`` is the COURTYARD: a deliberately padded boundary around the pins
that DRC uses for overlap checks. For ``r-axial-3`` it spans 10.16 mm while the resistor
body is 5 mm long, so drawing the outline draws a box half again too big, with no leads and
no shape. The real body comes from ``dims`` and is centred on the pins -- which is what
:func:`placement_for` computes, and why the parts now have leads with a body between them
instead of a rectangle covering both.

ONE TABLE, TWO RENDERERS. Colours live here rather than in either view so that a resistor is
the same beige in the editor, in the 3D view and in the build guide's step images. A part
that changed colour when the user turned the board over would undermine the one job the 3D
view has, which is to let someone check that what they are about to solder matches what they
meant (PLAN.md Sec 8.4).

NO ASSET LIBRARY, BY DECISION. PLAN.md's D6 fixes this as parametric generation: zero
assets, a body that cannot disagree with its own footprint, and no share-alike licence
inherited into an Apache-2.0 project (Sec 13 lists licence contamination as a high risk).
Everything here is derived from numbers already in the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from perfstudio.model import BodyArchetype, Footprint

# ---------------------------------------------------------------------------
# Appearance
# ---------------------------------------------------------------------------

#: 2D silhouette shape. The 3D view builds a solid per archetype and does not use this.
Silhouette: TypeAlias = Literal["rect", "circle", "dcut", "rounded"]


@dataclass(frozen=True, slots=True)
class BodyStyle:
    """How one archetype is coloured, in both views."""

    fill: str
    edge: str
    #: Stripes, bands, polarity marks and pin-1 dots. Always readable against ``fill``.
    accent: str
    #: 3D shading hint: a metal can and a plastic case catch light very differently, and
    #: that difference is most of what makes a rendered board look like a board.
    metallic: bool = False
    #: A lens rather than a case. Drawn lighter in 2D and given a highlight in 3D.
    lens: bool = False


_PLASTIC_EDGE = "#0e0f13"

BODY_STYLES: dict[BodyArchetype, BodyStyle] = {
    # Axial parts split by polarity below -- a resistor and a DO-41 diode share an
    # archetype and look nothing alike.
    "axial-cylinder": BodyStyle(fill="#d9c9a1", edge="#7d6c46", accent="#3b2d18"),
    "radial-electrolytic": BodyStyle(fill="#1f2a44", edge="#0b1120", accent="#c9d2e0"),
    "disc-ceramic": BodyStyle(fill="#b9884a", edge="#6d4c25", accent="#2c1d0c"),
    "box-film": BodyStyle(fill="#c2652f", edge="#6f3617", accent="#f0e3d2"),
    "dip": BodyStyle(fill="#24262d", edge=_PLASTIC_EDGE, accent="#c3c8d2"),
    "to92": BodyStyle(fill="#26282e", edge=_PLASTIC_EDGE, accent="#c3c8d2"),
    "to220": BodyStyle(fill="#22242a", edge=_PLASTIC_EDGE, accent="#9aa3ad"),
    "led-round": BodyStyle(fill="#d6392f", edge="#7d1f18", accent="#ffd9d5", lens=True),
    "pin-header": BodyStyle(fill="#1c1e24", edge=_PLASTIC_EDGE, accent="#d8b45a"),
    "screw-terminal": BodyStyle(fill="#2f7d4f", edge="#164a2c", accent="#c8ccd2"),
    "potentiometer": BodyStyle(fill="#2b4f8f", edge="#14264a", accent="#b9c0ca"),
    "tactile-switch": BodyStyle(fill="#22242a", edge=_PLASTIC_EDGE, accent="#e6e2d6"),
    "crystal-hc49": BodyStyle(fill="#a8b0ba", edge="#5a626c", accent="#3a4048", metallic=True),
    "relay-box": BodyStyle(fill="#46566f", edge="#22303f", accent="#c8ccd2"),
    "generic-box": BodyStyle(fill="#6b7280", edge="#343a44", accent="#e6e8ec"),
}

#: A polarized axial part is a diode, not a resistor. Same archetype, different object.
_DIODE_STYLE = BodyStyle(fill="#1d1f24", edge=_PLASTIC_EDGE, accent="#e8ecf2")

_FALLBACK_STYLE = BODY_STYLES["generic-box"]


def style_for(footprint: Footprint) -> BodyStyle:
    """The style for a footprint, honouring an explicit ``BodySpec.color`` override.

    The override replaces the fill only. A caller setting a colour is saying "this part is
    green", not "work out a whole new palette" -- so the edge and accent, which exist to
    stay legible against the fill, are left to the archetype.
    """
    archetype = footprint.body.archetype
    if archetype == "axial-cylinder" and footprint.polarized:
        base = _DIODE_STYLE
    else:
        base = BODY_STYLES.get(archetype, _FALLBACK_STYLE)
    override = footprint.body.color
    if override:
        return BodyStyle(
            fill=override,
            edge=base.edge,
            accent=base.accent,
            metallic=base.metallic,
            lens=base.lens,
        )
    return base


# ---------------------------------------------------------------------------
# Where the body actually sits
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BodyPlacement:
    """The physical body in component-local millimetres, before rotation or mirroring.

    Local space matches the footprint's own: +x is increasing column, +y increasing row,
    origin at the anchor pin. Both views apply the component's transform themselves, so
    nothing here needs to know about rotation.
    """

    silhouette: Silhouette
    #: Centre of the body, which is the centroid of the pin holes -- see placement_for.
    centre_x: float
    centre_y: float
    #: Extents in local x and y. Never zero: a body has to be drawable.
    size_x: float
    size_y: float
    #: Above the board surface.
    height: float
    #: The direction the part's leads run, so an axial body lies along its own wires and a
    #: 3D cylinder is turned the right way.
    axis: Literal["x", "y"]
    #: True when the part has a meaningful pin 1 / cathode / negative end to mark.
    polarized: bool

    @property
    def length(self) -> float:
        """The extent along ``axis``."""
        return self.size_x if self.axis == "x" else self.size_y

    @property
    def width(self) -> float:
        """The extent across ``axis``."""
        return self.size_y if self.axis == "x" else self.size_x


_SILHOUETTES: dict[BodyArchetype, Silhouette] = {
    "axial-cylinder": "rect",
    "radial-electrolytic": "circle",
    # A disc ceramic stands on edge, so from above it is a narrow rounded slab -- the disc
    # face is what you see from the SIDE, and drawing a circle in the top view would claim
    # it occupies twice the board it does.
    "disc-ceramic": "rounded",
    "box-film": "rect",
    "dip": "rect",
    "to92": "dcut",
    "to220": "rect",
    "led-round": "dcut",
    "pin-header": "rect",
    "screw-terminal": "rect",
    "potentiometer": "circle",
    "tactile-switch": "rect",
    "crystal-hc49": "rounded",
    "relay-box": "rounded",
    "generic-box": "rect",
}

#: Nothing is drawn thinner than this. Guards the degenerate registry entries -- a 1x1 pin
#: header records length 0.0, and a zero-sized body is invisible rather than small.
_MIN_MM = 1.2

#: Per archetype: (``dims`` key for the LOCAL X extent, key for the LOCAL Y extent, the axis
#: the part's leads run along).
#:
#: Mapped straight to x and y rather than to "along" and "across", because the registry's
#: dimension names follow each package's own datasheet and DO NOT agree with each other about
#: orientation:
#:
#:   - ``relay_footprint`` builds its courtyard as ``_rect_outline(pins, 19, 15, ...)``, and
#:     that helper takes x first -- so a relay's ``length`` is its X extent.
#:   - ``dip_footprint`` derives ``length`` from the pin COLUMN span, which runs down y, and
#:     ``rowSpacing`` is the gap between the two columns, across x. Exactly the other way
#:     round.
#:
#: So no rule of the form "length is the long side" or "length runs along the pins" can be
#: right for both, and inferring the axis from the pin layout is no better: a DIP-8's pin
#: block is square, which makes any span comparison a coin toss on the one archetype where
#: being wrong turns the package sideways. The invariant that keeps this table honest is that
#: every body must fit inside its own courtyard, which tests/test_ui.py checks for all 61
#: registry footprints -- that check is what caught the relay.
_DIM_KEYS: dict[BodyArchetype, tuple[str, str, Literal["x", "y"]]] = {
    "axial-cylinder": ("length", "diameter", "x"),
    "radial-electrolytic": ("diameter", "diameter", "x"),
    "disc-ceramic": ("diameter", "thickness", "x"),
    "box-film": ("length", "width", "x"),
    "dip": ("rowSpacing", "length", "y"),
    "to92": ("width", "depth", "x"),
    "to220": ("width", "depth", "x"),
    "led-round": ("diameter", "diameter", "x"),
    "screw-terminal": ("length", "width", "x"),
    "potentiometer": ("diameter", "diameter", "x"),
    "tactile-switch": ("width", "depth", "x"),
    "crystal-hc49": ("width", "depth", "x"),
    "relay-box": ("length", "width", "x"),
}


def placement_for(footprint: Footprint, pitch: float) -> BodyPlacement:
    """Work out the real body from ``dims``, centred on the part's pins.

    ``pitch`` converts the footprint's grid-step pin offsets to millimetres. Passed in
    rather than assumed to be 2.54, because ``Board.pitch`` is a field and a body placed on
    an assumed pitch would drift off its own pins on any board that sets it differently.

    Centring on the PIN CENTROID is one rule that happens to be right for every archetype
    here: it is the midpoint for a two-lead axial or radial part, the centre of the
    rectangle for a DIP, the middle pin of a TO-220, and the centre of the row for a
    header. No archetype needs a special case, which is why the bodies line up with their
    leads without a table of offsets to keep in step with the registry.

    Dimensions missing from ``dims`` fall back to the courtyard's own extent, so an
    archetype added to the registry without full dims still draws something honest rather
    than nothing.
    """
    body = footprint.body
    dims = body.dims
    archetype = body.archetype

    pins_mm = [(pin.d_col * pitch, pin.d_row * pitch) for pin in footprint.pins]
    if pins_mm:
        xs = [x for x, _ in pins_mm]
        ys = [y for _, y in pins_mm]
        centre_x = (min(xs) + max(xs)) / 2
        centre_y = (min(ys) + max(ys)) / 2
        span_x = max(xs) - min(xs)
        span_y = max(ys) - min(ys)
    else:
        centre_x = centre_y = span_x = span_y = 0.0

    outline_x, outline_y = _outline_extent(footprint)
    x_key, y_key, axis = _DIM_KEYS.get(archetype, ("length", "width", "x"))

    if archetype == "pin-header":
        # Derived from the pins rather than from dims: the registry records a header's width
        # as 0.0 because the moulding is exactly one hole wide per ROW, and a 2xN header has
        # two. Growing the pin span by one pitch in each direction gives the moulding for any
        # arrangement, single row or double.
        size_x = span_x + pitch
        size_y = span_y + pitch
        axis = "y" if span_y > span_x else "x"
    else:
        size_x = dims.get(x_key) or outline_x
        size_y = dims.get(y_key) or outline_y

    height = footprint.body_height or dims.get("height") or _MIN_MM

    return BodyPlacement(
        silhouette=_SILHOUETTES.get(archetype, "rect"),
        centre_x=centre_x,
        centre_y=centre_y,
        size_x=max(size_x, _MIN_MM),
        size_y=max(size_y, _MIN_MM),
        height=max(height, 0.6),
        axis=axis,
        polarized=footprint.polarized,
    )


def _outline_extent(footprint: Footprint) -> tuple[float, float]:
    """The courtyard's width and height, used only as a fallback for absent dims."""
    if not footprint.body_outline:
        return (_MIN_MM, _MIN_MM)
    xs = [point.x for point in footprint.body_outline]
    ys = [point.y for point in footprint.body_outline]
    return (max(xs) - min(xs), max(ys) - min(ys))


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Lead:
    """A visible lead from a pin hole to the edge of the body, in local mm."""

    from_x: float
    from_y: float
    to_x: float
    to_y: float


def leads_for(footprint: Footprint, placement: BodyPlacement, pitch: float) -> tuple[Lead, ...]:
    """Where a part's leads show between its pins and its body.

    Only for parts whose body is visibly smaller than their pin span -- a resistor bridging
    a three-hole span with a 5 mm body has a clear 2.5 mm of wire at each end, and drawing
    it is most of what makes the part read as a resistor rather than a block. Parts whose
    body already covers their pins (a DIP, a header) get none, and a can sits over its own
    leads so it gets none either.

    Each lead runs from the pin straight to the body edge along the part's axis, keeping the
    pin's own cross-axis position, so a part whose pins are not exactly on its centre line
    still gets leads that meet their pins.
    """
    if placement.silhouette == "circle" and footprint.body.archetype != "disc-ceramic":
        return ()

    leads: list[Lead] = []
    half_x = placement.size_x / 2
    half_y = placement.size_y / 2
    for pin in footprint.pins:
        pin_x, pin_y = pin.d_col * pitch, pin.d_row * pitch
        if placement.axis == "x":
            offset = pin_x - placement.centre_x
            if abs(offset) <= half_x + 0.05:
                continue  # The body already reaches this pin.
            edge_x = placement.centre_x + (half_x if offset > 0 else -half_x)
            leads.append(Lead(pin_x, pin_y, edge_x, pin_y))
        else:
            offset = pin_y - placement.centre_y
            if abs(offset) <= half_y + 0.05:
                continue
            edge_y = placement.centre_y + (half_y if offset > 0 else -half_y)
            leads.append(Lead(pin_x, pin_y, pin_x, edge_y))
    return tuple(leads)


# ---------------------------------------------------------------------------
# Polarity
# ---------------------------------------------------------------------------


#: Archetypes that are keyed even though the registry does not call them polarized. A DIP
#: has no electrical polarity -- which is what ``Footprint.polarized`` records -- and fitting
#: one backwards still destroys it, so its pin-1 dot must be drawn regardless.
_ALWAYS_KEYED: frozenset[BodyArchetype] = frozenset({"dip"})


def polarity_pin_offset(footprint: Footprint, pitch: float) -> tuple[float, float] | None:
    """Local mm of the pin that says which way round the part goes, or None if it is
    symmetrical.

    Pin 1 by convention: the cathode of a diode or LED, the positive lead of an
    electrolytic, pin 1 of a DIP. Getting this the wrong way round is the most common way a
    finished board turns out dead, so both views mark it from this one function rather than
    each deciding for itself.
    """
    if not footprint.polarized and footprint.body.archetype not in _ALWAYS_KEYED:
        return None
    for pin in footprint.pins:
        if pin.number == "1":
            return (pin.d_col * pitch, pin.d_row * pitch)
    return None


__all__ = [
    "BODY_STYLES",
    "BodyPlacement",
    "BodyStyle",
    "Lead",
    "Silhouette",
    "leads_for",
    "placement_for",
    "polarity_pin_offset",
    "style_for",
]
